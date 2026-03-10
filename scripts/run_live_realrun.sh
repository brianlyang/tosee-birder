#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
CURL_MAX_TIME_SECONDS="${FQG_LIVE_CURL_MAX_TIME_SECONDS:-20}"
CURL_POST_MAX_TIME_SECONDS="${FQG_LIVE_POST_MAX_TIME_SECONDS:-35}"

ts() {
  date '+%Y%m%d_%H%M%S'
}

get_last_msg() {
  local snap="$1"
  jq -r '.items[] | select(.identity_id=="feiqiao-guard-delivery-lead") | (.last_agent_message // "")' "${snap}"
}

poll_marker() {
  local marker="$1"
  local max_rounds="${2:-20}"
  local prefix="$3"
  local i
  for i in $(seq 1 "${max_rounds}"); do
    local snap="${OUT}/${prefix}_${i}.json"
    if ! curl -sS --connect-timeout 3 --max-time "${CURL_MAX_TIME_SECONDS}" \
      http://127.0.0.1:3001/v1/chat/leader/snapshot >"${snap}"; then
      echo "poll_marker_snapshot_curl_failed round=${i}" >>"${OUT}/${prefix}_errors.log"
      sleep 2
      continue
    fi
    local msg
    msg="$(get_last_msg "${snap}")"
    if echo "${msg}" | rg -q "^${marker}"; then
      echo "${msg}" >"${OUT}/${prefix}_seen_message.txt"
      return 0
    fi
    sleep 2
  done
  return 1
}

send_leader() {
  local payload="$1"
  local out="$2"
  curl -sS --connect-timeout 3 --max-time "${CURL_POST_MAX_TIME_SECONDS}" -X POST http://127.0.0.1:3001/v1/chat/leader/command \
    -H "Content-Type: application/json" \
    --data-binary @"${payload}" >"${out}" || {
      echo "{\"accepted\":false,\"error\":\"leader_command_curl_failed\"}" >"${out}"
      return 1
    }
}

DAY="$(date +%Y-%m-%d)"
RUN_TS="$(ts)"
OUT="artifacts/ops/${DAY}/live_realrun_${RUN_TS}"
mkdir -p "${OUT}"

curl -sS --connect-timeout 3 --max-time "${CURL_MAX_TIME_SECONDS}" \
  http://127.0.0.1:3001/healthz >"${OUT}/healthz.json"
curl -sS --connect-timeout 3 --max-time "${CURL_MAX_TIME_SECONDS}" \
  http://127.0.0.1:3001/v1/chat/routes >"${OUT}/routes.json"
curl -sS --connect-timeout 3 --max-time "${CURL_MAX_TIME_SECONDS}" \
  http://127.0.0.1:3001/v1/chat/leader/snapshot >"${OUT}/snapshot_before.json"

TOKEN="TOK-${RUN_TS: -6}"
M1="LIVEW-${RUN_TS: -4}"
M2="LIVER-${RUN_TS: -4}"

cat >"${OUT}/write_payload.json" <<JSON
{"message":"实跑写入：请记住口令 ${TOKEN}。只回复 ${M1} ACK ${TOKEN}","auto_collab":false,"verify_seconds":20,"metadata":{"channel":"selftest","task_tag":"live_realrun_${RUN_TS}","case":"write"}}
JSON
send_leader "${OUT}/write_payload.json" "${OUT}/write_response.json" || true

WRITE_SEEN=0
if poll_marker "${M1} ACK ${TOKEN}" 20 "snapshot_write"; then
  WRITE_SEEN=1
else
  for r in 1 2; do
    cat >"${OUT}/write_retry_${r}.json" <<JSON
{"message":"继续执行上一条写入口令任务，不要解释。只回复 ${M1} ACK ${TOKEN}","auto_collab":false,"verify_seconds":20,"metadata":{"channel":"selftest","task_tag":"live_realrun_${RUN_TS}","case":"write_retry_${r}"}}
JSON
    send_leader "${OUT}/write_retry_${r}.json" "${OUT}/write_retry_resp_${r}.json" || true
    if poll_marker "${M1} ACK ${TOKEN}" 20 "snapshot_write_retry_${r}"; then
      WRITE_SEEN=1
      break
    fi
  done
fi

cat >"${OUT}/recall_payload.json" <<JSON
{"message":"实跑读取：请只回答刚才口令，不要解释。只回复 ${M2} VALUE <口令>","auto_collab":false,"verify_seconds":20,"metadata":{"channel":"selftest","task_tag":"live_realrun_${RUN_TS}","case":"recall"}}
JSON
send_leader "${OUT}/recall_payload.json" "${OUT}/recall_response.json" || true

RECALL_SEEN=0
RECALLED=""
if poll_marker "${M2} VALUE" 20 "snapshot_recall"; then
  RECALL_SEEN=1
  MSG="$(cat "${OUT}/snapshot_recall_seen_message.txt")"
  RECALLED="$(echo "${MSG}" | sed -n "s/.*${M2} VALUE \\([A-Za-z0-9_.:\\/-]*\\).*/\\1/p" | head -n1)"
