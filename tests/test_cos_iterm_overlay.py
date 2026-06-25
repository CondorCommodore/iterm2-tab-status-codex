from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_iterm_overlay as overlay  # noqa: E402


def test_tab_variables_for_worker_tab():
    values = overlay.tab_variables(
        {
            "runtime": "codex",
            "project": "surface-ui",
            "cwd": "/Users/mikebook/code/surface-ui",
            "state": "running",
        }
    )

    assert values["user.cosRole"] == "worker"
    assert values["user.workerState"] == "running"
    assert values["user.workerGoal"] == "surface-ui"
    assert values["user.workerRuntime"] == "codex"
    assert values["user.workerCwd"] == "/Users/mikebook/code/surface-ui"


def test_tab_variables_marks_code_root_as_cos():
    values = overlay.tab_variables(
        {
            "runtime": "codex",
            "project": "code",
            "cwd": "/Users/mikebook/code",
            "state": "idle",
        }
    )

    assert values["user.cosRole"] == "cos"
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
