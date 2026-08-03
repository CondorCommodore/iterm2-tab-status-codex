from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_tab_dispatch as dispatch  # noqa: E402
from c2_contract import (  # noqa: E402
    ContractError,
    DispatchEnvelope,
    ReceiptStore,
    RunManifest,
)
from c2_runtime_hook import (  # noqa: E402
    HOOK_SCHEMA_VERSION,
    SignedRuntimeHookObservation,
    interrupt_challenge_binding_sha256,
    session_variable_values,
)
from c2_runtime_observation import RuntimeObservation  # noqa: E402
from c2_visual_decision import VisualDecision, VisualObservation  # noqa: E402

BROKER_KEY = b"test-only-broker-key" * 2


def _broker_verifier(report):
    claimed = dict(report)
    signature = claimed.pop("signature", "")
    canonical = json.dumps(claimed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {
        "verified": hmac.compare_digest(
            signature, hmac.new(BROKER_KEY, canonical, hashlib.sha256).hexdigest()
        ),
        "observation_digest": hashlib.sha256(canonical).hexdigest(),
        "challenge_id": claimed.get("challenge_id"),
        "observed_after_arm": bool(claimed.get("challenge_id")),
    }


def _broker_args():
    state = {"binding_sha256": ""}

    def create_challenge(request):
        state["binding_sha256"] = interrupt_challenge_binding_sha256(request)
        return {
            "challenge_id": "challenge-1",
            "issued_at": 1001.0,
            "binding_sha256": state["binding_sha256"],
        }

    def arm_challenge(request):
        return {
            "challenge_id": request["challenge_id"],
            "armed": True,
            "binding_sha256": request["binding_sha256"],
        }

    def verify(report):
        result = _broker_verifier(report)
        result["challenge_binding_sha256"] = state["binding_sha256"]
        return result

    return {
        "create_challenge": create_challenge,
        "arm_challenge": arm_challenge,
        "verify_hook_authenticity": verify,
        "hook_clock": lambda: 1002.0,
    }


def _signed_hook(
    *,
    sequence,
    prompt_state,
    input_buffer_state="empty",
    observed_at=None,
    cli_session_id="cli-worker",
    coord_session_id="coord-worker",
    iterm_session_id="iterm-worker",
    challenge_id=None,
):
    proof = SignedRuntimeHookObservation(
        hook_schema_version=HOOK_SCHEMA_VERSION,
        runtime_observation=RuntimeObservation.from_dict(
            {
                "runtime": "codex",
                "profile_id": "codex-cli",
                "profile_version": 1,
                "prompt_state": prompt_state,
                "input_buffer_state": input_buffer_state,
                "cli_session_id": cli_session_id,
                "coord_session_id": coord_session_id,
            }
        ),
        iterm_session_id=iterm_session_id,
        sequence=sequence,
        observed_at=(
            1002.0 if observed_at is None and prompt_state == "ready" else observed_at or 1000.0
        ),
        event_id=f"event-{sequence}",
        challenge_id=(
            "challenge-1"
            if challenge_id is None and prompt_state == "ready"
            else challenge_id or ""
        ),
        signature="",
    )
    signed = replace(
        proof,
        signature=hmac.new(BROKER_KEY, proof.canonical_bytes(), hashlib.sha256).hexdigest(),
    )
    return {f"user.{key}": value for key, value in session_variable_values(signed).items()}


def _dispatch_hook(
    *,
    sequence,
    prompt_state,
    input_buffer_state="empty",
    observed_at=None,
    runtime="codex",
    profile_id=None,
    profile_version=1,
    cli_session_id="cli-worker",
    coord_session_id="coord-worker",
    iterm_session_id="iterm-worker",
):
    runtime = runtime.lower()
    if profile_id is None:
        profile_id = "claude-code" if runtime == "claude" else "codex-cli"
    observed_at = (1001.0 if sequence == 1 else 1002.0) if observed_at is None else observed_at
    proof = SignedRuntimeHookObservation(
        hook_schema_version=HOOK_SCHEMA_VERSION,
        runtime_observation=RuntimeObservation.from_dict(
            {
                "runtime": runtime,
                "profile_id": profile_id,
                "profile_version": profile_version,
                "prompt_state": prompt_state,
                "input_buffer_state": input_buffer_state,
                "cli_session_id": cli_session_id,
                "coord_session_id": coord_session_id,
            }
        ),
        iterm_session_id=iterm_session_id,
        sequence=sequence,
        observed_at=observed_at,
        event_id=f"dispatch-event-{sequence}",
        challenge_id="",
        signature="",
    )
    signed = replace(
        proof,
        signature=hmac.new(BROKER_KEY, proof.canonical_bytes(), hashlib.sha256).hexdigest(),
    )
    return {f"user.{key}": value for key, value in session_variable_values(signed).items()}


def _verify_dispatch_hook(report):
    return _broker_verifier(report)


def _freeze_dispatch_clock(monkeypatch):
    monkeypatch.setattr(dispatch.time, "time", lambda: 1002.0)


def _hook_digest(values):
    return SignedRuntimeHookObservation.from_session_variables(values).digest()


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
    [
        "whoami",
        "",
        "/goal bad\nnext",
        "/goal bad\x00",
        "/goal bad\x03",
        "/goal bad\x04",
        "/goal bad\x07",
        "/goal bad\x1a",
        "/goal bad\x1b",
        "/goal bad\x7f",
    ],
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
        profile_id="codex-cli",
        profile_version=1,
        prompt_state="ready",
        input_buffer_state="empty",
    ):
        self.tty = tty
        self.runtime = runtime
        self.job = job
        self.session_id = session_id
        self.cli_session_id = cli_session_id
        self.coord_session_id = coord_session_id
        self.processing = processing
        self.snapshots = list(snapshots or [])
        self.profile_id = profile_id
        self.profile_version = profile_version
        self.prompt_state = prompt_state
        self.input_buffer_state = input_buffer_state
        self.snapshot_index = 0
        self.sent = []

    async def async_get_variable(self, name):
        values = {
            "tty": self.tty,
            "user.workerRuntime": self.runtime,
            "user.cliSessionId": self.cli_session_id,
            "user.coordSessionId": self.coord_session_id,
            "user.workerObservationProfile": self.profile_id,
            "user.workerObservationProfileVersion": self.profile_version,
            "user.workerPromptState": self.prompt_state,
            "user.workerInputBufferState": self.input_buffer_state,
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

    def trusted_hook_record(_state_dir, *, iterm_session_id, tty):
        target = next(
            (
                session
                for session in sessions
                if session.session_id == iterm_session_id and session.tty == tty
            ),
            None,
        )
        if target is None:
            return None
        return SimpleNamespace(
            runtime=target.runtime,
            profile_id=target.profile_id,
            profile_version=target.profile_version,
            prompt_state=target.prompt_state,
            input_buffer_state=target.input_buffer_state,
            cli_session_id=target.cli_session_id,
            coord_session_id=target.coord_session_id,
        )

    monkeypatch.setattr(dispatch, "load_runtime_hook_record", trusted_hook_record)


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


def _manifest(
    transport="tab",
    controller_visible: bool = True,
    *,
    worker_runtime: str = "codex",
):
    controller = {
        "controller_id": "cos",
        "host": "macbook",
        "runtime": "codex",
        "iterm_session_id": "iterm-cos",
        "tty": "/dev/ttys001",
        "cli_session_id": "cli-cos",
        "coord_session_id": "coord-cos",
        "coord_agent_id": "mikebook_codex",
    }
    if not controller_visible:
        controller.pop("iterm_session_id")
        controller.pop("tty")
    worker_profile_id = "claude-code" if worker_runtime == "claude" else "codex-cli"
    return RunManifest.from_dict(
        {
            "manifest_id": "test",
            "controller": controller,
            "workers": [
                {
                    "worker_id": "worker",
                    "host": "macbook",
                    "runtime": worker_runtime,
                    "iterm_session_id": "iterm-worker",
                    "tty": "/dev/ttys003",
                    "cli_session_id": "cli-worker",
                    "coord_session_id": "coord-worker",
                    "coord_agent_id": "mikebook_codex",
                    "observation_profile_id": worker_profile_id,
                    "observation_profile_version": 1,
                    "repositories": ["Condor/repo"],
                }
            ],
            "plan_paths": ["/plan"],
            "permitted_repositories": ["Condor/repo"],
            "permitted_actions": ["inspect", "test"],
            "dispatch_transport": transport,
        }
    )


def _manifest_with_colliding_worker(field: str, *, controller_visible: bool = True):
    manifest = _manifest(controller_visible=controller_visible)
    worker = replace(
        manifest.workers[0],
        **{field: getattr(manifest, f"controller_{field}")},
    )
    return replace(manifest, workers=(worker,))


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
            "observation_schema_version": 1,
            "worker_id": "worker",
            "iterm_session_id": "iterm-worker",
            "runtime": "codex",
            "profile_id": "codex-cli",
            "profile_version": 1,
            "prompt_state": "ready",
            "input_buffer_state": "empty",
            "cli_session_id": "cli-worker",
            "coord_session_id": "coord-worker",
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


def _escape_observation():
    captured_hook = _signed_hook(sequence=4, prompt_state="running")
    return replace(
        _visual_observation(),
        runtime_observation=RuntimeObservation.from_dict(
            {
                "runtime": "codex",
                "profile_id": "codex-cli",
                "profile_version": 1,
                "prompt_state": "running",
                "input_buffer_state": "empty",
                "cli_session_id": "cli-worker",
                "coord_session_id": "coord-worker",
            }
        ),
        runtime_hook_digest=_hook_digest(captured_hook),
    )


def _escape_decision(observation, key="interrupt-1", text="must-not-send"):
    return VisualDecision.from_dict(
        {
            "observation_digest": observation.digest(),
            "action": "press_escape",
            "text": "",
            "rationale": "Interrupt once for synthetic urgent delivery",
            "decided_by": "llm:test-supervisor",
            "idempotency_key": key,
            "delivery_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    )


def test_escape_transaction_reobserves_signed_prompt_and_fences_every_byte(monkeypatch, tmp_path):
    before = _signed_hook(sequence=4, prompt_state="running")
    after = _signed_hook(sequence=5, prompt_state="ready")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, before, after, after, after],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    verified = []
    result = asyncio.run(
        dispatch.execute_escape_delivery_transaction(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=_escape_decision(observation, text="URGENT synthetic reference M-test"),
            text="URGENT synthetic reference M-test",
            verify_epoch=lambda resource, epoch: verified.append((resource, epoch)),
            receipts=ReceiptStore(tmp_path / "interrupt.jsonl"),
            **_broker_args(),
            post_attempts=2,
            poll_interval=0,
        )
    )
    assert result["ok"] is False
    assert result["error"] == "recipient acknowledgement required"
    assert target.sent == ["\x1b", "URGENT synthetic reference M-test\r\n"]
    assert (
        verified
        == [
            ("workspace:mikebook:c2-supervisor", 7),
            ("workspace:mikebook:c2-worker:worker", 13),
        ]
        * 2
    )
    assert result["receipt"]["verification_state"] == "submitted-unacknowledged"


def test_escape_transaction_rejects_payload_not_bound_to_decision_before_effect(
    monkeypatch, tmp_path
):
    before = _signed_hook(sequence=4, prompt_state="running")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    with pytest.raises(ContractError, match="differs"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation, text="approved"),
                text="substituted",
                verify_epoch=lambda *_: None,
                receipts=ReceiptStore(tmp_path / "interrupt-payload-mismatch.jsonl"),
                **_broker_args(),
            )
        )
    assert target.sent == []


