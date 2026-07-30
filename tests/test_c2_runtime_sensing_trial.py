from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "runtime_sensing_comparison_v1.json"
BLIND_RESPONSE_PATH = ROOT / "tests" / "fixtures" / "runtime_sensing_independent_response_v1.json"
sys.path.insert(0, str(SCRIPTS))

import c2_runtime_sensing_trial as trial  # noqa: E402


def fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def complete_responses(value, *, independent="truth"):
    packet = trial.render_packet(value)
    artifact = trial.response_template(packet, value)
    truth_by_ref = {
        trial.case_reference(packet["fixture_sha256"], case["case_id"]): case["truth"]
        for case in value["cases"]
    }
    for response in artifact["responses"]:
        if response["candidate_id"] != "independent-observer-prototype":
            continue
        if independent == "truth":
            response.update(truth_by_ref[response["case_ref"]])
        elif independent == "ready-empty":
            response.update(prompt_state="ready", input_buffer_state="empty")
    return packet, artifact


def test_fixture_is_explicitly_synthetic_and_covers_both_runtimes():
    value = fixture()
    trial.validate_fixture(value)
    assert value["provenance"]["kind"] == "synthetic-design-vectors"
    assert value["provenance"]["live_evidence"] is False
    assert {case["runtime"] for case in value["cases"]} == {"codex", "claude"}
    assert len(value["cases"]) == 12


def test_blinded_packet_is_deterministic_and_contains_no_answer_key():
    value = fixture()
    first = trial.render_packet(value)
    second = trial.render_packet(value)
    assert first == second
    serialized = json.dumps(first, sort_keys=True)
    assert '"truth"' not in serialized
    assert '"case_id"' not in serialized
    assert "ready-empty" not in serialized
    assert "needs-input" not in serialized
    assert "staged-input" not in serialized
    assert "shell-fallback" not in serialized
    assert "prompt_state" not in serialized
    assert "input_buffer_state" not in serialized
    assert first["live_evidence"] is False
    assert first["fixture_sha256"] == trial.content_digest(value)


def test_response_template_rejects_rebound_or_answer_bearing_packet():
    value = fixture()
    packet = trial.render_packet(value)
    packet["truth"] = {"leak": True}
    with pytest.raises(trial.SensingTrialError, match="exact blinded"):
        trial.response_template(packet, value)


def test_response_template_hides_the_control_and_leaves_treatment_blank():
    value = fixture()
    packet = trial.render_packet(value)
    artifact = trial.response_template(packet, value)
    assert {item["candidate_id"] for item in packet["candidates"]} == {
        "independent-observer-prototype"
    }
    assert len(artifact["responses"]) == 12
    assert all(item["candidate_id"] != trial.BASELINE_CANDIDATE for item in artifact["responses"])
    assert all(item["prompt_state"] == "" for item in artifact["responses"])
    assert all(item["input_buffer_state"] == "" for item in artifact["responses"])


def test_scorer_reports_safety_and_coverage_without_selecting_policy():
    value = fixture()
    _packet, responses = complete_responses(value)
    result = trial.score_responses(value, responses)
    independent = result["by_candidate"]["independent-observer-prototype"]
    baseline = result["by_candidate"][trial.BASELINE_CANDIDATE]

    assert independent["overall"] == {
        "total": 12,
        "exact": 12,
        "prompt_correct": 12,
        "input_correct": 12,
        "false_ready": 0,
        "false_empty": 0,
        "unknown_prompt": 2,
        "unknown_input": 2,
        "actionable_predictions": 4,
        "unsafe_actionable": 0,
        "known_both": 10,
        "coverage": 10 / 12,
    }
    assert baseline["overall"]["false_ready"] == 0
    assert baseline["overall"]["false_empty"] == 0
    assert baseline["overall"]["unknown_input"] == 12
    assert baseline["overall"]["coverage"] == 0.0
    assert set(independent["by_runtime"]) == {"codex", "claude"}
    assert result["preferred_candidate"] is None
    assert result["operator_judgment_required"] is True
    assert result["test_2_passed"] is False
    assert result["delivery_activated"] is False
    assert result["live_evidence"] is False


