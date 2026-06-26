#!/usr/bin/env python3
"""Safe iTerm trigger sink for COS tab events.

iTerm triggers can invoke this script with matched terminal output. The script
classifies the line and appends a structured event. It never sends text back to
the terminal and is safe to use from alert-only trigger actions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_EVENTS_PATH = (
    Path.home() / ".claude" / "plans" / "fleet-reports" / "tab-state-events.jsonl"
)

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("done", re.compile(r"\b(DONE|complete|completed)\b", re.IGNORECASE)),
    ("blocked", re.compile(r"\b(BLOCKED|blocked|blocker)\b")),
    ("approve", re.compile(r"\bAPPROVE\b")),
    ("reject", re.compile(r"\bREJECT\b")),
    ("report", re.compile(r"\bReport:\s+|\bfleet-reports/")),
    ("traceback", re.compile(r"\bTraceback\b")),
    ("failed", re.compile(r"\bFAILED\b|\bfailed\b")),
    ("rate_limit", re.compile(r"\brate limit\b|quota|403|429", re.IGNORECASE)),
    ("merge_conflict", re.compile(r"\bmerge conflict\b|CONFLICT \(", re.IGNORECASE)),
]


def classify_line(line: str) -> str | None:
    for label, pattern in PATTERNS:
        if pattern.search(line):
            return label
    return None


def build_event(
    line: str,
    *,
    tty: str = "",
    cwd: str = "",
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    label = classify_line(line)
    if label is None:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        "event": "trigger_match",
        "trigger": label,
        "tty": tty,
        "cwd": cwd,
        "line": line.rstrip("\n")[:500],
    }


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append safe COS trigger event.")
    parser.add_argument("--events-path", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--tty", default=os.environ.get("TTY", ""))
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("line", nargs="*")
    args = parser.parse_args(argv)
    line = " ".join(args.line) if args.line else sys.stdin.read()
    event = build_event(line, tty=args.tty, cwd=args.cwd)
    if event is None:
        return 0
    append_event(args.events_path, event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