def test_escape_transaction_rejects_new_same_state_hook_after_visual_capture(monkeypatch, tmp_path):
    captured = _signed_hook(sequence=4, prompt_state="running")
    changed = _signed_hook(sequence=5, prompt_state="running")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[captured, changed],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    with pytest.raises(ContractError, match="changed after visual capture"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=lambda *_: None,
                receipts=ReceiptStore(tmp_path / "interrupt-hook-drift.jsonl"),
                **_broker_args(),
            )
        )
    assert target.sent == []


def test_escape_transaction_rejects_reordered_challenge_binding_before_effect(
    monkeypatch, tmp_path
):
    before = _signed_hook(sequence=4, prompt_state="running")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    broker_args = _broker_args()
    broker_args["create_challenge"] = lambda _request: {
        "challenge_id": "challenge-old",
        "issued_at": 1001.0,
        "binding_sha256": "d" * 64,
    }
    with pytest.raises(ContractError, match="differently bound"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=lambda *_: None,
                receipts=ReceiptStore(tmp_path / "interrupt-reordered-challenge.jsonl"),
                **broker_args,
            )
        )
    assert target.sent == []


@pytest.mark.parametrize("buffer_state", ["unknown", "nonempty"])
def test_escape_transaction_never_sends_text_for_unverified_post_buffer(
    monkeypatch, tmp_path, buffer_state
):
    before = _signed_hook(sequence=4, prompt_state="running")
    after = _signed_hook(sequence=5, prompt_state="ready", input_buffer_state=buffer_state)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, before, after, after],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    result = asyncio.run(
        dispatch.execute_escape_delivery_transaction(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=_escape_decision(observation),
            text="must-not-send",
            verify_epoch=lambda *_: None,
            receipts=ReceiptStore(tmp_path / f"interrupt-{buffer_state}.jsonl"),
            **_broker_args(),
            post_attempts=1,
            poll_interval=0,
        )
    )
    assert result["ok"] is False
    assert "buffer" in result["error"]
    assert target.sent == ["\x1b"]


