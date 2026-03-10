#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MISSION_LOCK = ROOT / "resource" / "ops" / "mission_lock_guixianren_e2e.md"
DEFAULT_CHANNEL_POLICY = ROOT / "resource" / "ops" / "dingtalk_robot_channel_policy.json"
DEFAULT_MEMORY_ROOT = ROOT / ".runtime" / "identity_memory"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _channel_status(policy: dict[str, Any], name: str) -> str:
    channels = policy.get("channels")
    if not isinstance(channels, list):
        return ""
    for row in channels:
        if not isinstance(row, dict):
            continue
        if str(row.get("name", "")).strip() == name:
            return str(row.get("status", "")).strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Identity governance preflight gate")
    ap.add_argument("--identity-id", required=True)
    ap.add_argument("--window-min", type=int, default=60)
    ap.add_argument("--mission-lock", default=str(DEFAULT_MISSION_LOCK))
    ap.add_argument("--channel-policy", default=str(DEFAULT_CHANNEL_POLICY))
    ap.add_argument("--memory-root", default=str(DEFAULT_MEMORY_ROOT))
    args = ap.parse_args()

    mission_lock = Path(args.mission_lock).expanduser().resolve()
    channel_policy = Path(args.channel_policy).expanduser().resolve()
    memory_root = Path(args.memory_root).expanduser().resolve()
    memory_file = memory_root / f"{args.identity_id}.json"
    # Fallback to sanitized file naming used by identity_memory store.
    if not memory_file.exists():
        memory_file = memory_root / (
            "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in args.identity_id).strip("._")
            + ".json"
        )

    report: dict[str, Any] = {
        "checked_at": _now(),
        "identity_id": args.identity_id,
        "checks": [],
    }

    def add_check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})

    lock_ok = mission_lock.exists()
    add_check("mission_lock_present", lock_ok, str(mission_lock))

    policy = _load_json(channel_policy)
    policy_ok = policy is not None
    add_check("channel_policy_readable", policy_ok, str(channel_policy))
    if policy_ok:
        n8n_status = _channel_status(policy or {}, "n8n自动化通知机器人")
        guixianren_status = _channel_status(policy or {}, "龟仙人")
        add_check("channel_n8n_paused", n8n_status == "PAUSED", f"status={n8n_status or '<missing>'}")
        add_check(
            "channel_guixianren_active",
            guixianren_status == "ACTIVE",
            f"status={guixianren_status or '<missing>'}",
        )

    mem_payload = _load_json(memory_file)
    mem_ok = mem_payload is not None
    add_check("memory_file_readable", mem_ok, str(memory_file))
    if mem_ok:
        turns = mem_payload.get("turns")
        turns_count = len(turns) if isinstance(turns, list) else 0
        window_size = int(mem_payload.get("window_size", 0) or 0)
        add_check("memory_window_size_min", window_size >= int(args.window_min), f"window_size={window_size}")
        add_check("memory_turns_present", turns_count > 0, f"turns={turns_count}")
        fresh_size = int(mem_payload.get("fresh_size", 0) or 0)
        stable_size = int(mem_payload.get("stable_size", 0) or 0)
        archive_size = int(mem_payload.get("archive_size", 0) or 0)
        add_check(
            "memory_tier_sizes_20_20_20",
            fresh_size == 20 and stable_size == 20 and archive_size == 20,
            f"fresh={fresh_size};stable={stable_size};archive={archive_size}",
        )

    checks = report.get("checks", [])
    overall_ok = all(bool(row.get("ok")) for row in checks if isinstance(row, dict))
    report["overall"] = "PASS" if overall_ok else "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
