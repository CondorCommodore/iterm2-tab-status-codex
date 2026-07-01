#!/usr/bin/env python3
"""Continuous COS control-plane file daemon.

This daemon deliberately does not dispatch work. It keeps tab state, report
events, and dashboard snapshots fresh so the COS tab can make decisions cheaply.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cos_dashboard
import cos_report_watcher
import cos_tab_state_monitor

DEFAULT_REPORT_DIR = Path.home() / ".claude" / "plans" / "fleet-reports"


def tick(*, signal_dir: Path, report_dir: Path) -> dict[str, object]:
    current_path = report_dir / cos_tab_state_monitor.DEFAULT_CURRENT_NAME
    events_path = report_dir / cos_tab_state_monitor.DEFAULT_EVENTS_NAME
    tab_state = cos_tab_state_monitor.build_current_state(signal_dir)
    tab_events = cos_tab_state_monitor.write_outputs(
        tab_state,
        current_path=current_path,
        events_path=events_path,
    )
    report_events = cos_report_watcher.watch_once(
        report_dir=report_dir,
        state_path=report_dir / ".cos-report-watcher-state.json",
        event_log=report_dir / "cos-report-events.jsonl",
        seed_if_missing=True,
    )
    dashboard = cos_dashboard.build_dashboard(
        state_path=current_path,
        iterm_live_state_path=current_path,
        report_dir=report_dir,
    )
    dashboard_path = report_dir / "cos-dashboard-current.json"
    dashboard_path.write_text(
        json.dumps(dashboard, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "tab_events": len(tab_events),
        "report_events": len(report_events),
        "dashboard_path": str(dashboard_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the COS control-plane file daemon.")
    parser.add_argument(
        "--signal-dir", type=Path, default=cos_tab_state_monitor.default_signal_dir()
    )
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true", dest="print_json")
    args = parser.parse_args(argv)

    while True:
        result = tick(signal_dir=args.signal_dir, report_dir=args.report_dir)
        if args.print_json:
            print(json.dumps(result, sort_keys=True))
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
