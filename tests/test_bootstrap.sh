#!/usr/bin/env bash
set -euo pipefail

PASS=0
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOOTSTRAP="$SCRIPT_DIR/scripts/bootstrap.sh"
TMPDIR_BASE="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_BASE"' EXIT

pass() { PASS=$((PASS + 1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ✗ $1: $2"; }

echo "=== Bootstrap script tests ==="

# Shared variables
CURRENT_VERSION="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$SCRIPT_DIR/.claude-plugin/plugin.json" | head -1)"
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"

# ---------------------------------------------------------------------------
# Test 1: Skips if marker file exists with matching version
# ---------------------------------------------------------------------------
echo "Test 1: Skips if marker file has matching version"
DIR1="$TMPDIR_BASE/t1"
mkdir -p "$DIR1/marker_dir" "$DIR1/iterm2_support/iterm2env/versions" "$DIR1/iterm2_support/Scripts/AutoLaunch"
MARKER1="$DIR1/marker_dir/bootstrapped"
# Write the current plugin version into the marker
CURRENT_VERSION="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$SCRIPT_DIR/.claude-plugin/plugin.json" | head -1)"
echo "$CURRENT_VERSION" > "$MARKER1"

ITERM2_SUPPORT="$DIR1/iterm2_support" \
BOOTSTRAP_MARKER="$MARKER1" \
BOOTSTRAP_LOCK="$DIR1/lock" \
CLAUDE_PLUGIN_ROOT="$SCRIPT_DIR" \
  bash "$BOOTSTRAP" 2>/dev/null

# Verify no venv was created (versions dir should be empty)
venv_count=$(find "$DIR1/iterm2_support/iterm2env/versions" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
if [[ "$venv_count" == "0" ]]; then
  pass "No venv created when marker version matches"
else
  fail "Marker skip" "venv was created despite marker version matching"
fi

# ---------------------------------------------------------------------------
# Test 2: Creates venv when no runtime exists
# ---------------------------------------------------------------------------
echo "Test 2: Creates venv when no runtime exists"
DIR2="$TMPDIR_BASE/t2"
mkdir -p "$DIR2/iterm2_support/iterm2env/versions" "$DIR2/iterm2_support/Scripts/AutoLaunch" "$DIR2/marker_dir"

# Create a fake adapter script so cp succeeds
FAKE_PLUGIN="$DIR2/plugin_root"
mkdir -p "$FAKE_PLUGIN/scripts"
echo '#!/usr/bin/env python3' > "$FAKE_PLUGIN/scripts/claude_tab_status.py"
mkdir -p "$FAKE_PLUGIN/.claude-plugin"
echo "{\"version\": \"$CURRENT_VERSION\"}" > "$FAKE_PLUGIN/.claude-plugin/plugin.json"

ITERM2_SUPPORT="$DIR2/iterm2_support" \
BOOTSTRAP_MARKER="$DIR2/marker_dir/bootstrapped" \
BOOTSTRAP_LOCK="$DIR2/lock" \
CLAUDE_PLUGIN_ROOT="$FAKE_PLUGIN" \
  bash "$BOOTSTRAP" 2>/dev/null

VENV2="$DIR2/iterm2_support/iterm2env/versions/$PY_VER"
if [[ -x "$VENV2/bin/python3" ]]; then
  pass "Venv created at versions/$PY_VER"
else
  fail "Venv creation" "expected python3 at $VENV2/bin/python3"
fi

# ---------------------------------------------------------------------------
# Test 3: Installs iterm2 package
# ---------------------------------------------------------------------------
echo "Test 3: iterm2 package importable in venv"
if [[ -x "$VENV2/bin/python3" ]]; then
  if "$VENV2/bin/python3" -c "import iterm2" 2>/dev/null; then
    pass "import iterm2 succeeds"
  else
    fail "iterm2 import" "import iterm2 failed in venv"
  fi
else
  fail "iterm2 import" "venv python3 not found (test 2 failed)"
fi

# ---------------------------------------------------------------------------
# Test 4: Copies adapter to AutoLaunch
# ---------------------------------------------------------------------------
echo "Test 4: Adapter copied to AutoLaunch directory"
AUTOLAUNCH2="$DIR2/iterm2_support/Scripts/AutoLaunch"
if [[ -f "$AUTOLAUNCH2/claude_tab_status.py" ]]; then
  pass "claude_tab_status.py present in AutoLaunch"
else
  fail "Adapter copy" "claude_tab_status.py not found in $AUTOLAUNCH2"
fi

# ---------------------------------------------------------------------------
# Test 5: Writes marker file
# ---------------------------------------------------------------------------
echo "Test 5: Marker file created after bootstrap"
if [[ -f "$DIR2/marker_dir/bootstrapped" ]]; then
  pass "Marker file created"
else
  fail "Marker file" "expected $DIR2/marker_dir/bootstrapped"
fi

# ---------------------------------------------------------------------------
# Test 5b: Version mismatch triggers re-deploy
# ---------------------------------------------------------------------------
echo "Test 5b: Re-deploys adapter when marker version is outdated"
DIR5B="$TMPDIR_BASE/t5b"
mkdir -p "$DIR5B/marker_dir" "$DIR5B/iterm2_support/iterm2env/versions" "$DIR5B/iterm2_support/Scripts/AutoLaunch"
MARKER5B="$DIR5B/marker_dir/bootstrapped"
echo "0.0.0-old" > "$MARKER5B"

# Copy the working venv so bootstrap skips venv creation but still re-deploys adapter
cp -a "$VENV2" "$DIR5B/iterm2_support/iterm2env/versions/$PY_VER"

ITERM2_SUPPORT="$DIR5B/iterm2_support" \
BOOTSTRAP_MARKER="$MARKER5B" \
BOOTSTRAP_LOCK="$DIR5B/lock" \
CLAUDE_PLUGIN_ROOT="$SCRIPT_DIR" \
  bash "$BOOTSTRAP" 2>/dev/null

if [[ -f "$DIR5B/iterm2_support/Scripts/AutoLaunch/claude_tab_status.py" ]]; then
  pass "Adapter re-deployed on version mismatch"
else
  fail "Version mismatch" "adapter not re-deployed"
fi
NEW_MARKER="$(cat "$MARKER5B")"
if [[ "$NEW_MARKER" == "$CURRENT_VERSION" ]]; then
  pass "Marker updated to current version"
else
  fail "Marker update" "expected '$CURRENT_VERSION', got '$NEW_MARKER'"
fi

# ---------------------------------------------------------------------------
# Test 6: Writes venv-path tracking file
# ---------------------------------------------------------------------------
echo "Test 6: Venv-path tracking file written"
VENV_PATH_FILE="$DIR2/marker_dir/iterm2-tab-status-venv-path"
if [[ -f "$VENV_PATH_FILE" ]]; then
  contents=$(cat "$VENV_PATH_FILE")
  if [[ "$contents" == "$VENV2" ]]; then
    pass "venv-path file contains correct path"
  else
    fail "venv-path contents" "expected '$VENV2', got '$contents'"
  fi
else
  fail "venv-path file" "file not found at $VENV_PATH_FILE"
fi

# ---------------------------------------------------------------------------
# Test 7: Skips venv if existing runtime has iterm2
# ---------------------------------------------------------------------------
echo "Test 7: Skips venv creation when existing runtime has iterm2"
DIR7="$TMPDIR_BASE/t7"
mkdir -p "$DIR7/iterm2_support/iterm2env/versions" "$DIR7/iterm2_support/Scripts/AutoLaunch" "$DIR7/marker_dir"

# Copy the working venv from test 2 into this fake iterm2 support dir
cp -a "$VENV2" "$DIR7/iterm2_support/iterm2env/versions/$PY_VER"

FAKE_PLUGIN7="$DIR7/plugin_root"
mkdir -p "$FAKE_PLUGIN7/scripts" "$FAKE_PLUGIN7/.claude-plugin"
echo '#!/usr/bin/env python3' > "$FAKE_PLUGIN7/scripts/claude_tab_status.py"
echo "{\"version\": \"$CURRENT_VERSION\"}" > "$FAKE_PLUGIN7/.claude-plugin/plugin.json"

ITERM2_SUPPORT="$DIR7/iterm2_support" \
BOOTSTRAP_MARKER="$DIR7/marker_dir/bootstrapped" \
BOOTSTRAP_LOCK="$DIR7/lock" \
CLAUDE_PLUGIN_ROOT="$FAKE_PLUGIN7" \
  bash "$BOOTSTRAP" 2>/dev/null

VENV_PATH_FILE7="$DIR7/marker_dir/iterm2-tab-status-venv-path"
if [[ ! -f "$VENV_PATH_FILE7" ]]; then
  pass "No venv-path file created (existing runtime reused)"
else
  fail "Existing runtime skip" "venv-path file was created despite existing runtime"
fi

# ---------------------------------------------------------------------------
# Test 8: Lock file prevents concurrent runs
# ---------------------------------------------------------------------------
echo "Test 8: Lock file prevents concurrent bootstrap"
DIR8="$TMPDIR_BASE/t8"
mkdir -p "$DIR8/iterm2_support/iterm2env/versions" "$DIR8/iterm2_support/Scripts/AutoLaunch" "$DIR8/marker_dir"
LOCK8="$DIR8/lock"
touch "$LOCK8"

ITERM2_SUPPORT="$DIR8/iterm2_support" \
BOOTSTRAP_MARKER="$DIR8/marker_dir/bootstrapped" \
BOOTSTRAP_LOCK="$LOCK8" \
CLAUDE_PLUGIN_ROOT="$SCRIPT_DIR" \
  bash "$BOOTSTRAP" 2>/dev/null

if [[ ! -f "$DIR8/marker_dir/bootstrapped" ]]; then
  pass "Marker not created when lock is held"
else
  fail "Lock guard" "bootstrap ran despite lock file"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
