# launchd — codex tab status daemon

Durable supervision for `scripts/codex_session.py --daemon` via macOS launchd.
Mirrors the pattern in `~/code/home-lab/launchd/` (conductor-tick).

## Files

| File | Purpose |
|---|---|
| `com.local.codex-tab-status.plist` | LaunchAgent template — `__REPO_ROOT__`, `__HOME__`, `__PYTHON3__` substituted at install time |
| `install-codex-tab-status-launchd.sh` | Resolves python3, expands the plist, bootstraps into `gui/$UID`, verifies first tick |
| `uninstall-codex-tab-status-launchd.sh` | Idempotent bootout + plist removal |

## Install

```bash
bash launchd/install-codex-tab-status-launchd.sh
```

Verifies by:
- Tailing the log at `~/.cache/codex-tab-status.log`
- Listing existing codex signal files at `~/.cache/claude-tab-status/codex-*.json`

After install, open a codex tab in iTerm2 — its title should pick up `⚡` while
codex is working and `💤` once a turn completes. The upstream iTerm2 adapter
(`claude_tab_status.py`) renders these unchanged.

## Uninstall

```bash
bash launchd/uninstall-codex-tab-status-launchd.sh
```

Stops the daemon and removes the agent plist. Signal files in
`~/.cache/claude-tab-status/` self-clean via the adapter's PID-liveness check
when the codex processes they describe exit.

## Manage

| Action | Command |
|---|---|
| Status | `launchctl print gui/$UID/com.local.codex-tab-status` |
| Logs | `tail -f ~/.cache/codex-tab-status.log` |
| Force restart | `launchctl kickstart -k gui/$UID/com.local.codex-tab-status` |

## Why `KeepAlive` not `StartInterval`

`codex_session.py --daemon` is a long-running poll loop (it watches for codex
process start/exit + scans rollout JSONLs continuously). That's a "keep this
running, respawn if it dies" job, not a "fire this every N seconds" job. So
the plist uses `KeepAlive=true` without `StartInterval`.

Compare conductor-tick (`com.mikebook.conductor-tick`) which IS one-shot
per 120s — that uses `StartInterval=120` without `KeepAlive`. Different
script lifecycles, different launchd primitives.
