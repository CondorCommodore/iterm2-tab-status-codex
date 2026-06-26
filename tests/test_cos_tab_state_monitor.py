from __future__ import annotations

import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

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


def test_build_current_state_fails_closed_when_iterm_processing_disagrees(tmp_path):
    _write_signal(tmp_path, "idle.json", tty="/dev/ttys004", pid=100, state="idle", ts=100)

    current = monitor.build_current_state(
        tmp_path,
        now_ts=120,
        pid_alive=lambda pid: pid == 100,
        live_iterm_states={
            "/dev/ttys004": {
                "window": 1,
                "tab": 4,
                "tty": "/dev/ttys004",
                "is_processing": True,
                "name": "code (codex)",
            }
        },
    )

    tab = current["tabs"][0]
    assert tab["tty"] == "/dev/ttys004"
    assert tab["signal_state"] == "idle"
    assert tab["state"] == "running"
    assert tab["state_source"] == "iterm_processing"
    assert tab["iterm_is_processing"] is True
    assert current["summary"]["counts_by_state"]["running"] == 1
    assert current["summary"]["counts_by_state"]["idle"] == 0


def test_read_live_iterm_states_parses_processing_by_tty():
    def fake_run(*args, **kwargs):
        return CompletedProcess(
            args=args,
            returncode=0,
            stdout=("1|1|/dev/ttys008|true|COS\n1|4|/dev/ttys004|false|code (codex)\nbad row\n"),
            stderr="",
        )

    states = monitor.read_live_iterm_states(run=fake_run)

    assert states["/dev/ttys008"]["is_processing"] is True
    assert states["/dev/ttys008"]["window"] == 1
    assert states["/dev/ttys004"]["is_processing"] is False
    assert "/dev/ttys999" not in states


def test_read_live_iterm_states_fail_open_on_osascript_error():
    def fake_run(*args, **kwargs):
        return CompletedProcess(args=args, returncode=1, stdout="", stderr="no iTerm")

    assert monitor.read_live_iterm_states(run=fake_run) == {}


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
