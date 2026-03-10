#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <host> <user>"
  echo "Requires env: SSH_PASSWORD"
  exit 1
fi

HOST="$1"
USER_NAME="$2"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ENV_FILE="${FQG_DEPLOY_ENV_FILE:-${ROOT_DIR}/.runtime/deploy.env}"

if [[ -f "${DEPLOY_ENV_FILE}" ]]; then
  # Optional local deploy profile; useful for stable one-command releases without
  # repeating long env lists on the shell command line.
  set -a
  # shellcheck source=/dev/null
  source "${DEPLOY_ENV_FILE}"
  set +a
fi

REMOTE_DIR="/root/feiqiao-guard"
SERVICE_PORT="${FQG_SERVICE_PORT:-3001}"
PUBLIC_BASE_URL="${FQG_PUBLIC_BASE_URL:-http://${HOST}:${SERVICE_PORT}}"
CHAT_CONTROL_TIMEOUT_SECONDS="${FQG_CHAT_CONTROL_TIMEOUT_SECONDS:-90}"
CHAT_DEFAULT_VERIFY_SECONDS="${FQG_CHAT_DEFAULT_VERIFY_SECONDS:-8}"
CHAT_LEADER_IDENTITY_ID="${FQG_CHAT_LEADER_IDENTITY_ID:-feiqiao-guard-delivery-lead}"
CHAT_COLLAB_IDENTITY_ID="${FQG_CHAT_COLLAB_IDENTITY_ID:-feiqiao-guard-collab-executor}"
DISABLE_AGENTS_ADD_DIR="${FQG_DISABLE_AGENTS_ADD_DIR:-1}"
STRIP_MCP_SERVERS="${FQG_STRIP_MCP_SERVERS:-n8n-mcp,firebase,fetcher}"
ENABLE_DINGTALK_STREAM_BRIDGE="${FQG_ENABLE_DINGTALK_STREAM_BRIDGE:-0}"
DINGTALK_STREAM_CLIENT_ID="${FQG_DINGTALK_STREAM_CLIENT_ID:-}"
DINGTALK_STREAM_CLIENT_SECRET="${FQG_DINGTALK_STREAM_CLIENT_SECRET:-}"
FORCE_REMOTE_STREAM_OFF="${FQG_FORCE_REMOTE_STREAM_OFF:-0}"
RAW_DINGTALK_WEBHOOK_URL="${FQG_DINGTALK_WEBHOOK_URL:-}"
DINGTALK_WEBHOOK_URL="${RAW_DINGTALK_WEBHOOK_URL}"
if [[ "${ENABLE_DINGTALK_STREAM_BRIDGE}" == "1" && -n "${DINGTALK_WEBHOOK_URL}" ]]; then
  echo "[guard] stream bridge is enabled; disable webhook channel to avoid wrong robot routing"
  DINGTALK_WEBHOOK_URL=""
fi
if [[ "${FORCE_REMOTE_STREAM_OFF}" == "1" ]]; then
  echo "[guard] FQG_FORCE_REMOTE_STREAM_OFF=1 -> force disable remote stream bridge"
  ENABLE_DINGTALK_STREAM_BRIDGE="0"
  DINGTALK_STREAM_CLIENT_ID=""
  DINGTALK_STREAM_CLIENT_SECRET=""
