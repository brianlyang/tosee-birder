from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import signal
import sqlite3
import struct
import sys
import termios
import time
import tty
import uuid
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


PROMPT_PATTERNS = [
    re.compile(r"Would you like to run the following command\?", re.IGNORECASE),
    re.compile(r"是否运行以下命令", re.IGNORECASE),
    re.compile(r"Would you like to make the following edits\?", re.IGNORECASE),
    re.compile(r"Would you like to apply (these|the following) changes\?", re.IGNORECASE),
    re.compile(r"是否应用以下修改", re.IGNORECASE),
    re.compile(r"是否进行以下修改", re.IGNORECASE),
]

COMMAND_PROMPT_PATTERNS = [
    re.compile(r"Would you like to run the following command\?", re.IGNORECASE),
    re.compile(r"是否运行以下命令", re.IGNORECASE),
]

EDIT_PROMPT_PATTERNS = [
    re.compile(r"Would you like to make the following edits\?", re.IGNORECASE),
    re.compile(r"Would you like to apply (these|the following) changes\?", re.IGNORECASE),
    re.compile(r"是否应用以下修改", re.IGNORECASE),
    re.compile(r"是否进行以下修改", re.IGNORECASE),
]

OPTION_LINE_PATTERN = re.compile(r"^\s*\d+\.\s")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
COMMON_COMMAND_TOKEN_PATTERN = re.compile(
    r"^(?:sudo\s+)?(bash|sh|python3?|pip3?|uv|pytest|node|npm|npx|pnpm|yarn|"
    r"git|ls|cat|rg|grep|find|sed|awk|cd|mkdir|cp|mv|rm|chmod|chown|"
    r"docker|kubectl|systemctl|tmux|curl|wget|ssh|scp|make|go|cargo)\b",
    re.IGNORECASE,
)
UNPARSED_COMMAND_SENTINEL = "<detected_prompt_without_command>"
UNPARSED_FALLBACK_ACTIONS = {"APPROVAL", "ENTER", "ESC"}
SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
STALL_PATTERNS = [
    re.compile(r"如果你同意", re.IGNORECASE),
    re.compile(r"如果你愿意", re.IGNORECASE),
    re.compile(r"需要我继续吗", re.IGNORECASE),
    re.compile(r"要我继续吗", re.IGNORECASE),
    re.compile(r"Would you like me to", re.IGNORECASE),
    re.compile(r"If you want,? I can", re.IGNORECASE),
    re.compile(r"I can continue if you", re.IGNORECASE),
    re.compile(r"Let me know and I(?:'|’)ll", re.IGNORECASE),
    re.compile(r"Conversation interrupted", re.IGNORECASE),
    re.compile(r"tell the model what to do differently", re.IGNORECASE),
    re.compile(r"Failed to apply patch", re.IGNORECASE),
    re.compile(r"patch rejected by user", re.IGNORECASE),
    re.compile(r"对话已中断", re.IGNORECASE),
    re.compile(r"补丁应用失败", re.IGNORECASE),
    re.compile(r"补丁被拒绝", re.IGNORECASE),
]


@dataclass
class WrapperConfig:
    gateway_base_url: str
    timeout_seconds: int
    poll_interval_seconds: float
    auto_continue_nudge: bool
    continue_nudge_text: str
    continue_nudge_cooldown_seconds: float
    continue_nudge_max_count: int


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _truncate_text(text: str, max_len: int = 180) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= max_len:
        return compact
    return f"{compact[: max_len - 3]}..."


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", text)


