from __future__ import annotations

from feiqiao_guard.wrapper import _extract_approval_payload, _extract_command, _should_auto_nudge


def test_should_auto_nudge_for_conversation_waiting_phrases() -> None:
    assert _should_auto_nudge("如果你同意，我可以继续执行下一步。")
    assert _should_auto_nudge("Would you like me to proceed with deployment?")
    assert _should_auto_nudge("Conversation interrupted - tell the model what to do differently.")
    assert _should_auto_nudge("Failed to apply patch")


def test_should_not_auto_nudge_for_approval_prompt() -> None:
    assert not _should_auto_nudge("Would you like to run the following command?")
    assert not _should_auto_nudge("是否运行以下命令")


def test_extract_command_from_fenced_block() -> None:
    prompt = """
Would you like to run the following command?
```bash
python3 scripts/guarded_session_control.py continue --session-id 019cb379-59ff-72f1-9380-2e1b687257a6
```
1. Approve
2. Reject
""".strip()
    assert (
        _extract_command(prompt)
        == "python3 scripts/guarded_session_control.py continue --session-id 019cb379-59ff-72f1-9380-2e1b687257a6"
    )


def test_extract_approval_payload_marks_parse_failed_with_default_approval() -> None:
    command, payload = _extract_approval_payload("Would you like to run the following command?", "command_execution")
    assert command == "<detected_prompt_without_command>"
    assert payload["prompt_parse_failed"] is True
    assert payload["unparsed_fallback_action"] == "APPROVAL"


def test_extract_approval_payload_uses_env_fallback_action(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FQG_UNPARSED_COMMAND_FALLBACK_ACTION", "enter")
    command, payload = _extract_approval_payload("Would you like to run the following command?", "command_execution")
    assert command == "<detected_prompt_without_command>"
    assert payload["prompt_parse_failed"] is True
    assert payload["unparsed_fallback_action"] == "ENTER"
