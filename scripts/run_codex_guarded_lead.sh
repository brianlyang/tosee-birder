#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_SCRIPT="${ROOT_DIR}/scripts/run_codex_guarded.sh"

export FQG_AUTO_CONTINUE_NUDGE="${FQG_AUTO_CONTINUE_NUDGE:-1}"
export FQG_CONTINUE_NUDGE_TEXT="${FQG_CONTINUE_NUDGE_TEXT:-继续执行直到任务完成后再汇报结果，不要等待我确认。}"
export FQG_CONTINUE_NUDGE_COOLDOWN_SECONDS="${FQG_CONTINUE_NUDGE_COOLDOWN_SECONDS:-8}"
export FQG_CONTINUE_NUDGE_MAX_COUNT="${FQG_CONTINUE_NUDGE_MAX_COUNT:-3}"
export FQG_UNPARSED_COMMAND_FALLBACK_ACTION="${FQG_UNPARSED_COMMAND_FALLBACK_ACTION:-ENTER}"

# If user is explicitly running a codex subcommand (resume/fork/exec...), do not inject prompt.
if [[ $# -gt 0 ]]; then
  case "$1" in
    resume|fork|exec|review|mcp|mcp-server|app|app-server|login|logout|completion|sandbox|debug|apply|cloud|features|help|-h|--help|-V|--version)
      exec "${BASE_SCRIPT}" "$@"
      ;;
  esac
fi

LEAD_PROMPT="${FQG_LEAD_BOOT_PROMPT:-在本会话中默认持续执行直到任务完成，不要停在“如果你同意我再继续”这类确认；只有在需要外部凭据、不可逆破坏操作或需求冲突时才暂停。}"

exec "${BASE_SCRIPT}" "${LEAD_PROMPT}" "$@"