def _extract_command(buffer: str) -> str:
    cleaned = _strip_ansi(buffer)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if any(pattern.search(line) for pattern in COMMAND_PROMPT_PATTERNS):
            in_fenced_block = False
            for candidate in lines[idx + 1 : idx + 30]:
                normalized = _normalize_command_candidate(candidate)
                lowered = normalized.lower()
                if normalized.startswith("```"):
                    in_fenced_block = not in_fenced_block
                    continue
                if not normalized:
                    continue
                if OPTION_LINE_PATTERN.match(normalized):
                    continue
                if lowered in {"approve", "reject"}:
                    continue
                if lowered.startswith(("press enter", "to cancel")):
                    continue
                if "approve" in lowered and "reject" in lowered:
                    continue
                if _looks_like_shell_command(normalized):
                    return normalized
                if in_fenced_block and len(normalized) >= 2:
                    return normalized
    return ""


def _normalize_command_candidate(candidate: str) -> str:
    stripped = candidate.strip()
    if stripped.startswith("`") and stripped.endswith("`") and len(stripped) >= 2:
        stripped = stripped.strip("`").strip()
    if stripped.startswith(("$", ">", "•", "-", "*")):
        stripped = stripped[1:].strip()
    return stripped


def _looks_like_shell_command(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if lowered in {"approve", "reject"}:
        return False
    if COMMON_COMMAND_TOKEN_PATTERN.match(text):
        return True
    if re.match(r"^(?:\./|/|~?/)", text):
        return True
    if re.match(r"^[\w./:@-]+\s+[\w./:@-]+", text):
        return True
    return any(token in text for token in ("&&", "||", "|", ";", "$(", "`"))


def _unparsed_command_fallback_action() -> str:
    raw = os.getenv("FQG_UNPARSED_COMMAND_FALLBACK_ACTION", "APPROVAL").strip().upper()
    if raw in UNPARSED_FALLBACK_ACTIONS:
        return raw
    return "APPROVAL"


def _extract_edit_preview(buffer: str) -> list[str]:
    cleaned = _strip_ansi(buffer)
    lines = [line.rstrip() for line in cleaned.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if not any(pattern.search(line) for pattern in EDIT_PROMPT_PATTERNS):
            continue
        previews: list[str] = []
        for candidate in lines[idx + 1 : idx + 40]:
            stripped = candidate.strip()
            lowered = stripped.lower()
            if not stripped:
                continue
            if OPTION_LINE_PATTERN.match(stripped):
                break
            if lowered.startswith("press enter to confirm") or lowered.startswith("press enter"):
                break
            if "to cancel" in lowered:
                break
            if lowered in {"approve", "reject"}:
                continue
            previews.append(_truncate_text(stripped, max_len=160))
            if len(previews) >= 6:
                break
        return previews
    return []


def _detect_prompt_type(buffer: str) -> str | None:
    cleaned = _strip_ansi(buffer)
    if any(pattern.search(cleaned) for pattern in COMMAND_PROMPT_PATTERNS):
        return "command_execution"
    if any(pattern.search(cleaned) for pattern in EDIT_PROMPT_PATTERNS):
        return "edit_application"
    return None


def _should_auto_nudge(buffer: str) -> bool:
    cleaned = _strip_ansi(buffer)
    if _detect_prompt_type(cleaned):
        return False
    return any(pattern.search(cleaned) for pattern in STALL_PATTERNS)


def _extract_approval_payload(buffer: str, prompt_type: str) -> tuple[str, dict]:
    if prompt_type == "edit_application":
        previews = _extract_edit_preview(buffer)
        if previews:
            summary = previews[0]
            joined = "; ".join(previews[:3])
            command = f"apply_edits: {joined}"
        else:
            summary = "<edit_prompt_detected>"
            command = "apply_edits: <edit_prompt_detected>"
        return command, {"prompt_type": prompt_type, "summary": summary, "preview_lines": previews}

    extracted = _extract_command(buffer)
    summary = _truncate_text(extracted or UNPARSED_COMMAND_SENTINEL, max_len=180)
    command = extracted or UNPARSED_COMMAND_SENTINEL
    payload: dict[str, object] = {
        "prompt_type": "command_execution",
        "summary": summary,
        "preview_lines": [summary],
    }
    if not extracted:
        payload["prompt_parse_failed"] = True
        payload["unparsed_fallback_action"] = _unparsed_command_fallback_action()
    return command, payload


def _codex_home_path() -> Path | None:
    codex_home = os.getenv("CODEX_HOME", "").strip()
    if not codex_home:
        return None
    return Path(codex_home).expanduser()


def _session_record_paths() -> tuple[Path, Path] | None:
    codex_home = _codex_home_path()
    if codex_home is None:
        return None
    return (
        codex_home / "last_codex_session_id",
        codex_home / "session_id_history.jsonl",
    )


def _persist_codex_session_id(session_id: str, *, source: str) -> None:
    if not SESSION_ID_PATTERN.match(session_id):
        return
    paths = _session_record_paths()
    if paths is None:
        return
    last_path, history_path = paths
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "codex_session_id": session_id,
        "source": source,
        "cwd": os.getcwd(),
    }
    with suppress(OSError):
        last_path.parent.mkdir(parents=True, exist_ok=True)
        last_path.write_text(f"{session_id}\n", encoding="utf-8")
    with suppress(OSError):
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _extract_explicit_session_id(command_argv: list[str]) -> str | None:
    # Match: codex [opts...] resume <session_id> [prompt...]
    tokens = command_argv[1:] if command_argv else []
    for idx, token in enumerate(tokens):
        if token not in {"resume", "fork"}:
            continue
        for candidate in tokens[idx + 1 :]:
            if candidate.startswith("-"):
                continue
            if SESSION_ID_PATTERN.match(candidate):
                return candidate
            return None
    return None


def _list_recent_thread_ids(*, cwd_filter: str | None, limit: int = 200) -> list[str]:
    codex_home = _codex_home_path()
    if codex_home is None:
        return []
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        return []
    query = (
        "SELECT id FROM threads "
        "WHERE archived = 0 "
        + ("AND cwd = ? " if cwd_filter else "")
        + "ORDER BY updated_at DESC LIMIT ?"
    )
    params: list[object] = [cwd_filter] if cwd_filter else []
    params.append(limit)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
        cur = conn.execute(query, tuple(params))
        rows = cur.fetchall()
    except sqlite3.Error:
        return []
    finally:
        with suppress(Exception):
            conn.close()  # type: ignore[name-defined]
    ids: list[str] = []
    for (sid,) in rows:
        sid_text = str(sid).strip()
        if SESSION_ID_PATTERN.match(sid_text):
            ids.append(sid_text)
    return ids


def _list_rollout_session_ids() -> list[str]:
    codex_home = _codex_home_path()
    if codex_home is None:
        return []
    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        return []

    items: list[tuple[float, str]] = []
    for candidate in sessions_root.rglob("rollout-*.jsonl"):
        m = re.search(r"-([0-9a-fA-F\-]{36})\.jsonl$", candidate.name)
        if not m:
            continue
        sid = m.group(1)
        if not SESSION_ID_PATTERN.match(sid):
            continue
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        items.append((mtime, sid))
    items.sort(key=lambda x: x[0], reverse=True)
    return [sid for _, sid in items]


def _detect_latest_codex_session_id(
    *,
    known_ids: set[str] | None = None,
    prefer_new: bool = False,
    cwd_filter: str | None = None,
) -> str | None:
    rollout_ids = _list_rollout_session_ids()
    if rollout_ids:
        for sid in rollout_ids:
            if prefer_new and known_ids is not None and sid in known_ids:
                continue
            return sid

    thread_ids = _list_recent_thread_ids(cwd_filter=cwd_filter, limit=50)
    if thread_ids:
        for sid in thread_ids:
            if prefer_new and known_ids is not None and sid in known_ids:
                continue
            return sid

    codex_home = _codex_home_path()
    if codex_home is None:
        return None
    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        return None

    candidates: list[tuple[float, Path]] = []
    for candidate in sessions_root.rglob("rollout-*.jsonl"):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, candidate))

    for _, latest_file in sorted(candidates, key=lambda item: item[0], reverse=True):
        m = re.search(r"-([0-9a-fA-F\-]{36})\.jsonl$", latest_file.name)
        sid: str | None = None
        if m:
            sid = m.group(1)
        else:
            try:
                first = latest_file.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            except (OSError, IndexError):
                first = ""
            if first:
                try:
                    row = json.loads(first)
                except json.JSONDecodeError:
                    row = {}
                sid = str((row.get("payload") or {}).get("id", "")).strip()
        if sid and SESSION_ID_PATTERN.match(sid):
            if prefer_new and known_ids is not None and sid in known_ids:
                continue
            return sid
    return None


