#!/usr/bin/env bash
# Install the persistent, pinned iTerm Python API C2 edge.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LABEL="com.local.cos-iterm-edge"
SRC="${SCRIPT_DIR}/${LABEL}.plist"
DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
MANIFEST="${COS_C2_MANIFEST:-${HOME}/.config/cos-c2/run-manifest.json}"
STATE_DIR="${COS_C2_STATE_DIR:-${HOME}/.local/state/cos-c2}"
SOCKET="${COS_C2_EDGE_SOCKET:-${HOME}/.cache/cos-c2/iterm-edge.sock}"
ITERM_PYTHON="${COS_C2_ITERM_PYTHON:-}"
DISCOVERY_PYTHON="$(command -v python3)"

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
    --socket)
      SOCKET="$2"
      shift 2
      ;;
    --python)
      ITERM_PYTHON="$2"
      shift 2
      ;;
    *)
      echo "usage: $0 [--manifest PATH] [--state-dir PATH] [--socket PATH] [--python PATH]" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "${MANIFEST}" ]]; then
  echo "[install] manifest not found: ${MANIFEST}" >&2
  exit 1
fi
if [[ -z "${ITERM_PYTHON}" ]]; then
  ITERM_PYTHON="$("${DISCOVERY_PYTHON}" -c '
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
candidates = []
for path in root.glob("*/bin/python3"):
    try:
        version = tuple(int(part) for part in path.parents[1].name.split("."))
    except ValueError:
        continue
    if not path.is_file():
        continue
    probe = subprocess.run(
        [str(path), "-c", "import iterm2"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    if probe.returncode == 0:
        candidates.append((version, path))
if candidates:
    print(max(candidates)[1])
' "${HOME}/Library/Application Support/iTerm2/iterm2env/versions")"
fi
if [[ -z "${ITERM_PYTHON}" || ! -x "${ITERM_PYTHON}" ]]; then
  echo "[install] iTerm Python runtime not found; pass --python PATH" >&2
  exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents" "${STATE_DIR}" "$(dirname "${SOCKET}")"
chmod 700 "${STATE_DIR}" "$(dirname "${SOCKET}")"
HOOK_KEY="${STATE_DIR}/runtime-observation.key"
if [[ ! -e "${HOOK_KEY}" ]]; then
  umask 077
  dd if=/dev/urandom of="${HOOK_KEY}" bs=32 count=1 2>/dev/null
fi
if [[ -L "${HOOK_KEY}" || ! -f "${HOOK_KEY}" ]]; then
  echo "[install] runtime observation key must be a regular non-symlink file: ${HOOK_KEY}" >&2
  exit 1
fi
chmod 600 "${HOOK_KEY}"
if [[ "$(wc -c < "${HOOK_KEY}" | tr -d ' ')" -lt 32 ]]; then
  echo "[install] runtime observation key must contain at least 32 bytes: ${HOOK_KEY}" >&2
  exit 1
fi
"${ITERM_PYTHON}" -c '
import html
import pathlib
import sys

source, destination, *values = sys.argv[1:]
keys = ("REPO_ROOT", "HOME", "ITERM_PYTHON", "MANIFEST", "STATE_DIR", "SOCKET")
rendered = pathlib.Path(source).read_text(encoding="utf-8")
if len(values) != len(keys):
    raise SystemExit("iTerm edge plist renderer argument mismatch")
for key, value in zip(keys, values):
    rendered = rendered.replace(f"__{key}__", html.escape(value))
pathlib.Path(destination).write_text(rendered, encoding="utf-8")
' "${SRC}" "${DST}" "${REPO_ROOT}" "${HOME}" "${ITERM_PYTHON}" "${MANIFEST}" "${STATE_DIR}" "${SOCKET}"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${DST}"
launchctl enable "gui/$(id -u)/${LABEL}"
launchctl print "gui/$(id -u)/${LABEL}" | sed -n '1,22p'
echo "[install] persistent iTerm edge loaded for manifest=${MANIFEST} state_dir=${STATE_DIR} socket=${SOCKET}"
echo "[install] runtime hook enrollment remains a separate, explicit source-profile step"
