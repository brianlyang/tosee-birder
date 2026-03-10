# Event Trace Contract

## Why This Exists

The longlink runtime can look healthy while user-level completion is still incomplete.
This reference defines the minimal event trace that must be recorded for each real DingTalk inbound.

## Trace Identity

For each real inbound, persist:

- `msg_id` (from DingTalk inbound event)
- `task_tag` (bridge dispatch tag)
- `identity_id` (must be `feiqiao-guard-delivery-lead` unless explicitly delegated)
- `recorded_at`

## Expected Tag Sequence

Minimum acceptable sequence:

1. `inbound_received`
2. `dispatch_accepted`
3. `dispatch_summary`
4. `dispatch_progress` (can repeat)
5. terminal event:
- preferred: `dispatch_final_result`
- acceptable for interactive continuation: `dispatch_turn_settled`

Optional convergence event (recommended when settled too early):

- `dispatch_force_final_retry` (or `dispatch_late_force_final_retry`)

## Evidence Sources

1. `.runtime/local_bridge/bridge.log`
- primary source for per-message tag progression

2. `.runtime/local_bridge/api.log`
- source for API acceptance and route behavior

3. `.runtime/local_bridge/bridge_heartbeat.json`
- source for connection freshness (`callback_count`, `reply_count`, timestamps)

4. `resource/reports/approval_audit.jsonl`
- source for `chat_inbound_received` proof

## Pass/Fail Rules

`PASS` for one inbound message requires all:

- has real DingTalk `chat_inbound_received`
- tag sequence includes `dispatch_accepted` and at least one progress/followup
- terminal event is either `dispatch_final_result` or `dispatch_turn_settled`
- no route conflict event for target identity

`FAIL` examples:

- no inbound event observed for the time window
- accepted exists but no subsequent summary/progress
- terminal timeout without any terminal tag
- route conflict (`route_status != ok` or non-empty `route_error`)

## Quick Inspection Commands

```bash
tail -n 200 .runtime/local_bridge/bridge.log
tail -n 120 .runtime/local_bridge/api.log
cat .runtime/local_bridge/bridge_heartbeat.json
rg -n "chat_inbound_received|msg_id=" resource/reports/approval_audit.jsonl | tail -n 40
```
