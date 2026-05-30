# Upstream PR Pitch: Codex Tab Support

## Summary

This branch adds OpenAI Codex CLI tab status support without changing the
existing Claude Code hook or iTerm2 adapter path.

The current plugin already has a clean runtime boundary:

```text
producer writes JSON signal file -> iTerm2 adapter renders tab status
```

Claude Code uses `scripts/hook.sh` as its producer. Codex does not have a hook
API, so this branch adds `scripts/codex_session.py` as a second producer that
writes the same signal-file shape the adapter already understands.

## Motivation

Users often run Claude Code and Codex side by side in separate iTerm2 tabs. The
Claude tabs currently get clear `running`, `idle`, and `attention` indicators,
while Codex tabs have no equivalent status display.

That leaves multi-agent operators with mixed visibility: one runtime is visible
through the plugin, and the other has to be inferred from tab titles,
AppleScript polling, or manual inspection. Codex sessions expose enough local
state through their rollout JSONL files to support the same iTerm2 status
experience.

## What Changed

This branch keeps the adapter untouched and adds Codex support around the
existing signal protocol.

| File | Change |
|---|---|
| `scripts/codex_session.py` | New Codex signal producer. Discovers live Codex processes, matches each one to a rollout JSONL under `~/.codex/sessions`, classifies state, and writes adapter-compatible signal files. |
| `tests/test_classify_codex.py` | Unit coverage for Codex event classification, process parsing, rollout matching, signal shape, and one-shot sweep behavior. |
| `tests/test_codex_session_tty.py` | Regression coverage for per-process TTY lookup so multiple Codex tabs map to the correct iTerm2 sessions. |
| `SMOKE.md` | Manual smoke procedure for unit tests, one-shot signal generation, and end-to-end iTerm2 verification. |
| `launchd/` | Optional macOS LaunchAgent template plus install/uninstall scripts for keeping the Codex producer daemon running. |
| `UPSTREAM-PR-PITCH.md` | This upstream-facing summary. |

Relative to the upstream `v0.2.0` snapshot, this branch adds files only. It does
not modify `scripts/claude_tab_status.py` or `scripts/hook.sh`.

## How Codex State Is Derived

`scripts/codex_session.py` runs either once or as a daemon:

```bash
python3 scripts/codex_session.py
python3 scripts/codex_session.py --daemon
```

Each sweep:

1. Finds live Codex CLI processes with `ps`.
2. Resolves each process TTY independently so same-window multi-tab sessions do
   not collide.
3. Finds the newest matching `rollout-*.jsonl` under `~/.codex/sessions`.
4. Reads recent rollout events.
5. Writes `${CLAUDE_ITERM2_TAB_STATUS_DIR:-...}/codex-<uuid>.json`.

State mapping:

| Codex event pattern | Signal `type` |
|---|---|
| Recent `task_started` with no later `task_complete` | `running` |
| Recent assistant/tool/reasoning activity | `running` |
| Latest `task_complete` | `idle` |
| Quiet longer than `CODEX_IDLE_AFTER` | `idle` |
| Latest `turn_aborted` | `idle` |
| No rollout or unreadable rollout | No signal written |

The producer intentionally does not synthesize `attention`. In the existing
plugin, `attention` means a permission prompt that should flash and show a
badge. Codex rollout events do not currently expose an exact equivalent, so the
Codex producer stays conservative and uses `running`/`idle` only.

## Backward Compatibility With Claude Code

The Claude path remains unchanged:

```text
Claude Code hooks -> scripts/hook.sh -> signal JSON -> claude_tab_status.py
```

Codex adds a parallel path:

```text
Codex rollout JSONL -> scripts/codex_session.py -> signal JSON -> claude_tab_status.py
```

Compatibility details:

- `scripts/claude_tab_status.py` is not modified.
- `scripts/hook.sh` is not modified.
- Codex writes the same core signal fields: `session_id`, `type`, `message`,
  `project`, `cwd`, `tty`, `pid`, and `ts`.
- Codex session IDs are prefixed with `codex-` to avoid collision with Claude
  session IDs.
- Codex uses the same `CLAUDE_ITERM2_TAB_STATUS_DIR` signal directory, so the
  existing adapter discovers both runtimes without new adapter configuration.
- Codex sets `pid` to the live Codex process and `tty` to the owning iTerm2 TTY,
  preserving the adapter's existing matching and stale-signal cleanup behavior.
- The optional `runtime: "codex"` field is diagnostic only. The adapter already
  ignores unknown fields.
- Claude installation, configuration, prefixes, subtitle support, badges,
  notifications, and permission-prompt behavior are unchanged.

## Configuration

The Codex producer has a small set of environment variables:

| Variable | Purpose |
|---|---|
| `CLAUDE_ITERM2_TAB_STATUS_DIR` | Reuses the existing plugin signal directory. |
| `CODEX_SESSIONS_DIR` | Overrides the default `~/.codex/sessions` rollout root. |
| `CODEX_POLL_INTERVAL` | Daemon sweep interval, default `2.0` seconds. |
| `CODEX_IDLE_AFTER` | Seconds of silence before a session is considered idle, default `30`. |

The launchd files are optional convenience wrappers for macOS users who want the
Codex producer to run continuously.

## Validation

Suggested checks for the upstream PR:

```bash
python3 -m pytest tests/test_classify_codex.py tests/test_codex_session_tty.py -q
```

Manual smoke coverage is documented in `SMOKE.md`:

1. Run the Codex unit tests.
2. Run a one-shot sweep into a temporary signal directory.
3. Run the daemon and verify a live Codex tab gets a status prefix.
4. Open a Claude tab in parallel and verify the existing Claude behavior is
   unchanged.

## Maintainer Notes

- The PR is intentionally producer-only. It treats the signal-file protocol as
  the stable integration point and avoids adding runtime-specific code to the
  iTerm2 adapter.
- The same pattern could support other runtimes later: add a runtime-specific
  producer that writes the shared signal schema.
- If upstream prefers not to ship launchd helpers in the first Codex PR, the
  `launchd/` directory can be split into a follow-up without changing the core
  producer design.
