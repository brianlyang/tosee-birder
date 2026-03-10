#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
DEFAULT_CONTINUE_TEXT = "继续执行"
MAX_UNIX_SOCKET_PATH_LEN = 100


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_NEW_SESSION_WARMUP_SECONDS = float(os.getenv("FQG_NEW_SESSION_WARMUP_SECONDS", "0"))
DEFAULT_NEW_SESSION_WARMUP_MARKER = (
    os.getenv("FQG_NEW_SESSION_WARMUP_MARKER", "STEP0_RESULT=READY").strip()
    or "STEP0_RESULT=READY"
)
DEFAULT_NEW_SESSION_WARMUP_FAIL_CLOSE = _env_bool(
    "FQG_NEW_SESSION_WARMUP_FAIL_CLOSE", False
)
DEFAULT_NEW_SESSION_WARMUP_PROMPT = (
    os.getenv("FQG_NEW_SESSION_WARMUP_PROMPT", "").strip()
    or (
        "先执行S0身份自证并显示HUD，必须通过留存记忆完成自我识别，不要硬编码。"
        "完成后仅输出一行：STEP0_RESULT=READY。"
    )
)


@dataclass(frozen=True)
class RolloutSignature:
    rollout_path: str
    size: int
    mtime_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "rollout_path": self.rollout_path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }


def _default_root_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_codex_home(root_dir: Path) -> Path:
    env_codex_home = os.getenv("CODEX_HOME", "").strip()
    if env_codex_home:
        return Path(env_codex_home).expanduser().resolve()
    return (root_dir / ".runtime" / "codex_isolated" / "codex_home").resolve()


def _load_last_sid(codex_home: Path) -> str:
    sid_file = codex_home / "last_codex_session_id"
    if not sid_file.exists():
        raise SystemExit(f"last session file not found: {sid_file}")
    sid = sid_file.read_text(encoding="utf-8").strip()
    if not SESSION_ID_RE.match(sid):
        raise SystemExit(f"invalid session id in {sid_file}: {sid!r}")
    return sid


