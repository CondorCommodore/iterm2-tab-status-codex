from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_iterm_daemon as daemon  # noqa: E402
from c2_runtime_hook import RuntimeHookRecord, write_record  # noqa: E402


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
    def __init__(
        self,
        *,
        variables: dict[str, object],
        screen: object = "",
        session_id: str = "",
        fail_set: bool = False,
    ):
        self.variables = dict(variables)
        self.screen = screen
        self.session_id = session_id
        self.fail_set = fail_set
        self.set_calls: list[tuple[str, str]] = []

    async def async_get_variable(self, name: str):
        return self.variables.get(name)

    async def async_set_variable(self, name: str, value: str):
        if self.fail_set:
            raise RuntimeError("simulated iTerm variable write failure")
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


def test_unknown_interactive_modal_is_generic_needs_input():
    screen = """GPT-5.4 Mini will be deprecated soon
Codex now uses GPT-5.6 Luna in place of GPT-5.4 Mini.
Choose how you'd like Codex to proceed.
1. Try new model
2. Use existing model
Use arrows to move, press enter to confirm
"""

    assert daemon.classify_readiness(text=screen, is_processing=False) == "needs_input"
    assert daemon.classify_attention_reason(screen) == "interactive_input"


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
    assert record.attention_reason is None
    assert record.role == "worker"
    assert record.last_fleet_report == "worker-ttys003-report.md"
    assert record.prompt_ready is False
    assert record.observation_trusted is False


def test_screen_ready_text_cannot_promote_without_trusted_runtime_hook():
    session = FakeSession(
        variables={
            "tty": "/dev/ttys003",
            "session.title": "codex worker",
            "path": "/tmp/disposable",
            "session.isProcessing": False,
        },
        screen="ready\n› ",
    )

    record = asyncio.run(
        daemon.read_session_record(
            session,
            window_index=1,
            tab_index=1,
            session_index=1,
        )
    )

    assert record.readiness == "ready"
    assert record.prompt_ready is False
    assert record.input_buffer_state == "unknown"
    assert record.observation_trusted is False


def test_partial_hook_without_live_runtime_cannot_promote():
    session = FakeSession(
        variables={
            "tty": "/dev/ttys003",
            "session.title": "codex worker",
            "path": "/tmp/disposable",
            "session.isProcessing": False,
            "user.workerObservationProfile": "codex-cli",
            "user.workerObservationProfileVersion": "1",
            "user.workerPromptState": "ready",
            "user.workerInputBufferState": "empty",
            "user.cliSessionId": "cli-1",
            "user.coordSessionId": "coord-1",
        },
        screen="ready\n› ",
    )

    record = asyncio.run(
        daemon.read_session_record(
            session,
            window_index=1,
            tab_index=1,
            session_index=1,
        )
    )

    assert record.runtime == "codex"
    assert record.prompt_ready is False
    assert record.observation_trusted is False


def test_unbacked_session_variables_cannot_self_promote():
    base = {
        "tty": "/dev/ttys003",
        "session.title": "codex worker",
        "path": "/tmp/disposable",
        "session.isProcessing": False,
        "user.workerRuntime": "codex",
        "user.workerObservationProfile": "codex-cli",
        "user.workerObservationProfileVersion": "1",
        "user.workerPromptState": "ready",
        "user.cliSessionId": "cli-1",
        "user.coordSessionId": "coord-1",
    }
    for buffer_state in ("empty", "nonempty", "unknown"):
        session = FakeSession(
            variables={**base, "user.workerInputBufferState": buffer_state},
            screen="› ",
        )
        record = asyncio.run(
            daemon.read_session_record(
                session,
                window_index=1,
                tab_index=1,
                session_index=1,
            )
        )
        assert record.prompt_ready is False
        assert record.observation_trusted is False


