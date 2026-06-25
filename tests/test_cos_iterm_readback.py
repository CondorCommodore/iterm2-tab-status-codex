from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_iterm_readback as readback  # noqa: E402


class FakeSession:
    def __init__(self, values):
        self.values = values

    async def async_get_variable(self, name):
        return self.values.get(name)


def test_session_snapshot_reads_cos_variables():
    session = FakeSession(
        {
            "tty": "/dev/ttys003",
            "user.cosRole": "worker",
            "user.workerState": "running",
        }
    )

    result = asyncio.run(readback.session_snapshot(session))

    assert result["tty"] == "/dev/ttys003"
    assert result["user.cosRole"] == "worker"
    assert result["user.workerState"] == "running"
    assert result["user.workerGoal"] == ""


def test_session_snapshot_tolerates_variable_errors():
    class BrokenSession:
        async def async_get_variable(self, name):
            raise RuntimeError("boom")

    result = asyncio.run(readback.session_snapshot(BrokenSession(), ("tty",)))

    assert result == {"tty": ""}
