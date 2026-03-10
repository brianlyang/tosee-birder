#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass
class Snapshot:
    session_id: str
    rollout_path: Path | None
    state: str
    process_probe_ok: bool
    alive_processes: list[str]
    last_event_at: datetime | None
    last_event_type: str
    last_event_summary: str
    last_agent_message: str
    idle_seconds: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "rollout_path": str(self.rollout_path) if self.rollout_path else "",
            "state": self.state,
            "process_probe_ok": self.process_probe_ok,
            "alive_processes": self.alive_processes,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else "",
            "last_event_type": self.last_event_type,
            "last_event_summary": self.last_event_summary,
            "last_agent_message": self.last_agent_message,
            "idle_seconds": self.idle_seconds,
        }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


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
    return max(matches, key=lambda p: p.stat().st_mtime)


def _read_tail_jsonl(path: Path, max_lines: int = 300) -> list[dict[str, Any]]:
    rows: deque[str] = deque(maxlen=max_lines)
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(line)
    parsed: list[dict[str, Any]] = []
    for line in rows:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            parsed.append(obj)
    return parsed


def _extract_event_summary(event: dict[str, Any]) -> str:
    et = str(event.get("type", "")).strip()
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return et or "<unknown>"

    if et == "event_msg":
        ptype = str(payload.get("type", "")).strip()
        if ptype == "agent_message":
            msg = str(payload.get("message", "")).strip().replace("\n", " ")
            return f"agent_message: {msg[:200]}"
        if ptype == "agent_reasoning":
            txt = str(payload.get("text", "")).strip().replace("\n", " ")
            return f"agent_reasoning: {txt[:200]}"
        if ptype == "task_complete":
            return "task_complete"
        return f"event_msg:{ptype}"

    if et == "response_item":
        ptype = str(payload.get("type", "")).strip()
        if ptype == "function_call":
            name = str(payload.get("name", "")).strip()
            return f"function_call:{name}"
        if ptype == "function_call_output":
            cid = str(payload.get("call_id", "")).strip()
            return f"function_call_output:{cid[:24]}"
        if ptype == "message":
            return "response_message"
        return f"response_item:{ptype}"

    return et or "<unknown>"


def _normalize_message_text(raw: str, *, limit: int = 1200) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    compact = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(compact) > limit:
        return f"{compact[:limit]}..."
    return compact


