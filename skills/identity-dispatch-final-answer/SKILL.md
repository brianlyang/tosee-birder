---
name: identity-dispatch-final-answer
description: Use when a real user question must be routed to a specific identity instance and converged to one final answer with anti-drift guards, question tags, and evidence.
---

# Identity Dispatch Final Answer

## Overview

This skill hardens the exact loop that drifted before:
1) pin one target identity,
2) attach a unique `question_tag`,
3) dispatch the real question through `/v1/chat/inbound`,
4) poll `/v1/chat/leader/snapshot` until terminal,
5) force one final-answer nudge when needed,
6) output a single final answer plus evidence files.

## Use When

- The user says "route to this identity and give final answer, no drift".
- DingTalk progress cards are moving but no final answer arrives.
- You need deterministic traceability (`question_tag`) for one real question.

## Required Inputs

- `identity_id` (required)
- `question` (required; real user question)
- `base_url` (default: `http://8.140.215.219:3001`)

## Run

```bash
bash skills/identity-dispatch-final-answer/scripts/run_identity_dispatch_final.sh \
  --base-url http://8.140.215.219:3001 \
  --identity-id custom-creative-ecom-analyst \
  --question "她是谁？请调用GLM4.6V给出答案"
```

Optional flags:
- `--question-tag <tag>` (if omitted, auto-generated)
- `--wait-seconds 300`
- `--poll-seconds 4`
- `--force-final-on-terminal 1`
- `--evidence-dir /tmp/identity_dispatch_final_xxx`

## Output Contract

Stdout includes:
- `QUESTION_TAG=<...>`
- `FINAL_STATUS=<SUCCESS|TIMEOUT|FAILED>`
- `FINAL_ANSWER=<...>`
- `EVIDENCE_DIR=<abs_path>`

Evidence files under `evidence_dir`:
- `routes.json`
- `dispatch_payload.json`
- `dispatch_response.json`
- `snapshot_poll.jsonl`
- `nudge_payload.json` (if used)
- `nudge_response.json` (if used)
- `final_report.json`
- `final_report.md`

## Anti-Drift Guards

- Hard route check: target identity must exist in `/v1/chat/routes` and must not have `route_error`.
- Snapshot polling only reads the target `identity_id` item.
- Terminal without final marker triggers one forced nudge (`FINAL_ANSWER=` contract).
- Every artifact is tied to `question_tag` for replay/audit.
