from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_tab_dispatch as dispatch  # noqa: E402
from c2_contract import DispatchEnvelope, ReceiptStore, RunManifest  # noqa: E402
from c2_visual_decision import VisualDecision, VisualObservation  # noqa: E402


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


@pytest.mark.parametrize(
    "text",
    ["whoami", "", "/goal bad\nnext", "/goal bad\x03", "/goal bad\x1b"],
)
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
    def __init__(
        self,
        tty,
        *,
        runtime="",
        job="",
        session_id="",
        cli_session_id="",
        coord_session_id="",
        processing=False,
        snapshots=None,
    ):
        self.tty = tty
        self.runtime = runtime
        self.job = job
        self.session_id = session_id
        self.cli_session_id = cli_session_id
        self.coord_session_id = coord_session_id
        self.processing = processing
        self.snapshots = list(snapshots or [])
        self.snapshot_index = 0
        self.sent = []

    async def async_get_variable(self, name):
        values = {
            "tty": self.tty,
            "user.workerRuntime": self.runtime,
            "user.cliSessionId": self.cli_session_id,
            "user.coordSessionId": self.coord_session_id,
            "session.isProcessing": self.processing,
            "jobName": self.job,
            "foregroundJobName": self.job,
        }
        if self.snapshots:
            values.update(self.snapshots[min(self.snapshot_index, len(self.snapshots) - 1)])
        result = values.get(name, "")
        if name == "session.currentCommand" and self.snapshots:
            self.snapshot_index += 1
        return result

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
        {"terminal_windows": [FakeWindow([FakeTab([FakeSession("/dev/ttys003"), wanted])])]},
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


def test_dispatch_sends_to_agent_without_focus_side_effects(monkeypatch):
    target = FakeSession("/dev/ttys003", runtime="codex")
    cos = FakeSession("/dev/ttys001", runtime="codex")
    _install_fake_iterm(monkeypatch, [cos, target])

    result = asyncio.run(
        dispatch.dispatch(
            object(),
            dispatch.DispatchRequest(
                tty="/dev/ttys003",
                text="/goal do work",
            ),
        )
    )

    assert result["ok"] is True
    assert target.sent == ["/goal do work\n"]
    assert "focus_returned" not in result


def test_looks_like_agent_session_uses_job_or_runtime():
    assert dispatch.looks_like_agent_session({"jobName": "codex", "user.workerRuntime": ""})
    assert dispatch.looks_like_agent_session({"jobName": "zsh", "user.workerRuntime": "claude"})
    assert not dispatch.looks_like_agent_session({"jobName": "zsh", "user.workerRuntime": ""})


def _manifest(transport="tab"):
    return RunManifest.from_dict(
        {
            "manifest_id": "test",
            "controller": {
                "controller_id": "cos",
                "host": "macbook",
                "runtime": "codex",
                "iterm_session_id": "iterm-cos",
                "tty": "/dev/ttys001",
                "cli_session_id": "cli-cos",
                "coord_session_id": "coord-cos",
                "coord_agent_id": "mikebook_codex",
            },
            "workers": [
                {
                    "worker_id": "worker",
                    "host": "macbook",
                    "runtime": "codex",
                    "iterm_session_id": "iterm-worker",
                    "tty": "/dev/ttys003",
                    "cli_session_id": "cli-worker",
                    "coord_session_id": "coord-worker",
                    "coord_agent_id": "mikebook_codex",
                    "repositories": ["Condor/repo"],
                }
            ],
            "plan_paths": ["/plan"],
            "permitted_repositories": ["Condor/repo"],
            "permitted_actions": ["inspect", "test"],
            "dispatch_transport": transport,
        }
    )


def _envelope():
    return DispatchEnvelope.from_dict(
        {
            "assignment_id": "assignment-1",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "worker_id": "worker",
            "cli_session_id": "cli-worker",
            "coord_session_id": "coord-worker",
            "objective": "do bounded work",
            "repo": "Condor/repo",
            "worktree": "/tmp/worktree",
            "scope": ["src/a.py"],
            "acceptance_tests": ["pytest"],
            "stopping_condition": "durable result",
            "report_destination": "coord-api",
            "authorization_limits": ["no deploy"],
            "permitted_actions": ["inspect", "test"],
            "controller_epoch": 7,
            "idempotency_key": "dispatch-1",
        }
    )


def _visual_observation():
    return VisualObservation.from_dict(
        {
            "worker_id": "worker",
            "iterm_session_id": "iterm-worker",
            "screenshot_sha256": "a" * 64,
            "captured_ts": time.time(),
            "summary": "The worker is blocked on an interactive choice",
            "controller_epoch": 7,
            "worker_epoch": 13,
        }
    )


def _visual_decision(observation):
    return VisualDecision.from_dict(
        {
            "observation_digest": observation.digest(),
            "action": "press_enter",
            "text": "",
            "rationale": "Continue using the selected bounded runtime option",
            "decided_by": "llm:test-supervisor",
            "idempotency_key": "visual-action-1",
        }
    )


