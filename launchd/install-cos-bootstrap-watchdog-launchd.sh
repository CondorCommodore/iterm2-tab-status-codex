#!/usr/bin/env bash
# Install the inert-until-armed bootstrap COS watchdog.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LABEL="com.local.cos-bootstrap-watchdog"
SRC="${SCRIPT_DIR}/${LABEL}.plist"
DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
PYTHON3="$(command -v python3)"
MANIFEST="${COS_C2_MANIFEST:-${HOME}/.config/cos-c2/run-manifest.json}"
STATE_DIR="${COS_C2_STATE_DIR:-${HOME}/.local/state/cos-c2}"

while (($#)); do
  case "$1" in
    --manifest)
      MANIFEST="$2"
      shift 2
      ;;
    --state-dir)
      STATE_DIR="$2"
      shift 2
      ;;
    *)
      echo "usage: $0 [--manifest PATH] [--state-dir PATH]" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "${MANIFEST}" ]]; then
  echo "[install] manifest not found: ${MANIFEST}" >&2
  exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents" "${STATE_DIR}"
"${PYTHON3}" -c '
import html
import pathlib
import sys

source, destination, *values = sys.argv[1:]
keys = ("REPO_ROOT", "HOME", "PYTHON3", "MANIFEST", "STATE_DIR")
rendered = pathlib.Path(source).read_text(encoding="utf-8")
if len(values) != len(keys):
    raise SystemExit("watchdog plist renderer argument mismatch")
for key, value in zip(keys, values):
    rendered = rendered.replace(f"__{key}__", html.escape(value))
pathlib.Path(destination).write_text(rendered, encoding="utf-8")
' "${SRC}" "${DST}" "${REPO_ROOT}" "${HOME}" "${PYTHON3}" "${MANIFEST}" "${STATE_DIR}"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${DST}"
launchctl enable "gui/$(id -u)/${LABEL}"
launchctl print "gui/$(id -u)/${LABEL}" | sed -n '1,18p'
echo "[install] watchdog loaded for manifest=${MANIFEST} state_dir=${STATE_DIR}"
echo "[install] it remains inert until that state directory is armed"
