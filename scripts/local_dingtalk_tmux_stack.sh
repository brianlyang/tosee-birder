#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${ROOT_DIR}/.runtime/local_bridge"
TMUX_SOCKET="${FQG_LOCAL_STACK_TMUX_SOCKET:-${ROOT_DIR}/.runtime/tmux/local_bridge.sock}"
API_SESSION="${FQG_LOCAL_STACK_API_SESSION:-fqg-local-api}"
BRIDGE_SESSION="${FQG_LOCAL_STACK_BRIDGE_SESSION:-fqg-local-stream}"
API_LOG="${RUNTIME_DIR}/api.log"
BRIDGE_LOG="${RUNTIME_DIR}/bridge.log"
BRIDGE_HEARTBEAT_FILE_DEFAULT="${RUNTIME_DIR}/bridge_heartbeat.json"
DEPLOY_ENV="${ROOT_DIR}/.runtime/deploy.env"
SECRETS_ENV_DEFAULT="${ROOT_DIR}/.runtime/deploy.secrets.env"
SECRETS_ENV="${FQG_LOCAL_STACK_SECRETS_ENV:-${SECRETS_ENV_DEFAULT}}"

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

# Guard against stale "simple mode" profiles that disable bridge self-healing.
# Set FQG_BRIDGE_ALLOW_IDLE_RESTART_ZERO=1 only for explicit troubleshooting.
DEFAULT_IDLE_RESTART_SECONDS="${FQG_BRIDGE_DEFAULT_IDLE_RESTART_SECONDS:-600}"
DEFAULT_MAX_UPTIME_SECONDS="${FQG_BRIDGE_DEFAULT_MAX_UPTIME_SECONDS:-7200}"
ALLOW_IDLE_RESTART_ZERO="${FQG_BRIDGE_ALLOW_IDLE_RESTART_ZERO:-0}"

_is_non_positive_number() {
  local raw="${1:-0}"
  awk -v v="${raw}" 'BEGIN { exit !(v + 0 <= 0) }'
}

if [[ "${ALLOW_IDLE_RESTART_ZERO}" != "1" ]]; then
  if [[ -z "${FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS:-}" ]] || _is_non_positive_number "${FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS:-0}"; then
    export FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS="${DEFAULT_IDLE_RESTART_SECONDS}"
  fi
fi
if [[ -z "${FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS:-}" ]] || _is_non_positive_number "${FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS:-0}"; then
  export FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS="${DEFAULT_MAX_UPTIME_SECONDS}"
fi

PORT="${FQG_PORT:-3001}"
HOST="${FQG_HOST:-127.0.0.1}"
BASE_URL="${FQG_BRIDGE_BASE_URL:-http://127.0.0.1:${PORT}}"
CLIENT_ID="${FQG_DINGTALK_STREAM_CLIENT_ID:-}"
CLIENT_SECRET="${FQG_DINGTALK_STREAM_CLIENT_SECRET:-}"
PYTHON_BIN_OVERRIDE="${FQG_LOCAL_STACK_PYTHON_BIN:-/usr/local/anaconda3/envs/py312/bin/python3}"
ROUTES_PATH_RAW="${FQG_IDENTITY_ROUTES_PATH:-${ROOT_DIR}/.runtime/identity_routes.local.custom.json}"
LEADER_ID="${FQG_CHAT_LEADER_IDENTITY_ID:-custom-creative-ecom-analyst}"
COLLAB_ID="${FQG_CHAT_COLLAB_IDENTITY_ID:-${LEADER_ID}}"
STACK_PREWARM_ENABLE="${FQG_STACK_PREWARM_ENABLE:-1}"
STACK_PREWARM_WAIT_SECONDS="${FQG_STACK_PREWARM_WAIT_SECONDS:-25}"
STACK_BRIDGE_READY_WAIT_SECONDS="${FQG_STACK_BRIDGE_READY_WAIT_SECONDS:-20}"
STACK_FORCE_RESET_GUARD_SERVER_ON_START="${FQG_STACK_FORCE_RESET_GUARD_SERVER_ON_START:-1}"
PYTHON_BIN=""

