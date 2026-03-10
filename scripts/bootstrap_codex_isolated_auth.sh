#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ISOLATION_ROOT="${FQG_ISOLATION_ROOT:-${ROOT_DIR}/.runtime/codex_isolated}"
CODEX_BIN="${CODEX_BIN:-$(command -v codex)}"

if [[ -z "${CODEX_BIN}" || ! -x "${CODEX_BIN}" ]]; then
  echo "codex binary not found or not executable" >&2
  exit 2
fi

mkdir -p \
  "${ISOLATION_ROOT}/home" \
  "${ISOLATION_ROOT}/tmp" \
  "${ISOLATION_ROOT}/xdg_config" \
  "${ISOLATION_ROOT}/xdg_cache" \
  "${ISOLATION_ROOT}/xdg_data" \
  "${ISOLATION_ROOT}/codex_home"
umask 077

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
  "HOME=${ISOLATION_ROOT}/home"
  "TMPDIR=${ISOLATION_ROOT}/tmp"
  "XDG_CONFIG_HOME=${ISOLATION_ROOT}/xdg_config"
  "XDG_CACHE_HOME=${ISOLATION_ROOT}/xdg_cache"
  "XDG_DATA_HOME=${ISOLATION_ROOT}/xdg_data"
  "CODEX_HOME=${ISOLATION_ROOT}/codex_home"
  "SHELL=${BASE_SHELL}"
  "USER=${BASE_USER}"
  "LOGNAME=${BASE_LOGNAME}"
)
[[ -n "${BASE_LC_ALL}" ]] && env_args+=("LC_ALL=${BASE_LC_ALL}")
[[ -n "${BASE_LC_CTYPE}" ]] && env_args+=("LC_CTYPE=${BASE_LC_CTYPE}")
[[ -n "${BASE_COLORTERM}" ]] && env_args+=("COLORTERM=${BASE_COLORTERM}")
[[ -n "${BASE_TERM_PROGRAM}" ]] && env_args+=("TERM_PROGRAM=${BASE_TERM_PROGRAM}")
[[ -n "${BASE_TERM_PROGRAM_VERSION}" ]] && env_args+=("TERM_PROGRAM_VERSION=${BASE_TERM_PROGRAM_VERSION}")

exec env -i "${env_args[@]}" \
  "${CODEX_BIN}" "$@"
