from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IdentityRoute:
    identity_id: str
    session_id: str | None = None
    codex_home: str | None = None
    session_name_prefix: str = "fqg"
    verify_seconds: float | None = None
    enabled: bool = True
    allow_shared_session: bool = False
    switch_ack_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_id": self.identity_id,
            "session_id": self.session_id,
            "codex_home": self.codex_home,
            "session_name_prefix": self.session_name_prefix,
            "verify_seconds": self.verify_seconds,
            "enabled": self.enabled,
            "allow_shared_session": self.allow_shared_session,
            "switch_ack_ref": self.switch_ack_ref,
        }


class IdentityRouteTable:
    def __init__(self, route_file: Path) -> None:
        self._route_file = route_file.expanduser().resolve()
        self._lock = threading.Lock()
        self._cached_mtime_ns: int | None = None
        self._cached_routes: dict[str, IdentityRoute] = {}
        self._cached_route_issues: dict[str, str] = {}

    @property
    def route_file(self) -> Path:
        return self._route_file

    def snapshot(self) -> dict[str, IdentityRoute]:
        with self._lock:
            self._reload_if_needed()
            return dict(self._cached_routes)

    def route_issues_snapshot(self) -> dict[str, str]:
        with self._lock:
            self._reload_if_needed()
            return dict(self._cached_route_issues)

    def route_issue(self, identity_id: str) -> str | None:
        with self._lock:
            self._reload_if_needed()
            issue = self._cached_route_issues.get(identity_id)
            return issue or None

    def resolve(self, identity_id: str) -> IdentityRoute | None:
        with self._lock:
            self._reload_if_needed()
            route = self._cached_routes.get(identity_id)
            if route is None or not route.enabled:
                return None
            return route

    def _reload_if_needed(self) -> None:
        if not self._route_file.exists():
            self._cached_mtime_ns = None
            self._cached_routes = {}
            self._cached_route_issues = {}
            return

        stat = self._route_file.stat()
        if self._cached_mtime_ns == stat.st_mtime_ns:
            return

        raw = json.loads(self._route_file.read_text(encoding="utf-8"))
        identities = raw.get("identities", {})
        if not isinstance(identities, dict):
            raise ValueError("identity_routes_invalid: identities must be object")

        routes: dict[str, IdentityRoute] = {}
        for identity_id, cfg in identities.items():
            if not isinstance(identity_id, str) or not identity_id.strip():
                continue
            route = self._build_route(identity_id.strip(), cfg)
            if route is not None:
                routes[route.identity_id] = route

        route_issues = _compute_route_issues(routes)

        self._cached_mtime_ns = stat.st_mtime_ns
        self._cached_routes = routes
        self._cached_route_issues = route_issues

    def _build_route(self, identity_id: str, raw: Any) -> IdentityRoute | None:
        if not isinstance(raw, dict):
            return None

        enabled = bool(raw.get("enabled", True))
        session_id = _normalize_str(raw.get("session_id"))
        codex_home = _normalize_str(raw.get("codex_home"))
        session_name_prefix = _normalize_str(raw.get("session_name_prefix")) or "fqg"
        allow_shared_session = bool(raw.get("allow_shared_session", False))
        switch_ack_ref = _normalize_str(raw.get("switch_ack_ref"))

        verify_seconds_raw = raw.get("verify_seconds")
        verify_seconds: float | None
        if verify_seconds_raw is None:
            verify_seconds = None
        else:
            try:
                verify_seconds = max(1.0, float(verify_seconds_raw))
            except (TypeError, ValueError):
                verify_seconds = None

        return IdentityRoute(
            identity_id=identity_id,
            session_id=session_id,
            codex_home=codex_home,
            session_name_prefix=session_name_prefix,
            verify_seconds=verify_seconds,
            enabled=enabled,
            allow_shared_session=allow_shared_session,
            switch_ack_ref=switch_ack_ref,
        )


def _normalize_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _compute_route_issues(routes: dict[str, IdentityRoute]) -> dict[str, str]:
    issues: dict[str, str] = {}
    session_holders: dict[str, list[IdentityRoute]] = {}
    codex_home_holders: dict[str, list[IdentityRoute]] = {}
    for route in routes.values():
        if not route.enabled:
            continue
        session_id = (route.session_id or "").strip()
        if not session_id:
            pass
        else:
            session_holders.setdefault(session_id, []).append(route)
        codex_home = (route.codex_home or "").strip()
        if codex_home:
            codex_home_holders.setdefault(codex_home, []).append(route)

    for session_id, holders in session_holders.items():
        if len(holders) <= 1:
            continue
        ids = sorted(r.identity_id for r in holders)
        allow_shared = all(r.allow_shared_session for r in holders)
        ack_refs = {str(r.switch_ack_ref or "").strip() for r in holders}
        ack_refs.discard("")
        if not allow_shared:
            issue = f"session_id_conflict_requires_switch_ack:{session_id}:{','.join(ids)}"
            for route in holders:
                issues[route.identity_id] = issue
            continue
        if len(ack_refs) != 1:
            issue = f"switch_ack_ref_inconsistent:{session_id}:{','.join(ids)}"
            for route in holders:
                issues[route.identity_id] = issue
            continue

    for codex_home, holders in codex_home_holders.items():
        if len(holders) <= 1:
            continue
        # Keep already-detected conflicts (session-id level) as-is.
        unresolved = [r for r in holders if r.identity_id not in issues]
        if len(unresolved) <= 1:
            continue
        ids = sorted(r.identity_id for r in unresolved)
        allow_shared = all(r.allow_shared_session for r in unresolved)
        ack_refs = {str(r.switch_ack_ref or "").strip() for r in unresolved}
        ack_refs.discard("")
        if not allow_shared:
            issue = f"codex_home_conflict_requires_switch_ack:{codex_home}:{','.join(ids)}"
            for route in unresolved:
                issues[route.identity_id] = issue
            continue
        if len(ack_refs) != 1:
            issue = f"switch_ack_ref_inconsistent:{codex_home}:{','.join(ids)}"
            for route in unresolved:
                issues[route.identity_id] = issue
            continue
    return issues
