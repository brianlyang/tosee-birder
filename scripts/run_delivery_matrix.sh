#!/usr/bin/env bash
set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_HOME_DEFAULT="${ROOT_DIR}/.runtime/codex_isolated/codex_home"

EVIDENCE_DIR=""
CODEX_HOME="${CODEX_HOME_DEFAULT}"
SESSION_ID=""
ENABLE_CONTINUE=0
ENABLE_LOCAL_SMOKE=0
ENABLE_SERVER_SMOKE=0
LOCAL_SMOKE_BASE_URL="${LOCAL_SMOKE_BASE_URL:-http://127.0.0.1:8765}"
SERVER_HOST="${SERVER_HOST:-8.140.215.219}"
SERVER_USER="${SERVER_USER:-root}"
SERVER_BASE_URL="${SERVER_BASE_URL:-http://127.0.0.1:3001}"
SERVER_PUBLIC_BASE_URL="${SERVER_PUBLIC_BASE_URL:-http://${SERVER_HOST}:3001}"
CONTINUE_TEXT="${CONTINUE_TEXT:-[matrix-check] continue path verification}"
VERIFY_SECONDS="${VERIFY_SECONDS:-8}"

usage() {
  cat <<'EOF'
Usage: run_delivery_matrix.sh [options]

Options:
  --evidence-dir <path>       Evidence output directory (default: /tmp/fqg_matrix_YYYYmmdd_HHMMSS)
  --codex-home <path>         Override CODEX_HOME for guarded/watchdog checks
  --session-id <sid>          Codex session id for continue/watchdog checks
  --enable-continue           Run guarded_session_control continue check
  --enable-local-smoke        Run local smoke_core_flow against --local-smoke-base-url
  --enable-server-smoke       Run remote smoke on server via sshpass + SSH_PASSWORD
  --local-smoke-base-url <u>  Local smoke base URL (default: http://127.0.0.1:8765)
  --server-host <host>        Remote server host (default: 8.140.215.219)
  --server-user <user>        Remote server user (default: root)
  --server-base-url <url>     Remote smoke base URL (default: http://127.0.0.1:3001)
  --server-public-base-url <u> Public smoke URL for HTTP fallback (default: http://<server-host>:3001)
  --help                      Show this help

Env:
  SSH_PASSWORD                Optional for --enable-server-smoke; when absent/failing, fallback uses --server-public-base-url
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --evidence-dir) EVIDENCE_DIR="$2"; shift 2 ;;
    --codex-home) CODEX_HOME="$2"; shift 2 ;;
    --session-id) SESSION_ID="$2"; shift 2 ;;
    --enable-continue) ENABLE_CONTINUE=1; shift ;;
    --enable-local-smoke) ENABLE_LOCAL_SMOKE=1; shift ;;
    --enable-server-smoke) ENABLE_SERVER_SMOKE=1; shift ;;
    --local-smoke-base-url) LOCAL_SMOKE_BASE_URL="$2"; shift 2 ;;
    --server-host) SERVER_HOST="$2"; shift 2 ;;
    --server-user) SERVER_USER="$2"; shift 2 ;;
    --server-base-url) SERVER_BASE_URL="$2"; shift 2 ;;
    --server-public-base-url) SERVER_PUBLIC_BASE_URL="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${EVIDENCE_DIR}" ]]; then
  EVIDENCE_DIR="/tmp/fqg_matrix_$(date +%Y%m%d_%H%M%S)"
fi
mkdir -p "${EVIDENCE_DIR}"

if [[ -z "${SESSION_ID}" && ${ENABLE_CONTINUE} -eq 1 ]]; then
  SID_FILE="${CODEX_HOME}/last_codex_session_id"
  if [[ -f "${SID_FILE}" ]]; then
    SESSION_ID="$(tr -d '[:space:]' < "${SID_FILE}")"
  fi
fi

SUMMARY_TSV="${EVIDENCE_DIR}/command_summary.tsv"
SUMMARY_JSON="${EVIDENCE_DIR}/status.json"
echo -e "step\trc\trequired\tlog\tcommand" > "${SUMMARY_TSV}"

REQUIRED_FAIL=0
LAST_RC=0

run_step() {
  local step="$1"
  local required="$2"
  shift 2
  local log="${EVIDENCE_DIR}/${step}.log"
  local rc=0
  local errexit_restore=0
  if [[ $- == *e* ]]; then
    errexit_restore=1
    set +e
  fi
  (
    printf '$'
    printf ' %q' "$@"
    printf '\n'
    "$@"
    rc=$?
    printf 'RC:%s\n' "${rc}"
    exit "${rc}"
  ) > "${log}" 2>&1
  rc=$?
  LAST_RC=$rc
  if [[ ${errexit_restore} -eq 1 ]]; then
    set -e
  fi
  echo -e "${step}\t${rc}\t${required}\t${log}\t$*" >> "${SUMMARY_TSV}"
  if [[ "${required}" == "required" && ${rc} -ne 0 ]]; then
    REQUIRED_FAIL=$((REQUIRED_FAIL + 1))
  fi
}

