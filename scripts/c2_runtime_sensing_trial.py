#!/usr/bin/env python3
"""Render and score a blinded, observation-only runtime sensing comparison.

The helper has no terminal, coordination, network, enrollment, installation, or
policy authority. Synthetic fixtures exercise the experiment contract; they are
not evidence that a live Codex or Claude terminal can be sensed safely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from cos_iterm_daemon import classify_readiness

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "runtime_sensing_comparison_v1.json"
FIXTURE_SCHEMA = "cos.runtime-sensing-comparison.fixture.v1"
PACKET_SCHEMA = "cos.runtime-sensing-comparison.packet.v1"
RESPONSE_SCHEMA = "cos.runtime-sensing-comparison.responses.v1"
RESULT_SCHEMA = "cos.runtime-sensing-comparison.result.v1"
PROMPT_STATES = {"ready", "running", "needs_input", "unknown"}
INPUT_STATES = {"empty", "nonempty", "unknown"}
RUNTIMES = {"codex", "claude"}
PROFILE_BY_RUNTIME = {"codex": "codex-cli", "claude": "claude-code"}
BASELINE_CANDIDATE = "existing-display-heuristic"


class SensingTrialError(ValueError):
    """Raised when an experiment artifact is malformed or rebound."""


def content_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SensingTrialError(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise SensingTrialError("JSON artifact must be an object")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SensingTrialError(f"{name} is required")
    return value.strip()


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise SensingTrialError(f"{name} fields do not match schema")


def validate_fixture(fixture: Mapping[str, Any]) -> None:
    _exact_keys(
        fixture,
        {"schema", "experiment_id", "provenance", "candidates", "cases"},
        "fixture",
    )
    if fixture.get("schema") != FIXTURE_SCHEMA:
        raise SensingTrialError("unsupported runtime sensing fixture schema")
    _required_text(fixture.get("experiment_id"), "experiment_id")
    provenance = fixture.get("provenance")
    if not isinstance(provenance, dict):
        raise SensingTrialError("fixture provenance must be an object")
    _exact_keys(provenance, {"kind", "live_evidence", "notice"}, "provenance")
    if provenance.get("kind") != "synthetic-design-vectors":
        raise SensingTrialError("fixture provenance kind is unsupported")
    if provenance.get("live_evidence") is not False:
        raise SensingTrialError("bundled fixture must not claim live evidence")
    _required_text(provenance.get("notice"), "provenance.notice")

    candidates = fixture.get("candidates")
    cases = fixture.get("cases")
    if not isinstance(candidates, list) or len(candidates) < 2:
        raise SensingTrialError("fixture requires at least two sensing candidates")
    if not isinstance(cases, list) or not cases:
        raise SensingTrialError("fixture requires sensing cases")
    candidate_ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise SensingTrialError("candidate must be an object")
        _exact_keys(candidate, {"candidate_id", "description"}, "candidate")
        candidate_ids.append(_required_text(candidate.get("candidate_id"), "candidate_id"))
        _required_text(candidate.get("description"), "candidate.description")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise SensingTrialError("candidate identities must be unique")
    if BASELINE_CANDIDATE not in candidate_ids:
        raise SensingTrialError("fixture lacks the existing display heuristic control")

    case_ids: list[str] = []
    seen_runtimes: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise SensingTrialError("case must be an object")
        _exact_keys(
            case,
            {"case_id", "runtime", "profile_id", "profile_version", "observation", "truth"},
            "case",
        )
        case_ids.append(_required_text(case.get("case_id"), "case_id"))
        runtime = _required_text(case.get("runtime"), "case.runtime")
        if runtime not in RUNTIMES:
            raise SensingTrialError("case runtime is unsupported")
        seen_runtimes.add(runtime)
        profile_id = _required_text(case.get("profile_id"), "case.profile_id")
        if profile_id != PROFILE_BY_RUNTIME[runtime]:
            raise SensingTrialError("case profile does not match runtime")
        if case.get("profile_version") != 1:
            raise SensingTrialError("case profile_version must be 1")
        observation = case.get("observation")
        truth = case.get("truth")
        if not isinstance(observation, dict) or not isinstance(truth, dict):
            raise SensingTrialError("case observation and truth must be objects")
        _exact_keys(observation, {"screen_tail", "is_processing", "scenario"}, "observation")
        if not isinstance(observation.get("screen_tail"), str):
            raise SensingTrialError("observation.screen_tail must be text")
        processing = observation.get("is_processing")
        if processing is not None and not isinstance(processing, bool):
            raise SensingTrialError("observation.is_processing must be boolean or null")
        _required_text(observation.get("scenario"), "observation.scenario")
        _exact_keys(truth, {"prompt_state", "input_buffer_state"}, "truth")
        if truth.get("prompt_state") not in PROMPT_STATES:
            raise SensingTrialError("truth prompt_state is unsupported")
        if truth.get("input_buffer_state") not in INPUT_STATES:
            raise SensingTrialError("truth input_buffer_state is unsupported")
    if len(case_ids) != len(set(case_ids)):
        raise SensingTrialError("case identities must be unique")
    if seen_runtimes != RUNTIMES:
        raise SensingTrialError("fixture must cover both Codex and Claude")


def _blinded_order(values: list[dict[str, Any]], digest: str, domain: str) -> list[dict[str, Any]]:
    return sorted(
        values,
        key=lambda value: hashlib.sha256(
            f"{digest}:{domain}:{json.dumps(value, sort_keys=True)}".encode()
        ).digest(),
    )


def case_reference(fixture_sha256: str, case_id: str) -> str:
    return "case-" + hashlib.sha256(f"{fixture_sha256}:case:{case_id}".encode()).hexdigest()[:20]


def render_packet(fixture: Mapping[str, Any]) -> dict[str, Any]:
    validate_fixture(fixture)
    digest = content_digest(fixture)
    cases = []
    for case in fixture["cases"]:
        cases.append(
            {
                "case_ref": case_reference(digest, case["case_id"]),
                "runtime": case["runtime"],
                "profile_id": case["profile_id"],
                "profile_version": case["profile_version"],
                "observation": case["observation"],
            }
        )
    return {
        "schema": PACKET_SCHEMA,
        "experiment_id": fixture["experiment_id"],
        "fixture_sha256": digest,
        "live_evidence": False,
        "notice": fixture["provenance"]["notice"],
        "instructions": (
            "Classify prompt and input-buffer state for every candidate/case coordinate. "
            "Use unknown whenever the supplied evidence cannot prove a state."
        ),
        "candidates": _blinded_order(
            [
                candidate
                for candidate in fixture["candidates"]
                if candidate["candidate_id"] != BASELINE_CANDIDATE
            ],
            digest,
            "candidates",
        ),
        "cases": _blinded_order(cases, digest, "cases"),
    }


def _baseline_prediction(case: Mapping[str, Any]) -> tuple[str, str]:
    observation = case["observation"]
    display_state = classify_readiness(
        text=observation["screen_tail"],
        is_processing=observation["is_processing"],
    )
    prompt_state = display_state if display_state in PROMPT_STATES else "unknown"
    # The existing display heuristic has no trusted input-buffer signal.
    return prompt_state, "unknown"


def response_template(packet: Mapping[str, Any], fixture: Mapping[str, Any]) -> dict[str, Any]:
    validate_fixture(fixture)
    if packet != render_packet(fixture):
        raise SensingTrialError("packet is not the exact blinded fixture projection")
    responses = []
    for candidate in packet["candidates"]:
        for case in packet["cases"]:
            responses.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "case_ref": case["case_ref"],
                    "prompt_state": "",
                    "input_buffer_state": "",
                }
            )
    return {
        "schema": RESPONSE_SCHEMA,
        "experiment_id": fixture["experiment_id"],
        "fixture_sha256": packet["fixture_sha256"],
        "responses": responses,
    }


def _empty_metrics() -> dict[str, int | float]:
    return {
        "total": 0,
        "exact": 0,
        "prompt_correct": 0,
        "input_correct": 0,
        "false_ready": 0,
        "false_empty": 0,
        "unknown_prompt": 0,
        "unknown_input": 0,
        "actionable_predictions": 0,
        "unsafe_actionable": 0,
        "known_both": 0,
        "coverage": 0.0,
    }


def _add_result(
    metrics: dict[str, int | float],
    predicted: Mapping[str, str],
    truth: Mapping[str, str],
) -> None:
    prompt = predicted["prompt_state"]
    input_state = predicted["input_buffer_state"]
    metrics["total"] += 1
    metrics["prompt_correct"] += int(prompt == truth["prompt_state"])
    metrics["input_correct"] += int(input_state == truth["input_buffer_state"])
    metrics["exact"] += int(
        prompt == truth["prompt_state"] and input_state == truth["input_buffer_state"]
    )
    metrics["false_ready"] += int(prompt == "ready" and truth["prompt_state"] != "ready")
    metrics["false_empty"] += int(input_state == "empty" and truth["input_buffer_state"] != "empty")
    metrics["unknown_prompt"] += int(prompt == "unknown")
    metrics["unknown_input"] += int(input_state == "unknown")
    actionable = prompt == "ready" and input_state == "empty"
    truth_actionable = truth["prompt_state"] == "ready" and truth["input_buffer_state"] == "empty"
    metrics["actionable_predictions"] += int(actionable)
    metrics["unsafe_actionable"] += int(actionable and not truth_actionable)
    metrics["known_both"] += int(prompt != "unknown" and input_state != "unknown")


def _finish_metrics(metrics: dict[str, int | float]) -> None:
    total = int(metrics["total"])
    metrics["coverage"] = int(metrics["known_both"]) / total if total else 0.0


def score_responses(
    fixture: Mapping[str, Any], response_artifact: Mapping[str, Any]
) -> dict[str, Any]:
    validate_fixture(fixture)
    _exact_keys(
        response_artifact,
        {"schema", "experiment_id", "fixture_sha256", "responses"},
        "response artifact",
    )
    if response_artifact.get("schema") != RESPONSE_SCHEMA:
        raise SensingTrialError("unsupported runtime sensing response schema")
    if response_artifact.get("experiment_id") != fixture["experiment_id"]:
        raise SensingTrialError("responses target another experiment")
    digest = content_digest(fixture)
    if response_artifact.get("fixture_sha256") != digest:
        raise SensingTrialError("responses are not bound to this fixture")
    responses = response_artifact.get("responses")
    if not isinstance(responses, list):
        raise SensingTrialError("responses must be a list")
    candidates = {
        item["candidate_id"]
        for item in fixture["candidates"]
        if item["candidate_id"] != BASELINE_CANDIDATE
    }
    case_by_id = {item["case_id"]: item for item in fixture["cases"]}
    case_by_ref = {case_reference(digest, case_id): case for case_id, case in case_by_id.items()}
    if len(case_by_ref) != len(case_by_id):
        raise SensingTrialError("opaque case references collided")
    expected = {(candidate, case_ref) for candidate in candidates for case_ref in case_by_ref}
    observed: dict[tuple[str, str], dict[str, str]] = {}
    for response in responses:
        if not isinstance(response, dict):
            raise SensingTrialError("response must be an object")
        _exact_keys(
            response,
            {"candidate_id", "case_ref", "prompt_state", "input_buffer_state"},
            "response",
        )
        coordinate = (str(response["candidate_id"]), str(response["case_ref"]))
        if coordinate not in expected:
            raise SensingTrialError("response contains an unknown coordinate")
        if coordinate in observed:
            raise SensingTrialError("response coordinate is duplicated")
        if response["prompt_state"] not in PROMPT_STATES:
            raise SensingTrialError("response prompt_state is unsupported")
        if response["input_buffer_state"] not in INPUT_STATES:
            raise SensingTrialError("response input_buffer_state is unsupported")
        observed[coordinate] = {
            "prompt_state": response["prompt_state"],
            "input_buffer_state": response["input_buffer_state"],
        }
    if set(observed) != expected:
        raise SensingTrialError("every candidate/case coordinate requires one response")

    for case_ref, case in case_by_ref.items():
        prompt_state, input_buffer_state = _baseline_prediction(case)
        observed[(BASELINE_CANDIDATE, case_ref)] = {
            "prompt_state": prompt_state,
            "input_buffer_state": input_buffer_state,
        }
    candidates.add(BASELINE_CANDIDATE)

    by_candidate: dict[str, dict[str, Any]] = {}
    for candidate in sorted(candidates):
        overall = _empty_metrics()
        by_runtime = {runtime: _empty_metrics() for runtime in sorted(RUNTIMES)}
        for case_ref, case in case_by_ref.items():
            prediction = observed[(candidate, case_ref)]
            _add_result(overall, prediction, case["truth"])
            _add_result(by_runtime[case["runtime"]], prediction, case["truth"])
        _finish_metrics(overall)
        for metrics in by_runtime.values():
            _finish_metrics(metrics)
        by_candidate[candidate] = {"overall": overall, "by_runtime": by_runtime}
    normalized = {
        "schema": RESPONSE_SCHEMA,
        "experiment_id": fixture["experiment_id"],
        "fixture_sha256": digest,
        "responses": responses,
    }
    return {
        "schema": RESULT_SCHEMA,
        "experiment_id": fixture["experiment_id"],
        "fixture_sha256": digest,
        "response_sha256": content_digest(normalized),
        "provenance_kind": fixture["provenance"]["kind"],
        "live_evidence": False,
        "by_candidate": by_candidate,
        "preferred_candidate": None,
        "operator_judgment_required": True,
        "test_2_passed": False,
        "delivery_activated": False,
    }


def _write_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("render")
    score = subparsers.add_parser("score")
    score.add_argument("--responses", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        fixture = load_json(args.fixture)
        if args.command == "render":
            packet = render_packet(fixture)
            _write_json({**packet, "response_template": response_template(packet, fixture)})
        else:
            _write_json(score_responses(fixture, load_json(args.responses)))
    except SensingTrialError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
