from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import c2_contract as c2  # noqa: E402


def manifest_dict(*, controller_visible: bool = True, **overrides):
    value = {
        "manifest_id": "test-v1",
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
                "worker_id": "worker-codex",
                "host": "macbook",
                "runtime": "codex",
                "iterm_session_id": "iterm-worker",
                "tty": "/dev/ttys003",
                "cli_session_id": "cli-worker",
                "coord_session_id": "coord-worker",
                "coord_agent_id": "mikebook_codex",
                "repositories": ["Condor/repo"],
            },
            {
                "worker_id": "worker-claude",
                "host": "macbook",
                "runtime": "claude",
                "iterm_session_id": "iterm-claude",
                "tty": "/dev/ttys004",
                "cli_session_id": "cli-claude",
                "coord_session_id": "coord-claude",
                "coord_agent_id": "mikebook_claude",
                "repositories": ["Condor/repo"],
            },
        ],
        "plan_paths": ["/plans/master.md"],
        "permitted_repositories": ["Condor/repo"],
        "permitted_actions": ["inspect", "test"],
        "dispatch_transport": "ab",
        "recovery_transport": "ab",
    }
    if not controller_visible:
        value["controller"].pop("iterm_session_id")
        value["controller"].pop("tty")
    value.update(overrides)
    return value


def envelope_dict(**overrides):
    value = {
        "assignment_id": "assignment-1",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "worker_id": "worker-codex",
        "cli_session_id": "cli-worker",
        "coord_session_id": "coord-worker",
        "objective": "implement the bounded slice",
        "repo": "Condor/repo",
        "worktree": "/tmp/worktree",
        "scope": ["src/a.py"],
        "acceptance_tests": ["pytest tests/test_a.py"],
        "stopping_condition": "PR merged or durable blocker",
        "report_destination": "coord-api task task-1",
        "authorization_limits": ["no deploy"],
        "permitted_actions": ["inspect", "test"],
        "controller_epoch": 7,
        "idempotency_key": "dispatch-task-1-attempt-1",
    }
    value.update(overrides)
    return value


def test_manifest_registers_mixed_runtimes_and_ab_is_deterministic():
    manifest = c2.RunManifest.from_dict(manifest_dict())

    assert [worker.runtime for worker in manifest.workers] == ["codex", "claude"]
    assert manifest.transport_for("assignment-1") == manifest.transport_for("assignment-1")
    assert manifest.transport_for("assignment-1") in {"tab", "headless"}
    assert manifest.recovery_for(0) == "tab"
    assert manifest.recovery_for(1) == "headless"
    assert manifest.terminal_actions_enabled is False


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_manifest_rejects_non_boolean_terminal_action_gate(value):
    with pytest.raises(c2.ContractError, match="terminal_actions_enabled must be a boolean"):
        c2.RunManifest.from_dict(manifest_dict(terminal_actions_enabled=value))


def test_manifest_explicitly_enables_terminal_actions():
    manifest = c2.RunManifest.from_dict(manifest_dict(terminal_actions_enabled=True))

    assert manifest.terminal_actions_enabled is True


def test_shipped_manifest_example_keeps_terminal_actions_disabled():
    example = Path(__file__).resolve().parent.parent / "config" / "run-manifest.example.json"
    manifest = c2.load_manifest(example)

    assert manifest.terminal_actions_enabled is False


def test_manifest_rejects_controller_worker_session_collision():
    value = manifest_dict()
    value["workers"][0]["iterm_session_id"] = "iterm-cos"

    with pytest.raises(c2.ContractError, match="controller session"):
        c2.RunManifest.from_dict(value)


def test_manifest_accepts_headless_controller_without_terminal_identity():
    manifest = c2.RunManifest.from_dict(manifest_dict(controller_visible=False))

    assert manifest.controller_iterm_session_id == ""
    assert manifest.controller_tty == ""
    assert manifest.controller_has_visible_terminal() is False
    assert manifest.controller_presentation == "headless"


