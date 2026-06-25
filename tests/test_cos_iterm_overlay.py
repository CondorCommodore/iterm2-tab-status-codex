from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_iterm_overlay as overlay  # noqa: E402


def test_tab_variables_for_worker_tab():
    values = overlay.tab_variables(
        {
            "tty": "/dev/ttys003",
            "runtime": "codex",
            "project": "surface-ui",
            "cwd": "/Users/mikebook/code/surface-ui",
            "state": "running",
        },
        reports_by_tty={"ttys003": "worker-ttys003-report.md"},
    )

    assert values["user.cosRole"] == "worker"
    assert values["user.workerState"] == "running"
    assert values["user.workerGoal"] == "surface-ui"
    assert values["user.workerRuntime"] == "codex"
    assert values["user.workerCwd"] == "/Users/mikebook/code/surface-ui"
    assert values["user.lastFleetReport"] == "worker-ttys003-report.md"


def test_tab_variables_does_not_guess_code_root_is_cos():
    values = overlay.tab_variables(
        {
            "runtime": "codex",
            "project": "code",
            "cwd": "/Users/mikebook/code",
            "state": "idle",
        }
    )

    assert values["user.cosRole"] == "worker"
    assert values["user.workerState"] == "idle"


def test_tab_variables_marks_configured_tty_as_cos():
    values = overlay.tab_variables(
        {
            "tty": "/dev/ttys006",
            "runtime": "codex",
            "project": "home-lab",
            "cwd": "/Users/mikebook/code/home-lab",
            "state": "running",
        },
        cos_ttys={"/dev/ttys006"},
    )

    assert values["user.cosRole"] == "cos"


def test_watch_overlay_polls_until_cancelled(monkeypatch, tmp_path):
    calls = []

    async def fake_apply(connection, state_path):
        calls.append((connection, state_path))

    async def fake_sleep(seconds):
        assert seconds == overlay.POLL_INTERVAL_SECONDS
        raise StopAsyncIteration

    monkeypatch.setattr(overlay, "apply_overlay", fake_apply)
    monkeypatch.setattr(overlay.asyncio, "sleep", fake_sleep)

    connection = object()
    state_path = tmp_path / "tab-state-current.json"
    try:
        asyncio.run(overlay.watch_overlay(connection, state_path))
    except StopAsyncIteration:
        pass

    assert calls == [(connection, state_path)]


def test_latest_report_by_tty_uses_newest_report(tmp_path):
    older = tmp_path / "worker-ttys003-old.md"
    newer = tmp_path / "worker-ttys003-new.md"
    other = tmp_path / "worker-ttys004.md"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    other.write_text("other", encoding="utf-8")
    os.utime(older, (1, 1))
    os.utime(other, (2, 2))
    os.utime(newer, (3, 3))

    result = overlay.latest_report_by_tty(tmp_path)

    assert result["ttys003"] == "worker-ttys003-new.md"
    assert result["ttys004"] == "worker-ttys004.md"