def test_escape_transaction_rejects_hook_that_predates_escape(monkeypatch, tmp_path):
    before = _signed_hook(sequence=4, prompt_state="running")
    pre_escape = _signed_hook(sequence=5, prompt_state="ready", observed_at=1001.5)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, before, pre_escape, pre_escape],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    broker_args = _broker_args()

    def broker_rejects_causality(report):
        verification = _broker_verifier(report)
        verification["observed_after_arm"] = False
        return verification

    broker_args["verify_hook_authenticity"] = broker_rejects_causality
    result = asyncio.run(
        dispatch.execute_escape_delivery_transaction(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=_escape_decision(observation),
            text="must-not-send",
            verify_epoch=lambda *_: None,
            receipts=ReceiptStore(tmp_path / "interrupt-pre-escape-hook.jsonl"),
            **broker_args,
            post_attempts=1,
            poll_interval=0,
        )
    )
    assert result["ok"] is False
    assert "predates terminal action" in result["error"]
    assert target.sent == ["\x1b"]


def test_escape_transaction_broker_arm_failure_prevents_escape(monkeypatch, tmp_path):
    before = _signed_hook(sequence=4, prompt_state="running")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    receipts = ReceiptStore(tmp_path / "interrupt-arm-failure.jsonl")
    broker_args = _broker_args()

    def fail_arm(_request):
        raise ContractError("coord broker arm unavailable")

    broker_args["arm_challenge"] = fail_arm
    with pytest.raises(ContractError, match="arm unavailable"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=lambda *_: None,
                receipts=receipts,
                **broker_args,
            )
        )
    assert target.sent == []
    assert receipts.records()[-1]["kind"] == "interrupt-delivery-reservation"
    assert receipts.records()[-1]["escape_writes"] == 0


