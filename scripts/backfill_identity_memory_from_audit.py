#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from feiqiao_guard.identity_memory import IdentityMemoryStore  # noqa: E402


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill identity rolling memory from approval audit jsonl")
    ap.add_argument("--identity-id", required=True)
    ap.add_argument("--audit-log", default=str(ROOT / "resource" / "reports" / "approval_audit.jsonl"))
    ap.add_argument("--memory-root", default=str(ROOT / ".runtime" / "identity_memory"))
    ap.add_argument("--window-size", type=int, default=60)
    ap.add_argument("--refresh-stride", type=int, default=5)
    args = ap.parse_args()

    audit_path = Path(args.audit_log).expanduser().resolve()
    if not audit_path.exists():
        print(json.dumps({"ok": False, "error": "audit_log_not_found", "path": str(audit_path)}, ensure_ascii=False))
        return 1

    store = IdentityMemoryStore(
        root_dir=Path(args.memory_root).expanduser().resolve(),
        window_size=max(1, int(args.window_size)),
        refresh_stride=max(1, int(args.refresh_stride)),
    )

    rows = _iter_jsonl(audit_path)
    appended = 0
    for row in rows:
        event = str(row.get("event", "")).strip()
        if event == "chat_inbound_received":
            if str(row.get("identity_id", "")).strip() != args.identity_id:
                continue
            store.append_turn(
                identity_id=args.identity_id,
                role="user",
                text=str(row.get("message_preview", "")).strip(),
                metadata=row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
            )
            appended += 1
            continue

        if event == "chat_inbound_dispatched":
            if str(row.get("identity_id", "")).strip() != args.identity_id:
                continue
            store.append_turn(
                identity_id=args.identity_id,
                role="system",
                text=(
                    f"dispatch accepted={bool(row.get('accepted'))} "
                    f"delivery_state={row.get('delivery_state')} "
                    f"control_exit_code={row.get('control_exit_code')}"
                ),
                metadata={
                    "route_source": row.get("route_source"),
                    "delivery_state": row.get("delivery_state"),
                    "control_exit_code": row.get("control_exit_code"),
                },
            )
            appended += 1
            continue

        if event == "chat_leader_command":
            if str(row.get("leader_identity_id", "")).strip() != args.identity_id:
                continue
            store.append_turn(
                identity_id=args.identity_id,
                role="system",
                text=(
                    f"leader_command accepted={bool(row.get('accepted'))} "
                    f"leader_delivery_state={row.get('leader_delivery_state')} "
                    f"collab_delivery_state={row.get('collab_delivery_state')}"
                ),
                metadata={
                    "entrypoint": "chat_leader_command",
                    "delivery_state": row.get("leader_delivery_state"),
                },
            )
            appended += 1

    payload = store.read(args.identity_id) or {}
    turns = payload.get("turns")
    print(
        json.dumps(
            {
                "ok": True,
                "identity_id": args.identity_id,
                "audit_log": str(audit_path),
                "memory_file": str((Path(args.memory_root).expanduser().resolve())),
                "appended_events": appended,
                "window_size": payload.get("window_size"),
                "stored_turns": len(turns) if isinstance(turns, list) else 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
