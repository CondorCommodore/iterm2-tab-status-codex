#!/usr/bin/env python3
# ruff: noqa: I001
"""Fast local dry-run harness for COS tab/report/dispatch logic."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

import cos_dashboard
import cos_dispatch_orchestrator
import cos_report_watcher


DEFAULT_SCENARIO = {
    "tabs": [
        {
            "tty": "/dev/ttys001",
            "state": "idle",
            "runtime": "codex",
            "role": "cos",
            "project": "code",
        },
        {
            "tty": "/dev/ttys003",
            "state": "idle",
            "runtime": "codex",
            "role": "worker",
            "project": "home-lab",
        },
        {
            "tty": "/dev/ttys004",
            "state": "running",
            "runtime": "codex",
            "role": "worker",
            "project": "forge",
        },
    ],
    "reports": {
        "worker-ttys003.md": "DONE PR #123\n\n## Next Step\nReady for next task.",
        "worker-ttys004.md": "RUNNING task T-3345",
    },
    "goal": "inspect next actionable task and report",
    "cos_tty": "/dev/ttys001",
}


def load_scenario(path: Path | None) -> dict[str, Any]:
    if path is None:
        return dict(DEFAULT_SCENARIO)
    return json.loads(path.read_text(encoding="utf-8"))


def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="cos-dry-run-") as tmp_s:
        tmp = Path(tmp_s)
        state_path = tmp / "tab-state-current.json"
        report_dir = tmp / "reports"
        report_dir.mkdir()
        state_path.write_text(
            json.dumps({"generated_at": "dry-run", "tabs": scenario.get("tabs", [])}),
            encoding="utf-8",
        )
        for name, text in dict(scenario.get("reports", {})).items():
            (report_dir / name).write_text(str(text), encoding="utf-8")
        watcher_events = cos_report_watcher.watch_once(
            report_dir=report_dir,
            state_path=tmp / "watcher-state.json",
            event_log=tmp / "events.jsonl",
        )
        dashboard = cos_dashboard.build_dashboard(
            state_path=state_path,
            iterm_live_state_path=state_path,
            report_dir=report_dir,
        )
        plan = cos_dispatch_orchestrator.build_dispatch_plan(
            goal=str(scenario.get("goal") or DEFAULT_SCENARIO["goal"]),
            state_path=state_path,
            report_dir=report_dir,
            target_host=str(scenario.get("target_host") or ""),
            cos_tty=str(scenario.get("cos_tty") or ""),
        )
        return {
            "dashboard": dashboard,
            "dispatch_plan": plan.__dict__,
            "watcher_events": watcher_events,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a local COS control-plane dry-run.")
    parser.add_argument("--scenario", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(run_scenario(load_scenario(args.scenario)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