def test_escape_transaction_hook_change_after_epoch_fence_prevents_escape(monkeypatch, tmp_path):
    before = _signed_hook(sequence=4, prompt_state="running")
    changed = _signed_hook(sequence=5, prompt_state="running")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, changed],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    with pytest.raises(ContractError, match="changed after Escape fence"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=lambda *_: None,
                receipts=ReceiptStore(tmp_path / "interrupt-post-fence-drift.jsonl"),
                **_broker_args(),
            )
        )
    assert target.sent == []


def test_escape_transaction_foreground_switch_during_escape_fence_prevents_escape(
    monkeypatch, tmp_path
):
    before = _signed_hook(sequence=4, prompt_state="running")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, before],
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "tty_foreground_group_matches_runtime", lambda *_: False)
    fence_calls = 0

    def switch_during_escape_fence(_resource, _epoch):
        nonlocal fence_calls
        fence_calls += 1
        if fence_calls == 2:
            target.job = "shell"

    observation = _escape_observation()
    with pytest.raises(ContractError, match="lost foreground after Escape fence"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=switch_during_escape_fence,
                receipts=ReceiptStore(tmp_path / "interrupt-escape-fence-race.jsonl"),
                **_broker_args(),
            )
        )
    assert target.sent == []


def test_escape_transaction_reordered_or_duplicate_request_never_repeats_escape(
    monkeypatch, tmp_path
):
    before = _signed_hook(sequence=4, prompt_state="running")
    reordered = _signed_hook(sequence=3, prompt_state="ready")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, before, reordered, reordered],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    receipts = ReceiptStore(tmp_path / "interrupt-duplicate.jsonl")
    kwargs = dict(
        connection=object(),
        manifest=_manifest(),
        observation=observation,
        decision=_escape_decision(observation),
        text="must-not-send",
        verify_epoch=lambda *_: None,
        receipts=receipts,
        **_broker_args(),
        post_attempts=1,
        poll_interval=0,
    )
    first = asyncio.run(dispatch.execute_escape_delivery_transaction(**kwargs))
    assert first["ok"] is False
    assert target.sent == ["\x1b"]
    with pytest.raises(ContractError, match="duplicate"):
        asyncio.run(dispatch.execute_escape_delivery_transaction(**kwargs))
    assert target.sent == ["\x1b"]


def test_escape_transaction_rejects_foreground_mismatch_before_escape(monkeypatch, tmp_path):
    before = _signed_hook(sequence=4, prompt_state="running")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="shell",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before],
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "tty_foreground_group_matches_runtime", lambda *_: False)
    observation = _escape_observation()
    result = asyncio.run(
        dispatch.execute_escape_delivery_transaction(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=_escape_decision(observation),
            text="must-not-send",
            verify_epoch=lambda *_: None,
            receipts=ReceiptStore(tmp_path / "interrupt-foreground.jsonl"),
            **_broker_args(),
        )
    )
    assert result["ok"] is False
    assert "foreground" in result["error"]
    assert target.sent == []


@pytest.mark.parametrize(
    "hook_overrides",
    [
        {"cli_session_id": "stale-cli"},
        {"coord_session_id": "stale-coord"},
        {"iterm_session_id": "reused-iterm"},
    ],
)
def test_escape_transaction_rejects_signed_identity_change_before_escape(
    monkeypatch, tmp_path, hook_overrides
):
    before = _signed_hook(sequence=4, prompt_state="running", **hook_overrides)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    with pytest.raises(ContractError):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=lambda *_: None,
                receipts=ReceiptStore(tmp_path / "interrupt-stale-identity.jsonl"),
                **_broker_args(),
            )
        )
    assert target.sent == []


