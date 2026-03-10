from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from feiqiao_guard.config import Settings
from feiqiao_guard.main import create_app


def _settings(
    tmp_path: Path,
    timeout_seconds: int = 120,
    poll_interval: int = 1,
    enable_high_risk_dual_approval: bool = False,
    trusted_bridge_decision_token: str = "",
) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8765,
        timeout_seconds=timeout_seconds,
        auto_expire_poll_interval_seconds=poll_interval,
        callback_max_skew_seconds=300,
        callback_base_url="http://127.0.0.1:8765",
        callback_signing_secret="test-secret",
        default_approver="zhouqihang",
        dingtalk_webhook_url="",
        dingtalk_signing_secret="",
        lark_webhook_url="",
        lark_signing_secret="",
        audit_log_path=tmp_path / "audit.jsonl",
        sqlite_path=tmp_path / "gateway.db",
        approver_allowlist=[],
        bypass_signature_verification=True,
        enable_high_risk_dual_approval=enable_high_risk_dual_approval,
        trusted_bridge_decision_token=trusted_bridge_decision_token,
    )


def test_create_and_approve_flow(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        actions: list[str] = []
        app.state.sessions.register("sess-001", actions.append)

        created = client.post(
            "/v1/approvals",
            json={
                "command": "ls -la",
                "terminal_session_id": "sess-001",
                "source": "pytest",
                "extra_context": {},
            },
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["status"] == "PENDING"
        assert payload["callback_token"]

        callback = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "approve",
                "token": payload["callback_token"],
                "nonce": "nonce-001",
                "approver": "zhouqihang",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert callback.status_code == 200
        assert callback.json()["status"] == "APPROVED"

        state = client.get(f"/v1/approvals/{payload['request_id']}")
        assert state.status_code == 200
        assert state.json()["status"] == "APPROVED"
        assert actions == ["ENTER"]


def test_timeout_auto_expire(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, timeout_seconds=1, poll_interval=1))
    with TestClient(app) as client:
        created = client.post(
            "/v1/approvals",
            json={
                "command": "echo hello",
                "terminal_session_id": "sess-timeout",
                "source": "pytest",
                "extra_context": {},
            },
        )
        request_id = created.json()["request_id"]
        time.sleep(2.2)
        app.state.service.expire_pending()
        state = client.get(f"/v1/approvals/{request_id}")
        assert state.status_code == 200
        assert state.json()["status"] == "EXPIRED"
        assert state.json()["terminal_action"] == "ESC"


def test_high_risk_command_rejected_immediately(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/v1/approvals",
            json={
                "command": "rm -rf /tmp/demo",
                "terminal_session_id": "sess-risk",
                "source": "pytest",
                "extra_context": {},
            },
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["status"] == "REJECTED"
        assert payload["reason"] == "blocked_high_risk_command"


def test_codex_wrapper_without_notification_channel_fails_fast(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/v1/approvals",
            json={
                "command": "ls -la",
                "terminal_session_id": "sess-fast-fail",
                "source": "codex_wrapper",
                "extra_context": {},
            },
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["status"] == "REJECTED"
        assert payload["reason"] == "no_notification_channel"
        assert payload["callback_token"] is None


def test_decision_link_flow(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/v1/approvals",
            json={
                "command": "ls -la",
                "terminal_session_id": "sess-link",
                "source": "pytest",
                "extra_context": {},
            },
        )
        payload = created.json()
        assert payload["status"] == "PENDING"
        assert payload["approve_url"]

        preview = client.get(payload["approve_url"])
        assert preview.status_code == 200
        assert "审批确认" in preview.text

        parsed = urlparse(payload["approve_url"])
        query = parse_qs(parsed.query)
        form_data = {
            "request_id": query["request_id"][0],
            "action": query["action"][0],
            "token": query["token"][0],
            "approver": query.get("approver", ["zhouqihang"])[0],
        }
        link = client.post("/v1/decision-link/confirm", data=form_data)
        assert link.status_code == 200
        assert "APPROVED" in link.text

        state = client.get(f"/v1/approvals/{payload['request_id']}")
        assert state.status_code == 200
        assert state.json()["status"] == "APPROVED"


def test_l3_command_single_approval_when_dual_disabled(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, enable_high_risk_dual_approval=False))
    with TestClient(app) as client:
        created = client.post(
            "/v1/approvals",
            json={
                "command": "python3 manage.py migrate",
                "terminal_session_id": "sess-l3-single",
                "source": "pytest",
                "extra_context": {},
            },
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["status"] == "PENDING"
        assert payload["risk_level"] == "L3"

        callback = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "approve",
                "token": payload["callback_token"],
                "nonce": "nonce-l3-single-1",
                "approver": "zhouqihang",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert callback.status_code == 200
        assert callback.json()["status"] == "APPROVED"


def test_l3_command_requires_two_distinct_approvers_when_enabled(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, enable_high_risk_dual_approval=True))
    with TestClient(app) as client:
        actions: list[str] = []
        app.state.sessions.register("sess-l3-dual", actions.append)

        created = client.post(
            "/v1/approvals",
            json={
                "command": "python3 manage.py migrate",
                "terminal_session_id": "sess-l3-dual",
                "source": "pytest",
                "extra_context": {},
            },
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["status"] == "PENDING"
        assert payload["risk_level"] == "L3"
        assert payload["reason"] == "awaiting_first_approval_high_risk"

        first = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "approve",
                "token": payload["callback_token"],
                "nonce": "nonce-l3-dual-1",
                "approver": "approver-a",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["status"] == "PENDING"
        assert first_payload["terminal_action"] == "WAIT"
        assert first_payload["reason"] == "awaiting_second_approval_high_risk"

        interim = client.get(f"/v1/approvals/{payload['request_id']}")
        assert interim.status_code == 200
        interim_payload = interim.json()
        assert interim_payload["status"] == "PENDING"
        assert interim_payload["reason"] == "awaiting_second_approval_high_risk"
        assert interim_payload["approver"] == "approver-a"
        assert actions == []

        same_approver = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "approve",
                "token": payload["callback_token"],
                "nonce": "nonce-l3-dual-2",
                "approver": "approver-a",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert same_approver.status_code == 200
        same_payload = same_approver.json()
        assert same_payload["status"] == "PENDING"
        assert same_payload["terminal_action"] == "WAIT"
        assert same_payload["reason"] == "awaiting_second_approval_high_risk"
        assert actions == []

        second = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "approve",
                "token": payload["callback_token"],
                "nonce": "nonce-l3-dual-3",
                "approver": "approver-b",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert second.status_code == 200
        assert second.json()["status"] == "APPROVED"
        assert second.json()["terminal_action"] == "ENTER"
        assert actions == ["ENTER"]


def test_votes_endpoint_tracks_dual_approval_progress(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, enable_high_risk_dual_approval=True))
    with TestClient(app) as client:
        created = client.post(
            "/v1/approvals",
            json={
                "command": "python3 manage.py migrate",
                "terminal_session_id": "sess-l3-votes",
                "source": "pytest",
                "extra_context": {},
            },
        )
        payload = created.json()

        first = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "approve",
                "token": payload["callback_token"],
                "nonce": "nonce-l3-votes-1",
                "approver": "approver-a",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert first.status_code == 200
        assert first.json()["status"] == "PENDING"

        duplicate = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "approve",
                "token": payload["callback_token"],
                "nonce": "nonce-l3-votes-2",
                "approver": "approver-a",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["status"] == "PENDING"

        votes_mid = client.get(f"/v1/approvals/{payload['request_id']}/votes")
        assert votes_mid.status_code == 200
        votes_mid_payload = votes_mid.json()
        assert votes_mid_payload["request_id"] == payload["request_id"]
        assert len(votes_mid_payload["votes"]) == 1
        assert votes_mid_payload["votes"][0]["approver"] == "approver-a"
        assert votes_mid_payload["votes"][0]["action"] == "approve"

        second = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "approve",
                "token": payload["callback_token"],
                "nonce": "nonce-l3-votes-3",
                "approver": "approver-b",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert second.status_code == 200
        assert second.json()["status"] == "APPROVED"

        votes_final = client.get(f"/v1/approvals/{payload['request_id']}/votes")
        assert votes_final.status_code == 200
        votes_final_payload = votes_final.json()
        assert [v["approver"] for v in votes_final_payload["votes"]] == ["approver-a", "approver-b"]
        assert all(v["action"] == "approve" for v in votes_final_payload["votes"])


def test_l3_dual_approval_reject_short_circuits(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, enable_high_risk_dual_approval=True))
    with TestClient(app) as client:
        actions: list[str] = []
        app.state.sessions.register("sess-l3-reject", actions.append)

        created = client.post(
            "/v1/approvals",
            json={
                "command": "python3 manage.py migrate",
                "terminal_session_id": "sess-l3-reject",
                "source": "pytest",
                "extra_context": {},
            },
        )
        payload = created.json()

        first = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "approve",
                "token": payload["callback_token"],
                "nonce": "nonce-l3-reject-1",
                "approver": "approver-a",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert first.status_code == 200
        assert first.json()["status"] == "PENDING"

        reject = client.post(
            "/v1/callback/decision",
            json={
                "request_id": payload["request_id"],
                "action": "reject",
                "token": payload["callback_token"],
                "nonce": "nonce-l3-reject-2",
                "approver": "approver-b",
                "timestamp": int(time.time()),
                "signature": "",
                "source_ip": "127.0.0.1",
                "raw_payload": {},
            },
        )
        assert reject.status_code == 200
        reject_payload = reject.json()
        assert reject_payload["status"] == "REJECTED"
        assert reject_payload["terminal_action"] == "ESC"
        assert reject_payload["reason"] == "reject_by_callback"

        state = client.get(f"/v1/approvals/{payload['request_id']}")
        assert state.status_code == 200
        assert state.json()["status"] == "REJECTED"
        assert actions == ["ESC"]


def test_l3_dual_approval_via_decision_link(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, enable_high_risk_dual_approval=True))
    with TestClient(app) as client:
        actions: list[str] = []
        app.state.sessions.register("sess-l3-link", actions.append)

        created = client.post(
            "/v1/approvals",
            json={
                "command": "python3 manage.py migrate",
                "terminal_session_id": "sess-l3-link",
                "source": "pytest",
                "extra_context": {},
            },
        )
        payload = created.json()
        parsed = urlparse(payload["approve_url"])
        query = parse_qs(parsed.query)

        first_form = {
            "request_id": query["request_id"][0],
            "action": query["action"][0],
            "token": query["token"][0],
            "approver": "approver-link-a",
        }
        first = client.post("/v1/decision-link/confirm", data=first_form)
        assert first.status_code == 200
        assert "PENDING" in first.text

        duplicate = client.post("/v1/decision-link/confirm", data=first_form)
        assert duplicate.status_code == 200
        assert "PENDING" in duplicate.text

        second_form = dict(first_form)
        second_form["approver"] = "approver-link-b"
        second = client.post("/v1/decision-link/confirm", data=second_form)
        assert second.status_code == 200
        assert "APPROVED" in second.text

        state = client.get(f"/v1/approvals/{payload['request_id']}")
        assert state.status_code == 200
        state_payload = state.json()
        assert state_payload["status"] == "APPROVED"
        assert state_payload["reason"] == "approve_by_decision_link_dual_second"
        assert actions == ["ENTER"]


