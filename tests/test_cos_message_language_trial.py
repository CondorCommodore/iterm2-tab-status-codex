import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_message_language_trial as trial  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "message_delivery_language_comparison_v1.json"


def experiment():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def complete_response(packet, source):
    labels = {row["candidate_id"]: row["labels"] for row in source["candidates"]}
    expected = {row["question_id"]: row["expected_rank"] for row in source["questions"]}
    return {
        "schema": trial.RESPONSE_SCHEMA,
        "experiment_sha256": packet["experiment_sha256"],
        "responses": [
            {
                "candidate_id": candidate_id,
                "question_id": question_id,
                "selected_label": labels[candidate_id][rank],
            }
            for question_id, rank in expected.items()
            for candidate_id in labels
        ],
    }


def test_packet_is_deterministic_and_hides_answer_and_reference_metadata():
    source = experiment()
    packet = trial.render_packet(source)

    assert packet == trial.render_packet(source)
    assert packet["experiment_sha256"] == trial.content_digest(source)
    serialized = json.dumps(packet, sort_keys=True)
    assert "expected_rank" not in serialized
    assert "experimental_reference_only" not in serialized
    assert "historical reference" not in serialized
    assert "does not change the selected terminology" in packet["notice"]
    assert "enable message delivery" in packet["notice"]
    source_labels = {row["candidate_id"]: row["labels"] for row in source["candidates"]}
    for candidate in packet["candidate_sets"]:
        assert candidate["labels"] != source_labels[candidate["candidate_id"]]
    assert [row["question_id"] for row in packet["scenarios"]] != [
        row["question_id"] for row in source["questions"]
    ]


def test_packet_rejects_malformed_experiment_instead_of_rendering_it():
    source = experiment()
    source["candidates"][0]["labels"] = ["only-one"]

    with pytest.raises(trial.LanguageTrialError, match="four unique ordered labels"):
        trial.render_packet(source)


def test_packet_rejects_missing_render_fields_without_a_traceback():
    source = experiment()
    del source["questions"][0]["scenario"]

    with pytest.raises(trial.LanguageTrialError, match="non-empty scenario"):
        trial.render_packet(source)


def test_response_template_contains_every_coordinate_without_answers():
    packet = trial.render_packet(experiment())
    template = trial.response_template(packet)

    assert len(template["responses"]) == 8
    assert {row["selected_label"] for row in template["responses"]} == {""}
    assert len({(row["candidate_id"], row["question_id"]) for row in template["responses"]}) == 8


def test_complete_response_scores_without_selecting_or_activating_policy():
    source = experiment()
    packet = trial.render_packet(source)
    result = trial.score_responses(source, complete_response(packet, source))

    assert result["complete"] is True
    assert result["preferred_candidate"] is None
    assert result["operator_judgment_required"] is True
    assert result["test_1_passed"] is False
    assert result["delivery_activated"] is False
    assert len(result["response_sha256"]) == 64


@pytest.mark.parametrize("digest", ["", "0" * 64])
def test_score_rejects_response_bound_to_another_experiment(digest):
    source = experiment()
    artifact = complete_response(trial.render_packet(source), source)
    artifact["experiment_sha256"] = digest

    with pytest.raises(trial.LanguageTrialError, match="not bound"):
        trial.score_responses(source, artifact)


def test_score_rejects_missing_duplicate_and_unknown_coordinates():
    source = experiment()
    artifact = complete_response(trial.render_packet(source), source)

    missing = {**artifact, "responses": artifact["responses"][:-1]}
    with pytest.raises(trial.LanguageTrialError, match="every candidate"):
        trial.score_responses(source, missing)

    duplicate = {**artifact, "responses": artifact["responses"] + [artifact["responses"][0]]}
    with pytest.raises(trial.LanguageTrialError, match="duplicate"):
        trial.score_responses(source, duplicate)

    unknown = json.loads(json.dumps(artifact))
    unknown["responses"][0]["selected_label"] = "surprise"
    with pytest.raises(trial.LanguageTrialError, match="unknown label"):
        trial.score_responses(source, unknown)


def test_markdown_is_operator_facing_and_contains_no_answer_key():
    packet = trial.render_packet(experiment())
    rendered = trial.render_markdown(packet)

    assert rendered.startswith("# Message wording comprehension trial")
    assert "expected_rank" not in rendered
    assert rendered.count(": ______") == 8
