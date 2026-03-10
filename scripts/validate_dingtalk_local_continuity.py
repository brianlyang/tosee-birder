#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("FQG_VALIDATE_BASE_URL", "http://127.0.0.1:3001").strip()
LOCAL_BRIDGE_SOCKET = ROOT / ".runtime" / "tmux" / "local_bridge.sock"
LOCAL_BRIDGE_SESSION = "fqg-local-stream"
EXPECTED_IDENTITY = "feiqiao-guard-delivery-lead"
BRIDGE_HEARTBEAT_FILE = ROOT / ".runtime" / "local_bridge" / "bridge_heartbeat.json"


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _http_get(path: str) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    req = Request(url=url, method="GET")
    with urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"non_object_response:{path}")
    return payload


def _http_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url=url,
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=90) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"non_object_response:{path}")
    return parsed


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _bridge_heartbeat_fresh(*, max_age_seconds: float = 90.0) -> tuple[bool, str]:
    if not BRIDGE_HEARTBEAT_FILE.exists():
        return False, "heartbeat_file_missing"
    try:
        payload = json.loads(BRIDGE_HEARTBEAT_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"heartbeat_parse_error:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return False, "heartbeat_not_object"
    epoch = payload.get("generated_at_epoch")
    try:
        ts = float(epoch)
    except Exception:  # noqa: BLE001
        return False, "heartbeat_epoch_missing"
    age = max(0.0, time.time() - ts)
    fresh = age <= max(1.0, float(max_age_seconds))
    return fresh, f"heartbeat_age_seconds={age:.1f}"


def _find_leader_item(snapshot: dict[str, Any]) -> dict[str, Any]:
    items = snapshot.get("items")
    if not isinstance(items, list):
        return {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("identity_id", "")).strip() == EXPECTED_IDENTITY:
            return item
    return {}


def _poll_last_message(
    *,
    matcher: re.Pattern[str],
    timeout_seconds: float,
    poll_seconds: float = 2.0,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        snapshot = _http_get("/v1/chat/leader/snapshot")
        leader = _find_leader_item(snapshot)
        msg = str(leader.get("last_agent_message", "")).strip()
        if msg:
            last = msg
        if matcher.search(msg):
            return True, msg
        time.sleep(max(0.5, poll_seconds))
    return False, last


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str

    def to_row(self) -> str:
        return f"{self.name}\t{int(self.ok)}\t{self.detail}"


def main() -> int:
    run_id = f"dingtalk_local_continuity_{_now_ts()}"
    out_dir = ROOT / "artifacts" / "ops" / datetime.now().strftime("%Y-%m-%d") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "run_id": run_id,
        "base_url": BASE_URL,
        "expected_identity": EXPECTED_IDENTITY,
        "checks": [],
    }
    checks: list[CheckResult] = []

    # Check 1: health + route strict binding
    try:
        health = _http_get("/healthz")
        routes = _http_get("/v1/chat/routes")
        snapshot = _http_get("/v1/chat/leader/snapshot")
        report["healthz"] = health
        report["routes"] = routes
        report["snapshot_before"] = snapshot

        status_ok = str(health.get("status", "")).strip() == "ok"
        items = routes.get("items")
        route_ok = False
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("identity_id", "")).strip() != EXPECTED_IDENTITY:
                    continue
                route_ok = (
                    str(item.get("route_status", "")).strip().lower() == "ok"
                    and str(item.get("session_id", "")).strip() != ""
                    and str(item.get("codex_home", "")).strip() != ""
                )
        leader_item = _find_leader_item(snapshot)
        probe_ok = bool(leader_item.get("process_probe_ok", False))
        checks.append(
            CheckResult(
                name="C1_health_route_probe",
                ok=(status_ok and route_ok and probe_ok),
                detail=f"status_ok={status_ok};route_ok={route_ok};process_probe_ok={probe_ok}",
            )
        )
    except (HTTPError, URLError, ValueError) as exc:
        checks.append(CheckResult("C1_health_route_probe", False, f"exception:{type(exc).__name__}:{exc}"))

    # Resolve current leader routing data for continue bridge check
    route_sid = ""
    route_home = ""
    route_prefix = "fqg-lead"
    try:
        routes = report.get("routes", {})
        items = routes.get("items") if isinstance(routes, dict) else []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("identity_id", "")).strip() != EXPECTED_IDENTITY:
                    continue
                route_sid = str(item.get("session_id", "")).strip()
                route_home = str(item.get("codex_home", "")).strip()
                route_prefix = str(item.get("session_name_prefix", "")).strip() or route_prefix
                break
    except Exception:  # noqa: BLE001
        pass

    token = f"MEM-{_now_ts()[-6:]}"

    # Check 2: memory write through leader command
    try:
        write_req = {
            "message": f"记忆测试A：请记住口令 {token}。只回复：ACK {token}",
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {
                "channel": "selftest",
                "task_tag": run_id,
                "case": "memory_write",
            },
        }
        write_resp = _http_post("/v1/chat/leader/command", write_req)
        report["memory_write_request"] = write_req
        report["memory_write_response"] = write_resp
        ok_dispatch = (
            bool(write_resp.get("accepted"))
            and str(write_resp.get("leader_identity_id", "")).strip() == EXPECTED_IDENTITY
            and str((write_resp.get("leader_result") or {}).get("delivery_state", "")).strip()
            in {"confirmed", "queued"}
        )
        ok_msg, got = _poll_last_message(
            matcher=re.compile(rf"ACK\s+{re.escape(token)}"),
            timeout_seconds=120,
        )
        report["memory_write_last_message"] = got
        checks.append(
            CheckResult(
                name="C2_memory_write",
                ok=(ok_dispatch and ok_msg),
                detail=f"dispatch_ok={ok_dispatch};ack_seen={ok_msg}",
            )
        )
    except (HTTPError, URLError, ValueError) as exc:
        checks.append(CheckResult("C2_memory_write", False, f"exception:{type(exc).__name__}:{exc}"))

    # Check 3: memory recall through same API entrypoint
    try:
        recall_req = {
            "message": "记忆测试B：请只回答刚才口令，不要解释。",
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {
                "channel": "selftest",
                "task_tag": run_id,
                "case": "memory_recall_api",
            },
        }
        recall_resp = _http_post("/v1/chat/leader/command", recall_req)
        report["memory_recall_api_request"] = recall_req
        report["memory_recall_api_response"] = recall_resp
        ok_dispatch = (
            bool(recall_resp.get("accepted"))
            and str((recall_resp.get("leader_result") or {}).get("delivery_state", "")).strip()
            in {"confirmed", "queued"}
        )
        ok_msg, got = _poll_last_message(
            matcher=re.compile(re.escape(token)),
            timeout_seconds=120,
        )
        report["memory_recall_api_last_message"] = got
        checks.append(
            CheckResult(
                name="C3_memory_recall_api",
                ok=(ok_dispatch and ok_msg),
                detail=f"dispatch_ok={ok_dispatch};token_seen={ok_msg}",
            )
        )
    except (HTTPError, URLError, ValueError) as exc:
        checks.append(CheckResult("C3_memory_recall_api", False, f"exception:{type(exc).__name__}:{exc}"))

    # Check 4: cross-entrypoint recall via guarded_session_control
    if route_sid and route_home:
        cmd = [
            "python3",
            str(ROOT / "scripts" / "guarded_session_control.py"),
            "continue",
            "--session-id",
            route_sid,
            "--codex-home",
            route_home,
            "--workspace-root",
            str(ROOT),
            "--session-name-prefix",
            route_prefix,
            "--verify-seconds",
            "20",
            "--text",
            "记忆测试C（跨入口）：这是另一个入口，请只回答刚才口令。",
            "--json",
        ]
        cp = _run(cmd)
        report["cross_entry_cmd"] = cmd
        report["cross_entry_stdout"] = cp.stdout
        report["cross_entry_stderr"] = cp.stderr
        report["cross_entry_rc"] = cp.returncode
        ok_dispatch = cp.returncode == 0
        ok_msg = False
        got = ""
        if ok_dispatch:
            ok_msg, got = _poll_last_message(
                matcher=re.compile(re.escape(token)),
                timeout_seconds=120,
            )
        report["cross_entry_last_message"] = got
        checks.append(
            CheckResult(
                name="C4_memory_cross_entrypoint",
                ok=(ok_dispatch and ok_msg),
                detail=f"dispatch_ok={ok_dispatch};token_seen={ok_msg}",
            )
        )
    else:
        checks.append(CheckResult("C4_memory_cross_entrypoint", False, "route_sid_or_codex_home_missing"))

    # Check 5: identity response must stay delivery-lead
    try:
        who_req = {
            "message": "身份测试：请仅回复当前identity_id，格式 identity_id=<id>。",
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {
                "channel": "selftest",
                "task_tag": run_id,
                "case": "identity_bind",
            },
        }
        who_resp = _http_post("/v1/chat/leader/command", who_req)
        report["identity_request"] = who_req
        report["identity_response"] = who_resp
        ok_dispatch = (
            bool(who_resp.get("accepted"))
            and str(who_resp.get("leader_identity_id", "")).strip() == EXPECTED_IDENTITY
        )
        ok_msg, got = _poll_last_message(
            matcher=re.compile(r"identity_id\s*=\s*feiqiao-guard-delivery-lead"),
            timeout_seconds=120,
        )
        report["identity_last_message"] = got
        checks.append(
            CheckResult(
                name="C5_identity_not_drifted",
                ok=(ok_dispatch and ok_msg),
                detail=f"dispatch_ok={ok_dispatch};identity_seen={ok_msg}",
            )
        )
    except (HTTPError, URLError, ValueError) as exc:
        checks.append(CheckResult("C5_identity_not_drifted", False, f"exception:{type(exc).__name__}:{exc}"))

    # Check 6: bridge stream process readiness
    bridge_ready = False
    bridge_detail = "unknown"
    try:
        cp_pane = _run(
            [
                "tmux",
                "-S",
                str(LOCAL_BRIDGE_SOCKET),
                "list-panes",
                "-t",
                LOCAL_BRIDGE_SESSION,
                "-F",
                "#{pane_dead} #{pane_current_command} #{pane_pid}",
            ]
        )
        pane_line = (cp_pane.stdout or "").strip()
        report["bridge_pane"] = pane_line
        pid = ""
        pane_ok = False
        if pane_line:
            parts = pane_line.split()
            if len(parts) >= 3:
                pane_dead = parts[0]
                pane_cmd = parts[1]
                pid = parts[2]
                # Keep this tolerant: pane command may be zsh/bash wrapper while child process runs python.
                pane_ok = pane_dead == "0" and bool(str(pane_cmd).strip())
        cp_lsof = _run(["lsof", "-n", "-P", "-a", "-p", pid, "-i"]) if pid else _run(["true"])
        lsof_text = cp_lsof.stdout or ""
        report["bridge_lsof"] = lsof_text

        bridge_log = ROOT / ".runtime" / "local_bridge" / "bridge.log"
        bridge_log_text = bridge_log.read_text(encoding="utf-8", errors="ignore")[-12000:] if bridge_log.exists() else ""
        report["bridge_log_tail"] = bridge_log_text
        has_started = "bridge_started" in bridge_log_text
        has_endpoint = "endpoint is" in bridge_log_text
        has_link = ("ESTABLISHED" in lsof_text and (":7897" in lsof_text or ":443" in lsof_text))
        heartbeat_fresh, heartbeat_detail = _bridge_heartbeat_fresh(max_age_seconds=120.0)
        report["bridge_heartbeat"] = {
            "fresh": heartbeat_fresh,
            "detail": heartbeat_detail,
            "path": str(BRIDGE_HEARTBEAT_FILE),
        }
        bridge_ready = pane_ok and has_started and has_endpoint and (has_link or heartbeat_fresh)
        bridge_detail = (
            "pane_ok={pane_ok};has_started={has_started};has_endpoint={has_endpoint};"
            "has_link={has_link};heartbeat_fresh={heartbeat_fresh};{heartbeat_detail}"
        ).format(
            pane_ok=pane_ok,
            has_started=has_started,
            has_endpoint=has_endpoint,
            has_link=has_link,
            heartbeat_fresh=heartbeat_fresh,
            heartbeat_detail=heartbeat_detail,
        )
    except Exception as exc:  # noqa: BLE001
        bridge_ready = False
        bridge_detail = f"exception:{type(exc).__name__}:{exc}"
    checks.append(CheckResult("C6_bridge_stream_ready", bridge_ready, bridge_detail))

    # Check 7: no sid=None in recent bridge tail
    sid_none_ok = True
    sid_none_detail = "no_sid_none_in_recent_tail"
    try:
        tail = str(report.get("bridge_log_tail", ""))
        if "sid=None" in tail:
            sid_none_ok = False
            sid_none_detail = "sid_none_detected_in_recent_bridge_tail"
    except Exception as exc:  # noqa: BLE001
        sid_none_ok = False
        sid_none_detail = f"exception:{type(exc).__name__}:{exc}"
    checks.append(CheckResult("C7_no_sid_none_recent", sid_none_ok, sid_none_detail))

    report["checks"] = [
        {"name": c.name, "ok": c.ok, "detail": c.detail}
        for c in checks
    ]
    overall_ok = all(c.ok for c in checks)
    report["overall"] = "PASS" if overall_ok else "FAIL"

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary_lines = ["name\tok\tdetail"]
    summary_lines.extend(c.to_row() for c in checks)
    summary_lines.append(f"OVERALL\t{1 if overall_ok else 0}\t{report['overall']}")
    summary_tsv = out_dir / "summary.tsv"
    summary_tsv.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    print(str(summary_tsv))
    print(str(report_path))
    print(report["overall"])
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