fi
BRIDGE_BASE_URL="${FQG_BRIDGE_BASE_URL:-http://127.0.0.1:${SERVICE_PORT}}"
BRIDGE_TIMEOUT_SECONDS="${FQG_BRIDGE_TIMEOUT_SECONDS:-120}"
BRIDGE_ALLOW_USER_IDS="${FQG_BRIDGE_ALLOW_USER_IDS:-}"
BRIDGE_ALLOW_CHAT_IDS="${FQG_BRIDGE_ALLOW_CHAT_IDS:-}"
BRIDGE_COMMAND_PREFIXES="${FQG_BRIDGE_COMMAND_PREFIXES:-/run,/cmd}"
BRIDGE_REQUIRE_PREFIX="${FQG_BRIDGE_REQUIRE_PREFIX:-0}"
BRIDGE_AUTO_COLLAB="${FQG_BRIDGE_AUTO_COLLAB:-1}"
BRIDGE_REQUIRE_AT="${FQG_BRIDGE_REQUIRE_AT:-1}"
BRIDGE_VERIFY_SECONDS="${FQG_BRIDGE_VERIFY_SECONDS:-6}"
BRIDGE_COLLAB_VERIFY_SECONDS="${FQG_BRIDGE_COLLAB_VERIFY_SECONDS:-6}"
BRIDGE_DEDUPE_FILE="${FQG_BRIDGE_DEDUPE_FILE:-/root/feiqiao-guard/.runtime/dingtalk_stream_dedupe.json}"
BRIDGE_DEDUPE_TTL_SECONDS="${FQG_BRIDGE_DEDUPE_TTL_SECONDS:-86400}"
BRIDGE_DEDUPE_MAX_ITEMS="${FQG_BRIDGE_DEDUPE_MAX_ITEMS:-5000}"
BRIDGE_LOG_LEVEL="${FQG_BRIDGE_LOG_LEVEL:-INFO}"
BRIDGE_FOLLOWUP_SECONDS="${FQG_BRIDGE_FOLLOWUP_SECONDS:-8}"
BRIDGE_PROGRESS_PUSH_COUNT="${FQG_BRIDGE_PROGRESS_PUSH_COUNT:-3}"
BRIDGE_COMPLETION_WAIT_SECONDS="${FQG_BRIDGE_COMPLETION_WAIT_SECONDS:-45}"
BRIDGE_COMPLETION_POLL_SECONDS="${FQG_BRIDGE_COMPLETION_POLL_SECONDS:-3}"
BRIDGE_COMPLETION_MAX_WAIT_SECONDS="${FQG_BRIDGE_COMPLETION_MAX_WAIT_SECONDS:-300}"
BRIDGE_REPLY_RETRY_ATTEMPTS="${FQG_BRIDGE_REPLY_RETRY_ATTEMPTS:-4}"
BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS="${FQG_BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS:-0.8}"
BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS="${FQG_BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS:-6}"
BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK="${FQG_BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK:-0}"
BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS="${FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS:-1800}"
BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS="${FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS:-21600}"
BRIDGE_WATCHDOG_CHECK_INTERVAL_SECONDS="${FQG_BRIDGE_WATCHDOG_CHECK_INTERVAL_SECONDS:-15}"
BRIDGE_WATCHDOG_GRACE_SECONDS="${FQG_BRIDGE_WATCHDOG_GRACE_SECONDS:-90}"
STREAM_RUNTIME_MAX_SECONDS="${FQG_STREAM_RUNTIME_MAX_SECONDS:-23400}"
NEW_SESSION_WARMUP_SECONDS="${FQG_NEW_SESSION_WARMUP_SECONDS:-120}"
NEW_SESSION_WARMUP_MARKER="${FQG_NEW_SESSION_WARMUP_MARKER:-STEP0_RESULT=READY}"
NEW_SESSION_WARMUP_FAIL_CLOSE="${FQG_NEW_SESSION_WARMUP_FAIL_CLOSE:-1}"
NEW_SESSION_WARMUP_PROMPT="${FQG_NEW_SESSION_WARMUP_PROMPT:-Please run S0 identity self-check using retained memory and output exactly STEP0_RESULT=READY, then wait for the next task.}"
ENABLE_RUNTIME_WATCHDOG="${FQG_ENABLE_RUNTIME_WATCHDOG:-1}"
WATCHDOG_INTERVAL_SECONDS="${FQG_WATCHDOG_INTERVAL_SECONDS:-60}"
WATCHDOG_LOG_FILE="${FQG_WATCHDOG_LOG_FILE:-/root/feiqiao-guard/.runtime/watchdog/runtime_watchdog.jsonl}"
WATCHDOG_RESTART_COOLDOWN_SECONDS="${FQG_WATCHDOG_RESTART_COOLDOWN_SECONDS:-8}"
E2E_IDENTITY_PROBE_ENABLE="${FQG_E2E_IDENTITY_PROBE_ENABLE:-1}"
E2E_IDENTITY_PROBE_ID="${FQG_E2E_IDENTITY_PROBE_ID:-${CHAT_LEADER_IDENTITY_ID}}"
E2E_IDENTITY_PROBE_TEXT="${FQG_E2E_IDENTITY_PROBE_TEXT:-请仅输出FINAL_ANSWER=bridge_probe_ok}"
PASSTHROUGH_ENV_NAMES="${FQG_PASSTHROUGH_ENV_NAMES:-ZAI_API_KEY,GLM_API_KEY,GLM_BASE_URL,GLM_MODEL_ID}"

