from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

import feiqiao_guard.main as main_module
from feiqiao_guard.config import Settings
from feiqiao_guard.main import create_app


def _settings(
    tmp_path: Path,
    routes_path: Path,
    *,
    queue_fail_close_unconfirmed: bool = False,
) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8765,
        timeout_seconds=120,
        auto_expire_poll_interval_seconds=15,
        callback_max_skew_seconds=300,
        callback_base_url="http://127.0.0.1:8765",
        callback_signing_secret="test-secret",
        default_approver="approver",
        dingtalk_webhook_url="",
        dingtalk_signing_secret="",
        lark_webhook_url="",
        lark_signing_secret="",
        audit_log_path=tmp_path / "audit.jsonl",
        sqlite_path=tmp_path / "gateway.db",
        approver_allowlist=[],
        bypass_signature_verification=True,
        enable_high_risk_dual_approval=False,
        identity_routes_path=routes_path,
        chat_control_timeout_seconds=10,
        chat_default_verify_seconds=8,
        chat_queue_retry_fail_close_unconfirmed=queue_fail_close_unconfirmed,
    )


def _write_routes(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_chat_inbound_uses_identity_route_and_dispatches(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    codex_home = str(tmp_path / "codex-home-collab")
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-collab-executor": {
                    "session_id": sid,
                    "codex_home": codex_home,
                    "session_name_prefix": "fqg-collab",
                    "verify_seconds": 9,
                }
            }
        },
    )

    captured: dict[str, object] = {}

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "session_id": sid,
                    "codex_home": codex_home,
                    "action": "tmux_send_keys",
                },
                ensure_ascii=False,
            )
            + "\n",
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/inbound",
                json={
                    "identity_id": "feiqiao-guard-collab-executor",
                    "message": "继续执行并完成测试回传",
                },
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is True
            assert payload["delivery_state"] == "confirmed"
            assert payload["route_source"] == "identity_route"
            assert payload["session_id"] == sid
            assert payload["codex_home"] == codex_home
    finally:
        main_module.subprocess.run = original_run

    cmd = list(captured["cmd"])  # type: ignore[arg-type]
    assert "--session-id" in cmd
    assert sid in cmd
    assert "--codex-home" in cmd
    assert codex_home in cmd
    assert "--session-name-prefix" in cmd
    assert "fqg-collab" in cmd


def test_chat_inbound_supports_request_override(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": "019cb379-59ff-72f1-9380-2e1b687257a6",
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "session_name_prefix": "fqg-lead",
                }
            }
        },
    )

    sid_override = "02d4ad3a-bf3d-4dc6-aa5a-7fc8a74ad201"
    codex_home_override = str(tmp_path / "codex-home-override")
    captured: dict[str, object] = {}

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "session_id": sid_override,
                    "codex_home": codex_home_override,
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/inbound",
                json={
                    "identity_id": "feiqiao-guard-delivery-lead",
                    "message": "继续推进",
                    "session_id": sid_override,
                    "codex_home": codex_home_override,
                    "session_name_prefix": "manual-prefix",
                    "verify_seconds": 12,
                },
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is True
            assert payload["delivery_state"] == "confirmed"
            assert payload["session_id"] == sid_override
            assert payload["codex_home"] == codex_home_override
            assert payload["session_name_prefix"] == "manual-prefix"
            assert payload["verify_seconds"] == 12
    finally:
        main_module.subprocess.run = original_run

    cmd = list(captured["cmd"])  # type: ignore[arg-type]
    assert sid_override in cmd
    assert codex_home_override in cmd
    assert "manual-prefix" in cmd
    assert "--verify-seconds" in cmd
    assert "12.0" in cmd


