from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_dry_run_harness as harness  # noqa: E402


def test_run_scenario_builds_dashboard_and_dispatch_plan():
    result = harness.run_scenario(harness.DEFAULT_SCENARIO)

    assert result["dashboard"]["tabs"]["active_tabs"] == 3
    assert result["dispatch_plan"]["ok"] is True
    assert result["dispatch_plan"]["tty"] == "/dev/ttys003"
    assert result["watcher_events"]