_extract_secret_from_snapshots() {
  local key_name="$1"
  local snapshot_dir="${ROOT_DIR}/.runtime/codex_isolated/codex_home/shell_snapshots"
  [[ -d "${snapshot_dir}" ]] || return 0
  local f line value
  while IFS= read -r f; do
    line="$(grep -E "^export ${key_name}=" "$f" | tail -n 1 || true)"
    [[ -n "${line}" ]] || continue
    value="${line#*=}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    if [[ -n "${value}" && "${value}" != "<YOUR_KEY>" && "${value}" != *"<"* ]]; then
      printf '%s' "${value}"
      return 0
    fi
  done < <(ls -1t "${snapshot_dir}"/*.sh 2>/dev/null || true)
  return 0
}

GLM_API_KEY_SECRET="${FQG_GLM_API_KEY:-$(_extract_secret_from_snapshots GLM_API_KEY)}"
ZAI_API_KEY_SECRET="${FQG_ZAI_API_KEY:-$(_extract_secret_from_snapshots ZAI_API_KEY)}"
GLM_BASE_URL_SECRET="${FQG_GLM_BASE_URL:-$(_extract_secret_from_snapshots GLM_BASE_URL)}"
GLM_MODEL_ID_SECRET="${FQG_GLM_MODEL_ID:-$(_extract_secret_from_snapshots GLM_MODEL_ID)}"

if [[ -n "${GLM_API_KEY_SECRET}" && -z "${ZAI_API_KEY_SECRET}" ]]; then
  ZAI_API_KEY_SECRET="${GLM_API_KEY_SECRET}"
fi
if [[ -n "${ZAI_API_KEY_SECRET}" && -z "${GLM_API_KEY_SECRET}" ]]; then
  GLM_API_KEY_SECRET="${ZAI_API_KEY_SECRET}"
fi
if [[ -n "${GLM_API_KEY_SECRET}" && -z "${GLM_BASE_URL_SECRET}" ]]; then
  GLM_BASE_URL_SECRET="https://api.z.ai/api/paas/v4"
fi
if [[ -n "${GLM_API_KEY_SECRET}" && -z "${GLM_MODEL_ID_SECRET}" ]]; then
  GLM_MODEL_ID_SECRET="glm-4.6v"
fi

_b64() {
  printf '%s' "$1" | base64 | tr -d '\n'
}

GLM_API_KEY_B64="$(_b64 "${GLM_API_KEY_SECRET}")"
ZAI_API_KEY_B64="$(_b64 "${ZAI_API_KEY_SECRET}")"
GLM_BASE_URL_B64="$(_b64 "${GLM_BASE_URL_SECRET}")"
GLM_MODEL_ID_B64="$(_b64 "${GLM_MODEL_ID_SECRET}")"

if [[ -z "${SSH_PASSWORD:-}" ]]; then
  echo "SSH_PASSWORD is required"
  exit 2
fi

if ! command -v sshpass >/dev/null 2>&1; then
  echo "sshpass not found"
  exit 3
fi

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)

echo "[1/5] prepare remote dir"
sshpass -p "$SSH_PASSWORD" ssh "${SSH_OPTS[@]}" "${USER_NAME}@${HOST}" "mkdir -p ${REMOTE_DIR}"

echo "[2/5] sync project"
sshpass -p "$SSH_PASSWORD" rsync -az --delete \
  --exclude '.git' \
  --exclude '.runtime/' \
  --exclude '.venv' \
  --exclude '.env' \
  --exclude '.pytest_cache' \
  --exclude '__pycache__' \
  "${ROOT_DIR}/" "${USER_NAME}@${HOST}:${REMOTE_DIR}/"

echo "[3/5] install runtime deps"
sshpass -p "$SSH_PASSWORD" ssh "${SSH_OPTS[@]}" "${USER_NAME}@${HOST}" "bash -s" <<'REMOTE'
set -euo pipefail
apt-get update -y
apt-get install -y python3-venv curl
cd /root/feiqiao-guard
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn pydantic httpx python-multipart dingtalk-stream
if command -v npm >/dev/null 2>&1; then
  if ! command -v codex >/dev/null 2>&1; then
    npm install -g @openai/codex >/dev/null 2>&1 || true
  fi
fi
REMOTE

echo "[4/5] write env + systemd"
sshpass -p "$SSH_PASSWORD" ssh "${SSH_OPTS[@]}" "${USER_NAME}@${HOST}" \
  "SERVICE_PORT='${SERVICE_PORT}' PUBLIC_BASE_URL='${PUBLIC_BASE_URL}' \
  CHAT_CONTROL_TIMEOUT_SECONDS='${CHAT_CONTROL_TIMEOUT_SECONDS}' \
  CHAT_DEFAULT_VERIFY_SECONDS='${CHAT_DEFAULT_VERIFY_SECONDS}' \
  CHAT_LEADER_IDENTITY_ID='${CHAT_LEADER_IDENTITY_ID}' \
  CHAT_COLLAB_IDENTITY_ID='${CHAT_COLLAB_IDENTITY_ID}' \
  DISABLE_AGENTS_ADD_DIR='${DISABLE_AGENTS_ADD_DIR}' \
  STRIP_MCP_SERVERS='${STRIP_MCP_SERVERS}' \
  ENABLE_DINGTALK_STREAM_BRIDGE='${ENABLE_DINGTALK_STREAM_BRIDGE}' \
  DINGTALK_WEBHOOK_URL='${DINGTALK_WEBHOOK_URL}' \
  DINGTALK_SIGNING_SECRET='${FQG_DINGTALK_SIGNING_SECRET:-}' \
  DINGTALK_STREAM_CLIENT_ID='${DINGTALK_STREAM_CLIENT_ID}' \
  DINGTALK_STREAM_CLIENT_SECRET='${DINGTALK_STREAM_CLIENT_SECRET}' \
  BRIDGE_BASE_URL='${BRIDGE_BASE_URL}' \
  BRIDGE_TIMEOUT_SECONDS='${BRIDGE_TIMEOUT_SECONDS}' \
  BRIDGE_ALLOW_USER_IDS='${BRIDGE_ALLOW_USER_IDS}' \
  BRIDGE_ALLOW_CHAT_IDS='${BRIDGE_ALLOW_CHAT_IDS}' \
  BRIDGE_COMMAND_PREFIXES='${BRIDGE_COMMAND_PREFIXES}' \
  BRIDGE_REQUIRE_PREFIX='${BRIDGE_REQUIRE_PREFIX}' \
  BRIDGE_AUTO_COLLAB='${BRIDGE_AUTO_COLLAB}' \
  BRIDGE_REQUIRE_AT='${BRIDGE_REQUIRE_AT}' \
  BRIDGE_VERIFY_SECONDS='${BRIDGE_VERIFY_SECONDS}' \
  BRIDGE_COLLAB_VERIFY_SECONDS='${BRIDGE_COLLAB_VERIFY_SECONDS}' \
  BRIDGE_DEDUPE_FILE='${BRIDGE_DEDUPE_FILE}' \
  BRIDGE_DEDUPE_TTL_SECONDS='${BRIDGE_DEDUPE_TTL_SECONDS}' \
  BRIDGE_DEDUPE_MAX_ITEMS='${BRIDGE_DEDUPE_MAX_ITEMS}' \
  BRIDGE_LOG_LEVEL='${BRIDGE_LOG_LEVEL}' \
  BRIDGE_FOLLOWUP_SECONDS='${BRIDGE_FOLLOWUP_SECONDS}' \
  BRIDGE_PROGRESS_PUSH_COUNT='${BRIDGE_PROGRESS_PUSH_COUNT}' \
  BRIDGE_COMPLETION_WAIT_SECONDS='${BRIDGE_COMPLETION_WAIT_SECONDS}' \
  BRIDGE_COMPLETION_POLL_SECONDS='${BRIDGE_COMPLETION_POLL_SECONDS}' \
  BRIDGE_COMPLETION_MAX_WAIT_SECONDS='${BRIDGE_COMPLETION_MAX_WAIT_SECONDS}' \
  BRIDGE_REPLY_RETRY_ATTEMPTS='${BRIDGE_REPLY_RETRY_ATTEMPTS}' \
  BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS='${BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS}' \
  BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS='${BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS}' \
  BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK='${BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK}' \
  BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS='${BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS}' \
  BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS='${BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS}' \
  BRIDGE_WATCHDOG_CHECK_INTERVAL_SECONDS='${BRIDGE_WATCHDOG_CHECK_INTERVAL_SECONDS}' \
  BRIDGE_WATCHDOG_GRACE_SECONDS='${BRIDGE_WATCHDOG_GRACE_SECONDS}' \
  STREAM_RUNTIME_MAX_SECONDS='${STREAM_RUNTIME_MAX_SECONDS}' \
  NEW_SESSION_WARMUP_SECONDS='${NEW_SESSION_WARMUP_SECONDS}' \
  NEW_SESSION_WARMUP_MARKER='${NEW_SESSION_WARMUP_MARKER}' \
  NEW_SESSION_WARMUP_FAIL_CLOSE='${NEW_SESSION_WARMUP_FAIL_CLOSE}' \
  NEW_SESSION_WARMUP_PROMPT='${NEW_SESSION_WARMUP_PROMPT}' \
  ENABLE_RUNTIME_WATCHDOG='${ENABLE_RUNTIME_WATCHDOG}' \
  WATCHDOG_INTERVAL_SECONDS='${WATCHDOG_INTERVAL_SECONDS}' \
  WATCHDOG_LOG_FILE='${WATCHDOG_LOG_FILE}' \
  WATCHDOG_RESTART_COOLDOWN_SECONDS='${WATCHDOG_RESTART_COOLDOWN_SECONDS}' \
  E2E_IDENTITY_PROBE_ENABLE='${E2E_IDENTITY_PROBE_ENABLE}' \
  E2E_IDENTITY_PROBE_ID='${E2E_IDENTITY_PROBE_ID}' \
  E2E_IDENTITY_PROBE_TEXT='${E2E_IDENTITY_PROBE_TEXT}' \
  PASSTHROUGH_ENV_NAMES='${PASSTHROUGH_ENV_NAMES}' \
  GLM_API_KEY_B64='${GLM_API_KEY_B64}' \
  ZAI_API_KEY_B64='${ZAI_API_KEY_B64}' \
  GLM_BASE_URL_B64='${GLM_BASE_URL_B64}' \
  GLM_MODEL_ID_B64='${GLM_MODEL_ID_B64}' \
  LARK_WEBHOOK_URL='${FQG_LARK_WEBHOOK_URL:-}' \
  LARK_SIGNING_SECRET='${FQG_LARK_SIGNING_SECRET:-}' bash -s" <<'REMOTE'
set -euo pipefail
chown -R root:root /root/feiqiao-guard
mkdir -p /root/feiqiao-guard/.runtime/codex_isolated/codex_home
mkdir -p /root/feiqiao-guard/.runtime/codex_isolated/codex_home_lead
mkdir -p /root/feiqiao-guard/.runtime/codex_isolated/codex_home_collab
if [[ ! -f /root/feiqiao-guard/.runtime/identity_routes.json ]]; then
cat > /root/feiqiao-guard/.runtime/identity_routes.json <<'JSON'
{
  "identities": {
    "feiqiao-guard-delivery-lead": {
      "codex_home": "/root/feiqiao-guard/.runtime/codex_isolated/codex_home_lead",
      "session_name_prefix": "fqg-lead",
      "verify_seconds": 8
    },
    "feiqiao-guard-collab-executor": {
      "codex_home": "/root/feiqiao-guard/.runtime/codex_isolated/codex_home_collab",
      "session_name_prefix": "fqg-collab",
      "verify_seconds": 8
    }
  }
}
JSON
fi
python3 - <<'PY'
import json
import re
from pathlib import Path

route_path = Path("/root/feiqiao-guard/.runtime/identity_routes.json")
if not route_path.exists():
    raise SystemExit(0)

doc = json.loads(route_path.read_text(encoding="utf-8"))
identities = doc.get("identities")
if not isinstance(identities, dict):
    identities = {}
    doc["identities"] = identities

lead_key = "feiqiao-guard-delivery-lead"
collab_key = "feiqiao-guard-collab-executor"
shared_home = "/root/feiqiao-guard/.runtime/codex_isolated/codex_home"
lead_home = "/root/feiqiao-guard/.runtime/codex_isolated/codex_home_lead"
collab_home = "/root/feiqiao-guard/.runtime/codex_isolated/codex_home_collab"
base_root = Path("/root/feiqiao-guard/.runtime/codex_isolated")

lead = identities.get(lead_key) if isinstance(identities.get(lead_key), dict) else {}
collab = identities.get(collab_key) if isinstance(identities.get(collab_key), dict) else {}

for node, default_home, default_prefix in (
    (lead, lead_home, "fqg-lead"),
    (collab, collab_home, "fqg-collab"),
):
    node.setdefault("enabled", True)
    node.setdefault("session_name_prefix", default_prefix)
    node.setdefault("verify_seconds", 8)
    home = str(node.get("codex_home", "")).strip()
    if not home or home == shared_home:
        node["codex_home"] = default_home

lead_sid = str(lead.get("session_id", "")).strip()
collab_sid = str(collab.get("session_id", "")).strip()
lead_allow_shared = bool(lead.get("allow_shared_session", False))
collab_allow_shared = bool(collab.get("allow_shared_session", False))

if lead_sid and collab_sid and lead_sid == collab_sid and not (lead_allow_shared and collab_allow_shared):
    # Reset stale shared SID so each codex_home can re-establish its own isolated session id.
    lead.pop("session_id", None)
    collab.pop("session_id", None)
    lead.pop("switch_ack_ref", None)
    collab.pop("switch_ack_ref", None)

identities[lead_key] = lead
identities[collab_key] = collab

def _slug(identity_id: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9._-]+", "-", identity_id.strip().lower()).strip("-")
    return raw or "identity"

for identity_id, node in list(identities.items()):
    if not isinstance(node, dict):
        continue
    node.setdefault("enabled", True)
    node.setdefault("session_name_prefix", "fqg")
    node.setdefault("verify_seconds", 8)
    if not str(node.get("codex_home", "")).strip():
        node["codex_home"] = str(base_root / f"codex_home_{_slug(identity_id)}")
    identities[identity_id] = node

# For non-shared mode, any duplicated codex_home/session_id must be split deterministically.
home_holders: dict[str, list[str]] = {}
sid_holders: dict[str, list[str]] = {}
for identity_id, node in identities.items():
    if not isinstance(node, dict):
        continue
    if not bool(node.get("enabled", True)):
        continue
    if bool(node.get("allow_shared_session", False)):
        continue
    home = str(node.get("codex_home", "")).strip()
    sid = str(node.get("session_id", "")).strip()
    if home:
        home_holders.setdefault(home, []).append(identity_id)
    if sid:
        sid_holders.setdefault(sid, []).append(identity_id)

for home, holders in home_holders.items():
    if len(holders) <= 1:
        continue
    for identity_id in sorted(holders):
        node = identities.get(identity_id)
        if not isinstance(node, dict):
            continue
        node["codex_home"] = str(base_root / f"codex_home_{_slug(identity_id)}")
        node.pop("session_id", None)
        node.pop("switch_ack_ref", None)
        identities[identity_id] = node

for sid, holders in sid_holders.items():
    if len(holders) <= 1:
        continue
    for identity_id in sorted(holders):
        node = identities.get(identity_id)
        if not isinstance(node, dict):
            continue
        node.pop("session_id", None)
        node.pop("switch_ack_ref", None)
        identities[identity_id] = node

for node in identities.values():
    if not isinstance(node, dict):
        continue
    home = str(node.get("codex_home", "")).strip()
    if home:
        Path(home).mkdir(parents=True, exist_ok=True)

route_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
cat > /root/feiqiao-guard/.env <<ENV
FQG_HOST=0.0.0.0
FQG_PORT=$SERVICE_PORT
FQG_TIMEOUT_SECONDS=120
FQG_AUTO_EXPIRE_POLL_INTERVAL_SECONDS=5
FQG_CALLBACK_MAX_SKEW_SECONDS=300
FQG_CALLBACK_BASE_URL=$PUBLIC_BASE_URL
FQG_CALLBACK_SIGNING_SECRET=changeme-callback-secret
FQG_DEFAULT_APPROVER=zhouqihang
FQG_DINGTALK_WEBHOOK_URL=$DINGTALK_WEBHOOK_URL
FQG_DINGTALK_SIGNING_SECRET=$DINGTALK_SIGNING_SECRET
FQG_DINGTALK_STREAM_CLIENT_ID=$DINGTALK_STREAM_CLIENT_ID
FQG_DINGTALK_STREAM_CLIENT_SECRET=$DINGTALK_STREAM_CLIENT_SECRET
FQG_ENABLE_DINGTALK_STREAM_BRIDGE=$ENABLE_DINGTALK_STREAM_BRIDGE
FQG_BRIDGE_BASE_URL=$BRIDGE_BASE_URL
FQG_BRIDGE_TIMEOUT_SECONDS=$BRIDGE_TIMEOUT_SECONDS
FQG_BRIDGE_ALLOW_USER_IDS=$BRIDGE_ALLOW_USER_IDS
FQG_BRIDGE_ALLOW_CHAT_IDS=$BRIDGE_ALLOW_CHAT_IDS
FQG_BRIDGE_COMMAND_PREFIXES=$BRIDGE_COMMAND_PREFIXES
FQG_BRIDGE_REQUIRE_PREFIX=$BRIDGE_REQUIRE_PREFIX
FQG_BRIDGE_AUTO_COLLAB=$BRIDGE_AUTO_COLLAB
FQG_BRIDGE_REQUIRE_AT=$BRIDGE_REQUIRE_AT
FQG_BRIDGE_VERIFY_SECONDS=$BRIDGE_VERIFY_SECONDS
FQG_BRIDGE_COLLAB_VERIFY_SECONDS=$BRIDGE_COLLAB_VERIFY_SECONDS
FQG_BRIDGE_DEDUPE_FILE=$BRIDGE_DEDUPE_FILE
FQG_BRIDGE_DEDUPE_TTL_SECONDS=$BRIDGE_DEDUPE_TTL_SECONDS
FQG_BRIDGE_DEDUPE_MAX_ITEMS=$BRIDGE_DEDUPE_MAX_ITEMS
FQG_BRIDGE_LOG_LEVEL=$BRIDGE_LOG_LEVEL
FQG_BRIDGE_FOLLOWUP_SECONDS=$BRIDGE_FOLLOWUP_SECONDS
FQG_BRIDGE_PROGRESS_PUSH_COUNT=$BRIDGE_PROGRESS_PUSH_COUNT
FQG_BRIDGE_COMPLETION_WAIT_SECONDS=$BRIDGE_COMPLETION_WAIT_SECONDS
FQG_BRIDGE_COMPLETION_POLL_SECONDS=$BRIDGE_COMPLETION_POLL_SECONDS
FQG_BRIDGE_COMPLETION_MAX_WAIT_SECONDS=$BRIDGE_COMPLETION_MAX_WAIT_SECONDS
FQG_BRIDGE_REPLY_RETRY_ATTEMPTS=$BRIDGE_REPLY_RETRY_ATTEMPTS
FQG_BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS=$BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS
FQG_BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS=$BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS
FQG_BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK=$BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK
FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS=$BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS
FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS=$BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS
FQG_BRIDGE_WATCHDOG_CHECK_INTERVAL_SECONDS=$BRIDGE_WATCHDOG_CHECK_INTERVAL_SECONDS
FQG_BRIDGE_WATCHDOG_GRACE_SECONDS=$BRIDGE_WATCHDOG_GRACE_SECONDS
FQG_STREAM_RUNTIME_MAX_SECONDS=$STREAM_RUNTIME_MAX_SECONDS
FQG_NEW_SESSION_WARMUP_SECONDS=$NEW_SESSION_WARMUP_SECONDS
FQG_NEW_SESSION_WARMUP_MARKER=$NEW_SESSION_WARMUP_MARKER
FQG_NEW_SESSION_WARMUP_FAIL_CLOSE=$NEW_SESSION_WARMUP_FAIL_CLOSE
FQG_NEW_SESSION_WARMUP_PROMPT=$NEW_SESSION_WARMUP_PROMPT
FQG_ENABLE_RUNTIME_WATCHDOG=$ENABLE_RUNTIME_WATCHDOG
FQG_WATCHDOG_INTERVAL_SECONDS=$WATCHDOG_INTERVAL_SECONDS
FQG_WATCHDOG_LOG_FILE=$WATCHDOG_LOG_FILE
FQG_WATCHDOG_RESTART_COOLDOWN_SECONDS=$WATCHDOG_RESTART_COOLDOWN_SECONDS
FQG_E2E_IDENTITY_PROBE_ENABLE=$E2E_IDENTITY_PROBE_ENABLE
FQG_E2E_IDENTITY_PROBE_ID=$E2E_IDENTITY_PROBE_ID
FQG_E2E_IDENTITY_PROBE_TEXT=$E2E_IDENTITY_PROBE_TEXT
FQG_PASSTHROUGH_ENV_NAMES=$PASSTHROUGH_ENV_NAMES
FQG_LARK_WEBHOOK_URL=$LARK_WEBHOOK_URL
FQG_LARK_SIGNING_SECRET=$LARK_SIGNING_SECRET
FQG_IDENTITY_ROUTES_PATH=/root/feiqiao-guard/.runtime/identity_routes.json
FQG_CHAT_CONTROL_TIMEOUT_SECONDS=$CHAT_CONTROL_TIMEOUT_SECONDS
FQG_CHAT_DEFAULT_VERIFY_SECONDS=$CHAT_DEFAULT_VERIFY_SECONDS
FQG_CHAT_LEADER_IDENTITY_ID=$CHAT_LEADER_IDENTITY_ID
FQG_CHAT_COLLAB_IDENTITY_ID=$CHAT_COLLAB_IDENTITY_ID
FQG_DISABLE_AGENTS_ADD_DIR=$DISABLE_AGENTS_ADD_DIR
FQG_STRIP_MCP_SERVERS=$STRIP_MCP_SERVERS
FQG_SQLITE_PATH=/root/feiqiao-guard/resource/reports/approval_gateway.db
FQG_AUDIT_LOG_PATH=/root/feiqiao-guard/resource/reports/approval_audit.jsonl
FQG_BYPASS_SIGNATURE_VERIFICATION=true
ENV

cat > /etc/systemd/system/feiqiao-guard.service <<UNIT
[Unit]
Description=FeiQiao Guard Approval Gateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/feiqiao-guard
EnvironmentFile=/root/feiqiao-guard/.env
EnvironmentFile=-/root/feiqiao-guard/.env.secrets
Environment=PYTHONPATH=/root/feiqiao-guard/src
ExecStart=/root/feiqiao-guard/.venv/bin/python -m uvicorn tosee_birder.main:create_app --factory --host 0.0.0.0 --port $SERVICE_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable feiqiao-guard
systemctl restart feiqiao-guard

if [[ "$ENABLE_DINGTALK_STREAM_BRIDGE" == "1" ]]; then
  if [[ -z "$DINGTALK_STREAM_CLIENT_ID" || -z "$DINGTALK_STREAM_CLIENT_SECRET" ]]; then
    echo "skip stream bridge enable: missing FQG_DINGTALK_STREAM_CLIENT_ID/FQG_DINGTALK_STREAM_CLIENT_SECRET"
    systemctl disable --now feiqiao-guard-dingtalk-stream >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/feiqiao-guard-dingtalk-stream.service
    systemctl daemon-reload
    exit 0
  fi
cat > /etc/systemd/system/feiqiao-guard-dingtalk-stream.service <<UNIT
[Unit]
Description=FeiQiao Guard DingTalk Stream Bridge
After=network.target feiqiao-guard.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/feiqiao-guard
EnvironmentFile=/root/feiqiao-guard/.env
EnvironmentFile=-/root/feiqiao-guard/.env.secrets
Environment=PYTHONPATH=/root/feiqiao-guard/src
ExecStart=/root/feiqiao-guard/.venv/bin/python /root/feiqiao-guard/scripts/run_dingtalk_stream_bridge.py
Restart=always
RestartSec=3
RuntimeMaxSec=${STREAM_RUNTIME_MAX_SECONDS}

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable feiqiao-guard-dingtalk-stream
  systemctl restart feiqiao-guard-dingtalk-stream
else
  systemctl disable --now feiqiao-guard-dingtalk-stream >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/feiqiao-guard-dingtalk-stream.service
  systemctl daemon-reload
fi

if [[ "$ENABLE_RUNTIME_WATCHDOG" == "1" ]]; then
cat > /etc/systemd/system/feiqiao-guard-watchdog.service <<UNIT
[Unit]
Description=FeiQiao Guard Runtime Watchdog
After=network.target feiqiao-guard.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/root/feiqiao-guard
EnvironmentFile=/root/feiqiao-guard/.env
EnvironmentFile=-/root/feiqiao-guard/.env.secrets
ExecStart=/root/feiqiao-guard/scripts/runtime_watchdog.sh
UNIT

cat > /etc/systemd/system/feiqiao-guard-watchdog.timer <<UNIT
[Unit]
Description=Run FeiQiao Guard Runtime Watchdog periodically

[Timer]
OnBootSec=30s
OnUnitActiveSec=${WATCHDOG_INTERVAL_SECONDS}s
AccuracySec=5s
Unit=feiqiao-guard-watchdog.service

[Install]
WantedBy=timers.target
UNIT

  systemctl daemon-reload
  systemctl enable --now feiqiao-guard-watchdog.timer
  systemctl start feiqiao-guard-watchdog.service || true
else
  systemctl disable --now feiqiao-guard-watchdog.timer >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/feiqiao-guard-watchdog.timer
  rm -f /etc/systemd/system/feiqiao-guard-watchdog.service
  systemctl daemon-reload
fi

if [[ ! -f /root/feiqiao-guard/.env.secrets ]]; then
cat > /root/feiqiao-guard/.env.secrets <<'ENV'
# Optional secrets overlay (not overwritten by deploy script once created).
# Fill with real values and keep file mode 600.
# ZAI_API_KEY=your_key
# GLM_API_KEY=your_key
# GLM_BASE_URL=https://api.z.ai/api/paas/v4
# GLM_MODEL_ID=glm-4.6v
ENV
  chmod 600 /root/feiqiao-guard/.env.secrets || true
fi

python3 - <<'PY'
import base64
from pathlib import Path

secret_path = Path("/root/feiqiao-guard/.env.secrets")
raw_lines = []
if secret_path.exists():
    raw_lines = secret_path.read_text(encoding="utf-8", errors="ignore").splitlines()

comments = [ln for ln in raw_lines if ln.strip().startswith("#")]
pairs = {}
for ln in raw_lines:
    line = ln.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    pairs[k.strip()] = v.strip()

def _decode(name: str) -> str:
    import os
    raw = os.getenv(name, "").strip()
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""

glm_key = _decode("GLM_API_KEY_B64")
zai_key = _decode("ZAI_API_KEY_B64")
glm_base = _decode("GLM_BASE_URL_B64")
glm_model = _decode("GLM_MODEL_ID_B64")

if glm_key:
    pairs["GLM_API_KEY"] = glm_key
if zai_key:
    pairs["ZAI_API_KEY"] = zai_key
if glm_base:
    pairs["GLM_BASE_URL"] = glm_base
if glm_model:
    pairs["GLM_MODEL_ID"] = glm_model

ordered_keys = ["ZAI_API_KEY", "GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL_ID"]
out = []
seen = set()
for ln in comments:
    if ln not in seen:
        out.append(ln)
        seen.add(ln)
for key in ordered_keys:
    if key in pairs:
        out.append(f"{key}={pairs[key]}")
        seen.add(key)
for key, value in pairs.items():
    if key in ordered_keys:
        continue
    out.append(f"{key}={value}")

secret_path.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
secret_path.chmod(0o600)
PY
REMOTE

echo "[5/5] smoke + route check"
sshpass -p "$SSH_PASSWORD" ssh "${SSH_OPTS[@]}" "${USER_NAME}@${HOST}" "bash -s" <<REMOTE
set -euo pipefail
for i in \$(seq 1 20); do
  if curl -fsS http://127.0.0.1:${SERVICE_PORT}/healthz >/dev/null 2>&1; then
    echo "[healthz]"
    curl -fsS http://127.0.0.1:${SERVICE_PORT}/healthz
    echo
    echo "[service_status]"
    systemctl is-active feiqiao-guard
    printf 'feiqiao-guard-dingtalk-stream='
    systemctl is-active feiqiao-guard-dingtalk-stream || true
    printf 'feiqiao-guard-watchdog.timer='
    systemctl is-active feiqiao-guard-watchdog.timer || true
    printf 'feiqiao-guard-watchdog.service='
    systemctl is-active feiqiao-guard-watchdog.service || true
    echo "[routes]"
    ROUTES_RAW="\$(curl -fsS http://127.0.0.1:${SERVICE_PORT}/v1/chat/routes || true)"
    if [[ -z "\${ROUTES_RAW}" ]]; then
      echo "routes_fetch_failed"
    else
      python3 - "\${ROUTES_RAW}" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    print("routes_json_invalid")
    print(raw[:240])
    raise SystemExit(0)
print(f"total={payload.get('total', 0)}")
for item in payload.get("items", [])[:8]:
    if not isinstance(item, dict):
        continue
    identity_id = str(item.get("identity_id", "")).strip()
    route_status = str(item.get("route_status", "")).strip() or "-"
    route_error_raw = item.get("route_error")
    route_error = "" if route_error_raw is None else str(route_error_raw).strip()
    if route_error.lower() in {"none", "null", "nil"}:
        route_error = ""
    route_error = route_error or "-"
    enabled = bool(item.get("enabled", True))
    session_prefix = str(item.get("session_name_prefix", "")).strip() or "-"
    print(
        f"{identity_id}: enabled={enabled}; prefix={session_prefix}; "
        f"route_status={route_status}; route_error={route_error}"
    )
PY
    fi
    echo "[env_snapshot]"
    grep -nE "^(FQG_CHAT_LEADER_IDENTITY_ID|FQG_BRIDGE_AUTO_COLLAB|FQG_BRIDGE_REQUIRE_AT|FQG_BRIDGE_REQUIRE_PREFIX|FQG_ENABLE_DINGTALK_STREAM_BRIDGE|FQG_NEW_SESSION_WARMUP_SECONDS|FQG_NEW_SESSION_WARMUP_MARKER|FQG_NEW_SESSION_WARMUP_FAIL_CLOSE|FQG_BRIDGE_ENABLE_IDENTITY_REFUSAL_FALLBACK|FQG_BRIDGE_REPLY_RETRY_ATTEMPTS|FQG_BRIDGE_REPLY_RETRY_BASE_DELAY_SECONDS|FQG_BRIDGE_REPLY_RETRY_MAX_DELAY_SECONDS|FQG_BRIDGE_ACTIVITY_IDLE_RESTART_SECONDS|FQG_BRIDGE_FORCE_RESTART_MAX_UPTIME_SECONDS|FQG_BRIDGE_WATCHDOG_CHECK_INTERVAL_SECONDS|FQG_BRIDGE_WATCHDOG_GRACE_SECONDS|FQG_STREAM_RUNTIME_MAX_SECONDS|FQG_ENABLE_RUNTIME_WATCHDOG|FQG_WATCHDOG_INTERVAL_SECONDS|FQG_WATCHDOG_LOG_FILE)=" /root/feiqiao-guard/.env || true
    echo "[watchdog_last_event]"
    if [[ -f "${WATCHDOG_LOG_FILE}" ]]; then
      tail -n 1 "${WATCHDOG_LOG_FILE}" || true
    else
      echo "watchdog_log_not_found"
    fi
    echo "[secret_presence]"
    if [[ -f /root/feiqiao-guard/.env.secrets ]]; then
      python3 - <<'PY'
from pathlib import Path

path = Path("/root/feiqiao-guard/.env.secrets")
raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
pairs = {}
for line in raw:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    pairs[k.strip()] = bool(v.strip())
for key in ("ZAI_API_KEY", "GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL_ID"):
    print(f"{key}_present={pairs.get(key, False)}")
PY
    else
      echo "env_secrets_missing"
    fi
    echo "[stream_log_tail]"
    journalctl -u feiqiao-guard-dingtalk-stream -n 60 --no-pager || true
    if [[ "${E2E_IDENTITY_PROBE_ENABLE}" == "1" ]]; then
      echo "[identity_probe]"
      PROBE_TEXT="${E2E_IDENTITY_PROBE_TEXT} [deploy_probe_ts=\$(date +%s)]"
      PROBE_PAYLOAD="\$(python3 - "${E2E_IDENTITY_PROBE_ID}" "\${PROBE_TEXT}" <<'PY'
import json
import sys

identity_id, message = sys.argv[1:3]
payload = {
    "identity_id": identity_id,
    "message": message,
    "metadata": {
        "source": "deploy_smoke_probe",
    },
}
print(json.dumps(payload, ensure_ascii=False))
PY
)"
      PROBE_RESP="\$(curl -sS -X POST "http://127.0.0.1:${SERVICE_PORT}/v1/chat/inbound" -H 'Content-Type: application/json' -d "\${PROBE_PAYLOAD}" || true)"
      echo "\${PROBE_RESP}"
      python3 - "\${PROBE_RESP}" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
if not raw:
    print("identity_probe_failed:empty_response")
    raise SystemExit(9)
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    print("identity_probe_failed:invalid_json")
    raise SystemExit(9)

accepted = bool(payload.get("accepted", False))
delivery_state = str(payload.get("delivery_state", "")).strip()
route_source = str(payload.get("route_source", "")).strip()
if not accepted:
    print("identity_probe_failed:not_accepted")
    raise SystemExit(9)
if delivery_state not in {"confirmed", "queued"}:
    print(f"identity_probe_failed:bad_state:{delivery_state}")
    raise SystemExit(9)
print(f"identity_probe_ok accepted={accepted} delivery_state={delivery_state} route_source={route_source}")
PY
    fi
    echo
    exit 0
  fi
  sleep 1
done
echo "health check timeout on port ${SERVICE_PORT}" >&2
exit 7
REMOTE

echo "Deploy complete"
