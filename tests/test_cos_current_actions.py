from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_current_actions as actions  # noqa: E402
from c2_contract import ContractError, RunManifest  # noqa: E402


def manifest() -> RunManifest:
    return RunManifest.from_dict(
        {
            "manifest_id": "test",
            "controller": {
                "controller_id": "cos",
                "host": "macbook",
                "runtime": "codex",
                "iterm_session_id": "iterm-cos",
                "tty": "/dev/ttys001",
                "cli_session_id": "cli-cos",
                "coord_session_id": "coord-cos",
                "coord_agent_id": "mikebook_codex",
            },
            "workers": [
                {
                    "worker_id": "worker",
                    "host": "macbook",
                    "runtime": "codex",
                    "iterm_session_id": "iterm-worker",
                    "tty": "/dev/ttys003",
                    "cli_session_id": "cli-worker",
                    "coord_session_id": "coord-worker",
                    "coord_agent_id": "mikebook_codex",
                }
            ],
            "plan_paths": ["/plan"],
            "permitted_repositories": ["repo"],
            "permitted_actions": ["inspect"],
        }
    )


def rewrite(path: Path, **changes) -> None:
    parsed = actions.parse_actions(path, manifest=manifest())
    header = {**parsed.header, **changes}
    path.write_text(
        f"--- {actions.SCHEMA}\n"
        f"{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
        f"---\n{parsed.body}\n",
        encoding="utf-8",
    )


def write_program_projection(path: Path, **changes) -> None:
    header = {
        "schema": actions.PROGRAM_SCHEMA,
        "manifest_id": "test",
        "controller_id": "cos",
        "controller_cli_session_id": "cli-cos",
        "controller_coord_session_id": "coord-cos",
        "controller_iterm_session_id": "iterm-cos",
        "controller_epoch": 7,
        "ownership": "visible",
        "decision_digest": "a" * 64,
        "action_digest": "b" * 64,
        "action_generation": 2,
        "status": "active",
        "written_at": "1970-01-01T00:03:20Z",
        "next_check_at": "1970-01-01T00:08:20Z",
        "references": ["/plan"],
        "direction_message_id": 11,
        "direction_digest": "c" * 64,
        "plan_generation": 3,
    }
    header.update(changes)
    body = """## Current portfolio
- wake_required=True
- wake_reasons=idle worker available
- action_digest=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
- next_check_at=1970-01-01T00:08:20Z

## Worker roster
- worker: idle (codex /dev/ttys003)

## Ordered actionable items
- task task-1 [queued]

## Durable direction and references
- plan_generation=3
- direction_message_id=11
- direction_digest=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
- plan_path=/plan

## Boundaries
- This projection is recovery guidance only and grants no authority without coord-api readback.
- Do not treat local projections as durable task truth or historical record.

## Rewrite or stop condition
- Rewrite after any material worker, message, PR, evidence, lease, or direction transition.
- Stop automatic work only when a later checkpoint marks the current actions complete.
"""
    path.write_text(
        f"--- {actions.PROGRAM_SCHEMA}\n"
        f"{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
        f"---\n{body}",
        encoding="utf-8",
    )


def test_seed_is_valid_bounded_recovery_checkpoint(tmp_path):
    path = tmp_path / "current-actions.txt"
    current = actions.seed_actions(
        manifest=manifest(), path=path, decision_digest="a" * 64, epoch=7, now_ts=100
    )

    assert current.generation == 1
    assert current.controller_epoch == 7
    assert current.next_check_ts - current.written_ts == 300
    assert path.stat().st_mode & 0o777 == 0o600


def test_parse_program_projection_accepts_bounded_projection(tmp_path):
    path = tmp_path / "program.md"
    write_program_projection(path)

    projection = actions.parse_program_projection(path, manifest=manifest(), now_ts=400)

    assert projection.controller_epoch == 7
    assert projection.action_digest == "b" * 64
    assert projection.decision_digest == "a" * 64


def test_parse_program_projection_rejects_out_of_bound_reference(tmp_path):
    path = tmp_path / "program.md"
    write_program_projection(path, references=["/plan", "/unexpected"])

    with pytest.raises(ContractError, match="references do not match manifest"):
        actions.parse_program_projection(path, manifest=manifest(), now_ts=400)