ensure_dirs() {
  mkdir -p "${RUNTIME_DIR}" "$(dirname "${TMUX_SOCKET}")"
}

curl_local() {
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    curl "$@"
}

python_bin_is_usable() {
  local candidate="$1"
  [[ -n "${candidate}" ]] || return 1
  [[ -x "${candidate}" ]] || return 1
  "${candidate}" - <<'PY' >/dev/null 2>&1
import sys
if sys.version_info < (3, 8):
    raise SystemExit(2)
import uvicorn  # noqa: F401
import dingtalk_stream  # noqa: F401
PY
}

resolve_python_bin() {
  if [[ -n "${PYTHON_BIN}" ]]; then
    return 0
  fi

  local candidates=()
  if [[ -n "${PYTHON_BIN_OVERRIDE}" ]]; then
    candidates+=("${PYTHON_BIN_OVERRIDE}")
  fi
  candidates+=("${ROOT_DIR}/.venv/bin/python3")
  candidates+=("${ROOT_DIR}/.venv/bin/python")
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("$(command -v python3)")
  fi
  candidates+=("/usr/local/anaconda3/envs/py312/bin/python3")
  candidates+=("/opt/homebrew/bin/python3")
  candidates+=("/usr/local/bin/python3")
  candidates+=("/usr/bin/python3")

  local seen=$'\n'
  local candidate=""
  for candidate in "${candidates[@]}"; do
    [[ -n "${candidate}" ]] || continue
    if [[ "${seen}" == *$'\n'"${candidate}"$'\n'* ]]; then
      continue
    fi
    seen+="${candidate}"$'\n'
    if python_bin_is_usable "${candidate}"; then
      PYTHON_BIN="${candidate}"
      return 0
    fi
  done

  echo "python_bin_not_found: requires python>=3.8 with uvicorn and dingtalk_stream" >&2
  return 1
}

