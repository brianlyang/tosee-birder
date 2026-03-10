from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import AuditStore
from .config import Settings
from .models import (
    ApprovalQueueItem,
    ApprovalQueueResponse,
    ApprovalStatus,
    ApprovalStatusResponse,
    ApprovalVote,
    ApprovalVotesResponse,
    CreateApprovalRequest,
    CreateApprovalResponse,
    DecisionCallbackRequest,
    DecisionResponse,
    RiskLevel,
)
from .policy import evaluate_command
from .storage import ApprovalStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CreatedApproval:
    response: CreateApprovalResponse
    command: str
    terminal_session_id: str
    extra_context: dict[str, Any]


class ApprovalService:
    def __init__(self, settings: Settings, store: ApprovalStore, audit_store: AuditStore) -> None:
        self._settings = settings
        self._store = store
        self._audit = audit_store

    def create_approval(self, req: CreateApprovalRequest) -> CreatedApproval:
        policy = evaluate_command(req.command)
        now = _now()
        expires_at = now + timedelta(seconds=self._settings.timeout_seconds)
        request_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(24)

        if policy.reason == "blocked_high_risk_command":
            status = ApprovalStatus.REJECTED
            reason = "blocked_high_risk_command"
            token_hash = None
            callback_token = None
            terminal_action = "ESC"
        else:
            status = ApprovalStatus.PENDING
            if self._settings.enable_high_risk_dual_approval and policy.risk_level is RiskLevel.L3:
                reason = "awaiting_first_approval_high_risk"
            else:
                reason = policy.reason
            token_hash = _token_hash(token)
            callback_token = token
            terminal_action = None

        self._store.insert_approval(
            {
                "request_id": request_id,
                "command": req.command,
                "terminal_session_id": req.terminal_session_id,
                "source": req.source,
                "status": status.value,
                "risk_level": policy.risk_level.value,
                "reason": reason,
                "token_hash": token_hash,
                "created_at": now,
                "expires_at": expires_at,
            }
        )

        if status is ApprovalStatus.REJECTED:
            self._store.set_decision(
                request_id,
                status=status.value,
                reason=reason,
                decision_at=now,
                approver="policy-engine",
                terminal_action=terminal_action,
                source_ip="local",
                signature_valid=True,
            )

        self._audit.append(
            "approval_created",
            {
                "request_id": request_id,
                "command": req.command,
                "terminal_session_id": req.terminal_session_id,
                "source": req.source,
                "status": status.value,
                "risk_level": policy.risk_level.value,
                "reason": reason,
                "expires_at": expires_at.isoformat(),
            },
        )

        response = CreateApprovalResponse(
            request_id=request_id,
            status=status,
            risk_level=policy.risk_level,
            reason=reason,
            callback_token=callback_token,
            expires_at=expires_at,
            poll_url=f"{self._settings.callback_base_url}/v1/approvals/{request_id}",
        )
        return CreatedApproval(
            response=response,
            command=req.command,
            terminal_session_id=req.terminal_session_id,
            extra_context=req.extra_context,
        )

    def reject_pending(
        self,
        request_id: str,
        *,
        reason: str,
        approver: str,
        source_ip: str,
    ) -> bool:
        row = self._store.get_approval(request_id)
        if row is None:
            raise KeyError(request_id)
        if ApprovalStatus(row["status"]) is not ApprovalStatus.PENDING:
            return False

        now = _now()
        self._store.set_decision(
            request_id,
            status=ApprovalStatus.REJECTED.value,
            reason=reason,
            decision_at=now,
            approver=approver,
            terminal_action="ESC",
            source_ip=source_ip,
            signature_valid=False,
        )
        self._audit.append(
            "approval_auto_rejected",
            {
                "request_id": request_id,
                "command": row["command"],
                "status": ApprovalStatus.REJECTED.value,
                "reason": reason,
                "approver": approver,
                "source_ip": source_ip,
            },
        )
        return True

    def get_status(self, request_id: str) -> ApprovalStatusResponse:
        row = self._store.get_approval(request_id)
        if row is None:
            raise KeyError(request_id)
        return ApprovalStatusResponse(
            request_id=row["request_id"],
            terminal_session_id=row["terminal_session_id"],
            status=ApprovalStatus(row["status"]),
            risk_level=RiskLevel(row["risk_level"]),
            command=row["command"],
            reason=row["reason"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            decision_at=row["decision_at"],
            approver=row["approver"],
            terminal_action=row["terminal_action"],
        )

    def get_terminal_session_id(self, request_id: str) -> str:
        row = self._store.get_approval(request_id)
        if row is None:
            raise KeyError(request_id)
        return row["terminal_session_id"]

    def get_votes(self, request_id: str) -> ApprovalVotesResponse:
        row = self._store.get_approval(request_id)
        if row is None:
            raise KeyError(request_id)
        entries = self._store.list_approval_vote_entries(request_id)
        votes = [
            ApprovalVote(
                request_id=e["request_id"],
                approver=e["approver"],
                action=e["action"],
                source_ip=e["source_ip"],
                signature_valid=bool(e["signature_valid"]),
                created_at=e["created_at"],
            )
            for e in entries
        ]
        return ApprovalVotesResponse(request_id=request_id, votes=votes)

    def list_queue(
        self,
        *,
        status_filter: ApprovalStatus | None = ApprovalStatus.PENDING,
        limit: int = 20,
    ) -> ApprovalQueueResponse:
        status_value = status_filter.value if isinstance(status_filter, ApprovalStatus) else None
        rows = self._store.list_approvals(status=status_value, limit=limit)
        items: list[ApprovalQueueItem] = []
        for row in rows:
            is_dual_required = self._is_dual_approval_target(row)
            required_approvals = 2 if is_dual_required else 1
            approvals_received = len(self._store.list_approval_votes(row["request_id"], action="approve"))
            remaining_approvals = max(0, required_approvals - approvals_received)
            command_preview_raw = str(row.get("command", "")).strip()
            command_preview = command_preview_raw if len(command_preview_raw) <= 220 else f"{command_preview_raw[:220]}..."
            items.append(
                ApprovalQueueItem(
                    request_id=str(row["request_id"]),
                    terminal_session_id=str(row["terminal_session_id"]),
                    status=ApprovalStatus(str(row["status"])),
                    risk_level=RiskLevel(str(row["risk_level"])),
                    reason=str(row["reason"]),
                    command_preview=command_preview,
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                    decision_at=row["decision_at"],
                    approver=str(row["approver"]) if row["approver"] else None,
                    terminal_action=str(row["terminal_action"]) if row["terminal_action"] else None,
                    approvals_received=approvals_received,
                    required_approvals=required_approvals,
                    remaining_approvals=remaining_approvals,
                )
            )
        return ApprovalQueueResponse(
            status_filter=status_filter,
            total=len(items),
            items=items,
        )

    def apply_decision(self, req: DecisionCallbackRequest, source_ip: str) -> DecisionResponse:
        row = self._store.get_approval(req.request_id)
        if row is None:
            raise KeyError(req.request_id)

        status = ApprovalStatus(row["status"])
        now = _now()

        if status is not ApprovalStatus.PENDING:
            return DecisionResponse(
                request_id=req.request_id,
                status=status,
                terminal_action=row["terminal_action"] or "",
                reason=f"already_finalized:{row['reason']}",
            )

        if row["expires_at"] <= now:
            self._store.set_decision(
                req.request_id,
                status=ApprovalStatus.EXPIRED.value,
                reason="timeout_auto_reject",
                decision_at=now,
                approver=req.approver,
                terminal_action="ESC",
                source_ip=source_ip,
                signature_valid=False,
            )
            self._audit.append(
                "approval_expired",
                {
                    "request_id": req.request_id,
                    "command": row["command"],
                    "approver": req.approver,
                    "source_ip": source_ip,
                },
            )
            return DecisionResponse(
                request_id=req.request_id,
                status=ApprovalStatus.EXPIRED,
                terminal_action="ESC",
                reason="timeout_auto_reject",
            )

        if not self._store.try_record_nonce(req.nonce, req.request_id):
            raise ValueError("nonce_replayed")

        if row["token_hash"] != _token_hash(req.token):
            raise ValueError("invalid_token")

        signature_valid = self._verify_callback_signature(req)
        if not signature_valid:
            raise ValueError("invalid_signature")

        if self._settings.approver_allowlist and req.approver not in self._settings.approver_allowlist:
            raise ValueError("approver_not_allowlisted")

        pending_response, second_step = self._maybe_handle_high_risk_dual_approval_step(
            row=row,
            request_id=req.request_id,
            action=req.action,
            approver=req.approver,
            source_ip=source_ip,
            signature_valid=signature_valid,
            first_event_name="approval_high_risk_first_approved",
            duplicate_event_name="approval_high_risk_duplicate_vote",
        )
        if pending_response is not None:
            return pending_response

        return self._finalize_decision(
            request_id=req.request_id,
            action=req.action,
            approver=req.approver,
            source_ip=source_ip,
            signature_valid=signature_valid,
            created_at=row["created_at"],
            command=row["command"],
            event_name="approval_high_risk_second_approved" if second_step else "approval_decided",
            reason_prefix="callback_dual_second" if second_step else "callback",
        )

    def apply_decision_by_link(
        self,
        *,
        request_id: str,
        action: str,
        token: str,
        approver: str,
        source_ip: str,
    ) -> DecisionResponse:
        row = self._store.get_approval(request_id)
        if row is None:
            raise KeyError(request_id)

        status = ApprovalStatus(row["status"])
        now = _now()
        if status is not ApprovalStatus.PENDING:
            return DecisionResponse(
                request_id=request_id,
                status=status,
                terminal_action=row["terminal_action"] or "",
                reason=f"already_finalized:{row['reason']}",
            )

        if row["expires_at"] <= now:
            self._store.set_decision(
                request_id,
                status=ApprovalStatus.EXPIRED.value,
                reason="timeout_auto_reject",
                decision_at=now,
                approver=approver,
                terminal_action="ESC",
                source_ip=source_ip,
                signature_valid=False,
            )
            self._audit.append(
                "approval_expired",
                {
                    "request_id": request_id,
                    "command": row["command"],
                    "approver": approver,
                    "source_ip": source_ip,
                },
            )
            return DecisionResponse(
                request_id=request_id,
                status=ApprovalStatus.EXPIRED,
                terminal_action="ESC",
                reason="timeout_auto_reject",
            )

        if row["token_hash"] != _token_hash(token):
            raise ValueError("invalid_token")

        if self._settings.approver_allowlist and approver not in self._settings.approver_allowlist:
            raise ValueError("approver_not_allowlisted")

        pending_response, second_step = self._maybe_handle_high_risk_dual_approval_step(
            row=row,
            request_id=request_id,
            action=action,
            approver=approver,
            source_ip=source_ip,
            signature_valid=False,
            first_event_name="approval_high_risk_first_approved_by_link",
            duplicate_event_name="approval_high_risk_duplicate_vote_by_link",
        )
        if pending_response is not None:
            return pending_response

        return self._finalize_decision(
            request_id=request_id,
            action=action,
            approver=approver,
            source_ip=source_ip,
            signature_valid=False,
            created_at=row["created_at"],
            command=row["command"],
            event_name="approval_high_risk_second_approved_by_link" if second_step else "approval_decided_by_link",
            reason_prefix="decision_link_dual_second" if second_step else "decision_link",
        )

    def apply_decision_trusted(
        self,
        *,
        request_id: str,
        action: str,
        approver: str,
        source_ip: str,
    ) -> DecisionResponse:
        row = self._store.get_approval(request_id)
        if row is None:
            raise KeyError(request_id)

        status = ApprovalStatus(row["status"])
        now = _now()
        if status is not ApprovalStatus.PENDING:
            return DecisionResponse(
                request_id=request_id,
                status=status,
                terminal_action=row["terminal_action"] or "",
                reason=f"already_finalized:{row['reason']}",
            )

        if row["expires_at"] <= now:
            self._store.set_decision(
                request_id,
                status=ApprovalStatus.EXPIRED.value,
                reason="timeout_auto_reject",
                decision_at=now,
                approver=approver,
                terminal_action="ESC",
                source_ip=source_ip,
                signature_valid=False,
            )
            self._audit.append(
                "approval_expired",
                {
                    "request_id": request_id,
                    "command": row["command"],
                    "approver": approver,
                    "source_ip": source_ip,
                },
            )
            return DecisionResponse(
                request_id=request_id,
                status=ApprovalStatus.EXPIRED,
                terminal_action="ESC",
                reason="timeout_auto_reject",
            )

        if self._settings.approver_allowlist and approver not in self._settings.approver_allowlist:
            raise ValueError("approver_not_allowlisted")

        pending_response, second_step = self._maybe_handle_high_risk_dual_approval_step(
            row=row,
            request_id=request_id,
            action=action,
            approver=approver,
            source_ip=source_ip,
            signature_valid=False,
            first_event_name="approval_high_risk_first_approved_by_trusted_bridge",
            duplicate_event_name="approval_high_risk_duplicate_vote_by_trusted_bridge",
        )
        if pending_response is not None:
            return pending_response

        return self._finalize_decision(
            request_id=request_id,
            action=action,
            approver=approver,
            source_ip=source_ip,
            signature_valid=False,
            created_at=row["created_at"],
            command=row["command"],
            event_name=(
                "approval_high_risk_second_approved_by_trusted_bridge"
                if second_step
                else "approval_decided_by_trusted_bridge"
            ),
            reason_prefix="trusted_bridge_dual_second" if second_step else "trusted_bridge",
        )

    def expire_pending(self) -> list[str]:
        now = _now()
        expired_ids = self._store.expire_due(now)
        for request_id in expired_ids:
            row = self._store.get_approval(request_id)
            if row is None:
                continue
            self._audit.append(
                "approval_expired",
                {
                    "request_id": request_id,
                    "command": row["command"],
                    "status": ApprovalStatus.EXPIRED.value,
                    "reason": "timeout_auto_reject",
                    "source_ip": "system",
                },
            )
        return expired_ids

    def _verify_callback_signature(self, req: DecisionCallbackRequest) -> bool:
        if self._settings.bypass_signature_verification:
            return True
        if not self._settings.callback_signing_secret:
            return False

        now_ts = int(time.time())
        if abs(now_ts - req.timestamp) > self._settings.callback_max_skew_seconds:
            return False

        msg = "\n".join(
            [
                req.request_id,
                req.action,
                req.token,
                req.nonce,
                req.approver,
                str(req.timestamp),
            ]
        )
        digest = hmac.new(
            self._settings.callback_signing_secret.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(digest, req.signature)

    def _finalize_decision(
        self,
        *,
        request_id: str,
        action: str,
        approver: str,
        source_ip: str,
        signature_valid: bool,
        created_at: datetime,
        command: str,
        event_name: str,
        reason_prefix: str,
    ) -> DecisionResponse:
        decision_status = ApprovalStatus.APPROVED if action == "approve" else ApprovalStatus.REJECTED
        terminal_action = "ENTER" if action == "approve" else "ESC"
        reason = f"{action}_by_{reason_prefix}"

        self._store.set_decision(
            request_id,
            status=decision_status.value,
            reason=reason,
            decision_at=_now(),
            approver=approver,
            terminal_action=terminal_action,
            source_ip=source_ip,
            signature_valid=signature_valid,
        )

        self._audit.append(
            event_name,
            {
                "request_id": request_id,
                "command": command,
                "status": decision_status.value,
                "terminal_action": terminal_action,
                "approver": approver,
                "source_ip": source_ip,
                "signature_valid": signature_valid,
                "latency_ms": int((_now() - created_at).total_seconds() * 1000),
            },
        )

        return DecisionResponse(
            request_id=request_id,
            status=decision_status,
            terminal_action=terminal_action,
            reason=reason,
        )

    def _is_dual_approval_target(self, row: dict[str, Any]) -> bool:
        if not self._settings.enable_high_risk_dual_approval:
            return False
        if row.get("reason") == "blocked_high_risk_command":
            return False
        return row.get("risk_level") == RiskLevel.L3.value

    def _maybe_handle_high_risk_dual_approval_step(
        self,
        *,
        row: dict[str, Any],
        request_id: str,
        action: str,
        approver: str,
        source_ip: str,
        signature_valid: bool,
        first_event_name: str,
        duplicate_event_name: str,
    ) -> tuple[DecisionResponse | None, bool]:
        if action != "approve" or not self._is_dual_approval_target(row):
            return None, False

        is_new_vote = self._store.record_approval_vote(
            request_id=request_id,
            approver=approver,
            action="approve",
            source_ip=source_ip,
            signature_valid=signature_valid,
        )
        approvers = self._store.list_approval_votes(request_id, action="approve")
        vote_count = len(approvers)
        if vote_count < 2:
            if not approvers:
                raise ValueError("approval_vote_missing")

            first_approver = approvers[0]
            if is_new_vote:
                event_name = first_event_name
            else:
                event_name = duplicate_event_name

            self._store.set_decision(
                request_id,
                status=ApprovalStatus.PENDING.value,
                reason="awaiting_second_approval_high_risk",
                decision_at=_now(),
                approver=first_approver,
                terminal_action=None,
                source_ip=source_ip,
                signature_valid=signature_valid,
            )
            self._audit.append(
                event_name,
                {
                    "request_id": request_id,
                    "command": row["command"],
                    "status": ApprovalStatus.PENDING.value,
                    "approver": approver,
                    "source_ip": source_ip,
                    "signature_valid": signature_valid,
                    "vote_count": vote_count,
                    "is_new_vote": is_new_vote,
                },
            )
            return (
                DecisionResponse(
                    request_id=request_id,
                    status=ApprovalStatus.PENDING,
                    terminal_action="WAIT",
                    reason="awaiting_second_approval_high_risk",
                ),
                False,
            )

        return None, True
