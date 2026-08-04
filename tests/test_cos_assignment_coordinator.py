from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_assignment_coordinator as coordinator  # noqa: E402
from c2_contract import RunManifest  # noqa: E402


def manifest():
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
                    "coord_agent_id": "worker-agent",
                    "repositories": ["owner/repo"],
                }
            ],
            "plan_paths": ["/plan"],
            "permitted_repositories": ["owner/repo"],
            "permitted_actions": ["inspect"],
        }
    )


def test_build_envelope_is_bound_to_existing_assigned_task():
    envelope = coordinator.build_envelope(
        manifest=manifest(),
        task={
            "id": "task-1",
            "status": "assigned",
            "to_agent": "worker-agent",
            "repo": "owner/repo",
            "summary": "bounded work",
            "target_files": ["src/a.py"],
        },
        worker_id="worker",
        controller_epoch=7,
        generation=3,
        authorization_limits=["no-deploy"],
    )
    assert envelope.task_id == "task-1"
    assert envelope.attempt_id == "attempt:task-1:3:worker"
    assert envelope.controller_epoch == 7
    assert envelope.coord_agent_id == "worker-agent"
    assert envelope.scope == ("src/a.py",)


def test_dispatch_task_creates_attempt_before_edge_call():
    calls = []

    class Client:
        def verify_live_epoch(self, resource, epoch):
            calls.append(("verify", resource, epoch))

        def task(self, task_id):
            return {
                "id": task_id,
                "status": "assigned",
                "to_agent": "worker-agent",
                "repo": "owner/repo",
                "summary": "work",
            }

        def post_claim_request(self, envelope):
            calls.append(("claim-request", envelope["assignment_id"]))
            return {"id": 1}

        def wait_for_claim(self, **kwargs):
            calls.append(("claim-readback", kwargs))
            return {
                **self.task(kwargs["task_id"]),
                "status": "in_progress",
                "claimed_by": kwargs["worker_id"],
                "claimed_by_session": kwargs["session_id"],
            }

        def ensure_attempt(self, **kwargs):
            calls.append(("attempt", kwargs))
            return kwargs

        def reserve_bca(self, envelope):
            calls.append(("bca-reserve", envelope["assignment_id"]))
            return {"ok": True, "item": {"event_type": "reserved"}}

        def bca_correlation_tuple(self, envelope):
            calls.append(("bca-correlation", envelope["assignment_id"]))
            return {"idempotency_key": envelope["idempotency_key"]}

        def wait_for_bca_terminal(self, key, *, expected_correlation=None):
            calls.append(("bca-readback", key, expected_correlation))
            return {"events": [{"event_payload": {"delivery_state": "acknowledged"}}]}

    def edge_dispatch(*, envelope):
        calls.append(("edge", envelope))
        return {"ok": True, "receipt": {"assignment_id": envelope["assignment_id"]}}

    def worker_receipt(envelope):
        calls.append(("worker-receipt-open", envelope.assignment_id))

        def complete(result):
            calls.append(("worker-receipt-commit", result["receipt"]["assignment_id"]))
            return {"ok": True, "delivery_state": "acknowledged"}

        return complete

    result = coordinator.dispatch_task(
        client=Client(),
        manifest=manifest(),
        task_id="task-1",
        worker_id="worker",
        controller_epoch=7,
        generation=3,
        authorization_limits=["no-deploy"],
        edge_dispatch=edge_dispatch,
        worker_receipt=worker_receipt,
    )
    assert result["ok"] is True
    assert [item[0] for item in calls] == [
        "verify",
        "claim-request",
        "claim-readback",
        "attempt",
        "bca-reserve",
        "worker-receipt-open",
        "edge",
        "worker-receipt-commit",
        "bca-correlation",
        "bca-readback",
    ]
    assert calls[-1][2] == {"idempotency_key": "c2-dispatch:task-1:3:worker"}


def test_preflight_rejects_ambiguous_candidate():
    with pytest.raises(coordinator.CandidateSelectionError) as error:
        coordinator.preflight_candidate(
            manifest=manifest(),
            task={"id": "task-1", "status": "assigned", "repo": "owner/repo", "priority": "???"},
            worker_id="worker",
        )
    assert error.value.code == "selection_underdetermined"


