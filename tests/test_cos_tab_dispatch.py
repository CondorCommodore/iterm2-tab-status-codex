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
    def __init__(self, tty):
        self.tty = tty
        self.sent = []

    async def async_get_variable(self, name):
        if name == "tty":
            return self.tty
        return ""

    async def async_send_text(self, payload):
        self.sent.append(payload)


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
