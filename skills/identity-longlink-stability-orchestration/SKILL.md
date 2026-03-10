---
name: identity-longlink-stability-orchestration
description: Use when running Guixianren DingTalk bridge as a stable longlink runtime (online relay + local tmux/codex), with fail-close gates, event-trace observability, and replayable acceptance evidence.
---

# Identity Longlink Stability Orchestration

## Purpose

Use this skill to keep one production-like execution mode stable:

- User talks in DingTalk (`龟仙人1`).
- DingTalk stream bridge receives inbound message.
- Message is dispatched to local `tmux/codex` runtime.
- Progress and final answer are pushed back to DingTalk.
- Every run has replay evidence (`msg_id`, tags, artifacts).

This skill is restricted to the `龟仙人1` channel path.

## Use This Skill When

- The user asks for "持续可对话" stability under DingTalk bridge.
- You must prevent route/identity/channel drift during long sessions.
- You need hard evidence for `accepted -> progress -> final_result` behavior.

## Hard Gates (Fail-Close)

1. Mission gate
- Must match `resource/ops/mission_lock_guixianren_e2e.md`.

2. Channel gate
- `龟仙人1` is ACTIVE.
- `n8n自动化通知机器人` is PAUSED (not deleted).

3. Trigger gate
- Acceptance evidence must include a real DingTalk inbound (`chat_inbound_received` from `dingtalk_stream`).
- Local-only dry runs cannot replace external acceptance.

4. Evidence gate
- Every run must emit:
  - `run_id`
  - `summary.tsv`
  - `report.json`
  - event trace with `msg_id` and tag sequence

5. No-hardcode gate
- Do not branch by user text keywords to force outcomes.
- Preserve channel-first forwarding (text/image/file all pass through).

6. Instance-first correction gate
- First dispatch to the target `tmux/codex` session, then correct by dialogue in that same session.
- If drift is large, `leader` acts as coach (constraints + evidence requirements), not as direct replacement executor.
- Do not bypass the target identity by answering from bridge/leader code paths.

## Fixed Workflow

0. Load mission lock
- Read `resource/ops/mission_lock_guixianren_e2e.md` first.

1. Start/restart local longlink stack
```bash
./scripts/local_dingtalk_tmux_stack.sh restart
```

Guard-stability defaults:
- Keep `FQG_STACK_GUARD_STRICT_HEALTH=0` (soft-ready mode) to avoid false kill/recreate churn.
- If `FQG_NEW_SESSION_WARMUP_FAIL_CLOSE=1`, verify warmup marker contract is reachable before load testing.

2. Verify three-plane health (local runtime)
```bash
curl -fsS http://127.0.0.1:3001/healthz
curl -sS http://127.0.0.1:3001/v1/chat/routes
curl -sS http://127.0.0.1:3001/v1/chat/leader/snapshot
```

3. Verify stream is truly connected
```bash
rg -n "bridge_started|endpoint is \\{" .runtime/local_bridge/bridge.log | tail -n 20
cat .runtime/local_bridge/bridge_heartbeat.json
```

4. Run acceptance suite
```bash
python3 scripts/run_guixianren_final_acceptance_suite.py
```

Preferred one-command cycle:
```bash
bash skills/identity-longlink-stability-orchestration/scripts/run_longlink_stability_cycle.sh
```

5. Inspect message-level progress
```bash
tail -n 200 .runtime/local_bridge/bridge.log
tail -n 120 .runtime/local_bridge/api.log
```

6. Handle partial completion correctly
- If a turn ends in `dispatch_turn_settled` and user requires a direct final answer,
  run `identity-dispatch-final-answer` skill for the exact `question_tag` path.

7. Publish evidence
- Report PASS/FAIL with concrete artifact paths.
- Include at least one real inbound `msg_id` and observed tag sequence.

## Output Contract

Required report fields:
- `run_id`
- `overall`
- `passed` / `failed`
- `cases[*].name` + `cases[*].detail`
- `latest_real_msg_id`
- `latest_real_tag_sequence`
- `evidence_dir`

## References

- `references/gates_and_evidence.md`
- `references/event_trace_contract.md`
- `resource/ops/mission_lock_guixianren_e2e.md`
- `resource/ops/guixianren_longlink_skill_runbook_20260310.md`
