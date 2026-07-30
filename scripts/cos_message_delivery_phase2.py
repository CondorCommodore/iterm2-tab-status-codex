#!/usr/bin/env python3
"""Producer/readback helpers for Test 1 Phase 2 durable reconstruction.

The producer persists immutable synthetic inputs and its claimed projection.
The reader needs only the durable response: it rebuilds the projection with the
pure policy and compares canonical bytes. Neither path owns terminal delivery.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

import cos_message_delivery_policy as policy
from c2_coord_client import CoordClient, CoordConfig


class Phase2EvidenceError(ValueError):
    """Raised when durable Phase 2 evidence is incomplete or inconsistent."""


RUN_FIELDS = (
    "run_id",
    "schema_version",
    "fixture_sha256",
    "input_snapshot",
    "events",
    "control_projection",
    "treatment_projection",
    "producer_projection_sha256",
)


def _require_fixture_schema(fixture: Mapping[str, Any]) -> None:
    if fixture.get("schema") != "cos.message-delivery-shadow.fixture.v1":
        raise Phase2EvidenceError("unsupported message-delivery fixture schema")


def _projection(fixture: Mapping[str, Any]) -> dict[str, Any]:
    return policy.project_delivery(
        messages=fixture["messages"],
        policies=fixture["policies"],
        events=fixture["events"],
        recipient_agent=fixture["recipient_agent"],
        recipient_session_id=fixture["recipient_session_id"],
        worker_state=fixture["worker_state"],
        now=fixture["now"],
        live_controller_epoch=fixture["live_controller_epoch"],
        verified_actor_sessions=fixture["verified_actor_sessions"],
    )


def build_run(fixture: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    """Build one digest-bound artifact suitable for the coord-api Phase 2 route."""

    _require_fixture_schema(fixture)
    source = dict(fixture)
    events = source.pop("events")
    if not isinstance(events, list):
        raise Phase2EvidenceError("fixture events must be a list")
    treatment = _projection(fixture)
    run = {
        "run_id": run_id,
        "schema_version": "cos.message-delivery-shadow.run.v1",
        "fixture_sha256": policy.content_digest(fixture),
        "input_snapshot": source,
        "events": events,
        "control_projection": treatment["control_comparison"],
        "treatment_projection": treatment,
        "producer_projection_sha256": policy.content_digest(treatment),
    }
    run["artifact_sha256"] = policy.content_digest(run)
    return run


def reconstruct(item: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct from durable readback only and require byte-identical evidence."""

    stored = dict(item)
    if stored.get("schema_version") != "cos.message-delivery-shadow.run.v1":
        raise Phase2EvidenceError("unsupported durable shadow-run schema")
    artifact_digest = stored.get("artifact_sha256")
    artifact_fields = {key: stored.get(key) for key in RUN_FIELDS}
    if artifact_digest != policy.content_digest(artifact_fields):
        raise Phase2EvidenceError("durable artifact digest mismatch")
    snapshot = stored.get("input_snapshot")
    events = stored.get("events")
    if not isinstance(snapshot, dict) or not isinstance(events, list):
        raise Phase2EvidenceError("durable artifact lacks reconstructable inputs")
    fixture = snapshot | {"events": events}
    _require_fixture_schema(fixture)
    if stored.get("fixture_sha256") != policy.content_digest(fixture):
        raise Phase2EvidenceError("durable fixture digest mismatch")
    treatment = _projection(fixture)
    if stored.get("producer_projection_sha256") != policy.content_digest(treatment):
        raise Phase2EvidenceError("producer projection digest mismatch")
    if policy.canonical_json(stored.get("treatment_projection")) != policy.canonical_json(
        treatment
    ):
        raise Phase2EvidenceError("durable treatment differs from reconstruction")
    if policy.canonical_json(stored.get("control_projection")) != policy.canonical_json(
        treatment["control_comparison"]
    ):
        raise Phase2EvidenceError("durable control comparison differs from reconstruction")
    return {
        "run_id": stored.get("run_id"),
        "fixture_sha256": stored["fixture_sha256"],
        "artifact_sha256": artifact_digest,
        "projection_sha256": treatment["projection_sha256"],
        "byte_identical": True,
        "producer_stop_observed": False,
        "projection": treatment,
    }


def produce(client: CoordClient, fixture: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    """Persist one synthetic run through the supported principal-bound route."""

    item = client.post_message_delivery_shadow_run(build_run(fixture, run_id))
    return {
        "run_id": item["run_id"],
        "artifact_sha256": item["artifact_sha256"],
        "recorded_by": item.get("recorded_by"),
        "recorded_at": item.get("recorded_at"),
    }


def readback(client: CoordClient, run_id: str) -> dict[str, Any]:
    """Fetch durable evidence and reconstruct it without producer state."""

    return reconstruct(client.message_delivery_shadow_run(run_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("produce", "readback"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--coord-config", type=Path)
    parser.add_argument("--coord-secrets", type=Path)
    args = parser.parse_args(argv)
    client = CoordClient(CoordConfig.load(args.coord_config, secrets_path=args.coord_secrets))
    if args.mode == "produce":
        if args.fixture is None:
            parser.error("produce requires --fixture")
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        result = produce(client, fixture, args.run_id)
    else:
        result = readback(client, args.run_id)
    print(policy.canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
