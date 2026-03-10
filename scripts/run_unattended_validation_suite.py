#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("FQG_VALIDATE_BASE_URL", "http://127.0.0.1:3001").strip()
EXPECTED_ID = "feiqiao-guard-delivery-lead"


def _now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _get(path: str) -> dict:
    req = Request(f"{BASE_URL}{path}", method="GET")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _post(path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"{BASE_URL}{path}",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _leader_last_message() -> str:
    snap = _get("/v1/chat/leader/snapshot")
    for item in snap.get("items", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("identity_id", "")).strip() != EXPECTED_ID:
            continue
        return str(item.get("last_agent_message", "")).strip()
    return ""


def _wait_match(pattern: str, timeout: float = 120.0) -> tuple[bool, str]:
    rgx = re.compile(pattern)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        msg = _leader_last_message()
        if msg:
            last = msg
        if rgx.search(msg):
            return True, msg
        time.sleep(2)
    return False, last


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def main() -> int:
    suite_id = f"unattended_suite_{_now()}"
    out_dir = ROOT / "artifacts" / "ops" / datetime.now().strftime("%Y-%m-%d") / suite_id
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_rows: list[str] = ["name\tok\tdetail"]
    details: dict[str, object] = {"suite_id": suite_id, "base_url": BASE_URL, "checks": []}

    # A) run continuity checker for 3 rounds
    for i in range(1, 4):
        cp = _run(["python3", str(ROOT / "scripts" / "validate_dingtalk_local_continuity.py")])
        ok = cp.returncode == 0
        detail = f"rc={cp.returncode}"
        out_log = out_dir / f"A{i}_continuity_round.log"
        out_log.write_text(
            f"STDOUT:\n{cp.stdout}\n\nSTDERR:\n{cp.stderr}\n",
            encoding="utf-8",
        )
        summary_rows.append(f"A{i}_continuity_round\t{1 if ok else 0}\t{detail};log={out_log}")
        details["checks"].append(
            {"name": f"A{i}_continuity_round", "ok": ok, "detail": detail, "log": str(out_log)}
        )
        time.sleep(2)

    # B) memory persistence across stack restart
    token = f"PERSIST-{_now()[-6:]}"
    write_payload = {
        "message": f"持久记忆测试-写入：请记住口令 {token}，只回复 ACK {token}",
        "auto_collab": False,
        "verify_seconds": 20,
        "metadata": {"channel": "selftest", "task_tag": suite_id, "case": "persist_write"},
    }
    write_resp = _post("/v1/chat/leader/command", write_payload)
    ok_write = bool(write_resp.get("accepted"))
    ack_seen, ack_msg = _wait_match(rf"ACK\s+{re.escape(token)}", timeout=150)
    summary_rows.append(f"B1_memory_write\t{1 if (ok_write and ack_seen) else 0}\tdispatch_ok={ok_write};ack_seen={ack_seen}")
    details["checks"].append(
        {
            "name": "B1_memory_write",
            "ok": bool(ok_write and ack_seen),
            "dispatch": write_resp,
            "last_message": ack_msg,
        }
    )

    # restart local stack
    cp_restart = _run([str(ROOT / "scripts" / "local_dingtalk_tmux_stack.sh"), "restart"])
    restart_ok = cp_restart.returncode == 0
    restart_log = out_dir / "B2_restart.log"
    restart_log.write_text(f"STDOUT:\n{cp_restart.stdout}\n\nSTDERR:\n{cp_restart.stderr}\n", encoding="utf-8")
    summary_rows.append(f"B2_stack_restart\t{1 if restart_ok else 0}\trc={cp_restart.returncode};log={restart_log}")
    details["checks"].append(
        {
            "name": "B2_stack_restart",
            "ok": restart_ok,
            "rc": cp_restart.returncode,
            "log": str(restart_log),
        }
    )

    # recall via leader API after restart
    recall_payload = {
        "message": "持久记忆测试-读取：请只回复刚才口令。",
        "auto_collab": False,
        "verify_seconds": 20,
        "metadata": {"channel": "selftest", "task_tag": suite_id, "case": "persist_recall_api"},
    }
    recall_resp = _post("/v1/chat/leader/command", recall_payload)
    recall_api_ok = bool(recall_resp.get("accepted"))
    token_seen_api, msg_api = _wait_match(re.escape(token), timeout=150)
    summary_rows.append(
        f"B3_memory_recall_api_after_restart\t{1 if (recall_api_ok and token_seen_api) else 0}\tdispatch_ok={recall_api_ok};token_seen={token_seen_api}"
    )
    details["checks"].append(
        {
            "name": "B3_memory_recall_api_after_restart",
            "ok": bool(recall_api_ok and token_seen_api),
            "dispatch": recall_resp,
            "last_message": msg_api,
        }
    )

    # recall via guarded continue (cross-entrypoint)
    routes = _get("/v1/chat/routes")
    sid = ""
    codex_home = ""
    prefix = "fqg-lead"
    for item in routes.get("items", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("identity_id", "")).strip() != EXPECTED_ID:
            continue
        sid = str(item.get("session_id", "")).strip()
        codex_home = str(item.get("codex_home", "")).strip()
        prefix = str(item.get("session_name_prefix", "")).strip() or prefix
        break
    cp_cross = _run(
        [
            "python3",
            str(ROOT / "scripts" / "guarded_session_control.py"),
            "continue",
            "--session-id",
            sid,
            "--codex-home",
            codex_home,
            "--workspace-root",
            str(ROOT),
            "--session-name-prefix",
            prefix,
            "--verify-seconds",
            "20",
            "--text",
            "持久记忆测试-跨入口读取：只回复刚才口令。",
            "--json",
        ]
    )
    cross_dispatch_ok = cp_cross.returncode == 0
    token_seen_cross, msg_cross = _wait_match(re.escape(token), timeout=150)
    cross_log = out_dir / "B4_cross_entry.log"
    cross_log.write_text(f"STDOUT:\n{cp_cross.stdout}\n\nSTDERR:\n{cp_cross.stderr}\n", encoding="utf-8")
    summary_rows.append(
        f"B4_memory_recall_cross_after_restart\t{1 if (cross_dispatch_ok and token_seen_cross) else 0}\tdispatch_ok={cross_dispatch_ok};token_seen={token_seen_cross};log={cross_log}"
    )
    details["checks"].append(
        {
            "name": "B4_memory_recall_cross_after_restart",
            "ok": bool(cross_dispatch_ok and token_seen_cross),
            "rc": cp_cross.returncode,
            "log": str(cross_log),
            "last_message": msg_cross,
        }
    )

    # C) identity drift guard
    who_payload = {
        "message": "最终身份检查：请仅回复 identity_id=<id>",
        "auto_collab": False,
        "verify_seconds": 20,
        "metadata": {"channel": "selftest", "task_tag": suite_id, "case": "identity_drift_guard"},
    }
    who_resp = _post("/v1/chat/leader/command", who_payload)
    who_ok = bool(who_resp.get("accepted")) and str(who_resp.get("leader_identity_id", "")).strip() == EXPECTED_ID
    who_seen, who_msg = _wait_match(r"identity_id\s*=\s*feiqiao-guard-delivery-lead", timeout=120)
    summary_rows.append(f"C1_identity_drift_guard\t{1 if (who_ok and who_seen) else 0}\tdispatch_ok={who_ok};identity_seen={who_seen}")
    details["checks"].append(
        {
            "name": "C1_identity_drift_guard",
            "ok": bool(who_ok and who_seen),
            "dispatch": who_resp,
            "last_message": who_msg,
        }
    )

    overall = all(bool(check.get("ok")) for check in details["checks"])
    details["overall"] = "PASS" if overall else "FAIL"
    details["token"] = token

    summary_rows.append(f"OVERALL\t{1 if overall else 0}\t{details['overall']}")
    summary_path = out_dir / "summary.tsv"
    report_path = out_dir / "report.json"
    summary_path.write_text("\n".join(summary_rows) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(str(summary_path))
    print(str(report_path))
    print(details["overall"])
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

