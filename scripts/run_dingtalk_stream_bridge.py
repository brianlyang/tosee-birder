#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from urllib.parse import unquote, urlparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tosee_birder.chat_bridge import (  # noqa: E402
    ApprovalCommand,
    BridgePolicy,
    InboundChatMessage,
    LeaderCommandTimeout,
    LeaderCommandClient,
    MessageDedupeStore,
    format_approval_queue_summary,
    format_dispatch_summary,
    format_leader_snapshot_summary,
    format_routes_summary,
    is_route_query_command,
    parse_approval_command,
    parse_csv_values,
)


LOGGER = logging.getLogger("fqg.dingtalk_stream_bridge")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_bool(raw: str, default: bool) -> bool:
    normalized = str(raw or "").strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _build_idle_watchdog_snapshot(
    activity_snapshot: dict[str, Any],
    *,
    now_monotonic: float,
) -> dict[str, Any]:
    started_at = float(activity_snapshot.get("started_at", now_monotonic))
    last_callback_at = float(activity_snapshot.get("last_callback_at", started_at))
    last_inbound_at = float(activity_snapshot.get("last_inbound_at", started_at))
    last_reply_at = float(activity_snapshot.get("last_reply_at", started_at))
    callback_count = int(activity_snapshot.get("callback_count", 0) or 0)
    inbound_count = int(activity_snapshot.get("inbound_count", 0) or 0)
    reply_count = int(activity_snapshot.get("reply_count", 0) or 0)
    last_activity_at = max(last_callback_at, last_inbound_at, last_reply_at)
    # Some older snapshots may not expose counters; timestamps still indicate activity.
    activity_seen = (
        callback_count > 0
        or inbound_count > 0
        or reply_count > 0
        or last_callback_at > started_at + 1e-6
        or last_inbound_at > started_at + 1e-6
        or last_reply_at > started_at + 1e-6
    )
    return {
        "started_at": started_at,
        "uptime_seconds": max(0.0, now_monotonic - started_at),
        "last_callback_at": last_callback_at,
        "last_inbound_at": last_inbound_at,
        "last_reply_at": last_reply_at,
        "last_activity_at": last_activity_at,
        "idle_seconds": max(0.0, now_monotonic - last_activity_at),
        "callback_count": callback_count,
        "inbound_count": inbound_count,
        "reply_count": reply_count,
        "activity_seen": bool(activity_seen),
    }


_DEFAULT_REASONING_MAX_ATTEMPTS = 3
_DEFAULT_REASONING_MANDATORY_FIELDS = [
    "attempt",
    "hypothesis",
    "patch",
    "expected_effect",
    "result",
]
_DEFAULT_REASONING_REQUIRE_NEXT_ACTION = True


def _default_identity_current_task_path(leader_identity_id: str) -> str:
    identity = str(leader_identity_id or "feiqiao-guard-delivery-lead").strip()
    if not identity:
        identity = "feiqiao-guard-delivery-lead"
    return str(ROOT_DIR / ".identity" / identity / "CURRENT_TASK.json")


def _normalize_reasoning_field_name(field: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(field or "").strip().lower()).strip("_")


def _normalize_reasoning_fields(raw_fields: Any) -> list[str]:
    if not isinstance(raw_fields, list):
        return list(_DEFAULT_REASONING_MANDATORY_FIELDS)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_fields:
        key = _normalize_reasoning_field_name(str(item))
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized or list(_DEFAULT_REASONING_MANDATORY_FIELDS)


def _parse_reasoning_fields_csv(raw: str) -> list[str]:
    parts = [part.strip() for part in str(raw or "").split(",")]
    normalized = [_normalize_reasoning_field_name(part) for part in parts if part]
    merged = [item for item in normalized if item]
    if not merged:
        return []
    deduped: list[str] = []
    seen: set[str] = set()
    for item in merged:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _load_reasoning_loop_contract(current_task_path: str) -> dict[str, Any]:
    default_contract: dict[str, Any] = {
        "max_attempts_before_escalation": _DEFAULT_REASONING_MAX_ATTEMPTS,
        "mandatory_fields_per_attempt": list(_DEFAULT_REASONING_MANDATORY_FIELDS),
        "failure_requires_next_action": _DEFAULT_REASONING_REQUIRE_NEXT_ACTION,
    }
    path_value = str(current_task_path or "").strip()
    if not path_value:
        return default_contract
    try:
        payload = json.loads(Path(path_value).expanduser().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default_contract
    if not isinstance(payload, dict):
        return default_contract
    raw_contract = payload.get("reasoning_loop_contract")
    if not isinstance(raw_contract, dict):
        return default_contract

    contract = dict(default_contract)
    raw_max_attempts = raw_contract.get("max_attempts_before_escalation")
    try:
        contract["max_attempts_before_escalation"] = max(0, int(raw_max_attempts))
    except Exception:  # noqa: BLE001
        contract["max_attempts_before_escalation"] = _DEFAULT_REASONING_MAX_ATTEMPTS
    contract["mandatory_fields_per_attempt"] = _normalize_reasoning_fields(
        raw_contract.get("mandatory_fields_per_attempt")
    )
    contract["failure_requires_next_action"] = bool(
        raw_contract.get(
            "failure_requires_next_action",
            _DEFAULT_REASONING_REQUIRE_NEXT_ACTION,
        )
    )
    return contract


def _resolve_reasoning_runtime_config(args: argparse.Namespace) -> argparse.Namespace:
    default_contract_path = _default_identity_current_task_path(
        str(getattr(args, "leader_identity_id", "") or "")
    )
    configured_contract_path = str(
        getattr(args, "identity_current_task_path", "") or ""
    ).strip()
    if not configured_contract_path:
        configured_contract_path = str(
            os.getenv("FQG_BRIDGE_IDENTITY_CURRENT_TASK_PATH", "") or ""
        ).strip()
    if not configured_contract_path:
        configured_contract_path = default_contract_path
    setattr(args, "identity_current_task_path", configured_contract_path)

    contract = _load_reasoning_loop_contract(configured_contract_path)
    contract_max_attempts = max(
        0,
        int(contract.get("max_attempts_before_escalation", _DEFAULT_REASONING_MAX_ATTEMPTS)),
    )
    contract_fields = _normalize_reasoning_fields(
        contract.get("mandatory_fields_per_attempt")
    )
    contract_require_next_action = bool(
        contract.get(
            "failure_requires_next_action",
            _DEFAULT_REASONING_REQUIRE_NEXT_ACTION,
        )
    )

    if getattr(args, "reasoning_loop_on_settled", None) is None:
        raw_loop = os.getenv("FQG_BRIDGE_REASONING_LOOP_ON_SETTLED")
        if raw_loop is None:
            raw_loop = os.getenv("FQG_BRIDGE_FORCE_FINAL_ON_SETTLED")
        setattr(
            args,
            "reasoning_loop_on_settled",
            _parse_bool(raw_loop, True) if raw_loop is not None else True,
        )
    else:
        setattr(args, "reasoning_loop_on_settled", bool(args.reasoning_loop_on_settled))

    if getattr(args, "reasoning_max_attempts", None) is None:
        raw_reasoning_max = os.getenv("FQG_BRIDGE_REASONING_MAX_ATTEMPTS")
        if raw_reasoning_max is None:
            raw_reasoning_max = os.getenv("FQG_BRIDGE_FORCE_FINAL_MAX_ATTEMPTS")
        if raw_reasoning_max is not None:
            try:
                setattr(args, "reasoning_max_attempts", max(0, int(raw_reasoning_max)))
            except Exception:  # noqa: BLE001
                setattr(args, "reasoning_max_attempts", contract_max_attempts)
        else:
            setattr(args, "reasoning_max_attempts", contract_max_attempts)
    else:
        setattr(args, "reasoning_max_attempts", max(0, int(args.reasoning_max_attempts)))

    if getattr(args, "reasoning_min_seconds", None) is None:
        raw_reasoning_min_seconds = os.getenv("FQG_BRIDGE_REASONING_MIN_SECONDS")
        if raw_reasoning_min_seconds is not None:
            try:
                setattr(args, "reasoning_min_seconds", max(1.0, float(raw_reasoning_min_seconds)))
            except Exception:  # noqa: BLE001
                setattr(args, "reasoning_min_seconds", 30.0)
        else:
            setattr(args, "reasoning_min_seconds", 30.0)
    else:
        setattr(args, "reasoning_min_seconds", max(1.0, float(args.reasoning_min_seconds)))

    raw_fields = str(getattr(args, "reasoning_mandatory_fields", "") or "").strip()
    if raw_fields:
        fields = _parse_reasoning_fields_csv(raw_fields)
    else:
        env_fields = str(os.getenv("FQG_BRIDGE_REASONING_MANDATORY_FIELDS", "") or "").strip()
        fields = _parse_reasoning_fields_csv(env_fields) if env_fields else contract_fields
    if not fields:
        fields = list(contract_fields)
    setattr(args, "reasoning_mandatory_fields", fields)

    if getattr(args, "reasoning_require_next_action", None) is None:
        raw_require_next_action = os.getenv("FQG_BRIDGE_REASONING_REQUIRE_NEXT_ACTION")
        if raw_require_next_action is not None:
            setattr(
                args,
                "reasoning_require_next_action",
                _parse_bool(raw_require_next_action, contract_require_next_action),
            )
        else:
            setattr(args, "reasoning_require_next_action", contract_require_next_action)
    else:
        setattr(args, "reasoning_require_next_action", bool(args.reasoning_require_next_action))
    return args


def _define_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "DingTalk Stream bridge: receive mobile chat and dispatch "
            "to /v1/chat/leader/command."
        )
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("FQG_DINGTALK_STREAM_CLIENT_ID", ""),
        help="DingTalk app client id (default: FQG_DINGTALK_STREAM_CLIENT_ID)",
    )
    parser.add_argument(
        "--client-secret",
        default=os.getenv("FQG_DINGTALK_STREAM_CLIENT_SECRET", ""),
        help="DingTalk app client secret (default: FQG_DINGTALK_STREAM_CLIENT_SECRET)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("FQG_BRIDGE_BASE_URL", "http://127.0.0.1:3001"),
        help="FeiQiao-Guard base URL (default: FQG_BRIDGE_BASE_URL or http://127.0.0.1:3001)",
    )
    parser.add_argument(
        "--identity-routes-path",
        default=os.getenv("FQG_IDENTITY_ROUTES_PATH", ""),
        help="Identity routes JSON path used for startup boundary checks",
    )
    parser.add_argument(
        "--leader-identity-id",
        default=os.getenv("FQG_CHAT_LEADER_IDENTITY_ID", "feiqiao-guard-delivery-lead"),
        help="Leader identity id used to inspect route binding at startup",
    )
    parser.add_argument(
        "--required-base-url-prefix",
        default=os.getenv("FQG_BRIDGE_REQUIRED_BASE_URL_PREFIX", ""),
        help="Fail-close if base_url does not start with this prefix",
    )
    parser.add_argument(
        "--required-codex-home-prefix",
        default=os.getenv("FQG_BRIDGE_REQUIRED_CODEX_HOME_PREFIX", ""),
        help="Fail-close if leader codex_home is outside this directory prefix",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("FQG_BRIDGE_TIMEOUT_SECONDS", "120")),
        help="HTTP timeout to FQG gateway (default: 120)",
    )
    parser.add_argument(
        "--allow-user-ids",
        default=os.getenv("FQG_BRIDGE_ALLOW_USER_IDS", ""),
        help="Comma-separated sender user IDs allowlist",
    )
    parser.add_argument(
        "--allow-chat-ids",
        default=os.getenv("FQG_BRIDGE_ALLOW_CHAT_IDS", ""),
        help="Comma-separated chat IDs allowlist",
    )
    parser.add_argument(
        "--trusted-decision-token",
        default=os.getenv("FQG_BRIDGE_TRUSTED_DECISION_TOKEN", ""),
        help=(
            "Trusted decision token used by bridge to approve/reject pending requests "
            "via /v1/approvals/{request_id}/trusted-decision"
        ),
    )
    parser.add_argument(
        "--bridge-approver",
        default=os.getenv("FQG_BRIDGE_APPROVER", "guixianren-bridge"),
        help="Approver label written into approval audit when bot issues approve/reject",
    )
    parser.add_argument(
        "--command-prefixes",
        default=os.getenv("FQG_BRIDGE_COMMAND_PREFIXES", "/run,/cmd"),
        help="Comma-separated command prefixes",
    )
    parser.add_argument(
        "--require-prefix",
        action="store_true",
        default=_env_bool("FQG_BRIDGE_REQUIRE_PREFIX", False),
        help="Require command prefix before dispatch",
    )
    parser.add_argument(
        "--no-require-at",
        action="store_true",
        default=not _env_bool("FQG_BRIDGE_REQUIRE_AT", True),
        help="Disable @bot requirement in group chats",
    )
    parser.add_argument(
        "--auto-collab",
        dest="auto_collab",
        action="store_true",
        default=_env_bool("FQG_BRIDGE_AUTO_COLLAB", True),
        help="Enable auto_collab for leader command dispatch",
    )
    parser.add_argument(
        "--no-auto-collab",
        dest="auto_collab",
        action="store_false",
        help="Disable auto_collab for leader command dispatch",
    )
    parser.add_argument(
        "--verify-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_VERIFY_SECONDS", "6")),
        help="verify_seconds for leader dispatch (default: 6)",
    )
    parser.add_argument(
        "--collab-verify-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_COLLAB_VERIFY_SECONDS", "6")),
        help="collab_verify_seconds (default: 6)",
    )
    parser.add_argument(
        "--dedupe-file",
        default=os.getenv(
            "FQG_BRIDGE_DEDUPE_FILE",
            str(ROOT_DIR / ".runtime" / "dingtalk_stream_dedupe.json"),
        ),
        help="Message dedupe file path",
    )
    parser.add_argument(
        "--dedupe-ttl-seconds",
        type=int,
        default=int(os.getenv("FQG_BRIDGE_DEDUPE_TTL_SECONDS", "86400")),
        help="Dedupe retention window in seconds",
    )
    parser.add_argument(
        "--dedupe-max-items",
        type=int,
        default=int(os.getenv("FQG_BRIDGE_DEDUPE_MAX_ITEMS", "5000")),
        help="Max dedupe records retained",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("FQG_BRIDGE_LOG_LEVEL", "INFO"),
        help="Python logging level",
    )
    parser.add_argument(
        "--followup-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_FOLLOWUP_SECONDS", "8")),
        help="Seconds between progress snapshot pushes after dispatch",
    )
    parser.add_argument(
        "--progress-push-count",
        type=int,
        default=int(os.getenv("FQG_BRIDGE_PROGRESS_PUSH_COUNT", "3")),
        help="How many delayed progress snapshots to push after dispatch",
    )
    parser.add_argument(
        "--strict-progress",
        dest="strict_progress",
        action="store_true",
        default=_env_bool("FQG_BRIDGE_STRICT_PROGRESS", True),
        help="Fail-close when progress_push_count <= 0 (default: true)",
    )
    parser.add_argument(
        "--no-strict-progress",
        dest="strict_progress",
        action="store_false",
        help="Disable strict progress_push_count startup gate",
    )
    parser.add_argument(
        "--completion-wait-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_COMPLETION_WAIT_SECONDS", "45")),
        help="Additional wait window for final result push after progress snapshots",
    )
    parser.add_argument(
        "--completion-poll-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_COMPLETION_POLL_SECONDS", "3")),
        help="Polling interval while waiting for final result push",
    )
    parser.add_argument(
        "--completion-max-wait-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_COMPLETION_MAX_WAIT_SECONDS", "300")),
        help="Hard upper bound for waiting final result push",
    )
    parser.add_argument(
        "--post-timeout-final-wait-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_POST_TIMEOUT_FINAL_WAIT_SECONDS", "900")),
        help=(
            "After dispatch_max_wait_reached is sent, keep monitoring in background "
            "for late final result up to N seconds (0 disables; default: 900)"
        ),
    )
    parser.add_argument(
        "--post-timeout-poll-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_POST_TIMEOUT_POLL_SECONDS", "3")),
        help="Polling interval for post-timeout late-final watcher (default: 3)",
    )
    parser.add_argument(
        "--auto-continue-on-waiting-input",
        dest="auto_continue_on_waiting_input",
        action="store_true",
        default=_env_bool("FQG_BRIDGE_AUTO_CONTINUE_ON_WAITING_INPUT", True),
        help=(
            "When task enters WAITING_INPUT before bridge can deliver result, "
            "auto-send one continue command and keep polling."
        ),
    )
    parser.add_argument(
        "--no-auto-continue-on-waiting-input",
        dest="auto_continue_on_waiting_input",
        action="store_false",
        help="Disable auto-continue on WAITING_INPUT.",
    )
    parser.add_argument(
        "--auto-continue-waiting-input-max-attempts",
        type=int,
        default=int(os.getenv("FQG_BRIDGE_AUTO_CONTINUE_WAITING_INPUT_MAX_ATTEMPTS", "1")),
        help="Max auto-continue attempts after WAITING_INPUT (default: 1)",
    )
    parser.add_argument(
        "--auto-continue-waiting-input-min-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_AUTO_CONTINUE_WAITING_INPUT_MIN_SECONDS", "30")),
        help="Min seconds between WAITING_INPUT auto-continue attempts (default: 30)",
    )
    parser.add_argument(
        "--identity-current-task-path",
        default=str(
            os.getenv(
                "FQG_BRIDGE_IDENTITY_CURRENT_TASK_PATH",
                _default_identity_current_task_path(
                    str(os.getenv("FQG_CHAT_LEADER_IDENTITY_ID", "feiqiao-guard-delivery-lead"))
                ),
            )
            or ""
        ),
        help=(
            "Path to CURRENT_TASK.json for loading reasoning_loop_contract "
            "(default: FQG_BRIDGE_IDENTITY_CURRENT_TASK_PATH or .identity/<leader>/CURRENT_TASK.json)."
        ),
    )
    parser.add_argument(
        "--reasoning-loop-on-settled",
        dest="reasoning_loop_on_settled",
        action="store_true",
        default=None,
        help=(
            "When a task enters WAITING_INPUT, auto-trigger L3 reasoning retry "
            "and continue polling."
        ),
    )
    parser.add_argument(
        "--no-reasoning-loop-on-settled",
        dest="reasoning_loop_on_settled",
        action="store_false",
        help="Disable L3 reasoning retry on WAITING_INPUT.",
    )
    # Backward-compatible aliases; keep hidden in help output.
    parser.add_argument(
        "--force-final-on-settled",
        dest="reasoning_loop_on_settled",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-force-final-on-settled",
        dest="reasoning_loop_on_settled",
        action="store_false",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reasoning-level",
        default=str(os.getenv("FQG_BRIDGE_REASONING_LEVEL", "L3") or "L3"),
        help="Reasoning policy level for settled-loop recovery (default: L3).",
    )
    parser.add_argument(
        "--reasoning-max-attempts",
        type=int,
        default=None,
        help=(
            "Max L3 reasoning-loop attempts before escalation hint "
            "(default: CURRENT_TASK.reasoning_loop_contract or env override)."
        ),
    )
    parser.add_argument(
        "--reasoning-min-seconds",
        type=float,
        default=None,
        help="Min seconds between L3 reasoning-loop attempts (default: env or 30).",
    )
    parser.add_argument(
        "--reasoning-mandatory-fields",
        default="",
        help=(
            "Comma-separated mandatory fields for each reasoning attempt. "
            "Empty means CURRENT_TASK.reasoning_loop_contract."
        ),
    )
    parser.add_argument(
        "--reasoning-require-next-action",
        dest="reasoning_require_next_action",
        action="store_true",
        default=None,
        help="Require NEXT_ACTION in every retry and escalation path.",
    )
    parser.add_argument(
        "--no-reasoning-require-next-action",
        dest="reasoning_require_next_action",
        action="store_false",
        help="Do not enforce NEXT_ACTION in reasoning escalation.",
    )
    parser.add_argument(
        "--force-final-max-attempts",
        type=int,
        dest="reasoning_max_attempts",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reply-retry-attempts",
        type=int,
        default=int(os.getenv("FQG_BRIDGE_REPLY_RETRY_ATTEMPTS", "4")),
        help="Reply send retry attempts when DingTalk session webhook is flaky (default: 4)",
    )
    parser.add_argument(
        "--reply-retry-base-delay-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS", "0.8")),
        help="Base backoff delay between reply retries (default: 0.8)",
    )
    parser.add_argument(
        "--reply-retry-max-delay-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS", "6.0")),
        help="Max backoff delay between reply retries (default: 6.0)",
    )
    parser.add_argument(
        "--enable-identity-refusal-fallback",
        action="store_true",
        default=_env_bool("FQG_BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK", False),
        help="Auto-switch to objective-description retry when identity-refusal text is detected",
    )
    parser.add_argument(
        "--enable-multimodal-reply",
        dest="enable_multimodal_reply",
        action="store_true",
        default=_env_bool("FQG_BRIDGE_ENABLE_MULTIMODAL_REPLY", True),
        help="Enable multimodal return (image/file/audio/video) on final delivery (default: true).",
    )
    parser.add_argument(
        "--no-enable-multimodal-reply",
        dest="enable_multimodal_reply",
        action="store_false",
        help="Disable multimodal return and keep text-only replies.",
    )
    parser.add_argument(
        "--multimodal-reply-max-items",
        type=int,
        default=int(os.getenv("FQG_BRIDGE_MULTIMODAL_REPLY_MAX_ITEMS", "2")),
        help="Max multimodal attachments to return per task (default: 2).",
    )
    parser.add_argument(
        "--activity-idle-restart-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS", "1800")),
        help=(
            "Force process recycle when no inbound/reply activity for N seconds "
            "(0 disables; default 1800)"
        ),
    )
    parser.add_argument(
        "--force-restart-max-uptime-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS", "21600")),
        help=(
            "Force process recycle after max uptime N seconds "
            "(0 disables; default 21600)"
        ),
    )
    parser.add_argument(
        "--watchdog-check-interval-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_WATCHDOG_CHECK_INTERVAL_SECONDS", "15")),
        help="Watchdog check interval in seconds (default: 15)",
    )
    parser.add_argument(
        "--watchdog-grace-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_WATCHDOG_GRACE_SECONDS", "90")),
        help="Startup grace window before watchdog can recycle process (default: 90)",
    )
    parser.add_argument(
        "--heartbeat-file",
        default=os.getenv(
            "FQG_BRIDGE_HEARTBEAT_FILE",
            str(ROOT_DIR / ".runtime" / "local_bridge" / "bridge_heartbeat.json"),
        ),
        help="Heartbeat file path for supervisor/observability",
    )
    parser.add_argument(
        "--heartbeat-write-interval-seconds",
        type=float,
        default=float(os.getenv("FQG_BRIDGE_HEARTBEAT_WRITE_INTERVAL_SECONDS", "5")),
        help="Heartbeat write interval in seconds (default: 5)",
    )
    return parser.parse_args()


