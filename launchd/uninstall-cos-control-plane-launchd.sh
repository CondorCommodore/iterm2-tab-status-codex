#!/usr/bin/env bash
# Uninstall com.local.cos-control-plane LaunchAgent.
set -euo pipefail

LABEL="com.local.cos-control-plane"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
rm -f "${PLIST}"
echo "[uninstall] removed ${LABEL}"
