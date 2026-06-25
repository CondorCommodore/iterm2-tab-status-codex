from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_dispatch_orchestrator as orchestrator  # noqa: E402


def test_build_dispatch_plan_selects_idle_worker(tmp_path):
    state_path = tmp_path / "state.json"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "tabs": [
                    {"tty": "/dev/ttys001", "state": "idle", "role": "cos"},
                    {"tty": "/dev/ttys002", "state": "idle", "role": "worker"},
                ]
            }
        ),
        encoding="utf-8",
    )

    plan = orchestrator.build_dispatch_plan(
        goal="do useful work",
        state_path=state_path,
        report_dir=report_dir,
        cos_tty="/dev/ttys001",
    )

    assert plan.ok is True
    assert plan.tty == "/dev/ttys002"
    assert plan.text == "/goal do useful work"
    assert plan.dry_run_payload == "'/goal do useful work\\n'"


def test_build_dispatch_plan_reports_no_worker(tmp_path):
    state_path = tmp_path / "state.json"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    state_path.write_text(json.dumps({"tabs": []}), encoding="utf-8")

    plan = orchestrator.build_dispatch_plan(
        goal="/goal work",
        state_path=state_path,
        report_dir=report_dir,
    )

    assert plan.ok is False
    assert plan.reason == "no eligible worker tab"
