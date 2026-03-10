#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MSG="${1:-L3排障测试：请在当前tmux/codex实例检查GLM4.6V调用环境。要求：1) 仅列出环境变量名是否存在，不要输出密钥值；2) 给出 ATTEMPT/HYPOTHESIS/PATCH/EXPECTED_EFFECT/RESULT；3) 输出 FINAL_ANSWER/EVIDENCE/NEXT_ACTION。}"
POLL_COUNT="${POLL_COUNT:-12}"
POLL_SECONDS="${POLL_SECONDS:-5}"

RUN_TS="$(date '+%Y%m%d_%H%M%S')"
RUN_DAY="$(date '+%Y-%m-%d')"
RUN_ID="glm_l3_single_command_smoke_${RUN_TS}"
OUT_DIR="$ROOT/artifacts/ops/${RUN_DAY}/${RUN_ID}"
mkdir -p "$OUT_DIR"

exec > >(tee "$OUT_DIR/console.log") 2>&1

echo "[run_id] $RUN_ID"
echo "[out_dir] $OUT_DIR"
echo "[step] restart local stack"
./scripts/local_dingtalk_tmux_stack.sh restart

echo "[step] healthz"
curl -fsS "http://127.0.0.1:3001/healthz" | tee "$OUT_DIR/healthz.json"
echo

echo "[step] dispatch leader command"
payload="$(jq -nc \
  --arg message "$MSG" \
  '{message:$message,auto_collab:false,verify_seconds:20,metadata:{channel:"selftest",task_tag:"glm-l3-single-command-smoke",case:"glm_env_reasoning"}}')"
curl -fsS -X POST "http://127.0.0.1:3001/v1/chat/leader/command" \
  -H "Content-Type: application/json" \
  -d "$payload" | tee "$OUT_DIR/dispatch.json"
echo

if [[ "$(jq -r '.accepted // false' "$OUT_DIR/dispatch.json")" != "true" ]]; then
  echo "[fail] dispatch not accepted"
  exit 2
fi

echo "[step] poll leader snapshot"
{
  echo "poll_index,timestamp,state,last_event_type,last_event_summary,last_agent_message"
  for i in $(seq 1 "$POLL_COUNT"); do
    snap_path="$OUT_DIR/snapshot_${i}.json"
    curl -fsS "http://127.0.0.1:3001/v1/chat/leader/snapshot" > "$snap_path"
    ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"
    state="$(jq -r '.items[0].state // ""' "$snap_path")"
    event_type="$(jq -r '.items[0].last_event_type // ""' "$snap_path")"
    event_summary="$(jq -r '.items[0].last_event_summary // ""' "$snap_path" | tr '\n' ' ' | sed 's/,/，/g')"
    msg="$(jq -r '.items[0].last_agent_message // ""' "$snap_path" | tr '\n' ' ' | sed 's/,/，/g')"
    echo "${i},${ts},${state},${event_type},${event_summary},${msg}"
    if [[ "$msg" == *"FINAL_ANSWER:"* ]]; then
      echo "[hint] detected FINAL_ANSWER at poll=$i"
      break
    fi
    sleep "$POLL_SECONDS"
  done
} | tee "$OUT_DIR/poll.csv"

echo "[step] collect logs"
tail -n 200 "$ROOT/.runtime/local_bridge/bridge.log" > "$OUT_DIR/bridge_tail.log" || true
tail -n 200 "$ROOT/.runtime/local_bridge/api.log" > "$OUT_DIR/api_tail.log" || true

echo "[done] report=$OUT_DIR"
