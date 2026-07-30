from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from c2_contract import ContractError, RunManifest  # noqa: E402
from c2_visual_decision import VisualDecision, VisualObservation  # noqa: E402
from test_c2_contract import manifest_dict  # noqa: E402


def observation(**overrides):
    value = {
        "observation_schema_version": 1,
        "worker_id": "worker-codex",
        "iterm_session_id": "iterm-worker",
        "runtime": "codex",
        "profile_id": "codex-cli",
        "profile_version": 1,
        "prompt_state": "ready",
        "input_buffer_state": "empty",
        "cli_session_id": "cli-worker",
        "coord_session_id": "coord-worker",
        "screenshot_sha256": "a" * 64,
        "captured_ts": 1000.0,
        "summary": "Interactive runtime choice is blocking the worker",
        "controller_epoch": 7,
        "worker_epoch": 11,
    }
    value.update(overrides)
    return VisualObservation.from_dict(value)


def decision(seen: VisualObservation, **overrides):
    value = {
        "observation_digest": seen.digest(),
        "action": "press_enter",
        "text": "",
        "rationale": "The selected option safely continues the registered worker",
        "decided_by": "llm:gpt-5.6-sol",
        "idempotency_key": "visual-decision-1",
    }
    value.update(overrides)
    return VisualDecision.from_dict(value)


def test_llm_visual_decision_binds_fresh_registered_observation():
    manifest = RunManifest.from_dict(manifest_dict())
    seen = observation()
    chosen = decision(seen)

    seen.validate_for(manifest, now_ts=1050.0)
    chosen.validate_for(seen)
    assert chosen.terminal_text() == "\r"


def test_visual_observation_rejects_stale_or_wrong_target():
    manifest = RunManifest.from_dict(manifest_dict())
    with pytest.raises(ContractError, match="stale"):
        observation().validate_for(manifest, now_ts=1201.0)
    with pytest.raises(ContractError, match="stale iTerm identity"):
        observation(iterm_session_id="reused-session").validate_for(manifest, now_ts=1050.0)


def test_visual_observation_rejects_legacy_unprofiled_registration():
    value = manifest_dict()
    value["workers"][0].pop("observation_profile_id")
    value["workers"][0].pop("observation_profile_version")
    manifest = RunManifest.from_dict(value)
    with pytest.raises(ContractError, match="no enrolled runtime observation profile"):
        observation().validate_for(manifest, now_ts=1050.0)


def test_visual_observation_requires_worker_fencing_epoch():
    with pytest.raises(ContractError, match="worker_epoch"):
        observation(worker_epoch=0)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("runtime", "claude", "unsupported runtime observation profile"),
        ("profile_version", 2, "unsupported runtime observation profile"),
        ("cli_session_id", "wrong-cli", "stale cli"),
        ("coord_session_id", "wrong-coord", "stale coord"),
    ],
)
def test_visual_observation_rejects_profile_or_identity_mismatch(field, value, error):
    manifest = RunManifest.from_dict(manifest_dict())
    with pytest.raises(ContractError, match=error):
        observation(**{field: value}).validate_for(manifest, now_ts=1050.0)


@pytest.mark.parametrize("input_buffer_state", ["unknown", "nonempty"])
def test_visual_decision_blocks_unverified_or_nonempty_input(input_buffer_state):
    seen = observation(input_buffer_state=input_buffer_state)
    with pytest.raises(ContractError, match="verified empty input buffer"):
        decision(seen).validate_for(seen)


def test_escape_requires_running_profile_and_empty_buffer():
    ready = observation(prompt_state="ready")
    with pytest.raises(ContractError, match="verified running"):
        decision(ready, action="press_escape").validate_for(ready)
    running = observation(prompt_state="running")
    decision(running, action="press_escape").validate_for(running)


@pytest.mark.parametrize("captured_ts", [float("nan"), float("inf"), float("-inf")])
def test_visual_observation_rejects_non_finite_timestamp(captured_ts):
    with pytest.raises(ContractError, match="finite"):
        observation(captured_ts=captured_ts)


def test_visual_decision_rejects_unbound_or_non_llm_action():
    seen = observation()
    with pytest.raises(ContractError, match="does not bind"):
        decision(seen, observation_digest="b" * 64).validate_for(seen)
    with pytest.raises(ContractError, match="supervising LLM"):
        decision(seen, decided_by="hard-coded-rule")


def test_visual_action_surface_is_bounded():
    seen = observation()
    with pytest.raises(ContractError, match="unsupported"):
        decision(seen, action="run_shell")
    with pytest.raises(ContractError, match="cannot include text"):
        decision(seen, text="unexpected")


@pytest.mark.parametrize(
    ("action", "terminal_text"),
    [
        ("press_enter", "\r"),
        ("press_escape", "\x1b"),
        ("press_tab", "\t"),
        ("clear_line", "\x15"),
    ],
)
def test_visual_key_actions_map_to_one_bounded_character(action, terminal_text):
    seen = observation()

    assert decision(seen, action=action).terminal_text() == terminal_text


@pytest.mark.parametrize("action", ["press_enter", "press_escape", "press_tab", "clear_line"])
def test_visual_key_actions_reject_attached_text(action):
    seen = observation()

    with pytest.raises(ContractError, match="cannot include text"):
        decision(seen, action=action, text="smuggled payload")


def test_interrupt_decision_binds_exact_delivery_text_digest():
    seen = observation(prompt_state="running")
    text = "FLASH synthetic message M-test"
    chosen = decision(
        seen,
        action="press_escape",
        delivery_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    assert chosen.validate_delivery_text(text) == hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(ContractError, match="differs"):
        chosen.validate_delivery_text("different message")
    with pytest.raises(ContractError, match="does not bind"):
        decision(seen, action="press_escape").validate_delivery_text(text)


@pytest.mark.parametrize("text", ["one\ntwo", "one\rtwo", "\x03", "\x1b", "\x7f"])
def test_visual_send_text_rejects_terminal_control_characters(text):
    seen = observation()
    with pytest.raises(ContractError, match="control characters"):
        decision(seen, action="send_text", text=text)