def test_chat_inbound_returns_404_when_identity_route_missing(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    _write_routes(routes_path, {"identities": {}})

    app = create_app(_settings(tmp_path, routes_path))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/inbound",
            json={"identity_id": "missing-identity", "message": "继续执行"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "identity_route_not_found"


def test_chat_inbound_returns_accepted_false_when_control_fails(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    codex_home = str(tmp_path / "codex-home-collab")
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-collab-executor": {
                    "session_id": sid,
                    "codex_home": codex_home,
                }
            }
        },
    )

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout=json.dumps({"ok": False, "error": "tmux not found"}, ensure_ascii=False),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/inbound",
                json={
                    "identity_id": "feiqiao-guard-collab-executor",
                    "message": "继续执行",
                },
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is False
            assert payload["delivery_state"] == "failed"
            assert payload["control_exit_code"] == 1
            assert payload["control_result"]["ok"] is False
    finally:
        main_module.subprocess.run = original_run


def test_chat_inbound_marks_queued_when_tmux_delivery_succeeds_without_rollout(
    tmp_path: Path,
) -> None:
    routes_path = tmp_path / "identity_routes.json"
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    codex_home = str(tmp_path / "codex-home-collab")
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-collab-executor": {
                    "session_id": sid,
                    "codex_home": codex_home,
                }
            }
        },
    )

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "action": "tmux_send_keys",
                    "tmux_returncode": 0,
                    "session_id": sid,
                    "codex_home": codex_home,
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/inbound",
                json={
                    "identity_id": "feiqiao-guard-collab-executor",
                    "message": "继续执行",
                },
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is True
            assert payload["delivery_state"] == "queued"
            assert payload["control_exit_code"] == 1
            assert payload["control_result"]["tmux_returncode"] == 0
    finally:
        main_module.subprocess.run = original_run


def test_chat_routes_lists_configured_identity_routes(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": "019cb379-59ff-72f1-9380-2e1b687257a6",
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "session_name_prefix": "lead",
                },
                "feiqiao-guard-collab-executor": {
                    "session_id": "02d4ad3a-bf3d-4dc6-aa5a-7fc8a74ad201",
                    "codex_home": str(tmp_path / "codex-home-collab"),
                    "session_name_prefix": "collab",
                    "enabled": False,
                },
            }
        },
    )

    app = create_app(_settings(tmp_path, routes_path))
    with TestClient(app) as client:
        resp = client.get("/v1/chat/routes")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] == 2
        assert payload["route_config_path"].endswith("identity_routes.json")
        identities = {item["identity_id"]: item for item in payload["items"]}
        assert identities["feiqiao-guard-delivery-lead"]["enabled"] is True
        assert identities["feiqiao-guard-collab-executor"]["enabled"] is False
        assert identities["feiqiao-guard-delivery-lead"]["route_status"] == "ok"
        assert identities["feiqiao-guard-delivery-lead"]["route_error"] is None


def test_chat_inbound_blocks_conflicting_shared_session_without_switch_ack(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "session_name_prefix": "lead",
                },
                "feiqiao-guard-collab-executor": {
                    "session_id": sid,
                    "codex_home": str(tmp_path / "codex-home-collab"),
                    "session_name_prefix": "collab",
                },
            }
        },
    )

    app = create_app(_settings(tmp_path, routes_path))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/inbound",
            json={"identity_id": "feiqiao-guard-delivery-lead", "message": "继续执行"},
        )
        assert resp.status_code == 409
        assert str(resp.json()["detail"]).startswith("session_id_conflict_requires_switch_ack:")

        routes_resp = client.get("/v1/chat/routes")
        assert routes_resp.status_code == 200
        routes_payload = routes_resp.json()
        identities = {item["identity_id"]: item for item in routes_payload["items"]}
        assert identities["feiqiao-guard-delivery-lead"]["route_status"] == "error"
        assert "session_id_conflict_requires_switch_ack" in (identities["feiqiao-guard-delivery-lead"]["route_error"] or "")


