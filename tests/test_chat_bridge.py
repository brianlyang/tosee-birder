from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from feiqiao_guard.chat_bridge import (
    BridgePolicy,
    InboundChatMessage,
    LeaderCommandClient,
    LeaderCommandTimeout,
    MessageDedupeStore,
    format_dispatch_summary,
    format_leader_snapshot_summary,
    format_routes_summary,
    is_route_query_command,
)


def test_policy_requires_at_in_group() -> None:
    policy = BridgePolicy(require_at_on_group=True)
    decision = policy.evaluate(
        InboundChatMessage(
            msg_id="m1",
            text="继续执行",
            sender_id="u1",
            chat_id="c1",
            is_group=True,
            is_at_bot=False,
        )
    )
    assert decision.accepted is False
    assert decision.reason == "at_required"


def test_policy_allows_single_chat_without_at() -> None:
    policy = BridgePolicy(require_at_on_group=True)
    decision = policy.evaluate(
        InboundChatMessage(
            msg_id="m2",
            text="继续执行",
            sender_id="u1",
            chat_id="c1",
            is_group=False,
            is_at_bot=False,
        )
    )
    assert decision.accepted is True
    assert decision.normalized_message == "继续执行"


def test_policy_prefix_normalization() -> None:
    policy = BridgePolicy(command_prefixes=("/run", "/cmd"))
    decision = policy.evaluate(
        InboundChatMessage(
            msg_id="m3",
            text="/run  继续执行",
            sender_id="u1",
            chat_id="c1",
            is_group=True,
            is_at_bot=True,
        )
    )
    assert decision.accepted is True
    assert decision.normalized_message == "继续执行"


def test_policy_strips_mentions_for_command() -> None:
    policy = BridgePolicy()
    decision = policy.evaluate(
        InboundChatMessage(
            msg_id="m3-mention",
            text="@龟仙人   发状态",
            sender_id="u1",
            chat_id="c1",
            is_group=True,
            is_at_bot=True,
        )
    )
    assert decision.accepted is True
    assert decision.normalized_message == "发状态"


def test_policy_rejects_without_required_prefix() -> None:
    policy = BridgePolicy(require_command_prefix=True, command_prefixes=("/run",))
    decision = policy.evaluate(
        InboundChatMessage(
            msg_id="m4",
            text="继续执行",
            sender_id="u1",
            chat_id="c1",
            is_group=False,
            is_at_bot=False,
        )
    )
    assert decision.accepted is False
    assert decision.reason == "command_prefix_required"


def test_policy_allowlist_rejects_sender() -> None:
    policy = BridgePolicy(allow_user_ids={"allowed-user"})
    decision = policy.evaluate(
        InboundChatMessage(
            msg_id="m5",
            text="继续执行",
            sender_id="blocked-user",
            chat_id="c1",
            is_group=False,
            is_at_bot=False,
        )
    )
    assert decision.accepted is False
    assert decision.reason == "sender_not_allowed"


def test_dedupe_store_blocks_second_dispatch(tmp_path: Path) -> None:
    store = MessageDedupeStore(tmp_path / "dedupe.json", ttl_seconds=3600, max_items=200)
    assert store.check_and_mark("msg-001", now=1000) is True
    assert store.check_and_mark("msg-001", now=1001) is False


def test_dedupe_store_prunes_expired_items(tmp_path: Path) -> None:
    store = MessageDedupeStore(tmp_path / "dedupe.json", ttl_seconds=10, max_items=200)
    assert store.check_and_mark("old-msg", now=1000) is True
    assert store.check_and_mark("new-msg", now=1011) is True
    assert store.check_and_mark("old-msg", now=1012) is True


def test_format_dispatch_summary_contains_session_and_state() -> None:
    summary = format_dispatch_summary(
        {
            "accepted": True,
            "leader_result": {"delivery_state": "confirmed", "session_id": "sid-lead"},
            "collab_result": {"delivery_state": "queued", "session_id": "sid-collab"},
            "orchestration_notes": ["collab_delivery_state=queued"],
        }
    )
    assert "执行已受理" in summary
    assert "leader: confirmed | sid=sid-lead" in summary
    assert "collab: queued | sid=sid-collab" in summary
    assert "collab_delivery_state=queued" in summary


def test_route_query_command_detection() -> None:
    assert is_route_query_command("sid") is True
    assert is_route_query_command("会话id") is True
    assert is_route_query_command("/status") is True
    assert is_route_query_command("状态") is True
    assert is_route_query_command("进度") is True
    assert is_route_query_command("发状态") is True
    assert is_route_query_command("查状态") is True
    assert is_route_query_command("@龟仙人 发状态") is True
    assert is_route_query_command("<at>@龟仙人</at> 发状态") is True
    assert is_route_query_command("请帮我看一下状态") is True
    assert is_route_query_command("发状态并继续执行") is False
    assert is_route_query_command("继续执行") is False


