#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("FQG_VALIDATE_BASE_URL", "http://127.0.0.1:3001").strip()
BRIDGE_LOG = ROOT / ".runtime" / "local_bridge" / "bridge.log"
BRIDGE_HEARTBEAT = ROOT / ".runtime" / "local_bridge" / "bridge_heartbeat.json"
AUDIT_LOG = ROOT / "resource" / "reports" / "approval_audit.jsonl"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    token = str(raw).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _curl_json(method: str, path: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    cmd = ["curl", "-sS", "-X", method, f"{BASE_URL}{path}"]
    merged_headers = {"Content-Type": "application/json"}
    if headers:
        merged_headers.update(headers)
    for key, value in merged_headers.items():
        cmd.extend(["-H", f"{key}: {value}"])
    if payload is not None:
        cmd.extend(["-d", json.dumps(payload, ensure_ascii=False)])
    cp = _run(cmd)
    if cp.returncode != 0:
        raise RuntimeError(f"curl_failed:{method}:{path}:{cp.stderr.strip()}")
    text = cp.stdout.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid_json:{method}:{path}:{text[:300]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"non_object_json:{method}:{path}")
    return data


def _read_deploy_env_token() -> str:
    env_file = ROOT / ".runtime" / "deploy.env"
    if not env_file.exists():
        return ""
    for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("FQG_TRUSTED_BRIDGE_DECISION_TOKEN="):
            continue
        return line.split("=", 1)[1].strip()
    return ""


def _leader_last_message() -> str:
    snapshot = _curl_json("GET", "/v1/chat/leader/snapshot")
    items = snapshot.get("items")
    if not isinstance(items, list):
        return ""
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        identity_id = str(item.get("identity_id", "")).strip()
        if identity_id != "feiqiao-guard-delivery-lead":
            continue
        return str(item.get("last_agent_message", "")).strip()
    return ""


def _poll_marker(marker: str, timeout_seconds: float = 120.0) -> tuple[bool, str]:
    deadline = time.monotonic() + max(5.0, float(timeout_seconds))
    last = ""
    while time.monotonic() < deadline:
        try:
            msg = _leader_last_message()
        except Exception:
            msg = ""
        if msg:
            last = msg
        if marker in msg:
            return True, msg
        time.sleep(2.0)
    return False, last


def _run_replay_script(script: str) -> tuple[bool, str, str]:
    cp = _run(["python3", str(ROOT / "scripts" / script)])
    out = (cp.stdout or "").strip()
    err = (cp.stderr or "").strip()
    combined = "\n".join([part for part in [out, err] if part]).strip()
    pass_flag = cp.returncode == 0 and re.search(r"(^|\n)PASS($|\n)", combined) is not None
    return pass_flag, out, err


def _check_launchd_running() -> tuple[bool, str]:
    cp = _run([str(ROOT / "scripts" / "local_stack_launchd.sh"), "status"])
    text = "\n".join([cp.stdout or "", cp.stderr or ""])
    ok = cp.returncode == 0 and "state = running" in text
    return ok, text.strip()


def _wait_health_route_ready(
    *,
    identity_id: str,
    timeout_seconds: float = 45.0,
    stable_rounds: int = 3,
    interval_seconds: float = 1.5,
) -> tuple[bool, str, dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + max(5.0, float(timeout_seconds))
    required_stable_rounds = max(1, int(stable_rounds))
    stable = 0
    last_detail = ""
    last_health: dict[str, Any] = {}
    last_routes: dict[str, Any] = {}

    while time.monotonic() < deadline:
        try:
            health = _curl_json("GET", "/healthz")
            routes = _curl_json("GET", "/v1/chat/routes")
            last_health = health
            last_routes = routes
        except Exception as exc:  # noqa: BLE001
            stable = 0
            last_detail = f"probe_exception:{type(exc).__name__}:{exc}"
            time.sleep(max(0.2, float(interval_seconds)))
            continue

        status_ok = str(health.get("status", "")).strip() == "ok"
        route_ok = False
        route_reason = "identity_missing"
        for raw in routes.get("items", []) if isinstance(routes.get("items"), list) else []:
            item = raw if isinstance(raw, dict) else {}
            if str(item.get("identity_id", "")).strip() != identity_id:
                continue
            route_ok = (
                str(item.get("route_status", "")).strip().lower() == "ok"
                and bool(str(item.get("session_id", "")).strip())
                and bool(str(item.get("codex_home", "")).strip())
            )
            route_reason = "ok" if route_ok else str(item.get("route_error", "")).strip() or "route_not_ready"
            break

        if status_ok and route_ok:
            stable += 1
            last_detail = (
                f"status_ok={status_ok};route_ok={route_ok};route_reason={route_reason};"
                f"stable={stable}/{required_stable_rounds}"
            )
            if stable >= required_stable_rounds:
                return True, last_detail, last_health, last_routes
        else:
            stable = 0
            last_detail = (
                f"status_ok={status_ok};route_ok={route_ok};route_reason={route_reason};"
                f"stable={stable}/{required_stable_rounds}"
            )
        time.sleep(max(0.2, float(interval_seconds)))

    return False, f"timeout_wait_ready;last={last_detail}", last_health, last_routes


def _restart_local_stack() -> tuple[bool, str]:
    cp = _run([str(ROOT / "scripts" / "local_dingtalk_tmux_stack.sh"), "restart"])
    output = "\n".join(part for part in [(cp.stdout or "").strip(), (cp.stderr or "").strip()] if part).strip()
    ok = cp.returncode == 0
    return ok, output


def _read_bridge_log_text() -> str:
    if not BRIDGE_LOG.exists():
        return ""
    return BRIDGE_LOG.read_text(encoding="utf-8", errors="ignore")


def _read_bridge_heartbeat() -> dict[str, Any]:
    if not BRIDGE_HEARTBEAT.exists():
        return {}
    try:
        data = json.loads(BRIDGE_HEARTBEAT.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_recorded_at(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _recent_real_dingtalk_inbound(*, within_hours: int = 24) -> dict[str, Any]:
    payload = {
        "ok": False,
        "reason": "audit_log_missing",
        "hits": 0,
        "latest_recorded_at": "",
    }
    if not AUDIT_LOG.exists():
        return payload
    threshold = datetime.now(timezone.utc) - timedelta(hours=max(1, int(within_hours)))
    hits = 0
    latest: datetime | None = None
    try:
        lines = AUDIT_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        payload["reason"] = "audit_log_unreadable"
        return payload
    for raw in reversed(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("event", "")).strip() != "chat_inbound_received":
            continue
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("channel", "")).strip() != "dingtalk_stream":
            continue
        if str(row.get("identity_id", "")).strip() != "feiqiao-guard-delivery-lead":
            continue
        msg_id = str(metadata.get("msg_id", "")).strip()
        chat_id = str(metadata.get("chat_id", "")).strip()
        if not msg_id or not chat_id:
            continue
        recorded_at = _parse_recorded_at(str(row.get("recorded_at", "")).strip())
        if recorded_at is None:
            continue
        if recorded_at < threshold:
            continue
        hits += 1
        if latest is None or recorded_at > latest:
            latest = recorded_at
    payload["hits"] = hits
    if hits > 0 and latest is not None:
        payload["ok"] = True
        payload["reason"] = "recent_audit_evidence"
        payload["latest_recorded_at"] = latest.isoformat()
    else:
        payload["reason"] = "no_recent_audit_evidence"
    return payload


def main() -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    run_id = f"guixianren_final_acceptance_{_ts()}"
    out_dir = ROOT / "artifacts" / "ops" / day / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {"run_id": run_id, "base_url": BASE_URL}
    bridge_log_before = _read_bridge_log_text()

    def add_check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) Launchd runtime state
    ok_launchd, launchd_detail = _check_launchd_running()
    add_check("A01_launchd_runtime", ok_launchd, launchd_detail[:1200])

    # 2) Health + route binding (wait until stack exits transient restart window).
    # If stack is down, do one fail-close restart recovery attempt.
    ready_ok, ready_detail, health, routes = _wait_health_route_ready(identity_id="feiqiao-guard-delivery-lead")
    if not ready_ok:
        restarted, restart_detail = _restart_local_stack()
        if restarted:
            ready_ok, recovered_detail, health, routes = _wait_health_route_ready(identity_id="feiqiao-guard-delivery-lead")
            ready_detail = f"{ready_detail};recovery=restart_ok;recovery_probe={recovered_detail}"
        else:
            ready_detail = f"{ready_detail};recovery=restart_failed;restart_detail={restart_detail[:320]}"
    if health:
        evidence["healthz"] = health
    if routes:
        evidence["routes"] = routes
    add_check("A02_health_route", ready_ok, ready_detail)

    # 3) Human-readable natural-language task set
    nl_cases = [
        {
            "id": "NL01",
            "task": "你现在是交付负责人。请给出“今天的3项工作优先级”，每项一句，不要废话。",
        },
        {
            "id": "NL02",
            "task": "模拟 openclaw 协作：输出“leader职责、collab职责、回传格式”三段。",
        },
        {
            "id": "NL03",
            "task": "请写一个可执行的风险检查清单：稳定性、路由、回传，三行。",
        },
    ]
    nl_results: list[dict[str, Any]] = []
    nonce = _ts()[-6:]
    for case in nl_cases:
        cid = case["id"]
        marker = f"{cid}_OK_{nonce}"
        prompt = (
            f"验收任务 {cid}：{case['task']} "
            f"输出要求：第一行只写 {marker}；第二行开始写自然语言结果。"
        )
        payload = {
            "message": prompt,
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {
                "channel": "selftest",
                "task_tag": run_id,
                "case": cid.lower(),
            },
        }
        try:
            resp = _curl_json("POST", "/v1/chat/leader/command", payload)
            dispatch_ok = bool(resp.get("accepted", False))
            seen, last = _poll_marker(marker, timeout_seconds=140)
            ok = dispatch_ok and seen
            detail = f"dispatch_ok={dispatch_ok};marker_seen={seen};marker={marker}"
            add_check(f"A03_{cid}", ok, detail)
            nl_results.append(
                {
                    "case_id": cid,
                    "marker": marker,
                    "dispatch_ok": dispatch_ok,
                    "marker_seen": seen,
                    "last_message": last[:2000],
                }
            )
        except Exception as exc:  # noqa: BLE001
            add_check(f"A03_{cid}", False, f"exception:{type(exc).__name__}:{exc}")
            nl_results.append({"case_id": cid, "error": f"{type(exc).__name__}:{exc}"})
    evidence["natural_language_cases"] = nl_results

    # 4) Trusted approval flow (queue + approve)
    token = _read_deploy_env_token().strip()
    if not token:
        add_check("A04_trusted_approval_flow", False, "missing_token:FQG_TRUSTED_BRIDGE_DECISION_TOKEN")
    else:
        try:
            created = _curl_json(
                "POST",
                "/v1/approvals",
                {
                    "command": "python3 manage.py migrate",
                    "terminal_session_id": f"{run_id}-trusted",
                    "source": "pytest",
                    "extra_context": {"channel": "selftest", "task_tag": run_id, "case": "trusted_approval"},
                },
            )
            request_id = str(created.get("request_id", "")).strip()
            queue = _curl_json("GET", "/v1/approvals/queue?status=PENDING&limit=20")
            queue_items = queue.get("items") if isinstance(queue.get("items"), list) else []
            in_queue = any(str((item if isinstance(item, dict) else {}).get("request_id", "")).strip() == request_id for item in queue_items)
            approved = _curl_json(
                "POST",
                f"/v1/approvals/{request_id}/trusted-decision",
                {
                    "action": "approve",
                    "approver": "guixianren-bridge",
                },
                headers={"x-fqg-bridge-token": token},
            )
            state = _curl_json("GET", f"/v1/approvals/{request_id}")
            approved_ok = str(approved.get("status", "")).strip() == "APPROVED"
            state_ok = str(state.get("status", "")).strip() == "APPROVED"
            ok = bool(request_id) and in_queue and approved_ok and state_ok
            add_check(
                "A04_trusted_approval_flow",
                ok,
                f"request_id={request_id};in_queue={in_queue};approved_ok={approved_ok};state_ok={state_ok}",
            )
            evidence["trusted_approval_flow"] = {
                "request_id": request_id,
                "created": created,
                "queue": queue,
                "approved": approved,
                "state": state,
            }
        except Exception as exc:  # noqa: BLE001
            add_check("A04_trusted_approval_flow", False, f"exception:{type(exc).__name__}:{exc}")

    # 5) Simulated inbound bridge replay suites
    pass_sim, out_sim, err_sim = _run_replay_script("run_simulated_inbound_bridge_e2e.py")
    add_check("A05_simulated_inbound_bridge", pass_sim, "PASS" if pass_sim else f"FAIL:{(err_sim or out_sim)[:300]}")
    evidence["simulated_inbound_bridge"] = {"stdout": out_sim, "stderr": err_sim}

    pass_appr, out_appr, err_appr = _run_replay_script("run_guixianren_approval_bridge_replay.py")
    add_check("A06_simulated_approval_bridge", pass_appr, "PASS" if pass_appr else f"FAIL:{(err_appr or out_appr)[:300]}")
    evidence["simulated_approval_bridge"] = {"stdout": out_appr, "stderr": err_appr}

    # 6) Real DingTalk inbound evidence gate (hard fail-close):
    # must observe new inbound callback + final-result reply in bridge log.
    bridge_log_after = _read_bridge_log_text()
    heartbeat = _read_bridge_heartbeat()
    tail = bridge_log_after
    if bridge_log_before and bridge_log_after.startswith(bridge_log_before):
        tail = bridge_log_after[len(bridge_log_before) :]
    has_inbound = "inbound msg_id=" in tail
    has_result_reply = any(
        token in tail
        for token in (
            "reply_sent tag=dispatch_final_result",
            "reply_sent tag=dispatch_final_result_late",
            "reply_sent tag=dispatch_progress_update",
            "reply_sent tag=dispatch_late_progress_update",
        )
    )
    counters = heartbeat.get("counters") if isinstance(heartbeat.get("counters"), dict) else {}
    ages = heartbeat.get("ages") if isinstance(heartbeat.get("ages"), dict) else {}
    callback_count = int(counters.get("callback_count", 0) or 0)
    inbound_count = int(counters.get("inbound_count", 0) or 0)
    reply_count = int(counters.get("reply_count", 0) or 0)
    reject_count = int(counters.get("rejected_count", 0) or 0)
    callback_age = str(ages.get("callback_age_seconds", "-"))
    inbound_age = str(ages.get("inbound_age_seconds", "-"))
    reply_age = str(ages.get("reply_age_seconds", "-"))
    failure_reason = ""
    if not has_inbound:
        if callback_count == 0:
            failure_reason = "no_stream_callback_observed"
        elif inbound_count == 0:
            failure_reason = "callbacks_seen_but_no_inbound_accepted"
    if not has_result_reply and failure_reason == "":
        if reply_count == 0:
            failure_reason = "no_bridge_reply_observed"
        else:
            failure_reason = "no_result_or_progress_reply_tag"
    recent_inbound = _recent_real_dingtalk_inbound(within_hours=24)
    realtime_ok = has_inbound and has_result_reply
    historical_ok = bool(recent_inbound.get("ok", False))
    allow_historical_fallback = _env_flag("FQG_ACCEPTANCE_ALLOW_HISTORICAL_FALLBACK", default=False)
    evidence_gate_ok = realtime_ok or (allow_historical_fallback and historical_ok)
    add_check(
        "A07_real_dingtalk_inbound_evidence",
        evidence_gate_ok,
        (
            f"gate_ok={evidence_gate_ok};allow_historical_fallback={allow_historical_fallback};"
            f"realtime_ok={realtime_ok};historical_ok={historical_ok};"
            f"has_inbound={has_inbound};has_result_reply={has_result_reply};"
            f"callback_count={callback_count};inbound_count={inbound_count};reply_count={reply_count};"
            f"reject_count={reject_count};callback_age={callback_age};inbound_age={inbound_age};"
            f"reply_age={reply_age};failure_reason={failure_reason or '-'};"
            f"recent_hits={recent_inbound.get('hits')};latest={recent_inbound.get('latest_recorded_at')};"
            f"recent_reason={recent_inbound.get('reason')}"
        ),
    )
    evidence["bridge_log_delta_tail"] = tail[-8000:]
    evidence["bridge_heartbeat"] = heartbeat
    evidence["recent_dingtalk_inbound_audit"] = recent_inbound

    overall_ok = all(bool(item.get("ok")) for item in checks)
    summary = {
        "run_id": run_id,
        "overall": "PASS" if overall_ok else "FAIL",
        "checks_total": len(checks),
        "checks_passed": sum(1 for item in checks if item.get("ok")),
        "checks_failed": sum(1 for item in checks if not item.get("ok")),
        "checks": checks,
    }
    evidence["summary"] = summary

    report_json = out_dir / "report.json"
    report_json.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary_tsv = out_dir / "summary.tsv"
    lines = ["name\tok\tdetail"]
    for item in checks:
        lines.append(f"{item['name']}\t{1 if item['ok'] else 0}\t{item['detail']}")
    lines.append(f"OVERALL\t{1 if overall_ok else 0}\t{summary['overall']}")
    summary_tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary_md = out_dir / "summary.md"
    md_lines = [
        f"# 龟仙人最终验收 {run_id}",
        "",
        f"- overall: **{summary['overall']}**",
        f"- passed: {summary['checks_passed']}/{summary['checks_total']}",
        "",
        "## 检查结果",
    ]
    for item in checks:
        mark = "PASS" if item["ok"] else "FAIL"
        md_lines.append(f"- {item['name']}: {mark} | {item['detail']}")
    md_lines.append("")
    md_lines.append(f"- report: {report_json}")
    md_lines.append(f"- summary_tsv: {summary_tsv}")
    summary_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(str(summary_tsv))
    print(str(report_json))
    print(str(summary_md))
    print(summary["overall"])
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