resolve_routes_path() {
  if [[ "${ROUTES_PATH_RAW}" = /* ]]; then
    echo "${ROUTES_PATH_RAW}"
    return
  fi
  echo "${ROOT_DIR}/${ROUTES_PATH_RAW}"
}

tmux_has_session() {
  local session_name="$1"
  tmux -S "${TMUX_SOCKET}" has-session -t "${session_name}" >/dev/null 2>&1
}

tmux_kill_session() {
  local session_name="$1"
  if tmux_has_session "${session_name}"; then
    tmux -S "${TMUX_SOCKET}" kill-session -t "${session_name}" >/dev/null 2>&1 || true
  fi
}

tmux_start_session() {
  local session_name="$1"
  local command="$2"
  tmux -S "${TMUX_SOCKET}" new-session -d -s "${session_name}" "${command}"
}

wait_healthz() {
  local deadline_sec="${1:-20}"
  local i=0
  while [[ "${i}" -lt "${deadline_sec}" ]]; do
    if curl_local -fsS "${BASE_URL}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

route_field() {
  local routes_path="$1"
  local identity_id="$2"
  local field="$3"
  python3 - "${routes_path}" "${identity_id}" "${field}" <<'PY'
import json
import sys
from pathlib import Path

routes_path = Path(sys.argv[1])
identity_id = str(sys.argv[2]).strip()
field = str(sys.argv[3]).strip()
obj = json.loads(routes_path.read_text(encoding="utf-8"))
identities = obj.get("identities")
if not isinstance(identities, dict):
    raise SystemExit(2)
item = identities.get(identity_id)
if not isinstance(item, dict):
    raise SystemExit(3)
value = item.get(field)
if value is None:
    print("")
else:
    print(str(value).strip())
PY
}

validate_routes_file() {
  local routes_path="$1"
  python3 - "${routes_path}" "${LEADER_ID}" <<'PY'
import json
import sys
from pathlib import Path

route_path = Path(sys.argv[1])
leader_id = str(sys.argv[2]).strip()
obj = json.loads(route_path.read_text(encoding="utf-8"))
identities = obj.get("identities")
if not isinstance(identities, dict):
    raise SystemExit("identity_routes_invalid: identities must be object")
if leader_id not in identities:
    raise SystemExit(f"leader_identity_not_found_in_routes:{leader_id}")
PY
}

wait_bridge_ready() {
  local deadline_sec="${1:-20}"
  local i=0
  while [[ "${i}" -lt "${deadline_sec}" ]]; do
    local pane_info
    pane_info="$(tmux -S "${TMUX_SOCKET}" list-panes -t "${BRIDGE_SESSION}" -F '#{pane_dead} #{pane_current_command}' 2>/dev/null || true)"
    local pane_dead
    pane_dead="$(echo "${pane_info}" | awk '{print $1}')"
    if [[ -f "${BRIDGE_LOG}" ]] && rg -q 'bridge_started' "${BRIDGE_LOG}" && rg -q 'endpoint is \{' "${BRIDGE_LOG}" && [[ "${pane_dead}" == "0" ]]; then
      if ! rg -q 'missing_client_credentials|dingtalk_stream_not_installed' "${BRIDGE_LOG}"; then
        return 0
      fi
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

guard_session_healthy() {
  local guard_socket="$1"
  local guard_session="$2"
  local pane_info pane_dead pane_cmd
  pane_info="$(tmux -S "${guard_socket}" list-panes -t "${guard_session}" -F '#{pane_dead} #{pane_current_command}' 2>/dev/null | head -n 1 || true)"
  pane_dead="$(echo "${pane_info}" | awk '{print $1}')"
  pane_cmd="$(echo "${pane_info}" | awk '{print $2}')"
  if [[ "${pane_dead}" != "0" ]]; then
    return 1
  fi
  # Guard is healthy only after runtime leaves shell bootstrap and enters codex/python/node process.
  [[ -n "${pane_cmd}" ]] || return 1
  if [[ "${pane_cmd}" == python* || "${pane_cmd}" == codex* || "${pane_cmd}" == node* ]]; then
    return 0
  fi
  return 1
}

prewarm_guard_session() {
  local routes_path="$1"
  local route_sid route_codex_home route_prefix computed
  route_sid="$(route_field "${routes_path}" "${LEADER_ID}" "session_id")"
  route_codex_home="$(route_field "${routes_path}" "${LEADER_ID}" "codex_home")"
  route_prefix="$(route_field "${routes_path}" "${LEADER_ID}" "session_name_prefix")"
  route_prefix="${route_prefix:-fqg-lead}"

  if [[ -z "${route_sid}" ]]; then
    echo "route_session_id_missing_for_leader:${LEADER_ID}" >&2
    return 1
  fi
  if [[ -z "${route_codex_home}" ]]; then
    echo "route_codex_home_missing_for_leader:${LEADER_ID}" >&2
    return 1
  fi
  if [[ ! -d "${route_codex_home}" ]]; then
    echo "route_codex_home_not_found:${route_codex_home}" >&2
    return 1
  fi

  computed="$(_compute_guard_payload "${route_codex_home}" "${route_sid}" "${route_prefix}")"
  local guard_socket guard_session isolation_root
  guard_socket="$(_payload_field "${computed}" "socket_path")"
  guard_session="$(_payload_field "${computed}" "session_name")"
  isolation_root="$(_payload_field "${computed}" "isolation_root")"
  if [[ -z "${guard_socket}" || -z "${guard_session}" || -z "${isolation_root}" ]]; then
    echo "guard_payload_invalid_for_leader:${LEADER_ID}" >&2
    return 1
  fi

  mkdir -p "$(dirname "${guard_socket}")"
  if tmux -S "${guard_socket}" has-session -t "${guard_session}" 2>/dev/null; then
    if ! guard_session_healthy "${guard_socket}" "${guard_session}"; then
      tmux -S "${guard_socket}" kill-session -t "${guard_session}" >/dev/null 2>&1 || true
    fi
  fi

  if ! tmux -S "${guard_socket}" has-session -t "${guard_session}" 2>/dev/null; then
    tmux -S "${guard_socket}" new-session -d -s "${guard_session}" \
      "cd ${ROOT_DIR} && PYTHON_BIN=${PYTHON_BIN} FQG_ISOLATION_ROOT=${isolation_root} FQG_GATEWAY_URL=${BASE_URL} ./scripts/run_codex_guarded_lead.sh resume ${route_sid}"
  fi

  local i=0
  local wait_sec="${STACK_PREWARM_WAIT_SECONDS}"
  while [[ "${i}" -lt "${wait_sec}" ]]; do
    if tmux -S "${guard_socket}" has-session -t "${guard_session}" 2>/dev/null && guard_session_healthy "${guard_socket}" "${guard_session}"; then
      echo "guard_session_ready:${guard_session}@${guard_socket}"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "guard_session_not_ready:${guard_session}@${guard_socket}" >&2
  return 1
}

_compute_guard_payload() {
  local codex_home="$1"
  local sid="$2"
  local prefix="$3"
  python3 - "${codex_home}" "${sid}" "${prefix}" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

sid_re = re.compile(r"^[0-9a-fA-F-]{36}$")
codex_home = Path(sys.argv[1]).expanduser().resolve()
sid = str(sys.argv[2]).strip()
prefix = re.sub(r"[^a-zA-Z0-9_-]", "", str(sys.argv[3]).strip() or "fqg")
if not sid_re.match(sid):
    print("")
    raise SystemExit(0)
preferred = codex_home / "session_monitor" / "tmux" / f"{sid}.sock"
if len(str(preferred)) <= 100:
    socket_path = preferred
else:
    digest = hashlib.sha1(f"{codex_home}:{sid}".encode("utf-8")).hexdigest()[:16]
    socket_path = (codex_home.parent.parent / "tmux" / f"{digest}.sock").resolve()
session_name = f"{prefix}-{sid[:8]}"
payload = {
    "socket_path": str(socket_path),
    "session_name": session_name,
    "isolation_root": str(codex_home.parent),
}
print(json.dumps(payload, ensure_ascii=False))
PY
}

_payload_field() {
  local payload="$1"
  local key="$2"
  python3 - <<'PY' "${payload}" "${key}"
import json
import sys
raw = str(sys.argv[1]).strip()
key = str(sys.argv[2]).strip()
if not raw or not key:
    print("")
    raise SystemExit(0)
obj = json.loads(raw)
print(str(obj.get(key, "")).strip())
PY
}

reset_guard_server_for_leader() {
  local routes_path="$1"
  local route_sid route_codex_home route_prefix computed guard_socket
  route_sid="$(route_field "${routes_path}" "${LEADER_ID}" "session_id")"
  route_codex_home="$(route_field "${routes_path}" "${LEADER_ID}" "codex_home")"
  route_prefix="$(route_field "${routes_path}" "${LEADER_ID}" "session_name_prefix")"
  route_prefix="${route_prefix:-fqg-lead}"
  if [[ -z "${route_sid}" || -z "${route_codex_home}" ]]; then
    return 0
  fi
  computed="$(_compute_guard_payload "${route_codex_home}" "${route_sid}" "${route_prefix}")"
  guard_socket="$(_payload_field "${computed}" "socket_path")"
  if [[ -z "${guard_socket}" ]]; then
    return 0
  fi
  if tmux -S "${guard_socket}" ls >/dev/null 2>&1; then
    tmux -S "${guard_socket}" kill-server >/dev/null 2>&1 || true
    echo "guard_server_reset:${guard_socket}"
  fi
}

prewarm_only() {
  ensure_dirs
  resolve_python_bin
  local routes_path
  routes_path="$(resolve_routes_path)"
  if [[ ! -f "${routes_path}" ]]; then
    echo "identity_routes_file_not_found: ${routes_path}" >&2
    exit 2
  fi
  validate_routes_file "${routes_path}"
  if ! prewarm_guard_session "${routes_path}"; then
    echo "guard_prewarm_failed_for_leader:${LEADER_ID}" >&2
    exit 1
  fi
}

print_status() {
  set +e
  echo "[tmux_socket] ${TMUX_SOCKET}"
  if tmux -S "${TMUX_SOCKET}" ls >/dev/null 2>&1; then
    tmux -S "${TMUX_SOCKET}" ls | rg "${API_SESSION}|${BRIDGE_SESSION}" || true
  else
    echo "tmux_not_running"
  fi
  echo
  echo "[healthz]"
  curl_local -sS "${BASE_URL}/healthz" || echo "healthz_unreachable"
  echo
  echo "[routes]"
  curl_local -sS "${BASE_URL}/v1/chat/routes" || echo "routes_unreachable"
  echo
  echo "[leader_snapshot]"
  curl_local -sS "${BASE_URL}/v1/chat/leader/snapshot" || echo "leader_snapshot_unreachable"
  echo
  echo "[api_log_tail]"
  tail -n 30 "${API_LOG}" 2>/dev/null || echo "api_log_missing"
  echo
  echo "[bridge_log_tail]"
  tail -n 40 "${BRIDGE_LOG}" 2>/dev/null || echo "bridge_log_missing"
  set -e
}

start_stack() {
  ensure_dirs
  resolve_python_bin
  local routes_path
  routes_path="$(resolve_routes_path)"

  if [[ -z "${CLIENT_ID}" || -z "${CLIENT_SECRET}" ]]; then
    echo "missing_dingtalk_stream_credentials" >&2
    exit 2
  fi

  if [[ ! -f "${routes_path}" ]]; then
    echo "identity_routes_file_not_found: ${routes_path}" >&2
    exit 2
  fi

  validate_routes_file "${routes_path}"

  tmux_kill_session "${API_SESSION}"
  tmux_kill_session "${BRIDGE_SESSION}"

  local ts
  ts="$(date +%Y%m%d_%H%M%S)"
  [[ -f "${API_LOG}" ]] && mv -f "${API_LOG}" "${API_LOG}.${ts}.bak"
  [[ -f "${BRIDGE_LOG}" ]] && mv -f "${BRIDGE_LOG}" "${BRIDGE_LOG}.${ts}.bak"

  local api_cmd bridge_cmd
  api_cmd="cd ${ROOT_DIR} && while true; do env PYTHONPATH=${ROOT_DIR}/src FQG_HOST=${HOST} FQG_PORT=${PORT} FQG_CALLBACK_BASE_URL=${BASE_URL} FQG_IDENTITY_ROUTES_PATH=${routes_path} FQG_CHAT_LEADER_IDENTITY_ID=${LEADER_ID} FQG_CHAT_COLLAB_IDENTITY_ID=${COLLAB_ID} FQG_CHAT_CONTROL_TIMEOUT_SECONDS=${FQG_CHAT_CONTROL_TIMEOUT_SECONDS:-120} FQG_CHAT_DEFAULT_VERIFY_SECONDS=${FQG_CHAT_DEFAULT_VERIFY_SECONDS:-8} FQG_DISABLE_AGENTS_ADD_DIR=${FQG_DISABLE_AGENTS_ADD_DIR:-1} FQG_TRUSTED_BRIDGE_DECISION_TOKEN=${FQG_TRUSTED_BRIDGE_DECISION_TOKEN:-} ${PYTHON_BIN} -m uvicorn tosee_birder.main:create_app --factory --host ${HOST} --port ${PORT} >> ${API_LOG} 2>&1; exit_code=\$?; echo \"\$(date '+%F %T') api_process_exit code=\${exit_code} restart_in=2s\" >> ${API_LOG}; sleep 2; done"

  bridge_cmd="cd ${ROOT_DIR} && while true; do env PYTHONPATH=${ROOT_DIR}/src FQG_DINGTALK_STREAM_CLIENT_ID=${CLIENT_ID} FQG_DINGTALK_STREAM_CLIENT_SECRET=${CLIENT_SECRET} FQG_BRIDGE_BASE_URL=${BASE_URL} FQG_IDENTITY_ROUTES_PATH=${routes_path} FQG_CHAT_LEADER_IDENTITY_ID=${LEADER_ID} FQG_BRIDGE_REQUIRED_BASE_URL_PREFIX=${FQG_BRIDGE_REQUIRED_BASE_URL_PREFIX:-http://127.0.0.1:} FQG_BRIDGE_REQUIRED_CODEX_HOME_PREFIX=${FQG_BRIDGE_REQUIRED_CODEX_HOME_PREFIX:-${ROOT_DIR}/.runtime/codex_isolated} FQG_BRIDGE_STRICT_PROGRESS=${FQG_BRIDGE_STRICT_PROGRESS:-1} FQG_BRIDGE_TIMEOUT_SECONDS=${FQG_BRIDGE_TIMEOUT_SECONDS:-120} FQG_BRIDGE_AUTO_COLLAB=${FQG_BRIDGE_AUTO_COLLAB:-0} FQG_BRIDGE_REQUIRE_AT=${FQG_BRIDGE_REQUIRE_AT:-0} FQG_BRIDGE_REQUIRE_PREFIX=${FQG_BRIDGE_REQUIRE_PREFIX:-0} FQG_BRIDGE_COMMAND_PREFIXES=${FQG_BRIDGE_COMMAND_PREFIXES:-/run,/cmd} FQG_BRIDGE_ALLOW_USER_IDS=${FQG_BRIDGE_ALLOW_USER_IDS:-} FQG_BRIDGE_ALLOW_CHAT_IDS=${FQG_BRIDGE_ALLOW_CHAT_IDS:-} FQG_BRIDGE_FOLLOWUP_SECONDS=${FQG_BRIDGE_FOLLOWUP_SECONDS:-8} FQG_BRIDGE_PROGRESS_PUSH_COUNT=${FQG_BRIDGE_PROGRESS_PUSH_COUNT:-3} FQG_BRIDGE_COMPLETION_WAIT_SECONDS=${FQG_BRIDGE_COMPLETION_WAIT_SECONDS:-45} FQG_BRIDGE_COMPLETION_POLL_SECONDS=${FQG_BRIDGE_COMPLETION_POLL_SECONDS:-3} FQG_BRIDGE_COMPLETION_MAX_WAIT_SECONDS=${FQG_BRIDGE_COMPLETION_MAX_WAIT_SECONDS:-300} FQG_BRIDGE_REPLY_RETRY_ATTEMPTS=${FQG_BRIDGE_REPLY_RETRY_ATTEMPTS:-4} FQG_BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS=${FQG_BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS:-0.8} FQG_BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS=${FQG_BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS:-6} FQG_BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK=${FQG_BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK:-0} FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS=${FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS:-600} FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS=${FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS:-7200} FQG_BRIDGE_HEARTBEAT_FILE=${FQG_BRIDGE_HEARTBEAT_FILE:-${BRIDGE_HEARTBEAT_FILE_DEFAULT}} FQG_BRIDGE_HEARTBEAT_WRITE_INTERVAL_SECONDS=${FQG_BRIDGE_HEARTBEAT_WRITE_INTERVAL_SECONDS:-5} FQG_BRIDGE_TRUSTED_DECISION_TOKEN=${FQG_BRIDGE_TRUSTED_DECISION_TOKEN:-${FQG_TRUSTED_BRIDGE_DECISION_TOKEN:-}} FQG_BRIDGE_APPROVER=${FQG_BRIDGE_APPROVER:-guixianren-bridge} ${PYTHON_BIN} scripts/run_dingtalk_stream_bridge.py >> ${BRIDGE_LOG} 2>&1; exit_code=\$?; echo \"\$(date '+%F %T') bridge_process_exit code=\${exit_code} restart_in=2s\" >> ${BRIDGE_LOG}; sleep 2; done"

  tmux_start_session "${API_SESSION}" "${api_cmd}"
  sleep 1
  tmux_start_session "${BRIDGE_SESSION}" "${bridge_cmd}"
  sleep 1

  if ! tmux_has_session "${API_SESSION}"; then
    echo "api_tmux_session_missing_after_start" >&2
    exit 1
  fi
  if ! tmux_has_session "${BRIDGE_SESSION}"; then
    echo "bridge_tmux_session_missing_after_start" >&2
    exit 1
  fi
  if ! wait_healthz 20; then
    echo "healthz_not_ready_after_start:${BASE_URL}/healthz" >&2
    exit 1
  fi
  if [[ "${STACK_PREWARM_ENABLE}" == "1" ]]; then
    if [[ "${STACK_FORCE_RESET_GUARD_SERVER_ON_START}" == "1" ]]; then
      reset_guard_server_for_leader "${routes_path}" || true
    fi
    if ! prewarm_guard_session "${routes_path}"; then
      echo "guard_prewarm_failed_for_leader:${LEADER_ID}" >&2
      exit 1
    fi
  fi
  if ! wait_bridge_ready "${STACK_BRIDGE_READY_WAIT_SECONDS}"; then
    echo "bridge_not_ready_after_start:${BRIDGE_LOG}" >&2
    exit 1
  fi

  echo "local_stack_started"
  print_status
}

stop_stack() {
  tmux_kill_session "${BRIDGE_SESSION}"
  tmux_kill_session "${API_SESSION}"
  echo "local_stack_stopped"
}

usage() {
  cat <<EOF
Usage: $(basename "$0") <start|stop|restart|status|prewarm>

Environment overrides:
  FQG_PORT (default: 3001)
  FQG_HOST (default: 127.0.0.1)
  FQG_LOCAL_STACK_PYTHON_BIN (optional: explicit python path)
  FQG_IDENTITY_ROUTES_PATH (default: .runtime/identity_routes.local.custom.json)
  FQG_CHAT_LEADER_IDENTITY_ID (default: custom-creative-ecom-analyst)
  FQG_CHAT_COLLAB_IDENTITY_ID (default: same as leader)
  FQG_DINGTALK_STREAM_CLIENT_ID / FQG_DINGTALK_STREAM_CLIENT_SECRET
  FQG_BRIDGE_REQUIRE_AT (default: 0, can set 1 for group @bot fail-close)
  FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS (default: 600)
  FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS (default: 7200)
  FQG_BRIDGE_ALLOW_IDLE_RESTART_ZERO (default: 0; set 1 only for explicit troubleshooting)
  FQG_BRIDGE_HEARTBEAT_FILE (default: ${RUNTIME_DIR}/bridge_heartbeat.json)
  FQG_BRIDGE_HEARTBEAT_WRITE_INTERVAL_SECONDS (default: 5)
  FQG_TRUSTED_BRIDGE_DECISION_TOKEN (enables bot text approve/reject)
  FQG_BRIDGE_APPROVER (default: guixianren-bridge)
  FQG_STACK_PREWARM_ENABLE (default: 1)
  FQG_STACK_PREWARM_WAIT_SECONDS (default: 25)
  FQG_STACK_BRIDGE_READY_WAIT_SECONDS (default: 20)
  FQG_STACK_FORCE_RESET_GUARD_SERVER_ON_START (default: 1; restart/start only)
  prewarm: ensure leader guard tmux/codex session is alive without restarting api/bridge
EOF
}

main() {
  local action="${1:-status}"
  case "${action}" in
    start) start_stack ;;
    stop) stop_stack ;;
    restart) stop_stack; start_stack ;;
    status) print_status ;;
    prewarm) prewarm_only ;;
    -h|--help|help) usage ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
