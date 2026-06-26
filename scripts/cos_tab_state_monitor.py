#!/usr/bin/env python3
"""COS tab-state monitor for Claude/Codex iTerm signal files.

This is a read-only companion to ``claude_tab_status.py``. The renderer keeps
iTerm tabs visually marked; this monitor turns the same signal directory into a
deduped COS-readable state file and an append-only transition log.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

VALID_STATES = {"running", "idle", "attention"}
DEFAULT_CURRENT_NAME = "tab-state-current.json"
DEFAULT_EVENTS_NAME = "tab-state-events.jsonl"

ITERM_TAB_STATE_SCRIPT = """\
tell application "iTerm2"
  set out to ""
  repeat with w from 1 to count of windows
    set theWin to window w
    repeat with t from 1 to count of tabs of theWin
      set sess to current session of (tab t of theWin)
      set out to out & w & "|" & t & "|" & (tty of sess) & "|" & ¬
        (is processing of sess) & "|" & (name of sess) & linefeed
    end repeat
  end repeat
  return out
end tell
"""


def default_signal_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    return Path(base) / "claude-tab-status"


def default_report_dir() -> Path:
    return Path.home() / ".claude" / "plans" / "fleet-reports"


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _parse_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _normalize_state(value: object) -> str:
    state = str(value or "").strip().lower()
    return state if state in VALID_STATES else "unknown"


def _parse_bool(value: object) -> bool | None:
    text = str(value or "").strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def read_live_iterm_states(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, dict[str, Any]]:
    """Return live iTerm processing state by TTY.

    Signal files are hook-derived and can lag after tab/session churn. iTerm's
    `is processing` is the authoritative busy bit for dispatch safety, so this
    source only ever tightens the monitor state.
    """
    try:
        result = run(
            ["osascript", "-e", ITERM_TAB_STATE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}

    states: dict[str, dict[str, Any]] = {}
    for raw_line in result.stdout.splitlines():
        parts = raw_line.split("|", 4)
        if len(parts) != 5:
            continue
        raw_window, raw_tab, tty, raw_processing, name = parts
        tty = tty.strip()
        processing = _parse_bool(raw_processing)
        if not tty or processing is None:
            continue
        try:
            window = int(raw_window)
            tab = int(raw_tab)
        except ValueError:
            window = None
            tab = None
        states[tty] = {
            "window": window,
            "tab": tab,
            "tty": tty,
            "is_processing": processing,
            "name": name,
        }
    return states


@dataclass(frozen=True)
class SignalRecord:
    path: Path
    session_id: str
    state: str
    tty: str
    pid: int
    ts: int
    mtime: float
    runtime: str
    cwd: str
    project: str
    message: str
    live_pid: bool

    @property
    def sort_key(self) -> tuple[int, float, str]:
        return (self.ts, self.mtime, self.session_id)

    def to_tab(self, *, now_ts: float) -> dict[str, Any]:
        return {
            "tty": self.tty,
            "pid": self.pid,
            "runtime": self.runtime,
            "cwd": self.cwd,
            "project": self.project,
            "state": self.state,
            "age_seconds": max(0, int(now_ts - self.ts)) if self.ts else None,
            "session_id": self.session_id,
            "message": self.message,
            "signal_path": str(self.path),
            "signal_ts": self.ts,
            "signal_updated_at": _iso(self.mtime),
        }


def _apply_live_iterm_state(
    tab: dict[str, Any],
    live_state: dict[str, Any] | None,
) -> dict[str, Any]:
    if not live_state:
        return tab
    reconciled = dict(tab)
    is_processing = bool(live_state.get("is_processing"))
    reconciled["iterm_is_processing"] = is_processing
    reconciled["iterm_window"] = live_state.get("window")
    reconciled["iterm_tab"] = live_state.get("tab")
    reconciled["iterm_name"] = live_state.get("name")
    if is_processing and reconciled.get("state") != "running":
        reconciled["signal_state"] = reconciled.get("state")
        reconciled["state"] = "running"
        reconciled["state_source"] = "iterm_processing"
    else:
        reconciled["state_source"] = "signal"
    return reconciled


def read_signal_records(
    signal_dir: Path,
    *,
    now_ts: float | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> list[SignalRecord]:
    records: list[SignalRecord] = []
    if not signal_dir.is_dir():
        return records
    now_ts = _now() if now_ts is None else now_ts
    for path in sorted(signal_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stat = path.stat()
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        session_id = str(payload.get("session_id") or path.stem)
        tty = str(payload.get("tty") or "").strip()
        pid = _parse_int(payload.get("pid"))
        ts = _parse_int(payload.get("ts"), int(stat.st_mtime))
        if not tty:
            continue
        records.append(
            SignalRecord(
                path=path,
                session_id=session_id,
                state=_normalize_state(payload.get("type")),
                tty=tty,
                pid=pid,
                ts=ts,
                mtime=stat.st_mtime,
                runtime=str(payload.get("runtime") or "claude"),
                cwd=str(payload.get("cwd") or ""),
                project=str(payload.get("project") or ""),
                message=str(payload.get("message") or ""),
                live_pid=pid_alive(pid),
            )
        )
    return records


def build_current_state(
    signal_dir: Path,
    *,
    now_ts: float | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
    live_iterm_states: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now_ts = _now() if now_ts is None else now_ts
    records = read_signal_records(signal_dir, now_ts=now_ts, pid_alive=pid_alive)
    live_by_tty: dict[str, list[SignalRecord]] = {}
    stale_count = 0
    invalid_pid_count = 0

    for record in records:
        if record.live_pid:
            live_by_tty.setdefault(record.tty, []).append(record)
        else:
            stale_count += 1
            invalid_pid_count += 1

    tabs: list[dict[str, Any]] = []
    duplicate_count = 0
    for tty, tty_records in sorted(live_by_tty.items()):
        tty_records.sort(key=lambda rec: rec.sort_key, reverse=True)
        selected = tty_records[0]
        duplicate_count += max(0, len(tty_records) - 1)
        tab = selected.to_tab(now_ts=now_ts)
        tabs.append(_apply_live_iterm_state(tab, (live_iterm_states or {}).get(tty)))

    counts_by_state: dict[str, int] = {
        state: 0 for state in sorted(VALID_STATES | {"unknown"})
    }
    for tab in tabs:
        state = str(tab["state"])
        counts_by_state[state] = counts_by_state.get(state, 0) + 1

    return {
        "generated_at": _iso(now_ts),
        "generated_ts": now_ts,
        "signal_dir": str(signal_dir),
        "summary": {
            "active_tabs": len(tabs),
            "signal_files": len(records),
            "duplicate_signals_ignored": duplicate_count,
            "stale_signals_ignored": stale_count,
            "dead_pid_signals": invalid_pid_count,
            "counts_by_state": counts_by_state,
        },
        "tabs": tabs,
    }


def _load_previous(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def transition_events(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> list[dict[str, Any]]:
    prev_tabs = {
        str(tab.get("tty")): tab
        for tab in (previous or {}).get("tabs", [])
        if isinstance(tab, dict) and tab.get("tty")
    }
    events: list[dict[str, Any]] = []
    for tab in current.get("tabs", []):
        if not isinstance(tab, dict):
            continue
        tty = str(tab.get("tty") or "")
        if not tty:
            continue
        prev = prev_tabs.get(tty)
        prev_state = str(prev.get("state")) if prev else None
        prev_session = str(prev.get("session_id")) if prev else None
        next_state = str(tab.get("state"))
        next_session = str(tab.get("session_id"))
        if prev_state != next_state or prev_session != next_session:
            events.append(
                {
                    "ts": current.get("generated_at"),
                    "tty": tty,
                    "event": "tab_state_changed" if prev else "tab_seen",
                    "previous_state": prev_state,
                    "state": next_state,
                    "previous_session_id": prev_session,
                    "session_id": next_session,
                    "runtime": tab.get("runtime"),
                    "cwd": tab.get("cwd"),
                    "project": tab.get("project"),
                }
            )
    for tty, prev in prev_tabs.items():
        if not any(
            isinstance(tab, dict) and tab.get("tty") == tty
            for tab in current.get("tabs", [])
        ):
            events.append(
                {
                    "ts": current.get("generated_at"),
                    "tty": tty,
                    "event": "tab_gone",
                    "previous_state": prev.get("state"),
                    "state": None,
                    "previous_session_id": prev.get("session_id"),
                    "session_id": None,
                    "runtime": prev.get("runtime"),
                    "cwd": prev.get("cwd"),
                    "project": prev.get("project"),
                }
            )
    return events


def write_outputs(
    current: dict[str, Any],
    *,
    current_path: Path,
    events_path: Path,
    previous: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    current_path.parent.mkdir(parents=True, exist_ok=True)
    previous = _load_previous(current_path) if previous is None else previous
    events = transition_events(previous, current)
    tmp = current_path.with_suffix(current_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(current_path)
    if events:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write COS-readable iTerm tab state.")
    parser.add_argument("--signal-dir", type=Path, default=default_signal_dir())
    parser.add_argument("--report-dir", type=Path, default=default_report_dir())
    parser.add_argument("--current-name", default=DEFAULT_CURRENT_NAME)
    parser.add_argument("--events-name", default=DEFAULT_EVENTS_NAME)
    parser.add_argument("--print", action="store_true", dest="print_json")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument(
        "--no-iterm-live",
        action="store_true",
        help="Do not reconcile signal files with live iTerm `is processing` state.",
    )
    args = parser.parse_args(argv)

    live_iterm_states = {} if args.no_iterm_live else read_live_iterm_states()
    current = build_current_state(args.signal_dir, live_iterm_states=live_iterm_states)
    events: list[dict[str, Any]] = []
    if not args.no_write:
        events = write_outputs(
            current,
            current_path=args.report_dir / args.current_name,
            events_path=args.report_dir / args.events_name,
        )
    if args.print_json:
        payload = dict(current)
        payload["events_written"] = len(events)
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
