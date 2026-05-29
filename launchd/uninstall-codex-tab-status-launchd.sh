#!/usr/bin/env bash
# Uninstall com.local.codex-tab-status.
#
# Idempotent: bootout succeeds even if not loaded; rm -f tolerates a missing
# plist. Leaves signal files in ~/.cache/claude-tab-status/ alone — they
# self-clean via the adapter's PID-liveness check when their codex process
# exits.
set -euo pipefail

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_NAME="com.local.codex-tab-status.plist"
LABEL="com.local.codex-tab-status"
DST="${LAUNCH_AGENTS_DIR}/${PLIST_NAME}"

echo "[uninstall] bootout gui/$(id -u)/${LABEL}"
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true

echo "[uninstall] rm -f ${DST}"
rm -f "${DST}"

echo "[uninstall] done — codex tabs will lose their status indicators next time the iTerm2 adapter sweeps."
