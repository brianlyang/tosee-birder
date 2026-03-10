---
name: identity-instance-triple-verify
description: Use when validating a target identity instance can provide complete API-call guidance, execute delegated tasks end-to-end, and produce reusable knowledge through direct memory cross-check.
---

# Identity Instance Triple Verify

## Overview

This skill validates a target identity instance with a strict three-step loop:
1) output quality for a complete callable method, 2) direct delegated execution evidence, 3) memory-learning evidence with local cross-check.

## Use When

- The user asks to prove "this identity can really do the task", not only discuss.
- You need multi-agent/multi-identity replay evidence.
- You must confirm if identity memory can retain and expose reusable knowledge.

## Required Inputs

- `identity_id` (target instance id)
- `identity_path` (absolute path)
- `protocol_home` (identity protocol base repo absolute path)
- `origin_workspace` (target identity original workspace absolute path)
- `tmux socket` + `tmux session` bound to target identity dialogue
- topic (default: `GLM4.6V`)

## Workflow

0. S0 self-recognition gate:
- Required hard gate (L1): `identity_id + identity_path` and HUD must match.
- Observation-only (L2/L3): `protocol_home + origin_workspace` are recorded for scheduling/migration/recovery analysis, but do not block execution.
- Do not provide concrete tuple values in S0 prompt body (anti-hardcode).
- Local runner compares self-reported values with expected tuple and marks `OBSERVE_HIT/MISS`.

1. S1 method output:
- Ask target identity to return a fully callable solution.
- Must include env var placeholders, `curl`, Python snippet, and image input variants.

2. S2 direct execution:
- Ask target identity to create runnable template scripts and run local sanity checks.
- Must emit summary artifact with command return codes.

3. S3 memory learning:
- Ask target identity to write a memory note into its runtime memory area.
- Locally scan memory files and verify knowledge can be learned/replayed.

4. Cross-review:
- session evidence (tmux capture)
- artifact evidence (created files + syntax checks)
- memory evidence (keyword hits + memory note existence)
- fail-close verdict

## Run

```bash
bash skills/identity-instance-triple-verify/scripts/run_identity_triple_verify.sh \
  --socket /Users/yangxi/claude/codex_project/fqsh/.runtime/tmux/c233d2a0cdf87229.sock \
  --session fqg-custom-019cb379 \
  --identity-id custom-creative-ecom-analyst \
  --identity-path /Users/yangxi/.codex/identity/instances/custom-creative-ecom-analyst \
  --protocol-home /Users/yangxi/claude/codex_project/weixinstore/identity-protocol-local \
  --origin-workspace /Users/yangxi/claude/codex_project/fqsh
```

Optional flags:
- `--topic GLM4.6V`
- `--evidence-dir /tmp/identity_triple_verify_xxx`
- `--timeout-seconds 240`

## Output Contract

The runner emits these files under evidence dir:
- `step1_method_capture.txt`
- `step0_identity_capture.txt`
- `step0_observed_values.txt`
- `step2_execution_capture.txt`
- `step2_generated_files.txt`
- `step3_memory_capture.txt`
- `step3_memory_scan.txt`
- `verification_summary.tsv`
- `verification_report.md`
- `verification_report.json`

## Failure Policy

- Unreachable tmux session: `FAIL_REQUIRED`.
- S0 L1 mismatch (identity_id/path/HUD): `FAIL_REQUIRED`.
- Missing callable details in S1: `FAIL_REQUIRED`.
- Missing runnable artifacts in S2: `FAIL_REQUIRED`.
- Missing memory-learning evidence in S3: `FAIL_REQUIRED`.
- S0 L2/L3 mismatch: observation warning only (`OBSERVE_MISS`), no fail-close.

## References

- `references/glm46v-call-template.md`
