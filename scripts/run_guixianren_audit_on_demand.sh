#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${FQG_VALIDATE_BASE_URL:-http://127.0.0.1:3001}"
TS="$(date +%Y%m%d_%H%M%S)"
RUN_ID="guixianren_audit_${TS}"
OUT_DIR="${ROOT_DIR}/resource/reports/${RUN_ID}"
HEARTBEAT_MAX_AGE_SECONDS="${FQG_AUDIT_HEARTBEAT_MAX_AGE_SECONDS:-180}"
ACTIVITY_MAX_AGE_SECONDS="${FQG_AUDIT_ACTIVITY_MAX_AGE_SECONDS:-1800}"
REQUIRED_IDS="${FQG_AUDIT_REQUIRED_IDENTITIES:-feiqiao-guard-delivery-lead,feiqiao-guard-collab-executor}"

mkdir -p "${OUT_DIR}"

collect() {
  local name="$1"
  local url="$2"
  if ! curl -sS "${url}" > "${OUT_DIR}/${name}.json"; then
    echo "{\"error\":\"curl_failed\",\"url\":\"${url}\"}" > "${OUT_DIR}/${name}.json"
  fi
}

collect "healthz" "${BASE_URL}/healthz"
collect "routes" "${BASE_URL}/v1/chat/routes"
collect "leader_snapshot" "${BASE_URL}/v1/chat/leader/snapshot"

if [[ -f "${ROOT_DIR}/.runtime/local_bridge/bridge_heartbeat.json" ]]; then
  cp "${ROOT_DIR}/.runtime/local_bridge/bridge_heartbeat.json" "${OUT_DIR}/bridge_heartbeat.json"
fi
if [[ -f "${ROOT_DIR}/.runtime/local_bridge/bridge.log" ]]; then
  tail -n 600 "${ROOT_DIR}/.runtime/local_bridge/bridge.log" > "${OUT_DIR}/bridge_tail.log" || true
fi
if [[ -f "${ROOT_DIR}/resource/reports/approval_audit.jsonl" ]]; then
  tail -n 600 "${ROOT_DIR}/resource/reports/approval_audit.jsonl" > "${OUT_DIR}/approval_audit_tail.jsonl" || true
fi

python3 - <<'PY' "${OUT_DIR}" "${BASE_URL}" "${HEARTBEAT_MAX_AGE_SECONDS}" "${ACTIVITY_MAX_AGE_SECONDS}" "${REQUIRED_IDS}"
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
import sys

out_dir = Path(sys.argv[1]).resolve()
base_url = str(sys.argv[2]).strip()
heartbeat_max_age = float(sys.argv[3])
activity_max_age = float(sys.argv[4])
required_ids = [x.strip() for x in str(sys.argv[5]).split(",") if x.strip()]

def load_json(name: str) -> dict:
    path = out_dir / name
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}

healthz = load_json("healthz.json")
routes = load_json("routes.json")
snapshot = load_json("leader_snapshot.json")
heartbeat = load_json("bridge_heartbeat.json")

checks: list[dict] = []

