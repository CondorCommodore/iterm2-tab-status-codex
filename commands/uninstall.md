---
description: Remove iTerm2 tab status adapter, venv (if created by iterm2-tab-status), and signal files
---

Clean up all iterm2-tab-status artifacts from the system:

1. Remove the adapter script from iTerm2 AutoLaunch
2. Remove the Python venv if it was created by bootstrap (not pre-existing)
3. Remove signal files
4. Remove marker and tracking files

Execute this cleanup:
```bash
ITERM2_SUPPORT="$HOME/Library/Application Support/iTerm2"
AUTOLAUNCH="$ITERM2_SUPPORT/Scripts/AutoLaunch"

# 1. Remove adapter from AutoLaunch
rm -f "$AUTOLAUNCH/claude_tab_status.py"
echo "Removed adapter from AutoLaunch"

# 2. Remove venv if we created it
VENV_PATH_FILE="$HOME/.claude/iterm2-tab-status-venv-path"
if [[ -f "$VENV_PATH_FILE" ]]; then
  VENV_PATH="$(cat "$VENV_PATH_FILE")"
  if [[ -d "$VENV_PATH" ]]; then
    rm -rf "$VENV_PATH"
    echo "Removed venv: $VENV_PATH"
  fi
  rm -f "$VENV_PATH_FILE"
fi

# 3. Remove signal files
rm -rf /tmp/claude-tab-status

# 4. Remove marker and lock
rm -f "$HOME/.claude/iterm2-tab-status-bootstrapped"
rm -f /tmp/claude-tab-status-bootstrap.lock

echo "Cleanup complete. Restart iTerm2 to finish."
```

After running, restart iTerm2. Then uninstall the plugin: `/plugin uninstall iterm2-tab-status`
