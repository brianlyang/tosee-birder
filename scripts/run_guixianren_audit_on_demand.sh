#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${FQG_VALIDATE_BASE_URL:-http://127.0.0.1:3001}"
TS="$(date +%Y%m%d_%H%M%S)"
RUN_ID="guixianren_audit_${TS}"
OUT_DIR="${ROOT_DIR}/resource/reports/${RUN_ID}"
HEARTBEAT_MAX_AGE_SECONDS="${FQG_AUDIT_HEARTBEAT_MAX_AGE_SECONDS:-180}"
ACTIVITY_MAX_AGE_SECONDS="${FQG_AUDIT_ACTIVITY_MAX_AGE_SECONDS:-7200}"
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
from datetime import timezone
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
audit_tail_path = out_dir / "approval_audit_tail.jsonl"

checks: list[dict] = []

def add(name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})


def parse_iso_to_epoch(raw: str) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    normalized = text
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def recent_local_activity(max_age_seconds: float) -> tuple[bool, str]:
    if not audit_tail_path.exists():
        return False, "audit_tail_missing"
    now = time.time()
    latest_ts = 0.0
    latest_case = ""
    latest_identity = ""
    hits = 0
    try:
        rows = audit_tail_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False, "audit_tail_unreadable"
    for raw in rows:
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("event", "")).strip() != "chat_inbound_received":
            continue
        iid = str(row.get("identity_id", "")).strip()
        if iid not in required_ids:
            continue
        ts = parse_iso_to_epoch(row.get("recorded_at"))
        if ts is None:
            continue
        hits += 1
        if ts > latest_ts:
            latest_ts = ts
            latest_identity = iid
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            latest_case = str(meta.get("case", "")).strip()
    if latest_ts <= 0:
        return False, "no_recent_chat_inbound_received"
    age = max(0.0, now - latest_ts)
    ok = age <= max_age_seconds
    return (
        ok,
        f"latest_local_inbound_age={age:.1f}s<= {max_age_seconds};hits={hits};"
        f"latest_identity={latest_identity or '-'};latest_case={latest_case or '-'}",
    )

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
        bridge_activity_ok = min(callback_age, inbound_age, reply_age) <= activity_max_age
        local_activity_ok, local_activity_detail = recent_local_activity(activity_max_age)
        hb_ok = age <= heartbeat_max_age and (bridge_activity_ok or local_activity_ok)
        hb_detail = (
            f"heartbeat_age={age:.1f}s<= {heartbeat_max_age};"
            f"callback_age={callback_age:.1f}s;"
            f"inbound_age={inbound_age:.1f}s;"
            f"reply_age={reply_age:.1f}s;"
            f"activity_max={activity_max_age};"
            f"bridge_activity_ok={bridge_activity_ok};"
            f"local_activity_ok={local_activity_ok};"
            f"{local_activity_detail}"
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