else
  for r in 1 2; do
    cat >"${OUT}/recall_retry_${r}.json" <<JSON
{"message":"继续执行上一条读取任务，不要解释。只回复 ${M2} VALUE <口令>","auto_collab":false,"verify_seconds":20,"metadata":{"channel":"selftest","task_tag":"live_realrun_${RUN_TS}","case":"recall_retry_${r}"}}
JSON
    send_leader "${OUT}/recall_retry_${r}.json" "${OUT}/recall_retry_resp_${r}.json" || true
    if poll_marker "${M2} VALUE" 20 "snapshot_recall_retry_${r}"; then
      RECALL_SEEN=1
      MSG="$(cat "${OUT}/snapshot_recall_retry_${r}_seen_message.txt")"
      RECALLED="$(echo "${MSG}" | sed -n "s/.*${M2} VALUE \\([A-Za-z0-9_.:\\/-]*\\).*/\\1/p" | head -n1)"
      break
    fi
  done
fi

curl -sS --connect-timeout 3 --max-time "${CURL_MAX_TIME_SECONDS}" \
  http://127.0.0.1:3001/v1/identity/memory/feiqiao-guard-delivery-lead >"${OUT}/memory.json"

HEALTH_OK="$(jq -r '.status=="ok"' "${OUT}/healthz.json")"
ROUTE_OK="$(jq -r '.items[] | select(.identity_id=="feiqiao-guard-delivery-lead") | ((.route_status=="ok") and (.session_id!=null) and (.codex_home!=null))' "${OUT}/routes.json" | head -n1)"
WRITE_ACCEPTED="$(jq -r '.accepted==true' "${OUT}/write_response.json")"
WRITE_STATE="$(jq -r '.leader_result.delivery_state // ""' "${OUT}/write_response.json")"
RECALL_ACCEPTED="$(jq -r '.accepted==true' "${OUT}/recall_response.json")"
RECALL_STATE="$(jq -r '.leader_result.delivery_state // ""' "${OUT}/recall_response.json")"
RETURNS_60="$(jq -r '.returned_turns==60' "${OUT}/memory.json")"
TIER_OK="$(jq -r '(.tier_counts.fresh==20 and .tier_counts.stable==20 and .tier_counts.archive==20)' "${OUT}/memory.json")"

PASS=1
if [[ "${HEALTH_OK}" != "true" ]]; then PASS=0; fi
if [[ "${ROUTE_OK}" != "true" ]]; then PASS=0; fi
if [[ "${WRITE_ACCEPTED}" != "true" ]]; then PASS=0; fi
if [[ "${WRITE_STATE}" != "confirmed" && "${WRITE_STATE}" != "queued" ]]; then PASS=0; fi
if [[ "${WRITE_SEEN}" != "1" ]]; then PASS=0; fi
if [[ "${RECALL_ACCEPTED}" != "true" ]]; then PASS=0; fi
if [[ "${RECALL_STATE}" != "confirmed" && "${RECALL_STATE}" != "queued" ]]; then PASS=0; fi
if [[ "${RECALL_SEEN}" != "1" ]]; then PASS=0; fi
if [[ "${RECALLED}" != "${TOKEN}" ]]; then PASS=0; fi
if [[ "${RETURNS_60}" != "true" ]]; then PASS=0; fi
if [[ "${TIER_OK}" != "true" ]]; then PASS=0; fi

jq -n \
  --arg out "${OUT}" \
  --arg token "${TOKEN}" \
  --arg recalled "${RECALLED}" \
  --argjson health_ok "${HEALTH_OK}" \
  --argjson route_ok "${ROUTE_OK}" \
  --argjson write_accepted "${WRITE_ACCEPTED}" \
  --arg write_state "${WRITE_STATE}" \
  --argjson write_seen "${WRITE_SEEN}" \
  --argjson recall_accepted "${RECALL_ACCEPTED}" \
  --arg recall_state "${RECALL_STATE}" \
  --argjson recall_seen "${RECALL_SEEN}" \
  --argjson returns_60 "${RETURNS_60}" \
  --argjson tier_ok "${TIER_OK}" \
  --argjson pass "${PASS}" \
  '{
    out_dir:$out,
    token:$token,
    recalled_token:$recalled,
    checks:{
      health_ok:$health_ok,
      route_ok:$route_ok,
      write_accepted:$write_accepted,
      write_state:$write_state,
      write_seen:($write_seen==1),
      recall_accepted:$recall_accepted,
      recall_state:$recall_state,
      recall_seen:($recall_seen==1),
      recall_token_match:($recalled==$token),
      memory_returned_60:$returns_60,
      memory_tier_20_20_20:$tier_ok
    },
    overall:(if $pass==1 then "PASS" else "FAIL" end)
  }' >"${OUT}/result.json"

cat "${OUT}/result.json"
