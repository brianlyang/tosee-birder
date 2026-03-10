#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <host> [user]"
  echo "Optional env: SSH_PASSWORD, SSH_KEY, SSH_PORT, SSH_CONNECT_TIMEOUT"
  exit 1
fi

HOST="$1"
USER_NAME="${2:-root}"
SSH_PORT="${SSH_PORT:-22}"
CONNECT_TIMEOUT="${SSH_CONNECT_TIMEOUT:-10}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/resource/ops/live"
mkdir -p "${OUT_DIR}"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="${OUT_DIR}/${HOST}_${STAMP}.txt"

REMOTE_CMD='
echo "[HOST]"; hostname
echo "[OS]"; uname -a
echo "[UPTIME]"; uptime
echo "[RUNTIME]"; node -v 2>/dev/null || echo "node:missing"
npm -v 2>/dev/null || echo "npm:missing"
python3 --version 2>/dev/null || echo "python3:missing"
python --version 2>/dev/null || echo "python:missing"
echo "[MEM]"; free -h 2>/dev/null || true
echo "[DISK]"; df -h / 2>/dev/null || true
echo "[PORTS]"; ss -lntp 2>/dev/null | head -n 30 || true
echo "[N8N]"; systemctl is-active n8n 2>/dev/null || true
'

BASE_SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o ConnectTimeout="${CONNECT_TIMEOUT}"
  -p "${SSH_PORT}"
)

if [[ -n "${SSH_KEY:-}" ]]; then
  ssh "${BASE_SSH_OPTS[@]}" -i "${SSH_KEY}" "${USER_NAME}@${HOST}" "${REMOTE_CMD}" | tee "${OUT_FILE}"
elif [[ -n "${SSH_PASSWORD:-}" ]]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    echo "sshpass is required when SSH_PASSWORD is set"
    exit 2
  fi
  sshpass -p "${SSH_PASSWORD}" ssh "${BASE_SSH_OPTS[@]}" "${USER_NAME}@${HOST}" "${REMOTE_CMD}" | tee "${OUT_FILE}"
else
  ssh "${BASE_SSH_OPTS[@]}" "${USER_NAME}@${HOST}" "${REMOTE_CMD}" | tee "${OUT_FILE}"
fi

echo
echo "Snapshot saved: ${OUT_FILE}"