def _extract_response_item_message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type", "")).strip().lower()
            if btype in {"output_text", "text", "input_text"}:
                txt = block.get("text")
                if not isinstance(txt, str):
                    txt = block.get("content")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
                continue
            if btype == "refusal":
                refusal_text = block.get("refusal")
                if isinstance(refusal_text, str) and refusal_text.strip():
                    parts.append(refusal_text.strip())
    joined = "\n".join(parts).strip()
    if joined:
        return _normalize_message_text(joined)

    for key in ("message", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_message_text(value)
    return ""


def _extract_latest_agent_message(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        et = str(event.get("type", "")).strip()
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue

        if et == "event_msg" and str(payload.get("type", "")).strip() == "agent_message":
            msg = payload.get("message")
            if isinstance(msg, str) and msg.strip():
                return _normalize_message_text(msg)

        if et == "response_item" and str(payload.get("type", "")).strip() == "message":
            extracted = _extract_response_item_message_text(payload)
            if extracted:
                return extracted
    return ""


def _collect_alive_processes(session_id: str) -> tuple[list[str], bool]:
    cmd = ["ps", "-axo", "pid,ppid,tty,stat,etime,pcpu,command"]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, PermissionError, OSError):
        return [], False
    lines: list[str] = []
    for line in out.splitlines():
        if session_id not in line:
            continue
        if "codex" not in line and "feiqiao_guard.wrapper" not in line:
            continue
        if "watch_guarded_session.py" in line or "watch_guarded_session.sh" in line:
            continue
        lines.append(line.strip())
    return lines, True


def _state_from_snapshot(
    *,
    process_probe_ok: bool,
    alive_processes: list[str],
    last_event_type: str,
    idle_seconds: int | None,
    waiting_threshold_seconds: int,
) -> str:
    if process_probe_ok and not alive_processes:
        return "STOPPED"
    if last_event_type == "task_complete":
        return "DONE_WAITING_INPUT"
    if not process_probe_ok:
        if idle_seconds is None:
            return "UNKNOWN"
        if idle_seconds >= waiting_threshold_seconds:
            return "WAITING_OR_STOPPED"
        return "RUNNING_UNKNOWN"
    if idle_seconds is None:
        return "RUNNING"
    if idle_seconds >= waiting_threshold_seconds:
        return "WAITING_INPUT"
    return "RUNNING"


def _build_snapshot(
    *,
    session_id: str,
    codex_home: Path,
    waiting_threshold_seconds: int,
) -> Snapshot:
    rollout_path = _find_rollout_file(codex_home, session_id)
    alive, process_probe_ok = _collect_alive_processes(session_id)

    last_event_at: datetime | None = None
    last_event_type = ""
    last_event_summary = ""
    last_agent_message = ""
    idle_seconds: int | None = None
    if rollout_path and rollout_path.exists():
        events = _read_tail_jsonl(rollout_path, max_lines=400)
        if events:
            last = events[-1]
            last_event_at = _parse_ts(str(last.get("timestamp", "")))
            last_event_type = str(last.get("type", "")).strip()
            payload = last.get("payload")
            if last_event_type == "event_msg" and isinstance(payload, dict):
                semantic_type = str(payload.get("type", "")).strip()
                if semantic_type:
                    last_event_type = semantic_type
            last_event_summary = _extract_event_summary(last)
            last_agent_message = _extract_latest_agent_message(events)
            if last_event_at:
                idle_seconds = max(0, int((_now_utc() - last_event_at).total_seconds()))

    state = _state_from_snapshot(
        process_probe_ok=process_probe_ok,
        alive_processes=alive,
        last_event_type=last_event_type,
        idle_seconds=idle_seconds,
        waiting_threshold_seconds=waiting_threshold_seconds,
    )
    return Snapshot(
        session_id=session_id,
        rollout_path=rollout_path,
        state=state,
        process_probe_ok=process_probe_ok,
        alive_processes=alive,
        last_event_at=last_event_at,
        last_event_type=last_event_type or "<none>",
        last_event_summary=last_event_summary or "<none>",
        last_agent_message=last_agent_message or "",
        idle_seconds=idle_seconds,
    )


def _print_snapshot(s: Snapshot) -> None:
    print("=" * 80)
    print(f"session_id      : {s.session_id}")
    print(f"state           : {s.state}")
    print(f"rollout_path    : {s.rollout_path if s.rollout_path else '<not found>'}")
    print(f"process_probe_ok: {s.process_probe_ok}")
    print(f"alive_processes : {len(s.alive_processes)}")
    for p in s.alive_processes[:3]:
        print(f"  - {p}")
    if len(s.alive_processes) > 3:
        print(f"  - ... ({len(s.alive_processes) - 3} more)")
    print(f"last_event_type : {s.last_event_type}")
    print(f"last_event_at   : {s.last_event_at.isoformat() if s.last_event_at else '<unknown>'}")
    print(f"idle_seconds    : {s.idle_seconds if s.idle_seconds is not None else '<unknown>'}")
    print(f"last_summary    : {s.last_event_summary}")
    print(f"last_reply      : {s.last_agent_message if s.last_agent_message else '<none>'}")


def _post_dingtalk(webhook_url: str, text: str, title: str) -> bool:
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text,
        },
    }
    req = urllib.request.Request(
        url=webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if not (200 <= resp.status < 300):
                return False
            raw = resp.read().decode("utf-8", errors="ignore").strip()
            if not raw:
                return True
            data = json.loads(raw)
            return int(data.get("errcode", 0)) == 0
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False


def _state_cache_file(codex_home: Path, session_id: str) -> Path:
    return codex_home / "session_monitor" / f"{session_id}.state.json"


def _load_previous_state(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("state", "")).strip()
    return ""


def _save_state(path: Path, snapshot: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "updated_at": _now_utc().isoformat(),
                **snapshot.to_dict(),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _continue_cache_file(codex_home: Path, session_id: str) -> Path:
    return codex_home / "session_monitor" / f"{session_id}.continue.json"


def _load_continue_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_continue_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _parse_trigger_states(raw: str) -> set[str]:
    states = {s.strip().upper() for s in raw.split(",") if s.strip()}
    return states or {"WAITING_INPUT", "WAITING_OR_STOPPED", "STOPPED"}


def _should_trigger_auto_continue(
    *,
    snapshot: Snapshot,
    trigger_states: set[str],
    min_idle_seconds: int,
    cooldown_seconds: int,
    now_epoch: float,
    last_attempt_epoch: float,
) -> tuple[bool, str]:
    if snapshot.state.upper() not in trigger_states:
        return False, "state_not_matched"
    if snapshot.idle_seconds is None:
        return False, "idle_unknown"
    if snapshot.idle_seconds < max(0, min_idle_seconds):
        return False, "idle_not_enough"
    if last_attempt_epoch > 0 and (now_epoch - last_attempt_epoch) < max(1, cooldown_seconds):
        return False, "cooldown"
    return True, "trigger"


def _invoke_auto_continue(
    *,
    session_id: str,
    codex_home: Path,
    text: str,
    verify_seconds: float,
) -> dict[str, Any]:
    root_dir = Path(__file__).resolve().parents[1]
    control_script = root_dir / "scripts" / "guarded_session_control.py"
    cmd = [
        sys.executable,
        str(control_script),
        "continue",
        "--session-id",
        session_id,
        "--codex-home",
        str(codex_home),
        "--workspace-root",
        str(root_dir),
        "--text",
        text,
        "--verify-seconds",
        f"{verify_seconds:.2f}",
        "--json",
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "returncode": 127,
            "error": f"spawn_failed:{exc}",
            "payload": {},
            "stdout": "",
            "stderr": "",
            "cmd": cmd,
        }

    payload: dict[str, Any] = {}
    stdout_text = cp.stdout.strip()
    if stdout_text:
        # Control script prints one JSON payload in --json mode.
        last_line = stdout_text.splitlines()[-1]
        try:
            obj = json.loads(last_line)
            if isinstance(obj, dict):
                payload = obj
        except json.JSONDecodeError:
            payload = {}
    ok = bool(payload.get("ok")) and cp.returncode == 0
    return {
        "ok": ok,
        "returncode": cp.returncode,
        "payload": payload,
        "stdout": stdout_text,
        "stderr": cp.stderr.strip(),
        "cmd": cmd,
    }


def _notify_if_changed(snapshot: Snapshot, webhook_url: str, cache_file: Path) -> None:
    prev_state = _load_previous_state(cache_file)
    if prev_state == snapshot.state:
        return
    text = (
        f"### FeiQiao-Guard Session Watchdog\n"
        f"- session_id: `{snapshot.session_id}`\n"
        f"- state: `{prev_state or 'UNKNOWN'} -> {snapshot.state}`\n"
        f"- last_event: `{snapshot.last_event_type}`\n"
        f"- idle_seconds: `{snapshot.idle_seconds}`\n"
        f"- summary: `{snapshot.last_event_summary}`\n"
    )
    _post_dingtalk(
        webhook_url=webhook_url,
        title=f"Session State Changed: {snapshot.state}",
        text=text,
    )
    _save_state(cache_file, snapshot)


def _clear_screen() -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch one guarded codex session and emit active alerts.")
    parser.add_argument("--session-id", default="", help="Target codex session id; default reads last_codex_session_id")
    parser.add_argument("--codex-home", default="", help="Override CODEX_HOME path")
    parser.add_argument("--watch", action="store_true", help="Continuously refresh status")
    parser.add_argument("--interval-seconds", type=float, default=5.0, help="Refresh interval in watch mode")
    parser.add_argument(
        "--waiting-threshold-seconds",
        type=int,
        default=90,
        help="Idle threshold to mark WAITING_INPUT",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of human-readable output")
    parser.add_argument(
        "--notify-dingtalk-webhook",
        default=os.getenv("FQG_DINGTALK_WEBHOOK_URL", ""),
        help="Webhook for state-change push (optional)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Only for watch mode; 0 means infinite",
    )
    parser.add_argument(
        "--auto-continue",
        action="store_true",
        help="Auto-trigger continue/resume when session is stuck waiting",
    )
    parser.add_argument(
        "--auto-continue-text",
        default="继续执行",
        help="Text used by auto-continue when triggering guarded_session_control.py",
    )
    parser.add_argument(
        "--auto-continue-cooldown-seconds",
        type=int,
        default=180,
        help="Cooldown between auto-continue attempts",
    )
    parser.add_argument(
        "--auto-continue-min-idle-seconds",
        type=int,
        default=0,
        help="Min idle seconds to trigger auto-continue (0 means waiting-threshold-seconds)",
    )
    parser.add_argument(
        "--auto-continue-verify-seconds",
        type=float,
        default=8.0,
        help="Verify timeout passed to guarded_session_control.py continue",
    )
    parser.add_argument(
        "--auto-continue-trigger-states",
        default="WAITING_INPUT,WAITING_OR_STOPPED,STOPPED",
        help="Comma-separated states that can trigger auto-continue",
    )
    args = parser.parse_args()

    cli_codex_home = args.codex_home.strip()
    env_codex_home = os.getenv("CODEX_HOME", "").strip()
    if cli_codex_home:
        codex_home = Path(cli_codex_home).expanduser().resolve()
    elif env_codex_home:
        codex_home = Path(env_codex_home).expanduser().resolve()
    else:
        codex_home = (
            Path(__file__).resolve().parents[1] / ".runtime" / "codex_isolated" / "codex_home"
        ).resolve()

    if not codex_home.exists():
        raise SystemExit(f"codex_home not found: {codex_home}")

    session_id = args.session_id.strip() or _load_last_sid(codex_home)
    if not SESSION_ID_RE.match(session_id):
        raise SystemExit(f"invalid --session-id: {session_id!r}")

    webhook = args.notify_dingtalk_webhook.strip()
    cache_file = _state_cache_file(codex_home, session_id)
    continue_cache_path = _continue_cache_file(codex_home, session_id)
    continue_cache = _load_continue_cache(continue_cache_path)
    last_attempt_epoch = float(continue_cache.get("last_attempt_epoch", 0) or 0)
    trigger_states = _parse_trigger_states(args.auto_continue_trigger_states)
    min_idle_seconds = (
        max(0, args.auto_continue_min_idle_seconds)
        if args.auto_continue_min_idle_seconds > 0
        else max(1, args.waiting_threshold_seconds)
    )

    if not args.watch:
        snap = _build_snapshot(
            session_id=session_id,
            codex_home=codex_home,
            waiting_threshold_seconds=max(1, args.waiting_threshold_seconds),
        )
        if args.json:
            print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_snapshot(snap)
        if webhook:
            _notify_if_changed(snap, webhook, cache_file)
        if args.auto_continue:
            now_epoch = time.time()
            should, reason = _should_trigger_auto_continue(
                snapshot=snap,
                trigger_states=trigger_states,
                min_idle_seconds=min_idle_seconds,
                cooldown_seconds=max(1, args.auto_continue_cooldown_seconds),
                now_epoch=now_epoch,
                last_attempt_epoch=last_attempt_epoch,
            )
            if should:
                outcome = _invoke_auto_continue(
                    session_id=session_id,
                    codex_home=codex_home,
                    text=args.auto_continue_text.strip() or "继续执行",
                    verify_seconds=max(1.0, args.auto_continue_verify_seconds),
                )
                last_attempt_epoch = now_epoch
                continue_cache = {
                    "updated_at": _now_utc().isoformat(),
                    "last_attempt_epoch": last_attempt_epoch,
                    "last_attempt_reason": reason,
                    "last_outcome": outcome,
                }
                _save_continue_cache(continue_cache_path, continue_cache)
                if not args.json:
                    print("\nauto_continue:")
                    print(json.dumps(outcome, ensure_ascii=False, indent=2))
            elif not args.json:
                print(f"\nauto_continue: skipped ({reason})")
        return 0

    iteration = 0
    while True:
        iteration += 1
        snap = _build_snapshot(
            session_id=session_id,
            codex_home=codex_home,
            waiting_threshold_seconds=max(1, args.waiting_threshold_seconds),
        )
        if args.json:
            print(json.dumps(snap.to_dict(), ensure_ascii=False))
        else:
            _clear_screen()
            _print_snapshot(snap)
            print(
                "\nwatch_mode=true  "
                f"interval={args.interval_seconds}s  "
                f"max_iterations={args.max_iterations or 'INF'}"
            )
        if webhook:
            _notify_if_changed(snap, webhook, cache_file)

        if args.auto_continue:
            now_epoch = time.time()
            should, reason = _should_trigger_auto_continue(
                snapshot=snap,
                trigger_states=trigger_states,
                min_idle_seconds=min_idle_seconds,
                cooldown_seconds=max(1, args.auto_continue_cooldown_seconds),
                now_epoch=now_epoch,
                last_attempt_epoch=last_attempt_epoch,
            )
            if should:
                outcome = _invoke_auto_continue(
                    session_id=session_id,
                    codex_home=codex_home,
                    text=args.auto_continue_text.strip() or "继续执行",
                    verify_seconds=max(1.0, args.auto_continue_verify_seconds),
                )
                last_attempt_epoch = now_epoch
                continue_cache = {
                    "updated_at": _now_utc().isoformat(),
                    "last_attempt_epoch": last_attempt_epoch,
                    "last_attempt_reason": reason,
                    "last_outcome": outcome,
                }
                _save_continue_cache(continue_cache_path, continue_cache)
                if args.json:
                    sys.stderr.write(
                        json.dumps(
                            {
                                "auto_continue": {
                                    "triggered": True,
                                    "reason": reason,
                                    "outcome_ok": bool(outcome.get("ok")),
                                }
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                else:
                    print(
                        f"\nauto_continue: triggered reason={reason} ok={bool(outcome.get('ok'))} "
                        f"rc={outcome.get('returncode')}"
                    )
                    if outcome.get("payload"):
                        print(
                            f"  action={outcome['payload'].get('action')} "
                            f"advanced={outcome['payload'].get('rollout_advanced')}"
                        )
            elif not args.json:
                print(f"\nauto_continue: skipped ({reason})")

        if args.max_iterations > 0 and iteration >= args.max_iterations:
            break
        time.sleep(max(0.5, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
