#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  run_identity_dispatch_final.sh \
    --identity-id <identity_id> \
    --question <real_user_question> \
    [--base-url <http://host:port>] \
    [--question-tag <tag>] \
    [--wait-seconds <seconds>] \
    [--poll-seconds <seconds>] \
    [--force-final-on-terminal <0|1>] \
    [--evidence-dir <dir>] \
    [--metadata-source <source>]

Example:
  bash skills/identity-dispatch-final-answer/scripts/run_identity_dispatch_final.sh \
    --base-url http://8.140.215.219:3001 \
    --identity-id custom-creative-ecom-analyst \
    --question "这个是谁？用刚才方法再跑出结果"
USAGE
}

require_cmd() {
  local c="$1"
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "[FAIL] missing command: $c" >&2
    exit 2
  fi
}

IDENTITY_ID=""
QUESTION=""
BASE_URL="http://8.140.215.219:3001"
QUESTION_TAG=""
WAIT_SECONDS=300
POLL_SECONDS=4
FORCE_FINAL_ON_TERMINAL=1
EVIDENCE_DIR=""
METADATA_SOURCE="identity_dispatch_final_skill"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --identity-id)
      IDENTITY_ID="$2"
      shift 2
      ;;
    --question)
      QUESTION="$2"
      shift 2
      ;;
    --base-url)
      BASE_URL="$2"
      shift 2
      ;;
    --question-tag)
      QUESTION_TAG="$2"
      shift 2
      ;;
    --wait-seconds)
      WAIT_SECONDS="$2"
      shift 2
      ;;
    --poll-seconds)
      POLL_SECONDS="$2"
      shift 2
      ;;
    --force-final-on-terminal)
      FORCE_FINAL_ON_TERMINAL="$2"
      shift 2
      ;;
    --evidence-dir)
      EVIDENCE_DIR="$2"
      shift 2
      ;;
    --metadata-source)
      METADATA_SOURCE="$2"
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

require_cmd curl
require_cmd python3

if [[ -z "$IDENTITY_ID" || -z "$QUESTION" ]]; then
  echo "[FAIL] --identity-id and --question are required" >&2
  usage
  exit 2
fi

if [[ -z "$QUESTION_TAG" ]]; then
  QUESTION_TAG="$(python3 - "$QUESTION" <<'PY'
import hashlib
import re
import sys
import time

q = sys.argv[1]
ts = time.strftime("%Y%m%d%H%M%S", time.localtime())
digest = hashlib.sha1(q.encode("utf-8")).hexdigest()[:8]
hint = re.sub(r"\s+", "", q)
hint = re.sub(r"[^\w\u4e00-\u9fff]+", "", hint, flags=re.UNICODE)
hint = (hint[:10] if hint else "query").lower()
print(f"RQ-{ts}-{digest}-{hint}")
PY
)"
fi

if [[ -z "$EVIDENCE_DIR" ]]; then
  TS="$(date +%Y%m%d_%H%M%S)"
  EVIDENCE_DIR="/tmp/identity_dispatch_final_${IDENTITY_ID}_${TS}"
fi
mkdir -p "$EVIDENCE_DIR"

ROUTES_JSON="$EVIDENCE_DIR/routes.json"
DISPATCH_PAYLOAD_JSON="$EVIDENCE_DIR/dispatch_payload.json"
DISPATCH_RESPONSE_JSON="$EVIDENCE_DIR/dispatch_response.json"
SNAPSHOT_POLL_JSONL="$EVIDENCE_DIR/snapshot_poll.jsonl"
NUDGE_PAYLOAD_JSON="$EVIDENCE_DIR/nudge_payload.json"
NUDGE_RESPONSE_JSON="$EVIDENCE_DIR/nudge_response.json"
FINAL_REPORT_JSON="$EVIDENCE_DIR/final_report.json"
FINAL_REPORT_MD="$EVIDENCE_DIR/final_report.md"

curl -fsS "$BASE_URL/healthz" >/dev/null
curl -sS "$BASE_URL/v1/chat/routes" > "$ROUTES_JSON"

ROUTE_CHECK="$(python3 - "$ROUTES_JSON" "$IDENTITY_ID" <<'PY'
import json
import sys
from pathlib import Path

obj = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
identity_id = sys.argv[2]
items = obj.get("items") or []
if not isinstance(items, list):
    print("route_items_invalid")
    sys.exit(4)