def test_escape_transaction_lease_loss_after_escape_prevents_next_byte(monkeypatch, tmp_path):
    before = _signed_hook(sequence=4, prompt_state="running")
    after = _signed_hook(sequence=5, prompt_state="ready")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, before, after, after],
    )
    _install_fake_iterm(monkeypatch, [target])
    calls = 0

    def lose_controller_lease(_resource, _epoch):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ContractError("controller lease epoch is no longer live")

    observation = _escape_observation()
    with pytest.raises(ContractError, match="no longer live"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=lose_controller_lease,
                receipts=ReceiptStore(tmp_path / "interrupt-lease-loss.jsonl"),
                **_broker_args(),
                post_attempts=1,
                poll_interval=0,
            )
        )
    assert target.sent == ["\x1b"]


def test_escape_transaction_foreground_loss_after_escape_prevents_text(monkeypatch, tmp_path):
    before = _signed_hook(sequence=4, prompt_state="running")
    after = {
        **_signed_hook(sequence=5, prompt_state="ready"),
        "foregroundJobName": "shell",
        "jobName": "shell",
    }
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, before, after, after],
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "tty_foreground_group_matches_runtime", lambda *_: False)
    observation = _escape_observation()
    result = asyncio.run(
        dispatch.execute_escape_delivery_transaction(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=_escape_decision(observation),
            text="must-not-send",
            verify_epoch=lambda *_: None,
            receipts=ReceiptStore(tmp_path / "interrupt-post-foreground.jsonl"),
            **_broker_args(),
            post_attempts=1,
            poll_interval=0,
        )
    )
    assert result["ok"] is False
    assert "lost terminal foreground" in result["error"]
    assert target.sent == ["\x1b"]


def test_escape_transaction_final_foreground_switch_prevents_atomic_command(monkeypatch, tmp_path):
    before = _signed_hook(sequence=4, prompt_state="running")
    after = _signed_hook(sequence=5, prompt_state="ready")
    final = {**after, "foregroundJobName": "shell", "jobName": "shell"}
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, before, after, final],
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "tty_foreground_group_matches_runtime", lambda *_: False)
    observation = _escape_observation()
    with pytest.raises(ContractError, match="before command write"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=lambda *_: None,
                receipts=ReceiptStore(tmp_path / "interrupt-final-foreground.jsonl"),
                **_broker_args(),
                post_attempts=1,
                poll_interval=0,
            )
        )
    assert target.sent == ["\x1b"]


def test_escape_transaction_foreground_switch_during_final_fence_prevents_command(
    monkeypatch, tmp_path
):
    before = _signed_hook(sequence=4, prompt_state="running")
    after = _signed_hook(sequence=5, prompt_state="ready")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="running",
        snapshots=[before, before, before, before, after, after, after],
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "tty_foreground_group_matches_runtime", lambda *_: False)
    fence_calls = 0

    def switch_during_final_fence(_resource, _epoch):
        nonlocal fence_calls
        fence_calls += 1
        if fence_calls == 4:
            target.job = "shell"

    observation = _escape_observation()
    with pytest.raises(ContractError, match="after final epoch fence"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=switch_during_final_fence,
                receipts=ReceiptStore(tmp_path / "interrupt-final-fence-race.jsonl"),
                **_broker_args(),
                post_attempts=1,
                poll_interval=0,
            )
        )
    assert target.sent == ["\x1b"]


@pytest.mark.parametrize("control", ["\x00", "\x1b", "\t", "\n", "\r", "\x7f"])
def test_escape_transaction_rejects_terminal_controls_before_escape(monkeypatch, tmp_path, control):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        snapshots=[_signed_hook(sequence=4, prompt_state="running")],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    text = f"unsafe{control}text"
    with pytest.raises(ValueError, match="terminal control"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation, text=text),
                text=text,
                verify_epoch=lambda *_: None,
                receipts=ReceiptStore(tmp_path / "interrupt-control.jsonl"),
                **_broker_args(),
            )
        )
    assert target.sent == []


def test_escape_transaction_rejects_text_not_bound_by_decision_before_escape(monkeypatch, tmp_path):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        snapshots=[_signed_hook(sequence=4, prompt_state="running")],
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _escape_observation()
    decision = _escape_decision(observation, text="approved text")
    with pytest.raises(ContractError, match="differs from the LLM decision"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=decision,
                text="substituted text",
                verify_epoch=lambda *_: None,
                receipts=ReceiptStore(tmp_path / "interrupt-text-binding.jsonl"),
                **_broker_args(),
            )
        )
    assert target.sent == []


