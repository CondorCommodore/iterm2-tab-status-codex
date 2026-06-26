from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_iterm_daemon as daemon  # noqa: E402


class FakeLine:
    def __init__(self, string: str):
        self.string = string


class FakeScreen:
    def __init__(self, lines: list[str]):
        self._lines = lines
        self.number_of_lines = len(lines)

    def line(self, idx: int) -> FakeLine:
        return FakeLine(self._lines[idx])


class FakeSession:
    def __init__(self, *, variables: dict[str, object], screen: object = ""):
        self.variables = dict(variables)
        self.screen = screen
        self.set_calls: list[tuple[str, str]] = []

    async def async_get_variable(self, name: str):
        return self.variables.get(name)

    async def async_set_variable(self, name: str, value: str):
        self.set_calls.append((name, value))

    async def async_get_screen_contents(self):
        return self.screen


def test_classify_readiness_prioritizes_input_and_queue():
    assert (
        daemon.classify_readiness(text="permission required", is_processing=True) == "needs_input"
    )
    assert daemon.classify_readiness(text="tab to queue", is_processing=True) == "queued"
    assert daemon.classify_readiness(text="Esc to interrupt", is_processing=None) == "running"
    assert daemon.classify_readiness(text="› ", is_processing=False) == "ready"
    assert daemon.classify_readiness(text="no prompt", is_processing=False) == "idle"


def test_screen_to_text_reads_tail_from_iterm_screen_shape():
    screen = FakeScreen(["one", "two", "three"])

    assert daemon.screen_to_text(screen, tail_lines=2) == "two\nthree"


def test_read_session_record_classifies_runtime_and_report(tmp_path):
    report = tmp_path / "worker-ttys003-report.md"
    report.write_text("done", encoding="utf-8")
    session = FakeSession(
        variables={
            "tty": "/dev/ttys003",
            "session.title": "codex worker",
            "path": "/Users/mikebook/code/home-lab",
            "session.isProcessing": False,
        },
        screen="› ",
    )

    record = asyncio.run(
        daemon.read_session_record(
            session,
            window_index=1,
            tab_index=2,
            session_index=1,
            reports_by_tty=daemon.latest_report_by_tty(tmp_path),
            cos_ttys={"/dev/ttys999"},
        )
    )

    assert record.tty == "/dev/ttys003"
    assert record.runtime == "codex"
    assert record.readiness == "ready"
    assert record.role == "worker"
    assert record.last_fleet_report == "worker-ttys003-report.md"


def test_set_session_variables_sets_status_surface():
    session = FakeSession(variables={})
    record = daemon.SessionRecord(
        window_index=1,
        tab_index=1,
        session_index=1,
        tty="/dev/ttys003",
        title="codex",
        cwd="/Users/mikebook/code/home-lab",
        runtime="codex",
        readiness="running",
        role="worker",
        screen_tail="working",
        last_fleet_report="worker-ttys003.md",
    )

    asyncio.run(daemon.set_session_variables(session, record))

    values = dict(session.set_calls)
    assert values["user.workerReadiness"] == "running"
    assert values["user.workerState"] == "running"
    assert values["user.workerGoal"] == "home-lab"
    assert values["user.lastFleetReport"] == "worker-ttys003.md"


def test_write_state_and_transition_events(tmp_path):
    state_path = tmp_path / daemon.DEFAULT_LIVE_STATE_NAME
    events_path = tmp_path / daemon.DEFAULT_EVENTS_NAME
    first = daemon.SessionRecord(
        window_index=1,
        tab_index=1,
        session_index=1,
        tty="/dev/ttys003",
        title="codex",
        cwd="/Users/mikebook/code/home-lab",
        runtime="codex",
        readiness="ready",
        role="worker",
        screen_tail="›",
        last_fleet_report="",
    )
    second = daemon.SessionRecord(
        window_index=1,
        tab_index=1,
        session_index=1,
        tty="/dev/ttys003",
        title="codex",
        cwd="/Users/mikebook/code/home-lab",
        runtime="codex",
        readiness="running",
        role="worker",
        screen_tail="working",
        last_fleet_report="",
    )

    daemon.write_state(
        [first],
        state_path=state_path,
        events_path=events_path,
        previous=None,
    )
    daemon.write_state([second], state_path=state_path, events_path=events_path)

    current = json.loads(state_path.read_text(encoding="utf-8"))
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert current["summary"]["session_count"] == 1
    assert current["sessions"][0]["readiness"] == "running"
    assert [event["event"] for event in events] == ["session_seen", "session_changed"]
