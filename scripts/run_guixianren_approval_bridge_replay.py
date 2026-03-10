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
PENDING_REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location("run_dingtalk_stream_bridge", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed_to_load:{SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _install_fake_dingtalk_module(message_texts: list[str]) -> tuple[types.ModuleType, type]:
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
            text_obj = data.get("text")
            content = str((text_obj or {}).get("content", "")).strip() if isinstance(text_obj, dict) else ""
            self.text = _FakeText(content)
            self._image_codes: list[str] = []

        @classmethod
        def from_dict(cls, data: dict[str, Any]):
            return cls(data)

        def get_image_list(self) -> list[str]:
            return list(self._image_codes)

    class _ChatbotHandler:
        def reply_text(self, text: str, incoming: _ChatbotMessage) -> dict[str, Any]:
            reply_bucket.append(str(text))
            return {"errcode": 0, "errmsg": "ok"}

    class _Credential:
        def __init__(self, client_id: str, client_secret: str) -> None:
            self.client_id = client_id
            self.client_secret = client_secret

    class _DingTalkStreamClient:
        last_replies: list[str] = []
        last_acks: list[str] = []

        def __init__(self, credential: _Credential) -> None:
            self.credential = credential
            self._handler = None

        def register_callback_handler(self, topic: str, handler: Any) -> None:
            self._handler = handler

        def start_forever(self) -> None:
            if self._handler is None:
                raise RuntimeError("handler_not_registered")
            self.__class__.last_acks = []
            reply_bucket.clear()

            base = {
                "conversationId": "cid-sim-approval-001",
                "senderStaffId": "user-sim-approval-001",
                "conversationType": "2",
                "isInAtList": True,
                "msgtype": "text",
                "robotCode": "ding-sim",
            }
            for idx, msg_text in enumerate(message_texts, start=1):
                payload = dict(base)
                payload["msgId"] = f"sim-{idx}-{_ts()}"
                payload["text"] = {"content": msg_text}
                callback = types.SimpleNamespace(data=payload)
                ack_status, ack_body = asyncio.run(self._handler.process(callback))
                self.__class__.last_acks.append(f"{ack_status}:{ack_body}")
                # Give background worker enough time to push progress/approval hints.
                time.sleep(1.2)

            # Wait final background replies
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if any("最终回复" in text and "APPROVAL_FLOW_FINAL_OK" in text for text in reply_bucket):
                    break
                time.sleep(0.2)
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
    pending_approved = False
    snapshot_calls = 0

    def __init__(self, *, base_url: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

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
                "session_id": "sid-sim-approval-001",
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
                    "session_id": "sid-sim-approval-001",
                }
            ],
        }

    def get_approval_queue(self, *, status_filter: str = "PENDING", limit: int = 10) -> dict[str, Any]:
        if self.__class__.pending_approved:
            return {
                "status_filter": status_filter,
                "total": 0,
                "items": [],
            }
        return {
            "status_filter": status_filter,
            "total": 1,
            "items": [
                {
                    "request_id": PENDING_REQUEST_ID,
                    "risk_level": "L3",
                    "reason": "awaiting_first_approval_high_risk",
                    "remaining_approvals": 1,
                    "command_preview": "python3 manage.py migrate",
                }
            ],
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
        if str(bridge_token).strip() != "sim-bridge-token":
            raise RuntimeError("trusted_decision_forbidden")
        if request_id != PENDING_REQUEST_ID:
            raise RuntimeError(f"approval_not_found:{request_id}")
        if action not in {"approve", "reject"}:
            raise RuntimeError(f"invalid_action:{action}")
        self.__class__.pending_approved = action == "approve"
        return {
            "request_id": request_id,
            "status": "APPROVED" if action == "approve" else "REJECTED",
            "terminal_action": "ENTER" if action == "approve" else "ESC",
            "reason": f"{action}_by_trusted_bridge",
        }

    def get_leader_snapshot(self) -> dict[str, Any]:
        self.__class__.snapshot_calls += 1
        if not self.__class__.pending_approved:
            state = "RUNNING"
            message = "任务等待审批中"
            summary = "event_msg:awaiting_approval"
        else:
            if self.__class__.snapshot_calls < 3:
                state = "RUNNING"
                message = "审批通过后继续执行中"
                summary = "event_msg:running_after_approval"
            else:
                state = "DONE_WAITING_INPUT"
                message = "APPROVAL_FLOW_FINAL_OK"
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
    run_id = f"guixianren_approval_bridge_replay_{_ts()}"
    out_dir = ROOT / "artifacts" / "ops" / day / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Reset stub state for deterministic replay.
    _StubLeaderCommandClient.pending_approved = False
    _StubLeaderCommandClient.snapshot_calls = 0

    replay_messages = [
        "请执行一个需要高危审批的部署任务，完成后给结果。",
        "待审批",
        f"同意 {PENDING_REQUEST_ID}",
    ]
    _fake_module, fake_client_cls = _install_fake_dingtalk_module(replay_messages)
    bridge = _load_bridge_module()
    bridge.LeaderCommandClient = _StubLeaderCommandClient  # type: ignore[attr-defined]

    with TemporaryDirectory(prefix="fqg-sim-approval-") as tmp_dir:
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
            verify_seconds=2.0,
            collab_verify_seconds=2.0,
            dedupe_file=str(dedupe_file),
            dedupe_ttl_seconds=3600,
            dedupe_max_items=500,
            log_level="INFO",
            followup_seconds=0.3,
            progress_push_count=1,
            completion_wait_seconds=2.0,
            completion_poll_seconds=0.3,
            completion_max_wait_seconds=8.0,
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
    acks = list(getattr(fake_client_cls, "last_acks", []))
    checks = {
        "accepted_seen": any("已受理，正在调度" in text for text in replies),
        "pending_hint_seen": any("检测到执行等待审批" in text for text in replies),
        "queue_seen": any(PENDING_REQUEST_ID in text and "当前待审批队列" in text for text in replies),
        "approval_processed_seen": any("审批已处理: 同意" in text for text in replies),
        "final_seen": any(
            ("APPROVAL_FLOW_FINAL_OK" in text)
            and (
                ("最终回复" in text)
                or ("执行进展" in text)
                or ("后台进展" in text)
                or ("会话已回到待输入状态" in text)
            )
            for text in replies
        ),
    }
    overall = "PASS" if rc == 0 and all(checks.values()) else "FAIL"

    report = {
        "run_id": run_id,
        "return_code": rc,
        "acks": acks,
        "messages_sent": replay_messages,
        "checks": checks,
        "replies": replies,
        "overall": overall,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path = out_dir / "summary.txt"
    summary_path.write_text(
        "\n".join(
            [
                f"run_id={run_id}",
                f"overall={overall}",
                f"return_code={rc}",
                *(f"{k}={int(v)}" for k, v in checks.items()),
                f"report={report_path}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(str(summary_path))
    print(str(report_path))
    print(overall)
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
