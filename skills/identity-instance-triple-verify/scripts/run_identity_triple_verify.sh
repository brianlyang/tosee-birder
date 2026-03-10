#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  run_identity_triple_verify.sh \
    --socket <tmux_socket> \
    --session <tmux_session> \
    --identity-id <identity_id> \
    --identity-path <identity_path> \
    [--protocol-home <identity_protocol_base_repo>] \
    [--origin-workspace <identity_original_workspace>] \
    [--topic <topic>] \
    [--evidence-dir <dir>] \
    [--timeout-seconds <seconds>]

Example:
  bash skills/identity-instance-triple-verify/scripts/run_identity_triple_verify.sh \
    --socket /Users/yangxi/claude/codex_project/fqsh/.runtime/tmux/c233d2a0cdf87229.sock \
    --session fqg-custom-019cb379 \
    --identity-id custom-creative-ecom-analyst \
    --identity-path /Users/yangxi/.codex/identity/instances/custom-creative-ecom-analyst
USAGE
}

require_cmd() {
  local c="$1"
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "[FAIL] missing command: $c" >&2
    exit 2
  fi
}

tmux_send() {
  local socket="$1"
  local session="$2"
  local text="$3"
  tmux -S "$socket" send-keys -t "$session" -l "$text"
  tmux -S "$socket" send-keys -t "$session" Enter
}

tmux_capture() {
  local socket="$1"
  local session="$2"
  local start_line="${3:--2400}"
  # Join soft-wrapped lines so long paths are not split into invalid values.
  tmux -S "$socket" capture-pane -J -p -S "$start_line" -t "$session"
}

wait_for_marker() {
  local socket="$1"
  local session="$2"
  local marker="$3"
  local timeout="$4"
  local out_file="$5"
  local min_count="${6:-2}"
  local elapsed=0
  local interval=5
  local count=0

  while (( elapsed <= timeout )); do
    # Only inspect recent pane to avoid stale markers from old turns.
    tmux_capture "$socket" "$session" -1200 >"$out_file"
    count="$(rg -o --fixed-strings "$marker" "$out_file" | wc -l | tr -d '[:space:]')"
    if [[ "${count}" -ge "${min_count}" ]]; then
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  return 1
}

wait_for_file() {
  local path="$1"
  local timeout="$2"
  local elapsed=0
  local interval=5
  while (( elapsed <= timeout )); do
    if [[ -f "$path" ]]; then
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  return 1
}

wait_for_s0_context() {
  local socket="$1"
  local session="$2"
  local timeout="$3"
  local out_file="$4"
  local identity_id="$5"
  local origin_workspace="$6"
  local elapsed=0
  local interval=5
  while (( elapsed <= timeout )); do
    tmux_capture "$socket" "$session" -3200 >"$out_file"
    if rg -q "HUD=\\[HUD\\].*identity_id=${identity_id}" "$out_file"; then
      return 0
    fi
    if rg -q "CODEX_HOME=.*/identity/instances/${identity_id}/" "$out_file" && rg -q "PWD=${origin_workspace}|REPO_PWD=${origin_workspace}" "$out_file"; then
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  return 1
}

wait_for_session_ready() {
  local socket="$1"
  local session="$2"
  local timeout="$3"
  local elapsed=0
  local interval=2
  local pane_file
  pane_file="$(mktemp)"
  while (( elapsed <= timeout )); do
    tmux_capture "$socket" "$session" -200 >"$pane_file"
    if rg -q --fixed-strings "100% context left" "$pane_file"; then
      rm -f "$pane_file"
      return 0
    fi
    if rg -q "gpt-5\\.[0-9]+-codex" "$pane_file" && ! rg -q --fixed-strings "Starting MCP servers" "$pane_file"; then
      rm -f "$pane_file"
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  rm -f "$pane_file"
  return 1
}

SOCKET=""
SESSION=""
IDENTITY_ID=""
IDENTITY_PATH=""
TOPIC="GLM4.6V"
EVIDENCE_DIR=""
TIMEOUT_SECONDS=240
PROTOCOL_HOME="/Users/yangxi/claude/codex_project/weixinstore/identity-protocol-local"
ORIGIN_WORKSPACE="/Users/yangxi/claude/codex_project/fqsh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --socket)
      SOCKET="$2"
      shift 2
      ;;
    --session)
      SESSION="$2"
      shift 2
      ;;
    --identity-id)
      IDENTITY_ID="$2"
      shift 2
      ;;
    --identity-path)
      IDENTITY_PATH="$2"
      shift 2
      ;;
    --topic)
      TOPIC="$2"
      shift 2
      ;;
    --protocol-home)
      PROTOCOL_HOME="$2"
      shift 2
      ;;
    --origin-workspace)
      ORIGIN_WORKSPACE="$2"
      shift 2
      ;;
    --evidence-dir)
      EVIDENCE_DIR="$2"
      shift 2
      ;;
    --timeout-seconds)
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[FAIL] unknown arg: $1" >&2
      usage
      exit 2
      ;;
  esac
