# FeiQiao-Guard Identity Setup

## Runtime Identity Home
- `IDENTITY_HOME`: `/Users/yangxi/claude/codex_project/fqsh/.identity`
- `catalog`: `/Users/yangxi/claude/codex_project/fqsh/.identity/catalog.local.yaml`
- `identity_id`: `feiqiao-guard-delivery-lead`

## Identity Pack
- pack root: `/Users/yangxi/claude/codex_project/fqsh/.identity/feiqiao-guard-delivery-lead`
- prompt: `IDENTITY_PROMPT.md`
- task contract: `CURRENT_TASK.json`
- rules: `RULEBOOK.jsonl`
- history: `TASK_HISTORY.md`

## Project Binding
- project name: 飞桥守护
- code name: `FeiQiao-Guard`
- owner: 周启航
- PM: 林若岚
- source specs: `/Users/yangxi/claude/codex_project/weixinstore/resource/specs/codex-feishu-approval-bridge`

## Validation Commands
Run from protocol base repo:

```bash
cd /Users/yangxi/claude/codex_project/weixinstore/identity-protocol-local

python3 scripts/validate_identity_runtime_contract.py \
  --catalog /Users/yangxi/claude/codex_project/fqsh/.identity/catalog.local.yaml \
  --identity-id feiqiao-guard-delivery-lead

python3 scripts/validate_identity_role_binding.py \
  --catalog /Users/yangxi/claude/codex_project/fqsh/.identity/catalog.local.yaml \
  --identity-id feiqiao-guard-delivery-lead

python3 scripts/validate_identity_state_consistency.py \
  --catalog /Users/yangxi/claude/codex_project/fqsh/.identity/catalog.local.yaml
```

Project-local shortcut:

```bash
cd /Users/yangxi/claude/codex_project/fqsh
./scripts/validate_identity_instance.sh
```
