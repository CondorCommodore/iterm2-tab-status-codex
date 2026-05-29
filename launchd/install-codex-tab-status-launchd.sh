#!/usr/bin/env bash
# Install com.local.codex-tab-status as a launchd LaunchAgent.
#
# Template-expands the plist into ~/Library/LaunchAgents, bootstraps into
# gui/$UID domain, enables, verifies. Uses modern launchctl bootstrap/
# enable/print verbs (NOT the deprecated load/unload).
#
# Pattern source: ~/code/home-lab/launchd/install-conductor-tick-launchd.sh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_NAME="com.local.codex-tab-status.plist"
LABEL="com.local.codex-tab-status"
SRC="${SCRIPT_DIR}/${PLIST_NAME}"
DST="${LAUNCH_AGENTS_DIR}/${PLIST_NAME}"
LOG_PATH="${HOME}/.cache/codex-tab-status.log"

# Resolve a real python3 — prefer homebrew, fall back to system. The plist
# embeds the resolved path so launchd doesn't need to search PATH at fire.
PYTHON3="$(command -v python3 || true)"
if [[ -x /opt/homebrew/bin/python3 ]]; then
  PYTHON3="/opt/homebrew/bin/python3"
fi
if [[ -z "${PYTHON3}" || ! -x "${PYTHON3}" ]]; then
  echo "[install] FATAL: no python3 found on PATH" >&2
  exit 1
fi

echo "[install] python3:     ${PYTHON3}"
echo "[install] repo root:   ${REPO_ROOT}"
echo "[install] plist src:   ${SRC}"
echo "[install] plist dst:   ${DST}"
echo "[install] log path:    ${LOG_PATH}"

mkdir -p "${LAUNCH_AGENTS_DIR}" "${HOME}/.cache"

# Template-expand placeholders
sed \
  -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
  -e "s|__HOME__|${HOME}|g" \
  -e "s|__PYTHON3__|${PYTHON3}|g" \
  "${SRC}" > "${DST}"

echo "[install] bootout (idempotent) then bootstrap into gui/$(id -u)"
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${DST}"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "[install] launchctl print:"
launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | sed -n '1,18p'

echo
echo "[install] waiting 3s for daemon to start..."
sleep 3
echo "[install] log tail (${LOG_PATH}):"
tail -10 "${LOG_PATH}" 2>/dev/null || echo "  (log not yet created — that's fine if no codex tabs are open)"

echo
echo "[install] signal files in ~/.cache/claude-tab-status/:"
ls -1 "${HOME}/.cache/claude-tab-status/codex-"*.json 2>/dev/null | head -5 \
  || echo "  (none yet — open a codex tab in iTerm2 and re-check)"

echo
echo "[install] done — uninstall with: bash ${SCRIPT_DIR}/uninstall-codex-tab-status-launchd.sh"