item = None
for it in items:
    if isinstance(it, dict) and str(it.get("identity_id", "")).strip() == identity_id:
        item = it
        break
if item is None:
    print("identity_route_not_found")
    sys.exit(5)

route_error = str(item.get("route_error") or "").strip()
if route_error:
    print(f"route_error:{route_error}")
    sys.exit(6)
print("ok")
PY
)"

if [[ "$ROUTE_CHECK" != "ok" ]]; then
  echo "QUESTION_TAG=$QUESTION_TAG"
  echo "FINAL_STATUS=FAILED"
  echo "FINAL_ANSWER=route_guard_failed:$ROUTE_CHECK"
  echo "EVIDENCE_DIR=$EVIDENCE_DIR"
  exit 1
fi

DISPATCH_MESSAGE="[$QUESTION_TAG] $QUESTION

Hard output contract: output exactly one line => FINAL_ANSWER=<raw answer>."

python3 - "$IDENTITY_ID" "$DISPATCH_MESSAGE" "$QUESTION_TAG" "$METADATA_SOURCE" <<'PY' > "$DISPATCH_PAYLOAD_JSON"
import json
import sys
identity_id, message, question_tag, source = sys.argv[1:5]
payload = {
    "identity_id": identity_id,
    "message": message,
    "metadata": {
        "source": source,
        "question_tag": question_tag,
    },
}
print(json.dumps(payload, ensure_ascii=False))
PY

curl -sS -X POST "$BASE_URL/v1/chat/inbound" \
  -H 'Content-Type: application/json' \
  -d @"$DISPATCH_PAYLOAD_JSON" > "$DISPATCH_RESPONSE_JSON"

DISPATCH_OK="$(python3 - "$DISPATCH_RESPONSE_JSON" <<'PY'
import json
import sys
from pathlib import Path
obj = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("1" if bool(obj.get("accepted", False)) else "0")
PY
)"

if [[ "$DISPATCH_OK" != "1" ]]; then
  echo "QUESTION_TAG=$QUESTION_TAG"
  echo "FINAL_STATUS=FAILED"
  echo "FINAL_ANSWER=dispatch_not_accepted"
  echo "EVIDENCE_DIR=$EVIDENCE_DIR"
  exit 1
fi

: > "$SNAPSHOT_POLL_JSONL"
FINAL_STATUS="TIMEOUT"
FINAL_ANSWER=""
LAST_STATE=""
LAST_SUMMARY=""
LAST_MESSAGE=""
NUDGE_SENT=0
ELAPSED=0

while (( ELAPSED <= WAIT_SECONDS )); do
  SNAPSHOT_RAW="$(curl -sS "$BASE_URL/v1/chat/leader/snapshot" || true)"
  if [[ -z "$SNAPSHOT_RAW" ]]; then
    sleep "$POLL_SECONDS"
    ELAPSED=$((ELAPSED + POLL_SECONDS))
    continue
  fi

  PARSED="$(printf '%s' "$SNAPSHOT_RAW" | python3 - "$IDENTITY_ID" <<'PY'
import json
import sys

identity_id = sys.argv[1]
obj = json.loads(sys.stdin.read())
state = "UNKNOWN"
summary = ""
message = ""
for it in obj.get("items") or []:
    if isinstance(it, dict) and str(it.get("identity_id", "")).strip() == identity_id:
        state = str(it.get("state", "UNKNOWN"))
        summary = str(it.get("last_event_summary", ""))
        message = str(it.get("last_agent_message", ""))
        break
print(state)
print(summary.replace("\n", "\\n"))
print(message)
PY
)"

  LAST_STATE="$(printf '%s\n' "$PARSED" | sed -n '1p')"
  LAST_SUMMARY="$(printf '%s\n' "$PARSED" | sed -n '2p')"
  LAST_MESSAGE="$(printf '%s\n' "$PARSED" | sed -n '3,$p')"

  python3 - "$QUESTION_TAG" "$IDENTITY_ID" "$ELAPSED" "$LAST_STATE" "$LAST_SUMMARY" "$LAST_MESSAGE" <<'PY' >> "$SNAPSHOT_POLL_JSONL"
import json
import sys
import time

