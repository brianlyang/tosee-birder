#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("FQG_VALIDATE_BASE_URL", "http://127.0.0.1:3001").strip()
LEADER_ID = os.getenv("FQG_CHAT_LEADER_IDENTITY_ID", "feiqiao-guard-delivery-lead").strip()
COLLAB_ID = os.getenv("FQG_CHAT_COLLAB_IDENTITY_ID", "feiqiao-guard-collab-executor").strip()
AUDIT_LOG = ROOT / "resource" / "reports" / "approval_audit.jsonl"

DISPATCH_VERIFY_SECONDS = float(os.getenv("FQG_DEEP_SUITE_VERIFY_SECONDS", "20").strip() or "20")
POLL_TIMEOUT_SECONDS = float(os.getenv("FQG_DEEP_SUITE_POLL_TIMEOUT_SECONDS", "90").strip() or "90")
POLL_INTERVAL_SECONDS = float(os.getenv("FQG_DEEP_SUITE_POLL_INTERVAL_SECONDS", "2").strip() or "2")
HTTP_RETRY = int(os.getenv("FQG_DEEP_SUITE_HTTP_RETRY", "4").strip() or "4")
HTTP_RETRY_SLEEP = float(os.getenv("FQG_DEEP_SUITE_HTTP_RETRY_SLEEP_SECONDS", "1").strip() or "1")


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _http_json(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    body = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, max(1, HTTP_RETRY) + 1):
        try:
            req = Request(url=url, method=method, data=body, headers=headers)
            with urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError(f"non_object_response:{path}")
            return obj
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= max(1, HTTP_RETRY):
                break
            time.sleep(max(0.2, HTTP_RETRY_SLEEP))
    raise RuntimeError(f"http_failed:{method}:{path}:{type(last_error).__name__}:{last_error}")


def _leader_last_message() -> str:
    snapshot = _http_json("GET", "/v1/chat/leader/snapshot")
    items = snapshot.get("items")
    if not isinstance(items, list):
        return ""
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        if str(item.get("identity_id", "")).strip() != LEADER_ID:
            continue
        return str(item.get("last_agent_message", "")).strip()
    return ""


def _wait_marker(marker: str, timeout_seconds: float) -> tuple[bool, str]:
    deadline = time.monotonic() + max(5.0, float(timeout_seconds))
    last = ""
    while time.monotonic() < deadline:
        msg = _leader_last_message()
        if msg:
            last = msg
        if marker in msg:
            return True, msg
        time.sleep(max(0.4, POLL_INTERVAL_SECONDS))
    return False, last


def _case_payload(case_id: str, marker: str, task: str, run_id: str, retry: bool = False) -> dict[str, Any]:
    if not retry:
        message = (
            f"双实例深测 {case_id}：{task}\n"
            f"输出要求：第一行必须且只能是 {marker}；第二行给出不超过28字中文结果。"
        )
    else:
        message = (
            f"格式纠偏 {case_id}：上一轮未按格式回传。\n"
            f"请严格执行：第一行必须且只能是 {marker}；第二行给出不超过28字中文结果。"
        )
    return {
        "message": message,
        "auto_collab": True,
        "verify_seconds": DISPATCH_VERIFY_SECONDS,
        "collab_verify_seconds": DISPATCH_VERIFY_SECONDS,
        "metadata": {
            "channel": "selftest",
            "task_tag": run_id,
            "case": case_id,
            "entry": "dual_identity_deep_suite",
            "retry": retry,
        },
    }


def _load_cases() -> list[tuple[str, str, str]]:
    # case_id, marker_suffix, natural-language task
    tasks = [
        "给出今天三项交付优先级",
        "写出两条稳定性门禁",
        "写出一条回滚策略",
        "给出leader与collab职责分工",
        "给出一条避免串线的规则",
        "写出一条审批失败收口动作",
        "给出一条网络波动下的重试原则",
        "给出一条日志采样建议",
        "给出一条运行态健康阈值",
        "给出一条审计证据最小集合",
        "写出一条任务完成定义",
        "写出一条异常升级触发条件",
        "写出一条会话隔离要求",
        "写出一条跨实例知识共享边界",
        "写出一条防硬编码约束",
        "写出一条故障复盘字段",
        "给出一条部署前检查项",
        "给出一条上线后巡检项",
        "给出一条恢复后验证项",
        "写出一条消息幂等策略",
        "写出一条回执格式要求",
        "写出一条超时强收敛策略",
        "写出一条任务标识规则",
        "写出一条路由冲突处理策略",
        "写出一条审批拒绝后反馈策略",
        "写出一条图片回传兼容原则",
        "写出一条附件回传兼容原则",
        "写出一条长连接守护原则",
        "写出一条老师-协作实例纠偏原则",
        "写出一条通知即审计执行口令",
    ]
    cases: list[tuple[str, str, str]] = []
    for idx, task in enumerate(tasks, start=1):
        cid = f"C{idx:02d}"
        marker = f"{cid}_DUAL_OK_{datetime.now().strftime('%H%M%S')}"
        cases.append((cid, marker, task))
    return cases


