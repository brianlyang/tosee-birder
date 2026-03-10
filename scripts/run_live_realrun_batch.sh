#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

wait_healthz() {
  local max_wait="${1:-30}"
  local i=0
  while [[ "${i}" -lt "${max_wait}" ]]; do
    if curl -fsS http://127.0.0.1:3001/healthz >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

RUNS="${1:-5}"
if ! [[ "${RUNS}" =~ ^[0-9]+$ ]] || [[ "${RUNS}" -le 0 ]]; then
  echo "invalid_runs:${RUNS}" >&2
  exit 2
fi

DAY="$(date +%Y-%m-%d)"
TS="$(date +%Y%m%d_%H%M%S)"
BATCH_DIR="artifacts/ops/${DAY}/live_realrun_batch_${TS}"
mkdir -p "${BATCH_DIR}"

printf "run\toverall\tout_dir\ttoken\trecalled_token\n" >"${BATCH_DIR}/summary.tsv"
PASS_COUNT=0

for i in $(seq 1 "${RUNS}"); do
  if ! wait_healthz 45; then
    jq -n --arg err "healthz_timeout_before_run_${i}" '{overall:"FAIL", error:$err}' >"${BATCH_DIR}/run_${i}.json"
  else
    set +e
    ./scripts/run_live_realrun.sh >"${BATCH_DIR}/run_${i}.json"
    run_rc=$?
    set -e
    if [[ "${run_rc}" -ne 0 ]]; then
      jq -n --arg err "run_script_exit_${run_rc}" '{overall:"FAIL", error:$err}' >"${BATCH_DIR}/run_${i}.json"
    fi
  fi

  OVERALL="$(jq -r '.overall // "FAIL"' "${BATCH_DIR}/run_${i}.json")"
  OUT_DIR="$(jq -r '.out_dir // ""' "${BATCH_DIR}/run_${i}.json")"
  TOKEN="$(jq -r '.token // ""' "${BATCH_DIR}/run_${i}.json")"
  RECALL="$(jq -r '.recalled_token // ""' "${BATCH_DIR}/run_${i}.json")"
  printf "%s\t%s\t%s\t%s\t%s\n" "${i}" "${OVERALL}" "${OUT_DIR}" "${TOKEN}" "${RECALL}" >>"${BATCH_DIR}/summary.tsv"
  if [[ "${OVERALL}" == "PASS" ]]; then
    PASS_COUNT=$((PASS_COUNT + 1))
  fi
  sleep 1
done

jq -n \
  --arg batch_dir "${BATCH_DIR}" \
  --arg total "${RUNS}" \
  --arg pass "${PASS_COUNT}" \
  '{
    batch_dir:$batch_dir,
    total_runs:($total|tonumber),
    pass_runs:($pass|tonumber),
    pass_rate:(($pass|tonumber)/($total|tonumber))
  }' >"${BATCH_DIR}/batch_result.json"

cat "${BATCH_DIR}/batch_result.json"
