#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("FQG_VALIDATE_BASE_URL", "http://8.140.215.219:3001").strip()
LEADER_ID = os.getenv("FQG_CHAT_LEADER_IDENTITY_ID", "feiqiao-guard-delivery-lead").strip()
ONLINE_MODE = os.getenv("FQG_SUITE_ONLINE_MODE", "1").strip().lower() in {"1", "true", "yes", "on"}

REMOTE_HOST = os.getenv("FQG_REMOTE_HOST", "8.140.215.219").strip()
REMOTE_USER = os.getenv("FQG_REMOTE_USER", "root").strip()
REMOTE_ROOT = os.getenv("FQG_REMOTE_ROOT", "/root/feiqiao-guard").strip()
REMOTE_SSH_PASSWORD = os.getenv("FQG_REMOTE_SSH_PASSWORD", "").strip()
REMOTE_SSH_CONNECT_TIMEOUT = os.getenv("FQG_REMOTE_SSH_CONNECT_TIMEOUT", "8").strip()

DINGTALK_BROADCAST = os.getenv("FQG_SUITE_DINGTALK_BROADCAST", "1").strip().lower() in {"1", "true", "yes", "on"}
DINGTALK_CLIENT_ID = os.getenv("FQG_DINGTALK_STREAM_CLIENT_ID", "").strip()
DINGTALK_CLIENT_SECRET = os.getenv("FQG_DINGTALK_STREAM_CLIENT_SECRET", "").strip()
DINGTALK_CHAT_ID = os.getenv("FQG_DINGTALK_CHAT_ID", "").strip()
DINGTALK_ROBOT_CODE = os.getenv("FQG_DINGTALK_ROBOT_CODE", DINGTALK_CLIENT_ID).strip()
OPENCLAW_CASE_FILTER_RAW = os.getenv("FQG_OPENCLAW_CASES", "").strip()
OPENCLAW_CASE_FILTER = {
    item.strip().upper()
    for item in OPENCLAW_CASE_FILTER_RAW.split(",")
    if item.strip()
}
CASE_TIMEOUT_SECONDS = float(os.getenv("FQG_OPENCLAW_CASE_TIMEOUT_SECONDS", "260").strip() or "260")
POLL_INTERVAL_SECONDS = float(os.getenv("FQG_OPENCLAW_POLL_INTERVAL_SECONDS", "1.6").strip() or "1.6")
TMUX_TAIL_LINES = int(os.getenv("FQG_OPENCLAW_TMUX_TAIL_LINES", "1200").strip() or "1200")

REMOTE_TMUX_SOCKET = ""
REMOTE_TMUX_SESSION = ""
_TOKEN_CACHE: dict[str, Any] = {"token": "", "fetched_at": 0.0}

OPENCLAW_SOURCE_BOOK: dict[str, str] = {
    "showcase_work": "https://open-claw.bot/docs/start/showcase#1-level-up-your-dev-workflow",
    "showcase_daily": "https://open-claw.bot/docs/start/showcase#2-automate-your-daily-tasks",
    "showcase_examples": "https://open-claw.bot/docs/start/showcase#real-world-examples",
    "showcase_memory": "https://open-claw.bot/docs/start/showcase#1-set-up-knowledge--memory",
    "showcase_hardware": "https://open-claw.bot/docs/start/showcase#3-hardware-and-health",
    "subagents": "https://docs.openclaw.ai/tools/subagents",
    "multi_agent": "https://docs.openclaw.ai/concepts/multi-agent",
    "browser": "https://docs.openclaw.ai/tools/browser",
    "browser_login": "https://docs.openclaw.ai/tools/browser-login",
    "cron_jobs": "https://docs.openclaw.ai/automation/cron-jobs",
    "cron_vs_heartbeat": "https://docs.openclaw.ai/automation/cron-vs-heartbeat",
    "webhook": "https://docs.openclaw.ai/automation/webhook",
    "gmail_pubsub": "https://docs.openclaw.ai/automation/gmail-pubsub",
    "media_understanding": "https://docs.openclaw.ai/nodes/media-understanding",
    "images": "https://docs.openclaw.ai/nodes/images",
}


def ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def compact_text(raw: str, max_len: int = 340) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    if len(text) <= max_len:
        return text
    return f"{text[:max_len-3]}..."


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
    token = str(_TOKEN_CACHE.get("token", "")).strip()
    fetched_at = float(_TOKEN_CACHE.get("fetched_at", 0.0) or 0.0)
    if token and now - fetched_at < 6000:
        return token
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
        return True, body[:280]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}:{exc}"


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


def _leader_last_message_snapshot() -> str:
    snap = http_get("/v1/chat/leader/snapshot")
    for item in snap.get("items", []):
        if isinstance(item, dict) and str(item.get("identity_id", "")).strip() == LEADER_ID:
            raw = item.get("last_agent_message")
            if raw is None:
                return ""
            return str(raw).strip()
    return ""


def wait_for(pattern: str, timeout: float = 180.0) -> tuple[bool, str]:
    rgx = re.compile(pattern, re.S)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        text = ""
        # Prefer API snapshot because tmux pane can contain unrelated queued context.
        try:
            text = _leader_last_message_snapshot()
        except Exception:  # noqa: BLE001
            text = ""

        if ONLINE_MODE:
            if (not text) and REMOTE_TMUX_SOCKET and REMOTE_TMUX_SESSION:
                cp = run_remote_cmd(
                    (
                        f"tmux -S {shlex.quote(REMOTE_TMUX_SOCKET)} capture-pane -pt "
                        f"{shlex.quote(REMOTE_TMUX_SESSION)} | tail -n {max(120, TMUX_TAIL_LINES)}"
                    )
                )
                text = cp.stdout if cp.returncode == 0 else f"{cp.stdout}\n{cp.stderr}"
        if text:
            last = text
        if rgx.search(text):
            return True, text
        time.sleep(max(0.6, POLL_INTERVAL_SECONDS))
    return False, last


def announce_start(
    case_id: str,
    title: str,
    prompt_text: str,
    expected_text: str,
    category: str = "",
    scenario_source: str = "",
) -> None:
    print(f"{case_id}: START | {title}", flush=True)
    msg = "\n".join(
        [
            "[OpenClaw-36 START]",
            f"案例: {case_id}",
            f"主题: {title}",
            f"类别: {category or '未分类'}",
            f"来源: {scenario_source or '未标注'}",
            f"自然语义指令: {compact_text(prompt_text)}",
            f"期望: {compact_text(expected_text)}",
        ]
    )
    ok, info = dingtalk_send_text(msg)
    if not ok:
        print(f"{case_id}: START_BROADCAST_FAIL | {info}", flush=True)


def announce_result(case_id: str, ok: bool, detail: str, last_message: str = "") -> None:
    print(f"{case_id}: {'PASS' if ok else 'FAIL'} | {detail}", flush=True)
    lines = [
        "[OpenClaw-36 RESULT]",
        f"案例: {case_id}",
        f"状态: {'PASS' if ok else 'FAIL'}",
        f"结果摘要: {compact_text(detail)}",
    ]
    if last_message:
        lines.append(f"实际回复摘要: {compact_text(last_message, max_len=420)}")
    b_ok, b_info = dingtalk_send_text("\n".join(lines))
    if not b_ok:
        print(f"{case_id}: RESULT_BROADCAST_FAIL | {b_info}", flush=True)