@dataclass
class CaseResult:
    case_id: str
    marker: str
    ok: bool
    dispatch_ok: bool
    leader_state: str
    collab_state: str
    marker_seen: bool
    attempts: int
    detail: str
    last_message: str


def _route_gate() -> tuple[bool, str]:
    routes = _http_json("GET", "/v1/chat/routes")
    items = routes.get("items")
    if not isinstance(items, list):
        return False, "routes_items_missing"
    need = {LEADER_ID, COLLAB_ID}
    seen: dict[str, dict[str, Any]] = {}
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        iid = str(item.get("identity_id", "")).strip()
        if iid in need:
            seen[iid] = item
    if set(seen) != need:
        return False, f"missing_identity:{','.join(sorted(need - set(seen)))}"
    parts: list[str] = []
    ok = True
    for iid in sorted(need):
        row = seen[iid]
        status = str(row.get("route_status", "")).strip().lower()
        sid = str(row.get("session_id", "")).strip()
        home = str(row.get("codex_home", "")).strip()
        one_ok = (status == "ok" and bool(sid) and bool(home))
        ok = ok and one_ok
        parts.append(f"{iid}:status={status or '-'} sid={'Y' if sid else 'N'} home={'Y' if home else 'N'}")
    return ok, "; ".join(parts)


def _audit_events_after(start_lines: int, run_id: str) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    if not AUDIT_LOG.exists():
        return mapping
    try:
        lines = AUDIT_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return mapping
    tail = lines[start_lines:] if start_lines < len(lines) else []
    for raw in tail:
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
        if str(metadata.get("task_tag", "")).strip() != run_id:
            continue
        case_id = str(metadata.get("case", "")).strip()
        if not case_id:
            continue
        identity_id = str(row.get("identity_id", "")).strip()
        if not identity_id:
            continue
        mapping.setdefault(case_id, set()).add(identity_id)
    return mapping


