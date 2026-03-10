#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${FQG_LOCAL_STACK_LAUNCHD_LABEL:-com.fqg.local-stack.supervisor}"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
SUPERVISOR_SCRIPT="${ROOT_DIR}/scripts/local_stack_supervisor.sh"
RUNTIME_DIR="${ROOT_DIR}/.runtime/local_bridge"
STDOUT_LOG="${RUNTIME_DIR}/launchd.stdout.log"
STDERR_LOG="${RUNTIME_DIR}/launchd.stderr.log"
BASE_LAUNCHD_PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

resolve_launchd_path() {
  local codex_bin codex_dir
  codex_bin="$(command -v codex 2>/dev/null || true)"
  if [[ -n "${codex_bin}" ]]; then
    codex_dir="$(dirname "${codex_bin}")"
    if [[ ":${BASE_LAUNCHD_PATH}:" != *":${codex_dir}:"* ]]; then
      echo "${codex_dir}:${BASE_LAUNCHD_PATH}"
      return
    fi
  fi
  echo "${BASE_LAUNCHD_PATH}"
}

mkdir -p "${RUNTIME_DIR}" "$(dirname "${PLIST_PATH}")"

ensure_supervisor_executable() {
  if [[ ! -x "${SUPERVISOR_SCRIPT}" ]]; then
    chmod +x "${SUPERVISOR_SCRIPT}"
  fi
}

generate_plist() {
  local launchd_path
  launchd_path="$(resolve_launchd_path)"
  cat >"${PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>-lc</string>
      <string>cd "${ROOT_DIR}" &amp;&amp; exec "${SUPERVISOR_SCRIPT}"</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${STDOUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${STDERR_LOG}</string>
    <key>WorkingDirectory</key>
    <string>${ROOT_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>${launchd_path}</string>
    </dict>
  </dict>
</plist>
PLIST
}

bootout_if_loaded() {
  launchctl bootout "gui/$(id -u)" "${PLIST_PATH}" >/dev/null 2>&1 || true
}

install_agent() {
  ensure_supervisor_executable
  generate_plist
  bootout_if_loaded
  launchctl bootstrap "gui/$(id -u)" "${PLIST_PATH}"
  launchctl enable "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/${LABEL}"
  echo "installed:${PLIST_PATH}"
}

start_agent() {
  launchctl enable "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/${LABEL}"
  echo "started:${LABEL}"
}

stop_agent() {
  launchctl bootout "gui/$(id -u)" "${PLIST_PATH}" >/dev/null 2>&1 || true
  echo "stopped:${LABEL}"
}

uninstall_agent() {
  stop_agent
  rm -f "${PLIST_PATH}"
  echo "uninstalled:${PLIST_PATH}"
}

status_agent() {
  echo "label:${LABEL}"
  echo "plist:${PLIST_PATH}"
  if [[ -f "${PLIST_PATH}" ]]; then
    echo "plist_exists:true"
  else
    echo "plist_exists:false"
  fi
  launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null || true
}

usage() {
  cat <<EOF
Usage: $(basename "$0") <install|start|stop|restart|uninstall|status>
EOF
}

main() {
  local action="${1:-status}"
  case "${action}" in
    install) install_agent ;;
    start) start_agent ;;
    stop) stop_agent ;;
    restart) stop_agent; install_agent ;;
    uninstall) uninstall_agent ;;
    status) status_agent ;;
    -h|--help|help) usage ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