def test_chat_inbound_blocks_conflict_even_with_explicit_override(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    sid = "019cb379-59ff-72f1-9380-2e1b687257a6"
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": sid,
                    "codex_home": str(tmp_path / "codex-home-shared"),
                    "session_name_prefix": "lead",
                },
                "feiqiao-guard-collab-executor": {
                    "session_id": sid,
                    "codex_home": str(tmp_path / "codex-home-shared"),
                    "session_name_prefix": "collab",
                },
            }
        },
    )

    app = create_app(_settings(tmp_path, routes_path))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/inbound",
            json={
                "identity_id": "feiqiao-guard-delivery-lead",
                "message": "继续执行",
                "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "codex_home": str(tmp_path / "manual-override-home"),
            },
        )
        assert resp.status_code == 409
        assert str(resp.json()["detail"]).startswith("session_id_conflict_requires_switch_ack:")


def test_chat_inbound_blocks_conflicting_codex_home_without_switch_ack(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    shared_home = str(tmp_path / "codex-home-shared")
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "codex_home": shared_home,
                    "session_name_prefix": "lead",
                },
                "feiqiao-guard-collab-executor": {
                    "codex_home": shared_home,
                    "session_name_prefix": "collab",
                },
            }
        },
    )

    app = create_app(_settings(tmp_path, routes_path))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/inbound",
            json={"identity_id": "feiqiao-guard-delivery-lead", "message": "继续执行"},
        )
        assert resp.status_code == 409
        assert str(resp.json()["detail"]).startswith("codex_home_conflict_requires_switch_ack:")


def test_leader_command_dispatches_leader_and_collab(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    leader_sid = "11111111-1111-4111-8111-111111111111"
    collab_sid = "22222222-2222-4222-8222-222222222222"
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": leader_sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "session_name_prefix": "leader",
                },
                "feiqiao-guard-collab-executor": {
                    "session_id": collab_sid,
                    "codex_home": str(tmp_path / "codex-home-collab"),
                    "session_name_prefix": "collab",
                },
            }
        },
    )

    call_payloads = [
        {
            "ok": True,
            "session_id": leader_sid,
            "codex_home": str(tmp_path / "codex-home-lead"),
            "action": "tmux_send_keys",
        },
        {
            "ok": True,
            "session_id": collab_sid,
            "codex_home": str(tmp_path / "codex-home-collab"),
            "action": "tmux_send_keys",
        },
    ]
    calls: list[list[str]] = []

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        calls.append(list(cmd))
        payload = call_payloads[len(calls) - 1]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/leader/command",
                json={"message": "继续推进本次需求直到完成"},
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is True
            assert payload["auto_collab"] is True
            assert payload["leader_identity_id"] == "feiqiao-guard-delivery-lead"
            assert payload["collab_identity_id"] == "feiqiao-guard-collab-executor"
            assert payload["leader_result"]["accepted"] is True
            assert payload["leader_result"]["delivery_state"] == "confirmed"
            assert payload["leader_result"]["session_id"] == leader_sid
            assert payload["collab_result"]["accepted"] is True
            assert payload["collab_result"]["delivery_state"] == "confirmed"
            assert payload["collab_result"]["session_id"] == collab_sid
            assert payload["collab_error"] is None
    finally:
        main_module.subprocess.run = original_run

    assert len(calls) == 2
    assert "--session-id" in calls[0] and leader_sid in calls[0]
    assert "--session-id" in calls[1] and collab_sid in calls[1]
    collab_text = calls[1][calls[1].index("--text") + 1]
    assert "Leader协同任务" in collab_text


def test_leader_command_can_disable_collab(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    leader_sid = "11111111-1111-4111-8111-111111111111"
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": leader_sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "session_name_prefix": "leader",
                }
            }
        },
    )

    calls: list[list[str]] = []

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        calls.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "session_id": leader_sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "action": "tmux_send_keys",
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/leader/command",
                json={"message": "只给leader", "auto_collab": False},
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is True
            assert payload["auto_collab"] is False
            assert payload["collab_identity_id"] is None
            assert payload["collab_result"] is None
            assert payload["collab_error"] is None
            assert "collab_disabled" in payload["orchestration_notes"]
    finally:
        main_module.subprocess.run = original_run

    assert len(calls) == 1
    assert leader_sid in calls[0]


