#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${FQG_WATCHDOG_BASE_URL:-http://127.0.0.1:${FQG_PORT:-3001}}"
LOG_FILE="${FQG_WATCHDOG_LOG_FILE:-${ROOT_DIR}/.runtime/watchdog/runtime_watchdog.jsonl}"
REQUIRE_STREAM="${FQG_ENABLE_DINGTALK_STREAM_BRIDGE:-0}"
RESTART_COOLDOWN_SECONDS="${FQG_WATCHDOG_RESTART_COOLDOWN_SECONDS:-30}"

mkdir -p "$(dirname "${LOG_FILE}")"

now_ts() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

status_guard="$(systemctl is-active feiqiao-guard 2>/dev/null || true)"
status_stream="$(systemctl is-active feiqiao-guard-dingtalk-stream 2>/dev/null || true)"
health_ok=0
routes_ok=0
route_total=0
route_errors=""
restart_reason=""
restart_triggered=0
restart_ok=1

health_raw="$(curl -fsS --max-time 4 "${BASE_URL}/healthz" 2>/dev/null || true)"
if [[ -n "${health_raw}" ]]; then
  health_state="$(
    python3 - "${health_raw}" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    print("")
    raise SystemExit(0)
print(str(payload.get("status", "")).strip())
PY
  )"
  if [[ "${health_state}" == "ok" ]]; then
    health_ok=1
  fi
fi

routes_raw="$(curl -fsS --max-time 4 "${BASE_URL}/v1/chat/routes" 2>/dev/null || true)"
if [[ -n "${routes_raw}" ]]; then
  parsed="$(
    python3 - "${routes_raw}" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    print("0|0|routes_json_invalid")
    raise SystemExit(0)

items = payload.get("items")
if not isinstance(items, list):
    print("0|0|routes_items_invalid")
    raise SystemExit(0)

errors = []
for item in items:
    if not isinstance(item, dict):
        continue
    identity_id = str(item.get("identity_id", "")).strip() or "-"
    route_status = str(item.get("route_status", "")).strip().lower()
    route_error_raw = item.get("route_error")
    route_error = "" if route_error_raw is None else str(route_error_raw).strip()
    if route_error.lower() in {"none", "null", "nil"}:
        route_error = ""
    if route_status == "error" or route_error:
        errors.append(f"{identity_id}:{route_error or route_status}")

ok = 1 if not errors else 0
print(f"{ok}|{len(items)}|{';'.join(errors)}")
PY
  )"
  IFS="|" read -r routes_ok route_total route_errors <<<"${parsed}"
fi

if [[ "${status_guard}" != "active" ]]; then
  restart_reason="guard_service_not_active:${status_guard}"
fi
if [[ "${health_ok}" != "1" ]]; then
  restart_reason="${restart_reason:+${restart_reason};}healthz_not_ok"
fi
if [[ "${routes_ok}" != "1" ]]; then
  restart_reason="${restart_reason:+${restart_reason};}routes_not_ok:${route_errors:-none}"
fi
if [[ "${REQUIRE_STREAM}" == "1" && "${status_stream}" != "active" ]]; then
  restart_reason="${restart_reason:+${restart_reason};}stream_service_not_active:${status_stream}"
fi

if [[ -n "${restart_reason}" ]]; then
  restart_triggered=1
  systemctl restart feiqiao-guard || restart_ok=0
  if [[ "${REQUIRE_STREAM}" == "1" ]]; then
    systemctl restart feiqiao-guard-dingtalk-stream || restart_ok=0
  fi
  sleep "${RESTART_COOLDOWN_SECONDS}"
  status_guard="$(systemctl is-active feiqiao-guard 2>/dev/null || true)"
  status_stream="$(systemctl is-active feiqiao-guard-dingtalk-stream 2>/dev/null || true)"
fi

event_json="$(
  python3 - \
    "$(now_ts)" \
    "${status_guard}" \
    "${status_stream}" \
    "${health_ok}" \
    "${routes_ok}" \
    "${route_total}" \
    "${route_errors}" \
    "${restart_triggered}" \
    "${restart_ok}" \
    "${restart_reason}" \
    <<'PY'
import json
import sys

(
    ts,
    status_guard,
    status_stream,
    health_ok,
    routes_ok,
    route_total,
    route_errors,
    restart_triggered,
    restart_ok,
    restart_reason,
) = sys.argv[1:11]

payload = {
    "ts": ts,
    "status_guard": status_guard,
    "status_stream": status_stream,
    "health_ok": bool(int(health_ok)),
    "routes_ok": bool(int(routes_ok)),
    "route_total": int(route_total),
    "route_errors": route_errors,
    "restart_triggered": bool(int(restart_triggered)),
    "restart_ok": bool(int(restart_ok)),
    "restart_reason": restart_reason,
}
print(json.dumps(payload, ensure_ascii=False))
PY
)"

echo "${event_json}" >> "${LOG_FILE}"
echo "${event_json}"

if [[ "${restart_triggered}" == "1" && "${restart_ok}" != "1" ]]; then
  exit 1
fi

if [[ "${status_guard}" != "active" ]]; then
  exit 1
fi
if [[ "${REQUIRE_STREAM}" == "1" && "${status_stream}" != "active" ]]; then
  exit 1
fi
exit 0
