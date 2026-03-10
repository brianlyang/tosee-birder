from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APPROVAL_REQUEST_ID_PATTERN = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)


def parse_csv_values(raw: str) -> set[str]:
    if not raw.strip():
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _clean_text_for_command(raw: str) -> str:
    text = str(raw or "")
    # DingTalk may carry mentions in plain text or <at>...</at> wrappers.
    text = re.sub(r"<at\b[^>]*>.*?</at>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"@[^\s]+", " ", text)
    text = text.replace("\u2005", " ").replace("\u00a0", " ")
    return " ".join(text.split()).strip()


@dataclass(frozen=True)
class InboundChatMessage:
    msg_id: str
    text: str
    sender_id: str = ""
    chat_id: str = ""
    is_group: bool = False
    is_at_bot: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    accepted: bool
    reason: str
    normalized_message: str = ""


@dataclass(frozen=True)
class ApprovalCommand:
    action: str
    request_id: str | None = None


@dataclass(frozen=True)
class BridgePolicy:
    require_at_on_group: bool = True
    require_command_prefix: bool = False
    allow_user_ids: set[str] = field(default_factory=set)
    allow_chat_ids: set[str] = field(default_factory=set)
    command_prefixes: tuple[str, ...] = ("/run", "/cmd")

    def evaluate(self, inbound: InboundChatMessage) -> PolicyDecision:
        text = _clean_text_for_command(inbound.text)
        if not text:
            return PolicyDecision(accepted=False, reason="message_empty")

        if self.allow_user_ids and inbound.sender_id and inbound.sender_id not in self.allow_user_ids:
            return PolicyDecision(accepted=False, reason="sender_not_allowed")
        if self.allow_chat_ids and inbound.chat_id and inbound.chat_id not in self.allow_chat_ids:
            return PolicyDecision(accepted=False, reason="chat_not_allowed")
        if self.require_at_on_group and inbound.is_group and not inbound.is_at_bot:
            return PolicyDecision(accepted=False, reason="at_required")

        normalized = self._strip_prefix(text)
        if self.require_command_prefix and normalized == text:
            return PolicyDecision(accepted=False, reason="command_prefix_required")
        if not normalized.strip():
            return PolicyDecision(accepted=False, reason="command_empty")

        return PolicyDecision(accepted=True, reason="accepted", normalized_message=normalized)

    def _strip_prefix(self, text: str) -> str:
        lowered = text.lower()
        for prefix in self.command_prefixes:
            p = prefix.strip()
            if not p:
                continue
            if lowered.startswith(p.lower()):
                return text[len(p) :].strip()
        return text.strip()


class MessageDedupeStore:
    def __init__(self, path: Path, *, ttl_seconds: int = 24 * 3600, max_items: int = 5000) -> None:
        self._path = path
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._max_items = max(100, int(max_items))
        self._lock = threading.Lock()

    def check_and_mark(self, msg_id: str, *, now: float | None = None) -> bool:
        normalized = msg_id.strip()
        if not normalized:
            return True
        with self._lock:
            now_ts = float(now if now is not None else time.time())
            state = self._load_state()
            self._prune(state, now_ts)
            if normalized in state:
                self._save_state(state)
                return False
            state[normalized] = now_ts
            self._prune_max_items(state)
            self._save_state(state)
            return True

    def _load_state(self) -> dict[str, float]:
        if not self._path.exists():
            return {}
        try:
            obj = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        raw_items = obj.get("items", {})
        if not isinstance(raw_items, dict):
            return {}
        state: dict[str, float] = {}
        for key, value in raw_items.items():
            if not isinstance(key, str):
                continue
            try:
                state[key] = float(value)
            except (TypeError, ValueError):
                continue
        return state

    def _save_state(self, state: dict[str, float]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"items": state}, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def _prune(self, state: dict[str, float], now_ts: float) -> None:
        cutoff = now_ts - float(self._ttl_seconds)
        stale = [key for key, ts in state.items() if ts < cutoff]
        for key in stale:
            state.pop(key, None)

    def _prune_max_items(self, state: dict[str, float]) -> None:
        if len(state) <= self._max_items:
            return
        sorted_items = sorted(state.items(), key=lambda item: item[1], reverse=True)
        keep = dict(sorted_items[: self._max_items])
        state.clear()
        state.update(keep)


class LeaderCommandTimeout(RuntimeError):
    """Raised when leader command dispatch hits an HTTP timeout."""


class LeaderCommandClient:
    def __init__(self, *, base_url: str, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = max(3, int(timeout_seconds))

    def send_leader_command(
        self,
        *,
        message: str,
        auto_collab: bool = True,
        metadata: dict[str, Any] | None = None,
        verify_seconds: float | None = None,
        collab_verify_seconds: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": message,
            "auto_collab": auto_collab,
            "metadata": metadata or {},
        }
        if verify_seconds is not None:
            payload["verify_seconds"] = float(verify_seconds)
        if collab_verify_seconds is not None:
            payload["collab_verify_seconds"] = float(collab_verify_seconds)

        task_api_error: str | None = None
        try:
            submitted = self._create_leader_task(payload)
            task_id = str(submitted.get("task_id", "")).strip()
            if task_id:
                return self._wait_leader_task_result(task_id)
            task_api_error = "leader_task_missing_task_id"
        except LeaderCommandTimeout:
            raise
        except RuntimeError as exc:
            task_api_error = str(exc)

        response = self._send_leader_command_legacy(payload)
        if task_api_error:
            notes = response.get("orchestration_notes")
            if not isinstance(notes, list):
                notes = []
                response["orchestration_notes"] = notes
            notes.append(f"task_api_fallback:{task_api_error}")
        return response

    def _create_leader_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url=f"{self._base_url}/v1/chat/leader/tasks",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                if not (200 <= resp.status < 300):
                    raise RuntimeError(f"http_status={resp.status} body={body[:400]}")
                return json.loads(body) if body else {}
        except (TimeoutError, socket.timeout) as exc:
            raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"http_error={exc.code} body={detail[:400]}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s") from exc
            if isinstance(reason, str) and "timed out" in reason.lower():
                raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s") from exc
            raise RuntimeError(f"connection_error={exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_json_response") from exc

    def _get_leader_task(self, task_id: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url=f"{self._base_url}/v1/chat/leader/tasks/{task_id}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                if not (200 <= resp.status < 300):
                    raise RuntimeError(f"http_status={resp.status} body={body[:400]}")
                return json.loads(body) if body else {}
        except (TimeoutError, socket.timeout) as exc:
            raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"http_error={exc.code} body={detail[:400]}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s") from exc
            if isinstance(reason, str) and "timed out" in reason.lower():
                raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s") from exc
            raise RuntimeError(f"connection_error={exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_json_response") from exc

    def _wait_leader_task_result(self, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + float(self._timeout_seconds)
        poll_seconds = 1.0
        while time.monotonic() < deadline:
            snapshot = self._get_leader_task(task_id)
            state = str(snapshot.get("state", "")).strip().lower()
            if state in {"final", "failed", "timeout"}:
                result_raw = snapshot.get("result")
                if isinstance(result_raw, dict):
                    return result_raw
                error_text = str(snapshot.get("error", "")).strip() or f"leader_task_state={state}"
                return {
                    "accepted": False,
                    "auto_collab": True,
                    "leader_identity_id": "",
                    "leader_result": {
                        "accepted": False,
                        "delivery_state": "failed",
                        "identity_id": "ad-hoc",
                        "route_source": "direct_request",
                        "session_name_prefix": "fqg",
                        "verify_seconds": 0,
                        "control_exit_code": 1,
                        "control_result": {"error": error_text},
                    },
                    "collab_result": None,
                    "collab_error": error_text,
                    "orchestration_notes": [f"task_state={state}"],
                }
            time.sleep(poll_seconds)
        raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s")

    def _send_leader_command_legacy(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url=f"{self._base_url}/v1/chat/leader/command",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                if not (200 <= resp.status < 300):
                    raise RuntimeError(f"http_status={resp.status} body={body[:400]}")
                return json.loads(body) if body else {}
        except (TimeoutError, socket.timeout) as exc:
            raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"http_error={exc.code} body={detail[:400]}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s") from exc
            if isinstance(reason, str) and "timed out" in reason.lower():
                raise LeaderCommandTimeout(f"request_timeout={self._timeout_seconds}s") from exc
            raise RuntimeError(f"connection_error={exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_json_response") from exc

    def get_identity_routes(self) -> dict[str, Any]:
        req = urllib.request.Request(
            url=f"{self._base_url}/v1/chat/routes",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                if not (200 <= resp.status < 300):
                    raise RuntimeError(f"http_status={resp.status} body={body[:400]}")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"http_error={exc.code} body={detail[:400]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"connection_error={exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_json_response") from exc

    def get_leader_snapshot(self) -> dict[str, Any]:
        req = urllib.request.Request(
            url=f"{self._base_url}/v1/chat/leader/snapshot",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                if not (200 <= resp.status < 300):
                    raise RuntimeError(f"http_status={resp.status} body={body[:400]}")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"http_error={exc.code} body={detail[:400]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"connection_error={exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_json_response") from exc

    def get_approval_queue(
        self,
        *,
        status_filter: str = "PENDING",
        limit: int = 10,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "status": status_filter,
                "limit": max(1, min(int(limit), 100)),
            }
        )
        req = urllib.request.Request(
            url=f"{self._base_url}/v1/approvals/queue?{query}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                if not (200 <= resp.status < 300):
                    raise RuntimeError(f"http_status={resp.status} body={body[:400]}")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"http_error={exc.code} body={detail[:400]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"connection_error={exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_json_response") from exc

    def apply_trusted_approval_decision(
        self,
        *,
        request_id: str,
        action: str,
        approver: str,
        bridge_token: str,
        source_ip: str = "",
    ) -> dict[str, Any]:
        payload = {
            "action": action,
            "approver": approver,
            "source_ip": source_ip,
        }
        req = urllib.request.Request(
            url=f"{self._base_url}/v1/approvals/{request_id}/trusted-decision",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-fqg-bridge-token": str(bridge_token or ""),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                if not (200 <= resp.status < 300):
                    raise RuntimeError(f"http_status={resp.status} body={body[:400]}")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"http_error={exc.code} body={detail[:400]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"connection_error={exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_json_response") from exc


def format_dispatch_summary(response: dict[str, Any]) -> str:
    accepted = bool(response.get("accepted", False))
    leader = response.get("leader_result") or {}
    collab = response.get("collab_result") or {}
    notes = response.get("orchestration_notes") or []

    leader_state = str(leader.get("delivery_state", "unknown"))
    leader_sid = str(leader.get("session_id", "")) or "-"
    collab_state = str(collab.get("delivery_state", "n/a")) if collab else "n/a"
    collab_sid = str(collab.get("session_id", "")) if collab else "-"
    collab_sid = collab_sid or "-"

    title = "执行已受理" if accepted else "执行受理失败"
    lines = [
        f"{title}",
        f"leader: {leader_state} | sid={leader_sid}",
        f"collab: {collab_state} | sid={collab_sid}",
    ]
    if notes:
        lines.append(f"notes: {'; '.join(str(x) for x in notes[:3])}")
    return "\n".join(lines)


def is_route_query_command(text: str) -> bool:
    normalized = "".join(_clean_text_for_command(str(text)).strip().lower().split())
    if not normalized:
        return False

    keywords = {
        "sid",
        "/sid",
        "session",
        "sessionid",
        "status",
        "/status",
        "progress",
        "/progress",
        "route",
        "/route",
        "会话id",
        "会话",
        "状态",
        "进度",
        "最近输出",
        "运行状态",
        "路由",
        "查看会话",
    }
    if normalized in keywords:
        return True

    lead_prefixes = (
        "请你帮我",
        "请帮我",
        "帮我",
        "麻烦你",
        "麻烦",
        "请你",
        "请",
        "给我",
        "给",
        "发一下",
        "发下",
        "发个",
        "发",
        "查",
        "查一下",
        "查下",
        "查询",
        "查看一下",
        "查看",
        "看一下",
        "看下",
        "看看",
        "看",
        "报一下",
        "报下",
        "报",
    )
    trailing_suffixes = ("一下", "下", "吧", "呢", "哈", "呗")

    candidates = {normalized}
    pending = [normalized]
    # Keep parsing shallow and deterministic; avoid over-normalization.
    for _ in range(3):
        if not pending:
            break
        next_pending: list[str] = []
        for value in pending:
            for prefix in lead_prefixes:
                if value.startswith(prefix) and len(value) > len(prefix):
                    trimmed = value[len(prefix) :]
                    if trimmed and trimmed not in candidates:
                        candidates.add(trimmed)
                        next_pending.append(trimmed)
        pending = next_pending

    snapshot = list(candidates)
    for value in snapshot:
        current = value
        for _ in range(2):
            trimmed_any = False
            for suffix in trailing_suffixes:
                if current.endswith(suffix) and len(current) > len(suffix):
                    current = current[: -len(suffix)]
                    trimmed_any = True
                    if current and current not in candidates:
                        candidates.add(current)
            if not trimmed_any:
                break

    return any(candidate in keywords for candidate in candidates)


def parse_approval_command(text: str) -> ApprovalCommand | None:
    cleaned = _clean_text_for_command(str(text))
    if not cleaned:
        return None
    normalized = re.sub(r"\s+", " ", cleaned).strip()
    lowered = normalized.lower()

    list_keywords = {
        "/approvals",
        "approvals",
        "pending approvals",
        "待审批",
        "审批列表",
        "查看审批",
        "查审批",
        "审批状态",
    }
    if lowered in list_keywords:
        return ApprovalCommand(action="list", request_id=None)

    req_match = APPROVAL_REQUEST_ID_PATTERN.search(normalized)
    request_id = req_match.group(0).lower() if req_match else None

    approve_prefixes = ("/approve", "approve", "同意", "批准", "通过审批", "审批通过")
    reject_prefixes = ("/reject", "reject", "拒绝", "驳回", "审批拒绝")

    if any(lowered.startswith(prefix) for prefix in approve_prefixes):
        return ApprovalCommand(action="approve", request_id=request_id)
    if any(lowered.startswith(prefix) for prefix in reject_prefixes):
        return ApprovalCommand(action="reject", request_id=request_id)
    return None


def format_approval_queue_summary(response: dict[str, Any]) -> str:
    items_raw = response.get("items")
    items = items_raw if isinstance(items_raw, list) else []
    if not items:
        return "当前没有待审批项。"

    lines = ["当前待审批队列"]
    for idx, raw in enumerate(items[:6], start=1):
        item = raw if isinstance(raw, dict) else {}
        request_id = str(item.get("request_id", "")).strip() or "-"
        risk_level = str(item.get("risk_level", "")).strip() or "L3"
        reason = str(item.get("reason", "")).strip() or "-"
        remaining = item.get("remaining_approvals")
        remain_text = str(remaining) if remaining is not None else "-"
        cmd = " ".join(str(item.get("command_preview", "")).split()).strip() or "-"
        if len(cmd) > 120:
            cmd = f"{cmd[:120]}..."
        lines.append(
            f"{idx}. {request_id} | risk={risk_level} | remain={remain_text} | reason={reason}"
        )
        lines.append(f"   cmd={cmd}")
    lines.append("审批指令：同意 <request_id> 或 拒绝 <request_id>")
    return "\n".join(lines)


def format_routes_summary(response: dict[str, Any]) -> str:
    items_raw = response.get("items")
    items = items_raw if isinstance(items_raw, list) else []
    if not items:
        return "当前没有可用路由配置。"

    lines = ["当前路由状态"]
    for raw in items[:6]:
        item = raw if isinstance(raw, dict) else {}
        identity_id = str(item.get("identity_id", "")).strip() or "-"
        session_id = str(item.get("session_id", "")).strip() or "-"
        session_prefix = str(item.get("session_name_prefix", "")).strip() or "-"
        enabled = bool(item.get("enabled", False))
        route_status = str(item.get("route_status", "ok")).strip().lower() or "ok"
        route_error = str(item.get("route_error", "")).strip()
        lines.append(
            f"{identity_id}: sid={session_id} | prefix={session_prefix} | enabled={'on' if enabled else 'off'} | route={route_status}"
        )
        if route_error:
            lines.append(f"  route_error: {route_error}")
    return "\n".join(lines)


def format_leader_snapshot_summary(response: dict[str, Any]) -> str:
    items_raw = response.get("items")
    items = items_raw if isinstance(items_raw, list) else []
    if not items:
        return "当前会话快照为空。"

    lines = ["当前会话状态"]
    for raw in items[:6]:
        item = raw if isinstance(raw, dict) else {}
        identity_id = str(item.get("identity_id", "")).strip() or "-"
        session_id = str(item.get("session_id", "")).strip() or "-"
        state = str(item.get("state", "")).strip() or "UNKNOWN"
        idle_seconds = item.get("idle_seconds")
        idle_text = "-" if idle_seconds is None else str(idle_seconds)
        summary = str(item.get("last_event_summary", "")).strip() or "-"
        latest_reply = str(item.get("last_agent_message", "")).strip()
        if len(summary) > 80:
            summary = f"{summary[:80]}..."
        if latest_reply:
            latest_reply = " ".join(latest_reply.split())
            if len(latest_reply) > 120:
                latest_reply = f"{latest_reply[:120]}..."
            lines.append(
                f"{identity_id}: sid={session_id} | state={state} | idle={idle_text}s | last={summary} | reply={latest_reply}"
            )
        else:
            lines.append(
                f"{identity_id}: sid={session_id} | state={state} | idle={idle_text}s | last={summary}"
            )
    return "\n".join(lines)