def test_visual_decision_requires_controller_and_worker_epochs(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _visual_observation()
    verified = []

    result = asyncio.run(
        dispatch.execute_visual_decision(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=_visual_decision(observation),
            verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
            receipts=ReceiptStore(tmp_path / "visual-receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is False
    assert result["action_applied"] is True
    assert result["verification_state"] == "pending"
    assert verified == [
        ("workspace:mikebook:c2-supervisor", 7),
        ("workspace:mikebook:c2-worker:worker", 13),
    ]


def test_unacknowledged_visual_decision_fails_closed(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _visual_observation()
    result = asyncio.run(
        dispatch.execute_visual_decision(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=_visual_decision(observation),
            verify_epoch=lambda *_args: None,
            receipts=ReceiptStore(tmp_path / "visual-receipts.jsonl"),
            ack_attempts=1,
        )
    )
    assert result["ok"] is False
    assert result["receipt"]["observed_ack"] is False
    assert target.sent == ["\r"]
    assert result["receipt"]["post_visual_verification_required"] is True


def test_visual_decision_lease_loss_prevents_terminal_input(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _visual_observation()

    def reject_worker_epoch(resource, _epoch):
        if resource.endswith(":worker"):
            raise RuntimeError("worker lease lost")

    with pytest.raises(RuntimeError, match="worker lease lost"):
        asyncio.run(
            dispatch.execute_visual_decision(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_visual_decision(observation),
                verify_epoch=reject_worker_epoch,
                receipts=ReceiptStore(tmp_path / "visual-receipts.jsonl"),
            )
        )
    assert target.sent == []


def test_registered_dispatch_uses_exact_session_epoch_and_crlf(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    verified = []

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is True
    assert verified == [("workspace:mikebook:c2-supervisor", 7)]
    assert len(target.sent) == 3
    assert target.sent[0].startswith("/goal C2_DISPATCH ")
    assert not target.sent[0].endswith("\r")
    assert target.sent[1] == "\r"
    assert target.sent[2] == "\n"
    assert result["receipt"]["observed_ack"] is True
    assert result["receipt"]["submit_method"] == "iterm2-python-api-crlf"
    assert result["receipt"]["metrics"] == {"recovery_submitted": False}


def test_registered_dispatch_accepts_exact_foreground_runtime_when_iterm_name_drifts(
    monkeypatch, tmp_path
):
    target = FakeSession(
        "/dev/ttys003",
        runtime="unknown",
        job="SkyComputerUseClient",
        session_id="iterm-worker",
        cli_session_id="",
        coord_session_id="",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "tty_foreground_group_matches_runtime", lambda *_args: True)

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda *_args: None,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is True
    assert target.sent[0].startswith("/goal C2_DISPATCH ")


def test_controller_poke_uses_same_crlf_submission_helper(monkeypatch):
    target = FakeSession(
        "/dev/ttys001",
        runtime="codex",
        job="codex",
        session_id="iterm-cos",
        cli_session_id="cli-cos",
        coord_session_id="coord-cos",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    verified = []

    result = asyncio.run(
        dispatch.send_controller_poke(
            object(),
            manifest=_manifest(),
            text="controller wake",
            controller_epoch=7,
            idempotency_key="poke-1",
            verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
            ack_attempts=1,
        )
    )

    assert result["ok"] is True
    assert verified == [("workspace:mikebook:c2-supervisor", 7)]
    assert target.sent == ["controller wake", "\r", "\n"]
    assert result["observed_ack"] is True
    assert result["submit_method"] == "iterm2-python-api-crlf"
    assert result["metrics"] == {"recovery_submitted": False}


def test_static_active_state_is_not_a_post_dispatch_ack(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            {
                "user.workerReadiness": "running",
                "session.isProcessing": True,
            }
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    verified = []

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is False
    assert result["error"] == "registered target did not acknowledge dispatch"
    assert result["receipt"]["observed_ack"] is False
    assert result["receipt"]["metrics"] == {"recovery_submitted": True}
    assert target.sent == [target.sent[0], "\r", "\n", "\r"]
    assert verified == [
        ("workspace:mikebook:c2-supervisor", 7),
        ("workspace:mikebook:c2-supervisor", 7),
    ]


def test_tab_dispatch_fences_worker_reservation_before_each_injection(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[{"session.isProcessing": False}, {"session.isProcessing": True}],
    )
    _install_fake_iterm(monkeypatch, [target])
    verified = []
    reservation = {"resource": "workspace:mikebook:c2-worker:worker-codex", "epoch": 19}
    asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            reservation=reservation,
            ack_attempts=1,
        )
    )
    assert target.sent
    assert (reservation["resource"], 19) in verified


def test_headless_dispatch_fences_worker_reservation(tmp_path):
    verified = []
    reservation = {
        "resource": "workspace:mikebook:c2-worker:worker-codex",
        "epoch": 23,
        "expires_at": "2099-01-01T00:00:00Z",
    }
    result = dispatch.dispatch_registered_headless(
        manifest=_manifest(),
        envelope=_envelope(),
        verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
        receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
        reservation=reservation,
        run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "done", ""),
    )
    assert result["ok"] is True
    assert verified == [("workspace:mikebook:c2-supervisor", 7), (reservation["resource"], 23)]


def test_headless_timeout_is_bounded_by_worker_reservation(tmp_path):
    observed = {}
    reservation = {
        "resource": "workspace:mikebook:c2-worker:worker-codex",
        "epoch": 23,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat(),
    }

    def run(command, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(command, 0, "done", "")

    result = dispatch.dispatch_registered_headless(
        manifest=_manifest(),
        envelope=_envelope(),
        verify_epoch=lambda *_args: None,
        receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
        reservation=reservation,
        run=run,
    )
    assert result["ok"] is True
    assert 30 <= observed["timeout"] < 40


def test_queued_prompt_gets_one_refenced_recovery_submit(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": False},
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    verified = []

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["receipt"]["observed_ack"] is True
    assert result["receipt"]["metrics"] == {"recovery_submitted": True}
    assert target.sent[-3:] == ["\r", "\n", "\r"]
    assert verified == [
        ("workspace:mikebook:c2-supervisor", 7),
        ("workspace:mikebook:c2-supervisor", 7),
    ]


def test_true_start_prevents_recovery_and_duplicate_submit(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    verified = []

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["receipt"]["observed_ack"] is True
    assert result["receipt"]["metrics"] == {"recovery_submitted": False}
    assert len(target.sent) == 3
    assert verified == [("workspace:mikebook:c2-supervisor", 7)]


def test_start_during_recovery_reread_suppresses_fallback(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    verified = []

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["receipt"]["observed_ack"] is True
    assert result["receipt"]["metrics"] == {"recovery_submitted": False}
    assert len(target.sent) == 3
    assert verified == [("workspace:mikebook:c2-supervisor", 7)]


def test_registered_dispatch_rejects_foreground_helper(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="SkyComputerUseClient",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "tty_foreground_group_matches_runtime", lambda *_: False)

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda *_args: None,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is False
    assert result["error"] == "registered agent does not own the terminal foreground"
    assert target.sent == []


def test_registered_dispatch_accepts_helper_label_when_runtime_owns_foreground_group(
    monkeypatch, tmp_path
):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="SkyComputerUseClient",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "tty_foreground_group_matches_runtime", lambda *_: True)

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda *_args: None,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is True
    assert target.sent[-2:] == ["\r", "\n"]


def test_registered_dispatch_ignores_unknown_runtime_hook(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="unknown",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda *_args: None,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is True


def test_foreground_group_output_requires_runtime_executable_in_tpgid():
    codex_group = """37282 37282 node /opt/bin/codex
76195 37282 /Applications/SkyComputerUseClient turn-ended codex-tui
"""
    helper_group = """37282 76195 node /opt/bin/codex
76195 76195 /Applications/SkyComputerUseClient turn-ended codex-tui
"""

    assert dispatch.foreground_group_output_matches_runtime(codex_group, "codex") is True
    assert dispatch.foreground_group_output_matches_runtime(helper_group, "codex") is False


def test_registered_dispatch_rejects_reused_tty_with_wrong_session(monkeypatch, tmp_path):
    target = FakeSession("/dev/ttys003", runtime="codex", session_id="iterm-successor")
    _install_fake_iterm(monkeypatch, [target])

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda *_args: None,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
        )
    )

    assert result == {"ok": False, "error": "registered iTerm session not found"}
    assert target.sent == []


def test_headless_commands_resume_same_uuid_for_codex_and_claude():
    codex = _manifest().workers[0]
    claude = type(codex)(
        **{**codex.__dict__, "runtime": "claude", "cli_session_id": "claude-session"}
    )

    assert dispatch.headless_command(codex, "/goal work")[:4] == [
        "codex",
        "exec",
        "resume",
        "cli-worker",
    ]
    assert dispatch.headless_command(claude, "/goal work")[:4] == [
        "claude",
        "--resume",
        "claude-session",
        "--print",
    ]


def test_applescript_write_without_ack_fails_closed(tmp_path):
    result = dispatch.dispatch_registered_applescript(
        manifest=_manifest(),
        envelope=_envelope(),
        verify_epoch=lambda *_args: None,
        receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
        run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "sent", ""),
    )
    assert result["ok"] is False
    assert result["receipt"]["observed_ack"] is False
    assert "no target acknowledgment" in result["error"]


def test_policy_routes_tab_through_iterm_api_edge(tmp_path):
    seen = []

    result = dispatch.dispatch_registered_by_policy(
        manifest=_manifest("tab"),
        envelope=_envelope(),
        verify_epoch=lambda *_args: None,
        receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
        edge_dispatch=lambda envelope: seen.append(envelope) or {"ok": True},
    )

    assert result == {"ok": True}
    assert seen[0]["assignment_id"] == "assignment-1"