def run_prompt_case(
    *,
    case_id: str,
    title: str,
    prompt_text: str,
    expect_regex: str,
    expect_human: str,
    timeout: float = CASE_TIMEOUT_SECONDS,
    metadata_case: str,
    category: str = "",
    scenario_source: str = "",
) -> dict[str, Any]:
    announce_start(case_id, title, prompt_text, expect_human, category=category, scenario_source=scenario_source)
    resp = http_post(
        "/v1/chat/leader/command",
        {
            "message": prompt_text,
            "auto_collab": False,
            "verify_seconds": 25,
            "metadata": {
                "channel": "openclaw36",
                "case": metadata_case,
                "run_ts": ts(),
                "category": category,
                "scenario_source": scenario_source,
            },
        },
    )
    accepted = bool(resp.get("accepted"))
    seen, last = wait_for(expect_regex, timeout=timeout)
    ok = accepted and seen
    detail = f"accepted={accepted};matched={seen};pattern={expect_regex}"
    announce_result(case_id, ok, detail, last_message=last)
    return {
        "name": case_id,
        "ok": ok,
        "detail": detail,
        "last_message": last,
        "title": title,
        "category": category,
        "scenario_source": scenario_source,
        "prompt": prompt_text,
    }


def run_restart_case(case_id: str, cycle: int) -> dict[str, Any]:
    prompt = "重启线上 api 与 stream 服务，并确认健康检查正常。"
    expected = "systemctl restart 成功，healthz=ok"
    announce_start(case_id, f"重启稳定性循环 {cycle}", prompt, expected)
    cp = run_remote_cmd(
        "systemctl restart feiqiao-guard-api feiqiao-guard-dingtalk-stream "
        "&& sleep 2 "
        "&& curl -fsS http://127.0.0.1:3001/healthz >/dev/null"
    )
    ok = cp.returncode == 0
    detail = f"restart_rc={cp.returncode}"
    last = f"stdout={cp.stdout}\nstderr={cp.stderr}"
    announce_result(case_id, ok, detail, last_message=last)
    return {"name": case_id, "ok": ok, "detail": detail, "last_message": last}


