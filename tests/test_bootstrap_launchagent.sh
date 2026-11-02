#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOOTSTRAP="$SCRIPT_DIR/scripts/bootstrap.sh"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
LABEL='com.local.cos-iterm-edge'

fail() { echo "FAIL: $*" >&2; exit 1; }

run_case() {
  local name="$1" loaded="$2" inspect_error="$3" bootout_error="$4" expect_success="$5"
  local root="$TMP_ROOT/$name" fakebin="$TMP_ROOT/$name/fakebin"
  local home="$TMP_ROOT/$name/home" support="$TMP_ROOT/$name/home/Library/Application Support/iTerm2"
  local state="$TMP_ROOT/$name/state" log="$TMP_ROOT/$name/launchctl.log"
  local marker="$TMP_ROOT/$name/home/.claude/marker" lock="$TMP_ROOT/$name/bootstrap.lock"
  mkdir -p "$fakebin" "$home" "$support/Scripts/AutoLaunch" "$support/iterm2env/versions/fake/bin" \
    "$home/Library/LaunchAgents"
  mkdir -p "$(dirname "$marker")"
  current_version="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$SCRIPT_DIR/.claude-plugin/plugin.json" | head -1)"
  printf '%s\n' "$current_version" > "$marker"
  printf '%s\n' legacy > "$support/Scripts/AutoLaunch/cos_iterm_edge_daemon.py"
  printf '%s\n' old-agent > "$home/Library/LaunchAgents/$LABEL.plist"
  : > "$log"
  if [[ "$loaded" == yes ]]; then : > "$state"; fi
  cat > "$fakebin/launchctl" <<'LAUNCHCTL'
#!/bin/sh
set -eu
echo "$*" >> "$FAKE_LAUNCHCTL_LOG"
if [ "${FAKE_INSPECT_ERROR:-0}" = 1 ]; then echo "permission denied" >&2; exit 1; fi
case "$1" in
  print)
    if [ -f "$FAKE_LAUNCHCTL_STATE" ]; then exit 0; fi
    echo "Could not find service com.local.cos-iterm-edge" >&2; exit 113 ;;
  bootout)
    if [ "${FAKE_BOOTOUT_ERROR:-0}" = 1 ]; then echo "operation not permitted" >&2; exit 1; fi
    rm -f "$FAKE_LAUNCHCTL_STATE" ;;
  *) echo "unexpected launchctl command" >&2; exit 2 ;;
esac
LAUNCHCTL
  cat > "$fakebin/uname" <<'UNAME'
#!/bin/sh
echo Darwin
UNAME
  cat > "$support/iterm2env/versions/fake/bin/python3" <<'RUNTIME_PYTHON'
#!/bin/sh
# Satisfy bootstrap's existing-runtime probe without installing anything.
exit 0
RUNTIME_PYTHON
  chmod +x "$fakebin/launchctl" "$fakebin/uname" "$support/iterm2env/versions/fake/bin/python3"

  local rc=0
  env HOME="$home" PATH="$fakebin:$PATH" ITERM2_SUPPORT="$support" \
    BOOTSTRAP_MARKER="$marker" BOOTSTRAP_LOCK="$lock" CLAUDE_PLUGIN_ROOT="$SCRIPT_DIR" \
    FAKE_LAUNCHCTL_LOG="$log" FAKE_LAUNCHCTL_STATE="$state" \
    FAKE_INSPECT_ERROR="$inspect_error" FAKE_BOOTOUT_ERROR="$bootout_error" \
    bash "$BOOTSTRAP" >"$root/stdout" 2>"$root/stderr" || rc=$?

  if [[ "$expect_success" == yes && "$rc" -ne 0 ]]; then
    cat "$root/stderr" >&2
    fail "$name expected bootstrap success, got $rc"
  elif [[ "$expect_success" == no && "$rc" -eq 0 ]]; then
    fail "$name expected bootstrap refusal"
  fi
  case "$name" in
    loaded)
      [[ "$(sed -n '1p' "$log")" == "print gui/$(id -u)/$LABEL" ]] || fail "$name used an unexpected launchd target"
      [[ "$(sed -n '2p' "$log")" == "bootout gui/$(id -u)/$LABEL" ]] || fail "$name did not bootout expected target"
      [[ ! -e "$state" && ! -e "$home/Library/LaunchAgents/$LABEL.plist" ]] || fail "$name left loaded job/plist"
      [[ ! -e "$support/Scripts/AutoLaunch/cos_iterm_edge_daemon.py" ]] || fail "$name left AutoLaunch daemon"
      ;;
    unloaded)
      [[ "$(cat "$log")" == "print gui/$(id -u)/$LABEL" ]] || fail "$name should only inspect expected launchd target"
      [[ ! -e "$home/Library/LaunchAgents/$LABEL.plist" ]] || fail "$name left stale plist"
      ;;
    inspect-failure)
      [[ "$(cat "$log")" == "print gui/$(id -u)/$LABEL" ]] || fail "$name should stop after inspect error"
      [[ -e "$home/Library/LaunchAgents/$LABEL.plist" ]] || fail "$name removed plist despite inspect error"
      ;;
    bootout-failure)
      [[ "$(sed -n '1p' "$log")" == "print gui/$(id -u)/$LABEL" && "$(sed -n '2p' "$log")" == "bootout gui/$(id -u)/$LABEL" ]] || fail "$name should inspect then attempt expected bootout"
      [[ -e "$state" && -e "$home/Library/LaunchAgents/$LABEL.plist" ]] || fail "$name did not preserve failed unload state"
      ;;
  esac
  echo "PASS: $name (bootstrap exit $rc)"
}

run_case loaded yes 0 0 yes
run_case unloaded no 0 0 yes
run_case inspect-failure yes 1 0 no
run_case bootout-failure yes 0 1 no
echo '4 bootstrap LaunchAgent cases passed'
