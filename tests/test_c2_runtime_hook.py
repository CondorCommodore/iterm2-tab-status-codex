from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from c2_contract import ContractError  # noqa: E402
from c2_runtime_hook import (  # noqa: E402
    HOOK_SCHEMA_VERSION,
    SignedRuntimeHookObservation,
    session_variable_values,
)
from c2_runtime_observation import RuntimeObservation  # noqa: E402

BROKER_KEY = b"test-only-broker-key" * 2


def broker_signature(observation: SignedRuntimeHookObservation) -> str:
    return hmac.new(BROKER_KEY, observation.canonical_bytes(), hashlib.sha256).hexdigest()


def broker_verifier(report):
    claimed = dict(report)
    signature = claimed.pop("signature", "")
    canonical = json.dumps(claimed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    valid = hmac.compare_digest(
        signature, hmac.new(BROKER_KEY, canonical, hashlib.sha256).hexdigest()
    )
    return {
        "verified": valid,
        "observation_digest": hashlib.sha256(canonical).hexdigest(),
    }


def signed(runtime="codex", profile_id="codex-cli", **overrides):
    observation = SignedRuntimeHookObservation(
        hook_schema_version=HOOK_SCHEMA_VERSION,
        runtime_observation=RuntimeObservation.from_dict(
            {
                "runtime": runtime,
                "profile_id": profile_id,
                "profile_version": 1,
                "prompt_state": overrides.pop("prompt_state", "ready"),
                "input_buffer_state": overrides.pop("input_buffer_state", "empty"),
                "cli_session_id": "cli-worker",
                "coord_session_id": "coord-worker",
            }
        ),
        iterm_session_id="iterm-worker",
        sequence=overrides.pop("sequence", 4),
        observed_at=overrides.pop("observed_at", 1000.0),
        event_id=overrides.pop("event_id", "event-4"),
        challenge_id=overrides.pop("challenge_id", ""),
        signature="",
    )
    assert not overrides
    return replace(observation, signature=broker_signature(observation))


@pytest.mark.parametrize(
    ("runtime", "profile"), [("codex", "codex-cli"), ("claude", "claude-code")]
)
def test_broker_authenticated_hook_round_trips_both_runtime_profiles(runtime, profile):
    proof = signed(runtime, profile)
    values = {f"user.{key}": value for key, value in session_variable_values(proof).items()}
    parsed = SignedRuntimeHookObservation.from_session_variables(values)
    parsed.verify(
        broker_verifier,
        runtime=runtime,
        profile_id=profile,
        profile_version=1,
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        iterm_session_id="iterm-worker",
        now_ts=1001.0,
    )
    assert parsed == proof


def test_hook_rejects_forgery_stale_identity_and_reordered_sequence():
    proof = signed()
    forged = replace(proof, event_id="forged")
    with pytest.raises(ContractError, match="broker rejected"):
        forged.verify(
            broker_verifier,
            runtime="codex",
            profile_id="codex-cli",
            profile_version=1,
            cli_session_id="cli-worker",
            coord_session_id="coord-worker",
            iterm_session_id="iterm-worker",
            now_ts=1001.0,
        )
    with pytest.raises(ContractError, match="stale iTerm"):
        proof.verify(
            broker_verifier,
            runtime="codex",
            profile_id="codex-cli",
            profile_version=1,
            cli_session_id="cli-worker",
            coord_session_id="coord-worker",
            iterm_session_id="reused",
            now_ts=1001.0,
        )
    with pytest.raises(ContractError, match="stale"):
        proof.verify(
            broker_verifier,
            runtime="codex",
            profile_id="codex-cli",
            profile_version=1,
            cli_session_id="cli-worker",
            coord_session_id="coord-worker",
            iterm_session_id="iterm-worker",
            now_ts=1100.0,
        )
    with pytest.raises(ContractError, match="did not advance"):
        proof.verify(
            broker_verifier,
            runtime="codex",
            profile_id="codex-cli",
            profile_version=1,
            cli_session_id="cli-worker",
            coord_session_id="coord-worker",
            iterm_session_id="iterm-worker",
            now_ts=1001.0,
            after_sequence=4,
        )


def test_hook_requires_causal_challenge_and_action_timestamp():
    proof = signed(challenge_id="challenge-1", observed_at=1002.0)
    proof.verify(
        broker_verifier,
        runtime="codex",
        profile_id="codex-cli",
        profile_version=1,
        cli_session_id="cli-worker",
        coord_session_id="coord-worker",
        iterm_session_id="iterm-worker",
        now_ts=1003.0,
        min_observed_at=1001.0,
        expected_challenge_id="challenge-1",
    )
    with pytest.raises(ContractError, match="predates"):
        proof.verify(
            broker_verifier,
            runtime="codex",
            profile_id="codex-cli",
            profile_version=1,
            cli_session_id="cli-worker",
            coord_session_id="coord-worker",
            iterm_session_id="iterm-worker",
            now_ts=1003.0,
            min_observed_at=1002.1,
        )
    with pytest.raises(ContractError, match="active challenge"):
        proof.verify(
            broker_verifier,
            runtime="codex",
            profile_id="codex-cli",
            profile_version=1,
            cli_session_id="cli-worker",
            coord_session_id="coord-worker",
            iterm_session_id="iterm-worker",
            now_ts=1003.0,
            expected_challenge_id="different",
        )


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_hook_rejects_non_finite_timestamp_before_broker_call(value):
    values = {f"user.{key}": item for key, item in session_variable_values(signed()).items()}
    values["user.workerHookObservedAt"] = value
    with pytest.raises(ContractError, match="finite"):
        SignedRuntimeHookObservation.from_session_variables(values)