def test_escape_transaction_rejects_exact_uuid_object_switch_before_command(monkeypatch, tmp_path):
    before = _signed_hook(sequence=4, prompt_state="running")
    after = _signed_hook(sequence=5, prompt_state="ready")
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        snapshots=[before, before, before, before, after],
    )
    replacement = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        snapshots=[after],
    )
    calls = 0

    async def switched_target(_connection, _session_id):
        nonlocal calls
        calls += 1
        return replacement if calls >= 5 else target

    monkeypatch.setattr(dispatch, "find_session_by_id", switched_target)
    observation = _escape_observation()
    with pytest.raises(ContractError, match="target changed"):
        asyncio.run(
            dispatch.execute_escape_delivery_transaction(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=_escape_decision(observation),
                text="must-not-send",
                verify_epoch=lambda *_: None,
                receipts=ReceiptStore(tmp_path / "interrupt-target-switch.jsonl"),
                **_broker_args(),
                post_attempts=1,
                poll_interval=0,
            )
        )
    assert target.sent == ["\x1b"]
    assert replacement.sent == []


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
    assert result["action_applied"] is False
    assert result["key_write_succeeded"] is True
    assert result["observed_presentation"] is False
    assert result["verification_state"] == "pending"
    assert verified == [
        ("workspace:mikebook:c2-supervisor", 7),
        ("workspace:mikebook:c2-worker:worker", 13),
        ("workspace:mikebook:c2-supervisor", 7),
        ("workspace:mikebook:c2-worker:worker", 13),
    ]


