#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("FQG_VALIDATE_BASE_URL", "http://127.0.0.1:3001").strip()
EXPECTED_ID = "feiqiao-guard-delivery-lead"


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def http_get(path: str) -> dict[str, Any]:
    req = Request(f"{BASE_URL}{path}", method="GET")
    with urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def http_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"{BASE_URL}{path}",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def leader_last_message() -> str:
    snap = http_get("/v1/chat/leader/snapshot")
    for item in snap.get("items", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("identity_id", "")).strip() == EXPECTED_ID:
            return str(item.get("last_agent_message", "")).strip()
    return ""


def wait_match(pattern: str, timeout: float = 150.0) -> tuple[bool, str]:
    rgx = re.compile(pattern, flags=re.S)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        msg = leader_last_message()
        if msg:
            last = msg
        if rgx.search(msg):
            return True, msg
        time.sleep(2)
    return False, last


def row(name: str, ok: bool, detail: str) -> str:
    return f"{name}\t{1 if ok else 0}\t{detail}"


def main() -> int:
    run_id = f"extended_unattended_suite_{now_ts()}"
    out_dir = ROOT / "artifacts" / "ops" / datetime.now().strftime("%Y-%m-%d") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    checks: list[dict[str, Any]] = []
    summary: list[str] = ["name\tok\tdetail"]

    # Resolve route
    health = http_get("/healthz")
    routes = http_get("/v1/chat/routes")
    snapshot = http_get("/v1/chat/leader/snapshot")
    route_item = {}
    for item in routes.get("items", []):
        if isinstance(item, dict) and str(item.get("identity_id", "")).strip() == EXPECTED_ID:
            route_item = item
            break
    sid = str(route_item.get("session_id", "")).strip()
    codex_home = str(route_item.get("codex_home", "")).strip()
    prefix = str(route_item.get("session_name_prefix", "")).strip() or "fqg-lead"
    probe_ok = False
    for item in snapshot.get("items", []):
        if isinstance(item, dict) and str(item.get("identity_id", "")).strip() == EXPECTED_ID:
            probe_ok = bool(item.get("process_probe_ok", False))
            break

    c1_ok = (
        str(health.get("status", "")).strip() == "ok"
        and bool(sid)
        and bool(codex_home)
        and str(route_item.get("route_status", "")).strip().lower() == "ok"
        and probe_ok
    )
    c1_detail = (
        f"health={health.get('status')};sid={bool(sid)};codex_home={bool(codex_home)};"
        f"route_status={route_item.get('route_status')};probe_ok={probe_ok}"
    )
    checks.append({"name": "E1_route_health_probe", "ok": c1_ok, "detail": c1_detail})
    summary.append(row("E1_route_health_probe", c1_ok, c1_detail))

    # E2: baseline continuity script once
    cp_cont = run_cmd(["python3", str(ROOT / "scripts" / "validate_dingtalk_local_continuity.py")])
    cont_log = out_dir / "E2_continuity.log"
    cont_log.write_text(f"STDOUT:\n{cp_cont.stdout}\n\nSTDERR:\n{cp_cont.stderr}\n", encoding="utf-8")
    e2_ok = cp_cont.returncode == 0
    e2_detail = f"rc={cp_cont.returncode};log={cont_log}"
    checks.append({"name": "E2_baseline_continuity", "ok": e2_ok, "detail": e2_detail})
    summary.append(row("E2_baseline_continuity", e2_ok, e2_detail))

    # E3-E5: multimodal on 3 images
    images = [
        ROOT / "resource" / "image" / "test1.jpg",
        ROOT / "resource" / "image" / "test2.png",
        ROOT / "resource" / "image" / "url_case_20260307.jpeg",
    ]
    for idx, img in enumerate(images, start=1):
        marker = f"MM{idx}_{now_ts()[-6:]}"
        payload = {
            "message": (
                f"多模态测试{idx}：请调用可用的GLM4.6V链路分析图片 {img}。"
                "只输出一行："
                f"{marker} status=<OK|FAIL> summary=<20字内客观描述>。"
                "不要做真人身份识别，只做客观画面描述。"
            ),
            "auto_collab": False,
            "verify_seconds": 25,
            "metadata": {"channel": "selftest", "task_tag": run_id, "case": f"multimodal_{idx}"},
        }
        resp = http_post("/v1/chat/leader/command", payload)
        dispatch_ok = bool(resp.get("accepted"))
        seen_ok, last = wait_match(rf"{re.escape(marker)}\s+status=OK", timeout=180)
        ok = dispatch_ok and seen_ok
        detail = f"dispatch_ok={dispatch_ok};status_ok_seen={seen_ok};image={img.name}"
        checks.append({"name": f"E{idx+2}_multimodal_{img.name}", "ok": ok, "detail": detail, "last": last})
        summary.append(row(f"E{idx+2}_multimodal_{img.name}", ok, detail))

    # E6-E8 memory persistence across restart
    token = f"EXT-{now_ts()[-6:]}"
    write_resp = http_post(
        "/v1/chat/leader/command",
        {
            "message": f"扩展记忆写入：记住口令 {token}，只回 ACK {token}",
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {"channel": "selftest", "task_tag": run_id, "case": "memory_write"},
        },
    )
    e6_dispatch_ok = bool(write_resp.get("accepted"))
    e6_seen, e6_last = wait_match(rf"ACK\s+{re.escape(token)}", timeout=150)
    e6_ok = e6_dispatch_ok and e6_seen
    checks.append({"name": "E6_memory_write", "ok": e6_ok, "detail": f"dispatch_ok={e6_dispatch_ok};ack_seen={e6_seen}", "last": e6_last})
    summary.append(row("E6_memory_write", e6_ok, f"dispatch_ok={e6_dispatch_ok};ack_seen={e6_seen}"))

    cp_restart = run_cmd([str(ROOT / "scripts" / "local_dingtalk_tmux_stack.sh"), "restart"])
    restart_log = out_dir / "E7_restart.log"
    restart_log.write_text(f"STDOUT:\n{cp_restart.stdout}\n\nSTDERR:\n{cp_restart.stderr}\n", encoding="utf-8")
    e7_ok = cp_restart.returncode == 0
    checks.append({"name": "E7_stack_restart", "ok": e7_ok, "detail": f"rc={cp_restart.returncode};log={restart_log}"})
    summary.append(row("E7_stack_restart", e7_ok, f"rc={cp_restart.returncode};log={restart_log}"))

    recall_resp = http_post(
        "/v1/chat/leader/command",
        {
            "message": "扩展记忆读取：只回复刚才口令。",
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {"channel": "selftest", "task_tag": run_id, "case": "memory_recall_after_restart"},
        },
    )
    e8_dispatch_ok = bool(recall_resp.get("accepted"))
    e8_seen, e8_last = wait_match(re.escape(token), timeout=150)
    e8_ok = e8_dispatch_ok and e8_seen
    checks.append({"name": "E8_memory_recall_after_restart", "ok": e8_ok, "detail": f"dispatch_ok={e8_dispatch_ok};token_seen={e8_seen}", "last": e8_last})
    summary.append(row("E8_memory_recall_after_restart", e8_ok, f"dispatch_ok={e8_dispatch_ok};token_seen={e8_seen}"))

    # E9-E11 multi-role (different identity labels / same execution context)
    role_tokens = []
    roles = ["planner-role", "ops-role"]
    for role in roles:
        role_token = f"{role}-{now_ts()[-4:]}"
        role_tokens.append(role_token)
        inbound_resp = http_post(
            "/v1/chat/inbound",
            {
                "identity_id": role,
                "session_id": sid,
                "codex_home": codex_home,
                "session_name_prefix": prefix,
                "verify_seconds": 20,
                "message": f"多角色测试：你当前角色标签是 {role}，请只回复 ROLE_OK role={role} token={role_token}",
                "metadata": {"channel": "selftest", "task_tag": run_id, "case": f"role_{role}"},
            },
        )
        role_dispatch_ok = bool(inbound_resp.get("accepted"))
        role_seen, role_last = wait_match(rf"ROLE_OK\s+role={re.escape(role)}\s+token={re.escape(role_token)}", timeout=150)
        role_ok = role_dispatch_ok and role_seen
        checks.append(
            {
                "name": f"E9_role_{role}",
                "ok": role_ok,
                "detail": f"dispatch_ok={role_dispatch_ok};seen={role_seen}",
                "last": role_last,
            }
        )
        summary.append(row(f"E9_role_{role}", role_ok, f"dispatch_ok={role_dispatch_ok};seen={role_seen}"))

    join_pat = ".*".join(re.escape(tk) for tk in role_tokens)
    role_recall_resp = http_post(
        "/v1/chat/leader/command",
        {
            "message": "多角色记忆汇总：请只回复最近两次角色token，按原样输出。",
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {"channel": "selftest", "task_tag": run_id, "case": "role_recall"},
        },
    )
    e11_dispatch_ok = bool(role_recall_resp.get("accepted"))
    e11_seen, e11_last = wait_match(join_pat, timeout=150)
    e11_ok = e11_dispatch_ok and e11_seen
    checks.append({"name": "E11_role_memory_recall", "ok": e11_ok, "detail": f"dispatch_ok={e11_dispatch_ok};tokens_seen={e11_seen}", "last": e11_last})
    summary.append(row("E11_role_memory_recall", e11_ok, f"dispatch_ok={e11_dispatch_ok};tokens_seen={e11_seen}"))

    # E12 identity drift guard
    who_resp = http_post(
        "/v1/chat/leader/command",
        {
            "message": "最终身份检查：请仅回复 identity_id=<id>",
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {"channel": "selftest", "task_tag": run_id, "case": "identity_guard"},
        },
    )
    e12_dispatch_ok = bool(who_resp.get("accepted")) and str(who_resp.get("leader_identity_id", "")).strip() == EXPECTED_ID
    e12_seen, e12_last = wait_match(r"identity_id\s*=\s*feiqiao-guard-delivery-lead", timeout=120)
    e12_ok = e12_dispatch_ok and e12_seen
    checks.append({"name": "E12_identity_not_drifted", "ok": e12_ok, "detail": f"dispatch_ok={e12_dispatch_ok};identity_seen={e12_seen}", "last": e12_last})
    summary.append(row("E12_identity_not_drifted", e12_ok, f"dispatch_ok={e12_dispatch_ok};identity_seen={e12_seen}"))

    # E13 no sid none + bridge ready
    bridge_log = ROOT / ".runtime" / "local_bridge" / "bridge.log"
    bridge_tail = bridge_log.read_text(encoding="utf-8", errors="ignore")[-14000:] if bridge_log.exists() else ""
    pane = run_cmd(
        [
            "tmux",
            "-S",
            str(ROOT / ".runtime" / "tmux" / "local_bridge.sock"),
            "list-panes",
            "-t",
            "fqg-local-stream",
            "-F",
            "#{pane_dead} #{pane_current_command} #{pane_pid}",
        ]
    )
    pane_line = (pane.stdout or "").strip()
    parts = pane_line.split()
    pane_ok = len(parts) >= 2 and parts[0] == "0" and "python" in parts[1]
    no_sid_none = "sid=None" not in bridge_tail
    e13_ok = pane_ok and no_sid_none and ("bridge_started" in bridge_tail) and ("endpoint is" in bridge_tail)
    e13_detail = f"pane_ok={pane_ok};no_sid_none={no_sid_none};bridge_started={'bridge_started' in bridge_tail};endpoint={'endpoint is' in bridge_tail}"
    checks.append({"name": "E13_bridge_health_no_sid_none", "ok": e13_ok, "detail": e13_detail})
    summary.append(row("E13_bridge_health_no_sid_none", e13_ok, e13_detail))

    overall_ok = all(bool(c.get("ok")) for c in checks)
    overall = "PASS" if overall_ok else "FAIL"
    summary.append(row("OVERALL", overall_ok, overall))

    report = {
        "run_id": run_id,
        "base_url": BASE_URL,
        "expected_identity": EXPECTED_ID,
        "token_memory": token,
        "role_tokens": role_tokens,
        "checks": checks,
        "overall": overall,
    }

    summary_path = out_dir / "summary.tsv"
    report_path = out_dir / "report.json"
    summary_path.write_text("\n".join(summary) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(str(summary_path))
    print(str(report_path))
    print(overall)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

