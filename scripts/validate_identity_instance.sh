#!/usr/bin/env bash
set -euo pipefail

PROTOCOL_HOME="${PROTOCOL_HOME:-/Users/yangxi/claude/codex_project/weixinstore/identity-protocol-local}"
CATALOG_PATH="${CATALOG_PATH:-/Users/yangxi/claude/codex_project/fqsh/.identity/catalog.local.yaml}"
IDENTITY_ID="${IDENTITY_ID:-feiqiao-guard-delivery-lead}"

python3 "${PROTOCOL_HOME}/scripts/validate_identity_runtime_contract.py" \
  --catalog "${CATALOG_PATH}" \
  --identity-id "${IDENTITY_ID}"

python3 "${PROTOCOL_HOME}/scripts/validate_identity_role_binding.py" \
  --catalog "${CATALOG_PATH}" \
  --identity-id "${IDENTITY_ID}"

python3 "${PROTOCOL_HOME}/scripts/validate_identity_state_consistency.py" \
  --catalog "${CATALOG_PATH}"

echo "[OK] identity instance validation completed: ${IDENTITY_ID}"
