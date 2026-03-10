#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_SCRIPT="${ROOT_DIR}/scripts/local_dingtalk_tmux_stack.sh"
DEPLOY_ENV="${ROOT_DIR}/.runtime/deploy.env"
SECRETS_ENV_DEFAULT="${ROOT_DIR}/.runtime/deploy.secrets.env"
SECRETS_ENV="${FQG_LOCAL_STACK_SECRETS_ENV:-${SECRETS_ENV_DEFAULT}}"
RUNTIME_DIR="${ROOT_DIR}/.runtime/local_bridge"
LOG_FILE="${RUNTIME_DIR}/supervisor.log"
BRIDGE_HEARTBEAT_FILE="${RUNTIME_DIR}/bridge_heartbeat.json"
BRIDGE_LOG_FILE="${RUNTIME_DIR}/bridge.log"

if [[ -f "${DEPLOY_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${DEPLOY_ENV}"
  set +a
fi

if [[ -f "${SECRETS_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${SECRETS_ENV}"
  set +a
fi

TMUX_SOCKET="${FQG_LOCAL_STACK_TMUX_SOCKET:-${ROOT_DIR}/.runtime/tmux/local_bridge.sock}"
API_SESSION="${FQG_LOCAL_STACK_API_SESSION:-fqg-local-api}"
BRIDGE_SESSION="${FQG_LOCAL_STACK_BRIDGE_SESSION:-fqg-local-stream}"
BASE_URL="${FQG_BRIDGE_BASE_URL:-http://127.0.0.1:${FQG_PORT:-3001}}"
CHECK_INTERVAL_SECONDS="${FQG_LOCAL_STACK_SUPERVISOR_INTERVAL_SECONDS:-8}"
START_GRACE_SECONDS="${FQG_LOCAL_STACK_SUPERVISOR_START_GRACE_SECONDS:-20}"
RESTART_RETRIES="${FQG_LOCAL_STACK_SUPERVISOR_RESTART_RETRIES:-3}"
RESTART_RETRY_DELAY_SECONDS="${FQG_LOCAL_STACK_SUPERVISOR_RETRY_DELAY_SECONDS:-2}"
FAILURE_STREAK_FOR_RESTART="${FQG_LOCAL_STACK_SUPERVISOR_FAILURE_STREAK_FOR_RESTART:-3}"
HEALTHZ_MAX_TIME_SECONDS="${FQG_LOCAL_STACK_SUPERVISOR_HEALTHZ_MAX_TIME_SECONDS:-2}"
HEARTBEAT_STALE_SECONDS="${FQG_LOCAL_STACK_SUPERVISOR_HEARTBEAT_STALE_SECONDS:-35}"
ENABLE_GUARD_PREWARM="${FQG_LOCAL_STACK_SUPERVISOR_ENABLE_GUARD_PREWARM:-1}"
GUARD_FAILURE_STREAK_FOR_RESTART="${FQG_LOCAL_STACK_SUPERVISOR_GUARD_FAILURE_STREAK_FOR_RESTART:-2}"
LOOP_HEARTBEAT_EVERY="${FQG_LOCAL_STACK_SUPERVISOR_LOOP_HEARTBEAT_EVERY:-10}"
GUARD_LAST_DETAIL=""
BRIDGE_ALLOW_IDLE_RESTART_ZERO="${FQG_BRIDGE_ALLOW_IDLE_RESTART_ZERO:-0}"
BRIDGE_MIN_IDLE_RESTART_SECONDS="${FQG_LOCAL_STACK_SUPERVISOR_MIN_IDLE_RESTART_SECONDS:-60}"
BRIDGE_MIN_MAX_UPTIME_SECONDS="${FQG_LOCAL_STACK_SUPERVISOR_MIN_MAX_UPTIME_SECONDS:-600}"

mkdir -p "${RUNTIME_DIR}" "$(dirname "${TMUX_SOCKET}")"

ts() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  printf '%s [local_stack_supervisor] %s\n' "$(ts)" "$*" | tee -a "${LOG_FILE}" >/dev/null
}

curl_local() {
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    curl "$@"
}

tmux_session_alive() {
  local session="$1"
  tmux -S "${TMUX_SOCKET}" has-session -t "${session}" >/dev/null 2>&1
}

stack_healthy() {
  stack_probe >/dev/null
}

heartbeat_fresh() {
  local now epoch
  now="$(date +%s)"
  epoch="$(python3 - "${BRIDGE_HEARTBEAT_FILE}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(3)
raw = json.loads(path.read_text(encoding="utf-8"))
value = raw.get("generated_at_epoch")
if value is None:
    raise SystemExit(4)
print(float(value))
PY
  )" || return 1

  local age
  age="$(python3 - "${now}" "${epoch}" <<'PY'
import sys
now = float(sys.argv[1])
epoch = float(sys.argv[2])
print(max(0.0, now - epoch))
PY
  )" || return 1

  python3 - "${age}" "${HEARTBEAT_STALE_SECONDS}" <<'PY'
import sys
age = float(sys.argv[1])
limit = float(sys.argv[2])
raise SystemExit(0 if age <= limit else 5)
PY
}

bridge_runtime_guard_ok() {
  if [[ "${BRIDGE_ALLOW_IDLE_RESTART_ZERO}" == "1" ]]; then
    echo "guard_bypassed_allow_idle_restart_zero"
    return 0
  fi
  python3 - "${BRIDGE_LOG_FILE}" "${BRIDGE_MIN_IDLE_RESTART_SECONDS}" "${BRIDGE_MIN_MAX_UPTIME_SECONDS}" <<'PY'
import re
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
min_idle = float(sys.argv[2])
min_uptime = float(sys.argv[3])
if not log_path.exists():
    print("log_missing_soft_pass")
    raise SystemExit(0)
text = log_path.read_text(encoding="utf-8", errors="ignore")
matches = re.findall(
    r"bridge_started .*?idle_restart_seconds=([0-9.]+).*?max_uptime_seconds=([0-9.]+)",
    text,
)
if not matches:
    print("bridge_started_missing_soft_pass")
    raise SystemExit(0)
idle_raw, uptime_raw = matches[-1]
idle = float(idle_raw)
uptime = float(uptime_raw)
ok = idle >= min_idle and uptime >= min_uptime
if ok:
    print(f"ok idle={idle:.1f} uptime={uptime:.1f}")
    raise SystemExit(0)
print(
    f"below_min idle={idle:.1f} uptime={uptime:.1f} "
    f"min_idle={min_idle:.1f} min_uptime={min_uptime:.1f}"
)
raise SystemExit(4)
PY
}

stack_probe() {
  local failures=()
  if ! tmux_session_alive "${API_SESSION}"; then
    failures+=("api_session_missing")
  fi
  if ! tmux_session_alive "${BRIDGE_SESSION}"; then
    failures+=("bridge_session_missing")
  fi
  if ! curl_local -fsS --max-time "${HEALTHZ_MAX_TIME_SECONDS}" "${BASE_URL}/healthz" >/dev/null 2>&1; then
    failures+=("healthz_unreachable")
  fi
  if ! heartbeat_fresh >/dev/null 2>&1; then
    failures+=("bridge_heartbeat_missing_or_stale")
  fi
  local runtime_guard_detail=""
  if ! runtime_guard_detail="$(bridge_runtime_guard_ok 2>&1)"; then
    runtime_guard_detail="$(echo "${runtime_guard_detail}" | tr '\n' ' ' | xargs || true)"
    if [[ -n "${runtime_guard_detail}" ]]; then
      failures+=("bridge_self_healing_disabled:${runtime_guard_detail}")
    else
      failures+=("bridge_self_healing_disabled")
    fi
  fi
  if [[ "${#failures[@]}" -gt 0 ]]; then
    printf '%s' "${failures[*]}"
    return 1
  fi
  printf 'ok'
  return 0
}

restart_stack() {
  log "restart requested"
  local attempt=1
  while [[ "${attempt}" -le "${RESTART_RETRIES}" ]]; do
    if "${STACK_SCRIPT}" restart >>"${LOG_FILE}" 2>&1; then
      log "restart succeeded on attempt=${attempt}"
      return 0
    fi
    log "restart attempt=${attempt} failed"
    if [[ "${attempt}" -lt "${RESTART_RETRIES}" ]]; then
      sleep "${RESTART_RETRY_DELAY_SECONDS}"
    fi
    attempt=$((attempt + 1))
  done
  log "restart failed after attempts=${RESTART_RETRIES}"
  return 1
}

prewarm_guard() {
  if [[ "${ENABLE_GUARD_PREWARM}" != "1" ]]; then
    return 0
  fi
  local output
  if output="$("${STACK_SCRIPT}" prewarm 2>&1)"; then
    GUARD_LAST_DETAIL="${output}"
    return 0
  fi
  GUARD_LAST_DETAIL="${output}"
  log "guard prewarm command failed detail=${output}"
  return 1
}

wait_for_healthy() {
  local i=0
  while [[ "${i}" -lt "${START_GRACE_SECONDS}" ]]; do
    if stack_healthy; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

main() {
  log "supervisor boot: socket=${TMUX_SOCKET} base_url=${BASE_URL} failure_streak_for_restart=${FAILURE_STREAK_FOR_RESTART} guard_prewarm=${ENABLE_GUARD_PREWARM}"
  local probe_detail
  if ! probe_detail="$(stack_probe)"; then
    log "initial health probe failed detail=${probe_detail}"
    restart_stack || true
    if wait_for_healthy; then
      log "initial health check passed"
    else
      log "initial health check failed"
    fi
  else
    log "stack already healthy"
  fi
  if [[ "${ENABLE_GUARD_PREWARM}" == "1" ]]; then
    if prewarm_guard; then
      log "initial guard prewarm ok"
    else
      log "initial guard prewarm failed"
    fi
  fi

  local failure_streak=0
  local guard_failure_streak=0
  local loop_count=0
  while true; do
    if [[ "${ENABLE_GUARD_PREWARM}" == "1" ]]; then
      if prewarm_guard; then
        if [[ "${guard_failure_streak}" -gt 0 ]]; then
          log "guard prewarm recovered after streak=${guard_failure_streak}"
        fi
        guard_failure_streak=0
      else
        guard_failure_streak=$((guard_failure_streak + 1))
        log "guard prewarm failed streak=${guard_failure_streak}/${GUARD_FAILURE_STREAK_FOR_RESTART}"
        if [[ "${guard_failure_streak}" -ge "${GUARD_FAILURE_STREAK_FOR_RESTART}" ]]; then
          log "guard prewarm reached threshold, forcing guard-only recover"
          if prewarm_guard; then
            log "guard recovered via guard-only recover"
            guard_failure_streak=0
          else
            log "guard-only recover still failing"
          fi
        fi
      fi
    fi

    if probe_detail="$(stack_probe)"; then
      if [[ "${failure_streak}" -gt 0 ]]; then
        log "health probe recovered after streak=${failure_streak}"
      fi
      failure_streak=0
      loop_count=$((loop_count + 1))
      if (( loop_count % LOOP_HEARTBEAT_EVERY == 0 )); then
        log "heartbeat ok loop=${loop_count} guard_streak=${guard_failure_streak} guard_detail=${GUARD_LAST_DETAIL}"
      fi
    else
      failure_streak=$((failure_streak + 1))
      log "health probe failed streak=${failure_streak}/${FAILURE_STREAK_FOR_RESTART} detail=${probe_detail}"
      if [[ "${failure_streak}" -ge "${FAILURE_STREAK_FOR_RESTART}" ]]; then
        log "health probe reached restart threshold, restarting stack"
        restart_stack || true
        if wait_for_healthy; then
          log "health recovered"
          failure_streak=0
        else
          log "health still failing after restart"
        fi
      fi
    fi
    sleep "${CHECK_INTERVAL_SECONDS}"
  done
}

main "$@"
