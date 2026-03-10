#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ISOLATION_ROOT="${FQG_ISOLATION_ROOT:-${ROOT_DIR}/.runtime/codex_isolated}"
SID_FILE="${ISOLATION_ROOT}/codex_home/last_codex_session_id"

if [[ ! -s "${SID_FILE}" ]]; then
  echo "No recorded Codex session id found: ${SID_FILE}" >&2
  echo "Start a guarded Codex session once, then retry." >&2
  exit 2
fi

SID="$(tr -d '[:space:]' < "${SID_FILE}")"
if [[ -z "${SID}" ]]; then
  echo "Recorded session id is empty: ${SID_FILE}" >&2
  exit 3
fi

echo "[FeiQiao-Guard] resuming codex_session_id=${SID}"
exec "${ROOT_DIR}/scripts/run_codex_guarded.sh" resume "${SID}" "$@"
