# Upstream PR draft

## Title (≤70 chars)

`feat: add codex CLI support via signal-file producer (no adapter changes)`

(66 chars)

## Body

### Summary

This PR adds per-tab status indicators for OpenAI's `codex` CLI by
introducing a *second producer* that writes the plugin's existing
signal-file format. The iTerm2 adapter
(`scripts/claude_tab_status.py`) is **byte-identical to upstream
v0.2.0** — no logic changes, no schema changes, no risk to existing
Claude Code users.

### The architectural insight

The plugin's signal-file protocol (`${STATUS_DIR}/<sid>.json`) turns
out to be a clean **public interface**. Anything that writes JSON of
the documented shape gets a tab indicator for free. Today that
interface has exactly one producer: `hook.sh`, fired by Claude Code's
hook system.

This PR treats the protocol as the contract it already is and adds a
second producer alongside `hook.sh`:

- `hook.sh` — producer for Claude Code (event-driven, unchanged)
- `scripts/codex_session.py` — producer for codex CLI (polling,
  because codex has no hook surface)

Future runtimes (Gemini CLI, Aider, Cursor agent, anything with an
observable session log) slot into the same shape: a small per-runtime
producer, zero coupling to the adapter, zero risk of cross-runtime
regressions.

### Why `claude_tab_status.py` is untouched

The adapter's job is "read signal files, paint tab titles, GC dead
PIDs." It is runtime-agnostic by design — it never decides *what*
running/idle/attention mean, it just renders them. Pushing
runtime-specific logic (codex rollout parsing, ps scanning, JSONL
tailing) into the adapter would have:

- introduced a dependency on codex internals into the hot path that
  paints every tab,
- forced upstream to inherit codex maintenance burden,
- broken the clean separation that makes future runtimes a trivial
  add.

Keeping the adapter untouched is the whole point.

### What's new

| File | Change | Lines |
|---|---|---|
| `scripts/codex_session.py` | NEW — producer | ~450 |
| `tests/test_classify_codex.py` | NEW — 20 unit tests | ~225 |
| `SMOKE.md` | NEW — operator smoke procedure | ~90 |
| `.gitignore` | adds `.in_use/`, `.venv/` | small |
| `scripts/claude_tab_status.py` | **UNCHANGED** | 0 |
| `scripts/hook.sh` | **UNCHANGED** | 0 |

### How the producer works

1. `ps -axo pid,tty,lstart,command` finds live `codex` CLI processes.
2. Each process is matched to a rollout JSONL under
   `~/.codex/sessions/YYYY/MM/DD/rollout-<uuid>.jsonl` by start time.
3. The tail of that JSONL is classified by event type
   (`task_started` / `task_complete` / `turn_aborted` /
   `agent_message` / `function_call` / `reasoning`).
4. The result is written to `${STATUS_DIR}/codex-<uuid>.json` using
   the same field shape as `hook.sh`, with `pid` set to the stable
   login-shell PID so the adapter's existing stale-PID GC works
   identically.

### State mapping

| Codex event sequence | Signal `type` |
|---|---|
| `task_started` recent, no later `task_complete` | `running` |
| `task_complete` latest, or quiet > `CODEX_IDLE_AFTER` | `idle` |
| `agent_message` / `function_call` / `reasoning` within idle window | `running` |
| `turn_aborted` latest | `idle` |
| no rollout matched | `idle` |

`attention` is deliberately **not** synthesized. In the upstream
schema, `attention` means "the runtime needs operator input"
(permission prompts). Codex has no 1:1 analogue today, and misusing
the flash/badge channel would degrade the indicator's signal-to-noise
for Claude users. If a real analogue emerges (e.g. codex pause-on-approval),
it can be added in a follow-up.

### Configuration

All knobs are env vars; defaults match operator expectations:

- `CODEX_SESSIONS_DIR` — override `~/.codex/sessions`
- `CODEX_POLL_INTERVAL` — daemon sweep period (default `2.0`)
- `CODEX_IDLE_AFTER` — silence threshold for `idle` (default `30`)
- `CLAUDE_ITERM2_TAB_STATUS_DIR` — reused from upstream