@pytest.mark.parametrize(("action", "expected"), [("press_tab", "\t"), ("clear_line", "\x15")])
def test_visual_queue_and_clear_actions_use_fenced_single_character(
    monkeypatch, tmp_path, action, expected
):
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
    decision = VisualDecision.from_dict(
        {
            "observation_digest": observation.digest(),
            "action": action,
            "text": "",
            "rationale": "Fresh visual evidence calls for this bounded key action",
            "decided_by": "llm:test-supervisor",
            "idempotency_key": f"visual-{action}-1",
        }
    )

    result = asyncio.run(
        dispatch.execute_visual_decision(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=decision,
            verify_epoch=lambda *_args: None,
            receipts=ReceiptStore(tmp_path / f"{action}-receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert target.sent == [expected]
    assert result["receipt"]["decision_action"] == action
    assert result["receipt"]["observed_ack"] is False
    assert result["receipt"]["key_write_succeeded"] is True
    assert result["verification_state"] == "pending"


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
    assert result["receipt"]["verification_state"] == "pending"


@pytest.mark.parametrize(
    ("session_kwargs", "error"),
    [
        ({"runtime": ""}, "runtime hook record is missing or unsupported"),
        ({"profile_version": 2}, "runtime hook record is missing or unsupported"),
        ({"input_buffer_state": "nonempty"}, "does not match visual observation"),
        ({"cli_session_id": "reused-cli"}, "does not match visual observation"),
        ({"coord_session_id": "reused-coord"}, "does not match visual observation"),
    ],
)
def test_visual_action_rejects_live_profile_buffer_or_identity_drift(
    monkeypatch, tmp_path, session_kwargs, error
):
    params = {
        "runtime": "codex",
        "job": "codex",
        "session_id": "iterm-worker",
        "cli_session_id": "cli-worker",
        "coord_session_id": "coord-worker",
    }
    params.update(session_kwargs)
    target = FakeSession("/dev/ttys003", **params)
    _install_fake_iterm(monkeypatch, [target])
    observation = _visual_observation()
    verified = []

    result = asyncio.run(
        dispatch.execute_visual_decision(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=_visual_decision(observation),
            verify_epoch=lambda *args: verified.append(args),
            receipts=ReceiptStore(tmp_path / "visual-drift.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is False
    assert error in result["error"]
    assert target.sent == []
    assert verified == []


def test_visual_action_rejects_stale_mirrored_vars_when_authoritative_cache_is_absent(
    monkeypatch, tmp_path
):
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        prompt_state="ready",
        input_buffer_state="empty",
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "load_runtime_hook_record", lambda *_args, **_kwargs: None)
    observation = _visual_observation()
    verified = []

    result = asyncio.run(
        dispatch.execute_visual_decision(
            object(),
            manifest=_manifest(),
            observation=observation,
            decision=_visual_decision(observation),
            verify_epoch=lambda *args: verified.append(args),
            receipts=ReceiptStore(tmp_path / "stale-mirror.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is False
    assert result["error"] == "fresh exact runtime hook record is required"
    assert target.sent == []
    assert verified == []


def test_visual_decision_reservation_prevents_replay_after_key_write_crash(monkeypatch, tmp_path):
    class CrashingSession(FakeSession):
        async def async_send_text(self, payload):
            self.sent.append(payload)
            raise RuntimeError("simulated crash after terminal write")

    target = CrashingSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
    )
    _install_fake_iterm(monkeypatch, [target])
    observation = _visual_observation()
    chosen = _visual_decision(observation)
    store = ReceiptStore(tmp_path / "visual-receipts.jsonl")

    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(
            dispatch.execute_visual_decision(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=chosen,
                verify_epoch=lambda *_args: None,
                receipts=store,
            )
        )
    assert target.sent == ["\r"]
    assert store.has_idempotency_key(chosen.idempotency_key)

    with pytest.raises(ContractError, match="duplicate visual decision"):
        asyncio.run(
            dispatch.execute_visual_decision(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=chosen,
                verify_epoch=lambda *_args: None,
                receipts=store,
            )
        )
    assert target.sent == ["\r"]


def test_visual_decision_rechecks_epochs_after_durable_reservation(monkeypatch, tmp_path):
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
    chosen = _visual_decision(observation)
    store = ReceiptStore(tmp_path / "visual-receipts.jsonl")
    calls = []

    def transfer_after_reservation(resource, epoch):
        calls.append((resource, epoch))
        if len(calls) == 3:
            raise RuntimeError("controller epoch transferred during reservation")

    with pytest.raises(RuntimeError, match="epoch transferred"):
        asyncio.run(
            dispatch.execute_visual_decision(
                object(),
                manifest=_manifest(),
                observation=observation,
                decision=chosen,
                verify_epoch=transfer_after_reservation,
                receipts=store,
            )
        )

    assert store.has_idempotency_key(chosen.idempotency_key)
    assert target.sent == []


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
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=2, prompt_state="running"),
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
            verify_hook_authenticity=_verify_dispatch_hook,
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
    assert result["receipt"]["metrics"]["pre_submit_sequence"] == 1
    assert result["receipt"]["metrics"]["post_submit_sequence"] == 2
    assert result["receipt"]["metrics"]["post_submit_prompt_state"] == "running"
    assert result["receipt"]["metrics"]["post_submit_input_buffer_state"] == "empty"
    assert result["receipt"]["metrics"]["recovery_submitted"] is False


@pytest.mark.parametrize("field", ["cli_session_id", "coord_session_id"])
def test_headless_registered_dispatch_rejects_controller_session_identity_collision(
    field, tmp_path
):
    manifest = _manifest_with_colliding_worker(field, controller_visible=False)
    envelope = replace(_envelope(), **{field: getattr(manifest, f"controller_{field}")})

    with pytest.raises(dispatch.ContractError, match="must not also be registered as a worker"):
        asyncio.run(
            dispatch.dispatch_registered_headless(
                manifest=manifest,
                envelope=envelope,
                verify_epoch=lambda *_args: None,
                receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            )
        )


def test_registered_dispatch_accepts_exact_foreground_runtime_when_iterm_name_drifts(
    monkeypatch, tmp_path
):
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="SkyComputerUseClient",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=2, prompt_state="running"),
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
            verify_hook_authenticity=_verify_dispatch_hook,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is True
    assert target.sent[0].startswith("/goal C2_DISPATCH ")


def test_registered_dispatch_accepts_claude_signed_receipt(monkeypatch, tmp_path):
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="claude",
        job="claude",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(sequence=1, prompt_state="ready", runtime="claude"),
            _dispatch_hook(sequence=2, prompt_state="running", runtime="claude"),
        ],
    )
    _install_fake_iterm(monkeypatch, [target])

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(worker_runtime="claude"),
            envelope=_envelope(),
            verify_epoch=lambda *_args: None,
            verify_hook_authenticity=_verify_dispatch_hook,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is True
    assert result["receipt"]["metrics"]["post_submit_sequence"] == 2


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


def test_controller_poke_accepts_kernel_foreground_runtime_fallback(monkeypatch):
    target = FakeSession(
        "/dev/ttys001",
        runtime="unknown",
        job="",
        session_id="iterm-cos",
        cli_session_id="cli-cos",
        coord_session_id="coord-cos",
        snapshots=[
            {"session.isProcessing": False},
            {"session.isProcessing": True},
        ],
    )
    _install_fake_iterm(monkeypatch, [target])
    monkeypatch.setattr(dispatch, "tty_foreground_group_matches_runtime", lambda *_: True)

    result = asyncio.run(
        dispatch.send_controller_poke(
            object(),
            manifest=_manifest(),
            text="controller wake",
            controller_epoch=7,
            idempotency_key="poke-kernel-fallback",
            verify_epoch=lambda *_: None,
            ack_attempts=1,
        )
    )

    assert result["ok"] is True
    assert target.sent == ["controller wake", "\r", "\n"]


def test_static_active_state_is_not_a_post_dispatch_ack(monkeypatch, tmp_path):
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=1, prompt_state="running"),
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
            verify_hook_authenticity=_verify_dispatch_hook,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is False
    assert result["error"] == "registered target did not acknowledge dispatch"
    assert result["receipt"]["observed_ack"] is False
    assert result["receipt"]["metrics"]["pre_submit_sequence"] == 1
    assert result["receipt"]["metrics"]["post_submit_sequence"] is None
    assert result["receipt"]["metrics"]["recovery_submitted"] is True
    assert target.sent == [target.sent[0], "\r", "\n", "\r"]
    assert verified == [
        ("workspace:mikebook:c2-supervisor", 7),
        ("workspace:mikebook:c2-supervisor", 7),
    ]


def test_tab_dispatch_fences_worker_reservation_before_each_injection(monkeypatch, tmp_path):
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=2, prompt_state="running"),
        ],
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
            verify_hook_authenticity=_verify_dispatch_hook,
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
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=2, prompt_state="running"),
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
            verify_hook_authenticity=_verify_dispatch_hook,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["receipt"]["observed_ack"] is True
    assert result["receipt"]["metrics"]["post_submit_sequence"] == 2
    assert result["receipt"]["metrics"]["recovery_submitted"] is True
    assert target.sent[-3:] == ["\r", "\n", "\r"]
    assert verified == [
        ("workspace:mikebook:c2-supervisor", 7),
        ("workspace:mikebook:c2-supervisor", 7),
    ]