def test_manifest_builds_exact_visible_and_headless_controller_producers():
    manifest = c2.RunManifest.from_dict(manifest_dict())

    visible = manifest.controller_producer("visible")
    headless = manifest.controller_producer("headless")

    assert visible == {
        "kind": "c2-supervisor",
        "manifest_id": "test-v1",
        "controller_id": "cos",
        "controller_host": "macbook",
        "controller_runtime": "codex",
        "controller_cli_session_id": "cli-cos",
        "controller_coord_session_id": "coord-cos",
        "controller_presentation": "visible",
        "ownership": "visible",
        "controller_iterm_session_id": "iterm-cos",
    }
    assert headless["controller_presentation"] == "headless"
    assert headless["ownership"] == "headless"
    assert "controller_iterm_session_id" not in headless
    assert manifest.controller_producer_matches({**headless, "extension": "preserved"}, "headless")
    assert not manifest.controller_producer_matches(
        {**headless, "controller_iterm_session_id": "iterm-cos"}, "headless"
    )


def test_headless_manifest_refuses_visible_controller_producer():
    manifest = c2.RunManifest.from_dict(manifest_dict(controller_visible=False))

    with pytest.raises(c2.ContractError, match="requires terminal identity"):
        manifest.controller_producer("visible")


def test_manifest_rejects_partially_visible_controller_identity():
    value = manifest_dict(controller_visible=False)
    value["controller"]["tty"] = "/dev/ttys001"

    with pytest.raises(c2.ContractError, match="both be present or both be omitted"):
        c2.RunManifest.from_dict(value)


@pytest.mark.parametrize("field", ["cli_session_id", "coord_session_id"])
def test_manifest_rejects_controller_session_identity_collision(field):
    value = manifest_dict()
    value["workers"][0][field] = value["controller"][field]

    with pytest.raises(c2.ContractError, match="must not also be registered as a worker"):
        c2.RunManifest.from_dict(value)


@pytest.mark.parametrize("field", ["cli_session_id", "coord_session_id"])
def test_manifest_rejects_headless_controller_session_identity_collision(field):
    value = manifest_dict(controller_visible=False)
    value["workers"][0][field] = value["controller"][field]

    with pytest.raises(c2.ContractError, match="must not also be registered as a worker"):
        c2.RunManifest.from_dict(value)


def test_envelope_is_bound_to_registration_repo_and_actions():
    manifest = c2.RunManifest.from_dict(manifest_dict())
    envelope = c2.DispatchEnvelope.from_dict(envelope_dict())

    assert envelope.validate_for(manifest).worker_id == "worker-codex"
    assert len(envelope.digest()) == 64

    with pytest.raises(c2.ContractError, match="cli_session_id"):
        c2.DispatchEnvelope.from_dict(envelope_dict(cli_session_id="stale-cli")).validate_for(
            manifest
        )
    with pytest.raises(c2.ContractError, match="outside run manifest"):
        c2.DispatchEnvelope.from_dict(envelope_dict(permitted_actions=["deploy"])).validate_for(
            manifest
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ready", "idle"),
        ("queued", "reserved"),
        ("attention", "needs_input"),
        ("running", "running"),
    ],
)
def test_worker_state_contract(raw, expected):
    assert c2.normalize_worker_state(raw) == expected


def test_worker_state_stale_and_lost_override_signal():
    assert c2.normalize_worker_state("idle", age_seconds=181) == "stale"
    assert c2.normalize_worker_state("idle", present=False) == "lost"


def test_receipt_store_rejects_duplicate_idempotency(tmp_path):
    store = c2.ReceiptStore(tmp_path / "receipts.jsonl")
    store.append({"idempotency_key": "same", "ok": True})

    with pytest.raises(c2.ContractError, match="duplicate"):
        store.append({"idempotency_key": "same", "ok": True})


def test_receipt_store_atomically_reserves_one_concurrent_idempotency_key(tmp_path):
    store = c2.ReceiptStore(tmp_path / "receipts.jsonl")

    def append_once(sequence):
        try:
            store.append({"idempotency_key": "same", "sequence": sequence})
            return "appended"
        except c2.ContractError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(append_once, [1, 2]))

    assert outcomes == ["appended", "duplicate"]
    assert len(store.records()) == 1
