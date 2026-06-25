from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_tab_state_monitor as monitor  # noqa: E402


def _write_signal(
    root: Path,
    name: str,
    *,
    tty: str,
    pid: int,
    state: str,
    ts: int,
    session_id: str | None = None,
) -> Path:
    path = root / name
    payload = {
        "session_id": session_id or name.removesuffix(".json"),
        "type": state,
        "tty": tty,
        "pid": str(pid),
        "ts": str(ts),
        "runtime": "codex",
        "cwd": "/Users/mikebook/code/home-lab",
        "project": "home-lab",
        "message": "",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_build_current_state_dedupes_by_live_tty_and_uses_newest(tmp_path):
    _write_signal(tmp_path, "old.json", tty="/dev/ttys001", pid=100, state="idle", ts=100)
    _write_signal(tmp_path, "new.json", tty="/dev/ttys001", pid=100, state="running", ts=200)
    _write_signal(tmp_path, "dead.json", tty="/dev/ttys002", pid=200, state="running", ts=300)

    current = monitor.build_current_state(
        tmp_path,
        now_ts=250,
        pid_alive=lambda pid: pid == 100,
    )

    assert current["summary"]["active_tabs"] == 1
    assert current["summary"]["duplicate_signals_ignored"] == 1
    assert current["summary"]["stale_signals_ignored"] == 1
    assert current["tabs"][0]["tty"] == "/dev/ttys001"
    assert current["tabs"][0]["state"] == "running"
    assert current["tabs"][0]["session_id"] == "new"


def test_transition_events_report_seen_changed_and_gone():
    previous = {
        "tabs": [
            {"tty": "/dev/ttys001", "state": "idle", "session_id": "a", "runtime": "codex"},
            {"tty": "/dev/ttys002", "state": "running", "session_id": "b", "runtime": "codex"},
        ]
    }
    current = {
        "generated_at": "2026-06-25T00:00:00Z",
        "tabs": [
            {"tty": "/dev/ttys001", "state": "running", "session_id": "a", "runtime": "codex"},
            {"tty": "/dev/ttys003", "state": "idle", "session_id": "c", "runtime": "codex"},
        ],
    }

    events = monitor.transition_events(previous, current)

    assert [(event["event"], event["tty"], event["state"]) for event in events] == [
        ("tab_state_changed", "/dev/ttys001", "running"),
        ("tab_seen", "/dev/ttys003", "idle"),
        ("tab_gone", "/dev/ttys002", None),
    ]


def test_write_outputs_writes_current_and_events(tmp_path):
    current_path = tmp_path / "current.json"
    events_path = tmp_path / "events.jsonl"
    previous = {"tabs": []}
    current = {
        "generated_at": "2026-06-25T00:00:00Z",
        "tabs": [{"tty": "/dev/ttys001", "state": "idle", "session_id": "a"}],
    }

    events = monitor.write_outputs(
        current,
        current_path=current_path,
        events_path=events_path,
        previous=previous,
    )

    assert json.loads(current_path.read_text(encoding="utf-8"))["tabs"][0]["tty"] == "/dev/ttys001"
    assert len(events) == 1
    assert json.loads(events_path.read_text(encoding="utf-8").strip())["event"] == "tab_seen"