def _is_group_message(data: dict[str, Any]) -> bool:
    raw = str(data.get("conversationType", "")).strip().lower()
    return raw in {"2", "group", "group_chat", "groupchat"}


def _is_at_bot(data: dict[str, Any]) -> bool:
    if data.get("isInAtList") is True:
        return True
    at_users = data.get("atUsers")
    if isinstance(at_users, list) and at_users:
        return True
    return False


def _extract_text(data: dict[str, Any], incoming_message: Any) -> str:
    content = ""
    text_obj = getattr(incoming_message, "text", None)
    if text_obj is not None:
        content = str(getattr(text_obj, "content", "") or "")
    if not content:
        maybe_text = data.get("text")
        if isinstance(maybe_text, dict):
            content = str(maybe_text.get("content", "") or "")
    rich_text_content = _extract_rich_text_text(data, incoming_message)
    merged_text = " ".join([part for part in [content.strip(), rich_text_content] if part]).strip()
    normalized = merged_text.strip()
    if normalized:
        return normalized

    image_codes = _extract_image_download_codes(data, incoming_message)
    message_type = _extract_message_type(data, incoming_message)
    if image_codes:
        preview_codes = ",".join(image_codes[:3])
        if message_type in {"picture", "image", "richtext"}:
            return (
                "用户发送了图片消息，请识别图片中的主体内容并用中文简要描述；"
                f"如果无法访问图片请明确说明。download_codes={preview_codes}"
            )
        return (
            f"用户发送了附件消息(type={message_type or 'unknown'})，"
            f"download_codes={preview_codes}。请优先读取附件内容后再回答。"
        )
    return ""


def _extract_message_type(data: dict[str, Any], incoming_message: Any) -> str:
    raw = (
        data.get("msgtype")
        or data.get("msgType")
        or data.get("messageType")
        or getattr(incoming_message, "message_type", "")
        or ""
    )
    return str(raw).strip().lower()


def _extract_image_download_codes(data: dict[str, Any], incoming_message: Any) -> list[str]:
    codes: list[str] = []

    image_getter = getattr(incoming_message, "get_image_list", None)
    if callable(image_getter):
        try:
            raw_codes = image_getter() or []
        except Exception:  # noqa: BLE001
            raw_codes = []
        for raw in raw_codes:
            code = str(raw or "").strip()
            if code:
                codes.append(code)

    content = data.get("content")
    if isinstance(content, dict):
        maybe_code = str(content.get("downloadCode", "") or "").strip()
        if maybe_code:
            codes.append(maybe_code)
        rich_text = content.get("richText")
        if isinstance(rich_text, list):
            for item in rich_text:
                if not isinstance(item, dict):
                    continue
                item_code = str(item.get("downloadCode", "") or "").strip()
                if item_code:
                    codes.append(item_code)

    unique: list[str] = []
    seen: set[str] = set()
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        unique.append(code)
    return unique


def _extract_rich_text_text(data: dict[str, Any], incoming_message: Any) -> str:
    texts: list[str] = []

    rich_obj = getattr(incoming_message, "rich_text_content", None)
    rich_list = getattr(rich_obj, "rich_text_list", None)
    if isinstance(rich_list, list):
        for item in rich_list:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "") or "").strip()
            if text:
                texts.append(text)

    content = data.get("content")
    if isinstance(content, dict):
        rich_data = content.get("richText")
        if isinstance(rich_data, list):
            for item in rich_data:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "") or "").strip()
                if text:
                    texts.append(text)

    unique: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return " ".join(unique).strip()