def _http_json(method: str, url: str, payload: dict | None = None, timeout: int = 10) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url=url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        if not raw:
            return {}
        return json.loads(raw)


def _create_approval(config: WrapperConfig, *, command: str, session_id: str, extra_context: dict) -> dict:
    url = f"{config.gateway_base_url}/v1/approvals"
    payload = {
        "command": command,
        "terminal_session_id": session_id,
        "source": "codex_wrapper",
        "extra_context": extra_context,
    }
    return _http_json("POST", url, payload=payload)


def _poll_status(config: WrapperConfig, request_id: str) -> dict:
    url = f"{config.gateway_base_url}/v1/approvals/{request_id}"
    deadline = time.monotonic() + config.timeout_seconds
    while time.monotonic() < deadline:
        status = _http_json("GET", url, timeout=5)
        if status.get("status") != "PENDING":
            return status
        time.sleep(config.poll_interval_seconds)
    return {
        "request_id": request_id,
        "status": "EXPIRED",
        "terminal_action": "ESC",
        "reason": "wrapper_poll_timeout",
    }


def run_wrapped_command(config: WrapperConfig, command_argv: list[str]) -> int:
    if not command_argv:
        raise ValueError("command_argv is empty")

    wrapper_session_id = str(uuid.uuid4())
    terminal_session_id = wrapper_session_id
    printed_detected_session = False
    last_persisted_session_id: str | None = None
    cwd_filter = os.getcwd()
    known_session_ids = set(_list_recent_thread_ids(cwd_filter=cwd_filter, limit=200))
    known_rollout_ids = set(_list_rollout_session_ids())
    known_session_ids.update(known_rollout_ids)
    explicit_session_id = _extract_explicit_session_id(command_argv)
    if explicit_session_id:
        terminal_session_id = explicit_session_id
        printed_detected_session = True
        _persist_codex_session_id(explicit_session_id, source="argv")
        last_persisted_session_id = explicit_session_id
        with suppress(OSError):
            os.write(
                sys.stderr.fileno(),
                f"[FeiQiao-Guard] codex_session_id={explicit_session_id} (from argv)\n".encode("utf-8"),
            )
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(command_argv[0], command_argv)
        raise SystemExit(127)

    with suppress(OSError):
        os.write(
            sys.stderr.fileno(),
            f"[FeiQiao-Guard] wrapper_session_id={wrapper_session_id}\n".encode("utf-8"),
        )

    def _forward_signal(signum: int, _: object) -> None:
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, _forward_signal)
    signal.signal(signal.SIGTERM, _forward_signal)

    seen_prompt = False
    buf = ""
    exit_code = 0
    stdin_fd = sys.stdin.fileno()
    saved_tty_attrs = None
    nudge_count = 0
    last_nudge_at = 0.0

    def _sync_window_size() -> None:
        if not os.isatty(stdin_fd):
            return
        with suppress(OSError):
            packed = fcntl_ioctl_get_winsz(stdin_fd)
            if packed is not None:
                fcntl_ioctl_set_winsz(fd, packed)

    if os.isatty(stdin_fd):
        with suppress(termios.error):
            saved_tty_attrs = termios.tcgetattr(stdin_fd)
            # Transparent relay mode for full-screen TUIs: forward raw key bytes
            # (including Enter as '\r' and arrow-key escape sequences) unchanged.
            tty.setraw(stdin_fd, termios.TCSANOW)
        _sync_window_size()

    def _handle_sigwinch(_: int, __: object) -> None:
        _sync_window_size()
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGWINCH)

    with suppress(Exception):
        signal.signal(signal.SIGWINCH, _handle_sigwinch)

    try:
        while True:
            ready, _, _ = select.select([fd, stdin_fd], [], [], 0.2)
            if not printed_detected_session:
                detected = _detect_latest_codex_session_id(
                    known_ids=known_session_ids,
                    prefer_new=True,
                    cwd_filter=cwd_filter,
                )
                if detected:
                    terminal_session_id = detected
                    printed_detected_session = True
                    known_session_ids.add(detected)
                    if detected != last_persisted_session_id:
                        _persist_codex_session_id(detected, source="runtime_detect")
                        last_persisted_session_id = detected
                    with suppress(OSError):
                        os.write(
                            sys.stderr.fileno(),
                            (
                                f"[FeiQiao-Guard] codex_session_id={detected} "
                                "(use this with `resume`)\n"
                            ).encode("utf-8"),
                        )
            if stdin_fd in ready:
                try:
                    in_data = os.read(stdin_fd, 1024)
                except OSError:
                    in_data = b""
                if in_data:
                    try:
                        os.write(fd, in_data)
                    except OSError:
                        pass

            if fd in ready:
                try:
                    data = os.read(fd, 1024)
                except OSError:
                    data = b""
                if data:
                    os.write(sys.stdout.fileno(), data)
                    chunk = data.decode("utf-8", errors="ignore")
                    buf = (buf + chunk)[-8000:]
                    prompt_type = _detect_prompt_type(buf)
                    if not seen_prompt and prompt_type:
                        seen_prompt = True
                        detected_codex_session_id = (
                            _detect_latest_codex_session_id(
                                known_ids=known_session_ids,
                                prefer_new=True,
                                cwd_filter=cwd_filter,
                            )
                            or _detect_latest_codex_session_id(
                                known_ids=None,
                                prefer_new=False,
                                cwd_filter=cwd_filter,
                            )
                        )
                        if detected_codex_session_id:
                            terminal_session_id = detected_codex_session_id
                            known_session_ids.add(detected_codex_session_id)
                            if detected_codex_session_id != last_persisted_session_id:
                                _persist_codex_session_id(detected_codex_session_id, source="prompt_detect")
                                last_persisted_session_id = detected_codex_session_id
                            if not printed_detected_session:
                                printed_detected_session = True
                                with suppress(OSError):
                                    os.write(
                                        sys.stderr.fileno(),
                                        (
                                            f"[FeiQiao-Guard] codex_session_id={detected_codex_session_id} "
                                            "(use this with `resume`)\n"
                                        ).encode("utf-8"),
                                    )
                        command_text, extra_context = _extract_approval_payload(buf, prompt_type)
                        extra_context = dict(extra_context)
                        extra_context["wrapper_session_id"] = wrapper_session_id
                        extra_context["terminal_session_id"] = terminal_session_id
                        if detected_codex_session_id:
                            extra_context["codex_session_id"] = detected_codex_session_id
                        try:
                            parse_failed = bool(extra_context.get("prompt_parse_failed", False))
                            fallback_action = str(extra_context.get("unparsed_fallback_action", "")).upper()
                            if parse_failed and fallback_action in {"ENTER", "ESC"}:
                                terminal_action = fallback_action
                                with suppress(OSError):
                                    os.write(
                                        sys.stderr.fileno(),
                                        (
                                            "[FeiQiao-Guard] command_parse_failed "
                                            f"fallback_action={terminal_action}\n"
                                        ).encode("utf-8"),
                                    )
                            else:
                                created = _create_approval(
                                    config,
                                    command=command_text,
                                    session_id=terminal_session_id,
                                    extra_context=extra_context,
                                )
                                status = created.get("status")
                                request_id = created.get("request_id", "")
                                if status == "PENDING":
                                    with suppress(OSError):
                                        os.write(
                                            sys.stderr.fileno(),
                                            (
                                                f"[FeiQiao-Guard] approval pending request_id={request_id}; "
                                                f"polling up to {config.timeout_seconds}s\n"
                                            ).encode("utf-8"),
                                        )
                                    final_state = _poll_status(config, request_id)
                                    terminal_action = final_state.get("terminal_action", "ESC")
                                    with suppress(OSError):
                                        os.write(
                                            sys.stderr.fileno(),
                                            (
                                                "[FeiQiao-Guard] approval resolved "
                                                f"status={final_state.get('status', 'UNKNOWN')} "
                                                f"action={terminal_action}\n"
                                            ).encode("utf-8"),
                                        )
                                else:
                                    terminal_action = "ESC" if status != "APPROVED" else "ENTER"
                        except (urllib.error.URLError, TimeoutError, ValueError):
                            terminal_action = "ESC"

                        key_bytes = b"\r" if terminal_action == "ENTER" else b"\x1b"
                        os.write(fd, key_bytes)
                        buf = ""
                        seen_prompt = False
                    elif (
                        config.auto_continue_nudge
                        and not seen_prompt
                        and nudge_count < config.continue_nudge_max_count
                        and _should_auto_nudge(buf)
                    ):
                        now = time.monotonic()
                        if now - last_nudge_at >= config.continue_nudge_cooldown_seconds:
                            nudge_count += 1
                            last_nudge_at = now
                            nudge = (config.continue_nudge_text.strip() or "继续执行直到任务完成后再汇报结果。") + "\r"
                            os.write(fd, nudge.encode("utf-8"))
                            with suppress(OSError):
                                os.write(
                                    sys.stderr.fileno(),
                                    (
                                        f"[FeiQiao-Guard] auto-nudge sent ({nudge_count}/"
                                        f"{config.continue_nudge_max_count})\n"
                                    ).encode("utf-8"),
                                )
                            buf = ""
                else:
                    break

            child_pid, status = os.waitpid(pid, os.WNOHANG)
            if child_pid == pid:
                if os.WIFEXITED(status):
                    exit_code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    exit_code = 128 + os.WTERMSIG(status)
                break
    finally:
        if saved_tty_attrs is not None and os.isatty(stdin_fd):
            with suppress(termios.error):
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_tty_attrs)

    try:
        os.close(fd)
    except OSError:
        pass
    return exit_code


