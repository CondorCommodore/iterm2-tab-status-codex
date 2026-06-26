#!/usr/bin/env python3
"""Optional iTerm2 COS overlay.

Reads ``tab-state-current.json`` from the COS monitor and mirrors each tab's
state into iTerm2 user variables. It intentionally never sends text to sessions.
Run from iTerm2's Python environment if you want profile subtitles/status bars
to consume the variables.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_STATE_PATH = Path.home() / ".claude" / "plans" / "fleet-reports" / "tab-state-current.json"
DEFAULT_REPORT_DIR = Path.home() / ".claude" / "plans" / "fleet-reports"
POLL_INTERVAL_SECONDS = float(os.environ.get("COS_ITERM_OVERLAY_INTERVAL", "2.0"))
TTY_NAME_RE = re.compile(r"ttys[0-9]+")

VARIABLE_MAP = {
    "role": "user.cosRole",
    "state": "user.workerState",
    "goal": "user.workerGoal",
    "report": "user.lastFleetReport",
}


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"tabs": []}
    return payload if isinstance(payload, dict) else {"tabs": []}


def _cos_ttys_from_env() -> set[str]:
    raw = os.environ.get("COS_TTYS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _tty_short(tty: str) -> str:
    return tty.rsplit("/", 1)[-1]


def latest_report_by_tty(report_dir: Path = DEFAULT_REPORT_DIR) -> dict[str, str]:
    if not report_dir.is_dir():
        return {}
    latest: dict[str, tuple[float, str]] = {}
    for path in report_dir.glob("*.md"):
        short_name = path.name
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        for match in TTY_NAME_RE.finditer(short_name):
            tty = match.group(0)
            prev = latest.get(tty)
            if prev is None or mtime > prev[0]:
                latest[tty] = (mtime, path.name)
    return {tty: name for tty, (_mtime, name) in latest.items()}


def tab_variables(
    tab: dict[str, Any],
    *,
    cos_ttys: set[str] | None = None,
    reports_by_tty: dict[str, str] | None = None,
) -> dict[str, str]:
    runtime = str(tab.get("runtime") or "unknown")
    project = str(tab.get("project") or "")
    cwd = str(tab.get("cwd") or "")
    state = str(tab.get("state") or "unknown")
    tty = str(tab.get("tty") or "")
    cos_ttys = set() if cos_ttys is None else cos_ttys
    reports_by_tty = {} if reports_by_tty is None else reports_by_tty
    role = "cos" if tty in cos_ttys else "worker"
    return {
        VARIABLE_MAP["role"]: role,
        VARIABLE_MAP["state"]: state,
        VARIABLE_MAP["goal"]: project,
        VARIABLE_MAP["report"]: reports_by_tty.get(_tty_short(tty), ""),
        "user.workerRuntime": runtime,
        "user.workerCwd": cwd,
    }


async def apply_overlay(connection: object, state_path: Path = DEFAULT_STATE_PATH) -> None:
    import iterm2

    app = await iterm2.async_get_app(connection)
    state = load_state(state_path)
    by_tty = {
        str(tab.get("tty")): tab
        for tab in state.get("tabs", [])
        if isinstance(tab, dict) and tab.get("tty")
    }
    reports_by_tty = latest_report_by_tty(
        Path(os.environ.get("COS_FLEET_REPORT_DIR", str(DEFAULT_REPORT_DIR)))
    )
    for window in app.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                tty = await session.async_get_variable("tty")
                if not tty or tty not in by_tty:
                    continue
                for key, value in tab_variables(
                    by_tty[tty],
                    cos_ttys=_cos_ttys_from_env(),
                    reports_by_tty=reports_by_tty,
                ).items():
                    await session.async_set_variable(key, value)


async def watch_overlay(connection: object, state_path: Path = DEFAULT_STATE_PATH) -> None:
    while True:
        try:
            await apply_overlay(connection, state_path)
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    try:
        import iterm2
    except ImportError:
        return

    state_path = Path(os.environ.get("COS_TAB_STATE_PATH", str(DEFAULT_STATE_PATH)))
    iterm2.run_forever(lambda connection: watch_overlay(connection, state_path))


if __name__ == "__main__":
    main()