def test_true_start_prevents_recovery_and_duplicate_submit(monkeypatch, tmp_path):
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=2, prompt_state="running"),
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
            verify_hook_authenticity=_verify_dispatch_hook,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["receipt"]["observed_ack"] is True
    assert result["receipt"]["metrics"]["post_submit_sequence"] == 2
    assert result["receipt"]["metrics"]["recovery_submitted"] is False
    assert len(target.sent) == 3
    assert verified == [("workspace:mikebook:c2-supervisor", 7)]


def test_start_during_recovery_reread_suppresses_fallback(monkeypatch, tmp_path):
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=2, prompt_state="running"),
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
            verify_hook_authenticity=_verify_dispatch_hook,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["receipt"]["observed_ack"] is True
    assert result["receipt"]["metrics"]["post_submit_sequence"] == 2
    assert result["receipt"]["metrics"]["recovery_submitted"] is False
    assert len(target.sent) == 3
    assert verified == [("workspace:mikebook:c2-supervisor", 7)]


def test_registered_dispatch_rejects_foreground_helper(monkeypatch, tmp_path):
    _freeze_dispatch_clock(monkeypatch)
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
            verify_hook_authenticity=_verify_dispatch_hook,
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
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="SkyComputerUseClient",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(sequence=1, prompt_state="ready"),
            _dispatch_hook(sequence=2, prompt_state="running"),
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
            verify_hook_authenticity=_verify_dispatch_hook,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is True
    assert target.sent[-2:] == ["\r", "\n"]


def test_registered_dispatch_rejects_missing_authoritative_signed_observation(
    monkeypatch, tmp_path
):
    _freeze_dispatch_clock(monkeypatch)
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

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda *_args: None,
            verify_hook_authenticity=_verify_dispatch_hook,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is False
    assert result["error"] == "trusted runtime observation variables are missing or unsupported"


@pytest.mark.parametrize(
    ("input_buffer_state", "expected_error"),
    [
        ("unknown", "terminal action requires a verified empty input buffer"),
        ("nonempty", "terminal action requires a verified empty input buffer"),
    ],
)
def test_registered_dispatch_rejects_unready_pre_submit_observation(
    monkeypatch, tmp_path, input_buffer_state, expected_error
):
    _freeze_dispatch_clock(monkeypatch)
    target = FakeSession(
        "/dev/ttys003",
        runtime="codex",
        job="codex",
        session_id="iterm-worker",
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        snapshots=[
            _dispatch_hook(
                sequence=1,
                prompt_state="ready",
                input_buffer_state=input_buffer_state,
            )
        ],
    )
    _install_fake_iterm(monkeypatch, [target])

    result = asyncio.run(
        dispatch.dispatch_registered(
            object(),
            manifest=_manifest(),
            envelope=_envelope(),
            verify_epoch=lambda *_args: None,
            verify_hook_authenticity=_verify_dispatch_hook,
            receipts=ReceiptStore(tmp_path / "receipts.jsonl"),
            ack_attempts=1,
        )
    )

    assert result["ok"] is False
    assert result["error"] == expected_error
    assert target.sent == []


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
            verify_hook_authenticity=_verify_dispatch_hook,
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


def test_controller_poke_applescript_rejects_headless_controller():
    result = dispatch.send_controller_poke_applescript(
        manifest=_manifest(controller_visible=False),
        text="/goal wake up",
        controller_epoch=7,
        idempotency_key="poke-1",
        verify_epoch=lambda *_args, **_kwargs: None,
        run=lambda *_args, **_kwargs: None,
    )

    assert result["ok"] is False
    assert "headless controller has no iTerm session" in result["error"]


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