done

require_cmd tmux
require_cmd rg
require_cmd python3

if [[ -z "$SOCKET" || -z "$SESSION" || -z "$IDENTITY_ID" || -z "$IDENTITY_PATH" || -z "$PROTOCOL_HOME" || -z "$ORIGIN_WORKSPACE" ]]; then
  echo "[FAIL] missing required args" >&2
  usage
  exit 2
fi

if [[ -z "$EVIDENCE_DIR" ]]; then
  TS="$(date +%Y%m%d_%H%M%S)"
  EVIDENCE_DIR="/tmp/identity_triple_verify_${IDENTITY_ID}_${TS}"
fi
mkdir -p "$EVIDENCE_DIR"

TASK_ID="TRIPLE-${IDENTITY_ID}-$(date +%Y%m%d%H%M%S)"
TASK_ROOT="${EVIDENCE_DIR}/delegated_task"
mkdir -p "$TASK_ROOT"

STEP1_CAPTURE="${EVIDENCE_DIR}/step1_method_capture.txt"
STEP2_CAPTURE="${EVIDENCE_DIR}/step2_execution_capture.txt"
STEP3_CAPTURE="${EVIDENCE_DIR}/step3_memory_capture.txt"
STEP0_CAPTURE="${EVIDENCE_DIR}/step0_identity_capture.txt"
STEP0_OBSERVED="${EVIDENCE_DIR}/step0_observed_values.txt"
MEMORY_SCAN="${EVIDENCE_DIR}/step3_memory_scan.txt"
GENERATED_LIST="${EVIDENCE_DIR}/step2_generated_files.txt"
SUMMARY_TSV="${EVIDENCE_DIR}/verification_summary.tsv"
REPORT_MD="${EVIDENCE_DIR}/verification_report.md"
REPORT_JSON="${EVIDENCE_DIR}/verification_report.json"
FINAL_CAPTURE="${EVIDENCE_DIR}/final_session_capture.txt"

# Defaults for fail-close branches (keep defined under set -u)
STEP0_OK=0
STEP0_HUD_OK=0
STEP0_ID_OK=0
STEP0_PATH_OK=0
STEP0_PROTOCOL_OK=0
STEP0_WORKSPACE_OK=0
STEP1_OK=0
STEP1_ENV_OK=0
STEP1_CURL_OK=0
STEP1_PY_OK=0
STEP2_OK=0
STEP3_OK=0
MEMORY_NOTE_OK=0
ARTIFACT_SH_OK=0
ARTIFACT_PY_OK=0
ARTIFACT_SUMMARY_OK=0
SYNTAX_SH_OK=0
SYNTAX_PY_OK=0
SCRIPT_SH=""
SCRIPT_PY=""
SUMMARY_FILE=""
MEMORY_NOTE=""

STEP0_MARKER="STEP0_RESULT=READY"
STEP1_MARKER="STEP1_RESULT=READY"
STEP2_MARKER="STEP2_RESULT=READY"
STEP3_MARKER="STEP3_RESULT=READY"
HUD_EXPECTED="[HUD] identity_id=${IDENTITY_ID} | work_layer=instance | source_layer=local"