def test_approval_queue_endpoint_lists_pending_requests(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/v1/approvals",
            json={
                "command": "python3 manage.py migrate",
                "terminal_session_id": "sess-queue",
                "source": "pytest",
                "extra_context": {},
            },
        )
        payload = created.json()
        assert payload["status"] == "PENDING"

        queue_resp = client.get("/v1/approvals/queue?status=PENDING&limit=10")
        assert queue_resp.status_code == 200
        queue_payload = queue_resp.json()
        assert queue_payload["status_filter"] == "PENDING"
        assert queue_payload["total"] >= 1
        assert any(item["request_id"] == payload["request_id"] for item in queue_payload["items"])


def test_trusted_decision_endpoint_requires_token_and_applies_action(tmp_path: Path) -> None:
    token = "bridge-token-001"
    app = create_app(_settings(tmp_path, trusted_bridge_decision_token=token))
    with TestClient(app) as client:
        actions: list[str] = []
        app.state.sessions.register("sess-trusted", actions.append)

        created = client.post(
            "/v1/approvals",
            json={
                "command": "python3 manage.py migrate",
                "terminal_session_id": "sess-trusted",
                "source": "pytest",
                "extra_context": {},
            },
        )
        payload = created.json()
        request_id = payload["request_id"]

        forbidden = client.post(
            f"/v1/approvals/{request_id}/trusted-decision",
            json={"action": "approve", "approver": "guixianren"},
        )
        assert forbidden.status_code == 403

        approved = client.post(
            f"/v1/approvals/{request_id}/trusted-decision",
            headers={"x-fqg-bridge-token": token},
            json={"action": "approve", "approver": "guixianren"},
        )
        assert approved.status_code == 200
        approved_payload = approved.json()
        assert approved_payload["status"] == "APPROVED"
        assert approved_payload["terminal_action"] == "ENTER"
        assert approved_payload["reason"] == "approve_by_trusted_bridge"
        assert actions == ["ENTER"]