def main() -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    run_id = f"openclaw36_{ts()}"
    out_dir = ROOT / "artifacts" / "ops" / day / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    health = http_get("/healthz")
    routes = http_get("/v1/chat/routes")
    route_item: dict[str, Any] = {}
    for item in routes.get("items", []):
        if isinstance(item, dict) and str(item.get("identity_id", "")).strip() == LEADER_ID:
            route_item = item
            break

    sid = str(route_item.get("session_id", "")).strip()
    codex_home = str(route_item.get("codex_home", "")).strip()
    prefix = str(route_item.get("session_name_prefix", "")).strip() or "fqg-lead"
    if ONLINE_MODE and sid and codex_home:
        global REMOTE_TMUX_SOCKET, REMOTE_TMUX_SESSION
        REMOTE_TMUX_SOCKET, REMOTE_TMUX_SESSION = compute_guard_tmux(codex_home, sid, prefix)

    cases: list[dict[str, Any]] = []
    image_a = Path(REMOTE_ROOT) / "resource" / "image" / "test1.jpg"
    image_b = Path(REMOTE_ROOT) / "resource" / "image" / "test2.png"
    image_c = Path(REMOTE_ROOT) / "resource" / "image" / "url_case_20260307.jpeg"

    # OpenClaw经典场景36例：每条都映射官方/社区来源，方便回溯。
    case_specs: list[dict[str, str]] = [
        {
            "id": "C01",
            "title": "Linear CLI 任务流",
            "category": "生产力与研发",
            "source": OPENCLAW_SOURCE_BOOK["showcase_work"],
            "task": "模拟 Linear CLI 场景：给出“需求->issue->执行->回收”的4步闭环。",
        },
        {
            "id": "C02",
            "title": "CodexMonitor 会话巡检",
            "category": "生产力与研发",
            "source": OPENCLAW_SOURCE_BOOK["showcase_work"],
            "task": "模拟 CodexMonitor 场景：输出会话巡检清单，至少4项。",
        },
        {
            "id": "C03",
            "title": "PR Review 结论回传",
            "category": "生产力与研发",
            "source": OPENCLAW_SOURCE_BOOK["showcase_work"],
            "task": "模拟 PR Review -> Telegram 场景：输出“结论+风险+下一步”三段。",
        },
        {
            "id": "C04",
            "title": "Jira Skill Builder",
            "category": "生产力与研发",
            "source": OPENCLAW_SOURCE_BOOK["showcase_examples"],
            "task": "模拟 Jira Skill Builder：用3步描述从需求到生成技能的过程。",
        },
        {
            "id": "C05",
            "title": "Slack Auto-Support",
            "category": "生产力与研发",
            "source": OPENCLAW_SOURCE_BOOK["showcase_examples"],
            "task": "模拟 Slack 自动支持：输出“触发条件/自动回复/告警升级”三条策略。",
        },
        {
            "id": "C06",
            "title": "Todoist via Telegram",
            "category": "生产力与研发",
            "source": OPENCLAW_SOURCE_BOOK["showcase_examples"],
            "task": "模拟 Telegram 指令建 Todoist：给出自然语言到任务结构的映射模板。",
        },
        {
            "id": "C07",
            "title": "Tesco Shop Autopilot",
            "category": "日常自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_daily"],
            "task": "模拟 Tesco 订购：给出“清单->时段->下单确认”三阶段方案。",
        },
        {
            "id": "C08",
            "title": "ParentPay 坐标点击容错",
            "category": "日常自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_daily"],
            "task": "模拟 ParentPay 无API网页点击：写出2条坐标容错策略和1条失败回退策略。",
        },
        {
            "id": "C09",
            "title": "Vienna Transport 通勤播报",
            "category": "日常自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_daily"],
            "task": "模拟通勤助手：输出“发车+电梯状态+异常提示”的播报模板。",
        },
        {
            "id": "C10",
            "title": "Beeper 多端消息桥接",
            "category": "日常自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_daily"],
            "task": "模拟 iMessage/WhatsApp 合流：给出消息归并与去重规则。",
        },
        {
            "id": "C11",
            "title": "Padel Court Booking",
            "category": "日常自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_examples"],
            "task": "模拟球场抢订：给出“偏好时段、候选场地、锁单策略”的优先级。",
        },
        {
            "id": "C12",
            "title": "Accounting Intake",
            "category": "日常自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_examples"],
            "task": "模拟会计资料收集：输出“邮件抓取->PDF归档->月度打包”流程。",
        },
        {
            "id": "C13",
            "title": "TradingView 无API分析",
            "category": "浏览器自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_examples"],
            "task": "模拟 TradingView 浏览器自动化：写出截图、特征提取、结论三步。",
        },
        {
            "id": "C14",
            "title": "Job Search Agent",
            "category": "浏览器自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_examples"],
            "task": "模拟职位检索代理：给出“JD匹配评分”的3个关键维度。",
        },
        {
            "id": "C15",
            "title": "Couch Potato Dev",
            "category": "浏览器自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_examples"],
            "task": "模拟手机远程开发：输出“内容迁移+发布+DNS切换”的验收清单。",
        },
        {
            "id": "C16",
            "title": "Missing Skill 即时构建",
            "category": "浏览器自动化",
            "source": OPENCLAW_SOURCE_BOOK["showcase_examples"],
            "task": "模拟技能缺失时即时生成：给出最小输入与回归验证步骤。",
        },
        {
            "id": "C17",
            "title": "Browser Login 安全策略",
            "category": "浏览器自动化",
            "source": OPENCLAW_SOURCE_BOOK["browser_login"],
            "task": "模拟 X/Twitter 场景：说明为何必须手工登录，给出2条安全规则。",
        },
        {
            "id": "C18",
            "title": "Managed Browser 隔离验证",
            "category": "浏览器自动化",
            "source": OPENCLAW_SOURCE_BOOK["browser"],
            "task": "模拟 openclaw 独立浏览器：输出“启动、快照、关闭”最小操作序列。",
        },
        {
            "id": "C19",
            "title": "WhatsApp Memory Vault",
            "category": "知识与记忆",
            "source": OPENCLAW_SOURCE_BOOK["showcase_memory"],
            "task": "模拟 WhatsApp 记忆库：给出“语音转写->索引->检索”链路。",
        },
        {
            "id": "C20",
            "title": "Karakeep 语义检索",
            "category": "知识与记忆",
            "source": OPENCLAW_SOURCE_BOOK["showcase_memory"],
            "task": "模拟书签语义检索：输出向量召回+重排+引用返回方案。",
        },
        {
            "id": "C21",
            "title": "Inside-Out 记忆模型",
            "category": "知识与记忆",
            "source": OPENCLAW_SOURCE_BOOK["showcase_memory"],
            "task": "模拟 session->memory->belief 更新：给出3层结构定义。",
        },
        {
            "id": "C22",
            "title": "xuezh 学习教练",
            "category": "知识与记忆",
            "source": OPENCLAW_SOURCE_BOOK["showcase_memory"],
            "task": "模拟中文学习引擎：给出“纠音反馈+复习计划+进度追踪”。",
        },
        {
            "id": "C23",
            "title": "跨日偏好记忆",
            "category": "知识与记忆",
            "source": OPENCLAW_SOURCE_BOOK["showcase_memory"],
            "task": "设定偏好 token=MEM-A1，输出保存格式。只写一个结构化片段。",
        },
        {
            "id": "C24",
            "title": "偏好记忆召回",
            "category": "知识与记忆",
            "source": OPENCLAW_SOURCE_BOOK["showcase_memory"],
            "task": "读取上一条偏好 token，输出“召回值+置信度+来源”。",
        },
        {
            "id": "C25",
            "title": "Subagent 并行分工",
            "category": "多Agent协同",
            "source": OPENCLAW_SOURCE_BOOK["subagents"],
            "task": "模拟主代理派发2个 subagent：给出任务拆分与汇总格式。",
        },
        {
            "id": "C26",
            "title": "Subagent 深度2编排",
            "category": "多Agent协同",
            "source": OPENCLAW_SOURCE_BOOK["subagents"],
            "task": "模拟 orchestrator->worker 链路，写明 depth-1 与 depth-2职责。",
        },
        {
            "id": "C27",
            "title": "多Agent路由隔离",
            "category": "多Agent协同",
            "source": OPENCLAW_SOURCE_BOOK["multi_agent"],
            "task": "模拟多账号路由：输出 channel/account/peer 三层路由优先级。",
        },
        {
            "id": "C28",
            "title": "工具权限最小化",
            "category": "多Agent协同",
            "source": OPENCLAW_SOURCE_BOOK["multi_agent"],
            "task": "模拟 family/work 代理隔离：列出 allow/deny 策略样例。",
        },
        {
            "id": "C29",
            "title": "主从协作结果回传",
            "category": "多Agent协同",
            "source": OPENCLAW_SOURCE_BOOK["subagents"],
            "task": "模拟子任务完成回传主会话：写出标准回执字段。",
        },
        {
            "id": "C30",
            "title": "并发与超时保护",
            "category": "多Agent协同",
            "source": OPENCLAW_SOURCE_BOOK["subagents"],
            "task": "模拟 maxConcurrent + runTimeoutSeconds 策略，给出推荐值和原因。",
        },
        {
            "id": "C31",
            "title": "Cron 精确调度",
            "category": "触发与调度",
            "source": OPENCLAW_SOURCE_BOOK["cron_jobs"],
            "task": "模拟每天7点晨报任务：输出 cron 表达式、时区与交付目标。",
        },
        {
            "id": "C32",
            "title": "Heartbeat 批处理",
            "category": "触发与调度",
            "source": OPENCLAW_SOURCE_BOOK["cron_vs_heartbeat"],
            "task": "模拟 heartbeat 清单：给出 inbox/calendar/project 三项巡检。",
        },
        {
            "id": "C33",
            "title": "Cron vs Heartbeat 选型",
            "category": "触发与调度",
            "source": OPENCLAW_SOURCE_BOOK["cron_vs_heartbeat"],
            "task": "对“20分钟提醒/每周深度分析/30分钟收件箱巡检”分别选型并说明一句。",
        },
        {
            "id": "C34",
            "title": "Webhook 外部触发",
            "category": "触发与调度",
            "source": OPENCLAW_SOURCE_BOOK["webhook"],
            "task": "模拟 /hooks/wake 与 /hooks/agent 输入：写出最小 JSON 字段。",
        },
        {
            "id": "C35",
            "title": "Gmail PubSub 入站",
            "category": "触发与调度",
            "source": OPENCLAW_SOURCE_BOOK["gmail_pubsub"],
            "task": "模拟 Gmail PubSub 到 agent 摘要：列出3步处理流。",
        },
        {
            "id": "C36",
            "title": "多模态媒体理解",
            "category": "触发与调度",
            "source": OPENCLAW_SOURCE_BOOK["media_understanding"],
            "task": (
                f"请同时参考图片A={image_a}、图片B={image_b}、图片C={image_c}。"
                "输出“可见元素/风险提示/回复策略”三行。"
            ),
        },
    ]

    run_nonce = ts()[-6:]
    for spec in case_specs:
        cid = spec["id"]
        if OPENCLAW_CASE_FILTER and cid.upper() not in OPENCLAW_CASE_FILTER:
            continue
        marker = f"{cid}_OK_{run_nonce}"
        prompt = (
            "你正在执行 OpenClaw 经典场景回归测试。"
            f"场景={spec['title']}；类别={spec['category']}；参考={spec['source']}。"
            f"任务：{spec['task']} "
            f"输出格式必须严格遵守：第一行仅输出 {marker}；"
            "第二行开始输出一个 JSON 对象；禁止输出其它引导语。"
        )
        cases.append(
            run_prompt_case(
                case_id=cid,
                title=spec["title"],
                prompt_text=prompt,
                expect_regex=rf"{re.escape(marker)}\s+",
                expect_human=f"第一行={marker}；第二行=JSON",
                timeout=CASE_TIMEOUT_SECONDS,
                metadata_case=cid.lower(),
                category=spec["category"],
                scenario_source=spec["source"],
            )
        )

    overall_ok = all(bool(item.get("ok")) for item in cases)
    report = {
        "run_id": run_id,
        "overall": "PASS" if overall_ok else "FAIL",
        "base_url": BASE_URL,
        "leader_identity": LEADER_ID,
        "online_mode": ONLINE_MODE,
        "remote_host": REMOTE_HOST if ONLINE_MODE else "",
        "cases_total": len(cases),
        "passed": sum(1 for c in cases if c.get("ok")),
        "failed": sum(1 for c in cases if not c.get("ok")),
        "cases": cases,
    }

    report_path = out_dir / "report.json"
    summary_path = out_dir / "summary.tsv"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_lines = ["case\tok\tdetail"]
    for item in cases:
        summary_lines.append(f"{item.get('name')}\t{1 if item.get('ok') else 0}\t{item.get('detail')}")
    summary_lines.append(f"OVERALL\t{1 if overall_ok else 0}\t{report['overall']}")
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    final_msg = (
        f"[OpenClaw-36 SUMMARY]\n"
        f"run_id={run_id}\n"
        f"overall={report['overall']}\n"
        f"passed={report['passed']}/{report['cases_total']}\n"
        f"report={report_path}"
    )
    dingtalk_send_text(final_msg)

    print(str(summary_path))
    print(str(report_path))
    print(report["overall"])
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
