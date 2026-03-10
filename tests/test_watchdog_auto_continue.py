from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path


def _load_watch_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "watch_guarded_session.py"
    spec = importlib.util.spec_from_file_location("watch_guarded_session", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclass resolution expects the module to be present in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


watch = _load_watch_module()


def _snapshot(*, state: str, idle: int | None):
    return watch.Snapshot(
        session_id="019cb379-59ff-72f1-9380-2e1b687257a6",
        rollout_path=None,
        state=state,
        process_probe_ok=True,
        alive_processes=[],
        last_event_at=datetime.now(timezone.utc),
        last_event_type="user_message",
        last_event_summary="event_msg:user_message",
        last_agent_message="",
        idle_seconds=idle,
    )


def test_parse_trigger_states_defaults_when_empty() -> None:
    assert watch._parse_trigger_states("") == {"WAITING_INPUT", "WAITING_OR_STOPPED", "STOPPED"}


def test_should_trigger_auto_continue_when_waiting_and_idle_enough() -> None:
    snap = _snapshot(state="WAITING_INPUT", idle=180)
    should, reason = watch._should_trigger_auto_continue(
        snapshot=snap,
        trigger_states={"WAITING_INPUT"},
        min_idle_seconds=90,
        cooldown_seconds=120,
        now_epoch=2000.0,
        last_attempt_epoch=0.0,
    )
    assert should is True
    assert reason == "trigger"


def test_should_skip_auto_continue_during_cooldown() -> None:
    snap = _snapshot(state="WAITING_INPUT", idle=180)
    should, reason = watch._should_trigger_auto_continue(
        snapshot=snap,
        trigger_states={"WAITING_INPUT"},
        min_idle_seconds=90,
        cooldown_seconds=120,
        now_epoch=2000.0,
        last_attempt_epoch=1950.0,
    )
    assert should is False
    assert reason == "cooldown"