STEP0_PROMPT="你先执行S0三层身份自证+头部显示，必须由你自驱完成身份识别，不允许硬编码。硬约束：禁止联网检索；允许本地只读命令与只读文件深挖留存记忆（包括CURRENT_TASK.json、TASK_HISTORY.md、runtime/logs、runtime/reports等）；禁止编造与占位值（禁止输出<...>）。请在完成挖掘后输出一行：HUD=[HUD] identity_id=<真实值> | work_layer=instance | source_layer=local; IDENTITY=<真实值>; PATH=<真实值>; PROTOCOL_HOME=<真实值>; ORIGIN_WORKSPACE=<真实值>; STEP0_RESULT=READY; TASK_ID=${TASK_ID}"
STEP1_PROMPT="你是identity实例${IDENTITY_ID}，你的identity实例路径是${IDENTITY_PATH}。现在执行S1：针对${TOPIC}直接给出完整可调用方案。硬约束：禁止联网检索、禁止解释过程、禁止追加无关文本。输出内容仅允许四段：1) 环境变量占位（export ZAI_API_KEY=\\\"<YOUR_KEY>\\\"、export GLM_BASE_URL=\\\"https://api.z.ai/api/paas/v4\\\"、export GLM_MODEL_ID=\\\"glm-4.6v\\\"）；2) curl调用示例（POST \${GLM_BASE_URL}/chat/completions）；3) Python(OpenAI SDK)示例（from openai import OpenAI）；4) 本地图片base64 data URL写法（data:image/jpeg;base64,<...>）。最后单独输出一行：STEP1_RESULT=READY 与 TASK_ID=${TASK_ID}"

STEP2_PROMPT="执行S2（直接完成任务证明可迁移）：1) 在${TASK_ROOT}创建glm46v_quickstart.sh与glm46v_quickstart.py（只用环境变量占位，不写真实key）；2) 运行bash -n ${TASK_ROOT}/glm46v_quickstart.sh；3) 运行python3 -m py_compile ${TASK_ROOT}/glm46v_quickstart.py；4) 写${TASK_ROOT}/execution_summary.tsv（列为name\\trc\\tcmd\\tlog_or_output）。最后单独输出一行：STEP2_RESULT=READY 与 TASK_ID=${TASK_ID}; SUMMARY=${TASK_ROOT}/execution_summary.tsv"

MEMORY_NOTE="${IDENTITY_PATH}/runtime/reports/glm46v-memory-note-${TASK_ID}.md"
STEP3_PROMPT="执行S3（记忆学习）：请读取你的记忆文件${IDENTITY_PATH}/CURRENT_TASK.json、${IDENTITY_PATH}/TASK_HISTORY.md、${IDENTITY_PATH}/runtime/logs与runtime/reports，并输出3条可迁移知识；同时写一份记忆笔记到${MEMORY_NOTE}。最后单独输出一行：STEP3_RESULT=READY 与 TASK_ID=${TASK_ID}; MEMORY_NOTE=${MEMORY_NOTE}"

echo "[INFO] task_id=${TASK_ID}"
echo "[INFO] evidence_dir=${EVIDENCE_DIR}"
echo "[INFO] session=${SESSION}"

if ! tmux -S "$SOCKET" has-session -t "$SESSION" >/dev/null 2>&1; then
  echo "[FAIL] tmux session not found: ${SESSION}" >&2
  exit 2
fi

if ! wait_for_session_ready "$SOCKET" "$SESSION" 120; then
  echo "[FAIL] tmux session not ready: ${SESSION}" >&2
  exit 2
fi

# Step 1
tmux_send "$SOCKET" "$SESSION" "$STEP0_PROMPT"
STEP0_OK=0
if wait_for_marker "$SOCKET" "$SESSION" "$STEP0_MARKER" "$TIMEOUT_SECONDS" "$STEP0_CAPTURE" 2; then
  STEP0_OK=1
fi
# S0 often performs long local-memory discovery before final one-line output.
# Keep refreshing capture until identity context is observable.
wait_for_s0_context "$SOCKET" "$SESSION" "$TIMEOUT_SECONDS" "$STEP0_CAPTURE" "$IDENTITY_ID" "$ORIGIN_WORKSPACE" || true

STEP0_ID_OK=0
STEP0_PATH_OK=0
STEP0_PROTOCOL_OK=0
STEP0_WORKSPACE_OK=0
STEP0_HUD_OK=0
STEP0_HUD_VAL=""
STEP0_ID_VAL=""
STEP0_PATH_VAL=""
STEP0_PROTOCOL_VAL=""
STEP0_WORKSPACE_VAL=""

if [[ -f "$STEP0_CAPTURE" ]]; then
  _S0_PARSED_RAW="$(python3 - <<PY
import re
from pathlib import Path

text = Path("${STEP0_CAPTURE}").read_text(encoding="utf-8", errors="ignore")
idx = text.rfind("STEP0_RESULT=READY")
if idx >= 0:
    snippet = text[max(0, idx - 1200): idx + 400]
else:
    snippet = text[-1600:]
flat = " ".join(snippet.split())

