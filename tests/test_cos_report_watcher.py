from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_report_watcher as watcher  # noqa: E402


def test_watch_once_emits_created_event(tmp_path):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    report = report_dir / "tab.md"
    report.write_text("DONE PR #7", encoding="utf-8")
    state_path = tmp_path / "state.json"
    event_log = tmp_path / "events.jsonl"

    events = watcher.watch_once(
        report_dir=report_dir,
        state_path=state_path,
        event_log=event_log,
    )

    assert events[0]["event"] == "report_created"
    assert events[0]["report"]["prs"] == ["7"]
    assert event_log.exists()
    assert (
        json.loads(event_log.read_text(encoding="utf-8").splitlines()[0])["event"]
        == "report_created"
    )


def test_watch_once_can_seed_without_initial_flood(tmp_path):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "old.md").write_text("DONE", encoding="utf-8")
    state_path = tmp_path / "state.json"
    event_log = tmp_path / "events.jsonl"

    events = watcher.watch_once(
        report_dir=report_dir,
        state_path=state_path,
        event_log=event_log,
        seed_if_missing=True,
    )

    assert events == []
    assert state_path.exists()
    assert not event_log.exists()


def test_diff_snapshots_detects_change_and_delete():
    events = watcher.diff_snapshots(
        {"a": (1.0, 1), "b": (1.0, 1)},
        {"a": (2.0, 1), "c": (1.0, 1)},
    )

    assert events == [
        {"event": "report_changed", "path": "a"},
        {"event": "report_created", "path": "c"},
        {"event": "report_deleted", "path": "b"},
    ]
