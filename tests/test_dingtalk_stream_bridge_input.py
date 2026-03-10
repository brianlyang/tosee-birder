from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

from feiqiao_guard.chat_bridge import InboundChatMessage


def _load_bridge_module() -> ModuleType:
    root = Path(__file__).resolve().parents[1]
    module_path = root / "scripts" / "run_dingtalk_stream_bridge.py"
    spec = importlib.util.spec_from_file_location("run_dingtalk_stream_bridge", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeText:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeIncomingMessage:
    def __init__(
        self,
        *,
        message_type: str,
        text: str = "",
        image_codes: list[str] | None = None,
        robot_code: str = "ding-demo",
    ) -> None:
        self.message_type = message_type
        self.text = _FakeText(text)
        self.robot_code = robot_code
        self._image_codes = image_codes or []

    def get_image_list(self) -> list[str]:
        return list(self._image_codes)


def test_extract_text_from_text_message() -> None:
    bridge = _load_bridge_module()
    data = {"msgtype": "text", "text": {"content": "  你好  "}}
    incoming = _FakeIncomingMessage(message_type="text", text="  你好  ")
    assert bridge._extract_text(data, incoming) == "你好"


def test_build_trace_and_task_ids_uses_msg_tail_and_uuid_nonce(monkeypatch) -> None:
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    trace_id, task_id = bridge._build_trace_and_task_ids("abc1234567890")
    assert trace_id == "1234567890"
    assert task_id == "1234567890-aaaaaaaa"


def test_build_trace_and_task_ids_without_msg_id_falls_back() -> None:
    bridge = _load_bridge_module()
    trace_id, task_id = bridge._build_trace_and_task_ids("")
    assert len(trace_id) >= 6
    assert task_id.startswith(f"{trace_id}-")
    assert len(task_id.split("-")[-1]) == 8


def test_is_stale_chat_reply_requires_mismatch_on_non_empty_ids() -> None:
    bridge = _load_bridge_module()
    assert bridge._is_stale_chat_reply(inbound_msg_id="m1", active_msg_id="m2") is True
    assert bridge._is_stale_chat_reply(inbound_msg_id="m1", active_msg_id="m1") is False
    assert bridge._is_stale_chat_reply(inbound_msg_id="", active_msg_id="m1") is False
    assert bridge._is_stale_chat_reply(inbound_msg_id="m1", active_msg_id="") is False


def test_build_idle_watchdog_snapshot_no_activity_seen_on_fresh_start() -> None:
    bridge = _load_bridge_module()
    snap = bridge._build_idle_watchdog_snapshot(
        {
            "started_at": 100.0,
            "last_callback_at": 100.0,
            "last_inbound_at": 100.0,
            "last_reply_at": 100.0,
            "callback_count": 0,
            "inbound_count": 0,
            "reply_count": 0,
        },
        now_monotonic=800.0,
    )
    assert snap["activity_seen"] is False
    assert snap["idle_seconds"] == 700.0
    assert snap["uptime_seconds"] == 700.0


def test_build_idle_watchdog_snapshot_marks_activity_by_counts_or_timestamps() -> None:
    bridge = _load_bridge_module()
    snap_by_count = bridge._build_idle_watchdog_snapshot(
        {
            "started_at": 10.0,
            "last_callback_at": 10.0,
            "last_inbound_at": 10.0,
            "last_reply_at": 10.0,
            "callback_count": 1,
            "inbound_count": 0,
            "reply_count": 0,
        },
        now_monotonic=15.0,
    )
    assert snap_by_count["activity_seen"] is True

    snap_by_timestamp = bridge._build_idle_watchdog_snapshot(
        {
            "started_at": 10.0,
            "last_callback_at": 10.1,
            "last_inbound_at": 10.0,
            "last_reply_at": 10.0,
            "callback_count": 0,
            "inbound_count": 0,
            "reply_count": 0,
        },
        now_monotonic=15.0,
    )
    assert snap_by_timestamp["activity_seen"] is True


def test_build_real_question_tag_includes_msg_tail_and_hint() -> None:
    bridge = _load_bridge_module()
    tag = bridge._build_real_question_tag(
        msg_id="msgABCDEFG123456",
        normalized_message="再看看test2;问GLM4.6V API 她是谁",
    )
    assert tag.startswith("RQ-")
    assert "-123456-" in tag
    assert tag.endswith("再看看test2")


def test_build_real_question_tag_fallback_hint_when_message_empty() -> None:
    bridge = _load_bridge_module()
    tag = bridge._build_real_question_tag(msg_id="", normalized_message="")
    assert tag.startswith("RQ-")
    assert tag.endswith("query")


def test_reply_result_ok_rejects_none_and_nonzero_errcode() -> None:
    bridge = _load_bridge_module()
    assert bridge._reply_result_ok(None) is False
    assert bridge._reply_result_ok({"errcode": 88, "errmsg": "fail"}) is False
    assert bridge._reply_result_ok({"errcode": "500"}) is False


def test_reply_result_ok_accepts_success_payloads() -> None:
    bridge = _load_bridge_module()
    assert bridge._reply_result_ok({"errcode": 0, "errmsg": "ok"}) is True
    assert bridge._reply_result_ok({"success": True}) is True
    assert bridge._reply_result_ok({"foo": "bar"}) is True


def test_has_image_context_accepts_local_paths() -> None:
    bridge = _load_bridge_module()
    metadata = {"image_local_paths": ["/tmp/a.png"]}
    assert bridge._has_image_context(metadata) is True


def test_build_safe_image_retry_prompt_contains_local_paths() -> None:
    bridge = _load_bridge_module()
    prompt = bridge._build_safe_image_retry_prompt(
        original_message="她是谁",
        metadata={
            "image_download_urls": ["http://example.com/a.png"],
            "image_local_paths": ["/tmp/a.png"],
        },
    )
    assert "本地图片路径" in prompt
    assert "/tmp/a.png" in prompt


def test_infer_attachment_kind_from_name_and_content_type() -> None:
    bridge = _load_bridge_module()
    assert bridge._infer_attachment_kind_from_name(name="x.jpg") == "image"
    assert bridge._infer_attachment_kind_from_name(name="x.mp3") == "audio"
    assert bridge._infer_attachment_kind_from_name(name="x.mp4") == "video"
    assert bridge._infer_attachment_kind_from_name(name="x.bin") == "file"
    assert bridge._infer_attachment_kind_from_name(name="noext", content_type="audio/mpeg") == "audio"


def test_coerce_attachment_kind_for_dingtalk_svg_uses_file_channel() -> None:
    bridge = _load_bridge_module()
    assert (
        bridge._coerce_attachment_kind_for_dingtalk(kind="image", path_value="/tmp/demo.svg")
        == "file"
    )
    assert (
        bridge._coerce_attachment_kind_for_dingtalk(
            kind="image",
            url_value="https://example.com/path/demo.svg",
        )
        == "file"
    )
    assert (
        bridge._coerce_attachment_kind_for_dingtalk(kind="image", path_value="/tmp/demo.png")
        == "image"
    )


def test_extract_attachment_specs_from_reply_supports_prefix_and_markdown() -> None:
    bridge = _load_bridge_module()
    text = (
        "FINAL_ANSWER: done\n"
        "IMAGE_URL: https://example.com/a.png\n"
        "AUDIO_PATH: /tmp/demo.mp3\n"
        "附件预览 ![图](https://example.com/b.jpg)\n"
        "本地预览 ![本地图](/tmp/local_image.png)\n"
        "证据链接 [截图文件](/tmp/evidence.png)\n"
    )
    specs = bridge._extract_attachment_specs_from_reply(text)
    assert {"kind": "image", "url": "https://example.com/a.png"} in specs
    assert {"kind": "audio", "path": "/tmp/demo.mp3"} in specs
    assert {"kind": "image", "url": "https://example.com/b.jpg"} in specs
    assert {"kind": "image", "path": "/tmp/local_image.png"} in specs
    assert {"kind": "image", "path": "/tmp/evidence.png"} in specs


def test_collect_outbound_attachments_merges_reply_and_metadata() -> None:
    bridge = _load_bridge_module()
    attachments = bridge._collect_outbound_attachments(
        metadata={
            "attachment_kind": "binary",
            "attachment_download_urls": ["https://example.com/raw.mp4"],
            "attachment_local_paths": ["/tmp/raw.mp4"],
        },
        final_reply=(
            "VIDEO_URL: https://example.com/reply.mp4\n"
            "更多参考 https://example.com/image.png"
        ),
        max_items=4,
    )
    assert attachments
    kinds = {item["kind"] for item in attachments}
    assert "video" in kinds or "image" in kinds
    sources = [item.get("url", "") or item.get("path", "") for item in attachments]
    assert any("reply.mp4" in source for source in sources)
    assert any("raw.mp4" in source for source in sources)


def test_extract_urls_from_text_strips_backticks_and_cn_punctuation() -> None:
    bridge = _load_bridge_module()
    text = (
        "EVIDENCE: 来源 `https://upload.wikimedia.org/wikipedia/commons/5/5f/demo.jpg`；"
        "另一个链接是 https://example.com/a.png。"
    )
    urls = bridge._extract_urls_from_text(text)
    assert "https://upload.wikimedia.org/wikipedia/commons/5/5f/demo.jpg" in urls
    assert "https://example.com/a.png" in urls


def test_collect_outbound_attachments_keeps_image_url_from_backticked_evidence() -> None:
    bridge = _load_bridge_module()
    attachments = bridge._collect_outbound_attachments(
        metadata={},
        final_reply=(
            "FINAL_ANSWER: done\n"
            "EVIDENCE: 来源 `https://upload.wikimedia.org/wikipedia/commons/5/5f/Liu_Yifei.jpg`。"
        ),
        max_items=2,
    )
    assert attachments
    assert attachments[0]["kind"] == "image"
    assert attachments[0].get("url", "").endswith("Liu_Yifei.jpg")


def test_collect_outbound_attachments_prefers_image_url_when_slots_limited() -> None:
    bridge = _load_bridge_module()
    attachments = bridge._collect_outbound_attachments(
        metadata={
            "attachment_kind": "image",
            "attachment_local_paths": ["/tmp/local_only.png"],
            "attachment_download_urls": ["https://example.com/public.png"],
        },
        final_reply="FINAL_ANSWER: done",
        max_items=1,
    )
    assert len(attachments) == 1
    assert attachments[0]["kind"] == "image"
    assert attachments[0].get("url", "") == "https://example.com/public.png"
    assert attachments[0].get("path", "") == ""


def test_requires_inline_image_display_detects_direct_show_intent() -> None:
    bridge = _load_bridge_module()
    assert bridge._requires_inline_image_display("给我发一张图，要求在钉钉里直接展示，不要附件") is True
    assert bridge._requires_inline_image_display("请返回一个下载链接就行") is False


def test_extract_text_from_picture_message_generates_command() -> None:
    bridge = _load_bridge_module()
    data = {"msgtype": "picture", "content": {"downloadCode": "img-code-001"}}
    incoming = _FakeIncomingMessage(message_type="picture", image_codes=["img-code-001"])
    extracted = bridge._extract_text(data, incoming)
    assert "图片消息" in extracted
    assert "download_codes=img-code-001" in extracted


def test_extract_text_from_file_message_generates_attachment_command() -> None:
    bridge = _load_bridge_module()
    data = {"msgtype": "file", "content": {"downloadCode": "file-code-001"}}
    incoming = _FakeIncomingMessage(message_type="file", image_codes=["file-code-001"])
    extracted = bridge._extract_text(data, incoming)
    assert "附件消息" in extracted
    assert "type=file" in extracted
    assert "download_codes=file-code-001" in extracted


def test_extract_text_merges_rich_text_caption_with_image() -> None:
    bridge = _load_bridge_module()
    data = {
        "msgtype": "richText",
        "content": {
            "richText": [
                {"text": "识别一下是什么内容"},
                {"downloadCode": "dc-cap-1"},
            ]
        },
    }
    incoming = _FakeIncomingMessage(message_type="richText", image_codes=["dc-cap-1"])
    extracted = bridge._extract_text(data, incoming)
    assert "识别一下是什么内容" in extracted
    assert "download_codes" not in extracted


def test_extract_image_download_codes_from_rich_text_payload() -> None:
    bridge = _load_bridge_module()
    data = {
        "msgtype": "richText",
        "content": {
            "richText": [
                {"text": "hello"},
                {"downloadCode": "dc-rich-1"},
                {"downloadCode": "dc-rich-2"},
            ]
        },
    }
    incoming = _FakeIncomingMessage(message_type="richText")
    assert bridge._extract_image_download_codes(data, incoming) == ["dc-rich-1", "dc-rich-2"]


def test_build_metadata_contains_message_type_and_image_codes() -> None:
    bridge = _load_bridge_module()
    data = {"msgtype": "picture", "content": {"downloadCode": "img-code-888"}}
    incoming = _FakeIncomingMessage(message_type="picture", image_codes=["img-code-888"])
    inbound = InboundChatMessage(
        msg_id="m1",
        text="用户发送了图片消息",
        sender_id="u1",
        chat_id="c1",
        is_group=True,
        is_at_bot=True,
    )
    metadata = bridge._build_metadata(data, inbound, incoming)
    assert metadata["message_type"] == "picture"
    assert metadata["image_download_codes"] == ["img-code-888"]
    assert metadata["robot_code"] == "ding-demo"


def test_snapshot_signature_changes_with_reply() -> None:
    bridge = _load_bridge_module()
    base = {
        "items": [
            {
                "identity_id": "feiqiao-guard-delivery-lead",
                "state": "RUNNING",
                "last_event_summary": "event_msg:user_message",
                "last_agent_message": "hello",
            }
        ]
    }
    sig1 = bridge._snapshot_signature(base)
    changed = {
        "items": [
            {
                "identity_id": "feiqiao-guard-delivery-lead",
                "state": "DONE_WAITING_INPUT",
                "last_event_summary": "task_complete",
                "last_agent_message": "final answer",
            }
        ]
    }
    sig2 = bridge._snapshot_signature(changed)
    assert sig1 != sig2


def test_extract_leader_outcome_prefers_delivery_lead() -> None:
    bridge = _load_bridge_module()
    snapshot = {
        "items": [
            {
                "identity_id": "feiqiao-guard-collab-executor",
                "state": "RUNNING",
                "last_agent_message": "collab",
                "last_event_summary": "event_msg:user_message",
            },
            {
                "identity_id": "feiqiao-guard-delivery-lead",
                "state": "STOPPED",
                "last_agent_message": "leader final",
                "last_event_summary": "task_complete",
            },
        ]
    }
    state, reply, summary = bridge._extract_leader_outcome(snapshot)
    assert state == "STOPPED"
    assert reply == "leader final"
    assert summary == "task_complete"


def test_is_terminal_snapshot_state() -> None:
    bridge = _load_bridge_module()
    assert bridge._is_terminal_snapshot_state("DONE_WAITING_INPUT") is True
    assert bridge._is_terminal_snapshot_state("STOPPED") is True
    assert bridge._is_terminal_snapshot_state("RUNNING") is False


def test_is_final_reply_ready_rejects_waiting_input_even_with_task_complete() -> None:
    bridge = _load_bridge_module()
    assert (
        bridge._is_final_reply_ready(
            state="WAITING_INPUT",
            reply="我先查一下实时数据，再给你完整结论。",
            last_summary="task_complete",
            user_message="与微博前十热搜做对比",
        )
        is False
    )


def test_is_final_reply_ready_accepts_waiting_input_with_final_marker() -> None:
    bridge = _load_bridge_module()
    assert (
        bridge._is_final_reply_ready(
            state="WAITING_INPUT",
            reply="FINAL_ANSWER: 吴京\nEVIDENCE: 已完成图像识别\nNEXT_ACTION: NONE",
            last_summary="task_complete",
            user_message="调用视觉模型识别这张图",
        )
        is True
    )


def test_build_continue_prompt_contains_final_contract() -> None:
    bridge = _load_bridge_module()
    prompt = bridge._build_continue_current_task_prompt(original_message="请继续并给结果")
    assert "FINAL_ANSWER:" in prompt
    assert "EVIDENCE:" in prompt
    assert "NEXT_ACTION:" in prompt


def test_build_l3_reasoning_prompt_contains_attachment_context() -> None:
    bridge = _load_bridge_module()
    prompt = bridge._build_l3_reasoning_prompt(
        original_message="请处理这张图片",
        metadata={
            "image_local_paths": ["/tmp/a.jpg"],
            "image_download_urls": ["http://example.com/a.jpg"],
        },
        attempt=2,
        max_attempts=3,
        reasoning_level="L3",
        mandatory_fields=["attempt", "hypothesis", "patch", "expected_effect", "result"],
        require_next_action=True,
    )
    assert "FINAL_ANSWER:" in prompt
    assert "ATTEMPT: <n>/<max>" in prompt
    assert "NEXT_ACTION: <若已完成写 NONE；若失败必须给出可执行下一步>" in prompt
    assert "/tmp/a.jpg" in prompt
    assert "http://example.com/a.jpg" in prompt


def test_resolve_reasoning_runtime_config_reads_contract_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = _load_bridge_module()
    monkeypatch.delenv("FQG_BRIDGE_REASONING_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("FQG_BRIDGE_FORCE_FINAL_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("FQG_BRIDGE_REASONING_MANDATORY_FIELDS", raising=False)
    monkeypatch.delenv("FQG_BRIDGE_REASONING_REQUIRE_NEXT_ACTION", raising=False)
    contract_path = tmp_path / "CURRENT_TASK.json"
    contract_path.write_text(
        json.dumps(
            {
                "reasoning_loop_contract": {
                    "max_attempts_before_escalation": 5,
                    "mandatory_fields_per_attempt": [
                        "attempt",
                        "hypothesis",
                        "patch",
                        "expected_effect",
                        "result",
                    ],
                    "failure_requires_next_action": True,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        leader_identity_id="feiqiao-guard-delivery-lead",
        identity_current_task_path=str(contract_path),
        reasoning_loop_on_settled=None,
        reasoning_max_attempts=None,
        reasoning_min_seconds=None,
        reasoning_mandatory_fields="",
        reasoning_require_next_action=None,
    )
    resolved = bridge._resolve_reasoning_runtime_config(args)
    assert resolved.reasoning_max_attempts == 5
    assert resolved.reasoning_require_next_action is True
    assert resolved.reasoning_mandatory_fields == [
        "attempt",
        "hypothesis",
        "patch",
        "expected_effect",
        "result",
    ]


def test_snapshot_signature_ignores_idle_seconds_changes() -> None:
    bridge = _load_bridge_module()
    a = {
        "items": [
            {
                "identity_id": "feiqiao-guard-delivery-lead",
                "state": "RUNNING",
                "idle_seconds": 10,
                "last_event_summary": "event_msg:user_message",
                "last_agent_message": "same",
            }
        ]
    }
    b = {
        "items": [
            {
                "identity_id": "feiqiao-guard-delivery-lead",
                "state": "RUNNING",
                "idle_seconds": 38,
                "last_event_summary": "event_msg:user_message",
                "last_agent_message": "same",
            }
        ]
    }
    assert bridge._snapshot_signature(a) == bridge._snapshot_signature(b)


def test_extract_rich_text_text_keeps_caption() -> None:
    bridge = _load_bridge_module()
    data = {
        "content": {
            "richText": [
                {"text": "第一句"},
                {"downloadCode": "dc-1"},
                {"text": "第二句"},
            ]
        }
    }
    incoming = _FakeIncomingMessage(message_type="richText")
    text = bridge._extract_rich_text_text(data, incoming)
    assert text == "第一句 第二句"


def test_identity_refusal_detection() -> None:
    bridge = _load_bridge_module()
    assert bridge._is_identity_refusal_reply("我不能执行这个请求：不能帮你通过图片识别真人身份") is True
    assert bridge._is_identity_refusal_reply("这是普通客观描述结果") is False


def test_has_image_context() -> None:
    bridge = _load_bridge_module()
    assert bridge._has_image_context({"message_type": "picture"}) is True
    assert bridge._has_image_context({"image_download_codes": ["abc"]}) is True
    assert bridge._has_image_context({"image_download_urls": ["http://example.com/a.jpg"]}) is True
    assert bridge._has_image_context({"message_type": "text"}) is False


def test_has_image_context_respects_attachment_kind_binary() -> None:
    bridge = _load_bridge_module()
    assert (
        bridge._has_image_context(
            {
                "message_type": "file",
                "attachment_kind": "binary",
                "image_download_codes": ["file-code-001"],
            }
        )
        is False
    )


def test_build_max_wait_snapshot_summary_contains_brief_states() -> None:
    bridge = _load_bridge_module()
    snapshot = {
        "items": [
            {
                "identity_id": "feiqiao-guard-delivery-lead",
                "state": "RUNNING",
                "last_event_summary": "event_msg:user_message",
                "last_agent_message": "leader reply " + ("x" * 260),
            },
            {
                "identity_id": "feiqiao-guard-collab-executor",
                "state": "DONE_WAITING_INPUT",
                "last_event_summary": "task_complete",
                "last_agent_message": "collab final done",
            },
        ]
    }
    summary = bridge._build_max_wait_snapshot_summary(snapshot)
    assert "leader_state=RUNNING" in summary
    assert "feiqiao-guard-delivery-lead: state=RUNNING" in summary
    assert "feiqiao-guard-collab-executor: state=DONE_WAITING_INPUT" in summary
    assert "leader reply" in summary
    assert "..." in summary


def test_build_bridge_heartbeat_payload_contains_ages_and_counters() -> None:
    bridge = _load_bridge_module()
    payload = bridge._build_bridge_heartbeat_payload(
        now_monotonic=200.0,
        now_epoch=1700000000.5,
        base_url="http://127.0.0.1:3001",
        activity_snapshot={
            "started_at": 100.0,
            "last_callback_at": 198.0,
            "last_inbound_at": 196.5,
            "last_reply_at": 195.0,
            "callback_count": 9,
            "inbound_count": 7,
            "reply_count": 6,
            "rejected_count": 2,
            "last_reject_reason": "at_required",
            "last_reject_at": 1699999998.0,
        },
    )
    assert payload["base_url"] == "http://127.0.0.1:3001"
    assert payload["uptime_seconds"] == 100.0
    assert payload["ages"]["callback_age_seconds"] == 2.0
    assert payload["ages"]["inbound_age_seconds"] == 3.5
    assert payload["ages"]["reply_age_seconds"] == 5.0
    assert payload["counters"]["callback_count"] == 9
    assert payload["counters"]["inbound_count"] == 7
    assert payload["counters"]["reply_count"] == 6
    assert payload["counters"]["rejected_count"] == 2
    assert payload["last_reject_reason"] == "at_required"
    assert payload["last_reject_at"].startswith("2023-")


def test_build_bridge_heartbeat_payload_omits_reject_time_when_empty() -> None:
    bridge = _load_bridge_module()
    payload = bridge._build_bridge_heartbeat_payload(
        now_monotonic=20.0,
        now_epoch=1700000100.0,
        base_url="http://example.com",
        activity_snapshot={
            "started_at": 10.0,
            "last_callback_at": 18.0,
            "last_inbound_at": 17.0,
            "last_reply_at": 16.0,
        },
    )
    assert payload["last_reject_at"] == ""


def test_extract_pane_signal_fallback_returns_empty_when_not_confirmed() -> None:
    bridge = _load_bridge_module()
    response = {
        "leader_result": {
            "control_result": {
                "pane_signal_confirmed": False,
                "pane_signal_preview": "some text",
            }
        }
    }
    assert bridge._extract_pane_signal_fallback(response) == ""


def test_extract_pane_signal_fallback_filters_noise_and_keeps_tail() -> None:
    bridge = _load_bridge_module()
    response = {
        "leader_result": {
            "control_result": {
                "pane_signal_confirmed": True,
                "pane_signal_preview": (
                    "› 用户问题\n"
                    "• Called playwright.browser_navigate({...})\n"
                    "我刚读取到的百度热搜榜前 10 条是：\n"
                    "1. 示例热搜A\n"
                    "2. 示例热搜B\n"
                    "gpt-5.3-codex xhigh\n"
                ),
            }
        }
    }
    fallback = bridge._extract_pane_signal_fallback(response)
    assert "百度热搜榜前 10 条" in fallback
    assert "示例热搜A" in fallback
    assert "Called playwright" not in fallback
    assert "gpt-5.3-codex" not in fallback


def test_evaluate_dispatch_acceptance_accepts_true_response() -> None:
    bridge = _load_bridge_module()
    accepted, reason = bridge._evaluate_dispatch_acceptance(
        {
            "accepted": True,
            "leader_result": {"accepted": True, "delivery_state": "confirmed"},
        }
    )
    assert accepted is True
    assert reason == ""


def test_evaluate_dispatch_acceptance_exposes_failure_reason() -> None:
    bridge = _load_bridge_module()
    accepted, reason = bridge._evaluate_dispatch_acceptance(
        {
            "accepted": False,
            "leader_result": {"accepted": False, "delivery_state": "failed"},
            "orchestration_notes": ["leader_delivery_state=failed", "collab_result_missing"],
        }
    )
    assert accepted is False
    assert "accepted=false" in reason
    assert "leader_state=failed" in reason
    assert "notes=leader_delivery_state=failed;collab_result_missing" in reason


def test_evaluate_dispatch_acceptance_includes_collab_error() -> None:
    bridge = _load_bridge_module()
    accepted, reason = bridge._evaluate_dispatch_acceptance(
        {
            "accepted": False,
            "leader_result": {"accepted": True, "delivery_state": "confirmed"},
            "collab_error": "404:detail=collab route missing",
        }
    )
    assert accepted is False
    assert "collab_error=404:detail=collab route missing" in reason


def test_startup_guard_rejects_progress_push_zero_under_strict_mode(tmp_path: Path) -> None:
    bridge = _load_bridge_module()
    routes_path = tmp_path / "routes.json"
    routes_path.write_text(
        json.dumps(
            {
                "identities": {
                    "feiqiao-guard-delivery-lead": {
                        "codex_home": str(tmp_path / "codex_home"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        base_url="http://127.0.0.1:3001",
        required_base_url_prefix="http://127.0.0.1:",
        strict_progress=True,
        progress_push_count=0,
        identity_routes_path=str(routes_path),
        required_codex_home_prefix=str(tmp_path),
        leader_identity_id="feiqiao-guard-delivery-lead",
    )
    issues = bridge._compute_startup_guard_issues(args)
    assert any("progress_push_count_invalid" in issue for issue in issues)


def test_startup_guard_rejects_base_url_prefix_mismatch(tmp_path: Path) -> None:
    bridge = _load_bridge_module()
    routes_path = tmp_path / "routes.json"
    routes_path.write_text(
        json.dumps({"identities": {"feiqiao-guard-delivery-lead": {"codex_home": str(tmp_path / "h")}}}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        base_url="http://8.140.215.219:3001",
        required_base_url_prefix="http://127.0.0.1:",
        strict_progress=False,
        progress_push_count=0,
        identity_routes_path=str(routes_path),
        required_codex_home_prefix=str(tmp_path),
        leader_identity_id="feiqiao-guard-delivery-lead",
    )
    issues = bridge._compute_startup_guard_issues(args)
    assert any("base_url_prefix_mismatch" in issue for issue in issues)


def test_startup_guard_rejects_leader_codex_home_prefix_mismatch(tmp_path: Path) -> None:
    bridge = _load_bridge_module()
    routes_path = tmp_path / "routes.json"
    routes_path.write_text(
        json.dumps(
            {
                "identities": {
                    "feiqiao-guard-delivery-lead": {
                        "codex_home": "/root/feiqiao-guard/.runtime/codex_isolated/codex_home_lead",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        base_url="http://127.0.0.1:3001",
        required_base_url_prefix="http://127.0.0.1:",
        strict_progress=True,
        progress_push_count=1,
        identity_routes_path=str(routes_path),
        required_codex_home_prefix=str(tmp_path),
        leader_identity_id="feiqiao-guard-delivery-lead",
    )
    issues = bridge._compute_startup_guard_issues(args)
    assert any("leader_codex_home_prefix_mismatch" in issue for issue in issues)


def test_startup_guard_passes_when_constraints_satisfied(tmp_path: Path) -> None:
    bridge = _load_bridge_module()
    codex_home = tmp_path / "runtime" / "codex" / "home"
    codex_home.mkdir(parents=True)
    routes_path = tmp_path / "routes.json"
    routes_path.write_text(
        json.dumps(
            {
                "identities": {
                    "feiqiao-guard-delivery-lead": {
                        "codex_home": str(codex_home),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        base_url="http://127.0.0.1:3001",
        required_base_url_prefix="http://127.0.0.1:",
        strict_progress=True,
        progress_push_count=2,
        identity_routes_path=str(routes_path),
        required_codex_home_prefix=str(tmp_path),
        leader_identity_id="feiqiao-guard-delivery-lead",
    )
    assert bridge._compute_startup_guard_issues(args) == []