### Tests

`tests/test_classify_codex.py` — **20/20 passing**. Pure-function
tests over the classifier + producer logic; no iTerm2 dependency.

```bash
python3 -m pytest tests/test_classify_codex.py -q
# 20 passed
```

Baseline `tests/test_adapter.py` upstream failures (missing
`pytest-asyncio` plugin in CI env) are pre-existing and unchanged
by this PR.

### Smoke recipe (operator side)

Reproduced from `SMOKE.md` §1–3:

**1. Unit tests**
```bash
cd ~/code/iterm2-tab-status-codex
python3 -m pytest tests/test_classify_codex.py -q
```

**2. One-shot sweep against your live system**
```bash
CLAUDE_ITERM2_TAB_STATUS_DIR=/tmp/codex-smoke python3 scripts/codex_session.py
ls -la /tmp/codex-smoke/
cat /tmp/codex-smoke/codex-*.json
```
Expect: one JSON per live codex tab, `type` in `{running, idle, attention}`,
`tty=/dev/ttysNNN`, `pid=<shell pid>`. If no codex tabs are open,
output is empty — that is correct behavior.

**3. End-to-end in iTerm2**
```bash
pkill -f 'codex_session.py --daemon' 2>/dev/null
python3 ~/code/iterm2-tab-status-codex/scripts/codex_session.py --daemon
```
Open a fresh `codex` tab. Within ~3s the title picks up the `⚡ `
prefix while codex is working, then `💤 ` after the turn completes.
A parallel `claude` tab must continue to behave exactly as before —
that's the regression guarantee.

### Install / lifecycle

The producer needs a long-lived daemon. **This PR ships only the
producer + tests + docs**, on the principle that lifecycle is
distro-specific and should land in a focused follow-up.

A reference launchd LaunchAgent (plist + install/uninstall scripts)
lives in `launchd/` in the fork
(`CondorCommodore/iterm2-tab-status-codex@feature/launchd`) and works
out of the box on macOS. I've left it **out of the upstream diff** so
you can decide whether to:

- merge it as part of this PR,
- take it as a follow-up PR,
- or leave lifecycle to operators / `bootstrap.sh` extension.

Happy to do whichever you prefer.

### Files filtered OUT of this upstream PR

The fork (`CondorCommodore/iterm2-tab-status-codex`) carries a few
files that are **not** part of this proposal:

| Path | Why excluded |
|---|---|
| `launchd/` | macOS-launchd-specific; lifecycle is a separate concern (see above) |
| `UPSTREAM-PR-PITCH.md` | fork-internal scratch doc |
| `UPSTREAM-PR-DRAFT.md` | this draft itself |

### Open questions for the maintainer

1. **Session ID prefix.** I used `codex-<uuid>` for the signal-file
   stem to avoid colliding with claude's UUIDs. Acceptable, or
   prefer a different convention?
2. **Optional `runtime` field.** The producer writes
   `"runtime": "codex"` into the JSON for debuggability. The adapter
   ignores unknown fields today; want it documented in the README
   signal schema, or stripped?
3. **Producer naming.** Open to renaming `codex_session.py` to
   something more general (e.g. `rollout_session_producer.py`) if
   you want to signal that other JSONL-based runtimes belong here.
4. **Lifecycle scope.** Include the `launchd/` LaunchAgent in this
   PR, take it as a follow-up, or leave it to operators?

### Non-goals

- No changes to the Claude Code path.
- No changes to the signal-file schema.
- No new dependencies (stdlib only).
- No changes to `bootstrap.sh` (deferred until lifecycle decision).

---

## Drafter's notes (for operator review — strip before pushing upstream)

- All commits on `feature/launchd` in the fork. The upstream PR
  should be cherry-picked from commits `044e334`, `c516102`,
  `70c582b`, `5aff52e` (the launchd commit `27e4b71` stays out per
  the filter above).
- Suggested upstream branch name: `feat/codex-session-producer`.
- Before pushing: confirm with maintainer whether they want
  `launchd/` bundled or split.