def test_inert_hook_cache_sets_exact_profile_but_never_prompt_ready(tmp_path):
    hook = RuntimeHookRecord.from_hook(
        runtime="codex",
        event="turn-ended",
        iterm_session_id="iterm-worker",
        tty="/dev/ttys003",
        cli_session_id="cli-1",
        coord_session_id="coord-1",
        payload={"session_id": "cli-1"},
    )
    write_record(hook, tmp_path)
    session = FakeSession(
        variables={
            "tty": "/dev/ttys003",
            "session.title": "codex worker",
            "path": "/tmp/disposable",
            "session.isProcessing": False,
        },
        screen="› ",
        session_id="iterm-worker",
    )

    record = asyncio.run(
        daemon.read_session_record(
            session,
            window_index=1,
            tab_index=1,
            session_index=1,
            runtime_hook_state_dir=tmp_path,
        )
    )
    asyncio.run(daemon.set_session_variables(session, record))
    values = dict(session.set_calls)

    assert record.observation_trusted is True
    assert record.prompt_state == "ready"
    assert record.input_buffer_state == "unknown"
    assert record.prompt_ready is False
    assert values["user.workerRuntime"] == "codex"
    assert values["user.workerObservationProfile"] == "codex-cli"
    assert values["user.workerInputBufferState"] == "unknown"
    assert values["user.cliSessionId"] == "cli-1"


def test_missing_or_wrong_iterm_hook_cache_clears_action_variables(tmp_path):
    wrong_runtime = RuntimeHookRecord.from_hook(
        runtime="claude",
        event="stop",
        iterm_session_id="new-iterm-worker",
        tty="/dev/ttys003",
        cli_session_id="cli-claude",
        coord_session_id="coord-claude",
        payload={"session_id": "cli-claude"},
    )
    write_record(wrong_runtime, tmp_path)
    session = FakeSession(
        variables={
            "tty": "/dev/ttys003",
            "session.title": "codex worker",
            "path": "/tmp/disposable",
            "session.isProcessing": False,
            "user.workerObservationProfile": "stale-profile",
            "user.workerPromptState": "ready",
            "user.workerInputBufferState": "empty",
            "user.cliSessionId": "stale-cli",
            "user.coordSessionId": "stale-coord",
        },
        screen="› ",
        session_id="new-iterm-worker",
    )

    record = asyncio.run(
        daemon.read_session_record(
            session,
            window_index=1,
            tab_index=1,
            session_index=1,
            runtime_hook_state_dir=tmp_path,
        )
    )
    asyncio.run(daemon.set_session_variables(session, record))
    values = dict(session.set_calls)

    assert record.observation_trusted is False
    assert record.prompt_ready is False
    assert values["user.workerObservationProfile"] == ""
    assert values["user.workerPromptState"] == ""
    assert values["user.workerInputBufferState"] == ""
    assert values["user.cliSessionId"] == ""
    assert values["user.coordSessionId"] == ""


def test_failed_best_effort_clear_does_not_create_trusted_record(tmp_path):
    session = FakeSession(
        variables={
            "tty": "/dev/ttys003",
            "session.title": "codex worker",
            "path": "/tmp/disposable",
            "session.isProcessing": False,
            "user.workerRuntime": "codex",
            "user.workerObservationProfile": "codex-cli",
            "user.workerObservationProfileVersion": "1",
            "user.workerPromptState": "ready",
            "user.workerInputBufferState": "empty",
            "user.cliSessionId": "stale-cli",
            "user.coordSessionId": "stale-coord",
        },
        screen="› ",
        session_id="iterm-worker",
        fail_set=True,
    )

    record = asyncio.run(
        daemon.read_session_record(
            session,
            window_index=1,
            tab_index=1,
            session_index=1,
            runtime_hook_state_dir=tmp_path,
        )
    )
    asyncio.run(daemon.set_session_variables(session, record))

    assert record.observation_trusted is False
    assert record.prompt_ready is False
    assert session.set_calls == []


def test_set_session_variables_sets_status_surface():
    session = FakeSession(variables={})
    record = daemon.SessionRecord(
        window_index=1,
        tab_index=1,
        session_index=1,
        iterm_session_id="iterm-worker-3",
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
        iterm_session_id="iterm-worker-3",
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
        iterm_session_id="iterm-worker-3",
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
