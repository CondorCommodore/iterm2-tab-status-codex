#!/usr/bin/env python3
"""Event-oriented COS iTerm2 daemon.

Run from iTerm2's Python runtime. The daemon never focuses tabs and never sends
input. It keeps a COS-readable live session snapshot fresh, classifies worker
readiness from prompt/screen state, and mirrors compact metadata into iTerm2
user variables for status bars/subtitles.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

DEFAULT_REPORT_DIR = Path.home() / ".claude" / "plans" / "fleet-reports"
DEFAULT_LIVE_STATE_NAME = "iterm-live-state.json"
DEFAULT_EVENTS_NAME = "iterm-live-events.jsonl"
DEFAULT_INTERVAL_SECONDS = float(os.environ.get("COS_ITERM_DAEMON_INTERVAL", "1.0"))
DEFAULT_SCREEN_TAIL_LINES = int(os.environ.get("COS_ITERM_DAEMON_SCREEN_TAIL_LINES", "40"))
TTY_RE = re.compile(r"^/dev/ttys\d+$")

VARIABLE_NAMES = (
    "tty",
    "session.name",
    "session.title",
    "path",
    "user.cosRole",
    "user.workerState",
    "user.workerReadiness",
    "user.workerGoal",
    "user.lastFleetReport",
    "user.workerRuntime",
    "user.workerCwd",
)

READY_PATTERNS = (
    re.compile(r"^\s*[›>$#]\s*$", re.MULTILINE),
    re.compile(r"\bready\b", re.IGNORECASE),
)
RUNNING_PATTERNS = (
    re.compile(r"\bworking\b", re.IGNORECASE),
    re.compile(r"\bes[ck]\s+to\s+interrupt\b", re.IGNORECASE),
    re.compile(r"\brunning\b", re.IGNORECASE),
    re.compile(r"\bprocessing\b", re.IGNORECASE),
)
QUEUE_PATTERNS = (
    re.compile(r"\btab\s+to\s+queue\b", re.IGNORECASE),
    re.compile(r"\bqueued\b", re.IGNORECASE),
)
NEEDS_INPUT_PATTERNS = (
    re.compile(r"\bpermission\b", re.IGNORECASE),
    re.compile(r"\bapprove\b", re.IGNORECASE),
    re.compile(r"\bconfirm\b", re.IGNORECASE),
    re.compile(r"\bpress\s+enter\b", re.IGNORECASE),
    re.compile(r"\bwaiting\s+for\s+(input|approval)\b", re.IGNORECASE),
)


def utc_now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(utc_now() if ts is None else ts),
    )


def compact_text(text: str, *, limit: int = 160) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def classify_runtime(label: str, cwd: str, text: str) -> str:
    haystack = "\n".join([label, cwd, text]).lower()
    if "claude" in haystack:
        return "claude"
    if "codex" in haystack:
        return "codex"
    if "ssh " in haystack or "@" in label:
        return "ssh"
    return "shell"


def classify_readiness(
    *,
    text: str,
    is_processing: bool | None = None,
    prompt_state: str | None = None,
) -> str:
    prompt = (prompt_state or "").strip().lower()
    if prompt in {"prompt", "ready", "idle"}:
        return "ready"
    if prompt in {"running", "processing", "busy"}:
        return "running"
    if any(pattern.search(text) for pattern in NEEDS_INPUT_PATTERNS):
        return "needs_input"
    if any(pattern.search(text) for pattern in QUEUE_PATTERNS):
        return "queued"
    if is_processing is True:
        return "running"
    if any(pattern.search(text) for pattern in RUNNING_PATTERNS):
        return "running"
    if is_processing is False and any(pattern.search(text) for pattern in READY_PATTERNS):
        return "ready"
    if is_processing is False:
        return "idle"
    return "unknown"


def latest_report_by_tty(report_dir: Path = DEFAULT_REPORT_DIR) -> dict[str, str]:
    if not report_dir.is_dir():
        return {}
    latest: dict[str, tuple[float, str]] = {}
    for path in report_dir.glob("*.md"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        for match in re.finditer(r"ttys\d+", path.name):
            tty = match.group(0)
            previous = latest.get(tty)
            if previous is None or mtime > previous[0]:
                latest[tty] = (mtime, path.name)
    return {tty: name for tty, (_mtime, name) in latest.items()}


def cos_ttys_from_env() -> set[str]:
    raw = os.environ.get("COS_TTYS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def tty_short(tty: str) -> str:
    return tty.rsplit("/", 1)[-1]


def role_for_tty(tty: str, cos_ttys: set[str]) -> str:
    return "cos" if tty in cos_ttys or tty_short(tty) in cos_ttys else "worker"


def screen_to_text(screen: Any, *, tail_lines: int = DEFAULT_SCREEN_TAIL_LINES) -> str:
    if screen is None:
        return ""
    if isinstance(screen, str):
        return "\n".join(screen.splitlines()[-tail_lines:])
    lines: list[str] = []
    if hasattr(screen, "number_of_lines") and hasattr(screen, "line"):
        try:
            count = int(screen.number_of_lines)
            start = max(0, count - tail_lines)
            for idx in range(start, count):
                line = screen.line(idx)
                lines.append(str(getattr(line, "string", line)))
            return "\n".join(lines)
        except Exception:
            pass
    raw_lines = getattr(screen, "lines", None)
    if raw_lines is not None:
        try:
            return "\n".join(
                str(getattr(line, "string", line)) for line in list(raw_lines)[-tail_lines:]
            )
        except Exception:
            pass
    return compact_text(str(screen), limit=4000)


@dataclass(frozen=True)
class SessionRecord:
    window_index: int
    tab_index: int
    session_index: int
    tty: str
    title: str
    cwd: str
    runtime: str
    readiness: str
    role: str
    screen_tail: str
    last_fleet_report: str

    def to_json(self) -> dict[str, Any]:
        return {
            "window_index": self.window_index,
            "tab_index": self.tab_index,
            "session_index": self.session_index,
            "tty": self.tty,
            "title": self.title,
            "cwd": self.cwd,
            "runtime": self.runtime,
            "readiness": self.readiness,
            "role": self.role,
            "screen_tail": compact_text(self.screen_tail, limit=500),
            "last_fleet_report": self.last_fleet_report,
        }


async def _get_variable(session: Any, name: str) -> str:
    try:
        value = await session.async_get_variable(name)
    except Exception:
        return ""
    return "" if value is None else str(value)


async def _get_processing(session: Any) -> bool | None:
    try:
        value = await session.async_get_variable("session.isProcessing")
        if isinstance(value, bool):
            return value
        if str(value).lower() in {"1", "true", "yes"}:
            return True
        if str(value).lower() in {"0", "false", "no"}:
            return False
    except Exception:
        pass
    return None


async def _get_screen_text(session: Any) -> str:
    for method_name in ("async_get_screen_contents", "async_get_contents"):
        method = getattr(session, method_name, None)
        if method is None:
            continue
        try:
            return screen_to_text(await method())
        except Exception:
            continue
    return ""


async def read_session_record(
    session: Any,
    *,
    window_index: int,
    tab_index: int,
    session_index: int,
    reports_by_tty: dict[str, str] | None = None,
    cos_ttys: set[str] | None = None,
) -> SessionRecord:
    reports_by_tty = {} if reports_by_tty is None else reports_by_tty
    cos_ttys = set() if cos_ttys is None else cos_ttys
    tty = await _get_variable(session, "tty")
    title = await _get_variable(session, "session.title") or await _get_variable(
        session, "session.name"
    )
    cwd = await _get_variable(session, "path")
    screen_tail = await _get_screen_text(session)
    is_processing = await _get_processing(session)
    runtime = classify_runtime(title, cwd, screen_tail)
    readiness = classify_readiness(text=screen_tail, is_processing=is_processing)
    role = role_for_tty(tty, cos_ttys)
    return SessionRecord(
        window_index=window_index,
        tab_index=tab_index,
        session_index=session_index,
        tty=tty,
        title=title,
        cwd=cwd,
        runtime=runtime,
        readiness=readiness,
        role=role,
        screen_tail=screen_tail,
        last_fleet_report=reports_by_tty.get(tty_short(tty), ""),
    )


def summarize(records: list[SessionRecord]) -> dict[str, Any]:
    by_readiness: dict[str, int] = {}
    by_runtime: dict[str, int] = {}
    for record in records:
        by_readiness[record.readiness] = by_readiness.get(record.readiness, 0) + 1
        by_runtime[record.runtime] = by_runtime.get(record.runtime, 0) + 1
    return {
        "session_count": len(records),
        "by_readiness": by_readiness,
        "by_runtime": by_runtime,
    }


def transition_events(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> list[dict[str, Any]]:
    previous_by_tty = {
        str(item.get("tty")): item
        for item in (previous or {}).get("sessions", [])
        if isinstance(item, dict) and item.get("tty")
    }
    events: list[dict[str, Any]] = []
    for item in current.get("sessions", []):
        if not isinstance(item, dict) or not item.get("tty"):
            continue
        tty = str(item["tty"])
        prev = previous_by_tty.get(tty)
        if prev is None:
            events.append(
                {
                    "ts": current["generated_at"],
                    "event": "session_seen",
                    "tty": tty,
                    "readiness": item.get("readiness"),
                }
            )
            continue
        if prev.get("readiness") != item.get("readiness") or prev.get("runtime") != item.get(
            "runtime"
        ):
            events.append(
                {
                    "ts": current["generated_at"],
                    "event": "session_changed",
                    "tty": tty,
                    "previous_readiness": prev.get("readiness"),
                    "readiness": item.get("readiness"),
                    "previous_runtime": prev.get("runtime"),
                    "runtime": item.get("runtime"),
                }
            )
    current_ttys = {
        str(item.get("tty"))
        for item in current.get("sessions", [])
        if isinstance(item, dict) and item.get("tty")
    }
    for tty, prev in previous_by_tty.items():
        if tty not in current_ttys:
            events.append(
                {
                    "ts": current["generated_at"],
                    "event": "session_gone",
                    "tty": tty,
                    "previous_readiness": prev.get("readiness"),
                    "readiness": None,
                }
            )
    return events


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def write_state(
    records: list[SessionRecord],
    *,
    state_path: Path,
    events_path: Path,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    current = {
        "generated_at": iso(now),
        "generated_ts": now,
        "source": "iterm2-python-api",
        "summary": summarize(records),
        "sessions": [record.to_json() for record in records if TTY_RE.match(record.tty)],
    }
    previous = load_json(state_path) if previous is None else previous
    events = transition_events(previous, current)
    state_path.write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if events:
        with events_path.open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
    return current


async def set_session_variables(session: Any, record: SessionRecord) -> None:
    values = {
        "user.cosRole": record.role,
        "user.workerState": record.readiness,
        "user.workerReadiness": record.readiness,
        "user.workerGoal": Path(record.cwd).name if record.cwd else "",
        "user.lastFleetReport": record.last_fleet_report,
        "user.workerRuntime": record.runtime,
        "user.workerCwd": record.cwd,
    }
    for key, value in values.items():
        try:
            await session.async_set_variable(key, value)
        except Exception:
            continue


async def collect_records(
    connection: Any,
    *,
    report_dir: Path = DEFAULT_REPORT_DIR,
) -> list[SessionRecord]:
    import iterm2

    app = await iterm2.async_get_app(connection)
    records: list[SessionRecord] = []
    reports_by_tty = latest_report_by_tty(report_dir)
    cos_ttys = cos_ttys_from_env()
    for window_index, window in enumerate(app.terminal_windows, start=1):
        for tab_index, tab in enumerate(window.tabs, start=1):
            for session_index, session in enumerate(tab.sessions, start=1):
                record = await read_session_record(
                    session,
                    window_index=window_index,
                    tab_index=tab_index,
                    session_index=session_index,
                    reports_by_tty=reports_by_tty,
                    cos_ttys=cos_ttys,
                )
                records.append(record)
                await set_session_variables(session, record)
    return records


async def observe_layout_events(
    connection: Any,
    refresh: Callable[[], Awaitable[None]],
) -> None:
    """Attach iTerm lifecycle monitors when available.

    The daemon still has a timed refresh fallback. Monitors just cut latency
    when iTerm supports them; failing to attach them must not kill the daemon.
    """
    try:
        import iterm2
    except ImportError:
        return
    monitors = []
    for monitor_name in (
        "LayoutChangeMonitor",
        "NewSessionMonitor",
        "SessionTerminationMonitor",
    ):
        monitor_type = getattr(iterm2, monitor_name, None)
        if monitor_type is None:
            continue
        try:
            monitors.append(monitor_type(connection))
        except Exception:
            continue
    if not monitors:
        return
    while True:
        try:
            tasks = [asyncio.create_task(monitor.async_get()) for monitor in monitors]
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
            await refresh()
        except Exception:
            await asyncio.sleep(DEFAULT_INTERVAL_SECONDS)


async def run_daemon(
    connection: Any,
    *,
    report_dir: Path = DEFAULT_REPORT_DIR,
    interval: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    state_path = report_dir / DEFAULT_LIVE_STATE_NAME
    events_path = report_dir / DEFAULT_EVENTS_NAME

    async def refresh() -> None:
        records = await collect_records(connection, report_dir=report_dir)
        write_state(records, state_path=state_path, events_path=events_path)

    asyncio.create_task(observe_layout_events(connection, refresh))
    while True:
        try:
            await refresh()
        except Exception:
            pass
        await asyncio.sleep(interval)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the COS iTerm2 Python API daemon.")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--once",
        action="store_true",
        help="collect once; only useful under iTerm2 Python",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        import iterm2
    except ImportError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": ("iterm2 module unavailable; run inside iTerm2's Python runtime"),
                },
                sort_keys=True,
            )
        )
        return 2

    async def entry(connection: Any) -> None:
        if args.once:
            records = await collect_records(connection, report_dir=args.report_dir)
            state = write_state(
                records,
                state_path=args.report_dir / DEFAULT_LIVE_STATE_NAME,
                events_path=args.report_dir / DEFAULT_EVENTS_NAME,
            )
            print(json.dumps(state, indent=2, sort_keys=True))
            return
        await run_daemon(connection, report_dir=args.report_dir, interval=args.interval)

    if args.once:
        iterm2.run_until_complete(entry)
    else:
        iterm2.run_forever(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