def _fetch_dingtalk_access_token(*, client_id: str, client_secret: str) -> str | None:
    query = (
        f"https://oapi.dingtalk.com/gettoken?appkey={client_id.strip()}&appsecret={client_secret.strip()}"
    )
    req = urllib.request.Request(query, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    token = str(payload.get("access_token", "") or "").strip()
    return token or None


def _resolve_message_file_download_urls(
    *,
    client_id: str,
    client_secret: str,
    robot_code: str,
    download_codes: list[str],
) -> list[str]:
    if not download_codes:
        return []
    token = _fetch_dingtalk_access_token(client_id=client_id, client_secret=client_secret)
    if not token:
        return []

    urls: list[str] = []
    for code in download_codes[:3]:
        payload = {
            "downloadCode": code,
            "robotCode": robot_code or client_id,
        }
        req = urllib.request.Request(
            url="https://api.dingtalk.com/v1.0/robot/messageFiles/download",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-acs-dingtalk-access-token": token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
        except (urllib.error.URLError, TimeoutError, OSError):
            continue

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            continue
        download_url = str(result.get("downloadUrl", "") or "").strip()
        if download_url:
            urls.append(download_url)
    return urls


def _infer_attachment_kind_from_name(*, name: str, content_type: str = "") -> str:
    value = str(name or "").strip().lower()
    ctype = str(content_type or "").strip().lower()
    suffix = Path(value).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS or ctype.startswith("image/"):
        return "image"
    if suffix in _AUDIO_EXTENSIONS or ctype.startswith("audio/"):
        return "audio"
    if suffix in _VIDEO_EXTENSIONS or ctype.startswith("video/"):
        return "video"
    return "file"


def _cache_download_urls_to_local_attachments(
    *,
    download_urls: list[str],
    msg_id: str,
    max_items: int = 3,
) -> list[dict[str, str]]:
    cached: list[dict[str, str]] = []
    if not download_urls:
        return cached

    cache_root = ROOT_DIR / ".runtime" / "dingtalk_stream_images"
    cache_root.mkdir(parents=True, exist_ok=True)
    safe_msg_id = re.sub(r"[^0-9A-Za-z._-]+", "_", str(msg_id or "").strip()) or "msg"

    for idx, raw_url in enumerate(download_urls[: max(1, int(max_items))], start=1):
        url = str(raw_url or "").strip()
        if not url:
            continue
        req = urllib.request.Request(url=url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                payload = resp.read()
                content_type = str(resp.headers.get("Content-Type", "") or "").split(";")[0].strip()
        except (urllib.error.URLError, TimeoutError, OSError):
            continue

        if not payload:
            continue

        parsed = urlparse(url)
        path_name = Path(unquote(parsed.path or "")).name
        ext = mimetypes.guess_extension(content_type) if content_type else None
        if not ext and path_name:
            ext = Path(path_name).suffix
        if not ext:
            ext = ".bin"
        if ext == ".jpe":
            ext = ".jpg"
        kind = _infer_attachment_kind_from_name(
            name=(path_name or f"download_{idx}{ext}"),
            content_type=content_type,
        )
        file_path = cache_root / f"{safe_msg_id}_{idx}{ext}"
        try:
            file_path.write_bytes(payload)
        except OSError:
            continue
        cached.append(
            {
                "url": url,
                "local_path": str(file_path),
                "kind": kind,
                "content_type": content_type,
            }
        )
    return cached


def _resolve_image_download_urls(
    *,
    client_id: str,
    client_secret: str,
    robot_code: str,
    download_codes: list[str],
) -> list[str]:
    # Backward-compatible alias used by existing tests.
    return _resolve_message_file_download_urls(
        client_id=client_id,
        client_secret=client_secret,
        robot_code=robot_code,
        download_codes=download_codes,
    )


def _cache_image_urls_to_local_files(
    *,
    image_urls: list[str],
    msg_id: str,
    max_images: int = 3,
) -> list[str]:
    # Backward-compatible alias used by existing tests.
    cached = _cache_download_urls_to_local_attachments(
        download_urls=image_urls,
        msg_id=msg_id,
        max_items=max_images,
    )
    return [str(item.get("local_path", "")).strip() for item in cached if str(item.get("local_path", "")).strip()]


def _build_inbound_message(data: dict[str, Any], incoming_message: Any) -> InboundChatMessage:
    msg_id = str(
        data.get("msgId")
        or data.get("messageId")
        or getattr(incoming_message, "message_id", "")
        or ""
    ).strip()
    sender_id = str(
        data.get("senderStaffId")
        or data.get("senderId")
        or getattr(incoming_message, "sender_staff_id", "")
        or ""
    ).strip()
    chat_id = str(
        data.get("conversationId") or getattr(incoming_message, "conversation_id", "") or ""
    ).strip()
    return InboundChatMessage(
        msg_id=msg_id,
        text=_extract_text(data, incoming_message),
        sender_id=sender_id,
        chat_id=chat_id,
        is_group=_is_group_message(data),
        is_at_bot=_is_at_bot(data),
    )


def _reject_text(reason: str) -> str:
    mapping = {
        "message_empty": "消息为空，未触发执行。",
        "sender_not_allowed": "发送人不在白名单，已拒绝。",
        "chat_not_allowed": "当前会话不在白名单，已拒绝。",
        "at_required": "群聊请先 @机器人 再下发指令。",
        "command_prefix_required": "请使用命令前缀（如 /run）。",
        "command_empty": "命令为空，未触发执行。",
    }
    return mapping.get(reason, f"消息未受理: {reason}")


def _build_metadata(
    data: dict[str, Any],
    inbound: InboundChatMessage,
    incoming_message: Any,
) -> dict[str, Any]:
    message_type = _extract_message_type(data, incoming_message)
    image_download_codes = _extract_image_download_codes(data, incoming_message)
    attachment_kind = (
        "image"
        if message_type in {"picture", "image", "richtext"}
        else ("binary" if image_download_codes else "none")
    )
    return {
        "channel": "dingtalk_stream",
        "msg_id": inbound.msg_id,
        "sender_id": inbound.sender_id,
        "chat_id": inbound.chat_id,
        "is_group": inbound.is_group,
        "is_at_bot": inbound.is_at_bot,
        "message_type": message_type,
        "image_download_codes": image_download_codes[:8],
        "download_codes": image_download_codes[:8],
        "attachment_kind": attachment_kind,
        "robot_code": str(data.get("robotCode") or getattr(incoming_message, "robot_code", "") or ""),
        "conversation_type": data.get("conversationType"),
        "received_ts": int(time.time()),
        "original_text_preview": inbound.text[:240],
    }


_TERMINAL_STATES = {
    "DONE_WAITING_INPUT",
    "WAITING_INPUT",
    "STOPPED",
    "FAILED",
    "ERROR",
    "SNAPSHOT_ERROR",
}


_IDENTITY_REFUSAL_KEYWORDS = (
    "不能执行这个请求",
    "不能帮你通过图片识别",
    "不能代调接口",
    "真人身份",
    "女明星",
    "身份识别",
)

_FINAL_DELIVERABLE_MARKERS = (
    "FINAL_ANSWER:",
    "FINAL_RESULT:",
    "最终答案：",
    "可交付结果：",
)

_ATTACHMENT_OUTPUT_PREFIX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*ATTACHMENT_PATH\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "file"),
    (re.compile(r"^\s*ATTACHMENT_URL\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "file"),
    (re.compile(r"^\s*IMAGE_PATH\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "image"),
    (re.compile(r"^\s*IMAGE_URL\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "image"),
    (re.compile(r"^\s*FILE_PATH\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "file"),
    (re.compile(r"^\s*FILE_URL\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "file"),
    (re.compile(r"^\s*AUDIO_PATH\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "audio"),
    (re.compile(r"^\s*AUDIO_URL\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "audio"),
    (re.compile(r"^\s*VOICE_PATH\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "audio"),
    (re.compile(r"^\s*VOICE_URL\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "audio"),
    (re.compile(r"^\s*VIDEO_PATH\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "video"),
    (re.compile(r"^\s*VIDEO_URL\s*[:：]\s*(.+?)\s*$", re.IGNORECASE), "video"),
)

_URL_PATTERN = re.compile(r"https?://[^\s<>()\"'`]+")
_MARKDOWN_IMAGE_SOURCE_PATTERN = re.compile(r"!\[[^\]]*]\(([^)\s]+)\)")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+]\(([^)\s]+)\)")

_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".heic",
    ".heif",
    ".svg",
}
_AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".flac",
    ".amr",
    ".opus",
}
_VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".wmv",
    ".webm",
    ".m4v",
    ".3gp",
}

_DINGTALK_IMAGE_UPLOAD_UNSUPPORTED_EXTENSIONS = {
    ".svg",
}


def _truncate_text(text: str, *, limit: int = 160) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def _normalize_extracted_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    url = url.lstrip("<(`'\"")
    url = url.rstrip(".,);]}>`'\"，。；：！？】）》」』、")
    return url.strip()


def _requires_inline_image_display(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    # 用户明确要求“在钉钉中直接看到图片”时，强制 leader 输出公网 IMAGE_URL。
    must_have = (
        "直接展示",
        "直接显示",
        "钉钉里展示",
        "钉钉里直接",
        "内嵌图片",
        "不要附件",
        "不要下载",
    )
    return any(token in text for token in must_have)


def _reply_result_ok(result: Any) -> bool:
    # dingtalk-stream returns None on HTTP/SSL failures and may return non-zero
    # errcode payloads for webhook delivery failures.
    if result is None:
        return False
    if isinstance(result, dict):
        errcode = result.get("errcode")
        if errcode not in (None, 0, "0"):
            return False
        success = result.get("success")
        if success is False:
            return False
    return True


def _is_stale_chat_reply(*, inbound_msg_id: str, active_msg_id: str) -> bool:
    inbound_value = str(inbound_msg_id or "").strip()
    active_value = str(active_msg_id or "").strip()
    if not inbound_value or not active_value:
        return False
    return inbound_value != active_value


def _build_trace_and_task_ids(msg_id: str) -> tuple[str, str]:
    trace_id = (str(msg_id or "").strip() or f"ts-{int(time.time())}")[-10:]
    # Keep trace_id short for mobile UX, and use uuid nonce to avoid accidental cross-task reuse.
    task_id = f"{trace_id}-{uuid.uuid4().hex[:8]}"
    return trace_id, task_id


def _build_real_question_tag(*, msg_id: str, normalized_message: str) -> str:
    ts = time.strftime("%Y%m%d%H%M%S", time.localtime())
    msg_tail = (str(msg_id or "").strip() or f"ts{int(time.time() * 1000)}")[-6:]
    digest = hashlib.sha1(str(normalized_message or "").encode("utf-8")).hexdigest()[:6]
    compact = re.sub(r"\s+", "", str(normalized_message or ""))
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", compact, flags=re.UNICODE)
    hint = (compact[:8] if compact else "query").lower()
    return f"RQ-{ts}-{msg_tail}-{digest}-{hint}"


def _build_max_wait_snapshot_summary(snapshot: dict[str, Any]) -> str:
    items_raw = snapshot.get("items")
    items = items_raw if isinstance(items_raw, list) else []
    leader_state, leader_reply, leader_last = _extract_leader_outcome(snapshot)

    lines = [
        f"leader_state={leader_state} leader_last={_truncate_text(leader_last, limit=80) or '-'}"
    ]
    if leader_reply:
        lines.append(f"leader_reply={_truncate_text(leader_reply, limit=220)}")

    for raw in items[:2]:
        item = raw if isinstance(raw, dict) else {}
        identity_id = str(item.get("identity_id", "")).strip() or "-"
        state = str(item.get("state", "")).strip() or "UNKNOWN"
        last = _truncate_text(str(item.get("last_event_summary", "")).strip(), limit=60) or "-"
        reply = _truncate_text(str(item.get("last_agent_message", "")).strip(), limit=100) or "-"
        lines.append(f"{identity_id}: state={state}; last={last}; reply={reply}")
    return "\n".join(lines)


def _extract_pane_signal_fallback(response: dict[str, Any]) -> str:
    leader_raw = response.get("leader_result")
    leader = leader_raw if isinstance(leader_raw, dict) else {}
    control_raw = leader.get("control_result")
    control = control_raw if isinstance(control_raw, dict) else {}
    if not bool(control.get("pane_signal_confirmed", False)):
        return ""
    preview = str(control.get("pane_signal_preview", "")).strip()
    if not preview:
        return ""

    filtered: list[str] = []
    for raw in preview.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("›", "◦", "└", "Question ", "Questions ")):
            continue
        if line.startswith(("• Called ", "• Calling ")):
            continue
        if line.startswith("gpt-"):
            continue
        if "tab to add notes" in line.lower():
            continue
        if "answer: approve" in line.lower():
            continue
        filtered.append(line)

    if not filtered:
        return ""
    # Keep concise and focus on the newest terminal lines.
    candidate = "\n".join(filtered[-14:]).strip()
    if len(candidate) > 1200:
        candidate = f"{candidate[-1200:]}"
    return candidate


def _evaluate_dispatch_acceptance(response: dict[str, Any]) -> tuple[bool, str]:
    accepted = bool(response.get("accepted", False))
    if accepted:
        return True, ""

    leader_raw = response.get("leader_result")
    leader = leader_raw if isinstance(leader_raw, dict) else {}
    collab_raw = response.get("collab_result")
    collab = collab_raw if isinstance(collab_raw, dict) else {}
    notes_raw = response.get("orchestration_notes")
    notes = notes_raw if isinstance(notes_raw, list) else []

    fragments: list[str] = ["accepted=false"]
    leader_state = str(leader.get("delivery_state", "")).strip().lower()
    if leader_state:
        fragments.append(f"leader_state={leader_state}")
    elif bool(leader.get("accepted", False)) is False:
        fragments.append("leader_accepted=false")

    collab_error = str(response.get("collab_error", "")).strip()
    if collab_error:
        fragments.append(f"collab_error={_truncate_text(collab_error, limit=96)}")
    elif collab:
        collab_state = str(collab.get("delivery_state", "")).strip().lower()
        if collab_state:
            fragments.append(f"collab_state={collab_state}")
        elif bool(collab.get("accepted", False)) is False:
            fragments.append("collab_accepted=false")

    if notes:
        normalized_notes = [str(item).strip() for item in notes if str(item).strip()]
        if normalized_notes:
            note_preview = ";".join(normalized_notes[:2])
            fragments.append(f"notes={_truncate_text(note_preview, limit=120)}")
    return False, " | ".join(fragments)


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(max(0.0, float(ts)), tz=timezone.utc).isoformat()


def _build_bridge_heartbeat_payload(
    *,
    now_monotonic: float,
    now_epoch: float,
    activity_snapshot: dict[str, Any],
    base_url: str,
) -> dict[str, Any]:
    started_at = float(activity_snapshot.get("started_at", now_monotonic))
    last_callback_at = float(activity_snapshot.get("last_callback_at", started_at))
    last_inbound_at = float(activity_snapshot.get("last_inbound_at", started_at))
    last_reply_at = float(activity_snapshot.get("last_reply_at", started_at))
    uptime_seconds = max(0.0, now_monotonic - started_at)
    callback_age_seconds = max(0.0, now_monotonic - last_callback_at)
    inbound_age_seconds = max(0.0, now_monotonic - last_inbound_at)
    reply_age_seconds = max(0.0, now_monotonic - last_reply_at)

    last_reject_at_epoch = float(activity_snapshot.get("last_reject_at", 0.0) or 0.0)

    return {
        "generated_at": _iso_utc(now_epoch),
        "generated_at_epoch": round(now_epoch, 3),
        "uptime_seconds": round(uptime_seconds, 3),
        "base_url": str(base_url or "").strip(),
        "ages": {
            "callback_age_seconds": round(callback_age_seconds, 3),
            "inbound_age_seconds": round(inbound_age_seconds, 3),
            "reply_age_seconds": round(reply_age_seconds, 3),
        },
        "counters": {
            "callback_count": int(activity_snapshot.get("callback_count", 0) or 0),
            "inbound_count": int(activity_snapshot.get("inbound_count", 0) or 0),
            "reply_count": int(activity_snapshot.get("reply_count", 0) or 0),
            "rejected_count": int(activity_snapshot.get("rejected_count", 0) or 0),
        },
        "last_reject_reason": str(activity_snapshot.get("last_reject_reason", "")).strip(),
        "last_reject_at": _iso_utc(last_reject_at_epoch) if last_reject_at_epoch > 0 else "",
    }


def _snapshot_signature(snapshot: dict[str, Any]) -> str:
    items_raw = snapshot.get("items")
    items = items_raw if isinstance(items_raw, list) else []
    normalized: list[dict[str, Any]] = []
    for raw in items[:8]:
        item = raw if isinstance(raw, dict) else {}
        normalized.append(
            {
                "identity_id": str(item.get("identity_id", "")).strip(),
                "state": str(item.get("state", "")).strip(),
                "last_event_summary": str(item.get("last_event_summary", "")).strip(),
                "last_agent_message": str(item.get("last_agent_message", "")).strip(),
            }
        )
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def _select_leader_item(snapshot: dict[str, Any]) -> dict[str, Any]:
    items_raw = snapshot.get("items")
    items = items_raw if isinstance(items_raw, list) else []
    candidates = [item for item in items if isinstance(item, dict)]
    if not candidates:
        return {}
    for item in candidates:
        identity_id = str(item.get("identity_id", "")).strip().lower()
        if "delivery-lead" in identity_id:
            return item
    for item in candidates:
        identity_id = str(item.get("identity_id", "")).strip().lower()
        if "leader" in identity_id:
            return item
    return candidates[0]


def _extract_leader_outcome(snapshot: dict[str, Any]) -> tuple[str, str, str]:
    leader_item = _select_leader_item(snapshot)
    state = str(leader_item.get("state", "UNKNOWN")).strip().upper() or "UNKNOWN"
    reply = str(leader_item.get("last_agent_message", "")).strip()
    last_summary = str(leader_item.get("last_event_summary", "")).strip()
    return state, reply, last_summary


def _is_terminal_snapshot_state(state: str) -> bool:
    return state.strip().upper() in _TERMINAL_STATES


def _normalize_for_compare(text: str) -> str:
    value = str(text or "").strip().lower()
    if not value:
        return ""
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[，。、“”‘’？！!?,.:;；：\-—_()\[\]{}<>《》/\\|@#%^&*+=~`$]", "", value)
    return value


def _looks_like_user_echo_reply(*, reply: str, user_message: str) -> bool:
    normalized_reply = _normalize_for_compare(reply)
    normalized_user = _normalize_for_compare(user_message)
    if not normalized_reply or not normalized_user:
        return False
    if normalized_reply == normalized_user:
        return True
    # Covers cases where bridge catches terminal state before real result is produced
    # and snapshot still contains the raw user instruction or a thin wrapper around it.
    if normalized_reply.startswith(normalized_user) and len(normalized_reply) <= len(normalized_user) + 24:
        return True
    return False


def _is_final_reply_ready(
    *,
    state: str,
    reply: str,
    last_summary: str,
    user_message: str,
) -> bool:
    normalized_state = state.strip().upper()
    summary = str(last_summary or "").strip().lower()
    text = str(reply or "").strip()
    if normalized_state in {"FAILED", "ERROR", "SNAPSHOT_ERROR", "STOPPED"}:
        return True
    if normalized_state in {"DONE_WAITING_INPUT", "WAITING_INPUT"}:
        if not text:
            return False
        if _looks_like_user_echo_reply(reply=text, user_message=user_message):
            return False
        # Allow explicit final markers to close the turn in WAITING_INPUT state.
        # This keeps interactive behavior while enabling deterministic convergence.
        if any(marker in text for marker in _FINAL_DELIVERABLE_MARKERS):
            return True
        return False
    return False


def _build_continue_current_task_prompt(*, original_message: str) -> str:
    original = str(original_message or "").strip()
    return (
        "继续执行当前任务，并直接输出可交付结果。不要只停在待输入状态。\n"
        "输出格式（必须包含以下三行）：\n"
        "FINAL_ANSWER: <最终可交付答案>\n"
        "EVIDENCE: <关键执行证据或阻断点>\n"
        "NEXT_ACTION: <若已完成写 NONE>\n"
        f"原始用户问题：{original}"
    )


def _is_identity_refusal_reply(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in _IDENTITY_REFUSAL_KEYWORDS)


def _has_image_context(metadata: dict[str, Any]) -> bool:
    attachment_kind = str(metadata.get("attachment_kind", "")).strip().lower()
    if attachment_kind and attachment_kind != "image":
        return False
    raw_message_type = str(metadata.get("message_type", "")).strip().lower()
    if raw_message_type in {"picture", "richtext"}:
        return True
    image_codes = metadata.get("image_download_codes")
    if isinstance(image_codes, list) and any(str(code or "").strip() for code in image_codes):
        return True
    image_urls = metadata.get("image_download_urls")
    if isinstance(image_urls, list) and any(str(url or "").strip() for url in image_urls):
        return True
    image_paths = metadata.get("image_local_paths")
    if isinstance(image_paths, list) and any(str(path or "").strip() for path in image_paths):
        return True
    return False


def _build_l3_reasoning_prompt(
    *,
    original_message: str,
    metadata: dict[str, Any],
    attempt: int,
    max_attempts: int,
    reasoning_level: str,
    mandatory_fields: list[str] | tuple[str, ...],
    require_next_action: bool,
) -> str:
    raw_paths = metadata.get("image_local_paths")
    image_paths = (
        [str(path).strip() for path in raw_paths if str(path).strip()]
        if isinstance(raw_paths, list)
        else []
    )
    raw_urls = metadata.get("image_download_urls")
    image_urls = (
        [str(url).strip() for url in raw_urls if str(url).strip()]
        if isinstance(raw_urls, list)
        else []
    )
    path_block = "\n".join(f"- {path}" for path in image_paths[:3]) if image_paths else "(无本地图片路径)"
    url_block = "\n".join(f"- {url}" for url in image_urls[:3]) if image_urls else "(无可下载图片URL)"
    attempt_idx = max(1, int(attempt))
    attempt_cap = max(1, int(max_attempts))
    field_templates: dict[str, str] = {
        "attempt": "ATTEMPT: <n>/<max>",
        "hypothesis": "HYPOTHESIS: <你判断当前卡住的原因>",
        "patch": "PATCH: <本轮你将执行的修复动作>",
        "expected_effect": "EXPECTED_EFFECT: <预期变化>",
        "result": "RESULT: <本轮实际结果，必须可验证>",
    }
    normalized_fields = _normalize_reasoning_fields(list(mandatory_fields))
    required_lines: list[str] = []
    for field in normalized_fields:
        if field in field_templates:
            required_lines.append(field_templates[field])
            continue
        fallback_name = str(field or "").strip().upper()
        fallback_name = fallback_name or "FIELD"
        required_lines.append(f"{fallback_name}: <补充该字段内容>")
    required_block = "\n".join(required_lines)
    next_action_line = (
        "NEXT_ACTION: <若已完成写 NONE；若失败必须给出可执行下一步>"
        if require_next_action
        else "NEXT_ACTION: <若无后续动作写 NONE>"
    )
    reasoning_level_text = str(reasoning_level or "L3").strip() or "L3"
    return (
        f"进入 {reasoning_level_text} 推理循环，请用推理而不是模板化回复收敛问题。\n"
        f"当前回合：{attempt_idx}/{attempt_cap}（超过上限必须给出可执行 next_action）。\n"
        "每次必须显式给出以下字段：\n"
        f"{required_block}\n"
        "然后给交付字段：\n"
        "FINAL_ANSWER: <最终答案>\n"
        "EVIDENCE: <调用证据/阻断证据/关键日志>\n"
        f"{next_action_line}\n"
        f"用户原始问题：{str(original_message or '').strip()}\n"
        f"本地图片路径：\n{path_block}\n"
        f"图片URL：\n{url_block}"
    )


def _build_l3_escalation_prompt(
    *,
    original_message: str,
    metadata: dict[str, Any],
    max_attempts: int,
    require_next_action: bool,
) -> str:
    attempt_cap = max(1, int(max_attempts))
    next_action_line = (
        "NEXT_ACTION: <必须给出可执行下一步；若已完成写 NONE>"
        if require_next_action
        else "NEXT_ACTION: <若无后续动作写 NONE>"
    )
    return (
        "L3 推理循环已达到上限，请停止重复等待输入并立即收敛。\n"
        f"已达上限：{attempt_cap} 次。\n"
        "请输出：\n"
        "FINAL_ANSWER: <最终答案>\n"
        "EVIDENCE: <关键日志/阻断证据>\n"
        f"{next_action_line}\n"
        f"用户原始问题：{str(original_message or '').strip()}\n"
        f"附带上下文：{json.dumps(metadata, ensure_ascii=False)[:800]}"
    )


def _build_force_final_prompt(*, original_message: str, metadata: dict[str, Any]) -> str:
    # Backward-compatible alias for tests/harnesses still importing old name.
    return _build_l3_reasoning_prompt(
        original_message=original_message,
        metadata=metadata,
        attempt=1,
        max_attempts=1,
        reasoning_level="L3",
        mandatory_fields=list(_DEFAULT_REASONING_MANDATORY_FIELDS),
        require_next_action=True,
    )


def _build_safe_image_retry_prompt(
    *,
    original_message: str,
    metadata: dict[str, Any],
) -> str:
    raw_urls = metadata.get("image_download_urls")
    image_urls = (
        [str(url).strip() for url in raw_urls if str(url).strip()]
        if isinstance(raw_urls, list)
        else []
    )
    raw_paths = metadata.get("image_local_paths")
    image_paths = (
        [str(path).strip() for path in raw_paths if str(path).strip()]
        if isinstance(raw_paths, list)
        else []
    )
    url_block = "\n".join(f"- {url}" for url in image_urls[:3]) if image_urls else "(未解析到可下载URL)"
    path_block = "\n".join(f"- {path}" for path in image_paths[:3]) if image_paths else "(未落地本地图片路径)"
    return (
        "请仅做图片客观内容描述，不要识别、确认或推断具体真人身份，也不要提及政策说明。\n"
        "输出要求：\n"
        "1) 主体（人物/物体/场景）\n"
        "2) 视觉要素（服饰、颜色、构图、光线）\n"
        "3) 一句简短结论\n"
        f"用户原始指令：{original_message.strip()}\n"
        f"可下载图片URL：\n{url_block}\n"
        f"本地图片路径：\n{path_block}"
    )


def _extract_attachment_specs_from_reply(text: str) -> list[dict[str, str]]:
    raw = str(text or "")
    if not raw.strip():
        return []
    specs: list[dict[str, str]] = []
    for line in raw.splitlines():
        stripped = str(line or "").strip()
        if not stripped:
            continue
        for pattern, hint_kind in _ATTACHMENT_OUTPUT_PREFIX_PATTERNS:
            matched = pattern.match(stripped)
            if not matched:
                continue
            value = str(matched.group(1) or "").strip().strip("'\"`")
            if not value:
                continue
            lower_value = value.lower()
            item: dict[str, str] = {"kind": hint_kind}
            if lower_value.startswith("http://") or lower_value.startswith("https://"):
                item["url"] = _normalize_extracted_url(value)
            else:
                item["path"] = value
            specs.append(item)
            break

    for matched in _MARKDOWN_IMAGE_SOURCE_PATTERN.findall(raw):
        source = str(matched or "").strip()
        if not source:
            continue
        if source.lower().startswith(("http://", "https://")):
            specs.append({"kind": "image", "url": source})
        else:
            specs.append({"kind": "image", "path": source})
    for matched in _MARKDOWN_LINK_PATTERN.findall(raw):
        source = str(matched or "").strip()
        if not source:
            continue
        lower_source = source.lower()
        if lower_source.startswith(("http://", "https://")):
            kind = _infer_attachment_kind_from_url(source)
            if kind in {"image", "audio", "video"}:
                specs.append({"kind": kind, "url": source})
            continue
        kind = _infer_attachment_kind_from_name(name=source)
        if kind in {"image", "audio", "video", "file"}:
            specs.append({"kind": kind, "path": source})
    return specs


def _extract_urls_from_text(text: str) -> list[str]:
    raw = str(text or "")
    if not raw.strip():
        return []
    urls: list[str] = []
    for matched in _URL_PATTERN.findall(raw):
        url = _normalize_extracted_url(str(matched or ""))
        if url:
            urls.append(url)
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def _infer_attachment_kind_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    name = unquote(parsed.path or "")
    return _infer_attachment_kind_from_name(name=name)


def _infer_attachment_suffix(*, path_value: str = "", url_value: str = "") -> str:
    path_str = str(path_value or "").strip()
    if path_str:
        return Path(path_str).suffix.lower()
    url_str = str(url_value or "").strip()
    if url_str.lower().startswith(("http://", "https://")):
        parsed = urlparse(url_str)
        return Path(unquote(parsed.path or "")).suffix.lower()
    return ""


def _coerce_attachment_kind_for_dingtalk(
    *,
    kind: str,
    path_value: str = "",
    url_value: str = "",
) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized not in {"image", "audio", "video", "file"}:
        normalized = "file"
    if normalized != "image":
        return normalized
    suffix = _infer_attachment_suffix(path_value=path_value, url_value=url_value)
    if suffix in _DINGTALK_IMAGE_UPLOAD_UNSUPPORTED_EXTENSIONS:
        return "file"
    return normalized


def _collect_outbound_attachments(
    *,
    metadata: dict[str, Any],
    final_reply: str,
    max_items: int,
) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []

    def _append(item: dict[str, str]) -> None:
        src = str(item.get("path", "")).strip() or str(item.get("url", "")).strip()
        if not src:
            return
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in {"image", "audio", "video", "file"}:
            kind = "file"
        normalized = {"kind": kind}
        if str(item.get("path", "")).strip():
            normalized["path"] = str(item.get("path", "")).strip()
        if str(item.get("url", "")).strip():
            normalized["url"] = str(item.get("url", "")).strip()
        attachments.append(normalized)

    for spec in _extract_attachment_specs_from_reply(final_reply):
        _append(spec)

    raw_attachment_paths = metadata.get("attachment_local_paths")
    attachment_paths = (
        [str(path).strip() for path in raw_attachment_paths if str(path).strip()]
        if isinstance(raw_attachment_paths, list)
        else []
    )
    raw_attachment_urls = metadata.get("attachment_download_urls")
    attachment_urls = (
        [str(url).strip() for url in raw_attachment_urls if str(url).strip()]
        if isinstance(raw_attachment_urls, list)
        else []
    )

    if not attachment_urls:
        raw_image_urls = metadata.get("image_download_urls")
        if isinstance(raw_image_urls, list):
            attachment_urls = [str(url).strip() for url in raw_image_urls if str(url).strip()]
    if not attachment_paths:
        raw_image_paths = metadata.get("image_local_paths")
        if isinstance(raw_image_paths, list):
            attachment_paths = [str(path).strip() for path in raw_image_paths if str(path).strip()]

    attachment_kind = str(metadata.get("attachment_kind", "")).strip().lower()

    for path in attachment_paths:
        kind = _infer_attachment_kind_from_name(name=path)
        if attachment_kind == "image":
            kind = "image"
        _append({"kind": kind, "path": path})
    for url in attachment_urls:
        kind = _infer_attachment_kind_from_url(url)
        if attachment_kind == "image":
            kind = "image"
        _append({"kind": kind, "url": url})

    # Parse direct URLs in final reply for extra attachment hints.
    for url in _extract_urls_from_text(final_reply):
        inferred = _infer_attachment_kind_from_url(url)
        if inferred in {"image", "audio", "video"}:
            _append({"kind": inferred, "url": url})

    # Prefer direct URLs (especially image URLs) over local paths so
    # constrained slots (max_items) still keep inline-display candidates first.
    prioritized: list[dict[str, str]] = [
        item
        for _, item in sorted(
            enumerate(attachments),
            key=lambda pair: (
                0
                if pair[1].get("kind") == "image" and str(pair[1].get("url", "")).strip()
                else (
                    1
                    if str(pair[1].get("url", "")).strip()
                    else (2 if pair[1].get("kind") == "image" else 3)
                ),
                pair[0],
            ),
        )
    ]

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in prioritized:
        key = f"{item.get('kind', 'file')}::{item.get('path', '') or item.get('url', '')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max(1, int(max_items)):
            break
    return deduped


def _path_within_prefix(path_value: str, prefix_value: str) -> bool:
    try:
        path = Path(path_value).expanduser().resolve()
        prefix = Path(prefix_value).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return False
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


def _compute_startup_guard_issues(args: argparse.Namespace) -> list[str]:
    issues: list[str] = []

    base_url = str(getattr(args, "base_url", "") or "").strip()
    required_base_prefix = str(getattr(args, "required_base_url_prefix", "") or "").strip()
    if required_base_prefix and not base_url.startswith(required_base_prefix):
        issues.append(
            f"base_url_prefix_mismatch: base_url={base_url};required_prefix={required_base_prefix}"
        )

    strict_progress = bool(getattr(args, "strict_progress", False))
    progress_push_count = int(getattr(args, "progress_push_count", 0))
    if strict_progress and progress_push_count <= 0:
        issues.append(
            f"progress_push_count_invalid: progress_push_count={progress_push_count};requires>=1"
        )

    routes_path_raw = str(getattr(args, "identity_routes_path", "") or "").strip()
    required_codex_prefix = str(getattr(args, "required_codex_home_prefix", "") or "").strip()
    leader_identity = str(getattr(args, "leader_identity_id", "") or "").strip()
    if routes_path_raw:
        routes_path = Path(routes_path_raw).expanduser()
        if not routes_path.exists():
            issues.append(f"identity_routes_missing: path={routes_path}")
        else:
            try:
                payload = json.loads(routes_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                issues.append(f"identity_routes_unreadable: path={routes_path};err={type(exc).__name__}")
            else:
                identities = payload.get("identities") if isinstance(payload, dict) else None
                if not isinstance(identities, dict):
                    issues.append(f"identity_routes_invalid: path={routes_path};reason=identities_not_object")
                elif leader_identity:
                    leader_route = identities.get(leader_identity)
                    if not isinstance(leader_route, dict):
                        issues.append(
                            f"leader_route_missing: identity_id={leader_identity};path={routes_path}"
                        )
                    else:
                        codex_home = str(leader_route.get("codex_home", "") or "").strip()
                        if not codex_home:
                            issues.append(
                                f"leader_codex_home_missing: identity_id={leader_identity};path={routes_path}"
                            )
                        elif required_codex_prefix and (
                            not _path_within_prefix(codex_home, required_codex_prefix)
                        ):
                            issues.append(
                                "leader_codex_home_prefix_mismatch: "
                                f"identity_id={leader_identity};codex_home={codex_home};"
                                f"required_prefix={required_codex_prefix}"
                            )
    elif required_codex_prefix:
        issues.append("identity_routes_required_when_codex_prefix_enforced")

    return issues


def _apply_missing_arg_defaults(args: argparse.Namespace) -> argparse.Namespace:
    # Keep compatibility with simulation/replay harnesses that monkeypatch _define_args
    # and may not include newly added CLI fields.
    defaults: dict[str, object] = {
        "post_timeout_final_wait_seconds": 900.0,
        "post_timeout_poll_seconds": 3.0,
        "completion_wait_seconds": 45.0,
        "completion_poll_seconds": 3.0,
        "completion_max_wait_seconds": 300.0,
        "reply_retry_attempts": 4,
        "reply_retry_base_delay_seconds": 0.8,
        "reply_retry_max_delay_seconds": 6.0,
        "enable_identity_refusal_fallback": False,
        "enable_multimodal_reply": True,
        "multimodal_reply_max_items": 2,
        "activity_idle_restart_seconds": 0.0,
        "force_restart_max_uptime_seconds": 0.0,
        "watchdog_check_interval_seconds": 15.0,
        "watchdog_grace_seconds": 90.0,
        "heartbeat_file": str(ROOT_DIR / ".runtime" / "local_bridge" / "bridge_heartbeat.json"),
        "heartbeat_write_interval_seconds": 5.0,
        "trusted_decision_token": "",
        "bridge_approver": "guixianren-bridge",
        "identity_routes_path": "",
        "leader_identity_id": "feiqiao-guard-delivery-lead",
        "identity_current_task_path": "",
        "required_base_url_prefix": "",
        "required_codex_home_prefix": "",
        "strict_progress": True,
        "reasoning_loop_on_settled": None,
        "reasoning_level": "L3",
        "reasoning_max_attempts": None,
        "reasoning_min_seconds": None,
        "reasoning_mandatory_fields": "",
        "reasoning_require_next_action": None,
    }
    for key, value in defaults.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    return args


def main() -> int:
    args = _resolve_reasoning_runtime_config(
        _apply_missing_arg_defaults(_define_args())
    )
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.client_id.strip() or not args.client_secret.strip():
        print("missing_client_credentials", file=sys.stderr)
        return 2

    try:
        import dingtalk_stream  # type: ignore
        from dingtalk_stream import AckMessage  # type: ignore
    except ImportError:
        print(
            "dingtalk_stream_not_installed; run: pip install dingtalk-stream",
            file=sys.stderr,
        )
        return 3

    startup_issues = _compute_startup_guard_issues(args)
    if startup_issues:
        for issue in startup_issues:
            print(f"startup_guard_failed:{issue}", file=sys.stderr)
        LOGGER.error("startup_guard_failed issues=%s", "; ".join(startup_issues))
        return 4

    policy = BridgePolicy(
        require_at_on_group=not args.no_require_at,
        require_command_prefix=args.require_prefix,
        allow_user_ids=parse_csv_values(args.allow_user_ids),
        allow_chat_ids=parse_csv_values(args.allow_chat_ids),
        command_prefixes=tuple(
            item.strip() for item in args.command_prefixes.split(",") if item.strip()
        ),
    )
    client = LeaderCommandClient(base_url=args.base_url, timeout_seconds=args.timeout_seconds)
    dedupe = MessageDedupeStore(
        Path(args.dedupe_file),
        ttl_seconds=args.dedupe_ttl_seconds,
        max_items=args.dedupe_max_items,
    )

    class _BridgeHandler(dingtalk_stream.ChatbotHandler):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            super().__init__()
            now = time.monotonic()
            self._activity_lock = threading.Lock()
            self._chat_activity_lock = threading.Lock()
            self._chat_active_msg: dict[str, str] = {}
            self._started_at = now
            self._last_callback_at = now
            self._last_inbound_at = now
            self._last_reply_at = now
            self._callback_count = 0
            self._inbound_count = 0
            self._reply_count = 0
            self._rejected_count = 0
            self._last_reject_reason = ""
            self._last_reject_at_epoch = 0.0

        def _set_active_chat_message(self, chat_id: str, msg_id: str) -> None:
            safe_chat_id = str(chat_id or "").strip()
            safe_msg_id = str(msg_id or "").strip()
            if not safe_chat_id or not safe_msg_id:
                return
            with self._chat_activity_lock:
                self._chat_active_msg[safe_chat_id] = safe_msg_id
                if len(self._chat_active_msg) > 256:
                    oldest_key = next(iter(self._chat_active_msg))
                    self._chat_active_msg.pop(oldest_key, None)

        def _active_chat_message_id(self, chat_id: str) -> str:
            safe_chat_id = str(chat_id or "").strip()
            if not safe_chat_id:
                return ""
            with self._chat_activity_lock:
                return str(self._chat_active_msg.get(safe_chat_id, "")).strip()

        def _is_stale_reply_for_chat(self, inbound: InboundChatMessage | None) -> bool:
            if inbound is None:
                return False
            safe_chat_id = str(inbound.chat_id or "").strip()
            safe_msg_id = str(inbound.msg_id or "").strip()
            if not safe_chat_id or not safe_msg_id:
                return False
            active_msg_id = self._active_chat_message_id(safe_chat_id)
            return _is_stale_chat_reply(inbound_msg_id=safe_msg_id, active_msg_id=active_msg_id)

        def _log_stale_reply_skip(self, *, inbound: InboundChatMessage | None, tag: str, channel: str) -> None:
            msg_id = "" if inbound is None else str(inbound.msg_id or "").strip()
            chat_id = "" if inbound is None else str(inbound.chat_id or "").strip()
            active_msg_id = self._active_chat_message_id(chat_id)
            LOGGER.info(
                "reply_skipped_stale tag=%s channel=%s msg_id=%s active_msg_id=%s chat_id=%s",
                tag,
                channel,
                msg_id,
                active_msg_id,
                chat_id,
            )

        def _mark_callback(self) -> None:
            with self._activity_lock:
                self._last_callback_at = time.monotonic()
                self._callback_count += 1

        def _mark_inbound(self) -> None:
            with self._activity_lock:
                self._last_inbound_at = time.monotonic()
                self._inbound_count += 1

        def _mark_reply(self) -> None:
            with self._activity_lock:
                self._last_reply_at = time.monotonic()
                self._reply_count += 1

        def _mark_rejected(self, reason: str) -> None:
            with self._activity_lock:
                self._rejected_count += 1
                self._last_reject_reason = str(reason or "").strip()
                self._last_reject_at_epoch = time.time()

        def activity_snapshot(self) -> dict[str, Any]:
            with self._activity_lock:
                return {
                    "started_at": self._started_at,
                    "last_callback_at": self._last_callback_at,
                    "last_inbound_at": self._last_inbound_at,
                    "last_reply_at": self._last_reply_at,
                    "callback_count": self._callback_count,
                    "inbound_count": self._inbound_count,
                    "reply_count": self._reply_count,
                    "rejected_count": self._rejected_count,
                    "last_reject_reason": self._last_reject_reason,
                    "last_reject_at": self._last_reject_at_epoch,
                }

        def _safe_reply(self, *, incoming, text: str, inbound: InboundChatMessage | None, tag: str) -> bool:  # noqa: ANN001
            if self._is_stale_reply_for_chat(inbound):
                self._log_stale_reply_skip(inbound=inbound, tag=tag, channel="text")
                return False
            attempts = max(1, int(args.reply_retry_attempts))
            base_delay = max(0.1, float(args.reply_retry_base_delay_seconds))
            max_delay = max(base_delay, float(args.reply_retry_max_delay_seconds))
            msg_id = "" if inbound is None else inbound.msg_id
            chat_id = "" if inbound is None else inbound.chat_id
            sender_id = "" if inbound is None else inbound.sender_id
            text_preview = str(text).replace("\n", " ")[:140]
            last_err = ""
            for attempt in range(1, attempts + 1):
                try:
                    result = self.reply_text(text, incoming)
                    if _reply_result_ok(result):
                        self._mark_reply()
                        _maybe_write_heartbeat(force=True)
                        LOGGER.info(
                            (
                                "reply_sent tag=%s msg_id=%s chat_id=%s sender_id=%s "
                                "attempt=%s/%s text_preview=%s"
                            ),
                            tag,
                            msg_id,
                            chat_id,
                            sender_id,
                            attempt,
                            attempts,
                            text_preview,
                        )
                        return True
                    last_err = _truncate_text(str(result), limit=240) or "reply_result_none"
                    LOGGER.warning(
                        (
                            "reply_retry_needed tag=%s msg_id=%s chat_id=%s sender_id=%s "
                            "attempt=%s/%s reason=invalid_result result_preview=%s"
                        ),
                        tag,
                        msg_id,
                        chat_id,
                        sender_id,
                        attempt,
                        attempts,
                        last_err,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{type(exc).__name__}:{exc}"
                    LOGGER.warning(
                        (
                            "reply_retry_needed tag=%s msg_id=%s chat_id=%s sender_id=%s "
                            "attempt=%s/%s reason=exception err=%s"
                        ),
                        tag,
                        msg_id,
                        chat_id,
                        sender_id,
                        attempt,
                        attempts,
                        type(exc).__name__,
                    )
                if attempt < attempts:
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    time.sleep(delay)
            LOGGER.error(
                (
                    "reply_give_up tag=%s msg_id=%s chat_id=%s sender_id=%s "
                    "attempts=%s last_err=%s text_preview=%s"
                ),
                tag,
                msg_id,
                chat_id,
                sender_id,
                attempts,
                _truncate_text(last_err, limit=300),
                text_preview,
            )
            return False

        def _safe_reply_markdown(
            self,
            *,
            incoming,
            title: str,
            text: str,
            inbound: InboundChatMessage | None,
            tag: str,
        ) -> bool:  # noqa: ANN001
            if self._is_stale_reply_for_chat(inbound):
                self._log_stale_reply_skip(inbound=inbound, tag=tag, channel="markdown")
                return False
            attempts = max(1, int(args.reply_retry_attempts))
            base_delay = max(0.1, float(args.reply_retry_base_delay_seconds))
            max_delay = max(base_delay, float(args.reply_retry_max_delay_seconds))
            msg_id = "" if inbound is None else inbound.msg_id
            chat_id = "" if inbound is None else inbound.chat_id
            sender_id = "" if inbound is None else inbound.sender_id
            title_preview = _truncate_text(title, limit=80)
            last_err = ""
            for attempt in range(1, attempts + 1):
                try:
                    result = self.reply_markdown(title, text, incoming)
                    if _reply_result_ok(result):
                        self._mark_reply()
                        _maybe_write_heartbeat(force=True)
                        LOGGER.info(
                            (
                                "reply_markdown_sent tag=%s msg_id=%s chat_id=%s sender_id=%s "
                                "attempt=%s/%s title=%s"
                            ),
                            tag,
                            msg_id,
                            chat_id,
                            sender_id,
                            attempt,
                            attempts,
                            title_preview,
                        )
                        return True
                    last_err = _truncate_text(str(result), limit=240) or "reply_result_none"
                    LOGGER.warning(
                        (
                            "reply_markdown_retry_needed tag=%s msg_id=%s chat_id=%s sender_id=%s "
                            "attempt=%s/%s reason=invalid_result result_preview=%s"
                        ),
                        tag,
                        msg_id,
                        chat_id,
                        sender_id,
                        attempt,
                        attempts,
                        last_err,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{type(exc).__name__}:{exc}"
                    LOGGER.warning(
                        (
                            "reply_markdown_retry_needed tag=%s msg_id=%s chat_id=%s sender_id=%s "
                            "attempt=%s/%s reason=exception err=%s"
                        ),
                        tag,
                        msg_id,
                        chat_id,
                        sender_id,
                        attempt,
                        attempts,
                        type(exc).__name__,
                    )
                if attempt < attempts:
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    time.sleep(delay)
            LOGGER.error(
                (
                    "reply_markdown_give_up tag=%s msg_id=%s chat_id=%s sender_id=%s "
                    "attempts=%s last_err=%s title=%s"
                ),
                tag,
                msg_id,
                chat_id,
                sender_id,
                attempts,
                _truncate_text(last_err, limit=300),
                title_preview,
            )
            return False

        def _post_session_payload(self, *, incoming, payload: dict[str, Any]) -> Any:  # noqa: ANN001
            session_webhook = str(getattr(incoming, "session_webhook", "") or "").strip()
            if not session_webhook:
                return None
            req = urllib.request.Request(
                url=session_webhook,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}

        def _safe_reply_payload(
            self,
            *,
            incoming,
            payload: dict[str, Any],
            inbound: InboundChatMessage | None,
            tag: str,
        ) -> bool:  # noqa: ANN001
            if self._is_stale_reply_for_chat(inbound):
                self._log_stale_reply_skip(inbound=inbound, tag=tag, channel="payload")
                return False
            attempts = max(1, int(args.reply_retry_attempts))
            base_delay = max(0.1, float(args.reply_retry_base_delay_seconds))
            max_delay = max(base_delay, float(args.reply_retry_max_delay_seconds))
            msg_id = "" if inbound is None else inbound.msg_id
            chat_id = "" if inbound is None else inbound.chat_id
            sender_id = "" if inbound is None else inbound.sender_id
            msgtype = str(payload.get("msgtype", "")).strip() or "unknown"
            last_err = ""
            for attempt in range(1, attempts + 1):
                try:
                    result = self._post_session_payload(incoming=incoming, payload=payload)
                    if _reply_result_ok(result):
                        self._mark_reply()
                        _maybe_write_heartbeat(force=True)
                        LOGGER.info(
                            (
                                "reply_payload_sent tag=%s msgtype=%s msg_id=%s chat_id=%s sender_id=%s "
                                "attempt=%s/%s"
                            ),
                            tag,
                            msgtype,
                            msg_id,
                            chat_id,
                            sender_id,
                            attempt,
                            attempts,
                        )
                        return True
                    last_err = _truncate_text(str(result), limit=240) or "reply_result_none"
                    LOGGER.warning(
                        (
                            "reply_payload_retry_needed tag=%s msgtype=%s msg_id=%s chat_id=%s sender_id=%s "
                            "attempt=%s/%s reason=invalid_result result_preview=%s"
                        ),
                        tag,
                        msgtype,
                        msg_id,
                        chat_id,
                        sender_id,
                        attempt,
                        attempts,
                        last_err,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{type(exc).__name__}:{exc}"
                    LOGGER.warning(
                        (
                            "reply_payload_retry_needed tag=%s msgtype=%s msg_id=%s chat_id=%s sender_id=%s "
                            "attempt=%s/%s reason=exception err=%s"
                        ),
                        tag,
                        msgtype,
                        msg_id,
                        chat_id,
                        sender_id,
                        attempt,
                        attempts,
                        type(exc).__name__,
                    )
                if attempt < attempts:
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    time.sleep(delay)
            LOGGER.error(
                (
                    "reply_payload_give_up tag=%s msgtype=%s msg_id=%s chat_id=%s sender_id=%s "
                    "attempts=%s last_err=%s"
                ),
                tag,
                msgtype,
                msg_id,
                chat_id,
                sender_id,
                attempts,
                _truncate_text(last_err, limit=300),
            )
            return False

        def _upload_local_attachment(self, *, local_path: str, kind: str) -> tuple[str, str]:
            path = Path(str(local_path or "").strip()).expanduser()
            if not path.exists() or not path.is_file():
                return "", "local_file_not_found"
            try:
                payload = path.read_bytes()
            except OSError:
                return "", "local_file_unreadable"
            if not payload:
                return "", "local_file_empty"
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            upload_type = "image" if kind == "image" else ("voice" if kind == "audio" else "file")
            try:
                media_id = self.dingtalk_client.upload_to_dingtalk(  # type: ignore[attr-defined]
                    payload,
                    filetype=upload_type,
                    filename=path.name,
                    mimetype=mime,
                )
            except Exception as exc:  # noqa: BLE001
                return "", f"upload_failed:{type(exc).__name__}:{exc}"
            return str(media_id or "").strip(), ""

        @staticmethod
        def _build_media_payload_candidates(
            *,
            kind: str,
            media_id: str,
            title: str,
            source_url: str = "",
        ) -> list[dict[str, Any]]:
            item_title = str(title or "").strip() or "attachment"
            suffix = Path(item_title).suffix.lstrip(".").strip().lower()
            file_type = suffix or "file"

            def _file_payloads() -> list[dict[str, Any]]:
                # DingTalk session webhook 对 file 消息要求 fileType；补齐后再回退到兼容写法。
                return [
                    {
                        "msgtype": "file",
                        "file": {
                            "media_id": media_id,
                            "fileName": item_title,
                            "fileType": file_type,
                        },
                    },
                    {
                        "msgtype": "file",
                        "file": {
                            "mediaId": media_id,
                            "fileName": item_title,
                            "fileType": file_type,
                        },
                    },
                    {"msgtype": "file", "file": {"media_id": media_id}},
                    {"msgtype": "file", "file": {"mediaId": media_id}},
                ]

            if kind == "image":
                payloads: list[dict[str, Any]] = []
                safe_url = str(source_url or "").strip()
                if safe_url.lower().startswith(("http://", "https://")):
                    payloads.append({"msgtype": "image", "image": {"picURL": safe_url}})
                    payloads.append({"msgtype": "image", "image": {"picUrl": safe_url}})
                payloads.extend(
                    [
                        {"msgtype": "image", "image": {"media_id": media_id}},
                        {"msgtype": "image", "image": {"mediaId": media_id}},
                    ]
                )
                payloads.extend(_file_payloads())
                return payloads
            if kind == "audio":
                return [
                    {"msgtype": "voice", "voice": {"media_id": media_id, "duration": "0"}},
                    {"msgtype": "voice", "voice": {"mediaId": media_id, "duration": "0"}},
                    *_file_payloads(),
                ]
            if kind == "video":
                return [
                    {
                        "msgtype": "video",
                        "video": {
                            "media_id": media_id,
                            "title": item_title,
                            "description": item_title,
                        },
                    },
                    {
                        "msgtype": "video",
                        "video": {
                            "mediaId": media_id,
                            "title": item_title,
                            "description": item_title,
                        },
                    },
                    *_file_payloads(),
                ]
            return _file_payloads()

        def _reply_attachment_item(
            self,
            *,
            incoming,
            inbound: InboundChatMessage,
            attachment: dict[str, str],
            index: int,
            question_tag: str,
            trace_id: str,
            task_id: str,
        ) -> bool:  # noqa: ANN001
            source_path = str(attachment.get("path", "")).strip()
            source_url = str(attachment.get("url", "")).strip()
            kind = str(attachment.get("kind", "file")).strip().lower()
            kind = _coerce_attachment_kind_for_dingtalk(
                kind=kind,
                path_value=source_path,
                url_value=source_url,
            )

            # 对公网图片直链优先尝试 picURL 直发，避免“先下载再上传”失败导致只能发链接。
            if kind == "image" and source_url.lower().startswith(("http://", "https://")):
                direct_payloads = [
                    {"msgtype": "image", "image": {"picURL": source_url}},
                    {"msgtype": "image", "image": {"picUrl": source_url}},
                ]
                for payload_idx, payload in enumerate(direct_payloads, start=1):
                    if self._safe_reply_payload(
                        incoming=incoming,
                        payload=payload,
                        inbound=inbound,
                        tag=f"attachment_payload_direct_url_{index}_{payload_idx}",
                    ):
                        return True

            local_path = source_path
            if (not local_path) and source_url:
                cached = _cache_download_urls_to_local_attachments(
                    download_urls=[source_url],
                    msg_id=inbound.msg_id,
                    max_items=1,
                )
                if cached:
                    local_path = str(cached[0].get("local_path", "")).strip()
                    if kind == "file":
                        inferred_kind = str(cached[0].get("kind", "")).strip().lower()
                        if inferred_kind in {"image", "audio", "video", "file"}:
                            kind = inferred_kind

            media_id = ""
            upload_err = ""
            if local_path:
                media_id, upload_err = self._upload_local_attachment(local_path=local_path, kind=kind)
            if not media_id:
                # Fallback to markdown link/text so用户至少能拿到可访问入口。
                if source_url:
                    link_text = (
                        f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n\n"
                        f"附件#{index}（{kind}）\n"
                        f"[点击下载附件]({source_url})"
                    )
                    return self._safe_reply_markdown(
                        incoming=incoming,
                        title=f"附件回传 #{index}",
                        text=link_text,
                        inbound=inbound,
                        tag=f"attachment_link_fallback_{index}",
                    )
                return self._safe_reply(
                    incoming=incoming,
                    text=(
                        f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                        f"附件#{index} 回传失败：{upload_err or '无法读取或上传本地文件。'}"
                    ),
                    inbound=inbound,
                    tag=f"attachment_upload_failed_{index}",
                )

            title = Path(local_path).name if local_path else f"attachment_{index}"
            payloads = self._build_media_payload_candidates(
                kind=kind,
                media_id=media_id,
                title=title,
                source_url=source_url,
            )
            for payload_idx, payload in enumerate(payloads, start=1):
                if self._safe_reply_payload(
                    incoming=incoming,
                    payload=payload,
                    inbound=inbound,
                    tag=f"attachment_payload_{index}_{payload_idx}",
                ):
                    return True

            if source_url:
                return self._safe_reply_markdown(
                    incoming=incoming,
                    title=f"附件回传 #{index}",
                    text=(
                        f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n\n"
                        f"附件#{index}（{kind}）已上传但消息类型未被会话 webhook 接受，"
                        f"请改用下载链接：\n{source_url}"
                    ),
                    inbound=inbound,
                    tag=f"attachment_payload_fallback_{index}",
                )
            fallback_lines = [
                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}",
                (
                    f"附件#{index}（{kind}）回传失败：会话 webhook 拒绝了全部附件 payload，"
                    "且当前附件没有可用的公网下载链接。"
                ),
            ]
            if local_path:
                fallback_lines.append(f"local_path={local_path}")
            fallback_lines.append(
                "NEXT_ACTION: 请要求执行端先生成可访问 URL（IMAGE_URL/FILE_URL），或改为直接输出可下载链接。"
            )
            return self._safe_reply(
                incoming=incoming,
                text="\n".join(fallback_lines),
                inbound=inbound,
                tag=f"attachment_payload_hard_fail_{index}",
            )

        def _reply_multimodal_results(
            self,
            *,
            incoming,
            inbound: InboundChatMessage,
            metadata_payload: dict[str, Any],
            final_reply: str,
            question_tag: str,
            trace_id: str,
            task_id: str,
            tag_prefix: str,
        ) -> None:  # noqa: ANN001
            if not bool(getattr(args, "enable_multimodal_reply", True)):
                return
            attachments = _collect_outbound_attachments(
                metadata=metadata_payload,
                final_reply=final_reply,
                max_items=max(1, int(getattr(args, "multimodal_reply_max_items", 2))),
            )
            if not attachments:
                return
            self._safe_reply(
                incoming=incoming,
                text=(
                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                    f"检测到 {len(attachments)} 个多模态附件，开始回传。"
                ),
                inbound=inbound,
                tag=f"{tag_prefix}_multimodal_start",
            )
            for idx, attachment in enumerate(attachments, start=1):
                self._reply_attachment_item(
                    incoming=incoming,
                    inbound=inbound,
                    attachment=attachment,
                    index=idx,
                    question_tag=question_tag,
                    trace_id=trace_id,
                    task_id=task_id,
                )

        def _reply_in_background(self, *, incoming, text: str, inbound: InboundChatMessage | None, tag: str) -> None:  # noqa: ANN001
            worker = threading.Thread(
                target=self._safe_reply,
                kwargs={
                    "incoming": incoming,
                    "text": text,
                    "inbound": inbound,
                    "tag": tag,
                },
                daemon=True,
                name=f"fqg-reply-{tag}",
            )
            worker.start()

        def _dispatch_in_background(self, *, incoming, data: dict[str, Any], inbound: InboundChatMessage, normalized_message: str) -> None:  # noqa: ANN001
            trace_id, task_id = _build_trace_and_task_ids(inbound.msg_id)
            question_tag = _build_real_question_tag(
                msg_id=inbound.msg_id,
                normalized_message=normalized_message,
            )
            message_preview = normalized_message.replace("\n", " ").strip()
            if len(message_preview) > 80:
                message_preview = f"{message_preview[:80]}..."
            self._safe_reply(
                incoming=incoming,
                text=(
                    "已受理，正在调度 leader 与协作实例执行。\n"
                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}"
                ),
                inbound=inbound,
                tag="accepted",
            )
            self._safe_reply(
                incoming=incoming,
                text=f"已理解你的指令[{question_tag}]：{message_preview or '-'}",
                inbound=inbound,
                tag="intent_echo",
            )

            metadata_payload = _build_metadata(data, inbound, incoming)
            metadata_payload["question_tag"] = question_tag
            metadata_payload["trace_id"] = trace_id
            metadata_payload["task_id"] = task_id
            dispatch_message = (
                f"{normalized_message}\n\n"
                "[执行约束]\n"
                "请直接执行并输出可交付结果，不要只回复计划句或停留在待输入状态。\n"
                "若存在阻断，请明确说明阻断原因和所需补充信息。"
            )
            if _requires_inline_image_display(normalized_message):
                dispatch_message = (
                    f"{dispatch_message}\n"
                    "[图片交付偏好]\n"
                    "用户明确希望在钉钉里直接看到图片，请优先返回公网可访问的 https 图片链接，"
                    "并在最终答案里附上该链接。\n"
                    "不要只给本地路径（如 `/Users/...`）。若确实拿不到公网链接，请说明原因，"
                    "并给出可执行的替代方案。"
                )
            raw_image_codes = metadata_payload.get("image_download_codes")
            image_codes = raw_image_codes if isinstance(raw_image_codes, list) else []
            image_codes = [str(code).strip() for code in image_codes if str(code).strip()]
            attachment_kind = str(metadata_payload.get("attachment_kind", "")).strip().lower()
            if image_codes:
                download_urls = _resolve_message_file_download_urls(
                    client_id=args.client_id,
                    client_secret=args.client_secret,
                    robot_code=str(metadata_payload.get("robot_code", "") or args.client_id),
                    download_codes=image_codes,
                )
                if download_urls:
                    metadata_payload["attachment_download_urls"] = download_urls
                    cached_attachments = _cache_download_urls_to_local_attachments(
                        download_urls=download_urls,
                        msg_id=inbound.msg_id,
                        max_items=3,
                    )
                    attachment_local_paths = [
                        str(item.get("local_path", "")).strip()
                        for item in cached_attachments
                        if str(item.get("local_path", "")).strip()
                    ]
                    if attachment_local_paths:
                        metadata_payload["attachment_local_paths"] = attachment_local_paths

                    image_urls = [
                        str(item.get("url", "")).strip()
                        for item in cached_attachments
                        if str(item.get("kind", "")).strip().lower() == "image"
                        and str(item.get("url", "")).strip()
                    ]
                    image_paths = [
                        str(item.get("local_path", "")).strip()
                        for item in cached_attachments
                        if str(item.get("kind", "")).strip().lower() == "image"
                        and str(item.get("local_path", "")).strip()
                    ]
                    if image_urls:
                        metadata_payload["image_download_urls"] = image_urls
                    if image_paths:
                        metadata_payload["image_local_paths"] = image_paths

                if attachment_kind == "image":
                    self._safe_reply(
                        incoming=incoming,
                        text=f"检测到图片消息（{len(image_codes)}张），开始识别。",
                        inbound=inbound,
                        tag="image_detected",
                    )
                if download_urls and attachment_kind == "image":
                    url_lines = "\n".join(f"- {url}" for url in download_urls[:3])
                    raw_image_paths = metadata_payload.get("image_local_paths")
                    local_paths = (
                        [str(path).strip() for path in raw_image_paths if str(path).strip()]
                        if isinstance(raw_image_paths, list)
                        else []
                    )
                    path_lines = (
                        "\n".join(f"- {path}" for path in local_paths[:3])
                        if local_paths
                        else "(未落地本地图片路径)"
                    )
                    dispatch_message = (
                        f"{normalized_message}\n\n"
                        "[图片消息上下文]\n"
                        "检测到用户发送图片，请优先识别图片内容并回答用户问题。\n"
                        "优先使用本地图片路径（避免外链不可达），再尝试URL。\n"
                        f"本地图片路径:\n{path_lines}\n"
                        f"可下载URL:\n{url_lines}"
                    )
                elif attachment_kind == "image":
                    dispatch_message = (
                        f"{normalized_message}\n\n"
                        "[图片消息上下文]\n"
                        "检测到用户发送图片(downloadCode可用)，但暂未解析到下载URL；"
                        "请明确告知用户并提示其补充文字或重发图片。"
                    )
                else:
                    message_type = str(metadata_payload.get("message_type", "")).strip() or "unknown"
                    url_lines = (
                        "\n".join(
                            f"- {url}" for url in (metadata_payload.get("attachment_download_urls") or [])[:3]
                        )
                        if isinstance(metadata_payload.get("attachment_download_urls"), list)
                        else "(未解析到下载URL)"
                    )
                    path_lines = (
                        "\n".join(
                            f"- {path}" for path in (metadata_payload.get("attachment_local_paths") or [])[:3]
                        )
                        if isinstance(metadata_payload.get("attachment_local_paths"), list)
                        else "(未落地本地附件路径)"
                    )
                    code_lines = "\n".join(f"- {code}" for code in image_codes[:5])
                    dispatch_message = (
                        f"{normalized_message}\n\n"
                        "[附件消息上下文]\n"
                        f"检测到非图片附件(type={message_type})，请优先读取附件内容并回答用户问题。\n"
                        f"downloadCode:\n{code_lines}\n"
                        f"本地附件路径:\n{path_lines}\n"
                        f"可下载URL:\n{url_lines}"
                    )
            reasoning_loop_focus = True
            metadata_payload["reasoning_loop_focus"] = reasoning_loop_focus
            # Keep backward-compatible metadata for older observers.
            metadata_payload["force_final_focus"] = reasoning_loop_focus
            dispatch_message = (
                f"{dispatch_message}\n\n"
                "[收敛执行要求]\n"
                "请直接收敛到最终可交付答案，不要停在计划句或待输入状态。\n"
                "输出格式必须包含：FINAL_ANSWER / EVIDENCE / NEXT_ACTION。"
            )

            def _push_followup_snapshots(initial_signature: str = "") -> str:
                progress_push_count = max(0, int(args.progress_push_count))
                if args.followup_seconds <= 0 or progress_push_count <= 0:
                    return initial_signature
                last_signature = initial_signature
                for idx in range(progress_push_count):
                    try:
                        time.sleep(max(0.5, float(args.followup_seconds)))
                        snapshot = client.get_leader_snapshot()
                        signature = _snapshot_signature(snapshot)
                        if signature == last_signature:
                            LOGGER.info(
                                "followup_snapshot_skipped_same trace_id=%s msg_id=%s chat_id=%s step=%s",
                                trace_id,
                                inbound.msg_id,
                                inbound.chat_id,
                                idx + 1,
                            )
                            continue
                        last_signature = signature
                        step = idx + 1
                        self._safe_reply(
                            incoming=incoming,
                            text=(
                                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} progress={step}/{progress_push_count}\n"
                                f"{format_leader_snapshot_summary(snapshot)}"
                            ),
                            inbound=inbound,
                            tag=f"dispatch_followup_snapshot_{step}",
                        )
                    except Exception as followup_exc:  # noqa: BLE001
                        LOGGER.warning(
                            "followup_snapshot_failed trace_id=%s msg_id=%s chat_id=%s step=%s err=%s",
                            trace_id,
                            inbound.msg_id,
                            inbound.chat_id,
                            idx + 1,
                            type(followup_exc).__name__,
                        )
                return last_signature

            def _schedule_late_final_push(
                *,
                baseline_signature: str = "",
                last_seen_signature: str = "",
            ) -> None:
                late_wait = max(0.0, float(args.post_timeout_final_wait_seconds))
                if late_wait <= 0:
                    return
                late_poll = max(1.0, float(args.post_timeout_poll_seconds))

                def _worker() -> None:
                    deadline = time.monotonic() + late_wait
                    latest_signature = str(last_seen_signature or "")
                    auto_continue_attempts = 0
                    max_auto_continue_attempts = max(
                        0,
                        int(args.auto_continue_waiting_input_max_attempts),
                    )
                    auto_continue_min_seconds = max(
                        1.0,
                        float(args.auto_continue_waiting_input_min_seconds),
                    )
                    next_auto_continue_at = 0.0
                    reasoning_attempts = 0
                    max_reasoning_attempts = max(0, int(args.reasoning_max_attempts))
                    reasoning_min_seconds = max(1.0, float(args.reasoning_min_seconds))
                    next_reasoning_at = 0.0
                    reasoning_escalated = False
                    while time.monotonic() < deadline:
                        try:
                            snapshot = client.get_leader_snapshot()
                        except Exception as late_exc:  # noqa: BLE001
                            LOGGER.info(
                                "late_final_snapshot_failed trace_id=%s msg_id=%s chat_id=%s err=%s",
                                trace_id,
                                inbound.msg_id,
                                inbound.chat_id,
                                type(late_exc).__name__,
                            )
                            time.sleep(late_poll)
                            continue

                        state, reply, last_summary = _extract_leader_outcome(snapshot)
                        signature = _snapshot_signature(snapshot)
                        if signature == latest_signature:
                            time.sleep(late_poll)
                            continue
                        latest_signature = signature
                        if not _is_terminal_snapshot_state(state):
                            if reply:
                                reply_text = reply if len(reply) <= 1200 else f"{reply[:1200]}..."
                                self._safe_reply(
                                    incoming=incoming,
                                    text=(
                                        f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} state={state}\n"
                                        f"后台进展：\n{reply_text}"
                                    ),
                                    inbound=inbound,
                                    tag="dispatch_late_progress_update",
                                )
                            time.sleep(late_poll)
                            continue
                        if baseline_signature and signature == baseline_signature:
                            time.sleep(late_poll)
                            continue
                        if not _is_final_reply_ready(
                            state=state,
                            reply=reply,
                            last_summary=last_summary,
                            user_message=normalized_message,
                        ):
                            if reply:
                                reply_text = reply if len(reply) <= 1200 else f"{reply[:1200]}..."
                                normalized_state = state.strip().upper()
                                if (
                                    normalized_state in {"DONE_WAITING_INPUT", "WAITING_INPUT"}
                                    and not _looks_like_user_echo_reply(reply=reply, user_message=normalized_message)
                                ):
                                    now = time.monotonic()
                                    if (
                                        reasoning_loop_focus
                                        and args.reasoning_loop_on_settled
                                        and reasoning_attempts < max_reasoning_attempts
                                        and now >= next_reasoning_at
                                    ):
                                        reasoning_attempts += 1
                                        next_reasoning_at = now + reasoning_min_seconds
                                        try:
                                            retry_response = client.send_leader_command(
                                                message=_build_l3_reasoning_prompt(
                                                    original_message=normalized_message,
                                                    metadata=metadata_payload,
                                                    attempt=reasoning_attempts,
                                                    max_attempts=max(1, max_reasoning_attempts),
                                                    reasoning_level=str(args.reasoning_level or "L3"),
                                                    mandatory_fields=list(args.reasoning_mandatory_fields),
                                                    require_next_action=bool(args.reasoning_require_next_action),
                                                ),
                                                auto_collab=False,
                                                metadata={
                                                    **metadata_payload,
                                                    "retry_mode": "l3_reasoning_late",
                                                    "retry_source_trace_id": trace_id,
                                                    "retry_source_task_id": task_id,
                                                },
                                                verify_seconds=max(4.0, float(args.verify_seconds or 6.0)),
                                                collab_verify_seconds=max(4.0, float(args.collab_verify_seconds or 6.0)),
                                            )
                                            self._safe_reply(
                                                incoming=incoming,
                                                text=(
                                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                                    "检测到会话在待输入状态，已自动触发 L3 推理收敛重试。\n"
                                                    f"{format_dispatch_summary(retry_response)}"
                                                ),
                                                inbound=inbound,
                                                tag="dispatch_l3_reasoning_retry_late",
                                            )
                                            latest_signature = ""
                                            time.sleep(late_poll)
                                            continue
                                        except Exception as retry_exc:  # noqa: BLE001
                                            LOGGER.warning(
                                                (
                                                    "late_l3_reasoning_retry_failed trace_id=%s msg_id=%s "
                                                    "chat_id=%s attempt=%s err=%s"
                                                ),
                                                trace_id,
                                                inbound.msg_id,
                                                inbound.chat_id,
                                                reasoning_attempts,
                                                type(retry_exc).__name__,
                                            )
                                    if (
                                        reasoning_loop_focus
                                        and args.reasoning_loop_on_settled
                                        and bool(args.reasoning_require_next_action)
                                        and reasoning_attempts >= max_reasoning_attempts
                                        and not reasoning_escalated
                                    ):
                                        reasoning_escalated = True
                                        try:
                                            retry_response = client.send_leader_command(
                                                message=_build_l3_escalation_prompt(
                                                    original_message=normalized_message,
                                                    metadata=metadata_payload,
                                                    max_attempts=max(1, max_reasoning_attempts),
                                                    require_next_action=bool(args.reasoning_require_next_action),
                                                ),
                                                auto_collab=False,
                                                metadata={
                                                    **metadata_payload,
                                                    "retry_mode": "l3_reasoning_escalation_late",
                                                    "retry_source_trace_id": trace_id,
                                                    "retry_source_task_id": task_id,
                                                },
                                                verify_seconds=max(4.0, float(args.verify_seconds or 6.0)),
                                                collab_verify_seconds=max(4.0, float(args.collab_verify_seconds or 6.0)),
                                            )
                                            self._safe_reply(
                                                incoming=incoming,
                                                text=(
                                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                                    f"L3 推理重试已达上限({max_reasoning_attempts})，"
                                                    "已触发升级收敛并要求给出 NEXT_ACTION。\n"
                                                    f"{format_dispatch_summary(retry_response)}"
                                                ),
                                                inbound=inbound,
                                                tag="dispatch_l3_reasoning_escalation_late",
                                            )
                                            latest_signature = ""
                                            time.sleep(late_poll)
                                            continue
                                        except Exception as retry_exc:  # noqa: BLE001
                                            LOGGER.warning(
                                                (
                                                    "late_l3_reasoning_escalation_failed trace_id=%s msg_id=%s "
                                                    "chat_id=%s err=%s"
                                                ),
                                                trace_id,
                                                inbound.msg_id,
                                                inbound.chat_id,
                                                type(retry_exc).__name__,
                                            )
                                    if (
                                        args.auto_continue_on_waiting_input
                                        and not (reasoning_loop_focus and args.reasoning_loop_on_settled)
                                        and auto_continue_attempts < max_auto_continue_attempts
                                        and now >= next_auto_continue_at
                                    ):
                                        auto_continue_attempts += 1
                                        next_auto_continue_at = now + auto_continue_min_seconds
                                        try:
                                            retry_response = client.send_leader_command(
                                                message=_build_continue_current_task_prompt(
                                                    original_message=normalized_message
                                                ),
                                                auto_collab=bool(args.auto_collab),
                                                metadata={
                                                    **metadata_payload,
                                                    "retry_mode": "auto_continue_waiting_input_late",
                                                    "retry_source_trace_id": trace_id,
                                                    "retry_source_task_id": task_id,
                                                },
                                                verify_seconds=max(4.0, float(args.verify_seconds or 6.0)),
                                                collab_verify_seconds=max(4.0, float(args.collab_verify_seconds or 6.0)),
                                            )
                                            self._safe_reply(
                                                incoming=incoming,
                                                text=(
                                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                                    "检测到会话进入待输入，已自动继续执行并保持跟踪。\n"
                                                    f"{format_dispatch_summary(retry_response)}"
                                                ),
                                                inbound=inbound,
                                                tag="dispatch_late_auto_continue_waiting_input",
                                            )
                                            latest_signature = ""
                                            time.sleep(late_poll)
                                            continue
                                        except Exception as retry_exc:  # noqa: BLE001
                                            LOGGER.warning(
                                                (
                                                    "late_auto_continue_waiting_input_failed trace_id=%s msg_id=%s "
                                                    "chat_id=%s attempt=%s err=%s"
                                                ),
                                                trace_id,
                                                inbound.msg_id,
                                                inbound.chat_id,
                                                auto_continue_attempts,
                                                type(retry_exc).__name__,
                                            )
                                    self._safe_reply(
                                        incoming=incoming,
                                        text=(
                                            f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} state={state}\n"
                                            "会话已回到待输入状态。\n"
                                            "如果要继续追到最终答案，请直接在钉钉发送：继续执行（我会自动沿用 question_tag 跟踪）。\n"
                                            f"当前回复：\n{reply_text}"
                                        ),
                                        inbound=inbound,
                                        tag="dispatch_late_turn_settled",
                                    )
                                    return
                                self._safe_reply(
                                    incoming=incoming,
                                    text=(
                                        f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} state={state}\n"
                                        f"后台进展：\n{reply_text}"
                                    ),
                                    inbound=inbound,
                                    tag="dispatch_late_progress_update",
                                )
                            time.sleep(late_poll)
                            continue

                        if reply:
                            reply_text = reply if len(reply) <= 1200 else f"{reply[:1200]}..."
                            self._safe_reply(
                                incoming=incoming,
                                text=(
                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} final_state={state}\n"
                                    f"最终回复（延迟补发）：\n{reply_text}"
                                ),
                                inbound=inbound,
                                tag="dispatch_final_result_late",
                            )
                            self._reply_multimodal_results(
                                incoming=incoming,
                                inbound=inbound,
                                metadata_payload=metadata_payload,
                                final_reply=reply_text,
                                question_tag=question_tag,
                                trace_id=trace_id,
                                task_id=task_id,
                                tag_prefix="dispatch_final_result_late",
                            )
                            return

                        summary = last_summary or "-"
                        self._safe_reply(
                            incoming=incoming,
                            text=(
                                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} final_state={state}\n"
                                f"执行已结束（延迟补发），但暂未抓到最终文本。last={summary}\n"
                                "请发送“状态”拉取最新会话内容。"
                            ),
                            inbound=inbound,
                            tag="dispatch_final_state_without_reply_late",
                        )
                        return

                    LOGGER.info(
                        "late_final_watch_expired trace_id=%s msg_id=%s chat_id=%s wait_seconds=%.1f",
                        trace_id,
                        inbound.msg_id,
                        inbound.chat_id,
                        late_wait,
                    )

                watcher = threading.Thread(
                    target=_worker,
                    name="fqg-late-final-watch",
                    daemon=True,
                )
                watcher.start()

            def _push_final_result(
                last_snapshot_signature: str,
                *,
                baseline_signature: str = "",
                pane_fallback_reply: str = "",
            ) -> None:
                soft_wait = max(5.0, float(args.completion_wait_seconds))
                hard_wait = max(soft_wait, float(args.completion_max_wait_seconds))
                poll_seconds = max(1.0, float(args.completion_poll_seconds))
                started = time.monotonic()
                soft_deadline = started + soft_wait
                hard_deadline = started + hard_wait
                # Seed with previous snapshot signature so old/stale terminal replies
                # are not reported as the result of this newly accepted task.
                latest_signature = str(last_snapshot_signature or "")
                waiting_hint_sent = False
                fallback_retried = False
                auto_continue_attempts = 0
                max_auto_continue_attempts = max(
                    0,
                    int(args.auto_continue_waiting_input_max_attempts),
                )
                auto_continue_min_seconds = max(
                    1.0,
                    float(args.auto_continue_waiting_input_min_seconds),
                )
                next_auto_continue_at = 0.0
                reasoning_attempts = 0
                max_reasoning_attempts = max(0, int(args.reasoning_max_attempts))
                reasoning_min_seconds = max(1.0, float(args.reasoning_min_seconds))
                next_reasoning_at = 0.0
                reasoning_escalated = False
                announced_pending_ids: set[str] = set()
                while True:
                    now = time.monotonic()
                    if now > hard_deadline:
                        timeout_extra = ""
                        try:
                            timeout_snapshot = client.get_leader_snapshot()
                            timeout_extra = _build_max_wait_snapshot_summary(timeout_snapshot)
                        except Exception as timeout_snapshot_exc:  # noqa: BLE001
                            LOGGER.warning(
                                "max_wait_snapshot_failed trace_id=%s msg_id=%s chat_id=%s err=%s",
                                trace_id,
                                inbound.msg_id,
                                inbound.chat_id,
                                type(timeout_snapshot_exc).__name__,
                            )
                        timeout_block = f"{timeout_extra}\n" if timeout_extra else ""
                        if pane_fallback_reply:
                            pane_hint = (
                                pane_fallback_reply
                                if len(pane_fallback_reply) <= 300
                                else f"{pane_fallback_reply[:300]}..."
                            )
                            timeout_block += f"pane_hint={pane_hint}\n"
                        self._safe_reply(
                            incoming=incoming,
                            text=(
                                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                "执行耗时较长，暂未拿到最终结果。\n"
                                f"{timeout_block}"
                                "请发送“状态”继续查看进展。"
                            ),
                            inbound=inbound,
                            tag="dispatch_max_wait_reached",
                        )
                        _schedule_late_final_push(
                            baseline_signature=baseline_signature,
                            last_seen_signature=latest_signature,
                        )
                        if float(args.post_timeout_final_wait_seconds) > 0:
                            self._safe_reply(
                                incoming=incoming,
                                text=(
                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                    "已转后台持续跟踪；拿到最终结果后会自动补发。"
                                ),
                                inbound=inbound,
                                tag="dispatch_late_watch_started",
                            )
                        return
                    if (not waiting_hint_sent) and now > soft_deadline:
                        waiting_hint_sent = True
                        self._safe_reply(
                            incoming=incoming,
                            text=(
                                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                "仍在执行中，继续等待最终结果并自动回传。"
                            ),
                            inbound=inbound,
                            tag="dispatch_still_running",
                        )

                    try:
                        pending_queue = client.get_approval_queue(status_filter="PENDING", limit=3)
                        pending_items = pending_queue.get("items") if isinstance(pending_queue, dict) else []
                        pending_ids: list[str] = []
                        if isinstance(pending_items, list):
                            for raw in pending_items:
                                item = raw if isinstance(raw, dict) else {}
                                rid = str(item.get("request_id", "")).strip()
                                if rid:
                                    pending_ids.append(rid)
                        fresh_pending = [rid for rid in pending_ids if rid not in announced_pending_ids]
                        if fresh_pending:
                            announced_pending_ids.update(fresh_pending)
                            self._safe_reply(
                                incoming=incoming,
                                text=(
                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                    "检测到执行等待审批，请在龟仙人回复：同意 <request_id> 或 拒绝 <request_id>\n"
                                    f"{format_approval_queue_summary(pending_queue)}"
                                ),
                                inbound=inbound,
                                tag="dispatch_pending_approval_hint",
                            )
                    except Exception:  # noqa: BLE001
                        pass

                    try:
                        snapshot = client.get_leader_snapshot()
                    except Exception as completion_exc:  # noqa: BLE001
                        LOGGER.warning(
                            "completion_snapshot_failed trace_id=%s msg_id=%s chat_id=%s err=%s",
                            trace_id,
                            inbound.msg_id,
                            inbound.chat_id,
                            type(completion_exc).__name__,
                        )
                        time.sleep(poll_seconds)
                        continue

                    state, reply, last_summary = _extract_leader_outcome(snapshot)
                    signature = _snapshot_signature(snapshot)
                    # Fast-path: if follow-up snapshots already observed a new terminal
                    # signature compared to dispatch baseline, emit final immediately
                    # even when the signature no longer changes (stable terminal state).
                    if (
                        baseline_signature
                        and signature == latest_signature
                        and signature != baseline_signature
                        and _is_terminal_snapshot_state(state)
                        and _is_final_reply_ready(
                            state=state,
                            reply=reply,
                            last_summary=last_summary,
                            user_message=normalized_message,
                        )
                    ):
                        if reply:
                            reply_text = reply if len(reply) <= 1200 else f"{reply[:1200]}..."
                            self._safe_reply(
                                incoming=incoming,
                                text=(
                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} final_state={state}\n"
                                    f"最终回复：\n{reply_text}"
                                ),
                                inbound=inbound,
                                tag="dispatch_final_result",
                            )
                            self._reply_multimodal_results(
                                incoming=incoming,
                                inbound=inbound,
                                metadata_payload=metadata_payload,
                                final_reply=reply_text,
                                question_tag=question_tag,
                                trace_id=trace_id,
                                task_id=task_id,
                                tag_prefix="dispatch_final_result_fast_terminal",
                            )
                        else:
                            summary = last_summary or "-"
                            self._safe_reply(
                                incoming=incoming,
                                text=(
                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} final_state={state}\n"
                                    f"执行已结束，但暂未抓到最终文本。last={summary}\n"
                                    "请发送“状态”拉取最新会话内容。"
                                ),
                                inbound=inbound,
                                tag="dispatch_final_state_without_reply",
                            )
                        return

                    if signature == latest_signature:
                        time.sleep(poll_seconds)
                        continue
                    latest_signature = signature

                    final_ready = _is_terminal_snapshot_state(state) and _is_final_reply_ready(
                        state=state,
                        reply=reply,
                        last_summary=last_summary,
                        user_message=normalized_message,
                    )
                    if not final_ready:
                        if reply:
                            reply_text = reply if len(reply) <= 1200 else f"{reply[:1200]}..."
                            normalized_state = state.strip().upper()
                            if (
                                normalized_state in {"DONE_WAITING_INPUT", "WAITING_INPUT"}
                                and not _looks_like_user_echo_reply(reply=reply, user_message=normalized_message)
                            ):
                                if (
                                    reasoning_loop_focus
                                    and args.reasoning_loop_on_settled
                                    and reasoning_attempts < max_reasoning_attempts
                                    and now >= next_reasoning_at
                                ):
                                    reasoning_attempts += 1
                                    next_reasoning_at = now + reasoning_min_seconds
                                    try:
                                        retry_response = client.send_leader_command(
                                            message=_build_l3_reasoning_prompt(
                                                original_message=normalized_message,
                                                metadata=metadata_payload,
                                                attempt=reasoning_attempts,
                                                max_attempts=max(1, max_reasoning_attempts),
                                                reasoning_level=str(args.reasoning_level or "L3"),
                                                mandatory_fields=list(args.reasoning_mandatory_fields),
                                                require_next_action=bool(args.reasoning_require_next_action),
                                            ),
                                            auto_collab=False,
                                            metadata={
                                                **metadata_payload,
                                                "retry_mode": "l3_reasoning",
                                                "retry_source_trace_id": trace_id,
                                                "retry_source_task_id": task_id,
                                            },
                                            verify_seconds=max(4.0, float(args.verify_seconds or 6.0)),
                                            collab_verify_seconds=max(4.0, float(args.collab_verify_seconds or 6.0)),
                                        )
                                        self._safe_reply(
                                            incoming=incoming,
                                            text=(
                                                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                                "检测到会话在待输入状态，已自动触发 L3 推理收敛重试。\n"
                                                f"{format_dispatch_summary(retry_response)}"
                                            ),
                                            inbound=inbound,
                                            tag="dispatch_l3_reasoning_retry",
                                        )
                                        started = time.monotonic()
                                        soft_deadline = started + soft_wait
                                        hard_deadline = started + hard_wait
                                        waiting_hint_sent = False
                                        latest_signature = ""
                                        continue
                                    except Exception as retry_exc:  # noqa: BLE001
                                        LOGGER.warning(
                                            (
                                                "l3_reasoning_retry_failed trace_id=%s msg_id=%s "
                                                "chat_id=%s attempt=%s err=%s"
                                            ),
                                            trace_id,
                                            inbound.msg_id,
                                            inbound.chat_id,
                                            reasoning_attempts,
                                            type(retry_exc).__name__,
                                        )
                                if (
                                    reasoning_loop_focus
                                    and args.reasoning_loop_on_settled
                                    and bool(args.reasoning_require_next_action)
                                    and reasoning_attempts >= max_reasoning_attempts
                                    and not reasoning_escalated
                                ):
                                    reasoning_escalated = True
                                    try:
                                        retry_response = client.send_leader_command(
                                            message=_build_l3_escalation_prompt(
                                                original_message=normalized_message,
                                                metadata=metadata_payload,
                                                max_attempts=max(1, max_reasoning_attempts),
                                                require_next_action=bool(args.reasoning_require_next_action),
                                            ),
                                            auto_collab=False,
                                            metadata={
                                                **metadata_payload,
                                                "retry_mode": "l3_reasoning_escalation",
                                                "retry_source_trace_id": trace_id,
                                                "retry_source_task_id": task_id,
                                            },
                                            verify_seconds=max(4.0, float(args.verify_seconds or 6.0)),
                                            collab_verify_seconds=max(4.0, float(args.collab_verify_seconds or 6.0)),
                                        )
                                        self._safe_reply(
                                            incoming=incoming,
                                            text=(
                                                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                                f"L3 推理重试已达上限({max_reasoning_attempts})，"
                                                "已触发升级收敛并要求给出 NEXT_ACTION。\n"
                                                f"{format_dispatch_summary(retry_response)}"
                                            ),
                                            inbound=inbound,
                                            tag="dispatch_l3_reasoning_escalation",
                                        )
                                        started = time.monotonic()
                                        soft_deadline = started + soft_wait
                                        hard_deadline = started + hard_wait
                                        waiting_hint_sent = False
                                        latest_signature = ""
                                        continue
                                    except Exception as retry_exc:  # noqa: BLE001
                                        LOGGER.warning(
                                            (
                                                "l3_reasoning_escalation_failed trace_id=%s msg_id=%s "
                                                "chat_id=%s err=%s"
                                            ),
                                            trace_id,
                                            inbound.msg_id,
                                            inbound.chat_id,
                                            type(retry_exc).__name__,
                                        )
                                if (
                                    args.auto_continue_on_waiting_input
                                    and not (reasoning_loop_focus and args.reasoning_loop_on_settled)
                                    and auto_continue_attempts < max_auto_continue_attempts
                                    and now >= next_auto_continue_at
                                ):
                                    auto_continue_attempts += 1
                                    next_auto_continue_at = now + auto_continue_min_seconds
                                    try:
                                        retry_response = client.send_leader_command(
                                            message=_build_continue_current_task_prompt(
                                                original_message=normalized_message
                                            ),
                                            auto_collab=bool(args.auto_collab),
                                            metadata={
                                                **metadata_payload,
                                                "retry_mode": "auto_continue_waiting_input",
                                                "retry_source_trace_id": trace_id,
                                                "retry_source_task_id": task_id,
                                            },
                                            verify_seconds=max(4.0, float(args.verify_seconds or 6.0)),
                                            collab_verify_seconds=max(4.0, float(args.collab_verify_seconds or 6.0)),
                                        )
                                        self._safe_reply(
                                            incoming=incoming,
                                            text=(
                                                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                                "检测到会话进入待输入，已自动继续执行并保持跟踪。\n"
                                                f"{format_dispatch_summary(retry_response)}"
                                            ),
                                            inbound=inbound,
                                            tag="dispatch_auto_continue_waiting_input",
                                        )
                                        started = time.monotonic()
                                        soft_deadline = started + soft_wait
                                        hard_deadline = started + hard_wait
                                        waiting_hint_sent = False
                                        latest_signature = ""
                                        continue
                                    except Exception as retry_exc:  # noqa: BLE001
                                        LOGGER.warning(
                                            (
                                                "auto_continue_waiting_input_failed trace_id=%s msg_id=%s "
                                                "chat_id=%s attempt=%s err=%s"
                                            ),
                                            trace_id,
                                            inbound.msg_id,
                                            inbound.chat_id,
                                            auto_continue_attempts,
                                            type(retry_exc).__name__,
                                        )
                                self._safe_reply(
                                    incoming=incoming,
                                    text=(
                                        f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} state={state}\n"
                                        "会话已回到待输入状态。\n"
                                        "如果要继续追到最终答案，请直接在钉钉发送：继续执行（我会自动沿用 question_tag 跟踪）。\n"
                                        f"当前回复：\n{reply_text}"
                                    ),
                                    inbound=inbound,
                                    tag="dispatch_turn_settled",
                                )
                                return
                            self._safe_reply(
                                incoming=incoming,
                                text=(
                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} state={state}\n"
                                    f"执行进展：\n{reply_text}"
                                ),
                                inbound=inbound,
                                tag="dispatch_progress_update",
                            )
                        time.sleep(poll_seconds)
                        continue

                    if (
                        args.enable_identity_refusal_fallback
                        and (not fallback_retried)
                        and _has_image_context(metadata_payload)
                        and _is_identity_refusal_reply(reply)
                    ):
                        fallback_retried = True
                        fallback_prompt = _build_safe_image_retry_prompt(
                            original_message=normalized_message,
                            metadata=metadata_payload,
                        )
                        self._safe_reply(
                            incoming=incoming,
                            text=(
                                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                "检测到身份识别类拒绝，已自动切换到“客观描述模式”重试。"
                            ),
                            inbound=inbound,
                            tag="dispatch_fallback_retry_start",
                        )
                        try:
                            retry_response = client.send_leader_command(
                                message=fallback_prompt,
                                auto_collab=False,
                                metadata={
                                    **metadata_payload,
                                    "retry_mode": "safe_image_description",
                                    "retry_source_trace_id": trace_id,
                                    "retry_source_task_id": task_id,
                                },
                                verify_seconds=max(4.0, float(args.verify_seconds or 6.0)),
                                collab_verify_seconds=max(4.0, float(args.collab_verify_seconds or 6.0)),
                            )
                            self._safe_reply(
                                incoming=incoming,
                                text=(
                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                    f"{format_dispatch_summary(retry_response)}"
                                ),
                                inbound=inbound,
                                tag="dispatch_fallback_retry_summary",
                            )
                            started = time.monotonic()
                            soft_deadline = started + max(20.0, soft_wait)
                            hard_deadline = started + max(120.0, hard_wait)
                            latest_signature = signature
                            waiting_hint_sent = False
                            continue
                        except Exception as retry_exc:  # noqa: BLE001
                            LOGGER.warning(
                                "fallback_retry_failed trace_id=%s msg_id=%s chat_id=%s err=%s",
                                trace_id,
                                inbound.msg_id,
                                inbound.chat_id,
                                type(retry_exc).__name__,
                            )
                            self._safe_reply(
                                incoming=incoming,
                                text=(
                                    f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                                    f"客观描述重试失败：{type(retry_exc).__name__}: {retry_exc}"
                                ),
                                inbound=inbound,
                                tag="dispatch_fallback_retry_failed",
                            )

                    if reply:
                        reply_text = reply if len(reply) <= 1200 else f"{reply[:1200]}..."
                        self._safe_reply(
                            incoming=incoming,
                            text=(
                                f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} final_state={state}\n"
                                f"最终回复：\n{reply_text}"
                            ),
                            inbound=inbound,
                            tag="dispatch_final_result",
                        )
                        self._reply_multimodal_results(
                            incoming=incoming,
                            inbound=inbound,
                            metadata_payload=metadata_payload,
                            final_reply=reply_text,
                            question_tag=question_tag,
                            trace_id=trace_id,
                            task_id=task_id,
                            tag_prefix="dispatch_final_result",
                        )
                        return

                    summary = last_summary or "-"
                    self._safe_reply(
                        incoming=incoming,
                        text=(
                            f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} final_state={state}\n"
                            f"执行已结束，但暂未抓到最终文本。last={summary}\n"
                            "请发送“状态”拉取最新会话内容。"
                        ),
                        inbound=inbound,
                        tag="dispatch_final_state_without_reply",
                    )
                    return

            try:
                baseline_signature = ""
                try:
                    baseline_signature = _snapshot_signature(client.get_leader_snapshot())
                except Exception as baseline_exc:  # noqa: BLE001
                    LOGGER.info(
                        "baseline_snapshot_unavailable trace_id=%s msg_id=%s chat_id=%s err=%s",
                        trace_id,
                        inbound.msg_id,
                        inbound.chat_id,
                        type(baseline_exc).__name__,
                    )
                response = client.send_leader_command(
                    message=dispatch_message,
                    auto_collab=bool(args.auto_collab),
                    metadata=metadata_payload,
                    verify_seconds=args.verify_seconds,
                    collab_verify_seconds=args.collab_verify_seconds,
                )
                pane_fallback_reply = _extract_pane_signal_fallback(response)
                self._safe_reply(
                    incoming=incoming,
                    text=(
                        f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                        f"{format_dispatch_summary(response)}"
                    ),
                    inbound=inbound,
                    tag="dispatch_summary",
                )
                dispatch_accepted, dispatch_reject_reason = _evaluate_dispatch_acceptance(response)
                if not dispatch_accepted:
                    self._safe_reply(
                        incoming=incoming,
                        text=(
                            f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                            "本次任务未成功受理，已停止后续状态追踪（fail-close），"
                            "不会复用上一任务的结果。\n"
                            f"reason={dispatch_reject_reason}\n"
                            "请直接发送新的完整问题重试。"
                        ),
                        inbound=inbound,
                        tag="dispatch_rejected_fail_close",
                    )
                    return
                if pane_fallback_reply and int(args.progress_push_count) <= 0:
                    reply_text = (
                        pane_fallback_reply
                        if len(pane_fallback_reply) <= 1200
                        else f"{pane_fallback_reply[:1200]}..."
                    )
                    self._safe_reply(
                        incoming=incoming,
                        text=(
                            f"question_tag={question_tag} trace_id={trace_id} task_id={task_id} "
                            "final_state=PANE_SIGNAL_FASTPATH\n"
                            f"最终回复（终端回显快速回传）：\n{reply_text}"
                        ),
                        inbound=inbound,
                        tag="dispatch_final_result_fastpath",
                    )
                    self._reply_multimodal_results(
                        incoming=incoming,
                        inbound=inbound,
                        metadata_payload=metadata_payload,
                        final_reply=reply_text,
                        question_tag=question_tag,
                        trace_id=trace_id,
                        task_id=task_id,
                        tag_prefix="dispatch_final_result_fastpath",
                    )
                    return
                last_signature = _push_followup_snapshots(baseline_signature)
                _push_final_result(
                    last_signature,
                    baseline_signature=baseline_signature,
                    pane_fallback_reply=pane_fallback_reply,
                )
            except LeaderCommandTimeout as exc:
                LOGGER.warning(
                    "dispatch_timeout msg_id=%s chat_id=%s sender_id=%s err=%s",
                    inbound.msg_id,
                    inbound.chat_id,
                    inbound.sender_id,
                    str(exc),
                )
                self._safe_reply(
                    incoming=incoming,
                    text=(
                        f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                        "分发回执超时，但任务可能已进入执行队列；继续回推会话状态。"
                    ),
                    inbound=inbound,
                    tag="dispatch_timeout_pending",
                )
                last_signature = _push_followup_snapshots(baseline_signature)
                _push_final_result(last_signature, baseline_signature=baseline_signature)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "dispatch_failed msg_id=%s chat_id=%s sender_id=%s",
                    inbound.msg_id,
                    inbound.chat_id,
                    inbound.sender_id,
                )
                self._safe_reply(
                    incoming=incoming,
                    text=(
                        f"question_tag={question_tag} trace_id={trace_id} task_id={task_id}\n"
                        f"分发失败: {type(exc).__name__}: {exc}"
                    ),
                    inbound=inbound,
                    tag="dispatch_failed",
                )

        def _dispatch_route_snapshot_in_background(self, *, incoming, inbound: InboundChatMessage) -> None:  # noqa: ANN001
            try:
                routes = client.get_leader_snapshot()
                self._safe_reply(
                    incoming=incoming,
                    text=format_leader_snapshot_summary(routes),
                    inbound=inbound,
                    tag="route_snapshot",
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "leader_snapshot_failed msg_id=%s chat_id=%s sender_id=%s",
                    inbound.msg_id,
                    inbound.chat_id,
                    inbound.sender_id,
                )
                try:
                    fallback = client.get_identity_routes()
                    self._safe_reply(
                        incoming=incoming,
                        text=format_routes_summary(fallback),
                        inbound=inbound,
                        tag="route_snapshot_fallback",
                    )
                except Exception as fallback_exc:  # noqa: BLE001
                    LOGGER.exception(
                        "route_snapshot_fallback_failed msg_id=%s chat_id=%s sender_id=%s",
                        inbound.msg_id,
                        inbound.chat_id,
                        inbound.sender_id,
                    )
                    self._safe_reply(
                        incoming=incoming,
                        text=f"查询路由失败: {type(exc).__name__}: {exc}; fallback={type(fallback_exc).__name__}: {fallback_exc}",
                        inbound=inbound,
                        tag="route_snapshot_failed",
                    )

        def _dispatch_approval_command_in_background(
            self,
            *,
            incoming,
            inbound: InboundChatMessage,
            approval_cmd: ApprovalCommand,
        ) -> None:  # noqa: ANN001
            try:
                if approval_cmd.action == "list":
                    queue = client.get_approval_queue(status_filter="PENDING", limit=8)
                    self._safe_reply(
                        incoming=incoming,
                        text=format_approval_queue_summary(queue),
                        inbound=inbound,
                        tag="approval_queue_list",
                    )
                    return

                if approval_cmd.action not in {"approve", "reject"}:
                    self._safe_reply(
                        incoming=incoming,
                        text=f"不支持的审批动作: {approval_cmd.action}",
                        inbound=inbound,
                        tag="approval_cmd_invalid",
                    )
                    return

                token = str(args.trusted_decision_token or "").strip()
                if not token:
                    self._safe_reply(
                        incoming=incoming,
                        text=(
                            "当前未配置 bot 审批密钥，无法执行同意/拒绝。"
                            "请先配置 FQG_BRIDGE_TRUSTED_DECISION_TOKEN。"
                        ),
                        inbound=inbound,
                        tag="approval_cmd_missing_token",
                    )
                    return

                request_id = str(approval_cmd.request_id or "").strip()
                if not request_id:
                    queue = client.get_approval_queue(status_filter="PENDING", limit=1)
                    items = queue.get("items") if isinstance(queue, dict) else []
                    first = items[0] if isinstance(items, list) and items else {}
                    request_id = str((first or {}).get("request_id", "")).strip()
                    if not request_id:
                        self._safe_reply(
                            incoming=incoming,
                            text="当前没有待审批项。",
                            inbound=inbound,
                            tag="approval_cmd_no_pending",
                        )
                        return

                result = client.apply_trusted_approval_decision(
                    request_id=request_id,
                    action=approval_cmd.action,
                    approver=str(args.bridge_approver or "guixianren-bridge").strip() or "guixianren-bridge",
                    bridge_token=token,
                )
                status = str(result.get("status", "")).strip() or "UNKNOWN"
                reason = str(result.get("reason", "")).strip() or "-"
                terminal_action = str(result.get("terminal_action", "")).strip() or "-"

                queue_after = client.get_approval_queue(status_filter="PENDING", limit=5)
                queue_summary = format_approval_queue_summary(queue_after)
                action_text = "同意" if approval_cmd.action == "approve" else "拒绝"
                self._safe_reply(
                    incoming=incoming,
                    text=(
                        f"审批已处理: {action_text} {request_id}\n"
                        f"status={status} terminal_action={terminal_action} reason={reason}\n"
                        f"{queue_summary}"
                    ),
                    inbound=inbound,
                    tag=f"approval_cmd_{approval_cmd.action}",
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "approval_cmd_failed msg_id=%s chat_id=%s sender_id=%s action=%s request_id=%s",
                    inbound.msg_id,
                    inbound.chat_id,
                    inbound.sender_id,
                    approval_cmd.action,
                    approval_cmd.request_id,
                )
                self._safe_reply(
                    incoming=incoming,
                    text=f"审批指令失败: {type(exc).__name__}: {exc}",
                    inbound=inbound,
                    tag="approval_cmd_failed",
                )

        async def process(self, callback):  # noqa: ANN001
            data = callback.data if isinstance(callback.data, dict) else {}
            try:
                self._mark_callback()
                incoming = dingtalk_stream.ChatbotMessage.from_dict(data)  # type: ignore[attr-defined]
                inbound = _build_inbound_message(data, incoming)
                decision = policy.evaluate(inbound)
                LOGGER.info(
                    "inbound msg_id=%s chat_id=%s sender_id=%s is_group=%s is_at=%s accepted=%s reason=%s text_preview=%s",
                    inbound.msg_id,
                    inbound.chat_id,
                    inbound.sender_id,
                    inbound.is_group,
                    inbound.is_at_bot,
                    decision.accepted,
                    decision.reason,
                    decision.normalized_message[:120],
                )
                if not decision.accepted:
                    self._mark_rejected(decision.reason)
                    _maybe_write_heartbeat(force=True)
                    self._reply_in_background(
                        incoming=incoming,
                        text=_reject_text(decision.reason),
                        inbound=inbound,
                        tag=f"reject-{decision.reason}",
                    )
                    return AckMessage.STATUS_OK, "ignored"

                if not dedupe.check_and_mark(inbound.msg_id):
                    self._reply_in_background(
                        incoming=incoming,
                        text="重复消息已忽略。",
                        inbound=inbound,
                        tag="duplicate",
                    )
                    return AckMessage.STATUS_OK, "duplicate"
                self._mark_inbound()
                self._set_active_chat_message(inbound.chat_id, inbound.msg_id)
                _maybe_write_heartbeat(force=True)

                approval_cmd = parse_approval_command(decision.normalized_message)
                if approval_cmd is not None:
                    self._reply_in_background(
                        incoming=incoming,
                        text="已受理审批指令，正在处理。",
                        inbound=inbound,
                        tag="approval_cmd_accepted",
                    )
                    approval_worker = threading.Thread(
                        target=self._dispatch_approval_command_in_background,
                        kwargs={
                            "incoming": incoming,
                            "inbound": inbound,
                            "approval_cmd": approval_cmd,
                        },
                        daemon=True,
                        name=f"fqg-approval-{inbound.msg_id[:10] or 'msg'}",
                    )
                    approval_worker.start()
                    return AckMessage.STATUS_OK, "approval_queued"

                if is_route_query_command(decision.normalized_message):
                    self._reply_in_background(
                        incoming=incoming,
                        text="已受理，正在查询路由状态。",
                        inbound=inbound,
                        tag="route_query_accepted",
                    )
                    route_worker = threading.Thread(
                        target=self._dispatch_route_snapshot_in_background,
                        kwargs={
                            "incoming": incoming,
                            "inbound": inbound,
                        },
                        daemon=True,
                        name=f"fqg-route-{inbound.msg_id[:10] or 'msg'}",
                    )
                    route_worker.start()
                    return AckMessage.STATUS_OK, "route_queued"

                worker = threading.Thread(
                    target=self._dispatch_in_background,
                    kwargs={
                        "incoming": incoming,
                        "data": data,
                        "inbound": inbound,
                        "normalized_message": decision.normalized_message,
                    },
                    daemon=True,
                    name=f"fqg-bridge-{inbound.msg_id[:10] or 'msg'}",
                )
                worker.start()
                return AckMessage.STATUS_OK, "queued"
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "process_failed msg_id=%s chat_id=%s sender_id=%s err=%s",
                    str(data.get("msgId") or data.get("messageId") or "").strip(),
                    str(data.get("conversationId") or "").strip(),
                    str(data.get("senderStaffId") or data.get("senderId") or "").strip(),
                    type(exc).__name__,
                )
                return AckMessage.STATUS_OK, f"error:{type(exc).__name__}"

    credential = dingtalk_stream.Credential(args.client_id, args.client_secret)  # type: ignore[attr-defined]
    stream_client = dingtalk_stream.DingTalkStreamClient(credential)  # type: ignore[attr-defined]
    handler = _BridgeHandler()
    heartbeat_file_value = str(
        getattr(
            args,
            "heartbeat_file",
            ROOT_DIR / ".runtime" / "local_bridge" / "bridge_heartbeat.json",
        )
    ).strip()
    heartbeat_path = Path(heartbeat_file_value or str(ROOT_DIR / ".runtime" / "local_bridge" / "bridge_heartbeat.json")).expanduser()
    heartbeat_interval = max(
        0.5,
        float(getattr(args, "heartbeat_write_interval_seconds", 5.0)),
    )
    heartbeat_state: dict[str, float] = {"last_emit_at": 0.0}
    heartbeat_lock = threading.Lock()

    def _write_heartbeat(*, now_monotonic: float) -> None:
        now_epoch = time.time()
        payload = _build_bridge_heartbeat_payload(
            now_monotonic=now_monotonic,
            now_epoch=now_epoch,
            activity_snapshot=handler.activity_snapshot(),
            base_url=args.base_url,
        )
        with heartbeat_lock:
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = heartbeat_path.with_name(
                f"{heartbeat_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(heartbeat_path)
            heartbeat_state["last_emit_at"] = now_monotonic

    def _maybe_write_heartbeat(*, force: bool = False) -> None:
        now = time.monotonic()
        last_emit = float(heartbeat_state.get("last_emit_at", 0.0))
        if (not force) and (now - last_emit < heartbeat_interval):
            return
        try:
            _write_heartbeat(now_monotonic=now)
        except Exception as heartbeat_exc:  # noqa: BLE001
            LOGGER.warning("heartbeat_write_failed err=%s", type(heartbeat_exc).__name__)

    stream_client.register_callback_handler(  # type: ignore[attr-defined]
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC,  # type: ignore[attr-defined]
        handler,
    )

    def _watchdog_loop() -> None:
        idle_limit = max(0.0, float(args.activity_idle_restart_seconds))
        max_uptime = max(0.0, float(args.force_restart_max_uptime_seconds))
        check_interval = max(1.0, float(args.watchdog_check_interval_seconds))
        grace = max(0.0, float(args.watchdog_grace_seconds))
        while True:
            time.sleep(check_interval)
            now = time.monotonic()
            snap = handler.activity_snapshot()
            _maybe_write_heartbeat()
            idle_watchdog = _build_idle_watchdog_snapshot(snap, now_monotonic=now)
            uptime = float(idle_watchdog.get("uptime_seconds", 0.0) or 0.0)
            if uptime < grace:
                continue
            idle_seconds = float(idle_watchdog.get("idle_seconds", 0.0) or 0.0)
            last_callback_at = float(idle_watchdog.get("last_callback_at", now))
            last_inbound_at = float(idle_watchdog.get("last_inbound_at", now))
            last_reply_at = float(idle_watchdog.get("last_reply_at", now))
            callback_count = int(idle_watchdog.get("callback_count", 0) or 0)
            inbound_count = int(idle_watchdog.get("inbound_count", 0) or 0)
            reply_count = int(idle_watchdog.get("reply_count", 0) or 0)
            activity_seen = bool(idle_watchdog.get("activity_seen", False))

            if max_uptime > 0 and uptime >= max_uptime:
                LOGGER.error(
                    "bridge_watchdog_recycle reason=max_uptime uptime_seconds=%.1f max_uptime_seconds=%.1f",
                    uptime,
                    max_uptime,
                )
                _maybe_write_heartbeat(force=True)
                os._exit(70)

            if idle_limit > 0 and idle_seconds >= idle_limit and activity_seen:
                LOGGER.error(
                    (
                        "bridge_watchdog_recycle reason=idle idle_seconds=%.1f idle_limit_seconds=%.1f "
                        "uptime_seconds=%.1f callback_age=%.1f inbound_age=%.1f reply_age=%.1f "
                        "callback_count=%d inbound_count=%d reply_count=%d"
                    ),
                    idle_seconds,
                    idle_limit,
                    uptime,
                    max(0.0, now - last_callback_at),
                    max(0.0, now - last_inbound_at),
                    max(0.0, now - last_reply_at),
                    callback_count,
                    inbound_count,
                    reply_count,
                )
                _maybe_write_heartbeat(force=True)
                os._exit(71)

    watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        name="fqg-stream-watchdog",
        daemon=True,
    )
    watchdog_thread.start()
    _maybe_write_heartbeat(force=True)
    LOGGER.info(
        (
            "bridge_started base_url=%s require_at=%s require_prefix=%s auto_collab=%s "
            "followup_seconds=%s progress_push_count=%s completion_wait_seconds=%s completion_poll_seconds=%s completion_max_wait_seconds=%s "
            "post_timeout_wait_seconds=%s post_timeout_poll_seconds=%s idle_restart_seconds=%s "
            "max_uptime_seconds=%s watchdog_check_interval_seconds=%s watchdog_grace_seconds=%s "
            "reasoning_loop_on_settled=%s reasoning_level=%s reasoning_max_attempts=%s reasoning_min_seconds=%s "
            "reasoning_mandatory_fields=%s reasoning_require_next_action=%s identity_current_task_path=%s "
            "reply_retry_attempts=%s reply_retry_base_delay_seconds=%s reply_retry_max_delay_seconds=%s "
            "enable_multimodal_reply=%s multimodal_reply_max_items=%s "
            "trusted_decision_enabled=%s bridge_approver=%s heartbeat_file=%s heartbeat_interval_seconds=%s"
        ),
        args.base_url,
        not args.no_require_at,
        args.require_prefix,
        bool(args.auto_collab),
        args.followup_seconds,
        args.progress_push_count,
        args.completion_wait_seconds,
        args.completion_poll_seconds,
        args.completion_max_wait_seconds,
        args.post_timeout_final_wait_seconds,
        args.post_timeout_poll_seconds,
        args.activity_idle_restart_seconds,
        args.force_restart_max_uptime_seconds,
        args.watchdog_check_interval_seconds,
        args.watchdog_grace_seconds,
        bool(args.reasoning_loop_on_settled),
        str(args.reasoning_level or "L3"),
        int(args.reasoning_max_attempts),
        float(args.reasoning_min_seconds),
        ",".join(str(item) for item in list(args.reasoning_mandatory_fields)),
        bool(args.reasoning_require_next_action),
        str(args.identity_current_task_path or ""),
        args.reply_retry_attempts,
        args.reply_retry_base_delay_seconds,
        args.reply_retry_max_delay_seconds,
        bool(args.enable_multimodal_reply),
        int(args.multimodal_reply_max_items),
        bool(str(args.trusted_decision_token or "").strip()),
        str(args.bridge_approver or "").strip() or "guixianren-bridge",
        str(heartbeat_path),
        heartbeat_interval,
    )
    stream_client.start_forever()  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
