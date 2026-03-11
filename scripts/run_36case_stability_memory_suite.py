#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("FQG_VALIDATE_BASE_URL", "http://127.0.0.1:3001").strip()
EXPECTED_ID = "feiqiao-guard-delivery-lead"
ONLINE_MODE = os.getenv("FQG_SUITE_ONLINE_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
REMOTE_HOST = os.getenv("FQG_REMOTE_HOST", "8.140.215.219").strip()
REMOTE_USER = os.getenv("FQG_REMOTE_USER", "root").strip()
REMOTE_ROOT = os.getenv("FQG_REMOTE_ROOT", "/root/feiqiao-guard").strip()
REMOTE_SSH_PASSWORD = os.getenv("FQG_REMOTE_SSH_PASSWORD", "").strip()
REMOTE_SSH_CONNECT_TIMEOUT = os.getenv("FQG_REMOTE_SSH_CONNECT_TIMEOUT", "8").strip()
REMOTE_TMUX_SOCKET = ""
REMOTE_TMUX_SESSION = ""
DINGTALK_BROADCAST = os.getenv("FQG_SUITE_DINGTALK_BROADCAST", "1").strip().lower() in {"1", "true", "yes", "on"}
DINGTALK_CLIENT_ID = os.getenv("FQG_DINGTALK_STREAM_CLIENT_ID", "").strip()
DINGTALK_CLIENT_SECRET = os.getenv("FQG_DINGTALK_STREAM_CLIENT_SECRET", "").strip()
DINGTALK_CHAT_ID = os.getenv("FQG_DINGTALK_CHAT_ID", "").strip()
DINGTALK_ROBOT_CODE = os.getenv("FQG_DINGTALK_ROBOT_CODE", DINGTALK_CLIENT_ID).strip()
POLL_INTERVAL_SECONDS = float(os.getenv("FQG_SUITE_POLL_INTERVAL_SECONDS", "1.6").strip() or "1.6")
TMUX_TAIL_LINES = int(os.getenv("FQG_SUITE_TMUX_TAIL_LINES", "1200").strip() or "1200")
_TOKEN_CACHE: dict[str, Any] = {"token": "", "fetched_at": 0.0}


def ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def http_get(path: str) -> dict[str, Any]:
    req = Request(f"{BASE_URL}{path}", method="GET")
    with urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def http_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"{BASE_URL}{path}",
        method="POST",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def run_remote_cmd(command: str) -> subprocess.CompletedProcess[str]:
    if not REMOTE_SSH_PASSWORD:
        return subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="missing_remote_ssh_password")
    return run_cmd(
        [
            "sshpass",
            "-p",
            REMOTE_SSH_PASSWORD,
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={REMOTE_SSH_CONNECT_TIMEOUT}",
            f"{REMOTE_USER}@{REMOTE_HOST}",
            command,
        ]
    )


def dingtalk_access_token() -> str:
    now = time.time()
    cached = str(_TOKEN_CACHE.get("token", "")).strip()
    fetched_at = float(_TOKEN_CACHE.get("fetched_at", 0.0) or 0.0)
    if cached and now - fetched_at < 6000:
        return cached
    query = (
        f"https://oapi.dingtalk.com/gettoken?appkey={DINGTALK_CLIENT_ID}&appsecret={DINGTALK_CLIENT_SECRET}"
    )
    req = Request(query, method="GET")
    with urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    token = str(payload.get("access_token", "")).strip()
    if token:
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["fetched_at"] = now
    return token


def dingtalk_send_text(content: str) -> tuple[bool, str]:
    if not DINGTALK_BROADCAST:
        return True, "broadcast_disabled"
    if not DINGTALK_CLIENT_ID or not DINGTALK_CLIENT_SECRET or not DINGTALK_CHAT_ID:
        return False, "missing_dingtalk_credentials_or_chat_id"
    try:
        token = dingtalk_access_token()
        if not token:
            return False, "empty_access_token"
        payload = {
            "robotCode": DINGTALK_ROBOT_CODE or DINGTALK_CLIENT_ID,
            "openConversationId": DINGTALK_CHAT_ID,
            "msgKey": "sampleText",
            "msgParam": json.dumps({"content": content}, ensure_ascii=False),
        }
        req = Request(
            "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
            method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-acs-dingtalk-access-token": token,
            },
        )
        with urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        return True, body[:300]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}:{exc}"


def compact_text(raw: str, max_len: int = 280) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    if len(text) <= max_len:
        return text
    return f"{text[:max_len-3]}..."