def test_leader_command_fails_when_queued_retry_turns_failed(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    leader_sid = "11111111-1111-4111-8111-111111111111"
    leader_home = str(tmp_path / "codex-home-lead")
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": leader_sid,
                    "codex_home": leader_home,
                    "session_name_prefix": "leader",
                }
            }
        },
    )

    calls: list[list[str]] = []
    call_payloads = [
        # first attempt: queued
        {
            "ok": False,
            "session_id": leader_sid,
            "codex_home": leader_home,
            "action": "tmux_send_keys",
            "tmux_returncode": 0,
        },
        # bounded retry: failed
        {
            "ok": False,
            "session_id": leader_sid,
            "codex_home": leader_home,
            "action": "tmux_new_session",
            "tmux_returncode": 1,
        },
    ]

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        calls.append(list(cmd))
        payload = call_payloads[len(calls) - 1]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/leader/command",
                json={"message": "继续执行", "auto_collab": False},
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is False
            assert payload["leader_result"]["delivery_state"] == "failed"
            notes = payload["orchestration_notes"]
            assert "leader_queued_retry:first=queued->retry=failed" in notes
            assert "leader_delivery_state=failed" in notes
            assert "collab_disabled" in notes
    finally:
        main_module.subprocess.run = original_run

    assert len(calls) == 2
    assert leader_sid in calls[0]
    assert leader_sid in calls[1]


def test_leader_command_soft_queues_when_retry_stays_unconfirmed_by_default(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    leader_sid = "11111111-1111-4111-8111-111111111111"
    leader_home = str(tmp_path / "codex-home-lead")
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": leader_sid,
                    "codex_home": leader_home,
                    "session_name_prefix": "leader",
                }
            }
        },
    )

    calls: list[list[str]] = []
    call_payloads = [
        # first attempt: queued, but no rollout confirmation
        {
            "ok": False,
            "session_id": leader_sid,
            "codex_home": leader_home,
            "action": "tmux_send_keys",
            "tmux_returncode": 0,
            "rollout_advanced": False,
        },
        # bounded retry: still queued with no rollout confirmation
        {
            "ok": False,
            "session_id": leader_sid,
            "codex_home": leader_home,
            "action": "tmux_send_keys",
            "tmux_returncode": 0,
            "rollout_advanced": False,
        },
    ]

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        calls.append(list(cmd))
        payload = call_payloads[len(calls) - 1]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/leader/command",
                json={"message": "继续执行", "auto_collab": False},
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is True
            assert payload["leader_result"]["delivery_state"] == "queued"
            assert payload["leader_result"]["control_result"]["queued_retry_unconfirmed"] is True
            notes = payload["orchestration_notes"]
            assert "leader_queued_retry:first=queued->retry=queued;soft_queued=retry_still_unconfirmed" in notes
            assert "leader_delivery_state=queued" in notes
            assert "collab_disabled" in notes
    finally:
        main_module.subprocess.run = original_run

    assert len(calls) == 2
    assert leader_sid in calls[0]
    assert leader_sid in calls[1]


def test_leader_command_hard_fails_when_unconfirmed_and_strict_mode_enabled(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    leader_sid = "11111111-1111-4111-8111-111111111111"
    leader_home = str(tmp_path / "codex-home-lead")
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": leader_sid,
                    "codex_home": leader_home,
                    "session_name_prefix": "leader",
                }
            }
        },
    )

    calls: list[list[str]] = []
    call_payloads = [
        {
            "ok": False,
            "session_id": leader_sid,
            "codex_home": leader_home,
            "action": "tmux_send_keys",
            "tmux_returncode": 0,
            "rollout_advanced": False,
        },
        {
            "ok": False,
            "session_id": leader_sid,
            "codex_home": leader_home,
            "action": "tmux_send_keys",
            "tmux_returncode": 0,
            "rollout_advanced": False,
        },
    ]

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        calls.append(list(cmd))
        payload = call_payloads[len(calls) - 1]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(
            _settings(
                tmp_path,
                routes_path,
                queue_fail_close_unconfirmed=True,
            )
        )
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/leader/command",
                json={"message": "继续执行", "auto_collab": False},
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is False
            assert payload["leader_result"]["delivery_state"] == "failed"
            assert (
                payload["leader_result"]["control_result"]["forced_failed_reason"]
                == "queued_retry_still_unconfirmed"
            )
            notes = payload["orchestration_notes"]
            assert (
                "leader_queued_retry:first=queued->retry=queued;"
                "forced_failed=queued_retry_still_unconfirmed"
            ) in notes
            assert "leader_delivery_state=failed" in notes
            assert "collab_disabled" in notes
    finally:
        main_module.subprocess.run = original_run

    assert len(calls) == 2
    assert leader_sid in calls[0]
    assert leader_sid in calls[1]