probe_watchdog_state() {
  local sid="$1"
  local log="$2"
  local rc=0
  local errexit_restore=0
  if [[ $- == *e* ]]; then
    errexit_restore=1
    set +e
  fi
  (
    printf '$'
    printf ' %q' python3 "${ROOT_DIR}/scripts/watch_guarded_session.py" --session-id "${sid}" --codex-home "${CODEX_HOME}" --json
    printf '\n'
    python3 "${ROOT_DIR}/scripts/watch_guarded_session.py" \
      --session-id "${sid}" \
      --codex-home "${CODEX_HOME}" \
      --json
    rc=$?
    printf 'RC:%s\n' "${rc}"
    exit "${rc}"
  ) > "${log}" 2>&1
  rc=$?
  if [[ ${errexit_restore} -eq 1 ]]; then
    set -e
  fi
  if [[ ${rc} -ne 0 ]]; then
    return "${rc}"
  fi
  python3 - "${log}" <<'PY'
import json
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore").strip().splitlines()
for line in reversed(text):
    line = line.strip()
    if not line or not line.startswith("{"):
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    state = str(obj.get("state", "")).strip()
    print(state)
    raise SystemExit(0)
print("")
PY
}

set -e

run_step "01_py_compile" "required" \
  python3 -m py_compile \
  "${ROOT_DIR}/scripts/guarded_session_control.py" \
  "${ROOT_DIR}/scripts/run_dingtalk_stream_bridge.py" \
  "${ROOT_DIR}/scripts/watch_guarded_session.py" \
  "${ROOT_DIR}/src/feiqiao_guard/approval_service.py" \
  "${ROOT_DIR}/src/feiqiao_guard/chat_bridge.py" \
  "${ROOT_DIR}/src/feiqiao_guard/dingtalk_client.py" \
  "${ROOT_DIR}/src/feiqiao_guard/identity_router.py" \
  "${ROOT_DIR}/src/feiqiao_guard/main.py" \
  "${ROOT_DIR}/tests/test_guarded_session_control.py" \
  "${ROOT_DIR}/tests/test_watchdog_auto_continue.py" \
  "${ROOT_DIR}/tests/test_approval_flow.py" \
  "${ROOT_DIR}/tests/test_chat_bridge.py" \
  "${ROOT_DIR}/tests/test_dingtalk_client.py" \
  "${ROOT_DIR}/tests/test_chat_inbound.py"

run_step "02_help_guarded" "required" \
  python3 "${ROOT_DIR}/scripts/guarded_session_control.py" --help

run_step "03_help_watchdog" "required" \
  python3 "${ROOT_DIR}/scripts/watch_guarded_session.py" --help

run_step "04_pytest_main" "required" \
  env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  "${ROOT_DIR}/tests/test_guarded_session_control.py" \
  "${ROOT_DIR}/tests/test_watchdog_auto_continue.py"

run_step "05_pytest_single_guarded" "required" \
  env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  "${ROOT_DIR}/tests/test_guarded_session_control.py"

run_step "06_pytest_single_watchdog" "required" \
  env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  "${ROOT_DIR}/tests/test_watchdog_auto_continue.py"

run_step "06b_pytest_approval_flow" "required" \
  env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  "${ROOT_DIR}/tests/test_approval_flow.py"

run_step "06c_pytest_dingtalk_client" "required" \
  env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  "${ROOT_DIR}/tests/test_dingtalk_client.py"

run_step "06d_pytest_chat_inbound" "required" \
  env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  "${ROOT_DIR}/tests/test_chat_inbound.py"

run_step "06e_pytest_chat_bridge" "required" \
  env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  "${ROOT_DIR}/tests/test_chat_bridge.py"

run_step "07_per_function" "required" \
  env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 "${ROOT_DIR}/scripts/per_function_validation.py" \
  --test-file "tests/test_guarded_session_control.py" \
  --test-file "tests/test_watchdog_auto_continue.py" \
  --test-file "tests/test_approval_flow.py" \
  --test-file "tests/test_chat_bridge.py" \
  --test-file "tests/test_dingtalk_client.py" \
  --test-file "tests/test_chat_inbound.py"