tag, identity_id, elapsed, state, summary, message = sys.argv[1:7]
print(json.dumps({
    "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    "question_tag": tag,
    "identity_id": identity_id,
    "elapsed_seconds": int(elapsed),
    "state": state,
    "summary": summary,
    "last_agent_message": message,
}, ensure_ascii=False))
PY

  if printf '%s' "$LAST_MESSAGE" | grep -q 'FINAL_ANSWER='; then
    FINAL_ANSWER="$(printf '%s' "$LAST_MESSAGE" | python3 - <<'PY'
import re
import sys
text = sys.stdin.read()
m = re.search(r"FINAL_ANSWER\s*=\s*(.+)", text)
if m:
    print(m.group(1).strip())
else:
    print(text.strip())
PY
)"
    FINAL_STATUS="SUCCESS"
    break
  fi

  UPPER_STATE="$(printf '%s' "$LAST_STATE" | tr '[:lower:]' '[:upper:]')"
  case "$UPPER_STATE" in
    DONE_WAITING_INPUT|WAITING_INPUT|STOPPED|FAILED|ERROR|SNAPSHOT_ERROR)
      if [[ "$FORCE_FINAL_ON_TERMINAL" == "1" && "$NUDGE_SENT" == "0" ]]; then
        NUDGE_SENT=1
        NUDGE_MESSAGE="[$QUESTION_TAG] Stop exploration now. Output exactly one line: FINAL_ANSWER=<final answer>."
        python3 - "$IDENTITY_ID" "$NUDGE_MESSAGE" "$QUESTION_TAG" "$METADATA_SOURCE" <<'PY' > "$NUDGE_PAYLOAD_JSON"
import json
import sys
identity_id, message, question_tag, source = sys.argv[1:5]
payload = {
    "identity_id": identity_id,
    "message": message,
    "metadata": {
        "source": source + "_nudge",
        "question_tag": question_tag,
        "nudge": True,
    },
}
print(json.dumps(payload, ensure_ascii=False))
PY
        curl -sS -X POST "$BASE_URL/v1/chat/inbound" \
          -H 'Content-Type: application/json' \
          -d @"$NUDGE_PAYLOAD_JSON" > "$NUDGE_RESPONSE_JSON"
      elif [[ -n "$LAST_MESSAGE" ]]; then
        FINAL_ANSWER="$LAST_MESSAGE"
        FINAL_STATUS="FAILED"
        break
      fi
      ;;
  esac

  sleep "$POLL_SECONDS"
  ELAPSED=$((ELAPSED + POLL_SECONDS))
done

if [[ -z "$FINAL_ANSWER" ]]; then
  FINAL_ANSWER="$LAST_MESSAGE"
fi

python3 - "$FINAL_REPORT_JSON" "$QUESTION_TAG" "$IDENTITY_ID" "$FINAL_STATUS" "$FINAL_ANSWER" "$BASE_URL" "$EVIDENCE_DIR" "$DISPATCH_RESPONSE_JSON" "$ROUTES_JSON" "$SNAPSHOT_POLL_JSONL" "$NUDGE_RESPONSE_JSON" <<'PY'
import json
import sys
from pathlib import Path

(
    out,
    question_tag,
    identity_id,
    final_status,
    final_answer,
    base_url,
    evidence_dir,
    dispatch_response_json,
    routes_json,
    snapshot_poll_jsonl,
    nudge_response_json,
) = sys.argv[1:12]

report = {
    "question_tag": question_tag,
    "identity_id": identity_id,
    "base_url": base_url,
    "final_status": final_status,
    "final_answer": final_answer,
    "evidence_dir": evidence_dir,
    "artifacts": {
        "routes": routes_json,
        "dispatch_response": dispatch_response_json,
        "snapshot_poll": snapshot_poll_jsonl,
        "nudge_response": nudge_response_json if Path(nudge_response_json).exists() else "",
    },
}
Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

cat > "$FINAL_REPORT_MD" <<EOF_REPORT
# Identity Dispatch Final Report

- question_tag: $QUESTION_TAG
- identity_id: $IDENTITY_ID
- final_status: $FINAL_STATUS
- final_answer: $FINAL_ANSWER
- evidence_dir: $EVIDENCE_DIR
EOF_REPORT

printf 'QUESTION_TAG=%s\n' "$QUESTION_TAG"
printf 'FINAL_STATUS=%s\n' "$FINAL_STATUS"
printf 'FINAL_ANSWER=%s\n' "$FINAL_ANSWER"
printf 'EVIDENCE_DIR=%s\n' "$EVIDENCE_DIR"

if [[ "$FINAL_STATUS" != "SUCCESS" ]]; then
  exit 1
fi