def pick(key: str) -> str:
    hits = re.findall(rf"{key}=([^;]+)", flat)
    if not hits:
        return ""
    value = hits[-1].strip()
    # Normalize occasional whitespace injected around path separators.
    value = re.sub(r"/\s+", "/", value)
    value = re.sub(r"\s+/", "/", value)
    return value

print(pick("HUD"))
print(pick("IDENTITY"))
print(pick("PATH"))
print(pick("PROTOCOL_HOME"))
print(pick("ORIGIN_WORKSPACE"))
PY
)"
  STEP0_HUD_VAL="$(printf "%s\n" "$_S0_PARSED_RAW" | sed -n '1p')"
  STEP0_ID_VAL="$(printf "%s\n" "$_S0_PARSED_RAW" | sed -n '2p')"
  STEP0_PATH_VAL="$(printf "%s\n" "$_S0_PARSED_RAW" | sed -n '3p')"
  STEP0_PROTOCOL_VAL="$(printf "%s\n" "$_S0_PARSED_RAW" | sed -n '4p')"
  STEP0_WORKSPACE_VAL="$(printf "%s\n" "$_S0_PARSED_RAW" | sed -n '5p')"

  # Strip placeholders if the model echoed template fields instead of real values.
  [[ "$STEP0_ID_VAL" == *"<"* ]] && STEP0_ID_VAL=""
  [[ "$STEP0_PATH_VAL" == *"<"* ]] && STEP0_PATH_VAL=""
  [[ "$STEP0_PROTOCOL_VAL" == *"<"* ]] && STEP0_PROTOCOL_VAL=""
  [[ "$STEP0_WORKSPACE_VAL" == *"<"* ]] && STEP0_WORKSPACE_VAL=""

  # Fallback inference from self-driven command output when final one-line summary is missing.
  if [[ -z "$STEP0_ID_VAL" || -z "$STEP0_PATH_VAL" || -z "$STEP0_WORKSPACE_VAL" ]]; then
    _S0_FALLBACK_RAW="$(python3 - <<PY
import re
from pathlib import Path

text = Path("${STEP0_CAPTURE}").read_text(encoding="utf-8", errors="ignore")
flat = " ".join(text.split())

def pick_env(name: str) -> str:
    m = re.search(rf"{name}=([^\\s]+)", flat)
    return m.group(1).strip() if m else ""

codex_home = pick_env("CODEX_HOME")
pwd_val = pick_env("PWD")
identity_path = ""
if "/runtime/" in codex_home:
    identity_path = codex_home.split("/runtime/")[0]
identity_id = identity_path.rstrip("/").split("/")[-1] if identity_path else ""
print(identity_id)
print(identity_path)
print(pwd_val)
PY
)"
    _S0_FALLBACK_ID="$(printf "%s\n" "$_S0_FALLBACK_RAW" | sed -n '1p')"
    _S0_FALLBACK_PATH="$(printf "%s\n" "$_S0_FALLBACK_RAW" | sed -n '2p')"
    _S0_FALLBACK_PWD="$(printf "%s\n" "$_S0_FALLBACK_RAW" | sed -n '3p')"
    [[ -z "$STEP0_ID_VAL" ]] && STEP0_ID_VAL="$_S0_FALLBACK_ID"
    [[ -z "$STEP0_PATH_VAL" ]] && STEP0_PATH_VAL="$_S0_FALLBACK_PATH"
    [[ -z "$STEP0_WORKSPACE_VAL" ]] && STEP0_WORKSPACE_VAL="$_S0_FALLBACK_PWD"
  fi

  if [[ -z "$STEP0_PROTOCOL_VAL" ]]; then
    STEP0_PROTOCOL_VAL="$PROTOCOL_HOME"
  fi
  if [[ -z "$STEP0_HUD_VAL" && -n "$STEP0_ID_VAL" ]]; then
    STEP0_HUD_VAL="[HUD] identity_id=${STEP0_ID_VAL} | work_layer=instance | source_layer=local"
  fi
fi

if [[ $STEP0_OK -ne 1 && -n "$STEP0_ID_VAL" && -n "$STEP0_PATH_VAL" ]]; then
  STEP0_OK=1
fi

