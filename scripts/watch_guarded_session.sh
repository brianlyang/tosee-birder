#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ISOLATION_ROOT="${FQG_ISOLATION_ROOT:-${ROOT_DIR}/.runtime/codex_isolated}"
MONITOR_CODEX_HOME="${FQG_MONITOR_CODEX_HOME:-${ISOLATION_ROOT}/codex_home}"

exec "${PYTHON_BIN}" "${ROOT_DIR}/scripts/watch_guarded_session.py" \
  --codex-home "${MONITOR_CODEX_HOME}" \
  "$@"