def _find_rollout_file(codex_home: Path, session_id: str) -> Path | None:
    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        return None
    matches = list(sessions_root.rglob(f"rollout-*{session_id}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime_ns)


def _capture_rollout_signature(codex_home: Path, session_id: str) -> RolloutSignature:
    rollout_path = _find_rollout_file(codex_home, session_id)
    if rollout_path is None or not rollout_path.exists():
        return RolloutSignature(rollout_path="", size=0, mtime_ns=0)
    st = rollout_path.stat()
    return RolloutSignature(
        rollout_path=str(rollout_path),
        size=int(st.st_size),
        mtime_ns=int(st.st_mtime_ns),
    )


def _wait_rollout_advance(
    *,
    codex_home: Path,
    session_id: str,
    before: RolloutSignature,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.5,
) -> tuple[bool, RolloutSignature]:
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    while time.monotonic() <= deadline:
        after = _capture_rollout_signature(codex_home, session_id)
        if after != before and (after.rollout_path or before.rollout_path):
            return True, after
        time.sleep(max(0.2, poll_interval_seconds))
    return False, _capture_rollout_signature(codex_home, session_id)


def _run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _join_output(*chunks: str) -> str:
    parts = [str(chunk or "").strip() for chunk in chunks if str(chunk or "").strip()]
    return "\n".join(parts).strip()


def _ensure_tmux_exists() -> str:
    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        raise SystemExit("tmux not found in PATH; install tmux first")
    return tmux_bin


def _tmux_socket_for_sid(codex_home: Path, session_id: str) -> Path:
    preferred = codex_home / "session_monitor" / "tmux" / f"{session_id}.sock"
    if len(str(preferred)) <= MAX_UNIX_SOCKET_PATH_LEN:
        return preferred
    # Fallback for long workspace paths; tmux unix socket path is length-limited.
    digest = hashlib.sha1(f"{codex_home}:{session_id}".encode("utf-8")).hexdigest()[:16]
    env_dir = os.getenv("FQG_TMUX_SOCKET_DIR", "").strip()
    candidates: list[Path] = []
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    # Prefer project-local runtime path for better isolation and sandbox compatibility.
    candidates.append((codex_home.parent.parent / "tmux").resolve())
    candidates.append(Path("/tmp/fqg_tmux"))
    candidates.append(Path("/tmp"))
    for socket_dir in candidates:
        candidate = socket_dir / f"{digest}.sock"
        if len(str(candidate)) <= MAX_UNIX_SOCKET_PATH_LEN:
            return candidate
    return Path(f"/tmp/{digest[:8]}.sock")


def _tmux_session_name(session_id: str, prefix: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "", prefix) or "fqg"
    return f"{normalized}-{session_id[:8]}"


def _tmux_has_session(tmux_bin: str, socket_path: Path, session_name: str) -> bool:
    cp = _run_cmd([tmux_bin, "-S", str(socket_path), "has-session", "-t", session_name])
    return cp.returncode == 0


def _tmux_send_keys(tmux_bin: str, socket_path: Path, session_name: str, text: str) -> subprocess.CompletedProcess[str]:
    # Use literal mode (-l) to avoid interpreting the text as key names.
    cp = _run_cmd([tmux_bin, "-S", str(socket_path), "send-keys", "-t", session_name, "-l", text])
    if cp.returncode != 0:
        return cp
    return _run_cmd([tmux_bin, "-S", str(socket_path), "send-keys", "-t", session_name, "Enter"])


def _tmux_press_enter(tmux_bin: str, socket_path: Path, session_name: str) -> subprocess.CompletedProcess[str]:
    return _run_cmd([tmux_bin, "-S", str(socket_path), "send-keys", "-t", session_name, "Enter"])


def _tmux_kill_session(tmux_bin: str, socket_path: Path, session_name: str) -> subprocess.CompletedProcess[str]:
    return _run_cmd([tmux_bin, "-S", str(socket_path), "kill-session", "-t", session_name])


def _tmux_capture_tail(
    tmux_bin: str,
    socket_path: Path,
    session_name: str,
    *,
    start_line: int = -260,
) -> str:
    cp = _run_cmd(
        [
            tmux_bin,
            "-S",
            str(socket_path),
            "capture-pane",
            "-p",
            "-S",
            str(start_line),
            "-t",
            session_name,
        ]
    )
    if cp.returncode != 0:
        return ""
    return str(cp.stdout or "")


def _wait_tmux_marker(
    *,
    tmux_bin: str,
    socket_path: Path,
    session_name: str,
    marker: str,
    timeout_seconds: float,
    poll_interval_seconds: float = 2.0,
) -> tuple[bool, str]:
    expected = str(marker or "").strip()
    if not expected:
        return True, ""
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    last_capture = ""
    while time.monotonic() <= deadline:
        capture = _tmux_capture_tail(
            tmux_bin,
            socket_path,
            session_name,
        )
        if capture:
            last_capture = capture
        if expected in capture:
            return True, capture
        time.sleep(max(0.5, float(poll_interval_seconds)))
    return False, last_capture


def _pane_hash(text: str) -> str:
    payload = (text or "").encode("utf-8", errors="ignore")
    return hashlib.sha1(payload).hexdigest()[:16]


def _pane_delta(before: str, after: str) -> str:
    if not before:
        return after
    idx = after.rfind(before)
    if idx >= 0:
        return after[idx + len(before) :]
    return after


def _extract_expected_markers(text: str) -> list[str]:
    raw = str(text or "")
    markers: list[str] = []
    for pattern in (
        r"\b[A-Z][A-Z0-9_-]{2,64}_OK\b",
        r"\bFINAL_ANSWER=[^\s\]]+\b",
        r"\bSTEP0_RESULT=[^\s\]]+\b",
    ):
        for token in re.findall(pattern, raw):
            token_norm = str(token).strip()
            if token_norm and token_norm not in markers:
                markers.append(token_norm)
    return markers


def _pane_delta_has_agent_signal(delta: str, text: str = "") -> bool:
    if not delta.strip():
        return False
    expected_markers = _extract_expected_markers(text)
    if expected_markers:
        return any(marker in delta for marker in expected_markers)
    patterns = [
        r"(?m)^\s*•\s+",
        r"FINAL_ANSWER\s*=",
        r"CONTINUE_EXEC_OK",
        r"(?i)\bdone\b",
        r"(?i)\bcompleted?\b",
        r"执行完成",
        r"已完成",
    ]
    return any(re.search(pattern, delta) for pattern in patterns)


def _tmux_start_resume(
    tmux_bin: str,
    *,
    root_dir: Path,
    socket_path: Path,
    session_name: str,
    session_id: str,
    text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    resume_cmd = f"./scripts/run_codex_guarded_lead.sh resume {shlex.quote(session_id)}"
    if text and text.strip():
        resume_cmd = f"{resume_cmd} {shlex.quote(text.strip())}"
    command = f"cd {shlex.quote(str(root_dir))} && {resume_cmd}"
    return _run_cmd(
        [
            tmux_bin,
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            session_name,
            command,
        ]
    )


def _continue_once(
    *,
    root_dir: Path,
    codex_home: Path,
    session_id: str,
    text: str,
    verify_seconds: float,
    session_name_prefix: str,
    tmux_socket_path: Path | None,
    warmup_seconds: float = 0.0,
    warmup_prompt: str = "",
    warmup_marker: str = DEFAULT_NEW_SESSION_WARMUP_MARKER,
    warmup_fail_close: bool = False,
) -> tuple[int, dict[str, Any]]:
    if not SESSION_ID_RE.match(session_id):
        return 2, {"error": f"invalid session_id: {session_id!r}"}

    tmux_bin = _ensure_tmux_exists()
    socket_path = tmux_socket_path or _tmux_socket_for_sid(codex_home, session_id)
    session_name = _tmux_session_name(session_id, session_name_prefix)
    before = _capture_rollout_signature(codex_home, session_id)
    pane_before = _tmux_capture_tail(tmux_bin, socket_path, session_name)
    pane_before_hash = _pane_hash(pane_before)

    action = ""
    tmux_output = ""
    fallback_used = False
    bootstrap_attempted = False
    bootstrap_output = ""
    warmup_enabled = warmup_seconds > 0 and bool(warmup_prompt.strip())
    warmup_attempted = False
    warmup_ready = not warmup_enabled
    warmup_output = ""
    warmup_marker_value = str(warmup_marker or "").strip() or DEFAULT_NEW_SESSION_WARMUP_MARKER
    verify_before = before
    pane_signal_confirmed = False
    pane_after_hash = pane_before_hash
    pane_signal_preview = ""

    if _tmux_has_session(tmux_bin, socket_path, session_name):
        action = "tmux_send_keys"
        cp = _tmux_send_keys(tmux_bin, socket_path, session_name, text)
        tmux_output = _join_output(cp.stdout, cp.stderr)
        if cp.returncode != 0:
            # Existing session may be stale/corrupted; recreate.
            fallback_used = True
            _tmux_kill_session(tmux_bin, socket_path, session_name)
            cp = _tmux_start_resume(
                tmux_bin,
                root_dir=root_dir,
                socket_path=socket_path,
                session_name=session_name,
                session_id=session_id,
                text=None if warmup_enabled else text,
            )
            action = "tmux_restart_resume"
            tmux_output = _join_output(tmux_output, cp.stdout, cp.stderr)
    else:
        cp = _tmux_start_resume(
            tmux_bin,
            root_dir=root_dir,
            socket_path=socket_path,
            session_name=session_name,
            session_id=session_id,
            text=None if warmup_enabled else text,
        )
        action = "tmux_new_resume"
        tmux_output = _join_output(cp.stdout, cp.stderr)

    if warmup_enabled and action in {"tmux_new_resume", "tmux_restart_resume"} and cp.returncode == 0:
        warmup_attempted = True
        warmup_cp = _tmux_send_keys(tmux_bin, socket_path, session_name, warmup_prompt.strip())
        warmup_output = _join_output(warmup_cp.stdout, warmup_cp.stderr)
        tmux_output = _join_output(tmux_output, warmup_output)
        if warmup_cp.returncode == 0:
            marker_ok, marker_capture = _wait_tmux_marker(
                tmux_bin=tmux_bin,
                socket_path=socket_path,
                session_name=session_name,
                marker=warmup_marker_value,
                timeout_seconds=max(1.0, float(warmup_seconds)),
            )
            warmup_ready = bool(marker_ok)
            warmup_output = _join_output(
                warmup_output,
                marker_capture[-2000:] if marker_capture else "",
            )
        else:
            warmup_ready = False
        if (not warmup_ready) and warmup_fail_close:
            after_sig = _capture_rollout_signature(codex_home, session_id)
            result = {
                "ok": False,
                "action": f"{action}_warmup_timeout",
                "fallback_used": fallback_used,
                "bootstrap_attempted": bootstrap_attempted,
                "bootstrap_output": bootstrap_output,
                "warmup_enabled": warmup_enabled,
                "warmup_attempted": warmup_attempted,
                "warmup_ready": warmup_ready,
                "warmup_seconds": float(warmup_seconds),
                "warmup_marker": warmup_marker_value,
                "warmup_fail_close": bool(warmup_fail_close),
                "warmup_output": warmup_output,
                "tmux_socket_path": str(socket_path),
                "tmux_session_name": session_name,
                "tmux_returncode": warmup_cp.returncode,
                "tmux_output": tmux_output,
                "before_signature": before.to_dict(),
                "after_signature": after_sig.to_dict(),
                "rollout_advanced": False,
                "pane_before_hash": pane_before_hash,
                "pane_after_hash": pane_before_hash,
                "pane_signal_confirmed": False,
                "pane_signal_preview": "",
                "attach_hint": f"tmux -S {shlex.quote(str(socket_path))} attach -t {shlex.quote(session_name)}",
            }
            return 1, result
        verify_before = _capture_rollout_signature(codex_home, session_id)
        dispatch_cp = _tmux_send_keys(tmux_bin, socket_path, session_name, text)
        cp = dispatch_cp
        tmux_output = _join_output(tmux_output, dispatch_cp.stdout, dispatch_cp.stderr)

    if cp.returncode != 0:
        result = {
            "ok": False,
            "action": action,
            "fallback_used": fallback_used,
            "bootstrap_attempted": bootstrap_attempted,
            "bootstrap_output": bootstrap_output,
            "warmup_enabled": warmup_enabled,
            "warmup_attempted": warmup_attempted,
            "warmup_ready": warmup_ready,
            "warmup_seconds": float(warmup_seconds) if warmup_enabled else 0.0,
            "warmup_marker": warmup_marker_value if warmup_enabled else "",
            "warmup_fail_close": bool(warmup_fail_close),
            "warmup_output": warmup_output,
            "tmux_socket_path": str(socket_path),
            "tmux_session_name": session_name,
            "tmux_returncode": cp.returncode,
            "tmux_output": tmux_output,
            "before_signature": before.to_dict(),
            "after_signature": before.to_dict(),
            "rollout_advanced": False,
            "pane_before_hash": pane_before_hash,
            "pane_after_hash": pane_before_hash,
            "pane_signal_confirmed": False,
            "pane_signal_preview": "",
        }
        return 1, result

    advanced, after = _wait_rollout_advance(
        codex_home=codex_home,
        session_id=session_id,
        before=verify_before,
        timeout_seconds=verify_seconds,
    )

    if (not advanced) and action in {"tmux_new_resume", "tmux_restart_resume"}:
        # New tmux sessions can pause on trust/bootstrap prompts before the
        # queued text can run. Nudge once with Enter + text.
        bootstrap_attempted = True
        enter_cp = _tmux_press_enter(tmux_bin, socket_path, session_name)
        bootstrap_output = (enter_cp.stdout + "\n" + enter_cp.stderr).strip()
        if enter_cp.returncode == 0:
            time.sleep(0.6)
            text_cp = _tmux_send_keys(tmux_bin, socket_path, session_name, text)
            text_output = (text_cp.stdout + "\n" + text_cp.stderr).strip()
            bootstrap_output = "\n".join([x for x in [bootstrap_output, text_output] if x]).strip()
            if text_cp.returncode == 0:
                # Some Codex prompts keep the typed text staged in the input
                # line; push one more Enter to commit reliably.
                final_enter_cp = _tmux_press_enter(tmux_bin, socket_path, session_name)
                final_enter_output = (final_enter_cp.stdout + "\n" + final_enter_cp.stderr).strip()
                bootstrap_output = "\n".join([x for x in [bootstrap_output, final_enter_output] if x]).strip()
                advanced, after = _wait_rollout_advance(
                    codex_home=codex_home,
                    session_id=session_id,
                    before=verify_before,
                    timeout_seconds=max(2.0, min(verify_seconds, 12.0)),
                )
                if advanced:
                    action = f"{action}_bootstrap"
    elif (not advanced) and action == "tmux_send_keys":
        # Existing sessions can occasionally keep typed content staged in the
        # input bar without committing it. Force one extra Enter to flush.
        bootstrap_attempted = True
        commit_cp = _tmux_press_enter(tmux_bin, socket_path, session_name)
        bootstrap_output = (commit_cp.stdout + "\n" + commit_cp.stderr).strip()
        if commit_cp.returncode == 0:
            advanced, after = _wait_rollout_advance(
                codex_home=codex_home,
                session_id=session_id,
                before=verify_before,
                timeout_seconds=max(2.0, min(verify_seconds, 12.0)),
            )
            if advanced:
                action = "tmux_send_keys_commit"

    if not advanced:
        pane_after = _tmux_capture_tail(tmux_bin, socket_path, session_name)
        pane_after_hash = _pane_hash(pane_after)
        pane_delta = _pane_delta(pane_before, pane_after)
        pane_signal_confirmed = _pane_delta_has_agent_signal(pane_delta, text=text)
        if pane_signal_confirmed:
            advanced = True
            action = f"{action}_pane_signal"
            pane_signal_preview = pane_delta[-800:]

    result = {
        "ok": advanced,
        "action": action,
        "fallback_used": fallback_used,
        "bootstrap_attempted": bootstrap_attempted,
        "bootstrap_output": bootstrap_output,
        "warmup_enabled": warmup_enabled,
        "warmup_attempted": warmup_attempted,
        "warmup_ready": warmup_ready,
        "warmup_seconds": float(warmup_seconds) if warmup_enabled else 0.0,
        "warmup_marker": warmup_marker_value if warmup_enabled else "",
        "warmup_fail_close": bool(warmup_fail_close),
        "warmup_output": warmup_output,
        "tmux_socket_path": str(socket_path),
        "tmux_session_name": session_name,
        "tmux_returncode": cp.returncode,
        "tmux_output": tmux_output,
        "before_signature": before.to_dict(),
        "after_signature": after.to_dict(),
        "rollout_advanced": advanced,
        "pane_before_hash": pane_before_hash,
        "pane_after_hash": pane_after_hash,
        "pane_signal_confirmed": pane_signal_confirmed,
        "pane_signal_preview": pane_signal_preview,
        "attach_hint": f"tmux -S {shlex.quote(str(socket_path))} attach -t {shlex.quote(session_name)}",
    }
    return (0 if advanced else 1), result


def main() -> int:
    parser = argparse.ArgumentParser(description="Control one guarded Codex session (continue/resume) via tmux.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_continue = sub.add_parser("continue", help="send continue text; auto-resume in tmux when needed")
    p_continue.add_argument("--session-id", default="", help="Codex session id; default reads last_codex_session_id")
    p_continue.add_argument("--codex-home", default="", help="Override isolated CODEX_HOME path")
    p_continue.add_argument("--workspace-root", default="", help="Project root; default is fqsh root")
    p_continue.add_argument("--text", default=DEFAULT_CONTINUE_TEXT, help="Text to send (default: 继续执行)")
    p_continue.add_argument("--verify-seconds", type=float, default=8.0, help="Wait time for rollout advance")
    p_continue.add_argument("--session-name-prefix", default="fqg", help="tmux session name prefix")
    p_continue.add_argument("--tmux-socket-path", default="", help="Override tmux socket path")
    p_continue.add_argument(
        "--warmup-seconds",
        type=float,
        default=DEFAULT_NEW_SESSION_WARMUP_SECONDS,
        help=(
            "When creating/restarting tmux session, wait up to this window for warmup marker "
            "(default from FQG_NEW_SESSION_WARMUP_SECONDS)"
        ),
    )
    p_continue.add_argument(
        "--warmup-prompt",
        default=DEFAULT_NEW_SESSION_WARMUP_PROMPT,
        help=(
            "Warmup prompt sent before real task on new/restarted session "
            "(default from FQG_NEW_SESSION_WARMUP_PROMPT)"
        ),
    )
    p_continue.add_argument(
        "--warmup-marker",
        default=DEFAULT_NEW_SESSION_WARMUP_MARKER,
        help=(
            "Expected marker text in tmux output to confirm warmup ready "
            "(default from FQG_NEW_SESSION_WARMUP_MARKER)"
        ),
    )
    p_continue.add_argument(
        "--warmup-fail-close",
        action="store_true",
        default=DEFAULT_NEW_SESSION_WARMUP_FAIL_CLOSE,
        help=(
            "Fail-close when warmup marker is not observed within warmup window "
            "(default from FQG_NEW_SESSION_WARMUP_FAIL_CLOSE)"
        ),
    )
    p_continue.add_argument(
        "--no-warmup-fail-close",
        action="store_false",
        dest="warmup_fail_close",
        help="Disable warmup fail-close even if env default is enabled",
    )
    p_continue.add_argument("--json", action="store_true", help="Print machine-readable JSON output")

    args = parser.parse_args()

    if args.cmd != "continue":
        return 2

    root_dir = Path(args.workspace_root).expanduser().resolve() if args.workspace_root else _default_root_dir()
    codex_home = (
        Path(args.codex_home).expanduser().resolve()
        if args.codex_home.strip()
        else _default_codex_home(root_dir)
    )
    if not codex_home.exists():
        raise SystemExit(f"codex_home not found: {codex_home}")

    session_id = args.session_id.strip() or _load_last_sid(codex_home)
    socket_path = (
        Path(args.tmux_socket_path).expanduser().resolve()
        if args.tmux_socket_path.strip()
        else None
    )

    code, payload = _continue_once(
        root_dir=root_dir,
        codex_home=codex_home,
        session_id=session_id,
        text=(args.text.strip() or DEFAULT_CONTINUE_TEXT),
        verify_seconds=max(1.0, float(args.verify_seconds)),
        session_name_prefix=args.session_name_prefix.strip() or "fqg",
        tmux_socket_path=socket_path,
        warmup_seconds=max(0.0, float(args.warmup_seconds)),
        warmup_prompt=str(args.warmup_prompt or "").strip(),
        warmup_marker=str(args.warmup_marker or "").strip() or DEFAULT_NEW_SESSION_WARMUP_MARKER,
        warmup_fail_close=bool(args.warmup_fail_close),
    )

    payload = {
        "session_id": session_id,
        "codex_home": str(codex_home),
        "workspace_root": str(root_dir),
        "text": args.text.strip() or DEFAULT_CONTINUE_TEXT,
        **payload,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
