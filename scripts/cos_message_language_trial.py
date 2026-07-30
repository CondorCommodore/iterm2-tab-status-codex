#!/usr/bin/env python3
"""Render and score the blinded human-language portion of mailman Test 1.

This is a pure, local experiment helper. It has no coordination, terminal,
network, persistence, or policy-selection authority.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from cos_message_delivery_policy import (
    DeliveryPolicyError,
    content_digest,
    score_language_comprehension,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "message_delivery_language_comparison_v1.json"
PACKET_SCHEMA = "cos.message-language-trial.packet.v1"
RESPONSE_SCHEMA = "cos.message-language-trial.responses.v1"


class LanguageTrialError(ValueError):
    """Raised when a trial artifact is incomplete or not bound to its fixture."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LanguageTrialError(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise LanguageTrialError("JSON artifact must be an object")
    return value


def render_packet(experiment: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic operator packet without answer-key metadata."""
    candidates = experiment.get("candidates")
    questions = experiment.get("questions")
    if not isinstance(candidates, list) or not isinstance(questions, list):
        raise LanguageTrialError("language fixture requires candidates and questions")

    packet = {
        "schema": PACKET_SCHEMA,
        "experiment_sha256": content_digest(experiment),
        "notice": (
            "This is a wording-comprehension experiment. No candidate is active "
            "product terminology and completing it does not enable message delivery."
        ),
        "instructions": (
            "For every scenario, select the one label in each candidate set that "
            "best communicates the described handling behavior."
        ),
        "candidate_sets": [
            {
                "candidate_id": str(candidate["candidate_id"]),
                "labels": [str(label) for label in candidate["labels"]],
            }
            for candidate in candidates
        ],
        "scenarios": [
            {
                "question_id": str(question["question_id"]),
                "scenario": str(question["scenario"]),
            }
            for question in questions
        ],
    }
    return packet


def response_template(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESPONSE_SCHEMA,
        "experiment_sha256": packet["experiment_sha256"],
        "responses": [
            {
                "candidate_id": candidate["candidate_id"],
                "question_id": scenario["question_id"],
                "selected_label": "",
            }
            for scenario in packet["scenarios"]
            for candidate in packet["candidate_sets"]
        ],
    }


def render_markdown(packet: Mapping[str, Any]) -> str:
    lines = [
        "# Message wording comprehension trial",
        "",
        str(packet["notice"]),
        "",
        str(packet["instructions"]),
        "",
    ]
    candidate_sets = packet["candidate_sets"]
    for scenario in packet["scenarios"]:
        lines.extend([f"## {scenario['question_id']}", "", str(scenario["scenario"]), ""])
        for candidate in candidate_sets:
            labels = " / ".join(candidate["labels"])
            lines.append(f"- Set {candidate['candidate_id']} ({labels}): ______")
        lines.append("")
    lines.extend(
        [
            f"Experiment: `{packet['experiment_sha256']}`",
            "",
        ]
    )
    return "\n".join(lines)


def score_responses(
    experiment: Mapping[str, Any], response_artifact: Mapping[str, Any]
) -> dict[str, Any]:
    if response_artifact.get("schema") != RESPONSE_SCHEMA:
        raise LanguageTrialError("unsupported response artifact schema")
    experiment_sha256 = content_digest(experiment)
    if response_artifact.get("experiment_sha256") != experiment_sha256:
        raise LanguageTrialError("response artifact is not bound to this experiment")
    responses = response_artifact.get("responses")
    if not isinstance(responses, list):
        raise LanguageTrialError("responses must be a list")
    try:
        score = score_language_comprehension(experiment, responses)
    except DeliveryPolicyError as exc:
        raise LanguageTrialError(str(exc)) from exc
    if not score["complete"]:
        raise LanguageTrialError("every candidate/scenario coordinate requires one response")
    if score["preferred_candidate"] is not None or not score["operator_judgment_required"]:
        raise LanguageTrialError("pure scorer attempted to select product policy")
    normalized = {
        "schema": RESPONSE_SCHEMA,
        "experiment_sha256": experiment_sha256,
        "responses": responses,
    }
    return {
        **score,
        "response_sha256": content_digest(normalized),
        "test_1_passed": False,
        "delivery_activated": False,
    }


def _write_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render")
    render.add_argument("--format", choices=("json", "markdown"), default="markdown")
    score = subparsers.add_parser("score")
    score.add_argument("--responses", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        experiment = load_json(args.fixture)
        if args.command == "render":
            packet = render_packet(experiment)
            if args.format == "json":
                _write_json({**packet, "response_template": response_template(packet)})
            else:
                print(render_markdown(packet), end="")
        else:
            _write_json(score_responses(experiment, load_json(args.responses)))
    except LanguageTrialError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
