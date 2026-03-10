#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY_URL="${FQG_GATEWAY_URL:-http://8.140.215.219:3001}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CODEX_BIN="${CODEX_BIN:-$(command -v codex)}"
ISOLATION_ROOT="${FQG_ISOLATION_ROOT:-${ROOT_DIR}/.runtime/codex_isolated}"
PRIMARY_CODEX_HOME="${PRIMARY_CODEX_HOME:-${CODEX_HOME:-${HOME}/.codex}}"
DISABLE_AGENTS_ADD_DIR="${FQG_DISABLE_AGENTS_ADD_DIR:-0}"
STRIP_MCP_SERVERS="${FQG_STRIP_MCP_SERVERS:-n8n-mcp,firebase}"
PASSTHROUGH_ENV_NAMES="${FQG_PASSTHROUGH_ENV_NAMES:-ZAI_API_KEY,GLM_API_KEY,GLM_BASE_URL,GLM_MODEL_ID}"

if [[ -z "${CODEX_BIN}" || ! -x "${CODEX_BIN}" ]]; then
  echo "codex binary not found or not executable" >&2
  exit 2
fi

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
mkdir -p \
  "${ISOLATION_ROOT}/home" \
  "${ISOLATION_ROOT}/tmp" \
  "${ISOLATION_ROOT}/xdg_config" \
  "${ISOLATION_ROOT}/xdg_cache" \
  "${ISOLATION_ROOT}/xdg_data" \
  "${ISOLATION_ROOT}/codex_home"
umask 077

ISO_HOME="${ISOLATION_ROOT}/home"
ISO_TMP="${ISOLATION_ROOT}/tmp"
ISO_XDG_CONFIG="${ISOLATION_ROOT}/xdg_config"
ISO_XDG_CACHE="${ISOLATION_ROOT}/xdg_cache"
ISO_XDG_DATA="${ISOLATION_ROOT}/xdg_data"
ISO_CODEX_HOME="${ISOLATION_ROOT}/codex_home"

CODEX_EXTRA_ARGS=()
if [[ "${DISABLE_AGENTS_ADD_DIR}" != "1" && -d "${ROOT_DIR}/.identity" ]]; then
  has_add_dir=false
  for arg in "$@"; do
    if [[ "${arg}" == "--add-dir" ]]; then
      has_add_dir=true
      break
    fi
  done
  if [[ "${has_add_dir}" == false ]]; then
    CODEX_EXTRA_ARGS+=(--add-dir "${ROOT_DIR}/.identity")
  fi
fi

if [[ -d "${PRIMARY_CODEX_HOME}" ]]; then
  for f in auth.json config.toml version.json; do
    if [[ -f "${PRIMARY_CODEX_HOME}/${f}" ]]; then
      cp -f "${PRIMARY_CODEX_HOME}/${f}" "${ISO_CODEX_HOME}/${f}"
      chmod 600 "${ISO_CODEX_HOME}/${f}" || true
    fi
  done
fi

if [[ -f "${ISO_CODEX_HOME}/config.toml" && -n "${STRIP_MCP_SERVERS// /}" ]]; then
  "${PYTHON_BIN}" - "${ISO_CODEX_HOME}/config.toml" "${STRIP_MCP_SERVERS}" <<'PY'
import sys
from pathlib import Path

cfg_path = Path(sys.argv[1])
raw_names = [x.strip() for x in sys.argv[2].split(",") if x.strip()]
if not raw_names:
    raise SystemExit(0)

targets = {name for name in raw_names}
lines = cfg_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
out: list[str] = []
drop = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        section = stripped[1:-1].strip()
        if section.startswith("mcp_servers."):
            name = section[len("mcp_servers.") :].split(".", 1)[0]
            drop = name in targets
        else:
            drop = False
    if not drop:
        out.append(line)

cfg_path.write_text("".join(out), encoding="utf-8")
PY
fi

BASE_PATH="${PATH}"
BASE_TERM="${TERM:-xterm-256color}"
BASE_LANG="${LANG:-C.UTF-8}"
BASE_LC_ALL="${LC_ALL:-}"
BASE_LC_CTYPE="${LC_CTYPE:-}"
BASE_COLORTERM="${COLORTERM:-}"
BASE_TERM_PROGRAM="${TERM_PROGRAM:-}"
BASE_TERM_PROGRAM_VERSION="${TERM_PROGRAM_VERSION:-}"
BASE_SHELL="${SHELL:-/bin/zsh}"
BASE_USER="${USER:-$(id -un 2>/dev/null || echo user)}"
BASE_LOGNAME="${LOGNAME:-${BASE_USER}}"

env_args=(
  "PATH=${BASE_PATH}"
  "TERM=${BASE_TERM}"
  "LANG=${BASE_LANG}"
  "HOME=${ISO_HOME}"
  "TMPDIR=${ISO_TMP}"
  "XDG_CONFIG_HOME=${ISO_XDG_CONFIG}"
  "XDG_CACHE_HOME=${ISO_XDG_CACHE}"
  "XDG_DATA_HOME=${ISO_XDG_DATA}"
  "CODEX_HOME=${ISO_CODEX_HOME}"
  "PYTHONPATH=${PYTHONPATH}"
  "SHELL=${BASE_SHELL}"
  "USER=${BASE_USER}"
  "LOGNAME=${BASE_LOGNAME}"
)
[[ -n "${BASE_LC_ALL}" ]] && env_args+=("LC_ALL=${BASE_LC_ALL}")
[[ -n "${BASE_LC_CTYPE}" ]] && env_args+=("LC_CTYPE=${BASE_LC_CTYPE}")
[[ -n "${BASE_COLORTERM}" ]] && env_args+=("COLORTERM=${BASE_COLORTERM}")
[[ -n "${BASE_TERM_PROGRAM}" ]] && env_args+=("TERM_PROGRAM=${BASE_TERM_PROGRAM}")
[[ -n "${BASE_TERM_PROGRAM_VERSION}" ]] && env_args+=("TERM_PROGRAM_VERSION=${BASE_TERM_PROGRAM_VERSION}")

for name in \
  FQG_AUTO_CONTINUE_NUDGE \
  FQG_CONTINUE_NUDGE_TEXT \
  FQG_CONTINUE_NUDGE_COOLDOWN_SECONDS \
  FQG_CONTINUE_NUDGE_MAX_COUNT \
  FQG_UNPARSED_COMMAND_FALLBACK_ACTION \
  FQG_STRIP_MCP_SERVERS \
  FQG_PASSTHROUGH_ENV_NAMES
do
  value="${!name-}"
  [[ -n "${value}" ]] && env_args+=("${name}=${value}")
done

IFS=',' read -r -a passthrough_names <<< "${PASSTHROUGH_ENV_NAMES}"
for raw_name in "${passthrough_names[@]}"; do
  name="$(echo "${raw_name}" | tr -d '[:space:]')"
  [[ -z "${name}" ]] && continue
  value="${!name-}"
  [[ -n "${value}" ]] && env_args+=("${name}=${value}")
done

exec env -i "${env_args[@]}" \
  "${PYTHON_BIN}" -m tosee_birder.wrapper \
  --gateway-base-url "${GATEWAY_URL}" \
  -- \
  "${CODEX_BIN}" "${CODEX_EXTRA_ARGS[@]}" "$@"