def test_parse_program_projection_rejects_unbounded_body_content(tmp_path):
    path = tmp_path / "program.md"
    write_program_projection(path)
    path.write_text(
        path.read_text(encoding="utf-8") + "\nThis is freeform history.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="bounded bullet format"):
        actions.parse_program_projection(path, manifest=manifest(), now_ts=400)


def test_checkpoint_requires_monotonic_generation_and_digest_chain(tmp_path):
    destination = tmp_path / "current-actions.txt"
    first = actions.seed_actions(
        manifest=manifest(), path=destination, decision_digest="a" * 64, epoch=7, now_ts=100
    )
    source = tmp_path / "candidate.txt"
    source.write_bytes(first.raw)
    rewrite(
        source,
        generation=2,
        previous_action_digest=first.digest,
        written_at="1970-01-01T00:03:20Z",
        next_check_at="1970-01-01T00:08:20Z",
    )

    receipt = actions.checkpoint_actions(
        source=source,
        destination=destination,
        manifest=manifest(),
        live_epoch=7,
        receipts_path=tmp_path / "receipts.jsonl",
    )
    assert receipt["generation"] == 2

    retry = actions.checkpoint_actions(
        source=source,
        destination=destination,
        manifest=manifest(),
        live_epoch=7,
        receipts_path=tmp_path / "receipts.jsonl",
    )
    assert retry["duplicate"] is True
    rewrite(source, next_check_at="1970-01-01T00:08:21Z")
    with pytest.raises(ContractError, match="generation must increase"):
        actions.checkpoint_actions(
            source=source,
            destination=destination,
            manifest=manifest(),
            live_epoch=7,
            receipts_path=tmp_path / "receipts.jsonl",
        )


def test_complete_checkpoint_requires_current_quiescent_decision(tmp_path):
    destination = tmp_path / "current-actions.txt"
    first = actions.seed_actions(
        manifest=manifest(), path=destination, decision_digest="a" * 64, epoch=7, now_ts=100
    )
    source = tmp_path / "candidate.txt"
    source.write_bytes(first.raw)
    rewrite(
        source,
        generation=2,
        previous_action_digest=first.digest,
        status="complete",
        completion_refs=["result:R-1"],
        written_at="1970-01-01T00:03:20Z",
        next_check_at="1970-01-01T00:08:20Z",
    )

    with pytest.raises(ContractError, match="wake is required"):
        actions.checkpoint_actions(
            source=source,
            destination=destination,
            manifest=manifest(),
            live_epoch=7,
            receipts_path=tmp_path / "receipts.jsonl",
            expected_decision_digest="a" * 64,
        )
    receipt = actions.checkpoint_actions(
        source=source,
        destination=destination,
        manifest=manifest(),
        live_epoch=7,
        receipts_path=tmp_path / "receipts.jsonl",
        expected_decision_digest="a" * 64,
        allow_complete=True,
    )
    assert receipt["status"] == "complete"


def test_ack_must_match_exact_digest_generation_epoch_and_ownership(tmp_path):
    path = tmp_path / "current-actions.txt"
    current = actions.seed_actions(
        manifest=manifest(), path=path, decision_digest="a" * 64, epoch=7, now_ts=100
    )
    kwargs = {
        "actions_path": path,
        "receipts_path": tmp_path / "receipts.jsonl",
        "manifest": manifest(),
        "digest": current.digest,
        "generation": 1,
        "epoch": 7,
        "ownership": "visible",
    }
    receipt = actions.acknowledge_actions(**kwargs)
    assert receipt["action_digest"] == current.digest

    with pytest.raises(ContractError, match="does not match current"):
        actions.acknowledge_actions(**{**kwargs, "digest": "b" * 64})
    with pytest.raises(ContractError, match="ownership"):
        actions.acknowledge_actions(**{**kwargs, "ownership": "headless"})


def test_duplicate_ack_does_not_refresh_progress_timestamp(tmp_path):
    path = tmp_path / "current-actions.txt"
    current = actions.seed_actions(
        manifest=manifest(), path=path, decision_digest="a" * 64, epoch=7, now_ts=100
    )
    kwargs = {
        "actions_path": path,
        "receipts_path": tmp_path / "receipts.jsonl",
        "manifest": manifest(),
        "digest": current.digest,
        "generation": 1,
        "epoch": 7,
        "ownership": "visible",
    }
    first = actions.acknowledge_actions(**kwargs)
    progress_result = actions.commit_action_ack(
        ack_receipt=first,
        coord_response={"id": 42, "accepted": True},
        progress_path=tmp_path / "progress.json",
        receipts_path=tmp_path / "receipts.jsonl",
    )
    progress = (tmp_path / "progress.json").read_bytes()
    duplicate = actions.acknowledge_actions(**kwargs)
    duplicate_progress = actions.commit_action_ack(
        ack_receipt=duplicate,
        coord_response={"id": 42, "accepted": True},
        progress_path=tmp_path / "progress.json",
        receipts_path=tmp_path / "receipts.jsonl",
    )

    assert duplicate["duplicate"] is True
    assert duplicate["recorded_ts"] == first["recorded_ts"]
    assert duplicate_progress["duplicate"] is True
    assert duplicate_progress["coord_accepted_ts"] == progress_result["coord_accepted_ts"]
    assert (tmp_path / "progress.json").read_bytes() == progress


def test_ack_retry_reuses_identical_prepared_receipt_before_coord_acceptance(tmp_path):
    path = tmp_path / "current-actions.txt"
    current = actions.seed_actions(
        manifest=manifest(), path=path, decision_digest="a" * 64, epoch=7, now_ts=100
    )
    kwargs = {
        "actions_path": path,
        "receipts_path": tmp_path / "receipts.jsonl",
        "manifest": manifest(),
        "digest": current.digest,
        "generation": 1,
        "epoch": 7,
        "ownership": "visible",
    }

    first = actions.acknowledge_actions(**kwargs)
    retry = actions.acknowledge_actions(**kwargs)

    assert retry["duplicate"] is True
    assert {key: value for key, value in retry.items() if key != "duplicate"} == first
    assert not (tmp_path / "progress.json").exists()


def test_deadline_and_changed_decision_are_independent_wake_signals(tmp_path):
    path = tmp_path / "current-actions.txt"
    current = actions.seed_actions(
        manifest=manifest(), path=path, decision_digest="a" * 64, epoch=7, now_ts=100
    )

    assert actions.action_wake_due(current, decision_digest="a" * 64, now_ts=200)[0] is False
    assert actions.action_wake_due(current, decision_digest="b" * 64, now_ts=200) == (
        True,
        "deterministic decision changed",
    )
    assert actions.action_wake_due(current, decision_digest="a" * 64, now_ts=400) == (
        True,
        "current action deadline reached",
    )


def test_changed_decision_overrides_complete_checkpoint(tmp_path):
    path = tmp_path / "current-actions.txt"
    actions.seed_actions(
        manifest=manifest(), path=path, decision_digest="a" * 64, epoch=7, now_ts=100
    )
    rewrite(path, status="complete", completion_refs=["result:R-1"])
    current = actions.parse_actions(path, manifest=manifest(), now_ts=100)

    assert actions.action_wake_due(current, decision_digest="b" * 64, now_ts=100) == (
        True,
        "deterministic decision changed",
    )


def test_rebind_advances_generation_and_preserves_intent_chain(tmp_path):
    path = tmp_path / "current-actions.txt"
    current = actions.seed_actions(
        manifest=manifest(), path=path, decision_digest="a" * 64, epoch=7, now_ts=100
    )
    rebound = actions.rebind_actions(
        current=current,
        path=path,
        manifest=manifest(),
        decision_digest="b" * 64,
        epoch=8,
        ownership="headless",
        now_ts=200,
    )

    assert rebound.generation == 2
    assert rebound.controller_epoch == 8
    assert rebound.header["ownership"] == "headless"
    assert rebound.header["previous_action_digest"] == current.digest
    assert rebound.body == current.body


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"next_check_at": "1970-01-01T00:00:30Z"}, "60 to 1800"),
        ({"controller_cli_session_id": "foreign"}, "does not match manifest"),
        ({"decision_digest": "not-a-digest"}, "lowercase SHA-256"),
        ({"status": "unknown"}, "status is unsupported"),
        ({"written_at": "2100-01-01T00:00:00Z"}, "too far in the future"),
        ({"status": "complete"}, "durable completion_refs"),
    ],
)
def test_unsafe_checkpoint_mutations_fail_closed(tmp_path, changes, match):
    path = tmp_path / "current-actions.txt"
    actions.seed_actions(
        manifest=manifest(), path=path, decision_digest="a" * 64, epoch=7, now_ts=0
    )
    rewrite(path, **changes)
    with pytest.raises(ContractError, match=match):
        actions.parse_actions(path, manifest=manifest())
