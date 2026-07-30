from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from c2_contract import ContractError  # noqa: E402
from c2_runtime_observation import RuntimeObservation  # noqa: E402


@pytest.mark.parametrize(
    ("runtime", "profile_id"),
    [("codex", "codex-cli"), ("claude", "claude-code")],
)
def test_supported_runtime_profiles_require_exact_identity(runtime, profile_id):
    observed = RuntimeObservation.from_dict(
        {
            "runtime": runtime,
            "profile_id": profile_id,
            "profile_version": 1,
            "prompt_state": "ready",
            "input_buffer_state": "empty",
            "cli_session_id": f"cli-{runtime}",
            "coord_session_id": f"coord-{runtime}",
        }
    )

    assert observed.prompt_ready is True
    observed.validate_registration(
        runtime=runtime,
        profile_id=profile_id,
        profile_version=1,
        cli_session_id=f"cli-{runtime}",
        coord_session_id=f"coord-{runtime}",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"runtime": "codex", "profile_id": "claude-code"},
        {"runtime": "claude", "profile_id": "codex-cli"},
        {"profile_version": 2},
        {"profile_version": "1.0"},
    ],
)
def test_unknown_or_cross_runtime_profile_fails_closed(overrides):
    value = {
        "runtime": "codex",
        "profile_id": "codex-cli",
        "profile_version": 1,
        "prompt_state": "ready",
        "input_buffer_state": "empty",
        "cli_session_id": "cli",
        "coord_session_id": "coord",
    }
    value.update(overrides)
    with pytest.raises(ContractError):
        RuntimeObservation.from_dict(value)


def test_missing_hook_variables_never_create_a_trusted_observation():
    assert (
        RuntimeObservation.from_session_variables(
            {
                "user.workerRuntime": "codex",
                "user.workerReadiness": "ready",
            }
        )
        is None
    )

