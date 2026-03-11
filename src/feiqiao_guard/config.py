from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    except ValueError as exc:
        raise ValueError(f"invalid integer env {name}={raw!r}") from exc


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    timeout_seconds: int
    auto_expire_poll_interval_seconds: int
    callback_max_skew_seconds: int
    callback_base_url: str
    callback_signing_secret: str
    default_approver: str
    dingtalk_webhook_url: str
    dingtalk_signing_secret: str
    lark_webhook_url: str
    lark_signing_secret: str
    audit_log_path: Path
    sqlite_path: Path
    approver_allowlist: list[str]
    bypass_signature_verification: bool
    enable_high_risk_dual_approval: bool
    identity_routes_path: Path = Path(".runtime/identity_routes.json")
    chat_control_timeout_seconds: int = 20
    chat_default_verify_seconds: int = 8
    chat_queue_retry_fail_close_unconfirmed: bool = False
    chat_leader_identity_id: str = "feiqiao-guard-delivery-lead"
    chat_collab_identity_id: str = "feiqiao-guard-collab-executor"
    dingtalk_paused_webhook_tokens: list[str] | None = None
    identity_memory_enabled: bool = True
    identity_memory_root: Path = Path(".runtime/identity_memory")
    identity_memory_window_size: int = 60
    identity_memory_fresh_size: int = 20
    identity_memory_stable_size: int = 20
    identity_memory_archive_size: int = 20
    identity_memory_refresh_stride: int = 5
    identity_memory_capture_snapshot_reply: bool = False
    trusted_bridge_decision_token: str = ""
    chat_task_retention_seconds: int = 86400
    chat_task_max_items: int = 1000


def load_settings() -> Settings:
    return Settings(
        host=os.getenv("FQG_HOST", "127.0.0.1"),
        port=_env_int("FQG_PORT", 8765),
        timeout_seconds=_env_int("FQG_TIMEOUT_SECONDS", 120),
        auto_expire_poll_interval_seconds=_env_int("FQG_AUTO_EXPIRE_POLL_INTERVAL_SECONDS", 15),
        callback_max_skew_seconds=_env_int("FQG_CALLBACK_MAX_SKEW_SECONDS", 300),
        callback_base_url=os.getenv("FQG_CALLBACK_BASE_URL", "http://127.0.0.1:8765"),
        callback_signing_secret=os.getenv("FQG_CALLBACK_SIGNING_SECRET", ""),
        default_approver=os.getenv("FQG_DEFAULT_APPROVER", "approver"),
        dingtalk_webhook_url=os.getenv("FQG_DINGTALK_WEBHOOK_URL", ""),
        dingtalk_signing_secret=os.getenv("FQG_DINGTALK_SIGNING_SECRET", ""),
        lark_webhook_url=os.getenv("FQG_LARK_WEBHOOK_URL", ""),
        lark_signing_secret=os.getenv("FQG_LARK_SIGNING_SECRET", ""),
        audit_log_path=Path(os.getenv("FQG_AUDIT_LOG_PATH", "resource/reports/approval_audit.jsonl")),
        sqlite_path=Path(os.getenv("FQG_SQLITE_PATH", "resource/reports/approval_gateway.db")),
        approver_allowlist=_env_list("FQG_APPROVER_ALLOWLIST"),
        bypass_signature_verification=_env_bool("FQG_BYPASS_SIGNATURE_VERIFICATION", False),
        enable_high_risk_dual_approval=_env_bool("FQG_ENABLE_HIGH_RISK_DUAL_APPROVAL", False),
        identity_routes_path=Path(os.getenv("FQG_IDENTITY_ROUTES_PATH", ".runtime/identity_routes.json")),
        chat_control_timeout_seconds=_env_int("FQG_CHAT_CONTROL_TIMEOUT_SECONDS", 20),
        chat_default_verify_seconds=_env_int("FQG_CHAT_DEFAULT_VERIFY_SECONDS", 8),
        chat_queue_retry_fail_close_unconfirmed=_env_bool(
            "FQG_CHAT_QUEUE_RETRY_FAIL_CLOSE_UNCONFIRMED",
            False,
        ),
        chat_leader_identity_id=os.getenv("FQG_CHAT_LEADER_IDENTITY_ID", "feiqiao-guard-delivery-lead"),
        chat_collab_identity_id=os.getenv("FQG_CHAT_COLLAB_IDENTITY_ID", "feiqiao-guard-collab-executor"),
        dingtalk_paused_webhook_tokens=_env_list("FQG_DINGTALK_PAUSED_WEBHOOK_TOKENS"),
        identity_memory_enabled=_env_bool("FQG_IDENTITY_MEMORY_ENABLED", True),
        identity_memory_root=Path(os.getenv("FQG_IDENTITY_MEMORY_ROOT", ".runtime/identity_memory")),
        identity_memory_window_size=max(1, _env_int("FQG_IDENTITY_MEMORY_WINDOW_SIZE", 60)),
        identity_memory_fresh_size=max(0, _env_int("FQG_IDENTITY_MEMORY_FRESH_SIZE", 20)),
        identity_memory_stable_size=max(0, _env_int("FQG_IDENTITY_MEMORY_STABLE_SIZE", 20)),
        identity_memory_archive_size=max(0, _env_int("FQG_IDENTITY_MEMORY_ARCHIVE_SIZE", 20)),
        identity_memory_refresh_stride=max(1, _env_int("FQG_IDENTITY_MEMORY_REFRESH_STRIDE", 5)),
        identity_memory_capture_snapshot_reply=_env_bool(
            "FQG_IDENTITY_MEMORY_CAPTURE_SNAPSHOT_REPLY",
            False,
        ),
        trusted_bridge_decision_token=os.getenv("FQG_TRUSTED_BRIDGE_DECISION_TOKEN", "").strip(),
        chat_task_retention_seconds=max(60, _env_int("FQG_CHAT_TASK_RETENTION_SECONDS", 86400)),
        chat_task_max_items=max(100, _env_int("FQG_CHAT_TASK_MAX_ITEMS", 1000)),
    )
