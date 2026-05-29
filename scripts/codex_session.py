"""codex-session — signal-file producer for codex tabs.

The upstream iTerm2 adapter (claude_tab_status.py) consumes signal files written
by Claude Code hooks. Codex has no equivalent hook system, so this module
synthesizes the same signal files by polling per-tab codex rollout JSONLs.

Architecture:

  ps (find codex processes) -> codex sessions (with tty + pid)
  for each session:
    locate newest ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl matching tab
    classify last events into running/idle/attention
    write ${STATUS_DIR}/<session_id>.json in upstream signal format

The adapter then displays codex tabs with no changes to its own code.

Daemon mode:
  python3 codex_session.py --daemon
    polls every CODEX_POLL_INTERVAL seconds (default 2) and writes signals.

One-shot mode (testing / cron):
  python3 codex_session.py
    runs one sweep and exits.

Environment:
  CLAUDE_ITERM2_TAB_STATUS_DIR  signal dir (default matches upstream hook.sh)
  CODEX_SESSIONS_DIR            override ~/.codex/sessions
  CODEX_POLL_INTERVAL           daemon sweep period in seconds (default 2.0)
  CODEX_IDLE_AFTER              seconds after last event to call idle (default 30)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# --- Defaults / config -------------------------------------------------------

_HOME = os.path.expanduser("~")


def _default_signal_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(_HOME, ".cache")
    return os.path.join(base, "claude-tab-status")


def _default_sessions_dir() -> str:
    return os.environ.get("CODEX_SESSIONS_DIR") or os.path.join(_HOME, ".codex", "sessions")


SIGNAL_DIR = os.environ.get("CLAUDE_ITERM2_TAB_STATUS_DIR") or _default_signal_dir()
IDLE_AFTER = float(os.environ.get("CODEX_IDLE_AFTER", "30"))
POLL_INTERVAL = float(os.environ.get("CODEX_POLL_INTERVAL", "2.0"))

_ROLLOUT_RE = re.compile(
    r"rollout-(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(?P<uuid>[0-9a-f-]{36})\.jsonl$"
)

# --- Codex-process discovery -------------------------------------------------


@dataclass(frozen=True)
class CodexProc:
    pid: int
    tty: str  # full /dev/ttysNNN form
    started: float  # epoch seconds; best-effort


def _ps_codex_procs(ps_output: Optional[str] = None) -> list[CodexProc]:
    """Find live codex CLI processes via ps. Pure function — pass ps_output to test.

    Looks for processes whose command is `codex` or starts with `codex ` (the CLI),
    excluding anything that just *mentions* codex (grep, editors, etc.).
    """
    lookup_tty_per_pid = ps_output is None
    if ps_output is None:
        try:
            ps_output = subprocess.check_output(
                ["ps", "-axo", "pid=,lstart=,command="],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []

    procs: list[CodexProc] = []
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        # Test fixtures may use the older inline-tty shape:
        # "<pid> <tty> <lstart...5 fields...> <command...>"
        # Live discovery deliberately omits tty from the bulk ps scan and
        # resolves it per PID below, avoiding stale/cached tty reuse.
        parts = line.split(None, 7)
        if len(parts) >= 8 and _looks_like_tty_field(parts[1]):
            pid_s = parts[0]
            tty_s = parts[1]
            lstart = " ".join(parts[2:7])
            command = parts[7]
        elif len(parts) >= 7:
            pid_s = parts[0]
            tty_s = None
            lstart = " ".join(parts[1:6])
            command = parts[6]
        else:
            continue

        if not _is_codex_command(command):
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        tty_full = _tty_for_pid(pid) if lookup_tty_per_pid else _normalize_tty(tty_s)
        if not tty_full:
            continue
        started = _parse_lstart(lstart)
        procs.append(CodexProc(pid=pid, tty=tty_full, started=started))
    return procs


def _looks_like_tty_field(value: str) -> bool:
    return value in ("?", "??", "-") or value.startswith(("tty", "/dev/tty"))


def _normalize_tty(tty_s: str | None) -> str | None:
    if tty_s is None:
        return None
    tty_s = tty_s.strip()
    if tty_s in ("", "?", "??", "-"):
        return None
    if any(ch.isspace() for ch in tty_s):
        return None
    return tty_s if tty_s.startswith("/dev/") else f"/dev/{tty_s}"


def _tty_for_pid(pid: int) -> str | None:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "tty="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    first = out.splitlines()[0] if out.splitlines() else ""
    return _normalize_tty(first)


def _is_codex_command(command: str) -> bool:
    """True iff this command line is a codex CLI invocation we should track."""
    # First token = program path. Strip path.
    first = command.split()[0] if command.strip() else ""
    name = os.path.basename(first)
    if name == "codex":
        return True
    # `node /path/to/codex/cli.js ...` — codex CLI may be a node script.
    # Be conservative; only match if the *argv[1]* basename is exactly 'codex'.
    if name in ("node", "deno", "bun"):
        rest = command.split()
        if len(rest) >= 2 and os.path.basename(rest[1]) == "codex":
            return True
    return False


def _parse_lstart(lstart: str) -> float:
    """Parse `ps -o lstart=` format to epoch. Best-effort, returns 0.0 on fail."""
    # Format: "Mon May 28 19:20:01 2026"
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b  %d %H:%M:%S %Y"):
        try:
            return time.mktime(time.strptime(lstart, fmt))
        except ValueError:
            continue
    return 0.0


# --- Rollout-file discovery --------------------------------------------------


def find_rollouts(sessions_dir: str) -> list[Path]:
    """Return all rollout-*.jsonl under sessions_dir, newest first by mtime."""
    root = Path(sessions_dir)
    if not root.is_dir():
        return []
    out: list[tuple[float, Path]] = []
    for p in root.rglob("rollout-*.jsonl"):
        try:
            out.append((p.stat().st_mtime, p))
        except OSError:
            continue
    out.sort(reverse=True)
    return [p for _, p in out]


def match_rollout_for_proc(
    proc: CodexProc, rollouts: Iterable[Path]
) -> Optional[Path]:
    """Pick the rollout that most likely belongs to this codex process.

    Strategy: newest rollout whose mtime >= proc.started (with a small grace).
    Codex creates the rollout at session start, then appends as events fire.
    If proc.started is 0 (parse failed), fall back to most recently modified.
    """
    grace = 5.0
    candidates = []
    for r in rollouts:
        try:
            mt = r.stat().st_mtime
        except OSError:
            continue
        if proc.started <= 0 or mt + grace >= proc.started:
            candidates.append((mt, r))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


# --- Rollout classification --------------------------------------------------

# State strings reused from upstream signal vocabulary.
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_ATTENTION = "attention"


def _read_last_events(path: Path, max_events: int = 50) -> list[dict]:
    """Return the last `max_events` parsed JSONL events from path."""
    try:
        # Read whole file — rollouts can be large; tail by line count.
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 256 * 1024)  # 256KB tail is plenty for 50 events
            f.seek(size - chunk, os.SEEK_SET)
            data = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    lines = data.splitlines()[-max_events:]
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _event_ts(event: dict) -> float:
    """Parse the 'timestamp' field to epoch seconds (UTC). 0.0 on failure.

    Format: 2026-05-26T14:48:07.391Z
    """
    import calendar

    ts = event.get("timestamp")
    if not isinstance(ts, str):
        return 0.0
    clean = ts.rstrip("Z")
    frac = 0.0
    if "." in clean:
        clean, frac_s = clean.split(".", 1)
        try:
            frac = float("0." + frac_s)
        except ValueError:
            frac = 0.0
    try:
        t = time.strptime(clean, "%Y-%m-%dT%H:%M:%S")
        return calendar.timegm(t) + frac
    except (ValueError, TypeError):
        return 0.0


def classify_codex_state(
    events: list[dict],
    now: Optional[float] = None,
    idle_after: float = IDLE_AFTER,
) -> str:
    """Classify codex session state from recent rollout events.

    Rules (precedence top to bottom):
      - errored/attention: last event_msg payload type is 'turn_aborted'
      - running: last task_started has no matching task_complete after it,
                 OR last event mtime is within `idle_after` seconds AND
                 last payload type is task_started/agent_message/function_call/reasoning
      - idle: last payload type is task_complete, or quiet for > idle_after
    Default: idle.
    """
    if not events:
        return STATE_IDLE
    now = now if now is not None else time.time()

    # Walk in reverse to find the most recent decisive event.
    last_task_started_idx = -1
    last_task_complete_idx = -1
    last_aborted_idx = -1
    last_payload_type: Optional[str] = None
    last_ts = 0.0

    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        payload = ev.get("payload") or {}
        if isinstance(payload, dict):
            pt = payload.get("type")
            if pt:
                last_payload_type = pt
                if pt == "task_started":
                    last_task_started_idx = i
                elif pt == "task_complete":
                    last_task_complete_idx = i
                elif pt == "turn_aborted":
                    last_aborted_idx = i
        ts = _event_ts(ev)
        if ts > last_ts:
            last_ts = ts

    # Aborted takes precedence only if it is the *latest* decisive event.
    if last_aborted_idx > max(last_task_started_idx, last_task_complete_idx):
        # Map to 'idle' rather than 'attention'; aborted is not a permission prompt.
        # Upstream 'attention' triggers flash+badge, which would be misleading.
        return STATE_IDLE

    # No activity in a while -> idle, regardless of task markers.
    # (A task_started that never produced a task_complete but went quiet for
    # > idle_after seconds is almost certainly an orphaned/dead session.)
    if last_ts and (now - last_ts) > idle_after:
        return STATE_IDLE

    # Active task: started after the last complete.
    if last_task_started_idx > last_task_complete_idx:
        return STATE_RUNNING

    # Recent activity but no task_started — treat assistant streaming as running.
    if last_payload_type in {"agent_message", "function_call", "reasoning", "tool_search_call"}:
        # Only if the activity is fresh.
        if last_ts and (now - last_ts) <= idle_after:
            return STATE_RUNNING

    return STATE_IDLE


# --- Signal-file emission ----------------------------------------------------


def _session_id_from_rollout(path: Path) -> str:
    """Derive a stable session_id from the rollout filename uuid."""
    m = _ROLLOUT_RE.search(path.name)
    if m:
        return f"codex-{m.group('uuid')}"
    # Fallback: hash-ish from name.
    return f"codex-{path.stem}"


def _cwd_from_rollout(events: list[dict]) -> str:
    """Find cwd from session_meta payload if present."""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "session_meta":
            p = ev.get("payload") or {}
            if isinstance(p, dict):
                cwd = p.get("cwd")
                if isinstance(cwd, str):
                    return cwd
    return ""


def build_signal(
    proc: CodexProc, rollout: Path, state: str, head_events: list[dict] | None = None
) -> dict:
    """Build a signal dict matching the upstream schema."""
    sid = _session_id_from_rollout(rollout)
    cwd = ""
    if head_events:
        cwd = _cwd_from_rollout(head_events)
    project = os.path.basename(cwd) if cwd else "codex"
    return {
        "session_id": sid,
        "type": state,
        "message": "",
        "project": project,
        "cwd": cwd,
        "tty": proc.tty,
        "pid": str(proc.pid),
        "ts": str(int(time.time())),
        "runtime": "codex",  # diagnostic; adapter ignores unknown fields
    }


def write_signal(signal_dir: str, signal: dict) -> Path:
    """Atomically write a signal file."""
    d = Path(signal_dir)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    sid = signal["session_id"]
    final = d / f"{sid}.json"
    tmp = d / f".{sid}.json.tmp"
    tmp.write_text(json.dumps(signal, indent=2))
    os.replace(tmp, final)
    return final


def _read_head_events(path: Path, n: int = 3) -> list[dict]:
    """Read first n JSONL events for session_meta extraction."""
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= n:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


# --- Sweep -------------------------------------------------------------------


def sweep_once(signal_dir: str = SIGNAL_DIR, sessions_dir: Optional[str] = None) -> list[Path]:
    """Run one discovery + classify + emit pass. Returns list of written files."""
    sessions_dir = sessions_dir or _default_sessions_dir()
    procs = _ps_codex_procs()
    if not procs:
        return []
    rollouts = find_rollouts(sessions_dir)
    written: list[Path] = []
    used_rollouts: set[Path] = set()
    for proc in procs:
        rollout = match_rollout_for_proc(proc, [r for r in rollouts if r not in used_rollouts])
        if not rollout:
            continue
        used_rollouts.add(rollout)
        events = _read_last_events(rollout)
        state = classify_codex_state(events)
        head = _read_head_events(rollout)
        signal = build_signal(proc, rollout, state, head)
        try:
            written.append(write_signal(signal_dir, signal))
        except OSError as e:
            print(f"codex_session: write failed for {signal['session_id']}: {e}", file=sys.stderr)
    return written


def daemon(signal_dir: str = SIGNAL_DIR, interval: float = POLL_INTERVAL) -> None:
    """Run sweep_once in a loop until interrupted."""
    while True:
        try:
            sweep_once(signal_dir)
        except Exception as e:  # noqa: BLE001
            print(f"codex_session: sweep error: {e}", file=sys.stderr)
        time.sleep(interval)


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if "--daemon" in argv:
        daemon()
        return 0
    written = sweep_once()
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
