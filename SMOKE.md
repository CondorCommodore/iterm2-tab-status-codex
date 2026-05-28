# Smoke test — codex-session support

The iTerm2 adapter (`claude_tab_status.py`) is unchanged. All codex support
lives in `scripts/codex_session.py`, a separate signal-file producer that
writes the same JSON shape Claude Code hooks already write.

## 1. Unit tests (safe to run anywhere)

```bash
cd ~/code/iterm2-tab-status-codex
python3 -m pytest tests/test_classify_codex.py -q
```

All 20 should pass. Baseline upstream failures in `tests/test_adapter.py`
(14 `pytest.mark.asyncio` failures) are pre-existing environmental issues
(missing `pytest-asyncio` plugin), not regressions from this fork.

## 2. One-shot sweep against your live system

This will look at your real codex tabs and write real signal files.

```bash
# Show what would be written (does NOT touch live signal dir until you remove --dry).
CLAUDE_ITERM2_TAB_STATUS_DIR=/tmp/codex-smoke python3 scripts/codex_session.py
ls -la /tmp/codex-smoke/
cat /tmp/codex-smoke/codex-*.json
```

Expect: one JSON per live codex tab, with `type` in
`{running, idle, attention}`, `tty=/dev/ttysNNN`, `pid=<shell pid>`.

If no codex tabs are open, output is empty — that is correct behavior.

## 3. End-to-end: light up codex tabs in iTerm2

The iTerm2 AutoLaunch script must be reloaded after any code change. The
operator (not Claude) does this from the iTerm2 menu.

### Steps

1. **Stop the running daemon if installed**
   ```bash
   pkill -f 'codex_session.py --daemon' 2>/dev/null
   ```
2. **Start the daemon in foreground for the smoke**
   ```bash
   python3 ~/code/iterm2-tab-status-codex/scripts/codex_session.py --daemon
   ```
   It logs nothing on success; errors go to stderr.
3. **Open a codex tab** (e.g. `codex` in a fresh iTerm2 tab). Within ~3
   seconds the tab title should pick up the `⚡ ` prefix while codex is
   working, then `💤 ` once codex completes its turn.
4. **Verify a claude tab still works**: open a `claude` tab in parallel.
   It must continue to behave exactly as before — claude is signaled by
   the existing hook, codex by this new producer; the adapter treats both
   identically.

### Suggested install (operator decision)

Add a launchd plist to keep the daemon running:

```xml
<!-- ~/Library/LaunchAgents/com.local.codex-tab-status.plist -->
<plist version="1.0"><dict>
  <key>Label</key><string>com.local.codex-tab-status</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/mikebook/code/iterm2-tab-status-codex/scripts/codex_session.py</string>
    <string>--daemon</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

Then: `launchctl load ~/Library/LaunchAgents/com.local.codex-tab-status.plist`.

## 4. Things to check

- **Stale signals**: when you close a codex tab, the adapter's existing
  PID-liveness check should clear the signal after ~10s (the codex
  process exits → PID dies → adapter removes the file).
- **Multi-tab**: two codex tabs in the same iTerm2 window should each
  get their own signal file (keyed by rollout UUID).
- **No double-claim**: a tab that is BOTH a claude tab and somehow has a
  codex process child should not duplicate. `_ps_codex_procs` matches
  on argv[0]=`codex`, not on substring, so claude (which spawns `node`)
  is not falsely tagged.