if [[ ${ENABLE_CONTINUE} -eq 1 ]]; then
  if [[ -z "${SESSION_ID}" ]]; then
    echo "session_id_missing" > "${EVIDENCE_DIR}/08_guarded_continue.log"
    echo -e "08_guarded_continue\t2\trequired\t${EVIDENCE_DIR}/08_guarded_continue.log\tsession_id_missing" >> "${SUMMARY_TSV}"
    REQUIRED_FAIL=$((REQUIRED_FAIL + 1))
  else
    PRECHECK_LOG="${EVIDENCE_DIR}/08a_continue_precheck.log"
    if WATCHDOG_STATE="$(probe_watchdog_state "${SESSION_ID}" "${PRECHECK_LOG}")"; then
      WATCHDOG_STATE="${WATCHDOG_STATE:-UNKNOWN}"
      echo -e "08a_continue_precheck\t0\toptional\t${PRECHECK_LOG}\tstate=${WATCHDOG_STATE}" >> "${SUMMARY_TSV}"
      if [[ "${WATCHDOG_STATE}" == "WAITING_INPUT" ]]; then
        run_step "08_guarded_continue" "required" \
          python3 "${ROOT_DIR}/scripts/guarded_session_control.py" continue \
          --session-id "${SESSION_ID}" \
          --codex-home "${CODEX_HOME}" \
          --workspace-root "${ROOT_DIR}" \
          --text "${CONTINUE_TEXT}" \
          --verify-seconds "${VERIFY_SECONDS}" \
          --json
      else
        echo "skip_not_waiting_input state=${WATCHDOG_STATE}" > "${EVIDENCE_DIR}/08_guarded_continue.log"
        echo -e "08_guarded_continue\t0\toptional\t${EVIDENCE_DIR}/08_guarded_continue.log\tskip_not_waiting_input state=${WATCHDOG_STATE}" >> "${SUMMARY_TSV}"
      fi
    else
      echo -e "08a_continue_precheck\t1\toptional\t${PRECHECK_LOG}\twatchdog_precheck_failed" >> "${SUMMARY_TSV}"
      echo "skip_not_waiting_input state=UNAVAILABLE precheck_failed" > "${EVIDENCE_DIR}/08_guarded_continue.log"
      echo -e "08_guarded_continue\t0\toptional\t${EVIDENCE_DIR}/08_guarded_continue.log\tskip_not_waiting_input state=UNAVAILABLE precheck_failed" >> "${SUMMARY_TSV}"
    fi
  fi
else
  echo "skip_continue_check" > "${EVIDENCE_DIR}/08_guarded_continue.log"
  echo -e "08_guarded_continue\t0\toptional\t${EVIDENCE_DIR}/08_guarded_continue.log\tskip_continue_check" >> "${SUMMARY_TSV}"
fi

if [[ -n "${SESSION_ID}" ]]; then
  run_step "09_watchdog_status" "required" \
    python3 "${ROOT_DIR}/scripts/watch_guarded_session.py" \
    --session-id "${SESSION_ID}" \
    --codex-home "${CODEX_HOME}" \
    --json
else
  echo "skip_watchdog_no_session_id" > "${EVIDENCE_DIR}/09_watchdog_status.log"
  echo -e "09_watchdog_status\t0\toptional\t${EVIDENCE_DIR}/09_watchdog_status.log\tskip_watchdog_no_session_id" >> "${SUMMARY_TSV}"
fi

if [[ ${ENABLE_LOCAL_SMOKE} -eq 1 ]]; then
  run_step "10_local_smoke" "optional" \
    python3 "${ROOT_DIR}/scripts/smoke_core_flow.py" --base-url "${LOCAL_SMOKE_BASE_URL}"
else
  echo "skip_local_smoke" > "${EVIDENCE_DIR}/10_local_smoke.log"
  echo -e "10_local_smoke\t0\toptional\t${EVIDENCE_DIR}/10_local_smoke.log\tskip_local_smoke" >> "${SUMMARY_TSV}"
fi

if [[ ${ENABLE_SERVER_SMOKE} -eq 1 ]]; then
  if [[ -n "${SSH_PASSWORD:-}" ]]; then
    run_step "11a_server_smoke_ssh" "optional" \
      sshpass -p "${SSH_PASSWORD}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
      "${SERVER_USER}@${SERVER_HOST}" \
      "cd /root/feiqiao-guard && python3 scripts/smoke_core_flow.py --base-url ${SERVER_BASE_URL}"
    if [[ ${LAST_RC} -eq 0 ]]; then
      echo "server_smoke_via_ssh_ok" > "${EVIDENCE_DIR}/11_server_smoke.log"
      echo -e "11_server_smoke\t0\trequired\t${EVIDENCE_DIR}/11_server_smoke.log\tserver_smoke_via_ssh_ok" >> "${SUMMARY_TSV}"
    else
      run_step "11_server_smoke" "required" \
        python3 "${ROOT_DIR}/scripts/smoke_core_flow.py" --base-url "${SERVER_PUBLIC_BASE_URL}"
    fi
  else
    run_step "11_server_smoke" "required" \
      python3 "${ROOT_DIR}/scripts/smoke_core_flow.py" --base-url "${SERVER_PUBLIC_BASE_URL}"
  fi
else
  echo "skip_server_smoke" > "${EVIDENCE_DIR}/11_server_smoke.log"
  echo -e "11_server_smoke\t0\toptional\t${EVIDENCE_DIR}/11_server_smoke.log\tskip_server_smoke" >> "${SUMMARY_TSV}"
fi

STATUS="READY"
if [[ ${REQUIRED_FAIL} -ne 0 ]]; then
  STATUS="NOT_READY"
fi

python3 - <<PY
import json
from pathlib import Path

status = {
    "status": "${STATUS}",
    "required_failures": ${REQUIRED_FAIL},
    "evidence_dir": "${EVIDENCE_DIR}",
    "summary_tsv": "${SUMMARY_TSV}",
}
Path("${SUMMARY_JSON}").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
print(json.dumps(status, ensure_ascii=False, indent=2))
PY

echo
echo "Summary: ${SUMMARY_TSV}"
echo "Status : ${SUMMARY_JSON}"
