from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_dashboard as dashboard  # noqa: E402


def test_build_dashboard_summarizes_tabs_and_reports(tmp_path):
    state_path = tmp_path / "tab-state-current.json"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-25T00:00:00Z",
                "tabs": [
                    {
                        "tty": "/dev/ttys001",
                        "state": "idle",
                        "runtime": "codex",
                        "project": "home-lab",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "worker.md").write_text("APPROVE PR #42", encoding="utf-8")

    result = dashboard.build_dashboard(
        state_path=state_path,
        report_dir=report_dir,
        report_limit=5,
    )

    assert result["tabs"]["active_tabs"] == 1
    assert result["tabs"]["by_state"] == {"idle": 1}
    assert result["reports"][0]["status"] == "approved"
    assert result["recommended_actions"] == ["dispatch next /goal to /dev/ttys001"]


def test_recommended_actions_prioritizes_blockers():
    result = {
        "reports": [{"status": "blocked", "needs_decision": False}],
        "tabs": {"tabs": [{"state": "idle", "tty": "/dev/ttys001"}]},
    }

    assert dashboard.recommended_actions(result)[0] == "review blocked fleet reports"


def test_recommended_actions_does_not_dispatch_to_cos_tab():
    result = {
        "reports": [],
        "tabs": {
            "tabs": [
                {"state": "idle", "tty": "/dev/ttys001", "role": "cos"},
                {"state": "idle", "tty": "/dev/ttys002", "role": "worker"},
            ]
        },
    }

    assert dashboard.recommended_actions(result) == ["dispatch next /goal to /dev/ttys002"]