def test_scorer_exposes_unsafe_actionable_predictions():
    value = fixture()
    _packet, responses = complete_responses(value, independent="ready-empty")
    metrics = trial.score_responses(value, responses)["by_candidate"][
        "independent-observer-prototype"
    ]["overall"]
    assert metrics["false_ready"] == 6
    assert metrics["false_empty"] == 4
    assert metrics["unsafe_actionable"] == 8
    assert metrics["actionable_predictions"] == 12


def test_frozen_opaque_blind_response_preserves_non_promotional_result():
    value = fixture()
    responses = json.loads(BLIND_RESPONSE_PATH.read_text(encoding="utf-8"))
    result = trial.score_responses(value, responses)
    metrics = result["by_candidate"]["independent-observer-prototype"]["overall"]
    assert result["response_sha256"] == (
        "e672322ec61e689cfd26d1ad83f4d95383be8052c4e54826eee69c314948f357"
    )
    assert metrics["prompt_correct"] == 12
    assert metrics["input_correct"] == 8
    assert metrics["coverage"] == 0.5
    assert metrics["false_ready"] == metrics["false_empty"] == 0
    assert metrics["unsafe_actionable"] == 0
    assert result["live_evidence"] is False
    assert result["test_2_passed"] is False
    assert result["delivery_activated"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item["responses"].pop(), "every candidate/case"),
        (lambda item: item["responses"].append(deepcopy(item["responses"][0])), "duplicated"),
        (
            lambda item: item["responses"][0].update(candidate_id="unknown"),
            "unknown coordinate",
        ),
        (
            lambda item: item["responses"][0].update(prompt_state="idle"),
            "prompt_state",
        ),
        (
            lambda item: item["responses"][0].update(input_buffer_state="maybe"),
            "input_buffer_state",
        ),
        (lambda item: item.update(fixture_sha256="0" * 64), "not bound"),
        (lambda item: item.update(experiment_id="another"), "another experiment"),
        (lambda item: item.update(truth={"leak": True}), "fields do not match"),
        (lambda item: item.update(control_predictions=[]), "fields do not match"),
    ],
)
def test_scorer_rejects_incomplete_duplicate_unknown_or_rebound_responses(mutation, message):
    value = fixture()
    _packet, responses = complete_responses(value)
    mutation(responses)
    with pytest.raises(trial.SensingTrialError, match=message):
        trial.score_responses(value, responses)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["provenance"].update(live_evidence=True),
            "must not claim live",
        ),
        (
            lambda value: value["candidates"].append(deepcopy(value["candidates"][0])),
            "candidate identities",
        ),
        (
            lambda value: value["cases"].append(deepcopy(value["cases"][0])),
            "case identities",
        ),
        (
            lambda value: value["cases"][0]["observation"].update(is_processing=1),
            "is_processing",
        ),
        (
            lambda value: value["cases"][0]["truth"].update(prompt_state="idle"),
            "truth prompt_state",
        ),
        (
            lambda value: value["cases"][0].update(profile_id="claude-code"),
            "profile does not match",
        ),
    ],
)
def test_fixture_rejects_false_provenance_duplicates_and_malformed_states(mutate, message):
    value = fixture()
    mutate(value)
    with pytest.raises(trial.SensingTrialError, match=message):
        trial.validate_fixture(value)


def test_cli_render_is_local_blinded_and_bound():
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / "c2_runtime_sensing_trial.py"), "render"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    packet = json.loads(completed.stdout)
    assert packet["fixture_sha256"] == trial.content_digest(fixture())
    assert '"truth"' not in completed.stdout
    assert packet["response_template"]["schema"] == trial.RESPONSE_SCHEMA
