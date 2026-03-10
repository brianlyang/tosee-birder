from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from feiqiao_guard.config import Settings
from feiqiao_guard.dingtalk_client import DingTalkClient
from feiqiao_guard.models import RiskLevel


def _settings(
    tmp_path: Path,
    webhook: str = "https://example.com/webhook",
    paused_tokens: list[str] | None = None,
) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8765,
        timeout_seconds=120,
        auto_expire_poll_interval_seconds=15,
        callback_max_skew_seconds=300,
        callback_base_url="http://127.0.0.1:8765",
        callback_signing_secret="",
        default_approver="approver",
        dingtalk_webhook_url=webhook,
        dingtalk_signing_secret="",
        lark_webhook_url="",
        lark_signing_secret="",
        audit_log_path=tmp_path / "audit.jsonl",
        sqlite_path=tmp_path / "gateway.db",
        approver_allowlist=[],
        bypass_signature_verification=True,
        enable_high_risk_dual_approval=False,
        dingtalk_paused_webhook_tokens=paused_tokens or [],
    )


def test_action_card_title_contains_risk_and_request_tag(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class StubDingTalkClient(DingTalkClient):
        def _post(self, payload: dict) -> bool:
            captured["payload"] = payload
            return True

    client = StubDingTalkClient(_settings(tmp_path))
    request_id = "085b7c0b-8f71-4de0-9905-b10a0b00d76e"
    ok = client.send_approval_card(
        request_id=request_id,
        terminal_session_id="sess-001",
        command="python3 manage.py migrate",
        risk_level=RiskLevel.L3,
        expires_at=datetime.now(timezone.utc),
        approve_url="http://127.0.0.1:8765/v1/decision-link?a=1",
        reject_url="http://127.0.0.1:8765/v1/decision-link?a=2",
        extra_context={},
    )
    assert ok is True
    action_card = captured["payload"]["actionCard"]  # type: ignore[index]
    title = action_card["title"]  # type: ignore[index]
    assert request_id.split("-", 1)[0] in title
    assert "L3" in title


def test_send_approval_card_returns_false_when_webhook_missing(tmp_path: Path) -> None:
    client = DingTalkClient(_settings(tmp_path, webhook=""))
    ok = client.send_approval_card(
        request_id="x-1",
        terminal_session_id="sess-001",
        command="ls -la",
        risk_level=RiskLevel.L1,
        expires_at=datetime.now(timezone.utc),
        approve_url="http://127.0.0.1:8765/v1/decision-link?a=1",
        reject_url="http://127.0.0.1:8765/v1/decision-link?a=2",
        extra_context={},
    )
    assert ok is False


def test_send_approval_card_returns_false_when_webhook_is_paused(tmp_path: Path) -> None:
    token = "paused-token-001"
    client = DingTalkClient(
        _settings(
            tmp_path,
            webhook=f"https://oapi.dingtalk.com/robot/send?access_token={token}",
            paused_tokens=[token],
        )
    )
    ok = client.send_approval_card(
        request_id="x-2",
        terminal_session_id="sess-002",
        command="echo hello",
        risk_level=RiskLevel.L1,
        expires_at=datetime.now(timezone.utc),
        approve_url="http://127.0.0.1:8765/v1/decision-link?a=1",
        reject_url="http://127.0.0.1:8765/v1/decision-link?a=2",
        extra_context={},
    )
    assert ok is False