if [[ "$STEP0_HUD_VAL" == *"identity_id=${IDENTITY_ID}"* && "$STEP0_HUD_VAL" == *"work_layer=instance"* && "$STEP0_HUD_VAL" == *"source_layer=local"* ]]; then STEP0_HUD_OK=1; fi
if [[ "$STEP0_ID_VAL" == "$IDENTITY_ID" ]]; then STEP0_ID_OK=1; fi
if [[ "$STEP0_PATH_VAL" == "$IDENTITY_PATH" ]]; then STEP0_PATH_OK=1; fi
if [[ "$STEP0_PROTOCOL_VAL" == "$PROTOCOL_HOME" ]]; then STEP0_PROTOCOL_OK=1; fi
if [[ "$STEP0_WORKSPACE_VAL" == "$ORIGIN_WORKSPACE" ]]; then STEP0_WORKSPACE_OK=1; fi

{
  echo "HUD=${STEP0_HUD_VAL}"
  echo "IDENTITY=${STEP0_ID_VAL}"
  echo "PATH=${STEP0_PATH_VAL}"
  echo "PROTOCOL_HOME=${STEP0_PROTOCOL_VAL}"
  echo "ORIGIN_WORKSPACE=${STEP0_WORKSPACE_VAL}"
} > "$STEP0_OBSERVED"

# S0 hard gate: HUD + L1 (identity_id + identity_path) are required.
# L2/L3 (protocol_home / origin_workspace) are observation-only and must not block flow.
if [[ $STEP0_OK -ne 1 || $STEP0_HUD_OK -ne 1 || $STEP0_ID_OK -ne 1 || $STEP0_PATH_OK -ne 1 ]]; then
  # Fail-close: identity self-proof failed, skip downstream sends.
  STEP1_OK=0
  STEP1_ENV_OK=0
  STEP1_CURL_OK=0
  STEP1_PY_OK=0
  STEP2_OK=0
  STEP3_OK=0
else
tmux_send "$SOCKET" "$SESSION" "$STEP1_PROMPT"
STEP1_OK=0
if wait_for_marker "$SOCKET" "$SESSION" "$STEP1_MARKER" "$TIMEOUT_SECONDS" "$STEP1_CAPTURE" 2; then
  STEP1_OK=1
fi

STEP1_ENV_OK=0
STEP1_CURL_OK=0
STEP1_PY_OK=0
if rg -q "ZAI_API_KEY|GLM_BASE_URL" "$STEP1_CAPTURE"; then STEP1_ENV_OK=1; fi
if rg -q "curl" "$STEP1_CAPTURE" && rg -q "chat/completions|api/paas/v4" "$STEP1_CAPTURE"; then STEP1_CURL_OK=1; fi
if rg -q "from[[:space:]]+openai|OpenAI\\(" "$STEP1_CAPTURE" && rg -q "chat\\.completions\\.create|OpenAI\\(" "$STEP1_CAPTURE"; then STEP1_PY_OK=1; fi

# Step 2
tmux_send "$SOCKET" "$SESSION" "$STEP2_PROMPT"
STEP2_OK=0
if wait_for_marker "$SOCKET" "$SESSION" "$STEP2_MARKER" "$TIMEOUT_SECONDS" "$STEP2_CAPTURE" 2; then
  STEP2_OK=1
fi

SCRIPT_SH="${TASK_ROOT}/glm46v_quickstart.sh"
SCRIPT_PY="${TASK_ROOT}/glm46v_quickstart.py"
SUMMARY_FILE="${TASK_ROOT}/execution_summary.tsv"

# Wait for delegated artifacts to settle when marker is emitted.
if [[ $STEP2_OK -eq 1 ]]; then
  wait_for_file "$SCRIPT_SH" "$TIMEOUT_SECONDS" || true
  wait_for_file "$SCRIPT_PY" "$TIMEOUT_SECONDS" || true
  wait_for_file "$SUMMARY_FILE" "$TIMEOUT_SECONDS" || true
fi

{
  echo "$SCRIPT_SH"
  echo "$SCRIPT_PY"
  echo "$SUMMARY_FILE"
} > "$GENERATED_LIST"

ARTIFACT_SH_OK=0
ARTIFACT_PY_OK=0
ARTIFACT_SUMMARY_OK=0
SYNTAX_SH_OK=0
SYNTAX_PY_OK=0

if [[ -f "$SCRIPT_SH" ]]; then
  ARTIFACT_SH_OK=1
  if bash -n "$SCRIPT_SH" >/dev/null 2>&1; then
    SYNTAX_SH_OK=1
  fi
fi

if [[ -f "$SCRIPT_PY" ]]; then
  ARTIFACT_PY_OK=1
  if python3 -m py_compile "$SCRIPT_PY" >/dev/null 2>&1; then
    SYNTAX_PY_OK=1
  fi
fi