def test_dispatch_task_rejects_nonterminal_bca_readback():
    class Client:
        def verify_live_epoch(self, resource, epoch):
            return None

        def task(self, task_id):
            return {
                "id": task_id,
                "status": "assigned",
                "to_agent": "worker-agent",
                "repo": "owner/repo",
                "summary": "work",
            }

        def post_claim_request(self, envelope):
            return {"id": 1}

        def wait_for_claim(self, **kwargs):
            return {
                **self.task(kwargs["task_id"]),
                "status": "in_progress",
                "claimed_by": kwargs["worker_id"],
                "claimed_by_session": kwargs["session_id"],
            }

        def ensure_attempt(self, **kwargs):
            return kwargs

        def reserve_bca(self, envelope):
            return {"ok": True, "item": {"event_type": "reserved"}}

        def bca_correlation_tuple(self, envelope):
            return {"idempotency_key": envelope["idempotency_key"]}

        def wait_for_bca_terminal(self, key, *, expected_correlation=None):
            return {"events": [{"event_payload": {"delivery_state": "queued"}}]}

    def edge_dispatch(*, envelope):
        return {"ok": True, "receipt": {"assignment_id": envelope["assignment_id"]}}

    def worker_receipt(_envelope):
        return lambda _result: {"ok": True, "delivery_state": "acknowledged"}

    with pytest.raises(coordinator.ContractError, match="no terminal worker receipt"):
        coordinator.dispatch_task(
            client=Client(),
            manifest=manifest(),
            task_id="task-1",
            worker_id="worker",
            controller_epoch=7,
            generation=3,
            authorization_limits=["no-deploy"],
            edge_dispatch=edge_dispatch,
            worker_receipt=worker_receipt,
        )


def test_dispatch_task_rejects_unsuccessful_worker_receipt():
    class Client:
        def verify_live_epoch(self, resource, epoch):
            return None

        def task(self, task_id):
            return {
                "id": task_id,
                "status": "assigned",
                "to_agent": "worker-agent",
                "repo": "owner/repo",
                "summary": "work",
            }

        def post_claim_request(self, envelope):
            return {"id": 1}

        def wait_for_claim(self, **kwargs):
            return {
                **self.task(kwargs["task_id"]),
                "status": "in_progress",
                "claimed_by": kwargs["worker_id"],
                "claimed_by_session": kwargs["session_id"],
            }

        def ensure_attempt(self, **kwargs):
            return kwargs

        def reserve_bca(self, envelope):
            return {"ok": True, "item": {"event_type": "reserved"}}

        def wait_for_bca_terminal(self, key):
            raise AssertionError("terminal readback should not be queried after a failed receipt")

    def edge_dispatch(*, envelope):
        return {"ok": True, "receipt": {"assignment_id": envelope["assignment_id"]}}

    def worker_receipt(_envelope):
        return lambda _result: {"ok": False, "delivery_state": "refused"}

    with pytest.raises(
        coordinator.ContractError,
        match="worker runtime did not durably submit a BCA receipt",
    ):
        coordinator.dispatch_task(
            client=Client(),
            manifest=manifest(),
            task_id="task-1",
            worker_id="worker",
            controller_epoch=7,
            generation=3,
            authorization_limits=["no-deploy"],
            edge_dispatch=edge_dispatch,
            worker_receipt=worker_receipt,
        )


@pytest.mark.parametrize("delivery_state", ["refused", "dead_lettered"])
def test_dispatch_task_rejects_negative_terminal_bca_readback(delivery_state):
    class Client:
        def verify_live_epoch(self, resource, epoch):
            return None

        def task(self, task_id):
            return {
                "id": task_id,
                "status": "assigned",
                "to_agent": "worker-agent",
                "repo": "owner/repo",
                "summary": "work",
            }

        def post_claim_request(self, envelope):
            return {"id": 1}

        def wait_for_claim(self, **kwargs):
            return {
                **self.task(kwargs["task_id"]),
                "status": "in_progress",
                "claimed_by": kwargs["worker_id"],
                "claimed_by_session": kwargs["session_id"],
            }

        def ensure_attempt(self, **kwargs):
            return kwargs

        def reserve_bca(self, envelope):
            return {"ok": True, "item": {"event_type": "reserved"}}

        def bca_correlation_tuple(self, envelope):
            return {"idempotency_key": envelope["idempotency_key"]}

        def wait_for_bca_terminal(self, key, *, expected_correlation=None):
            return {"events": [{"event_payload": {"delivery_state": delivery_state}}]}

    def edge_dispatch(*, envelope):
        return {"ok": True, "receipt": {"assignment_id": envelope["assignment_id"]}}

    def worker_receipt(_envelope):
        return lambda _result: {"ok": True, "delivery_state": "acknowledged"}

    with pytest.raises(
        coordinator.ContractError,
        match=f"negative worker receipt state: {delivery_state}",
    ):
        coordinator.dispatch_task(
            client=Client(),
            manifest=manifest(),
            task_id="task-1",
            worker_id="worker",
            controller_epoch=7,
            generation=3,
            authorization_limits=["no-deploy"],
            edge_dispatch=edge_dispatch,
            worker_receipt=worker_receipt,
        )
