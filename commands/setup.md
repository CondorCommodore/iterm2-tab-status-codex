---
description: Manually run iTerm2 bootstrap (create Python runtime + deploy adapter)
---

Run the iTerm2 tab status bootstrap script to set up the Python runtime and deploy the adapter.

Execute this command:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh"
```

If the bootstrap was already completed, it will skip automatically. To force re-bootstrap, first remove the marker:
```bash
rm -f ~/.claude/iterm2-tab-status-bootstrapped
bash "${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh"
```

After bootstrap completes, restart iTerm2 or toggle the script off/on via Scripts → AutoLaunch → claude_tab_status.py (click twice).
