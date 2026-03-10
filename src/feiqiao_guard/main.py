from __future__ import annotations

import argparse
import asyncio
import json
import html
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .approval_service import ApprovalService
from .audit import AuditStore
from .config import Settings, load_settings
from .dingtalk_client import DingTalkClient
from .identity_memory import IdentityMemoryStore
from .identity_router import IdentityRouteTable
from .leader_task_runtime import LeaderTaskRuntime
from .lark_client import LarkClient
from .models import (
    ApprovalQueueResponse,
    ApprovalStatus,
    ApprovalStatusResponse,
    ApprovalVotesResponse,
    ChatInboundRequest,
    ChatInboundResponse,
    CreateApprovalRequest,
    CreateApprovalResponse,
    DecisionCallbackRequest,
    DecisionResponse,
    IdentityRouteItem,
    IdentityRoutesResponse,
    LeaderTaskAcceptedResponse,
    LeaderTaskStatusResponse,
    LeaderSessionSnapshotResponse,
    TrustedDecisionRequest,
    LeaderCommandRequest,
    LeaderCommandResponse,
    SessionSnapshotItem,
)
from .session_registry import SessionRegistry
from .storage import ApprovalStore

SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    store = ApprovalStore(resolved.sqlite_path)
    audit = AuditStore(resolved.audit_log_path)
    service = ApprovalService(resolved, store, audit)
    dingtalk_client = DingTalkClient(resolved)
    lark_client = LarkClient(resolved)
    sessions = SessionRegistry()
    identity_routes = IdentityRouteTable(resolved.identity_routes_path)
    identity_memory = IdentityMemoryStore(
        root_dir=resolved.identity_memory_root,
        window_size=max(60, int(resolved.identity_memory_window_size)),
        fresh_size=max(0, int(resolved.identity_memory_fresh_size)),
        stable_size=max(0, int(resolved.identity_memory_stable_size)),
        archive_size=max(0, int(resolved.identity_memory_archive_size)),
        refresh_stride=max(1, int(resolved.identity_memory_refresh_stride)),
    )
    leader_tasks = LeaderTaskRuntime(
        retention_seconds=resolved.chat_task_retention_seconds,
        max_items=resolved.chat_task_max_items,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = asyncio.Event()
        expire_task = asyncio.create_task(_expire_loop(service, sessions, resolved, stop_event))
        try:
            yield
        finally:
            stop_event.set()
            await expire_task

    app = FastAPI(title="FeiQiao-Guard", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved
    app.state.store = store
    app.state.audit = audit
    app.state.service = service
    app.state.dingtalk_client = dingtalk_client
    app.state.lark_client = lark_client
    app.state.sessions = sessions
    app.state.identity_routes = identity_routes
    app.state.identity_memory = identity_memory
    app.state.leader_tasks = leader_tasks

    def _append_identity_memory_turn(
        *,
        identity_id: str,
        role: str,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if not resolved.identity_memory_enabled:
            return
        normalized_identity = (identity_id or "").strip()
        if not normalized_identity or normalized_identity == "ad-hoc":
            return
        normalized_text = (text or "").strip()
        if not normalized_text:
            return
        try:
            identity_memory.append_turn(
                identity_id=normalized_identity,
                role=role,
                text=normalized_text,
                metadata=metadata,
            )
        except Exception:
            # Memory trail must never break request handling.
            return

    def _dispatch_chat(
        *,
        message: str,
        identity_id: str | None = None,
        session_id: str | None = None,
        codex_home: str | None = None,
        session_name_prefix: str | None = None,
        verify_seconds: float | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ChatInboundResponse:
        normalized_message = message.strip()
        if not normalized_message:
            raise HTTPException(status_code=400, detail="message_empty")

        requested_identity = (identity_id or "").strip()
        requested_session_id = (session_id or "").strip()
        requested_codex_home = (codex_home or "").strip()
        requested_prefix = (session_name_prefix or "").strip()
        route_source = "request_override"

        route = None
        if requested_identity:
            try:
                route = identity_routes.resolve(requested_identity)
            except ValueError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            if route is None and not requested_session_id and not requested_codex_home:
                raise HTTPException(status_code=404, detail="identity_route_not_found")
            if route is not None:
                route_source = "identity_route"
                if (
                    requested_session_id
                    or requested_codex_home
                    or requested_prefix
                    or verify_seconds is not None
                ):
                    route_source = "identity_route_override"
        elif not requested_session_id and not requested_codex_home:
            raise HTTPException(status_code=400, detail="identity_or_session_or_codex_home_required")

        route_issue = ""
        if route is not None:
            route_issue = str(identity_routes.route_issue(route.identity_id) or "").strip()
            if route_issue:
                audit.append(
                    "chat_inbound_blocked_route_issue",
                    {
                        "identity_id": route.identity_id,
                        "route_source": route_source,
                        "session_id": route.session_id,
                        "codex_home": route.codex_home,
                        "route_issue": route_issue,
                    },
                )
                raise HTTPException(status_code=409, detail=route_issue)

        target_identity = requested_identity or "ad-hoc"
        target_session_id = requested_session_id or (route.session_id if route else "") or ""
        target_codex_home = requested_codex_home or (route.codex_home if route else "") or ""
        target_prefix = requested_prefix or (route.session_name_prefix if route else "") or "fqg"
        target_verify = verify_seconds
        if target_verify is None and route is not None and route.verify_seconds is not None:
            target_verify = route.verify_seconds
        if target_verify is None:
            target_verify = float(resolved.chat_default_verify_seconds)
        target_verify = max(1.0, float(target_verify))

        if not target_session_id and not target_codex_home:
            raise HTTPException(status_code=400, detail="session_or_codex_home_required")

        command = _build_chat_control_command(
            message=normalized_message,
            session_id=target_session_id or None,
            codex_home=target_codex_home or None,
            session_name_prefix=target_prefix,
            verify_seconds=target_verify,
        )

        audit.append(
            "chat_inbound_received",
            {
                "identity_id": target_identity,
                "route_source": route_source,
                "session_id": target_session_id or None,
                "codex_home": target_codex_home or None,
                "session_name_prefix": target_prefix,
                "verify_seconds": target_verify,
                "message_preview": _message_preview(normalized_message),
                "metadata": metadata or {},
            },
        )
        _append_identity_memory_turn(
            identity_id=target_identity,
            role="user",
            text=normalized_message,
            metadata={
                **(metadata or {}),
                "route_source": route_source,
                "entrypoint": str((metadata or {}).get("entrypoint", "chat_inbound")),
                "verify_seconds": target_verify,
            },
        )

        try:
            cp = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(3, int(resolved.chat_control_timeout_seconds)),
            )
        except subprocess.TimeoutExpired as exc:
            audit.append(
                "chat_inbound_timeout",
                {
                    "identity_id": target_identity,
                    "route_source": route_source,
                    "session_id": target_session_id or None,
                    "codex_home": target_codex_home or None,
                    "timeout_seconds": int(resolved.chat_control_timeout_seconds),
                },
            )
            raise HTTPException(status_code=504, detail=f"chat_control_timeout:{exc}") from exc

        parsed_output = _parse_control_output(cp.stdout)
        control_result = (
            parsed_output
            if isinstance(parsed_output, dict)
            else {
                "stdout": cp.stdout.strip(),
                "stderr": cp.stderr.strip(),
            }
        )

        delivery_state = _infer_chat_delivery_state(cp.returncode, control_result)
        accepted = delivery_state != "failed"
        resolved_session_id = str(control_result.get("session_id", target_session_id or "")) or None
        resolved_codex_home = str(control_result.get("codex_home", target_codex_home or "")) or None

        audit.append(
            "chat_inbound_dispatched",
            {
                "identity_id": target_identity,
                "route_source": route_source,
                "session_id": resolved_session_id,
                "codex_home": resolved_codex_home,
                "session_name_prefix": target_prefix,
                "verify_seconds": target_verify,
                "control_exit_code": cp.returncode,
                "accepted": accepted,
                "delivery_state": delivery_state,
                "control_ok": bool(control_result.get("ok", False)),
            },
        )

        snapshot_reply = ""
        if accepted and resolved.identity_memory_capture_snapshot_reply:
            snapshot_item = _build_identity_snapshot_item(identity_routes, target_identity)
            snapshot_reply = str(snapshot_item.last_agent_message or "").strip()

        if snapshot_reply:
            _append_identity_memory_turn(
                identity_id=target_identity,
                role="assistant",
                text=snapshot_reply,
                metadata={
                    **(metadata or {}),
                    "route_source": route_source,
                    "delivery_state": delivery_state,
                    "control_exit_code": cp.returncode,
                },
            )

        _append_identity_memory_turn(
            identity_id=target_identity,
            role="system",
            text=(
                f"dispatch_result accepted={accepted} delivery_state={delivery_state} "
                f"control_exit_code={cp.returncode}"
            ),
            metadata={
                **(metadata or {}),
                "route_source": route_source,
                "delivery_state": delivery_state,
                "control_exit_code": cp.returncode,
            },
        )

        return ChatInboundResponse(
            accepted=accepted,
            delivery_state=delivery_state,
            identity_id=target_identity,
            route_source=route_source,
            session_id=resolved_session_id,
            codex_home=resolved_codex_home,
            session_name_prefix=target_prefix,
            verify_seconds=target_verify,
            control_exit_code=cp.returncode,
            control_result=control_result,
        )

    def _dispatch_chat_with_queued_retry(
        *,
        message: str,
        identity_id: str,
        verify_seconds: float | None,
        metadata: dict[str, object] | None,
        role: str,
    ) -> tuple[ChatInboundResponse, str | None]:
        first = _dispatch_chat(
            message=message,
            identity_id=identity_id,
            verify_seconds=verify_seconds,
            metadata=metadata,
        )
        if first.delivery_state != "queued":
            return first, None

        # New tmux/codex sessions can be created successfully but still miss rollout
        # advancement inside the first short verify window. One bounded auto-continue
        # retry improves confirmation reliability without creating an endless loop.
        retry_text = "继续执行直到任务完成后再汇报结果，不要等待我确认。"
        metadata_payload = metadata or {}
        channel = str(metadata_payload.get("channel", "")).strip().lower()
        is_mobile_bridge = channel == "dingtalk_stream"
        min_retry_verify = 8.0 if is_mobile_bridge else 20.0
        retry_scale = 1.5 if is_mobile_bridge else 2.0
        retry_verify = max(
            min_retry_verify,
            float(verify_seconds or resolved.chat_default_verify_seconds) * retry_scale,
        )
        time.sleep(1.2)
        retried = _dispatch_chat(
            message=retry_text,
            identity_id=identity_id,
            verify_seconds=retry_verify,
            metadata={
                **metadata_payload,
                "entrypoint": "chat_leader_command",
                "role": role,
                "retry_reason": "queued_auto_continue",
            },
        )
        note = (
            f"{role}_queued_retry:first={first.delivery_state}->retry={retried.delivery_state}"
        )
        if retried.delivery_state == "confirmed":
            return retried, note
        if retried.delivery_state == "failed":
            # Fail-close for false-positive queued dispatch:
            # first attempt entered "queued", but bounded retry already proved
            # the command could not be delivered. Surface failure explicitly.
            return retried, note
        if retried.delivery_state == "queued":
            # Fail-close for "silent queued" cases where both attempts were
            # accepted into tmux but still had no rollout advancement proof.
            first_result = first.control_result or {}
            retried_result = retried.control_result or {}
            first_unconfirmed = (
                not bool(first_result.get("ok", False))
                and not bool(first_result.get("rollout_advanced", False))
            )
            retried_unconfirmed = (
                not bool(retried_result.get("ok", False))
                and not bool(retried_result.get("rollout_advanced", False))
            )
            if first_unconfirmed and retried_unconfirmed:
                forced_failed = retried.model_copy(
                    update={
                        "accepted": False,
                        "delivery_state": "failed",
                        "control_result": {
                            **retried_result,
                            "forced_failed_reason": "queued_retry_still_unconfirmed",
                            "first_delivery_state": first.delivery_state,
                            "retry_delivery_state": retried.delivery_state,
                        },
                    }
                )
                return forced_failed, f"{note};forced_failed=queued_retry_still_unconfirmed"
        return first, note

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/chat/routes", response_model=IdentityRoutesResponse)
    def get_identity_routes() -> IdentityRoutesResponse:
        try:
            snapshot = identity_routes.snapshot()
            route_issues = identity_routes.route_issues_snapshot()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        items = [
            IdentityRouteItem(
                **route.to_dict(),
                route_status="error" if route_issues.get(route.identity_id) else "ok",
                route_error=route_issues.get(route.identity_id),
            )
            for route in sorted(snapshot.values(), key=lambda r: r.identity_id)
        ]
        return IdentityRoutesResponse(
            route_config_path=str(identity_routes.route_file),
            total=len(items),
            items=items,
        )

    @app.get("/v1/chat/leader/snapshot", response_model=LeaderSessionSnapshotResponse)
    def get_leader_snapshot() -> LeaderSessionSnapshotResponse:
        identities = [resolved.chat_leader_identity_id]
        if resolved.chat_collab_identity_id != resolved.chat_leader_identity_id:
            identities.append(resolved.chat_collab_identity_id)

        items: list[SessionSnapshotItem] = []
        for identity_id in identities:
            items.append(_build_identity_snapshot_item(identity_routes, identity_id))

        return LeaderSessionSnapshotResponse(
            generated_at=datetime.now(timezone.utc),
            total=len(items),
            items=items,
        )

    @app.get("/v1/identity/memory/{identity_id}")
    def get_identity_memory(identity_id: str, limit: int = 60) -> dict[str, object]:
        if not resolved.identity_memory_enabled:
            raise HTTPException(status_code=404, detail="identity_memory_disabled")
        payload = identity_memory.read(identity_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="identity_memory_not_found")
        turns = payload.get("turns")
        if isinstance(turns, list):
            bounded = max(1, min(500, int(limit)))
            payload["turns"] = turns[-bounded:]
            payload["returned_turns"] = len(payload["turns"])
        return payload

    @app.post("/v1/chat/inbound", response_model=ChatInboundResponse)
    def chat_inbound(req: ChatInboundRequest) -> ChatInboundResponse:
        return _dispatch_chat(
            message=req.message,
            identity_id=req.identity_id,
            session_id=req.session_id,
            codex_home=req.codex_home,
            session_name_prefix=req.session_name_prefix,
            verify_seconds=req.verify_seconds,
            metadata=req.metadata,
        )

    def _run_leader_command(req: LeaderCommandRequest) -> LeaderCommandResponse:
        leader_identity_id = resolved.chat_leader_identity_id
        collab_identity_id = resolved.chat_collab_identity_id
        notes: list[str] = []

        leader_result, leader_retry_note = _dispatch_chat_with_queued_retry(
            message=req.message,
            identity_id=leader_identity_id,
            verify_seconds=req.verify_seconds
            if req.verify_seconds is not None
            else resolved.chat_default_verify_seconds,
            metadata={
                **req.metadata,
                "entrypoint": "chat_leader_command",
                "role": "leader",
            },
            role="leader",
        )
        if leader_retry_note:
            notes.append(leader_retry_note)

        collab_result: ChatInboundResponse | None = None
        collab_error: str | None = None
        if req.auto_collab:
            collab_message = (
                req.collab_message.strip()
                if (req.collab_message or "").strip()
                else _build_default_collab_message(req.message)
            )
            try:
                collab_result, collab_retry_note = _dispatch_chat_with_queued_retry(
                    message=collab_message,
                    identity_id=collab_identity_id,
                    verify_seconds=req.collab_verify_seconds
                    if req.collab_verify_seconds is not None
                    else resolved.chat_default_verify_seconds,
                    metadata={
                        **req.metadata,
                        "entrypoint": "chat_leader_command",
                        "role": "collab",
                        "leader_identity_id": leader_identity_id,
                    },
                    role="collab",
                )
                if collab_retry_note:
                    notes.append(collab_retry_note)
            except HTTPException as exc:
                collab_error = f"{exc.status_code}:{exc.detail}"
                notes.append("collab_dispatch_http_exception")

        accepted = leader_result.accepted and (
            (collab_result.accepted if collab_result is not None else True)
            if req.auto_collab
            else True
        )
        if collab_error:
            accepted = False
        if req.auto_collab and collab_result is None and not collab_error:
            notes.append("collab_result_missing")
            accepted = False
        if leader_result.delivery_state != "confirmed":
            notes.append(f"leader_delivery_state={leader_result.delivery_state}")
        if collab_result is not None and collab_result.delivery_state != "confirmed":
            notes.append(f"collab_delivery_state={collab_result.delivery_state}")
        if not req.auto_collab:
            notes.append("collab_disabled")

        audit.append(
            "chat_leader_command",
            {
                "accepted": accepted,
                "leader_identity_id": leader_identity_id,
                "collab_identity_id": collab_identity_id if req.auto_collab else None,
                "leader_delivery_state": leader_result.delivery_state,
                "collab_delivery_state": collab_result.delivery_state if collab_result else None,
                "collab_error": collab_error,
                "notes": notes,
            },
        )

        return LeaderCommandResponse(
            accepted=accepted,
            auto_collab=req.auto_collab,
            leader_identity_id=leader_identity_id,
            collab_identity_id=collab_identity_id if req.auto_collab else None,
            leader_result=leader_result,
            collab_result=collab_result,
            collab_error=collab_error,
            orchestration_notes=notes,
        )

    @app.post("/v1/chat/leader/command", response_model=LeaderCommandResponse)
    def chat_leader_command(req: LeaderCommandRequest) -> LeaderCommandResponse:
        return _run_leader_command(req)

    @app.post("/v1/chat/leader/tasks", response_model=LeaderTaskAcceptedResponse)
    def create_leader_task(req: LeaderCommandRequest) -> LeaderTaskAcceptedResponse:
        snapshot = leader_tasks.submit(request=req, runner=_run_leader_command)
        task_id = str(snapshot.get("task_id", "")).strip()
        if not task_id:
            raise HTTPException(status_code=500, detail="leader_task_submit_failed")
        created_at = snapshot.get("created_at")
        if not isinstance(created_at, datetime):
            created_at = datetime.now(timezone.utc)

        poll_url = f"/v1/chat/leader/tasks/{task_id}"
        audit.append(
            "chat_leader_task_submitted",
            {
                "task_id": task_id,
                "state": str(snapshot.get("state", "accepted")),
                "auto_collab": req.auto_collab,
                "poll_url": poll_url,
            },
        )
        return LeaderTaskAcceptedResponse(
            accepted=True,
            task_id=task_id,
            state=str(snapshot.get("state", "accepted")),
            created_at=created_at,
            poll_url=poll_url,
        )

    @app.get("/v1/chat/leader/tasks/{task_id}", response_model=LeaderTaskStatusResponse)
    def get_leader_task(task_id: str) -> LeaderTaskStatusResponse:
        snapshot = leader_tasks.get(task_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="leader_task_not_found")
        return LeaderTaskStatusResponse(**snapshot)

    @app.post("/v1/approvals", response_model=CreateApprovalResponse)
    def create_approval(req: CreateApprovalRequest) -> CreateApprovalResponse:
        created = service.create_approval(req)
        if created.response.status is ApprovalStatus.PENDING and created.response.callback_token:
            expires_at = created.response.expires_at
            if expires_at is None:
                raise HTTPException(status_code=500, detail="missing_expiry")
            approve_url = _build_decision_link(
                resolved.callback_base_url,
                created.response.request_id,
                "approve",
                created.response.callback_token,
                resolved.default_approver,
            )
            reject_url = _build_decision_link(
                resolved.callback_base_url,
                created.response.request_id,
                "reject",
                created.response.callback_token,
                resolved.default_approver,
            )
            created.response.approve_url = approve_url
            created.response.reject_url = reject_url

            notification_delivered = False
            failure_reason = "notification_delivery_failed"
            if resolved.dingtalk_webhook_url:
                notification_delivered = dingtalk_client.send_approval_card(
                    request_id=created.response.request_id,
                    terminal_session_id=created.terminal_session_id,
                    command=created.command,
                    risk_level=created.response.risk_level,
                    expires_at=expires_at,
                    approve_url=approve_url,
                    reject_url=reject_url,
                    extra_context=created.extra_context,
                )
            elif resolved.lark_webhook_url:
                notification_delivered = lark_client.send_approval_card(
                    request_id=created.response.request_id,
                    terminal_session_id=created.terminal_session_id,
                    command=created.command,
                    risk_level=created.response.risk_level,
                    expires_at=expires_at,
                    callback_token=created.response.callback_token,
                    extra_context=created.extra_context,
                )
            else:
                failure_reason = "no_notification_channel"

            # Wrapper-triggered approvals must not remain pending if no one can receive the card.
            if req.source == "codex_wrapper" and not notification_delivered:
                service.reject_pending(
                    created.response.request_id,
                    reason=failure_reason,
                    approver="system-notifier",
                    source_ip="local",
                )
                sessions.apply_terminal_action(created.terminal_session_id, "ESC")
                created.response.status = ApprovalStatus.REJECTED
                created.response.reason = failure_reason
                created.response.callback_token = None
                created.response.approve_url = None
                created.response.reject_url = None
        if created.response.status is ApprovalStatus.REJECTED:
            sessions.apply_terminal_action(created.terminal_session_id, "ESC")
        return created.response

    @app.get("/v1/approvals/queue", response_model=ApprovalQueueResponse)
    def get_approval_queue(
        status: ApprovalStatus | None = ApprovalStatus.PENDING,
        limit: int = 20,
    ) -> ApprovalQueueResponse:
        try:
            resolved_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid_limit") from exc
        return service.list_queue(status_filter=status, limit=resolved_limit)

    @app.get("/v1/approvals/{request_id}", response_model=ApprovalStatusResponse)
    def get_approval(request_id: str) -> ApprovalStatusResponse:
        try:
            return service.get_status(request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval_not_found") from exc

    @app.get("/v1/approvals/{request_id}/votes", response_model=ApprovalVotesResponse)
    def get_approval_votes(request_id: str) -> ApprovalVotesResponse:
        try:
            return service.get_votes(request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval_not_found") from exc

    @app.post("/v1/approvals/{request_id}/trusted-decision", response_model=DecisionResponse)
    def trusted_decision(
        request_id: str,
        req: TrustedDecisionRequest,
        request: Request,
    ) -> DecisionResponse:
        configured = str(resolved.trusted_bridge_decision_token or "").strip()
        presented = str(request.headers.get("x-fqg-bridge-token", "")).strip()
        if not configured or not presented or presented != configured:
            raise HTTPException(status_code=403, detail="trusted_decision_forbidden")

        source_ip = req.source_ip or (request.client.host if request.client else "")
        try:
            result = service.apply_decision_trusted(
                request_id=request_id,
                action=req.action,
                approver=req.approver,
                source_ip=source_ip,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result.terminal_action in {"ENTER", "ESC"}:
            session_id = service.get_terminal_session_id(request_id)
            sessions.apply_terminal_action(session_id, result.terminal_action)
        return result

    @app.post("/v1/callback/decision", response_model=DecisionResponse)
    def callback(req: DecisionCallbackRequest, request: Request) -> DecisionResponse:
        source_ip = req.source_ip or (request.client.host if request.client else "")
        try:
            result = service.apply_decision(req, source_ip=source_ip)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        row = store.get_approval(req.request_id)
        if row is not None and row["terminal_action"]:
            applied = sessions.apply_terminal_action(row["terminal_session_id"], row["terminal_action"])
            if not applied:
                audit.append(
                    "terminal_action_not_applied",
                    {
                        "request_id": req.request_id,
                        "terminal_session_id": row["terminal_session_id"],
                        "terminal_action": row["terminal_action"],
                        "reason": "session_not_registered",
                    },
                )
        return result

    @app.get("/v1/decision-link")
    def decision_link(
        request_id: str,
        action: str,
        token: str,
        approver: str | None = None,
    ) -> HTMLResponse:
        if action not in {"approve", "reject"}:
            raise HTTPException(status_code=400, detail="invalid_action")
        resolved_approver = approver or resolved.default_approver

        try:
            state = service.get_status(request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval_not_found") from exc

        if state.status is not ApprovalStatus.PENDING:
            page_html = (
                "<html><body style='font-family:Arial,sans-serif;padding:24px;'>"
                f"<h3>飞桥守护审批结果: {state.status.value}</h3>"
                f"<p>request_id: {state.request_id}</p>"
                f"<p>reason: already_finalized:{state.reason}</p>"
                "</body></html>"
            )
            return HTMLResponse(content=page_html, status_code=200)

        action_text = "同意执行" if action == "approve" else "拒绝执行"
        safe_command = html.escape(state.command)
        session_id = html.escape(service.get_terminal_session_id(request_id))
        page_html = (
            "<html><body style='font-family:Arial,sans-serif;padding:24px;'>"
            "<h3>飞桥守护审批确认</h3>"
            f"<p>request_id: {request_id}</p>"
            f"<p>terminal_session_id: {session_id}</p>"
            f"<p>approver: {resolved_approver}</p>"
            f"<p>待确认动作: {action_text}</p>"
            "<p>待审批内容:</p>"
            f"<pre style='white-space:pre-wrap;background:#f5f5f5;padding:12px;border-radius:6px;'>{safe_command}</pre>"
            "<form method='post' action='/v1/decision-link/confirm'>"
            f"<input type='hidden' name='request_id' value='{request_id}' />"
            f"<input type='hidden' name='action' value='{action}' />"
            f"<input type='hidden' name='token' value='{token}' />"
            f"<input type='hidden' name='approver' value='{resolved_approver}' />"
            "<button type='submit' style='padding:8px 14px;'>确认提交</button>"
            "</form>"
            "</body></html>"
        )
        return HTMLResponse(content=page_html, status_code=200)

    @app.post("/v1/decision-link/confirm")
    def decision_link_confirm(
        request: Request,
        request_id: str = Form(...),
        action: str = Form(...),
        token: str = Form(...),
        approver: str | None = Form(None),
    ) -> HTMLResponse:
        if action not in {"approve", "reject"}:
            raise HTTPException(status_code=400, detail="invalid_action")
        source_ip = request.client.host if request.client else ""
        resolved_approver = approver or resolved.default_approver
        try:
            result = service.apply_decision_by_link(
                request_id=request_id,
                action=action,
                token=token,
                approver=resolved_approver,
                source_ip=source_ip,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result.terminal_action in {"ENTER", "ESC"}:
            session_id = service.get_terminal_session_id(request_id)
            sessions.apply_terminal_action(session_id, result.terminal_action)

        page_html = (
            "<html><body style='font-family:Arial,sans-serif;padding:24px;'>"
            f"<h3>飞桥守护审批结果: {result.status.value}</h3>"
            f"<p>request_id: {result.request_id}</p>"
            f"<p>action: {action}</p>"
            f"<p>reason: {result.reason}</p>"
            "</body></html>"
        )
        return HTMLResponse(content=page_html, status_code=200)

    return app


async def _expire_loop(
    service: ApprovalService,
    sessions: SessionRegistry,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        expired_ids = service.expire_pending()
        if expired_ids:
            for request_id in expired_ids:
                state = service.get_status(request_id)
                if state.terminal_action:
                    session_id = service.get_terminal_session_id(request_id)
                    sessions.apply_terminal_action(session_id, state.terminal_action)
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=max(1, settings.auto_expire_poll_interval_seconds),
            )
        except asyncio.TimeoutError:
            continue


def _build_decision_link(
    callback_base_url: str,
    request_id: str,
    action: str,
    token: str,
    approver: str,
) -> str:
    base = f"{callback_base_url.rstrip('/')}/v1/decision-link"
    query = urlencode(
        {
            "request_id": request_id,
            "action": action,
            "token": token,
            "approver": approver,
        }
    )
    return f"{base}?{query}"


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _build_chat_control_command(
    *,
    message: str,
    session_id: str | None,
    codex_home: str | None,
    session_name_prefix: str,
    verify_seconds: float,
) -> list[str]:
    root_dir = _workspace_root()
    command = [
        sys.executable,
        str(root_dir / "scripts" / "guarded_session_control.py"),
        "continue",
        "--workspace-root",
        str(root_dir),
        "--text",
        message,
        "--verify-seconds",
        str(verify_seconds),
        "--session-name-prefix",
        session_name_prefix,
        "--json",
    ]
    if session_id:
        command.extend(["--session-id", session_id])
    if codex_home:
        command.extend(["--codex-home", codex_home])
    return command


def _parse_control_output(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _infer_chat_delivery_state(control_exit_code: int, control_result: dict[str, object]) -> str:
    if control_exit_code == 0 and bool(control_result.get("ok", False)):
        return "confirmed"

    action = str(control_result.get("action", "")).strip()
    tmux_rc = control_result.get("tmux_returncode")
    if action.startswith("tmux_") and tmux_rc == 0:
        # Message is queued into the target tmux/codex flow, but rollout
        # confirmation did not arrive within verify window.
        return "queued"

    return "failed"


def _message_preview(text: str, limit: int = 240) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def _build_default_collab_message(user_message: str) -> str:
    return (
        "【Leader协同任务】\n"
        "你是 feiqiao-guard-collab-executor。\n"
        "请按以下闭环执行：\n"
        "1) 基于用户目标执行具体实现；\n"
        "2) 自行完成测试；\n"
        "3) 仅回传结论与证据绝对路径。\n"
        f"用户原始指令：{user_message.strip()}"
    )


def _build_identity_snapshot_item(
    identity_routes: IdentityRouteTable,
    identity_id: str,
) -> SessionSnapshotItem:
    try:
        route = identity_routes.resolve(identity_id)
    except ValueError as exc:
        return SessionSnapshotItem(
            identity_id=identity_id,
            enabled=False,
            session_name_prefix="fqg",
            state="ERROR",
            error=f"route_invalid:{exc}",
        )

    if route is None:
        return SessionSnapshotItem(
            identity_id=identity_id,
            enabled=False,
            session_name_prefix="fqg",
            state="DISABLED_OR_UNROUTED",
            error="identity_route_not_found_or_disabled",
        )

    route_issue = str(identity_routes.route_issue(identity_id) or "").strip()
    if route_issue:
        return SessionSnapshotItem(
            identity_id=identity_id,
            enabled=route.enabled,
            session_id=route.session_id,
            session_name_prefix=route.session_name_prefix,
            codex_home=route.codex_home,
            state="ROUTE_CONFLICT",
            error=route_issue,
        )

    codex_home = (route.codex_home or "").strip()
    if not codex_home:
        return SessionSnapshotItem(
            identity_id=identity_id,
            enabled=route.enabled,
            session_id=route.session_id,
            session_name_prefix=route.session_name_prefix,
            codex_home=None,
            state="NO_CODEX_HOME",
            error="codex_home_missing",
        )

    codex_home_path = Path(codex_home).expanduser().resolve()
    resolved_session_id = _resolve_session_id(route.session_id, codex_home_path)
    if not resolved_session_id:
        return SessionSnapshotItem(
            identity_id=identity_id,
            enabled=route.enabled,
            session_id=None,
            session_name_prefix=route.session_name_prefix,
            codex_home=str(codex_home_path),
            state="NO_SESSION",
            error="session_id_missing",
        )

    snapshot_data, snapshot_error = _read_session_snapshot(
        session_id=resolved_session_id,
        codex_home=codex_home_path,
    )
    if snapshot_data is None:
        return SessionSnapshotItem(
            identity_id=identity_id,
            enabled=route.enabled,
            session_id=resolved_session_id,
            session_name_prefix=route.session_name_prefix,
            codex_home=str(codex_home_path),
            state="SNAPSHOT_ERROR",
            error=snapshot_error or "snapshot_read_failed",
        )

    return SessionSnapshotItem(
        identity_id=identity_id,
        enabled=route.enabled,
        session_id=resolved_session_id,
        session_name_prefix=route.session_name_prefix,
        codex_home=str(codex_home_path),
        state=str(snapshot_data.get("state", "UNKNOWN")).strip() or "UNKNOWN",
        idle_seconds=_to_int_or_none(snapshot_data.get("idle_seconds")),
        last_event_type=str(snapshot_data.get("last_event_type", "")).strip() or None,
        last_event_summary=str(snapshot_data.get("last_event_summary", "")).strip() or None,
        last_agent_message=str(snapshot_data.get("last_agent_message", "")).strip() or None,
        process_probe_ok=_to_bool_or_none(snapshot_data.get("process_probe_ok")),
    )


def _resolve_session_id(route_session_id: str | None, codex_home: Path) -> str | None:
    direct = (route_session_id or "").strip()
    if direct and SESSION_ID_PATTERN.match(direct):
        return direct
    sid_file = codex_home / "last_codex_session_id"
    if not sid_file.exists():
        return None
    try:
        raw = sid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw if SESSION_ID_PATTERN.match(raw) else None


def _read_session_snapshot(*, session_id: str, codex_home: Path) -> tuple[dict[str, object] | None, str | None]:
    script = _workspace_root() / "scripts" / "watch_guarded_session.py"
    command = [
        sys.executable,
        str(script),
        "--session-id",
        session_id,
        "--codex-home",
        str(codex_home),
        "--json",
    ]
    try:
        cp = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None, "snapshot_timeout"
    except OSError as exc:
        return None, f"snapshot_spawn_failed:{exc}"

    if cp.returncode != 0:
        stderr_preview = " ".join(cp.stderr.strip().split())[:180]
        return None, f"snapshot_failed:rc={cp.returncode}:{stderr_preview}"

    payload = _parse_json_payload(cp.stdout)
    if payload is None:
        return None, "snapshot_invalid_json"
    return payload, None


def _parse_json_payload(stdout: str) -> dict[str, object] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # Defensive fallback: parse from the last JSON object line.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _to_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_bool_or_none(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="FeiQiao-Guard approval gateway")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    settings = load_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    uvicorn.run("feiqiao_guard.main:create_app", host=host, port=port, factory=True)


if __name__ == "__main__":
    main()
