#!/usr/bin/env python3
"""Read back COS iTerm2 user variables from live sessions.

This is a diagnostic companion to ``cos_iterm_overlay.py``. It uses the iTerm2
Python API when run from iTerm2's script runtime and prints JSON for COS.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

VARIABLES = (
    "tty",
    "user.cosRole",
    "user.workerState",
    "user.workerReadiness",
    "user.workerGoal",
    "user.lastFleetReport",
    "user.workerRuntime",
    "user.workerCwd",
)


async def session_snapshot(
    session: object,
    variables: tuple[str, ...] = VARIABLES,
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in variables:
        try:
            value = await session.async_get_variable(name)  # type: ignore[attr-defined]
        except Exception:
            value = ""
        snapshot[name] = "" if value is None else str(value)
    return snapshot


async def collect_readback(connection: object) -> dict[str, Any]:
    import iterm2

    app = await iterm2.async_get_app(connection)
    sessions: list[dict[str, str]] = []
    for window in app.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                sessions.append(await session_snapshot(session))
    return {
        "session_count": len(sessions),
        "sessions": sessions,
    }


async def print_readback(connection: object) -> None:
    print(json.dumps(await collect_readback(connection), indent=2, sort_keys=True))


def build_arg_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Print live iTerm2 COS user-variable readback as JSON."
    )


def main() -> int:
    build_arg_parser().parse_args()
    try:
        import iterm2
    except ImportError:
        print(
            json.dumps(
                {
                    "error": (
                        "iterm2 module unavailable; "
                        "run inside iTerm2's Python runtime"
                    ),
                    "session_count": 0,
                    "sessions": [],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    iterm2.run_until_complete(print_readback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
