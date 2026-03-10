#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
import types
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "run_dingtalk_stream_bridge.py"


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location("run_dingtalk_stream_bridge", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _install_fake_dingtalk_module() -> tuple[types.ModuleType, type]:
    fake = types.ModuleType("dingtalk_stream")
    reply_bucket: list[str] = []

    class _AckMessage:
        STATUS_OK = "OK"

    class _FakeText:
        def __init__(self, content: str) -> None:
            self.content = content

    class _ChatbotMessage:
        TOPIC = "fake.topic.chatbot"

        def __init__(self, data: dict[str, Any]) -> None:
            self.message_type = str(data.get("msgtype", "text") or "text")
            self.message_id = str(data.get("msgId", "") or "")
            self.sender_staff_id = str(data.get("senderStaffId", "") or "")
            self.conversation_id = str(data.get("conversationId", "") or "")
            self.robot_code = str(data.get("robotCode", "ding-sim") or "ding-sim")
            text = data.get("text")
            if isinstance(text, dict):
                content = str(text.get("content", "") or "")
            else:
                content = ""
            self.text = _FakeText(content)
            self._image_codes: list[str] = []
            self._replies: list[str] = []

        @classmethod
        def from_dict(cls, data: dict[str, Any]):
            return cls(data)

        def get_image_list(self) -> list[str]:
            return list(self._image_codes)

    class _ChatbotHandler:
        def reply_text(self, text: str, incoming: _ChatbotMessage) -> dict[str, Any]:
            incoming._replies.append(str(text))
            reply_bucket.append(str(text))
            return {"errcode": 0, "errmsg": "ok"}

    class _Credential:
        def __init__(self, client_id: str, client_secret: str) -> None:
            self.client_id = client_id
            self.client_secret = client_secret

    class _DingTalkStreamClient:
        last_replies: list[str] = []
        last_ack: str = ""

        def __init__(self, credential: _Credential) -> None:
            self.credential = credential
            self._handler = None

        def register_callback_handler(self, topic: str, handler: Any) -> None:
            self._handler = handler

        def start_forever(self) -> None:
            if self._handler is None:
                raise RuntimeError("handler_not_registered")
            reply_bucket.clear()
            callback_payload = {
                "msgId": f"sim-{_ts()}",
                "conversationId": "cid-sim-001",
                "senderStaffId": "user-sim-001",
                "conversationType": "2",
                "isInAtList": True,
                "msgtype": "text",
                "text": {"content": "请模拟完整链路并返回最终结果"},
                "robotCode": "ding-sim",
            }
            incoming = _ChatbotMessage.from_dict(callback_payload)
            callback = types.SimpleNamespace(data=callback_payload)
            ack_status, ack_body = asyncio.run(self._handler.process(callback))
            self.__class__.last_ack = f"{ack_status}:{ack_body}"
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if any("最终回复" in item for item in reply_bucket):
                    break
                time.sleep(0.1)
            self.__class__.last_replies = list(reply_bucket)

    fake.AckMessage = _AckMessage
    fake.ChatbotHandler = _ChatbotHandler
    fake.ChatbotMessage = _ChatbotMessage
    fake.Credential = _Credential
    fake.DingTalkStreamClient = _DingTalkStreamClient
    fake.chatbot = types.SimpleNamespace(ChatbotMessage=_ChatbotMessage)
    sys.modules["dingtalk_stream"] = fake
    return fake, _DingTalkStreamClient


class _StubLeaderCommandClient:
    def __init__(self, *, base_url: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self._snapshot_calls = 0

    def send_leader_command(
        self,
        *,
        message: str,
        auto_collab: bool = True,
        metadata: dict[str, Any] | None = None,
        verify_seconds: float | None = None,
        collab_verify_seconds: float | None = None,
    ) -> dict[str, Any]:
        return {
            "accepted": True,
            "auto_collab": bool(auto_collab),
            "leader_identity_id": "feiqiao-guard-delivery-lead",
            "leader_result": {
                "accepted": True,
                "delivery_state": "confirmed",
                "identity_id": "feiqiao-guard-delivery-lead",
                "session_id": "sid-sim-001",
                "codex_home": "/tmp/codex_home_sim",
                "verify_seconds": verify_seconds if verify_seconds is not None else 6.0,
            },
            "collab_result": None,
            "orchestration_notes": [],
            "echo": {
                "message_preview": str(message)[:160],
                "metadata_keys": sorted((metadata or {}).keys()),
            },
        }

    def get_identity_routes(self) -> dict[str, Any]:
        return {
            "total": 1,
            "items": [
                {
                    "identity_id": "feiqiao-guard-delivery-lead",
                    "route_status": "ok",
                    "session_id": "sid-sim-001",
                }
            ],
        }

    def get_approval_queue(self, *, status_filter: str = "PENDING", limit: int = 10) -> dict[str, Any]:
        return {
            "status_filter": status_filter,
            "total": 0,
            "items": [],
        }

    def apply_trusted_approval_decision(
        self,
        *,
        request_id: str,
        action: str,
        approver: str,
        bridge_token: str,
        source_ip: str = "",
    ) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "status": "APPROVED" if action == "approve" else "REJECTED",
            "terminal_action": "ENTER" if action == "approve" else "ESC",
            "reason": f"{action}_by_trusted_bridge",
        }

    def get_leader_snapshot(self) -> dict[str, Any]:
        self._snapshot_calls += 1
        if self._snapshot_calls == 1:
            state = "RUNNING"
            message = "处理中(阶段0)"
            summary = "event_msg:user_message"
        elif self._snapshot_calls == 2:
            state = "RUNNING"
            message = "处理中(阶段1)"
            summary = "event_msg:progress"
        elif self._snapshot_calls == 3:
            state = "RUNNING"
            message = "处理中(阶段2)"
            summary = "event_msg:progress"
        else:
            state = "DONE_WAITING_INPUT"
            message = "SIM_FINAL_ANSWER=bridge_inbound_ok"
            summary = "task_complete"
        return {
            "items": [
                {
                    "identity_id": "feiqiao-guard-delivery-lead",
                    "state": state,
                    "last_agent_message": message,
                    "last_event_summary": summary,
                }
            ]
        }


def main() -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    run_id = f"simulated_inbound_e2e_{_ts()}"
    out_dir = ROOT / "artifacts" / "ops" / day / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    _fake_module, fake_client_cls = _install_fake_dingtalk_module()
    bridge = _load_bridge_module()
    bridge.LeaderCommandClient = _StubLeaderCommandClient  # type: ignore[attr-defined]

    with TemporaryDirectory(prefix="fqg-sim-bridge-") as tmp_dir:
        dedupe_file = Path(tmp_dir) / "dedupe.json"
        bridge._define_args = lambda: argparse.Namespace(  # type: ignore[attr-defined]
            client_id="ding-sim-client",
            client_secret="ding-sim-secret",
            base_url="http://127.0.0.1:3001",
            timeout_seconds=10,
            allow_user_ids="",
            allow_chat_ids="",
            trusted_decision_token="sim-bridge-token",
            bridge_approver="guixianren-bridge",
            command_prefixes="/run,/cmd",
            require_prefix=False,
            no_require_at=True,
            auto_collab=False,
            verify_seconds=4.0,
            collab_verify_seconds=4.0,
            dedupe_file=str(dedupe_file),
            dedupe_ttl_seconds=3600,
            dedupe_max_items=500,
            log_level="INFO",
            followup_seconds=0.2,
            progress_push_count=1,
            completion_wait_seconds=0.6,
            completion_poll_seconds=0.1,
            completion_max_wait_seconds=4.0,
            auto_continue_on_waiting_input=True,
            auto_continue_waiting_input_max_attempts=1,
            auto_continue_waiting_input_min_seconds=0.3,
            force_final_on_settled=True,
            force_final_max_attempts=1,
            reply_retry_attempts=2,
            reply_retry_base_delay_seconds=0.1,
            reply_retry_max_delay_seconds=0.2,
            enable_identity_refusal_fallback=False,
            activity_idle_restart_seconds=0.0,
            force_restart_max_uptime_seconds=0.0,
            watchdog_check_interval_seconds=1.0,
            watchdog_grace_seconds=999.0,
            heartbeat_file=str(Path(tmp_dir) / "bridge_heartbeat.json"),
            heartbeat_write_interval_seconds=1.0,
        )
        rc = int(bridge.main())

    replies = list(getattr(fake_client_cls, "last_replies", []))
    ack = str(getattr(fake_client_cls, "last_ack", ""))
    accepted_seen = any("已受理，正在调度" in item for item in replies)
    summary_seen = any(("执行已受理" in item) or ("leader: confirmed" in item) for item in replies)
    final_seen = any(
        ("SIM_FINAL_ANSWER=bridge_inbound_ok" in item)
        and (
            ("最终回复" in item)
            or ("执行进展" in item)
            or ("后台进展" in item)
            or ("会话已回到待输入状态" in item)
        )
        for item in replies
    )
    progress_seen = any("progress=" in item for item in replies)

    result = {
        "run_id": run_id,
        "return_code": rc,
        "ack": ack,
        "checks": {
            "accepted_seen": accepted_seen,
            "summary_seen": summary_seen,
            "progress_seen": progress_seen,
            "final_seen": final_seen,
        },
        "replies": replies,
        "overall": "PASS" if (rc == 0 and accepted_seen and summary_seen and final_seen) else "FAIL",
    }
    report = out_dir / "report.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(report))
    print(result["overall"])
    return 0 if result["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
