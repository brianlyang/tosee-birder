from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class RiskLevel(str, Enum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class CreateApprovalRequest(BaseModel):
    command: str = Field(min_length=1)
    terminal_session_id: str = Field(min_length=1)
    source: str = Field(default="codex_wrapper", min_length=1)
    extra_context: dict[str, Any] = Field(default_factory=dict)


class CreateApprovalResponse(BaseModel):
    request_id: str
    status: ApprovalStatus
    risk_level: RiskLevel
    reason: str
    callback_token: str | None = None
    approve_url: str | None = None
    reject_url: str | None = None
    expires_at: datetime | None = None
    poll_url: str | None = None


class DecisionCallbackRequest(BaseModel):
    request_id: str = Field(min_length=1)
    action: str = Field(pattern="^(approve|reject)$")
    token: str = Field(min_length=1)
    nonce: str = Field(min_length=1)
    approver: str = Field(min_length=1)
    timestamp: int
    signature: str = Field(default="")
    source_ip: str = Field(default="")
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class TrustedDecisionRequest(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    approver: str = Field(min_length=1)
    source_ip: str = Field(default="")


class DecisionResponse(BaseModel):
    request_id: str
    status: ApprovalStatus
    terminal_action: str
    reason: str


class ApprovalStatusResponse(BaseModel):
    request_id: str
    terminal_session_id: str
    status: ApprovalStatus
    risk_level: RiskLevel
    command: str
    reason: str
    created_at: datetime
    expires_at: datetime
    decision_at: datetime | None = None
    approver: str | None = None
    terminal_action: str | None = None


class ApprovalVote(BaseModel):
    request_id: str
    approver: str
    action: str
    source_ip: str
    signature_valid: bool
    created_at: datetime


class ApprovalVotesResponse(BaseModel):
    request_id: str
    votes: list[ApprovalVote]


class ChatInboundRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    identity_id: str | None = None
    session_id: str | None = None
    codex_home: str | None = None
    session_name_prefix: str | None = None
    verify_seconds: float | None = Field(default=None, ge=1.0, le=120.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatInboundResponse(BaseModel):
    accepted: bool
    delivery_state: str
    identity_id: str
    route_source: str
    session_id: str | None = None
    codex_home: str | None = None
    session_name_prefix: str
    verify_seconds: float
    control_exit_code: int
    control_result: dict[str, Any] = Field(default_factory=dict)


class LeaderCommandRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    auto_collab: bool = True
    collab_message: str | None = None
    verify_seconds: float | None = Field(default=None, ge=1.0, le=120.0)
    collab_verify_seconds: float | None = Field(default=None, ge=1.0, le=120.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LeaderCommandResponse(BaseModel):
    accepted: bool
    auto_collab: bool
    leader_identity_id: str
    collab_identity_id: str | None = None
    leader_result: ChatInboundResponse
    collab_result: ChatInboundResponse | None = None
    collab_error: str | None = None
    orchestration_notes: list[str] = Field(default_factory=list)


class LeaderTaskEvent(BaseModel):
    at: datetime
    state: str
    note: str | None = None


class LeaderTaskAcceptedResponse(BaseModel):
    accepted: bool
    task_id: str
    state: str
    created_at: datetime
    poll_url: str


class LeaderTaskStatusResponse(BaseModel):
    task_id: str
    state: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    result: LeaderCommandResponse | None = None
    events: list[LeaderTaskEvent] = Field(default_factory=list)


class IdentityRouteItem(BaseModel):
    identity_id: str
    session_id: str | None = None
    codex_home: str | None = None
    session_name_prefix: str
    verify_seconds: float | None = None
    enabled: bool
    allow_shared_session: bool = False
    switch_ack_ref: str | None = None
    route_status: str = "ok"
    route_error: str | None = None


class IdentityRoutesResponse(BaseModel):
    route_config_path: str
    total: int
    items: list[IdentityRouteItem]


class SessionSnapshotItem(BaseModel):
    identity_id: str
    enabled: bool
    session_id: str | None = None
    session_name_prefix: str
    codex_home: str | None = None
    state: str
    idle_seconds: int | None = None
    last_event_type: str | None = None
    last_event_summary: str | None = None
    last_agent_message: str | None = None
    process_probe_ok: bool | None = None
    error: str | None = None


class LeaderSessionSnapshotResponse(BaseModel):
    generated_at: datetime
    total: int
    items: list[SessionSnapshotItem]


class ApprovalQueueItem(BaseModel):
    request_id: str
    terminal_session_id: str
    status: ApprovalStatus
    risk_level: RiskLevel
    reason: str
    command_preview: str
    created_at: datetime
    expires_at: datetime
    decision_at: datetime | None = None
    approver: str | None = None
    terminal_action: str | None = None
    approvals_received: int
    required_approvals: int
    remaining_approvals: int


class ApprovalQueueResponse(BaseModel):
    status_filter: ApprovalStatus | None = None
    total: int
    items: list[ApprovalQueueItem]
