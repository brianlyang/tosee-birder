from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text)


class ApprovalStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path.expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS approvals (
                    request_id TEXT PRIMARY KEY,
                    command TEXT NOT NULL,
                    terminal_session_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    token_hash TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decision_at TEXT,
                    approver TEXT,
                    terminal_action TEXT,
                    source_ip TEXT,
                    signature_valid INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS used_nonces (
                    nonce TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_votes (
                    request_id TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    action TEXT NOT NULL,
                    source_ip TEXT NOT NULL,
                    signature_valid INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (request_id, approver)
                );
                """
            )
            conn.commit()

    def insert_approval(self, row: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals(
                    request_id, command, terminal_session_id, source, status,
                    risk_level, reason, token_hash, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["request_id"],
                    row["command"],
                    row["terminal_session_id"],
                    row["source"],
                    row["status"],
                    row["risk_level"],
                    row["reason"],
                    row.get("token_hash"),
                    _to_iso(row["created_at"]),
                    _to_iso(row["expires_at"]),
                ),
            )
            conn.commit()

    def get_approval(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT request_id, command, terminal_session_id, source, status,
                       risk_level, reason, token_hash, created_at, expires_at,
                       decision_at, approver, terminal_action, source_ip, signature_valid
                FROM approvals
                WHERE request_id = ?
                """,
                (request_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "command": row["command"],
            "terminal_session_id": row["terminal_session_id"],
            "source": row["source"],
            "status": row["status"],
            "risk_level": row["risk_level"],
            "reason": row["reason"],
            "token_hash": row["token_hash"],
            "created_at": _from_iso(row["created_at"]),
            "expires_at": _from_iso(row["expires_at"]),
            "decision_at": _from_iso(row["decision_at"]),
            "approver": row["approver"],
            "terminal_action": row["terminal_action"],
            "source_ip": row["source_ip"],
            "signature_valid": bool(row["signature_valid"]),
        }

    def set_decision(
        self,
        request_id: str,
        *,
        status: str,
        reason: str,
        decision_at: datetime,
        approver: str | None,
        terminal_action: str | None,
        source_ip: str | None,
        signature_valid: bool,
    ) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE approvals
                SET status = ?,
                    reason = ?,
                    decision_at = ?,
                    approver = ?,
                    terminal_action = ?,
                    source_ip = ?,
                    signature_valid = ?
                WHERE request_id = ?
                """,
                (
                    status,
                    reason,
                    _to_iso(decision_at),
                    approver,
                    terminal_action,
                    source_ip,
                    1 if signature_valid else 0,
                    request_id,
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def try_record_nonce(self, nonce: str, request_id: str) -> bool:
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO used_nonces(nonce, request_id, created_at) VALUES (?, ?, ?)",
                    (nonce, request_id, _to_iso(_utcnow())),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def record_approval_vote(
        self,
        *,
        request_id: str,
        approver: str,
        action: str,
        source_ip: str,
        signature_valid: bool,
    ) -> bool:
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO approval_votes(
                        request_id, approver, action, source_ip, signature_valid, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        approver,
                        action,
                        source_ip,
                        1 if signature_valid else 0,
                        _to_iso(_utcnow()),
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def list_approval_votes(self, request_id: str, *, action: str = "approve") -> list[str]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT approver
                FROM approval_votes
                WHERE request_id = ? AND action = ?
                ORDER BY created_at ASC
                """,
                (request_id, action),
            )
            rows = cur.fetchall()
        return [str(r["approver"]) for r in rows]

    def list_approval_vote_entries(self, request_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT request_id, approver, action, source_ip, signature_valid, created_at
                FROM approval_votes
                WHERE request_id = ?
                ORDER BY created_at ASC, approver ASC
                """,
                (request_id,),
            )
            rows = cur.fetchall()
        return [
            {
                "request_id": str(r["request_id"]),
                "approver": str(r["approver"]),
                "action": str(r["action"]),
                "source_ip": str(r["source_ip"]),
                "signature_valid": bool(r["signature_valid"]),
                "created_at": _from_iso(r["created_at"]),
            }
            for r in rows
        ]

    def list_approvals(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        resolved_limit = max(1, int(limit))
        resolved_limit = min(resolved_limit, 200)
        with self._connect() as conn:
            if status:
                cur = conn.execute(
                    """
                    SELECT request_id, command, terminal_session_id, source, status,
                           risk_level, reason, token_hash, created_at, expires_at,
                           decision_at, approver, terminal_action, source_ip, signature_valid
                    FROM approvals
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (status, resolved_limit),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT request_id, command, terminal_session_id, source, status,
                           risk_level, reason, token_hash, created_at, expires_at,
                           decision_at, approver, terminal_action, source_ip, signature_valid
                    FROM approvals
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (resolved_limit,),
                )
            rows = cur.fetchall()
        return [
            {
                "request_id": row["request_id"],
                "command": row["command"],
                "terminal_session_id": row["terminal_session_id"],
                "source": row["source"],
                "status": row["status"],
                "risk_level": row["risk_level"],
                "reason": row["reason"],
                "token_hash": row["token_hash"],
                "created_at": _from_iso(row["created_at"]),
                "expires_at": _from_iso(row["expires_at"]),
                "decision_at": _from_iso(row["decision_at"]),
                "approver": row["approver"],
                "terminal_action": row["terminal_action"],
                "source_ip": row["source_ip"],
                "signature_valid": bool(row["signature_valid"]),
            }
            for row in rows
        ]

    def expire_due(self, now: datetime) -> list[str]:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                SELECT request_id FROM approvals
                WHERE status = 'PENDING' AND expires_at < ?
                """,
                (_to_iso(now),),
            )
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                return []
            conn.execute(
                """
                UPDATE approvals
                SET status = 'EXPIRED',
                    reason = 'timeout_auto_reject',
                    decision_at = ?,
                    terminal_action = 'ESC'
                WHERE status = 'PENDING' AND expires_at < ?
                """,
                (_to_iso(now), _to_iso(now)),
            )
            conn.commit()
            return ids
