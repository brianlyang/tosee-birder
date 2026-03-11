from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path


def _load_control_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "guarded_session_control.py"
    spec = importlib.util.spec_from_file_location("guarded_session_control", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclass resolution expects the module to be present in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


control = _load_control_module()


def test_tmux_session_name_sanitizes_prefix() -> None:
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    assert control._tmux_session_name(sid, "fqg.prod") == "fqgprod-019cb379"
    assert control._tmux_session_name(sid, "!!!") == "fqg-019cb379"


def test_capture_rollout_signature_returns_empty_when_missing(tmp_path: Path) -> None:
    sig = control._capture_rollout_signature(tmp_path, "019cb379-59ff-72f1-9380-2e1b687257a6")
    assert sig.rollout_path == ""
    assert sig.size == 0
    assert sig.mtime_ns == 0


def test_tmux_socket_path_uses_preferred_under_short_root() -> None:
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="fqgss-") as short_root:
        socket_path = control._tmux_socket_for_sid(Path(short_root), sid)
        assert str(socket_path).endswith(f"{sid}.sock")
        assert "session_monitor" in str(socket_path)
        assert len(str(socket_path)) <= control.MAX_UNIX_SOCKET_PATH_LEN


def test_tmux_socket_path_falls_back_when_path_too_long() -> None:
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    long_root = Path("/tmp") / ("deep" * 40)
    socket_path = control._tmux_socket_for_sid(long_root, sid)
    assert sid not in str(socket_path)
    assert socket_path.suffix == ".sock"
    assert len(str(socket_path)) <= control.MAX_UNIX_SOCKET_PATH_LEN


