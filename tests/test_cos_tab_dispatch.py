from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_tab_dispatch as dispatch  # noqa: E402


def test_payload_for_goal_dispatch_appends_enter():
    request = dispatch.DispatchRequest(tty="/dev/ttys003", text="/goal work item 59")

    assert dispatch.payload_for_request(request) == "/goal work item 59\n"


def test_payload_can_validate_without_submit():
    request = dispatch.DispatchRequest(
        tty="/dev/ttys003",
        text="/goal work item 59",
        submit=False,
    )

    assert dispatch.payload_for_request(request) == "/goal work item 59"


@pytest.mark.parametrize("text", ["whoami", "", "/goal bad\nnext", "/goal bad\x03", "/goal bad\x1b"])
def test_payload_rejects_unsafe_text(text):
    request = dispatch.DispatchRequest(tty="/dev/ttys003", text=text)

    with pytest.raises(ValueError):
        dispatch.payload_for_request(request)


@pytest.mark.parametrize("tty", ["ttys003", "/dev/console", "/dev/ttys003;rm"])
def test_payload_rejects_unsafe_tty(tty):
    request = dispatch.DispatchRequest(tty=tty, text="/goal work")

    with pytest.raises(ValueError):
        dispatch.payload_for_request(request)


class FakeSession:
    def __init__(self, tty, *, runtime="", job=""):
        self.tty = tty
        self.runtime = runtime
        self.job = job
        self.sent = []
        self.activated = False

    async def async_get_variable(self, name):
        if name == "tty":
            return self.tty
        if name == "user.workerRuntime":
            return self.runtime
        if name in ("jobName", "foregroundJobName"):
            return self.job
        return ""

    async def async_send_text(self, payload):
        self.sent.append(payload)

    async def async_activate(self):
        self.activated = True


class FakeTab:
    def __init__(self, sessions):
        self.sessions = sessions


class FakeWindow:
    def __init__(self, tabs):
        self.tabs = tabs


def test_find_session_by_tty_with_mocked_iterm(monkeypatch):
    wanted = FakeSession("/dev/ttys004")
    app = type(
        "App",
        (),
        {
            "terminal_windows": [
                FakeWindow([FakeTab([FakeSession("/dev/ttys003"), wanted])])
            ]
        },
    )()

    async def fake_get_app(connection):
        return app

    fake_iterm2 = type("Iterm2", (), {"async_get_app": fake_get_app})
    monkeypatch.setitem(sys.modules, "iterm2", fake_iterm2)

    result = asyncio.run(dispatch.find_session_by_tty(object(), "/dev/ttys004"))

    assert result is wanted


def _install_fake_iterm(monkeypatch, sessions):
    app = type("App", (), {"terminal_windows": [FakeWindow([FakeTab(sessions)])]})()

    async def fake_get_app(connection):
        return app

    fake_iterm2 = type("Iterm2", (), {"async_get_app": fake_get_app})
    monkeypatch.setitem(sys.modules, "iterm2", fake_iterm2)


def test_dispatch_rejects_shell_like_target(monkeypatch):
    target = FakeSession("/dev/ttys003", job="zsh")
    _install_fake_iterm(monkeypatch, [target])

    result = asyncio.run(
        dispatch.dispatch(
            object(),
            dispatch.DispatchRequest(tty="/dev/ttys003", text="/goal do work"),
        )
    )

    assert result["ok"] is False
    assert "does not look like codex/claude" in result["error"]
    assert target.sent == []


def test_dispatch_sends_to_agent_and_returns_focus(monkeypatch):
    target = FakeSession("/dev/ttys003", runtime="codex")
    cos = FakeSession("/dev/ttys001", runtime="codex")
    _install_fake_iterm(monkeypatch, [cos, target])

    result = asyncio.run(
        dispatch.dispatch(
            object(),
            dispatch.DispatchRequest(
                tty="/dev/ttys003",
                text="/goal do work",
                return_tty="/dev/ttys001",
            ),
        )
    )

    assert result["ok"] is True
    assert target.sent == ["/goal do work\n"]
    assert cos.activated is True
    assert result["focus_returned"] is True


def test_looks_like_agent_session_uses_job_or_runtime():
    assert dispatch.looks_like_agent_session({"jobName": "codex", "user.workerRuntime": ""})
    assert dispatch.looks_like_agent_session({"jobName": "zsh", "user.workerRuntime": "claude"})
    assert not dispatch.looks_like_agent_session({"jobName": "zsh", "user.workerRuntime": ""})
