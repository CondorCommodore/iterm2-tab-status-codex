#!/usr/bin/env bash
# Install com.local.cos-control-plane as a launchd LaunchAgent.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_NAME="com.local.cos-control-plane.plist"
LABEL="com.local.cos-control-plane"
SRC="${SCRIPT_DIR}/${PLIST_NAME}"
DST="${LAUNCH_AGENTS_DIR}/${PLIST_NAME}"
LOG_PATH="${HOME}/.cache/cos-control-plane.log"

PYTHON3="$(command -v python3 || true)"
if [[ -x /opt/homebrew/bin/python3 ]]; then
  PYTHON3="/opt/homebrew/bin/python3"
fi
if [[ -z "${PYTHON3}" || ! -x "${PYTHON3}" ]]; then
  echo "[install] FATAL: no python3 found on PATH" >&2
  exit 1
fi

mkdir -p "${LAUNCH_AGENTS_DIR}" "${HOME}/.cache"
sed \
  -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
  -e "s|__HOME__|${HOME}|g" \
  -e "s|__PYTHON3__|${PYTHON3}|g" \
  "${SRC}" > "${DST}"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${DST}"
launchctl enable "gui/$(id -u)/${LABEL}"

launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | sed -n '1,18p'
echo "[install] log path: ${LOG_PATH}"
echo "[install] dashboard: ${HOME}/.claude/plans/fleet-reports/cos-dashboard-current.json"