def announce_case_start(case_name: str, summary: str, *, prompt_text: str = "", expected: str = "") -> None:
    print(f"{case_name}: START | {summary}", flush=True)
    lines = [
        "[36Case-START]",
        f"案例: {case_name}",
        f"任务语义: {summary}",
    ]
    if prompt_text:
        lines.append(f"指令原文: {compact_text(prompt_text)}")
    if expected:
        lines.append(f"期望回复: {compact_text(expected)}")
    ok, info = dingtalk_send_text("\n".join(lines))
    if not ok:
        print(f"{case_name}: START_BROADCAST_FAIL | {info}", flush=True)


def compute_guard_tmux(codex_home: str, sid: str, prefix: str) -> tuple[str, str]:
    codex_home_norm = str(Path(codex_home).as_posix()).strip()
    preferred = f"{codex_home_norm}/session_monitor/tmux/{sid}.sock"
    if len(preferred) <= 100:
        socket_path = preferred
    else:
        digest = hashlib.sha1(f"{codex_home_norm}:{sid}".encode("utf-8")).hexdigest()[:16]
        runtime_root = str(Path(codex_home_norm).parent.parent)
        socket_path = f"{runtime_root}/tmux/{digest}.sock"
    safe_prefix = re.sub(r"[^a-zA-Z0-9_-]", "", prefix.strip() or "fqg")
    session_name = f"{safe_prefix}-{sid[:8]}"
    return socket_path, session_name


def leader_last_message() -> str:
    snap = http_get("/v1/chat/leader/snapshot")
    for item in snap.get("items", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("identity_id", "")).strip() == EXPECTED_ID:
            raw = item.get("last_agent_message")
            if raw is None:
                return ""
            return str(raw).strip()
    return ""


def wait_for(pattern: str, timeout: float = 120.0) -> tuple[bool, str]:
    rgx = re.compile(pattern, re.S)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        msg = ""
        try:
            msg = leader_last_message()
        except Exception:  # noqa: BLE001
            msg = ""
        if ONLINE_MODE:
            if (not msg) and REMOTE_TMUX_SOCKET and REMOTE_TMUX_SESSION:
                cp = run_remote_cmd(
                    (
                        f"tmux -S {shlex.quote(REMOTE_TMUX_SOCKET)} capture-pane -pt "
                        f"{shlex.quote(REMOTE_TMUX_SESSION)} | tail -n {max(120, TMUX_TAIL_LINES)}"
                    )
                )
                msg = cp.stdout if cp.returncode == 0 else f"{cp.stdout}\n{cp.stderr}".strip()
        if msg:
            last = msg
        if rgx.search(msg):
            return True, msg
        time.sleep(max(0.6, POLL_INTERVAL_SECONDS))
    return False, last


def extract_slot_value(message: str, marker: str) -> str:
    # Expected format: "<marker> VALUE <token>"
    m = re.search(rf"{re.escape(marker)}\s+VALUE\s+([A-Za-z0-9_.:/-]+)", message, re.S)
    if not m:
        return ""
    return m.group(1).strip()


def append_case(summary: list[str], detail_cases: list[dict[str, Any]], name: str, ok: bool, detail: str, extra: dict[str, Any] | None = None) -> None:
    summary.append(f"{name}\t{1 if ok else 0}\t{detail}")
    print(f"{name}: {'PASS' if ok else 'FAIL'} | {detail}", flush=True)
    lines = [
        "[36Case-RESULT]",
        f"案例: {name}",
        f"状态: {'PASS' if ok else 'FAIL'}",
        f"结果摘要: {compact_text(detail)}",
    ]
    if extra and extra.get("last_message"):
        lines.append(f"实际回复: {compact_text(str(extra.get('last_message', '')), max_len=360)}")
    broadcast_text = "\n".join(lines)
    b_ok, b_info = dingtalk_send_text(broadcast_text)
    if not b_ok:
        print(f"{name}: RESULT_BROADCAST_FAIL | {b_info}", flush=True)
    payload: dict[str, Any] = {"name": name, "ok": ok, "detail": detail}
    if extra:
        payload.update(extra)
    detail_cases.append(payload)


