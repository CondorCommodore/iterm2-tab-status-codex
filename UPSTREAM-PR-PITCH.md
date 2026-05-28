# Upstream PR pitch — add codex-session support

## What this adds

A second runtime — OpenAI's `codex` CLI — gets the same per-tab status
indicator the plugin already provides for Claude Code. No changes to the
iTerm2 adapter; one new producer script.

## Why

Operators running multiple AI runtimes side-by-side (Claude Code +
codex) currently see status indicators on half their tabs. Fleet
conductors fall back to AppleScript polling to figure out codex state,
which is fragile.

Codex tabs are a perfect fit for this plugin's existing protocol —
their state is just as observable, and the *display* problem is
identical to claude's.

## Architecture (deliberately small)

The plugin's signal-file protocol turns out to be a clean public
interface. Anything that can write `${STATUS_DIR}/<sid>.json` in the
documented shape gets a status indicator for free. The PR exploits
this:

- **No adapter changes.** `claude_tab_status.py` is byte-identical to
  upstream `0.2.0`.
- **One new producer: `scripts/codex_session.py`** that:
  1. Finds live codex CLI processes via `ps -axo pid,tty,lstart,command`.
  2. Locates each tab's rollout JSONL under `~/.codex/sessions/`.
  3. Classifies state from `task_started` / `task_complete` /
     `turn_aborted` / `agent_message` events.
  4. Writes the same signal-file shape as `hook.sh`, with `pid` set to
     the stable login-shell PID (so the adapter's stale-PID cleanup
     works identically).

This means future runtimes (Gemini CLI, Aider, etc.) can be added the
same way — a small producer per runtime, zero coupling to the adapter.

## Why not extend hook.sh?

Codex has no hook system equivalent. The only observable surface is
the rollout JSONL it writes per session. So a polling producer is the
right shape.

## What changed in this PR

| File | Change |
|---|---|
| `scripts/codex_session.py` | NEW — producer (~280 lines) |
| `tests/test_classify_codex.py` | NEW — 20 unit tests |
| `SMOKE.md` | NEW — manual smoke procedure |
| `.gitignore` | adds `.in_use/`, `.venv/` |
| `scripts/claude_tab_status.py` | UNCHANGED |
| `scripts/hook.sh` | UNCHANGED |

## State mapping

| Codex event sequence | Signal `type` |
|---|---|
| `task_started` recent, no later `task_complete` | `running` |
| `task_complete` latest, or quiet > 30s | `idle` |
| `agent_message` / `function_call` / `reasoning` within 30s | `running` |
| `turn_aborted` latest | `idle` (deliberately *not* `attention`) |
| empty / no rollout matched | `idle` |

`attention` is reserved for permission-prompts (the upstream meaning).
Codex doesn't have a 1:1 analogue today, so we don't synthesize one —
better than misusing the flash/badge channel.

## Smoke procedure

See `SMOKE.md`. Two steps:

1. `pytest tests/test_classify_codex.py` (20 pure-function tests).
2. Run `codex_session.py --daemon`, open a codex tab, watch the title
   pick up the prefix.

## Configuration knobs (env vars)

- `CODEX_SESSIONS_DIR` — override `~/.codex/sessions`
- `CODEX_POLL_INTERVAL` — daemon sweep period (default `2.0`)
- `CODEX_IDLE_AFTER` — seconds of silence before declaring idle (default `30`)
- `CLAUDE_ITERM2_TAB_STATUS_DIR` — reused from upstream

## Install / lifecycle

Producer needs to run as a daemon. The PR could optionally:

- ship a launchd plist template in `scripts/`
- extend `bootstrap.sh` to install/load it when codex is detected on PATH

I'd suggest doing that in a follow-up PR; this PR keeps to the producer
+ tests + docs.

## Open questions for upstream maintainer

1. Naming: I used `codex-<uuid>` for the `session_id` to avoid collision
   with claude's UUIDs. Acceptable?
2. Optional `runtime: "codex"` field added to the signal JSON for
   debuggability. The adapter ignores unknown fields today. Want it
   documented in the README signal schema?
3. Open to renaming `codex_session.py` to something more general like
   `rollout_session_producer.py` if you want to leave room for other
   JSONL-based runtimes.
