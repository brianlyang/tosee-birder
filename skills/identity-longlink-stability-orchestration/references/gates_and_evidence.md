# Gates And Evidence

## Gate Checklist

1. `mission_lock_loaded=1`
2. `guixianren_active=1`
3. `n8n_paused=1`
4. `online_base_url=1`
5. `evidence_plan_ready=1`

If any gate fails, stop execution and return `FAIL_CLOSE`.

## Minimal Evidence Set

- Health snapshot:
  - `/healthz`
  - `/v1/chat/routes`
  - `/v1/chat/leader/snapshot`
- Test artifacts:
  - `summary.tsv`
  - `report.json`
- Channel evidence:
  - DingTalk START message
  - DingTalk RESULT message
  - DingTalk SUMMARY message

## Retry Policy

1. If service down: restart stack once via launchd and retry health checks.
2. If route conflict: stop and fix route, do not continue tests.
3. If case fails: mark case id and continue full suite; summarize top failures.
