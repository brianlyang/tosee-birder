from __future__ import annotations

from feiqiao_guard.chat_bridge import (
    format_approval_queue_summary,
    parse_approval_command,
)


def test_parse_approval_command_list() -> None:
    cmd = parse_approval_command("待审批")
    assert cmd is not None
    assert cmd.action == "list"
    assert cmd.request_id is None


def test_parse_approval_command_approve_with_request_id() -> None:
    request_id = "123e4567-e89b-12d3-a456-426614174000"
    cmd = parse_approval_command(f"同意 {request_id}")
    assert cmd is not None
    assert cmd.action == "approve"
    assert cmd.request_id == request_id


def test_parse_approval_command_reject_without_request_id() -> None:
    cmd = parse_approval_command("拒绝")
    assert cmd is not None
    assert cmd.action == "reject"
    assert cmd.request_id is None


def test_format_approval_queue_summary_includes_hint() -> None:
    text = format_approval_queue_summary(
        {
            "items": [
                {
                    "request_id": "123e4567-e89b-12d3-a456-426614174000",
                    "risk_level": "L3",
                    "remaining_approvals": 1,
                    "reason": "awaiting_second_approval_high_risk",
                    "command_preview": "python3 manage.py migrate",
                }
            ]
        }
    )
    assert "当前待审批队列" in text
    assert "123e4567-e89b-12d3-a456-426614174000" in text
    assert "审批指令：同意 <request_id> 或 拒绝 <request_id>" in text
