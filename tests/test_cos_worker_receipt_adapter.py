from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_worker_receipt_adapter as adapter  # noqa: E402
from c2_contract import DispatchEnvelope  # noqa: E402


def envelope() -> DispatchEnvelope:
    return DispatchEnvelope.from_dict(
        {
            "assignment_id": "assignment:task-1:1:worker",
            "task_id": "task-1",
            "attempt_id": "attempt:task-1:1:worker",
            "worker_id": "worker",
            "cli_session_id": "cli-worker",
            "coord_session_id": "coord-worker",
            "coord_agent_id": "worker-agent",
            "objective": "work",
            "repo": "owner/repo",
            "worktree": "/tmp/worktree",
            "scope": ["owner/repo"],
            "acceptance_tests": ["report durable result"],
            "stopping_condition": "report",
            "report_destination": "coord-api:/tasks/task-1",
            "authorization_limits": ["no-deploy"],
            "permitted_actions": ["inspect"],
            "controller_epoch": 3,
            "idempotency_key": "dispatch:1",
        }
    )


def test_adapter_posts_acknowledged_receipt_with_exact_identity():
    calls = []

    class Client:
        def post_bca_receipt(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}

    sink = adapter.bca_receipt_adapter(
        envelope(),
        config_loader=lambda **kwargs: type("Cfg", (), {"principal_id": "worker-agent"})(),
        client_factory=lambda config: Client(),
        session_capability_loader=lambda session_id: "capability-1",
    )

    result = sink({"ok": True, "receipt": {"assignment_id": "assignment:task-1:1:worker"}})

    assert result == {"ok": True}
    args, kwargs = calls[0]
    assert args == ("dispatch:1",)
    assert kwargs["outcome"] == "acknowledged"
    assert kwargs["attempt_number"] == 1
    assert kwargs["worker_id"] == "worker-agent"
    assert kwargs["session_id"] == "coord-worker"
    assert kwargs["session_capability"] == "capability-1"
    assert kwargs["payload_digest"] == envelope().digest()


def test_adapter_rejects_wrong_principal():
    with pytest.raises(
        adapter.ContractError,
        match="principal does not match envelope worker",
    ):
        adapter.bca_receipt_adapter(
            envelope(),
            config_loader=lambda **kwargs: type("Cfg", (), {"principal_id": "other-agent"})(),
            client_factory=lambda config: object(),
            session_capability_loader=lambda session_id: "capability-1",
        )


def test_adapter_maps_negative_edge_result_to_refused_receipt():
    calls = []

    class Client:
        def post_bca_receipt(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}

    sink = adapter.bca_receipt_adapter(
        envelope(),
        config_loader=lambda **kwargs: type("Cfg", (), {"principal_id": "worker-agent"})(),
        client_factory=lambda config: Client(),
        session_capability_loader=lambda session_id: "capability-1",
    )

    sink({"ok": False, "error": "edge dispatch failed"})

    _args, kwargs = calls[0]
    assert kwargs["outcome"] == "refused"
    assert kwargs["reason"] == "edge dispatch failed"


def test_adapter_rejects_missing_session_capability():
    with pytest.raises(
        adapter.ContractError,
        match="worker session capability unavailable",
    ):
        adapter.bca_receipt_adapter(
            envelope(),
            config_loader=lambda **kwargs: type("Cfg", (), {"principal_id": "worker-agent"})(),
            client_factory=lambda config: object(),
            session_capability_loader=lambda session_id: "",
        )


def test_adapter_rejects_malformed_edge_result():
    sink = adapter.bca_receipt_adapter(
        envelope(),
        config_loader=lambda **kwargs: type("Cfg", (), {"principal_id": "worker-agent"})(),
        client_factory=lambda config: type(
            "Client", (), {"post_bca_receipt": lambda self, *args, **kwargs: {"ok": True}}
        )(),
        session_capability_loader=lambda session_id: "capability-1",
    )

    with pytest.raises(adapter.ContractError, match="edge dispatch result must be an object"):
        sink("not-a-dict")