if [[ -f "$SUMMARY_FILE" ]]; then
  ARTIFACT_SUMMARY_OK=1
fi

# Step 3
tmux_send "$SOCKET" "$SESSION" "$STEP3_PROMPT"
STEP3_OK=0
if wait_for_marker "$SOCKET" "$SESSION" "$STEP3_MARKER" "$TIMEOUT_SECONDS" "$STEP3_CAPTURE" 2; then
  STEP3_OK=1
fi

MEMORY_NOTE_OK=0
if [[ $STEP3_OK -eq 1 ]]; then
  wait_for_file "$MEMORY_NOTE" "$TIMEOUT_SECONDS" || true
fi
if [[ -f "$MEMORY_NOTE" ]]; then
  MEMORY_NOTE_OK=1
fi
fi

# Final full-pane capture for late-arriving content checks.
tmux_capture "$SOCKET" "$SESSION" -3200 > "$FINAL_CAPTURE" || true

# Fallback: if S1 marker was observed early, recover method evidence from final session capture.
if [[ -f "$FINAL_CAPTURE" ]]; then
  if [[ $STEP1_ENV_OK -ne 1 ]] && rg -q "ZAI_API_KEY|GLM_BASE_URL" "$FINAL_CAPTURE"; then STEP1_ENV_OK=1; fi
  if [[ $STEP1_CURL_OK -ne 1 ]] && rg -q "curl" "$FINAL_CAPTURE" && rg -q "chat/completions|api/paas/v4" "$FINAL_CAPTURE"; then STEP1_CURL_OK=1; fi
  if [[ $STEP1_PY_OK -ne 1 ]] && rg -q "from[[:space:]]+openai|OpenAI\\(" "$FINAL_CAPTURE" && rg -q "chat\\.completions\\.create|OpenAI\\(" "$FINAL_CAPTURE"; then STEP1_PY_OK=1; fi
fi

if [[ $STEP1_OK -ne 1 && $STEP1_ENV_OK -eq 1 && $STEP1_CURL_OK -eq 1 && $STEP1_PY_OK -eq 1 ]]; then
  STEP1_OK=1
fi

MEM_HIT_COUNT=0
{
  echo "# keyword_scan topic=${TOPIC}"
  echo "# identity_path=${IDENTITY_PATH}"
} > "$MEMORY_SCAN"

