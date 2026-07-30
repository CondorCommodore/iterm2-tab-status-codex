from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cos_message_delivery_phase2 as phase2  # noqa: E402


def fixture():
    return json.loads(
        (ROOT / "tests/fixtures/message_delivery_shadow_v1.json").read_text()
    )


def test_durable_readback_reconstructs_byte_identically():
    durable_item = phase2.build_run(fixture(), "phase2-producer-stop-1") | {
        "recorded_by": "mikebook_codex",
        "recorded_at": "2026-07-29T23:00:00Z",
    }

    result = phase2.reconstruct(durable_item)

    assert result["byte_identical"] is True
    assert result["producer_stop_observed"] is False
    assert result["run_id"] == "phase2-producer-stop-1"
    assert result["projection_sha256"] == result["projection"]["projection_sha256"]


def test_producer_and_fresh_reader_share_only_durable_route_state_without_stop_claim():
    class DurableClient:
        def __init__(self):
            self.item = None

        def post_message_delivery_shadow_run(self, run):
            self.item = copy.deepcopy(run) | {
                "recorded_by": "mikebook_codex",
                "recorded_at": "2026-07-29T23:00:00Z",
            }
            return copy.deepcopy(self.item)

        def message_delivery_shadow_run(self, run_id):
            assert self.item["run_id"] == run_id
            return copy.deepcopy(self.item)

    durable = DurableClient()
    produced = phase2.produce(durable, fixture(), "phase2-separate-reader-1")
    assert produced["recorded_by"] == "mikebook_codex"

    reconstructed = phase2.readback(durable, "phase2-separate-reader-1")
    assert reconstructed["byte_identical"] is True
    assert reconstructed["artifact_sha256"] == produced["artifact_sha256"]


def test_cli_can_isolate_coord_config_from_operator_secrets(monkeypatch, tmp_path):
    captured = {}

    class StopAfterLoad(Exception):
        pass

    def load(path, *, secrets_path):
        captured.update(path=path, secrets_path=secrets_path)
        raise StopAfterLoad

    monkeypatch.setattr(phase2.CoordConfig, "load", load)
    config_path = tmp_path / "coord.json"
    secrets_path = tmp_path / "empty-secrets"
    with pytest.raises(StopAfterLoad):
        phase2.main(
            [
                "readback",
                "--run-id",
                "phase2-cli-isolation-1",
                "--coord-config",
                str(config_path),
                "--coord-secrets",
                str(secrets_path),
            ]
        )
    assert captured == {"path": config_path, "secrets_path": secrets_path}


@pytest.mark.parametrize(
    "mutate,match",
    [
        (
            lambda value: value["events"][0].update(actor_id="forged-edge"),
            "artifact digest mismatch",
        ),
        (
            lambda value: value["treatment_projection"].update(proposed_action={}),
            "artifact digest mismatch",
        ),
    ],
)
def test_readback_fails_closed_on_durable_mutation(mutate, match):
    item = phase2.build_run(fixture(), "phase2-mutation-1")
    mutate(item)
    with pytest.raises(phase2.Phase2EvidenceError, match=match):
        phase2.reconstruct(item)


def test_matching_self_asserted_digest_cannot_hide_wrong_projection():
    item = phase2.build_run(fixture(), "phase2-forged-projection-1")
    item["treatment_projection"] = copy.deepcopy(item["treatment_projection"])
    item["treatment_projection"]["proposed_action"] = {"action": "HOLD"}
    item["producer_projection_sha256"] = phase2.policy.content_digest(
        item["treatment_projection"]
    )
    unsigned = {key: value for key, value in item.items() if key != "artifact_sha256"}
    item["artifact_sha256"] = phase2.policy.content_digest(unsigned)

    with pytest.raises(phase2.Phase2EvidenceError, match="projection digest mismatch"):
        phase2.reconstruct(item)


@pytest.mark.parametrize(
    "field,path",
    [
        ("treatment_projection", ("proposed_action", "message_id")),
        ("control_projection", ("control_proposed_action", "message_id")),
    ],
)
def test_python_equal_but_canonically_different_projection_fails_closed(field, path):
    item = phase2.build_run(fixture(), "phase2-canonical-bytes-1")
    target = item[field]
    for part in path[:-1]:
        target = target[part]
    original = target[path[-1]]
    assert isinstance(original, int)
    target[path[-1]] = float(original)
    assert target[path[-1]] == original
    unsigned = {key: value for key, value in item.items() if key != "artifact_sha256"}
    item["artifact_sha256"] = phase2.policy.content_digest(unsigned)

    with pytest.raises(phase2.Phase2EvidenceError, match="differs from reconstruction"):
        phase2.reconstruct(item)


def test_readback_rejects_unknown_schema_even_when_artifact_digest_matches():
    item = phase2.build_run(fixture(), "phase2-schema-version-1")
    item["schema_version"] = "cos.message-delivery-shadow.run.v2"
    unsigned = {key: value for key, value in item.items() if key != "artifact_sha256"}
    item["artifact_sha256"] = phase2.policy.content_digest(unsigned)

    with pytest.raises(phase2.Phase2EvidenceError, match="unsupported.*schema"):
        phase2.reconstruct(item)


def test_producer_rejects_unknown_fixture_schema():
    value = fixture()
    value["schema"] = "cos.message-delivery-shadow.fixture.v999"

    with pytest.raises(phase2.Phase2EvidenceError, match="unsupported.*fixture schema"):
        phase2.build_run(value, "phase2-fixture-schema-produce-1")


def test_readback_rejects_unknown_fixture_schema_with_matching_digests():
    item = phase2.build_run(fixture(), "phase2-fixture-schema-readback-1")
    item["input_snapshot"]["schema"] = "cos.message-delivery-shadow.fixture.v999"
    reconstructed_fixture = item["input_snapshot"] | {"events": item["events"]}
    item["fixture_sha256"] = phase2.policy.content_digest(reconstructed_fixture)
    unsigned = {key: value for key, value in item.items() if key != "artifact_sha256"}
    item["artifact_sha256"] = phase2.policy.content_digest(unsigned)

    with pytest.raises(phase2.Phase2EvidenceError, match="unsupported.*fixture schema"):
        phase2.reconstruct(item)