def test_leader_command_returns_error_details_when_collab_route_missing(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    leader_sid = "11111111-1111-4111-8111-111111111111"
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": leader_sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "session_name_prefix": "leader",
                }
            }
        },
    )

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "session_id": leader_sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "action": "tmux_send_keys",
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/leader/command",
                json={"message": "需要协作"},
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["accepted"] is False
            assert payload["leader_result"]["accepted"] is True
            assert payload["collab_result"] is None
            assert payload["collab_error"].startswith("404:")
            assert "collab_dispatch_http_exception" in payload["orchestration_notes"]
    finally:
        main_module.subprocess.run = original_run


def test_leader_task_completes_with_final_state(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    leader_sid = "11111111-1111-4111-8111-111111111111"
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": leader_sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "session_name_prefix": "leader",
                }
            }
        },
    )

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "session_id": leader_sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "action": "tmux_send_keys",
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            submit = client.post(
                "/v1/chat/leader/tasks",
                json={"message": "任务化执行测试", "auto_collab": False},
            )
            assert submit.status_code == 200
            submit_payload = submit.json()
            assert submit_payload["accepted"] is True
            assert submit_payload["state"] == "accepted"
            task_id = submit_payload["task_id"]

            status_payload = {}
            for _ in range(40):
                status = client.get(f"/v1/chat/leader/tasks/{task_id}")
                assert status.status_code == 200
                status_payload = status.json()
                if status_payload["state"] in {"final", "failed", "timeout"}:
                    break
                time.sleep(0.05)

            assert status_payload["state"] == "final"
            assert status_payload["result"]["accepted"] is True
            assert status_payload["result"]["leader_result"]["delivery_state"] == "confirmed"
            assert status_payload["events"]
    finally:
        main_module.subprocess.run = original_run


def test_leader_task_reports_failed_state_when_dispatch_unaccepted(tmp_path: Path) -> None:
    routes_path = tmp_path / "identity_routes.json"
    leader_sid = "11111111-1111-4111-8111-111111111111"
    _write_routes(
        routes_path,
        {
            "identities": {
                "feiqiao-guard-delivery-lead": {
                    "session_id": leader_sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "session_name_prefix": "leader",
                }
            }
        },
    )

    def _stub_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "session_id": leader_sid,
                    "codex_home": str(tmp_path / "codex-home-lead"),
                    "action": "tmux_new_session",
                    "tmux_returncode": 1,
                    "error": "tmux_failed",
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    original_run = main_module.subprocess.run
    main_module.subprocess.run = _stub_run
    try:
        app = create_app(_settings(tmp_path, routes_path))
        with TestClient(app) as client:
            submit = client.post(
                "/v1/chat/leader/tasks",
                json={"message": "任务失败测试", "auto_collab": False},
            )
            assert submit.status_code == 200
            task_id = submit.json()["task_id"]

            status_payload = {}
            for _ in range(40):
                status = client.get(f"/v1/chat/leader/tasks/{task_id}")
                assert status.status_code == 200
                status_payload = status.json()
                if status_payload["state"] in {"final", "failed", "timeout"}:
                    break
                time.sleep(0.05)

            assert status_payload["state"] == "failed"
            assert status_payload["result"]["accepted"] is False
            assert status_payload["result"]["leader_result"]["delivery_state"] == "failed"
            assert status_payload["events"]
    finally:
        main_module.subprocess.run = original_run
