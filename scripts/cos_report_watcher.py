#!/usr/bin/env python3
"""Watch fleet reports for new or changed files."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import cos_report_parser

DEFAULT_REPORT_DIR = Path.home() / ".claude" / "plans" / "fleet-reports"
DEFAULT_EVENT_LOG = DEFAULT_REPORT_DIR / "cos-report-events.jsonl"


def snapshot_reports(report_dir: Path = DEFAULT_REPORT_DIR) -> dict[str, tuple[float, int]]:
    if not report_dir.is_dir():
        return {}
    snapshot: dict[str, tuple[float, int]] = {}
    for path in report_dir.glob("*.md"):
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = (stat.st_mtime, stat.st_size)
    return snapshot


def diff_snapshots(
    previous: dict[str, tuple[float, int]],
    current: dict[str, tuple[float, int]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path_s, stamp in sorted(current.items()):
        if path_s not in previous:
            events.append({"event": "report_created", "path": path_s})
        elif previous[path_s] != stamp:
            events.append({"event": "report_changed", "path": path_s})
    for path_s in sorted(set(previous) - set(current)):
        events.append({"event": "report_deleted", "path": path_s})
    return events


def enrich_event(event: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(event.get("path") or ""))
    enriched = dict(event)
    if event.get("event") != "report_deleted" and path.exists():
        enriched["report"] = cos_report_parser.parse_report(path).__dict__
    return enriched


def append_events(events: list[dict[str, Any]], event_log: Path = DEFAULT_EVENT_LOG) -> None:
    if not events:
        return
    event_log.parent.mkdir(parents=True, exist_ok=True)
    with event_log.open("a", encoding="utf-8") as handle:
        for event in events:
            payload = dict(event)
            payload["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def watch_once(
    *,
    report_dir: Path = DEFAULT_REPORT_DIR,
    state_path: Path,
    event_log: Path = DEFAULT_EVENT_LOG,
    seed_if_missing: bool = False,
) -> list[dict[str, Any]]:
    previous: dict[str, tuple[float, int]] = {}
    state_existed = state_path.exists()
    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            previous = {
                str(key): (float(value[0]), int(value[1]))
                for key, value in raw.items()
                if isinstance(value, list) and len(value) == 2
            }
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            previous = {}
    current = snapshot_reports(report_dir)
    events = [] if seed_if_missing and not state_existed else [
        enrich_event(event) for event in diff_snapshots(previous, current)
    ]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_events(events, event_log=event_log)
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch fleet-report changes.")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_REPORT_DIR / ".cos-report-watcher-state.json",
    )
    parser.add_argument("--event-log", type=Path, default=DEFAULT_EVENT_LOG)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true", dest="print_json")
    args = parser.parse_args(argv)

    while True:
        events = watch_once(
            report_dir=args.report_dir,
            state_path=args.state_path,
            event_log=args.event_log,
        )
        if args.print_json and events:
            for event in events:
                print(json.dumps(event, sort_keys=True))
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