def main() -> int:
    run_id = f"suite36_{ts()}"
    out_dir = ROOT / "artifacts" / "ops" / datetime.now().strftime("%Y-%m-%d") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_lines: list[str] = ["name\tok\tdetail"]
    case_details: list[dict[str, Any]] = []

    # pre-flight info
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
    if ONLINE_MODE and sid and codex_home:
        global REMOTE_TMUX_SOCKET, REMOTE_TMUX_SESSION
        REMOTE_TMUX_SOCKET, REMOTE_TMUX_SESSION = compute_guard_tmux(codex_home, sid, prefix)

    probe_ok = False
    for item in snapshot.get("items", []):
        if isinstance(item, dict) and str(item.get("identity_id", "")).strip() == EXPECTED_ID:
            probe_ok = bool(item.get("process_probe_ok", False))
            break

    announce_case_start(
        "CASE01_preflight_route_health",
        "检查线上 health + route 基础可用性",
        prompt_text="GET /healthz + GET /v1/chat/routes",
        expected="health=ok 且 leader 有 session_id/codex_home",
    )
    append_case(
        summary_lines,
        case_details,
        "CASE01_preflight_route_health",
        str(health.get("status", "")).strip() == "ok" and bool(sid) and bool(codex_home),
        f"health={health.get('status')};sid={bool(sid)};codex_home={bool(codex_home)}",
    )
    announce_case_start(
        "CASE02_preflight_probe",
        "检查 leader probe 和 route_status",
        prompt_text="GET /v1/chat/leader/snapshot",
        expected="process_probe_ok=true 且 route_status=ok",
    )
    append_case(
        summary_lines,
        case_details,
        "CASE02_preflight_probe",
        probe_ok and str(route_item.get("route_status", "")).strip().lower() == "ok",
        f"probe_ok={probe_ok};route_status={route_item.get('route_status')}",
    )

    # CASE03-CASE22: 10 rounds memory write+recall via leader API (20 cases)
    memory_tokens: list[str] = []
    memory_slots: list[tuple[str, str]] = []
    for i in range(1, 11):
        slot = f"S{i:02d}"
        token = f"M{i:02d}-{ts()[-6:]}"
        memory_tokens.append(token)
        memory_slots.append((slot, token))
        marker = f"W{i:02d}-{ts()[-4:]}"
        write_case_name = f"CASE{2*i+1:02d}_memory_write_{i:02d}"
        write_prompt = (
            f"稳定性写入{i}：将槽位 {slot} 设置为 {token}。"
            f"只回复 {marker} ACK {slot}，不要输出其他字符。"
        )
        announce_case_start(
            write_case_name,
            f"写入记忆槽位 {slot}",
            prompt_text=write_prompt,
            expected=f"{marker} ACK {slot}",
        )
        req_write = {
            "message": write_prompt,
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {"channel": "selftest", "task_tag": run_id, "case": f"write_{i}"},
        }
        resp_write = http_post("/v1/chat/leader/command", req_write)
        seen_write, msg_write = wait_for(rf"{re.escape(marker)}\s+ACK\s+{re.escape(slot)}", timeout=140)
        write_ok = bool(resp_write.get("accepted")) and seen_write
        append_case(
            summary_lines,
            case_details,
            write_case_name,
            write_ok,
            f"dispatch_ok={bool(resp_write.get('accepted'))};seen={seen_write}",
            {"last_message": msg_write},
        )

        recall_marker = f"R{i:02d}-{ts()[-4:]}"
        recall_case_name = f"CASE{2*i+2:02d}_memory_recall_{i:02d}"
        recall_prompt = (
            f"稳定性读取{i}：读取槽位 {slot}。"
            f"只回复 {recall_marker} VALUE <槽位值>，不要输出其他字符。"
        )
        announce_case_start(
            recall_case_name,
            f"读取记忆槽位 {slot} 并校验值",
            prompt_text=recall_prompt,
            expected=f"{recall_marker} VALUE {token}",
        )
        req_recall = {
            "message": recall_prompt,
            "auto_collab": False,
            "verify_seconds": 20,
            "metadata": {"channel": "selftest", "task_tag": run_id, "case": f"recall_{i}"},
        }
        resp_recall = http_post("/v1/chat/leader/command", req_recall)
        seen_recall, msg_recall = wait_for(rf"{re.escape(recall_marker)}\s+VALUE\s+", timeout=140)
        recalled = extract_slot_value(msg_recall, recall_marker)
        recall_ok = bool(resp_recall.get("accepted")) and seen_recall and recalled == token
        append_case(
            summary_lines,
            case_details,
            recall_case_name,
            recall_ok,
            (
                f"dispatch_ok={bool(resp_recall.get('accepted'))};seen={seen_recall};"
                f"expected={token};recalled={recalled or '<empty>'}"
            ),
            {"last_message": msg_recall, "expected_token": token, "recalled_token": recalled},
        )

    # CASE23-CASE27: cross-entry recall via guarded_session_control (5 cases)
    for idx, (slot, token) in enumerate(memory_slots[:5], start=1):
        marker = f"X{idx:02d}-{ts()[-4:]}"
        cross_case_name = f"CASE{22+idx:02d}_cross_entry_recall_{idx:02d}"
        text = (
            f"跨入口读取{idx}：读取槽位 {slot}。"
            f"只回复 {marker} VALUE <槽位值>，不要输出其他字符。"
        )
        announce_case_start(
            cross_case_name,
            f"跨入口读取槽位 {slot} 并校验值",
            prompt_text=text,
            expected=f"{marker} VALUE {token}",
        )
        if ONLINE_MODE:
            remote_cmd = (
                f"python3 {shlex.quote(str(Path(REMOTE_ROOT) / 'scripts' / 'guarded_session_control.py'))} continue "
                f"--session-id {shlex.quote(sid)} "
                f"--codex-home {shlex.quote(codex_home)} "
                f"--workspace-root {shlex.quote(REMOTE_ROOT)} "
                f"--session-name-prefix {shlex.quote(prefix)} "
                f"--verify-seconds 20 "
                f"--text {shlex.quote(text)} "
                "--json"
            )
            cp = run_remote_cmd(remote_cmd)
        else:
            cp = run_cmd(
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
                    text,
                    "--json",
                ]
            )
        seen, msg = wait_for(rf"{re.escape(marker)}\s+VALUE\s+", timeout=140)
        recalled = extract_slot_value(msg, marker)
        ok = cp.returncode == 0 and seen and recalled == token
        log_path = out_dir / f"CASE{22+idx:02d}_cross_entry.log"
        log_path.write_text(f"STDOUT:\n{cp.stdout}\n\nSTDERR:\n{cp.stderr}\n", encoding="utf-8")
        append_case(
            summary_lines,
            case_details,
            cross_case_name,
            ok,
            (
                f"dispatch_ok={cp.returncode==0};seen={seen};"
                f"expected={token};recalled={recalled or '<empty>'};log={log_path}"
            ),
            {"last_message": msg, "expected_token": token, "recalled_token": recalled},
        )

    # CASE28-CASE33: 3 restart cycles + recall (6 cases)
    for j in range(1, 4):
        restart_case_name = f"CASE{27+(j*2-1):02d}_restart_cycle_{j}"
        announce_case_start(
            restart_case_name,
            "重启线上 api + stream 并检查 health",
            prompt_text="systemctl restart feiqiao-guard-api feiqiao-guard-dingtalk-stream",
            expected="healthz 返回 ok",
        )
        if ONLINE_MODE:
            cp_restart = run_remote_cmd(
                "systemctl restart feiqiao-guard-api feiqiao-guard-dingtalk-stream "
                "&& sleep 2 "
                "&& curl -fsS http://127.0.0.1:3001/healthz >/dev/null"
            )
        else:
            cp_restart = run_cmd([str(ROOT / "scripts" / "local_dingtalk_tmux_stack.sh"), "restart"])
        restart_ok = cp_restart.returncode == 0
        restart_log = out_dir / f"CASE{27+(j*2-1):02d}_restart_{j}.log"
        restart_log.write_text(f"STDOUT:\n{cp_restart.stdout}\n\nSTDERR:\n{cp_restart.stderr}\n", encoding="utf-8")
        append_case(
            summary_lines,
            case_details,
            restart_case_name,
            restart_ok,
            f"rc={cp_restart.returncode};log={restart_log}",
        )

        slot, token = memory_slots[-j]
        marker = f"Z{j:02d}-{ts()[-4:]}"
        post_restart_case_name = f"CASE{27+(j*2):02d}_post_restart_recall_{j}"
        post_restart_prompt = (
            f"重启后读取{j}：读取槽位 {slot}。"
            f"只回复 {marker} VALUE <槽位值>，不要输出其他字符。"
        )
        announce_case_start(
            post_restart_case_name,
            f"重启后读取槽位 {slot} 并校验值",
            prompt_text=post_restart_prompt,
            expected=f"{marker} VALUE {token}",
        )
        resp = http_post(
            "/v1/chat/leader/command",
            {
                "message": post_restart_prompt,
                "auto_collab": False,
                "verify_seconds": 20,
                "metadata": {"channel": "selftest", "task_tag": run_id, "case": f"post_restart_{j}"},
            },
        )
        seen, msg = wait_for(rf"{re.escape(marker)}\s+VALUE\s+", timeout=150)
        recalled = extract_slot_value(msg, marker)
        ok = bool(resp.get("accepted")) and seen and recalled == token
        append_case(
            summary_lines,
            case_details,
            post_restart_case_name,
            ok,
            (
                f"dispatch_ok={bool(resp.get('accepted'))};seen={seen};"
                f"expected={token};recalled={recalled or '<empty>'}"
            ),
            {"last_message": msg, "expected_token": token, "recalled_token": recalled},
        )

    # CASE34-CASE36: multimodal 3 images
    if ONLINE_MODE:
        images = [
            Path(REMOTE_ROOT) / "resource" / "image" / "test1.jpg",
            Path(REMOTE_ROOT) / "resource" / "image" / "test2.png",
            Path(REMOTE_ROOT) / "resource" / "image" / "url_case_20260307.jpeg",
        ]
    else:
        images = [
            ROOT / "resource" / "image" / "test1.jpg",
            ROOT / "resource" / "image" / "test2.png",
            ROOT / "resource" / "image" / "url_case_20260307.jpeg",
        ]
    for k, img in enumerate(images, start=1):
        marker = f"MM{k}-{ts()[-4:]}"
        mm_case_name = f"CASE{33+k:02d}_multimodal_{img.name}"
        mm_prompt = (
            f"多模态稳定性{k}：请调用可用视觉链路分析图片 {img}，"
            f"只回复 {marker} OK <20字客观描述>。不要做真人身份识别。"
        )
        announce_case_start(
            mm_case_name,
            f"多模态分析图片 {img.name}",
            prompt_text=mm_prompt,
            expected=f"{marker} OK <客观描述>",
        )
        resp = http_post(
            "/v1/chat/leader/command",
            {
                "message": mm_prompt,
                "auto_collab": False,
                "verify_seconds": 25,
                "metadata": {"channel": "selftest", "task_tag": run_id, "case": f"mm_{k}"},
            },
        )
        seen, msg = wait_for(rf"{re.escape(marker)}\s+OK", timeout=210)
        retried = False
        if not seen and bool(resp.get("accepted")):
            retried = True
            retry_marker = f"{marker}R"
            retry_resp = http_post(
                "/v1/chat/leader/command",
                {
                    "message": (
                        f"多模态重试{k}：请分析图片 {img}。"
                        f"只回复 {retry_marker} OK <20字客观描述>。"
                    ),
                    "auto_collab": False,
                    "verify_seconds": 25,
                    "metadata": {"channel": "selftest", "task_tag": run_id, "case": f"mm_{k}_retry"},
                },
            )
            seen, msg = wait_for(rf"{re.escape(retry_marker)}\s+OK", timeout=210)
            if bool(retry_resp.get("accepted")):
                resp = retry_resp
        ok = bool(resp.get("accepted")) and seen
        append_case(
            summary_lines,
            case_details,
            mm_case_name,
            ok,
            f"dispatch_ok={bool(resp.get('accepted'))};seen={seen};retried={retried}",
            {"last_message": msg},
        )

    # Bridge guard summary
    if ONLINE_MODE:
        cp_bridge = run_remote_cmd("journalctl -u feiqiao-guard-dingtalk-stream -n 200 --no-pager || true")
        bridge_tail = f"{cp_bridge.stdout}\n{cp_bridge.stderr}"[-18000:]
    else:
        bridge_log = ROOT / ".runtime" / "local_bridge" / "bridge.log"
        bridge_tail = bridge_log.read_text(encoding="utf-8", errors="ignore")[-18000:] if bridge_log.exists() else ""
    sid_none_recent = "sid=None" not in bridge_tail
    drift_recent = "identity_id=office-ops-expert" not in bridge_tail
    append_case(
        summary_lines,
        case_details,
        "CASE_EXTRA_bridge_sanity",
        sid_none_recent and drift_recent,
        f"sid_none_recent={sid_none_recent};drift_recent={drift_recent}",
    )

    # overall
    only_cases = [c for c in case_details if c["name"].startswith("CASE")]
    overall_ok = all(bool(c["ok"]) for c in only_cases)
    overall = "PASS" if overall_ok else "FAIL"
    summary_lines.append(f"OVERALL\t{1 if overall_ok else 0}\t{overall}")

    report = {
        "run_id": run_id,
        "base_url": BASE_URL,
        "online_mode": ONLINE_MODE,
        "remote_host": REMOTE_HOST if ONLINE_MODE else "",
        "expected_identity": EXPECTED_ID,
        "sid": sid,
        "codex_home": codex_home,
        "memory_tokens": memory_tokens,
        "cases_total": len([c for c in case_details if c["name"].startswith("CASE")]),
        "cases": case_details,
        "overall": overall,
    }

    summary_path = out_dir / "summary.tsv"
    report_path = out_dir / "report.json"
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(str(summary_path))
    print(str(report_path))
    print(overall)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