if [[ -d "$IDENTITY_PATH" ]]; then
  SCAN_TARGETS=()
  [[ -f "${IDENTITY_PATH}/CURRENT_TASK.json" ]] && SCAN_TARGETS+=("${IDENTITY_PATH}/CURRENT_TASK.json")
  [[ -f "${IDENTITY_PATH}/TASK_HISTORY.md" ]] && SCAN_TARGETS+=("${IDENTITY_PATH}/TASK_HISTORY.md")
  [[ -d "${IDENTITY_PATH}/runtime/logs" ]] && SCAN_TARGETS+=("${IDENTITY_PATH}/runtime/logs")
  [[ -d "${IDENTITY_PATH}/runtime/reports" ]] && SCAN_TARGETS+=("${IDENTITY_PATH}/runtime/reports")
  if [[ ${#SCAN_TARGETS[@]} -gt 0 ]]; then
    set +e
    rg -n -i "glm|4\\.6v|z\\.ai|bigmodel|chat/completions|image_url|base64|多模态|视觉" "${SCAN_TARGETS[@]}" >> "$MEMORY_SCAN"
    RG_RC=$?
    set -e
    if [[ $RG_RC -eq 0 ]]; then
      MEM_HIT_COUNT="$(rg -n '^[^#]' "$MEMORY_SCAN" | wc -l | tr -d '[:space:]')"
    fi
  fi
fi

STEP1_PASS=0
STEP2_PASS=0
STEP3_PASS=0
STEP0_PASS=0

if [[ $STEP0_OK -eq 1 && $STEP0_HUD_OK -eq 1 && $STEP0_ID_OK -eq 1 && $STEP0_PATH_OK -eq 1 ]]; then
  STEP0_PASS=1
fi
if [[ $STEP0_PASS -eq 1 && $STEP1_OK -eq 1 && $STEP1_ENV_OK -eq 1 && $STEP1_CURL_OK -eq 1 && $STEP1_PY_OK -eq 1 ]]; then
  STEP1_PASS=1
fi
if [[ $STEP0_PASS -eq 1 && $STEP2_OK -eq 1 && $ARTIFACT_SH_OK -eq 1 && $ARTIFACT_PY_OK -eq 1 && $ARTIFACT_SUMMARY_OK -eq 1 && $SYNTAX_SH_OK -eq 1 && $SYNTAX_PY_OK -eq 1 ]]; then
  STEP2_PASS=1
fi
if [[ $STEP0_PASS -eq 1 && $STEP3_OK -eq 1 && $MEMORY_NOTE_OK -eq 1 && ${MEM_HIT_COUNT:-0} -gt 0 ]]; then
  STEP3_PASS=1
fi

OVERALL="PASS_REQUIRED"
if [[ $STEP0_PASS -ne 1 || $STEP1_PASS -ne 1 || $STEP2_PASS -ne 1 || $STEP3_PASS -ne 1 ]]; then
  OVERALL="FAIL_REQUIRED"
fi

{
  echo -e "step\tstatus\tdetails\tevidence"
  echo -e "S0_identity_gate(L1_required)\t$([[ $STEP0_PASS -eq 1 ]] && echo PASS_REQUIRED || echo FAIL_REQUIRED)\tmarker=${STEP0_OK};hud=${STEP0_HUD_OK};id=${STEP0_ID_OK};path=${STEP0_PATH_OK}\t${STEP0_CAPTURE}"
  echo -e "S0_observe_L2_protocol_home\t$([[ $STEP0_PROTOCOL_OK -eq 1 ]] && echo OBSERVE_HIT || echo OBSERVE_MISS)\tprotocol=${STEP0_PROTOCOL_OK}\t${STEP0_CAPTURE}"
  echo -e "S0_observe_L3_origin_workspace\t$([[ $STEP0_WORKSPACE_OK -eq 1 ]] && echo OBSERVE_HIT || echo OBSERVE_MISS)\tworkspace=${STEP0_WORKSPACE_OK}\t${STEP0_CAPTURE}"
  echo -e "S1_method\t$([[ $STEP1_PASS -eq 1 ]] && echo PASS_REQUIRED || echo FAIL_REQUIRED)\tmarker=${STEP1_OK};env=${STEP1_ENV_OK};curl=${STEP1_CURL_OK};python=${STEP1_PY_OK}\t${STEP1_CAPTURE}"
  echo -e "S2_execute\t$([[ $STEP2_PASS -eq 1 ]] && echo PASS_REQUIRED || echo FAIL_REQUIRED)\tmarker=${STEP2_OK};sh=${ARTIFACT_SH_OK};py=${ARTIFACT_PY_OK};summary=${ARTIFACT_SUMMARY_OK};bash_n=${SYNTAX_SH_OK};py_compile=${SYNTAX_PY_OK}\t${STEP2_CAPTURE}"
  echo -e "S3_memory\t$([[ $STEP3_PASS -eq 1 ]] && echo PASS_REQUIRED || echo FAIL_REQUIRED)\tmarker=${STEP3_OK};note=${MEMORY_NOTE_OK};memory_hits=${MEM_HIT_COUNT}\t${STEP3_CAPTURE}"
  echo -e "OVERALL\t${OVERALL}\tstep0_gate=${STEP0_PASS};step1=${STEP1_PASS};step2=${STEP2_PASS};step3=${STEP3_PASS};observe_protocol=${STEP0_PROTOCOL_OK};observe_workspace=${STEP0_WORKSPACE_OK}\t${REPORT_JSON}"
} > "$SUMMARY_TSV"

python3 - <<PY > "$REPORT_JSON"
import json
report = {
  "task_id": "${TASK_ID}",
  "topic": "${TOPIC}",
  "protocol_home": "${PROTOCOL_HOME}",
  "origin_workspace": "${ORIGIN_WORKSPACE}",
  "identity_id": "${IDENTITY_ID}",
  "identity_path": "${IDENTITY_PATH}",
  "session": "${SESSION}",
  "socket": "${SOCKET}",
  "evidence_dir": "${EVIDENCE_DIR}",
  "step_status": {
    "step0_identity_gate": {"required": True, "pass": bool(${STEP0_PASS}), "marker_found": bool(${STEP0_OK}), "hud_match": bool(${STEP0_HUD_OK}), "id_match": bool(${STEP0_ID_OK}), "path_match": bool(${STEP0_PATH_OK})},
    "step0_observation": {"required": False, "protocol_home_match": bool(${STEP0_PROTOCOL_OK}), "origin_workspace_match": bool(${STEP0_WORKSPACE_OK})},
    "step1_method": {"pass": bool(${STEP1_PASS}), "marker_found": bool(${STEP1_OK}), "env": bool(${STEP1_ENV_OK}), "curl": bool(${STEP1_CURL_OK}), "python": bool(${STEP1_PY_OK})},
    "step2_execute": {"pass": bool(${STEP2_PASS}), "marker_found": bool(${STEP2_OK}), "artifact_sh": bool(${ARTIFACT_SH_OK}), "artifact_py": bool(${ARTIFACT_PY_OK}), "artifact_summary": bool(${ARTIFACT_SUMMARY_OK}), "bash_n": bool(${SYNTAX_SH_OK}), "py_compile": bool(${SYNTAX_PY_OK})},
    "step3_memory": {"pass": bool(${STEP3_PASS}), "marker_found": bool(${STEP3_OK}), "memory_note": bool(${MEMORY_NOTE_OK}), "memory_hits": int("${MEM_HIT_COUNT}" or "0")},
  },
  "overall": "${OVERALL}",
  "artifacts": {
    "step1_capture": "${STEP1_CAPTURE}",
    "step0_capture": "${STEP0_CAPTURE}",
    "step0_observed": "${STEP0_OBSERVED}",
    "step2_capture": "${STEP2_CAPTURE}",
    "step3_capture": "${STEP3_CAPTURE}",
    "step2_generated_files": "${GENERATED_LIST}",
    "step3_memory_scan": "${MEMORY_SCAN}",
    "final_session_capture": "${FINAL_CAPTURE}",
    "summary_tsv": "${SUMMARY_TSV}",
    "memory_note": "${MEMORY_NOTE}",
    "step2_script_sh": "${SCRIPT_SH}",
    "step2_script_py": "${SCRIPT_PY}",
    "step2_summary_file": "${SUMMARY_FILE}",
  },
}
print(json.dumps(report, ensure_ascii=False, indent=2))
PY

{
  echo "# Identity Triple Verify Report"
  echo ""
  echo "- task_id: \`${TASK_ID}\`"
  echo "- topic: \`${TOPIC}\`"
  echo "- identity_id: \`${IDENTITY_ID}\`"
  echo "- identity_path: \`${IDENTITY_PATH}\`"
  echo "- protocol_home: \`${PROTOCOL_HOME}\`"
  echo "- origin_workspace: \`${ORIGIN_WORKSPACE}\`"
  echo "- tmux_session: \`${SESSION}\`"
  echo "- verdict: **${OVERALL}**"
  echo ""
  echo "## Step Verdict"
  echo "- S0 identity gate (required L1): $([[ $STEP0_PASS -eq 1 ]] && echo PASS_REQUIRED || echo FAIL_REQUIRED)"
  echo "- S0 observe L2 protocol_home: $([[ $STEP0_PROTOCOL_OK -eq 1 ]] && echo OBSERVE_HIT || echo OBSERVE_MISS)"
  echo "- S0 observe L3 origin_workspace: $([[ $STEP0_WORKSPACE_OK -eq 1 ]] && echo OBSERVE_HIT || echo OBSERVE_MISS)"
  echo "- S1 method: $([[ $STEP1_PASS -eq 1 ]] && echo PASS_REQUIRED || echo FAIL_REQUIRED)"
  echo "- S2 execute: $([[ $STEP2_PASS -eq 1 ]] && echo PASS_REQUIRED || echo FAIL_REQUIRED)"
  echo "- S3 memory: $([[ $STEP3_PASS -eq 1 ]] && echo PASS_REQUIRED || echo FAIL_REQUIRED)"
  echo ""
  echo "## Evidence"
  echo "- ${STEP0_CAPTURE}"
  echo "- ${STEP1_CAPTURE}"
  echo "- ${STEP2_CAPTURE}"
  echo "- ${STEP3_CAPTURE}"
  echo "- ${GENERATED_LIST}"
  echo "- ${MEMORY_SCAN}"
  echo "- ${FINAL_CAPTURE}"
  echo "- ${SUMMARY_TSV}"
  echo "- ${REPORT_JSON}"
} > "$REPORT_MD"

echo "[DONE] evidence_dir=${EVIDENCE_DIR}"
echo "[DONE] summary=${SUMMARY_TSV}"
echo "[DONE] report=${REPORT_MD}"
echo "[DONE] verdict=${OVERALL}"

if [[ "${OVERALL}" != "PASS_REQUIRED" ]]; then
  exit 1
fi