def main() -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    run_id = f"dual_identity_deep_suite_{_ts()}"
    out_dir = ROOT / "artifacts" / "ops" / day / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    start_audit_lines = 0
    if AUDIT_LOG.exists():
        try:
            start_audit_lines = len(AUDIT_LOG.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            start_audit_lines = 0

    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "run_id": run_id,
        "base_url": BASE_URL,
        "leader_id": LEADER_ID,
        "collab_id": COLLAB_ID,
    }

    health = _http_json("GET", "/healthz")
    h_ok = str(health.get("status", "")).strip().lower() == "ok"
    checks.append({"name": "D00_healthz", "ok": h_ok, "detail": f"status={health.get('status')!r}"})
    evidence["healthz"] = health

    route_ok, route_detail = _route_gate()
    checks.append({"name": "D01_route_gate", "ok": route_ok, "detail": route_detail})
    if not (h_ok and route_ok):
        evidence["checks"] = checks
        evidence["overall"] = "FAIL"
        report = out_dir / "report.json"
        report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary = out_dir / "summary.tsv"
        lines = ["name\tok\tdetail"] + [f"{c['name']}\t{1 if c['ok'] else 0}\t{c['detail']}" for c in checks]
        lines.append("OVERALL\t0\tFAIL")
        summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(str(summary))
        print(str(report))
        print("FAIL")
        return 1

    results: list[CaseResult] = []
    for case_id, marker, task in _load_cases():
        attempts = 0
        dispatch_ok = False
        leader_state = ""
        collab_state = ""
        marker_seen = False
        last_message = ""
        detail = ""
        for retry in (False, True):
            attempts += 1
            payload = _case_payload(case_id=case_id, marker=marker, task=task, run_id=run_id, retry=retry)
            try:
                resp = _http_json("POST", "/v1/chat/leader/command", payload)
            except Exception as exc:  # noqa: BLE001
                detail = f"dispatch_exception:{type(exc).__name__}:{exc}"
                continue
            dispatch_ok = bool(resp.get("accepted", False))
            leader = resp.get("leader_result") if isinstance(resp.get("leader_result"), dict) else {}
            collab = resp.get("collab_result") if isinstance(resp.get("collab_result"), dict) else {}
            leader_state = str(leader.get("delivery_state", "")).strip().lower()
            collab_state = str(collab.get("delivery_state", "")).strip().lower()
            delivery_ok = leader_state in {"confirmed", "queued"} and collab_state in {"confirmed", "queued"}
            if not (dispatch_ok and delivery_ok):
                detail = (
                    f"dispatch_ok={dispatch_ok};leader_state={leader_state or '-'};"
                    f"collab_state={collab_state or '-'}"
                )
                continue
            marker_seen, last_message = _wait_marker(marker=marker, timeout_seconds=POLL_TIMEOUT_SECONDS)
            detail = (
                f"dispatch_ok={dispatch_ok};leader_state={leader_state};"
                f"collab_state={collab_state};marker_seen={marker_seen};attempt={attempts}"
            )
            if marker_seen:
                break
        ok = dispatch_ok and marker_seen
        results.append(
            CaseResult(
                case_id=case_id,
                marker=marker,
                ok=ok,
                dispatch_ok=dispatch_ok,
                leader_state=leader_state or "-",
                collab_state=collab_state or "-",
                marker_seen=marker_seen,
                attempts=attempts,
                detail=detail or "unknown",
                last_message=last_message[:4000],
            )
        )

    evidence["cases"] = [
        {
            "case_id": item.case_id,
            "marker": item.marker,
            "ok": item.ok,
            "dispatch_ok": item.dispatch_ok,
            "leader_state": item.leader_state,
            "collab_state": item.collab_state,
            "marker_seen": item.marker_seen,
            "attempts": item.attempts,
            "detail": item.detail,
            "last_message": item.last_message,
        }
        for item in results
    ]

    audit_case_map = _audit_events_after(start_lines=start_audit_lines, run_id=run_id)
    audit_checks: list[dict[str, Any]] = []
    for item in results:
        ids = audit_case_map.get(item.case_id, set())
        ok = LEADER_ID in ids and COLLAB_ID in ids
        audit_checks.append(
            {
                "case_id": item.case_id,
                "ok": ok,
                "received_identities": sorted(ids),
            }
        )
    evidence["audit_case_map"] = {k: sorted(v) for k, v in audit_case_map.items()}
    evidence["audit_checks"] = audit_checks

    cases_ok = all(item.ok for item in results)
    audit_ok = all(bool(row.get("ok")) for row in audit_checks) if audit_checks else False
    checks.append(
        {
            "name": "D02_cases_marker_and_dispatch",
            "ok": cases_ok,
            "detail": f"pass={sum(1 for r in results if r.ok)}/{len(results)}",
        }
    )
    checks.append(
        {
            "name": "D03_audit_dual_identity",
            "ok": audit_ok,
            "detail": f"pass={sum(1 for a in audit_checks if a.get('ok'))}/{len(audit_checks)}",
        }
    )

    overall = all(bool(c["ok"]) for c in checks)
    evidence["checks"] = checks
    evidence["overall"] = "PASS" if overall else "FAIL"

    report = out_dir / "report.json"
    report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = out_dir / "summary.tsv"
    lines = ["name\tok\tdetail"]
    lines.extend(f"{c['name']}\t{1 if c['ok'] else 0}\t{c['detail']}" for c in checks)
    for item in results:
        lines.append(
            f"{item.case_id}\t{1 if item.ok else 0}\t{item.detail}"
        )
    lines.append(f"OVERALL\t{1 if overall else 0}\t{evidence['overall']}")
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary_md = out_dir / "summary.md"
    md_lines = [
        f"# Dual Identity Deep Suite {run_id}",
        "",
        f"- overall: **{evidence['overall']}**",
        f"- cases_pass: {sum(1 for r in results if r.ok)}/{len(results)}",
        f"- audit_pass: {sum(1 for a in audit_checks if a.get('ok'))}/{len(audit_checks)}",
        "",
        "## Gates",
    ]
    for c in checks:
        md_lines.append(f"- {c['name']}: {'PASS' if c['ok'] else 'FAIL'} | {c['detail']}")
    md_lines.extend(
        [
            "",
            "## Evidence",
            f"- report: {report}",
            f"- summary_tsv: {summary}",
        ]
    )
    summary_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(str(summary))
    print(str(report))
    print(str(summary_md))
    print(evidence["overall"])
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
