#!/usr/bin/env python3
"""Parse COS fleet reports into compact structured status records."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_REPORT_DIR = Path.home() / ".claude" / "plans" / "fleet-reports"

PR_RE = re.compile(r"(?:PR|pull request)\s*#?(\d+)", re.IGNORECASE)
SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
TASK_RE = re.compile(r"\bT-\d+\b|\bTASK\s*#?\d+\b", re.IGNORECASE)
TTY_RE = re.compile(r"\bttys\d+\b")


@dataclass(frozen=True)
class FleetReport:
    path: str
    name: str
    mtime: float
    status: str
    summary: str
    prs: list[str]
    tasks: list[str]
    shas: list[str]
    tty: str
    needs_decision: bool
    blocker: str
    next_step: str


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip(" #\t")
        if stripped:
            return stripped[:240]
    return ""


def _section_line(text: str, names: Iterable[str]) -> str:
    names_lower = tuple(name.lower() for name in names)
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip(" #\t").lower().rstrip(":")
        if stripped in names_lower:
            for next_line in lines[idx + 1 : idx + 6]:
                candidate = next_line.strip(" -\t")
                if candidate:
                    return candidate[:240]
    return ""


def classify_status(text: str) -> str:
    lowered = text.lower()
    if re.search(r"\breject(?:ed)?\b", lowered):
        return "rejected"
    if re.search(r"\bblocked\b|\bblocking\b", lowered):
        return "blocked"
    if re.search(r"\bapprove(?:d)?\b", lowered):
        return "approved"
    if re.search(r"\bmerged\b|\blanded\b|\bcomplete(?:d)?\b|\bdone\b", lowered):
        return "complete"
    if re.search(r"\brunning\b|\bin[-_ ]progress\b|\bworking\b", lowered):
        return "running"
    return "unknown"


def parse_report(path: Path) -> FleetReport:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        stat = path.stat()
    except OSError:
        text = ""
        stat = path.stat()
    tty_match = TTY_RE.search(path.name)
    blocker = _section_line(text, ("blocker", "blockers", "blocked", "issue", "issues"))
    next_step = _section_line(text, ("next step", "next steps", "remaining", "todo"))
    return FleetReport(
        path=str(path),
        name=path.name,
        mtime=stat.st_mtime,
        status=classify_status(text),
        summary=_first_nonempty_line(text),
        prs=sorted(set(PR_RE.findall(text)), key=int),
        tasks=sorted(
            set(
                match.upper().replace("TASK", "TASK ")
                for match in TASK_RE.findall(text)
            )
        ),
        shas=sorted(set(SHA_RE.findall(text))),
        tty=tty_match.group(0) if tty_match else "",
        needs_decision=bool(
            re.search(
                r"\b(needs? decision|operator decision|question|approve\?)\b",
                text,
                re.I,
            )
        ),
        blocker=blocker,
        next_step=next_step,
    )


def recent_reports(
    report_dir: Path = DEFAULT_REPORT_DIR,
    *,
    limit: int = 20,
) -> list[FleetReport]:
    if not report_dir.is_dir():
        return []
    paths = sorted(
        report_dir.glob("*.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [parse_report(path) for path in paths[:limit]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse recent COS fleet reports.")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    reports = [
        asdict(report)
        for report in recent_reports(args.report_dir, limit=args.limit)
    ]
    print(json.dumps({"reports": reports}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