def fcntl_ioctl_get_winsz(fd: int) -> bytes | None:
    import fcntl

    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    except OSError:
        return None


def fcntl_ioctl_set_winsz(fd: int, packed: bytes) -> None:
    import fcntl

    with suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex CLI approval wrapper for FeiQiao-Guard")
    parser.add_argument("--gateway-base-url", default=os.getenv("FQG_CALLBACK_BASE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("FQG_TIMEOUT_SECONDS", "120")))
    parser.add_argument("--poll-interval-seconds", type=float, default=0.5)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command_argv = args.command
    if command_argv and command_argv[0] == "--":
        command_argv = command_argv[1:]
    if not command_argv:
        raise SystemExit("usage: python -m feiqiao_guard.wrapper -- <command> [args...]")

    config = WrapperConfig(
        gateway_base_url=args.gateway_base_url.rstrip("/"),
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        auto_continue_nudge=_env_bool("FQG_AUTO_CONTINUE_NUDGE", False),
        continue_nudge_text=os.getenv(
            "FQG_CONTINUE_NUDGE_TEXT",
            "继续执行直到任务完成后再汇报结果，不要停在“如果你同意我再继续”这类确认。",
        ),
        continue_nudge_cooldown_seconds=_env_float("FQG_CONTINUE_NUDGE_COOLDOWN_SECONDS", 8.0),
        continue_nudge_max_count=max(0, _env_int("FQG_CONTINUE_NUDGE_MAX_COUNT", 3)),
    )
    code = run_wrapped_command(config, command_argv)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