def test_format_routes_summary_basic() -> None:
    summary = format_routes_summary(
        {
            "items": [
                {
                    "identity_id": "feiqiao-guard-delivery-lead",
                    "session_id": "sid-lead",
                    "session_name_prefix": "fqg-lead",
                    "enabled": True,
                },
                {
                    "identity_id": "feiqiao-guard-collab-executor",
                    "session_id": "sid-collab",
                    "session_name_prefix": "fqg-collab",
                    "enabled": False,
                },
            ]
        }
    )
    assert "当前路由状态" in summary
    assert "feiqiao-guard-delivery-lead: sid=sid-lead | prefix=fqg-lead | enabled=on" in summary
    assert "feiqiao-guard-collab-executor: sid=sid-collab | prefix=fqg-collab | enabled=off" in summary


def test_format_routes_summary_includes_route_error() -> None:
    summary = format_routes_summary(
        {
            "items": [
                {
                    "identity_id": "feiqiao-guard-delivery-lead",
                    "session_id": "sid-lead",
                    "session_name_prefix": "fqg-lead",
                    "enabled": True,
                    "route_status": "error",
                    "route_error": "session_id_conflict_requires_switch_ack:sid-lead:lead,collab",
                }
            ]
        }
    )
    assert "route=error" in summary
    assert "route_error: session_id_conflict_requires_switch_ack:sid-lead:lead,collab" in summary


def test_format_leader_snapshot_summary_basic() -> None:
    summary = format_leader_snapshot_summary(
        {
            "items": [
                {
                    "identity_id": "feiqiao-guard-delivery-lead",
                    "session_id": "sid-lead",
                    "state": "WAITING_INPUT",
                    "idle_seconds": 12,
                    "last_event_summary": "agent_message: done",
                },
                {
                    "identity_id": "feiqiao-guard-collab-executor",
                    "session_id": "sid-collab",
                    "state": "RUNNING",
                    "idle_seconds": 2,
                    "last_event_summary": "function_call:exec_command",
                },
            ]
        }
    )
    assert "当前会话状态" in summary
    assert "feiqiao-guard-delivery-lead: sid=sid-lead | state=WAITING_INPUT | idle=12s" in summary
    assert "feiqiao-guard-collab-executor: sid=sid-collab | state=RUNNING | idle=2s" in summary


def test_format_leader_snapshot_summary_includes_latest_reply() -> None:
    summary = format_leader_snapshot_summary(
        {
            "items": [
                {
                    "identity_id": "feiqiao-guard-delivery-lead",
                    "session_id": "sid-lead",
                    "state": "WAITING_INPUT",
                    "idle_seconds": 5,
                    "last_event_summary": "agent_message: done",
                    "last_agent_message": "已完成 timeout 修复，等待你下一条指令。",
                }
            ]
        }
    )
    assert "reply=已完成 timeout 修复，等待你下一条指令。" in summary


def test_leader_command_client_timeout_error_maps_to_domain_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_timeout(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _raise_timeout)
    client = LeaderCommandClient(base_url="http://127.0.0.1:3001", timeout_seconds=7)
    with pytest.raises(LeaderCommandTimeout):
        client.send_leader_command(message="继续执行")


def test_leader_command_client_urlerror_timeout_maps_to_domain_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_urlerror(*_args, **_kwargs):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _raise_urlerror)
    client = LeaderCommandClient(base_url="http://127.0.0.1:3001", timeout_seconds=7)
    with pytest.raises(LeaderCommandTimeout):
        client.send_leader_command(message="继续执行")


def test_leader_command_client_falls_back_to_legacy_when_task_api_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        def __init__(self, body: dict) -> None:
            self.status = 200
            self._raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

        def read(self) -> bytes:
            return self._raw

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            return False

    def _urlopen(req, timeout):  # noqa: ANN001
        url = str(req.full_url)
        if url.endswith("/v1/chat/leader/tasks"):
            raise urllib.error.HTTPError(
                url=url,
                code=404,
                msg="not found",
                hdrs=None,
                fp=io.BytesIO(b'{"detail":"not_found"}'),
            )
        if url.endswith("/v1/chat/leader/command"):
            return _Resp(
                {
                    "accepted": True,
                    "auto_collab": False,
                    "leader_identity_id": "feiqiao-guard-delivery-lead",
                    "leader_result": {
                        "accepted": True,
                        "delivery_state": "confirmed",
                        "identity_id": "feiqiao-guard-delivery-lead",
                        "route_source": "identity_route",
                        "session_id": "sid-1",
                        "codex_home": "/tmp/home",
                        "session_name_prefix": "fqg-lead",
                        "verify_seconds": 8.0,
                        "control_exit_code": 0,
                        "control_result": {"ok": True},
                    },
                    "collab_result": None,
                    "collab_error": None,
                    "orchestration_notes": [],
                }
            )
        raise AssertionError(f"unexpected_url={url}")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    client = LeaderCommandClient(base_url="http://127.0.0.1:3001", timeout_seconds=7)
    response = client.send_leader_command(message="继续执行", auto_collab=False)
    assert response["accepted"] is True
    notes = response.get("orchestration_notes") or []
    assert any(str(note).startswith("task_api_fallback:") for note in notes)
