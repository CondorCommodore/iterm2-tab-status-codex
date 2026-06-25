from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_control_daemon as daemon  # noqa: E402


def test_tick_writes_dashboard_with_empty_signal_dir(tmp_path):
    signal_dir = tmp_path / "signals"
    report_dir = tmp_path / "reports"
    signal_dir.mkdir()
    report_dir.mkdir()
    (report_dir / "worker.md").write_text("DONE PR #1", encoding="utf-8")

    result = daemon.tick(signal_dir=signal_dir, report_dir=report_dir)

    dashboard_path = Path(str(result["dashboard_path"]))
    assert dashboard_path.exists()
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    assert dashboard["tabs"]["active_tabs"] == 0
    assert dashboard["reports"][0]["prs"] == ["1"]
