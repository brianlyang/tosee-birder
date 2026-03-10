#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
RUN_ID="longlink_cycle_$(date +%Y%m%d_%H%M%S)"
DAY_DIR="$(date +%Y-%m-%d)"
OUT_DIR="${ROOT_DIR}/artifacts/ops/${DAY_DIR}/${RUN_ID}"

mkdir -p "${OUT_DIR}"

{
  echo "run_id=${RUN_ID}"
  echo "root_dir=${ROOT_DIR}"
  echo "started_at=$(date -Iseconds)"
} > "${OUT_DIR}/meta.txt"

echo "[1/5] restart local dingtalk tmux stack"
"${ROOT_DIR}/scripts/local_dingtalk_tmux_stack.sh" restart > "${OUT_DIR}/restart.log" 2>&1

echo "[2/5] three-plane health"
curl -fsS "http://127.0.0.1:3001/healthz" > "${OUT_DIR}/healthz.json"
curl -sS "http://127.0.0.1:3001/v1/chat/routes" > "${OUT_DIR}/routes.json"
curl -sS "http://127.0.0.1:3001/v1/chat/leader/snapshot" > "${OUT_DIR}/leader_snapshot.json"

echo "[3/5] stream readiness check"
cat "${ROOT_DIR}/.runtime/local_bridge/bridge_heartbeat.json" > "${OUT_DIR}/bridge_heartbeat.json"
rg -n "bridge_started|endpoint is \\{" "${ROOT_DIR}/.runtime/local_bridge/bridge.log" > "${OUT_DIR}/bridge_ready_lines.log" || true
tail -n 200 "${ROOT_DIR}/.runtime/local_bridge/bridge.log" > "${OUT_DIR}/bridge_tail.log" || true
tail -n 120 "${ROOT_DIR}/.runtime/local_bridge/api.log" > "${OUT_DIR}/api_tail.log" || true

echo "[4/5] run acceptance suite"
set +e
python3 "${ROOT_DIR}/scripts/run_guixianren_final_acceptance_suite.py" > "${OUT_DIR}/acceptance.stdout" 2> "${OUT_DIR}/acceptance.stderr"
SUITE_RC=$?
set -e
echo "suite_rc=${SUITE_RC}" >> "${OUT_DIR}/meta.txt"

SUMMARY_TSV="$(sed -n '1p' "${OUT_DIR}/acceptance.stdout" | tr -d '\r')"
REPORT_JSON="$(sed -n '2p' "${OUT_DIR}/acceptance.stdout" | tr -d '\r')"
SUMMARY_MD="$(sed -n '3p' "${OUT_DIR}/acceptance.stdout" | tr -d '\r')"
OVERALL="$(sed -n '4p' "${OUT_DIR}/acceptance.stdout" | tr -d '\r' | tr '[:lower:]' '[:upper:]')"

{
  echo "summary_tsv=${SUMMARY_TSV}"
  echo "report_json=${REPORT_JSON}"
  echo "summary_md=${SUMMARY_MD}"
  echo "overall=${OVERALL}"
} >> "${OUT_DIR}/meta.txt"

echo "[5/5] finalize summary"
{
  echo "# Longlink Stability Cycle"
  echo
  echo "- run_id: ${RUN_ID}"
  echo "- overall: ${OVERALL:-UNKNOWN}"
  echo "- suite_rc: ${SUITE_RC}"
  echo "- summary_tsv: ${SUMMARY_TSV}"
  echo "- report_json: ${REPORT_JSON}"
  echo "- summary_md: ${SUMMARY_MD}"
  echo "- output_dir: ${OUT_DIR}"
} > "${OUT_DIR}/summary.md"

if [[ "${SUITE_RC}" -ne 0 || "${OVERALL}" != "PASS" ]]; then
  echo "FAIL ${OUT_DIR}"
  exit 2
fi

echo "PASS ${OUT_DIR}"