def test_wait_rollout_advance_detects_size_change(tmp_path: Path) -> None:
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    rollout = (
        tmp_path
        / "sessions"
        / "2026"
        / "03"
        / "04"
        / f"rollout-2026-03-04T00-00-00-{sid}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text('{"type":"event_msg","payload":{"type":"task_started"}}\n', encoding="utf-8")
    before = control._capture_rollout_signature(tmp_path, sid)
    rollout.write_text(
        '{"type":"event_msg","payload":{"type":"task_started"}}\n'
        '{"type":"event_msg","payload":{"type":"user_message"}}\n',
        encoding="utf-8",
    )
    advanced, after = control._wait_rollout_advance(
        codex_home=tmp_path,
        session_id=sid,
        before=before,
        timeout_seconds=1.0,
        poll_interval_seconds=0.1,
    )
    assert advanced is True
    assert after.size > before.size


def test_continue_once_bootstrap_nudges_new_tmux_session() -> None:
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    before_sig = control.RolloutSignature(rollout_path="/tmp/a.jsonl", size=10, mtime_ns=1)
    after_sig = control.RolloutSignature(rollout_path="/tmp/a.jsonl", size=20, mtime_ns=2)
    calls = {"wait": 0, "enter": 0, "send": 0}

    original_ensure_tmux_exists = control._ensure_tmux_exists
    original_tmux_socket_for_sid = control._tmux_socket_for_sid
    original_tmux_session_name = control._tmux_session_name
    original_capture_rollout_signature = control._capture_rollout_signature
    original_tmux_has_session = control._tmux_has_session
    original_tmux_start_resume = control._tmux_start_resume
    original_wait_rollout_advance = control._wait_rollout_advance
    original_tmux_press_enter = control._tmux_press_enter
    original_tmux_send_keys = control._tmux_send_keys

    control._ensure_tmux_exists = lambda: "tmux"  # type: ignore[assignment]
    control._tmux_socket_for_sid = lambda codex_home, session_id: Path("/tmp/fqg-test.sock")  # type: ignore[assignment]
    control._tmux_session_name = lambda session_id, prefix: "fqg-019cb379"  # type: ignore[assignment]
    control._capture_rollout_signature = lambda codex_home, session_id: before_sig  # type: ignore[assignment]
    control._tmux_has_session = lambda tmux_bin, socket_path, session_name: False  # type: ignore[assignment]
    control._tmux_start_resume = (  # type: ignore[assignment]
        lambda tmux_bin, **kwargs: subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")
    )

    def _stub_wait(*, codex_home, session_id, before, timeout_seconds, poll_interval_seconds=0.5):
        calls["wait"] += 1
        if calls["wait"] == 1:
            return False, before_sig
        return True, after_sig

    def _stub_enter(tmux_bin, socket_path, session_name):
        calls["enter"] += 1
        return subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

    def _stub_send(tmux_bin, socket_path, session_name, text):
        calls["send"] += 1
        return subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

    control._wait_rollout_advance = _stub_wait  # type: ignore[assignment]
    control._tmux_press_enter = _stub_enter  # type: ignore[assignment]
    control._tmux_send_keys = _stub_send  # type: ignore[assignment]

    try:
        rc, payload = control._continue_once(
            root_dir=Path("/tmp"),
            codex_home=Path("/tmp"),
            session_id=sid,
            text="继续执行",
            verify_seconds=8.0,
            session_name_prefix="fqg",
            tmux_socket_path=None,
        )
    finally:
        control._ensure_tmux_exists = original_ensure_tmux_exists  # type: ignore[assignment]
        control._tmux_socket_for_sid = original_tmux_socket_for_sid  # type: ignore[assignment]
        control._tmux_session_name = original_tmux_session_name  # type: ignore[assignment]
        control._capture_rollout_signature = original_capture_rollout_signature  # type: ignore[assignment]
        control._tmux_has_session = original_tmux_has_session  # type: ignore[assignment]
        control._tmux_start_resume = original_tmux_start_resume  # type: ignore[assignment]
        control._wait_rollout_advance = original_wait_rollout_advance  # type: ignore[assignment]
        control._tmux_press_enter = original_tmux_press_enter  # type: ignore[assignment]
        control._tmux_send_keys = original_tmux_send_keys  # type: ignore[assignment]

    assert rc == 0
    assert payload["ok"] is True
    assert payload["bootstrap_attempted"] is True
    assert payload["action"] == "tmux_new_resume_bootstrap"
    assert payload["rollout_advanced"] is True
    assert calls["wait"] == 2
    assert calls["enter"] == 2
    assert calls["send"] == 1


def test_continue_once_send_keys_commit_nudge_for_existing_session() -> None:
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    before_sig = control.RolloutSignature(rollout_path="/tmp/a.jsonl", size=10, mtime_ns=1)
    after_sig = control.RolloutSignature(rollout_path="/tmp/a.jsonl", size=20, mtime_ns=2)
    calls = {"wait": 0, "enter": 0, "send": 0}

    original_ensure_tmux_exists = control._ensure_tmux_exists
    original_tmux_socket_for_sid = control._tmux_socket_for_sid
    original_tmux_session_name = control._tmux_session_name
    original_capture_rollout_signature = control._capture_rollout_signature
    original_tmux_has_session = control._tmux_has_session
    original_tmux_send_keys = control._tmux_send_keys
    original_wait_rollout_advance = control._wait_rollout_advance
    original_tmux_press_enter = control._tmux_press_enter

    control._ensure_tmux_exists = lambda: "tmux"  # type: ignore[assignment]
    control._tmux_socket_for_sid = lambda codex_home, session_id: Path("/tmp/fqg-test.sock")  # type: ignore[assignment]
    control._tmux_session_name = lambda session_id, prefix: "fqg-019cb379"  # type: ignore[assignment]
    control._capture_rollout_signature = lambda codex_home, session_id: before_sig  # type: ignore[assignment]
    control._tmux_has_session = lambda tmux_bin, socket_path, session_name: True  # type: ignore[assignment]

    def _stub_send(tmux_bin, socket_path, session_name, text):
        calls["send"] += 1
        return subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

    def _stub_wait(*, codex_home, session_id, before, timeout_seconds, poll_interval_seconds=0.5):
        calls["wait"] += 1
        if calls["wait"] == 1:
            return False, before_sig
        return True, after_sig

    def _stub_enter(tmux_bin, socket_path, session_name):
        calls["enter"] += 1
        return subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

    control._tmux_send_keys = _stub_send  # type: ignore[assignment]
    control._wait_rollout_advance = _stub_wait  # type: ignore[assignment]
    control._tmux_press_enter = _stub_enter  # type: ignore[assignment]

    try:
        rc, payload = control._continue_once(
            root_dir=Path("/tmp"),
            codex_home=Path("/tmp"),
            session_id=sid,
            text="继续执行",
            verify_seconds=8.0,
            session_name_prefix="fqg",
            tmux_socket_path=None,
        )
    finally:
        control._ensure_tmux_exists = original_ensure_tmux_exists  # type: ignore[assignment]
        control._tmux_socket_for_sid = original_tmux_socket_for_sid  # type: ignore[assignment]
        control._tmux_session_name = original_tmux_session_name  # type: ignore[assignment]
        control._capture_rollout_signature = original_capture_rollout_signature  # type: ignore[assignment]
        control._tmux_has_session = original_tmux_has_session  # type: ignore[assignment]
        control._tmux_send_keys = original_tmux_send_keys  # type: ignore[assignment]
        control._wait_rollout_advance = original_wait_rollout_advance  # type: ignore[assignment]
        control._tmux_press_enter = original_tmux_press_enter  # type: ignore[assignment]

    assert rc == 0
    assert payload["ok"] is True
    assert payload["bootstrap_attempted"] is True
    assert payload["action"] == "tmux_send_keys_commit"
    assert payload["rollout_advanced"] is True
    assert calls["send"] == 1
    assert calls["wait"] == 2
    assert calls["enter"] == 1


def test_continue_once_new_session_warmup_fail_close_blocks_dispatch() -> None:
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    before_sig = control.RolloutSignature(rollout_path="/tmp/a.jsonl", size=10, mtime_ns=1)

    original_ensure_tmux_exists = control._ensure_tmux_exists
    original_tmux_socket_for_sid = control._tmux_socket_for_sid
    original_tmux_session_name = control._tmux_session_name
    original_capture_rollout_signature = control._capture_rollout_signature
    original_tmux_has_session = control._tmux_has_session
    original_tmux_start_resume = control._tmux_start_resume
    original_tmux_send_keys = control._tmux_send_keys
    original_wait_tmux_marker = control._wait_tmux_marker

    sends: list[str] = []

    control._ensure_tmux_exists = lambda: "tmux"  # type: ignore[assignment]
    control._tmux_socket_for_sid = lambda codex_home, session_id: Path("/tmp/fqg-test.sock")  # type: ignore[assignment]
    control._tmux_session_name = lambda session_id, prefix: "fqg-019cb379"  # type: ignore[assignment]
    control._capture_rollout_signature = lambda codex_home, session_id: before_sig  # type: ignore[assignment]
    control._tmux_has_session = lambda tmux_bin, socket_path, session_name: False  # type: ignore[assignment]
    control._tmux_start_resume = (  # type: ignore[assignment]
        lambda tmux_bin, **kwargs: subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")
    )

    def _stub_send(tmux_bin, socket_path, session_name, text):
        sends.append(text)
        return subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

    def _stub_wait_marker(
        *,
        tmux_bin,
        socket_path,
        session_name,
        marker,
        timeout_seconds,
        poll_interval_seconds=2.0,
        strict_line_match=False,
    ):
        return False, "no marker"

    control._tmux_send_keys = _stub_send  # type: ignore[assignment]
    control._wait_tmux_marker = _stub_wait_marker  # type: ignore[assignment]

    try:
        rc, payload = control._continue_once(
            root_dir=Path("/tmp"),
            codex_home=Path("/tmp"),
            session_id=sid,
            text="继续执行",
            verify_seconds=8.0,
            session_name_prefix="fqg",
            tmux_socket_path=None,
            warmup_seconds=60.0,
            warmup_prompt="请先输出STEP0_RESULT=READY",
            warmup_marker="STEP0_RESULT=READY",
            warmup_fail_close=True,
        )
    finally:
        control._ensure_tmux_exists = original_ensure_tmux_exists  # type: ignore[assignment]
        control._tmux_socket_for_sid = original_tmux_socket_for_sid  # type: ignore[assignment]
        control._tmux_session_name = original_tmux_session_name  # type: ignore[assignment]
        control._capture_rollout_signature = original_capture_rollout_signature  # type: ignore[assignment]
        control._tmux_has_session = original_tmux_has_session  # type: ignore[assignment]
        control._tmux_start_resume = original_tmux_start_resume  # type: ignore[assignment]
        control._tmux_send_keys = original_tmux_send_keys  # type: ignore[assignment]
        control._wait_tmux_marker = original_wait_tmux_marker  # type: ignore[assignment]

    assert rc == 1
    assert payload["ok"] is False
    assert payload["warmup_enabled"] is True
    assert payload["warmup_attempted"] is True
    assert payload["warmup_ready"] is False
    assert payload["action"] == "tmux_new_resume_warmup_timeout"
    assert sends == ["请先输出STEP0_RESULT=READY"]


def test_pane_signal_requires_expected_marker_when_text_contains_marker() -> None:
    text = "只回复 C04_OK <结构化结论>"
    delta = "• 我继续执行一轮在线探测并完成收尾回报。"
    assert control._pane_delta_has_agent_signal(delta, text=text) is False


def test_pane_signal_accepts_expected_marker_when_present() -> None:
    text = "请仅输出 FINAL_ANSWER=net_probe_ok"
    delta = "• FINAL_ANSWER=net_probe_ok"
    assert control._pane_delta_has_agent_signal(delta, text=text) is True


def test_pane_signal_without_marker_keeps_original_heuristic() -> None:
    text = "继续执行"
    delta = "• 已继续执行并完成本轮。"
    assert control._pane_delta_has_agent_signal(delta, text=text) is True


def test_wait_tmux_marker_strict_line_match_ignores_prompt_echo() -> None:
    original_capture = control._tmux_capture_tail
    try:
        control._tmux_capture_tail = (  # type: ignore[assignment]
            lambda tmux_bin, socket_path, session_name, start_line=-260: (
                "完成后仅输出一行：STEP0_RESULT=READY。"
            )
        )
        ok, _ = control._wait_tmux_marker(
            tmux_bin="tmux",
            socket_path=Path("/tmp/fqg-test.sock"),
            session_name="fqg-019cb379",
            marker="STEP0_RESULT=READY",
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
            strict_line_match=True,
        )
        assert ok is False

        ok_loose, _ = control._wait_tmux_marker(
            tmux_bin="tmux",
            socket_path=Path("/tmp/fqg-test.sock"),
            session_name="fqg-019cb379",
            marker="STEP0_RESULT=READY",
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
            strict_line_match=False,
        )
        assert ok_loose is True
    finally:
        control._tmux_capture_tail = original_capture  # type: ignore[assignment]


def test_continue_once_recreates_session_when_new_resume_disappears() -> None:
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    before_sig = control.RolloutSignature(rollout_path="/tmp/a.jsonl", size=10, mtime_ns=1)
    after_sig = control.RolloutSignature(rollout_path="/tmp/a.jsonl", size=22, mtime_ns=2)
    calls = {"start_resume": 0, "wait": 0}

    original_ensure_tmux_exists = control._ensure_tmux_exists
    original_tmux_socket_for_sid = control._tmux_socket_for_sid
    original_tmux_session_name = control._tmux_session_name
    original_capture_rollout_signature = control._capture_rollout_signature
    original_tmux_has_session = control._tmux_has_session
    original_tmux_start_resume = control._tmux_start_resume
    original_wait_rollout_advance = control._wait_rollout_advance
    original_tmux_send_keys = control._tmux_send_keys
    original_tmux_press_enter = control._tmux_press_enter
    original_tmux_capture_tail = control._tmux_capture_tail

    control._ensure_tmux_exists = lambda: "tmux"  # type: ignore[assignment]
    control._tmux_socket_for_sid = lambda codex_home, session_id: Path("/tmp/fqg-test.sock")  # type: ignore[assignment]
    control._tmux_session_name = lambda session_id, prefix: "fqg-019cb379"  # type: ignore[assignment]
    control._capture_rollout_signature = lambda codex_home, session_id: before_sig  # type: ignore[assignment]
    control._tmux_has_session = lambda tmux_bin, socket_path, session_name: False  # type: ignore[assignment]

    def _stub_start_resume(tmux_bin, **kwargs):
        calls["start_resume"] += 1
        return subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

    def _stub_wait(*, codex_home, session_id, before, timeout_seconds, poll_interval_seconds=0.5):
        calls["wait"] += 1
        if calls["wait"] == 1:
            return False, before_sig
        return True, after_sig

    control._tmux_start_resume = _stub_start_resume  # type: ignore[assignment]
    control._wait_rollout_advance = _stub_wait  # type: ignore[assignment]
    control._tmux_send_keys = lambda *args, **kwargs: subprocess.CompletedProcess(  # type: ignore[assignment]
        args=["tmux"], returncode=0, stdout="", stderr=""
    )
    control._tmux_press_enter = lambda *args, **kwargs: subprocess.CompletedProcess(  # type: ignore[assignment]
        args=["tmux"], returncode=1, stdout="", stderr="no server"
    )
    control._tmux_capture_tail = lambda *args, **kwargs: ""  # type: ignore[assignment]

    try:
        rc, payload = control._continue_once(
            root_dir=Path("/tmp"),
            codex_home=Path("/tmp"),
            session_id=sid,
            text="继续执行",
            verify_seconds=8.0,
            session_name_prefix="fqg",
            tmux_socket_path=None,
            warmup_seconds=0.0,
            warmup_prompt="",
        )
    finally:
        control._ensure_tmux_exists = original_ensure_tmux_exists  # type: ignore[assignment]
        control._tmux_socket_for_sid = original_tmux_socket_for_sid  # type: ignore[assignment]
        control._tmux_session_name = original_tmux_session_name  # type: ignore[assignment]
        control._capture_rollout_signature = original_capture_rollout_signature  # type: ignore[assignment]
        control._tmux_has_session = original_tmux_has_session  # type: ignore[assignment]
        control._tmux_start_resume = original_tmux_start_resume  # type: ignore[assignment]
        control._wait_rollout_advance = original_wait_rollout_advance  # type: ignore[assignment]
        control._tmux_send_keys = original_tmux_send_keys  # type: ignore[assignment]
        control._tmux_press_enter = original_tmux_press_enter  # type: ignore[assignment]
        control._tmux_capture_tail = original_tmux_capture_tail  # type: ignore[assignment]

    assert rc == 0
    assert payload["ok"] is True
    assert payload["fallback_used"] is True
    assert payload["action"] == "tmux_new_resume_recreate_direct"
    assert calls["start_resume"] == 2
    assert calls["wait"] == 2
