#!/usr/bin/env python3
"""Safely dispatch one line of text to a target iTerm2 tab by TTY.

The dispatch path uses iTerm2's Python API instead of keyboard focus, escape,
or control-key automation. By default it only accepts ``/goal ...`` commands.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any

TTY_RE = re.compile(r"^/dev/ttys[0-9A-Za-z_.-]+$")
FORBIDDEN_CONTROL_CHARS = {"\x03", "\x1b"}


@dataclass(frozen=True)
class DispatchRequest:
    tty: str
    text: str
    submit: bool = True
    require_goal: bool = True
    require_agent: bool = True


def validate_tty(tty: str) -> str:
    if not TTY_RE.match(tty):
        raise ValueError(f"unsafe tty target: {tty!r}")
    return tty


def validate_text(text: str, *, require_goal: bool = True) -> str:
    if any(char in text for char in FORBIDDEN_CONTROL_CHARS):
        raise ValueError("dispatch text must not contain Ctrl-C or Escape")
    if "\n" in text or "\r" in text:
        raise ValueError("dispatch text must be exactly one line")
    if require_goal and not text.startswith("/goal "):
        raise ValueError("dispatch text must start with '/goal '")
    if not text.strip():
        raise ValueError("dispatch text must not be empty")
    return text


def payload_for_request(request: DispatchRequest) -> str:
    validate_tty(request.tty)
    text = validate_text(request.text, require_goal=request.require_goal)
    return text + ("\n" if request.submit else "")


async def find_session_by_tty(connection: object, tty: str) -> object | None:
    import iterm2

    app = await iterm2.async_get_app(connection)
    for window in app.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                session_tty = await session.async_get_variable("tty")
                if session_tty == tty:
                    return session
    return None


async def session_variables(session: object) -> dict[str, str]:
    names = (
        "tty",
        "jobName",
        "foregroundJobName",
        "name",
        "user.workerRuntime",
        "user.cosRole",
        "user.workerState",
    )
    values: dict[str, str] = {}
    for name in names:
        try:
            value = await session.async_get_variable(name)  # type: ignore[attr-defined]
        except Exception:
            value = ""
        values[name] = "" if value is None else str(value)
    return values


def looks_like_agent_session(values: dict[str, str]) -> bool:
    haystack = " ".join(values.values()).lower()
    return "codex" in haystack or "claude" in haystack


async def dispatch(connection: object, request: DispatchRequest) -> dict[str, Any]:
    payload = payload_for_request(request)
    session = await find_session_by_tty(connection, request.tty)
    if session is None:
        return {"ok": False, "tty": request.tty, "error": "target tty not found"}
    values = await session_variables(session)
    if request.require_agent and not looks_like_agent_session(values):
        return {
            "ok": False,
            "tty": request.tty,
            "error": "target session does not look like codex/claude agent",
            "session": values,
        }
    await session.async_send_text(payload)
    return {
        "ok": True,
        "tty": request.tty,
        "bytes_sent": len(payload.encode("utf-8")),
        "submitted": request.submit,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dispatch a safe command to an iTerm2 tab.")
    parser.add_argument("--tty", required=True, help="Target TTY, for example /dev/ttys003")
    parser.add_argument("--text", required=True, help="Single command line to send")
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help="Do not append Enter/newline after the command.",
    )
    parser.add_argument(
        "--allow-non-goal",
        action="store_true",
        help="Allow commands that do not start with '/goal '.",
    )
    parser.add_argument(
        "--allow-shell-target",
        action="store_true",
        help="Do not require the target session to look like Codex or Claude.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the dispatch payload without sending.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    request = DispatchRequest(
        tty=args.tty,
        text=args.text,
        submit=not args.no_submit,
        require_goal=not args.allow_non_goal,
        require_agent=not args.allow_shell_target,
    )
    payload = payload_for_request(request)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "tty": request.tty,
                    "payload_repr": repr(payload),
                    "require_agent": request.require_agent,
                    "submitted": request.submit,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    try:
        import iterm2
    except ImportError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "tty": request.tty,
                    "error": "iterm2 module unavailable; run inside iTerm2's Python runtime",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    async def _run(connection: object) -> None:
        print(json.dumps(await dispatch(connection, request), indent=2, sort_keys=True))

    iterm2.run_until_complete(_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