def add(name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

status_ok = str(healthz.get("status", "")).strip().lower() == "ok"
add("H1_healthz_ok", status_ok, f"status={healthz.get('status')!r}")

items = routes.get("items")
route_map: dict[str, dict] = {}
if isinstance(items, list):
    for raw in items:
        if isinstance(raw, dict):
            iid = str(raw.get("identity_id", "")).strip()
            if iid:
                route_map[iid] = raw

missing = [iid for iid in required_ids if iid not in route_map]
route_state_ok = True
route_details: list[str] = []
for iid in required_ids:
    row = route_map.get(iid) or {}
    status = str(row.get("route_status", "")).strip().lower()
    sid = str(row.get("session_id", "")).strip()
    home = str(row.get("codex_home", "")).strip()
    ok = (status == "ok" and bool(sid) and bool(home))
    route_state_ok = route_state_ok and ok
    route_details.append(f"{iid}:status={status or '-'} sid={'Y' if sid else 'N'} home={'Y' if home else 'N'}")
if missing:
    route_state_ok = False
    route_details.append(f"missing={','.join(missing)}")
add("H2_routes_dual_identity_ok", route_state_ok, "; ".join(route_details))

hb_ok = False
hb_detail = "heartbeat_missing"
if heartbeat:
    ts = heartbeat.get("generated_at_epoch")
    ages = heartbeat.get("ages") if isinstance(heartbeat.get("ages"), dict) else {}
    try:
        age = max(0.0, time.time() - float(ts))
        callback_age = float(ages.get("callback_age_seconds", 1e9) or 1e9)
        inbound_age = float(ages.get("inbound_age_seconds", 1e9) or 1e9)
        reply_age = float(ages.get("reply_age_seconds", 1e9) or 1e9)
        hb_ok = age <= heartbeat_max_age and min(callback_age, inbound_age, reply_age) <= activity_max_age
        hb_detail = (
            f"heartbeat_age={age:.1f}s<= {heartbeat_max_age};"
            f"callback_age={callback_age:.1f}s;"
            f"inbound_age={inbound_age:.1f}s;"
            f"reply_age={reply_age:.1f}s;"
            f"activity_max={activity_max_age}"
        )
    except Exception as exc:  # noqa: BLE001
        hb_ok = False
        hb_detail = f"heartbeat_parse_error:{type(exc).__name__}"
add("H3_heartbeat_fresh", hb_ok, hb_detail)

snap_items = snapshot.get("items")
snap_ok = True
snap_detail_parts: list[str] = []
if not isinstance(snap_items, list):
    snap_ok = False
    snap_detail_parts.append("snapshot_items_missing")
else:
    snap_map = {}
    for raw in snap_items:
        if isinstance(raw, dict):
            iid = str(raw.get("identity_id", "")).strip()
            if iid:
                snap_map[iid] = raw
    for iid in required_ids:
        row = snap_map.get(iid)
        if not isinstance(row, dict):
            snap_ok = False
            snap_detail_parts.append(f"{iid}:missing")
            continue
        probe_ok = bool(row.get("process_probe_ok", False))
        state = str(row.get("state", "")).strip() or "-"
        event = str(row.get("last_event_type", "")).strip() or "-"
        snap_ok = snap_ok and probe_ok
        snap_detail_parts.append(f"{iid}:probe={'Y' if probe_ok else 'N'} state={state} event={event}")
add("H4_snapshot_probe_ok", snap_ok, "; ".join(snap_detail_parts))

overall_ok = all(bool(x.get("ok")) for x in checks)
report = {
    "run_id": out_dir.name,
    "generated_at": datetime.now().isoformat(),
    "base_url": base_url,
    "required_ids": required_ids,
    "overall": "PASS" if overall_ok else "FAIL",
    "checks": checks,
    "evidence_files": sorted([p.name for p in out_dir.iterdir() if p.is_file()]),
}

(out_dir / "audit_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
lines = [
    f"# 龟仙人通知即审计 {out_dir.name}",
    "",
    f"- overall: **{report['overall']}**",
    f"- base_url: `{base_url}`",
    f"- required_ids: `{', '.join(required_ids)}`",
    "",
    "## Checks",
]
for item in checks:
    mark = "PASS" if item["ok"] else "FAIL"
    lines.append(f"- {item['name']}: {mark} | {item['detail']}")
lines.extend(
    [
        "",
        "## Evidence",
        f"- report_json: {out_dir / 'audit_report.json'}",
        f"- healthz: {out_dir / 'healthz.json'}",
        f"- routes: {out_dir / 'routes.json'}",
        f"- snapshot: {out_dir / 'leader_snapshot.json'}",
    ]
)
(out_dir / "audit_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(str(out_dir / "audit_summary.md"))
print(str(out_dir / "audit_report.json"))
print(report["overall"])
PY
