from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SAFE_ID_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_identity_file_name(identity_id: str) -> str:
    cleaned = _SAFE_ID_PATTERN.sub("_", identity_id.strip())
    cleaned = cleaned.strip("._")
    return cleaned or "unknown_identity"


def _clean_text(text: str, *, max_len: int = 4000) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= max_len:
        return normalized
    return f"{normalized[: max_len - 3]}..."


def _compact_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    raw = metadata or {}
    keep_keys = {
        "channel",
        "entrypoint",
        "role",
        "trace_id",
        "task_id",
        "question_tag",
        "message_type",
        "route_source",
        "delivery_state",
        "verify_seconds",
        "control_exit_code",
    }
    compact: dict[str, Any] = {}
    for key in keep_keys:
        if key not in raw:
            continue
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            compact[key] = _clean_text(value, max_len=256)
            continue
        if isinstance(value, (int, float, bool)):
            compact[key] = value
            continue
        if isinstance(value, list):
            compact[key] = [_clean_text(v, max_len=128) for v in value[:5]]
            continue
        compact[key] = _clean_text(value, max_len=256)
    return compact


class IdentityMemoryStore:
    def __init__(
        self,
        *,
        root_dir: Path,
        window_size: int = 60,
        fresh_size: int = 20,
        stable_size: int = 20,
        archive_size: int = 20,
        refresh_stride: int = 5,
        archive_summary_limit: int = 200,
    ) -> None:
        self._root = root_dir
        self._window_size = max(1, int(window_size))
        self._fresh_size, self._stable_size, self._archive_size = self._normalize_tier_sizes(
            window_size=self._window_size,
            fresh_size=fresh_size,
            stable_size=stable_size,
            archive_size=archive_size,
        )
        self._tier_roll_policy = (
            f"rolling_{self._window_size}:latest{self._fresh_size}_"
            f"stable{self._stable_size}_archive{self._archive_size}"
        )
        self._refresh_stride = max(1, int(refresh_stride))
        self._archive_summary_limit = max(10, int(archive_summary_limit))
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    def read(self, identity_id: str) -> dict[str, Any] | None:
        path = self._path_for(identity_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def append_turn(
        self,
        *,
        identity_id: str,
        role: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            payload = self._load_or_init(identity_id)
            clean_role = _clean_text(role or "system", max_len=32).lower()
            clean_text = _clean_text(text or "")
            compact_meta = _compact_metadata(metadata)

            turns = payload.get("turns")
            if not isinstance(turns, list):
                turns = []

            # Avoid duplicate appends caused by repeated polling with same turn signature.
            if turns:
                last = turns[-1]
                if (
                    isinstance(last, dict)
                    and str(last.get("role", "")) == clean_role
                    and str(last.get("text", "")) == clean_text
                    and str(last.get("trace_id", "")) == str(compact_meta.get("trace_id", ""))
                    and str(last.get("task_id", "")) == str(compact_meta.get("task_id", ""))
                ):
                    payload["updated_at"] = _now_iso()
                    self._save(identity_id, payload)
                    return last

            seq = int(payload.get("seq", 0)) + 1
            turn = {
                "turn_id": f"T{seq:06d}",
                "timestamp": _now_iso(),
                "role": clean_role,
                "text": clean_text,
                "channel": str(compact_meta.get("channel", "")).strip() or None,
                "trace_id": str(compact_meta.get("trace_id", "")).strip() or None,
                "task_id": str(compact_meta.get("task_id", "")).strip() or None,
                "question_tag": str(compact_meta.get("question_tag", "")).strip() or None,
                "metadata": compact_meta,
            }
            turns.append(turn)

            archive_summary = payload.get("archive_summary")
            if not isinstance(archive_summary, list):
                archive_summary = []
            while len(turns) > self._window_size:
                dropped = turns.pop(0)
                dropped_role = str(dropped.get("role", "")).strip() or "unknown"
                dropped_text = _clean_text(str(dropped.get("text", "")), max_len=72)
                archive_summary.append(f"{dropped_role}:{dropped_text}")
            if len(archive_summary) > self._archive_summary_limit:
                archive_summary = archive_summary[-self._archive_summary_limit :]

            payload["identity_id"] = identity_id
            payload["window_size"] = self._window_size
            payload["fresh_size"] = self._fresh_size
            payload["stable_size"] = self._stable_size
            payload["archive_size"] = self._archive_size
            payload["refresh_stride"] = self._refresh_stride
            payload["seq"] = seq
            payload["updated_at"] = _now_iso()
            payload["turns"] = turns
            payload["archive_summary"] = archive_summary
            archive_turns, stable_turns, fresh_turns = self._split_tiers(turns)
            payload["tier_counts"] = {
                "fresh": len(fresh_turns),
                "stable": len(stable_turns),
                "archive": len(archive_turns),
            }
            payload["tier_turn_ids"] = {
                "fresh": self._extract_turn_ids(fresh_turns),
                "stable": self._extract_turn_ids(stable_turns),
                "archive": self._extract_turn_ids(archive_turns),
            }
            payload["fresh_summary"] = self._build_summary(fresh_turns)
            payload["stable_summary"] = self._build_summary(stable_turns or turns[-self._refresh_stride :])
            payload["archive_window_summary"] = self._build_summary(archive_turns)
            payload["rolling_summary"] = self._build_summary(turns[-10:])
            if seq % self._refresh_stride == 0 or not str(payload.get("stable_checkpoint_summary", "")).strip():
                payload["stable_checkpoint_summary"] = self._build_summary(turns[-self._refresh_stride :])
            payload["tier_roll_policy"] = self._tier_roll_policy

            self._save(identity_id, payload)
            return turn

    def _path_for(self, identity_id: str) -> Path:
        return self._root / f"{_safe_identity_file_name(identity_id)}.json"

    def _load_or_init(self, identity_id: str) -> dict[str, Any]:
        existing = self.read(identity_id)
        if isinstance(existing, dict):
            return existing
        return {
            "identity_id": identity_id,
            "window_size": self._window_size,
            "fresh_size": self._fresh_size,
            "stable_size": self._stable_size,
            "archive_size": self._archive_size,
            "refresh_stride": self._refresh_stride,
            "seq": 0,
            "updated_at": _now_iso(),
            "turns": [],
            "rolling_summary": "",
            "fresh_summary": "",
            "stable_summary": "",
            "archive_window_summary": "",
            "stable_checkpoint_summary": "",
            "archive_summary": [],
            "tier_counts": {
                "fresh": 0,
                "stable": 0,
                "archive": 0,
            },
            "tier_turn_ids": {
                "fresh": [],
                "stable": [],
                "archive": [],
            },
            "tier_roll_policy": self._tier_roll_policy,
        }

    def _save(self, identity_id: str, payload: dict[str, Any]) -> None:
        path = self._path_for(identity_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _build_summary(self, turns: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for item in turns:
            role = str(item.get("role", "unknown")).strip() or "unknown"
            text = _clean_text(str(item.get("text", "")), max_len=120)
            trace_id = str(item.get("trace_id", "")).strip()
            task_id = str(item.get("task_id", "")).strip()
            suffix_bits = []
            if trace_id:
                suffix_bits.append(f"trace={trace_id}")
            if task_id:
                suffix_bits.append(f"task={task_id}")
            suffix = f" ({', '.join(suffix_bits)})" if suffix_bits else ""
            lines.append(f"{role}: {text}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _normalize_tier_sizes(
        *,
        window_size: int,
        fresh_size: int,
        stable_size: int,
        archive_size: int,
    ) -> tuple[int, int, int]:
        fresh = max(0, int(fresh_size))
        stable = max(0, int(stable_size))
        archive = max(0, int(archive_size))
        total = fresh + stable + archive
        if total == 0:
            fresh = min(20, window_size)
            remain = max(0, window_size - fresh)
            stable = min(20, remain)
            archive = max(0, window_size - fresh - stable)
            return fresh, stable, archive

        if total > window_size:
            fresh = min(fresh, window_size)
            remain = max(0, window_size - fresh)
            stable = min(stable, remain)
            remain = max(0, remain - stable)
            archive = min(archive, remain)
            return fresh, stable, archive

        if total < window_size:
            archive += window_size - total
        return fresh, stable, archive

    def _split_tiers(
        self, turns: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        total = len(turns)
        if total <= 0:
            return [], [], []

        fresh_start = max(0, total - self._fresh_size)
        fresh_turns = turns[fresh_start:]

        stable_end = fresh_start
        stable_start = max(0, stable_end - self._stable_size)
        stable_turns = turns[stable_start:stable_end]

        archive_end = stable_start
        archive_start = max(0, archive_end - self._archive_size)
        archive_turns = turns[archive_start:archive_end]
        return archive_turns, stable_turns, fresh_turns

    @staticmethod
    def _extract_turn_ids(turns: list[dict[str, Any]]) -> list[str]:
        ids: list[str] = []
        for item in turns:
            turn_id = str(item.get("turn_id", "")).strip()
            if turn_id:
                ids.append(turn_id)
        return ids
