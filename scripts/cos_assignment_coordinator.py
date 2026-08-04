"""Compose durable task/attempt state with the fenced terminal edge."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, Callable

from c2_contract import (
    SUPERVISOR_RESOURCE,
    ContractError,
    DispatchEnvelope,
    RunManifest,
    load_manifest,
)
from c2_coord_client import CoordClient, CoordConfig
from cos_iterm_edge_client import dispatch_envelope as default_edge_dispatch


def _first(value: Any, fallback: str = "") -> str:
    return str(value or fallback).strip()


class CandidateSelectionError(ContractError):
    """A candidate cannot be safely selected or dispatched."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def preflight_candidate(
    *, manifest: RunManifest, task: dict[str, Any], worker_id: str
) -> dict[str, Any]:
    """Validate one already ordered candidate without mutating coord state."""
    worker = manifest.worker(worker_id)
    status = str(task.get("status") or "").strip().lower()
    if status not in {"assigned", "in_progress"}:
        raise CandidateSelectionError(
            "task_not_dispatchable", "task is not assigned or in_progress"
        )
    repo = _first(task.get("repo") or task.get("branch_repo") or task.get("pr_repo"))
    if not repo:
        raise CandidateSelectionError(
            "selection_underdetermined", "candidate has no structured repository"
        )
    target_agent = _first(task.get("to_agent"))
    if target_agent and target_agent != worker.coord_agent_id:
        raise CandidateSelectionError(
            "worker_not_assignee", "task is assigned to a different worker principal"
        )
    required = task.get("required_capabilities") or task.get("capabilities") or []
    if not isinstance(required, (list, tuple, set)):
        raise CandidateSelectionError(
            "selection_underdetermined", "candidate capabilities are not structured"
        )
    missing = sorted(
        {str(value) for value in required if str(value).strip()} - set(worker.capabilities)
    )
    if missing:
        raise CandidateSelectionError(
            "capability_mismatch", f"worker lacks capabilities: {', '.join(missing)}"
        )
    dependencies = task.get("dependencies") or []
    if not isinstance(dependencies, (list, tuple)):
        raise CandidateSelectionError(
            "selection_underdetermined", "candidate dependencies are not structured"
        )
    for dependency in dependencies:
        if isinstance(dependency, dict) and str(dependency.get("status") or "").lower() not in {
            "done",
            "completed",
            "merged",
            "success",
        }:
            raise CandidateSelectionError(
                "dependency_hold", "candidate has an incomplete dependency"
            )
    worktree_owner = _first(task.get("worktree_owner"))
    if worktree_owner and worktree_owner != worker.coord_agent_id:
        raise CandidateSelectionError(
            "worktree_collision", "candidate worktree is owned by another principal"
        )
    priority = task.get("priority")
    if priority is not None and str(priority).strip().lower() not in {
        "low",
        "normal",
        "priority",
        "immediate",
        "high",
        "flash",
    }:
        raise CandidateSelectionError(
            "selection_underdetermined", "candidate priority is ambiguous"
        )
    return {"ok": True, "task_id": _first(task.get("id") or task.get("task_id")), "repo": repo}


def build_envelope(
    *,
    manifest: RunManifest,
    task: dict[str, Any],
    worker_id: str,
    controller_epoch: int,
    generation: int,
    authorization_limits: list[str],
    plan_id: str = "legacy",
    direction_digest: str = "",
) -> DispatchEnvelope:
    worker = manifest.worker(worker_id)
    task_id = _first(task.get("id") or task.get("task_id"), "task")
    repo = _first(task.get("repo") or task.get("branch_repo") or task.get("pr_repo"))
    if not repo:
        raise ContractError("dispatch task has no structured repository")
    target_agent = _first(task.get("to_agent"))
    if target_agent and target_agent != worker.coord_agent_id:
        raise ContractError("task is assigned to a different registered worker principal")
    if task.get("status") not in {"assigned", "in_progress"}:
        raise ContractError("dispatch requires an assigned or in-progress task")
    assignment_id = f"assignment:{task_id}:{generation}:{worker_id}"
    attempt_id = f"attempt:{task_id}:{generation}:{worker_id}"
    objective = _first(task.get("description") or task.get("summary"))
    scope = tuple(str(item) for item in (task.get("target_files") or []) if str(item).strip())
    if not scope:
        scope = (repo,)
    acceptance = _first(task.get("acceptance_criteria"), "durable task result and evidence")
    manifest.permits(repo, ("inspect",))
    if not direction_digest:
        direction_digest = hashlib.sha256(f"{plan_id}:{generation}".encode()).hexdigest()
    return DispatchEnvelope(
        assignment_id=assignment_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worker_id=worker.worker_id,
        cli_session_id=worker.cli_session_id,
        coord_session_id=worker.coord_session_id,
        coord_agent_id=worker.coord_agent_id,
        objective=objective,
        repo=repo,
        worktree=_first(task.get("worktree") or task.get("branch_name") or repo),
        scope=scope,
        acceptance_tests=(acceptance,),
        stopping_condition="report durable result, evidence, and blocker before stopping",
        report_destination=f"coord-api:/tasks/{task_id}",
        authorization_limits=tuple(authorization_limits or ["no-deploy", "no-merge"]),
        permitted_actions=("inspect",),
        controller_epoch=controller_epoch,
        idempotency_key=f"c2-dispatch:{task_id}:{generation}:{worker_id}",
        plan_id=plan_id,
        generation=generation,
        direction_digest=direction_digest,
    )


def dispatch_task(
    *,
    client: CoordClient,
    manifest: RunManifest,
    task_id: str,
    worker_id: str,
    controller_epoch: int,
    generation: int,
    authorization_limits: list[str],
    plan_id: str = "legacy",
    direction_digest: str = "",
    edge_dispatch: Callable[[dict[str, Any]], dict[str, Any]] = default_edge_dispatch,
    worker_receipt: (
        Callable[[DispatchEnvelope], Callable[[dict[str, Any]], dict[str, Any]]] | None
    ) = None,
) -> dict[str, Any]:
    client.verify_live_epoch(SUPERVISOR_RESOURCE, controller_epoch)
    task = client.task(task_id)
    if not task:
        raise ContractError(f"task not found: {task_id}")
    preflight_candidate(manifest=manifest, task=task, worker_id=worker_id)
    if worker_receipt is None:
        raise ContractError(
            "worker-authenticated BCA receipt adapter is required before terminal injection"
        )
    envelope = build_envelope(
        manifest=manifest,
        task=task,
        worker_id=worker_id,
        controller_epoch=controller_epoch,
        generation=generation,
        authorization_limits=authorization_limits,
        plan_id=plan_id,
        direction_digest=direction_digest or _first(task.get("direction_digest")),
    )
    worker = manifest.worker(worker_id)
    client.post_claim_request(json.loads(envelope.canonical_json()))
    task = client.wait_for_claim(
        task_id=envelope.task_id,
        worker_id=worker.coord_agent_id,
        session_id=worker.coord_session_id,
    )
    attempt_context = json.dumps(
        {
            "plan_id": envelope.plan_id,
            "generation": envelope.generation,
            "direction_digest": envelope.direction_digest,
            "assignment_id": envelope.assignment_id,
            "payload_digest": envelope.digest(),
            "controller_epoch": envelope.controller_epoch,
            "worker_id": worker.coord_agent_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    attempt = client.ensure_attempt(
        attempt_id=envelope.attempt_id,
        task_id=envelope.task_id,
        session_id=worker.coord_session_id,
        context=attempt_context,
    )
    if isinstance(attempt, dict) and attempt.get("context") != attempt_context:
        raise ContractError("attempt readback correlation context does not match the envelope")
    reservation = client.reserve_bca(json.loads(envelope.canonical_json()))
    if not reservation.get("ok"):
        raise ContractError("BCA delivery reservation was not durably accepted")
    try:
        receipt_sink = worker_receipt(envelope)
    except Exception as exc:
        raise ContractError(f"worker receipt channel could not be established: {exc}") from exc
    if not callable(receipt_sink):
        raise ContractError("worker receipt adapter did not return a completion sink")
    # The edge performs the final supervisor and worker-reservation fencing.
    result = edge_dispatch(envelope=json.loads(envelope.canonical_json()))
    # The edge is controller-owned transport evidence.  Only the enrolled worker
    # runtime may commit the BCA terminal receipt, using its session capability.
    receipt = receipt_sink(result)
    if not isinstance(receipt, dict) or not receipt.get("ok", True):
        raise ContractError("worker runtime did not durably submit a BCA receipt")
    terminal_receipt = client.wait_for_bca_terminal_receipt(
        envelope.idempotency_key,
        expected_correlation=client.bca_correlation_tuple(json.loads(envelope.canonical_json())),
    )
    final_state = str((terminal_receipt.get("event_payload") or {}).get("delivery_state") or "")
    if final_state != "acknowledged":
        raise ContractError(f"dispatch ended with negative worker receipt state: {final_state}")
    return {
        "ok": bool(result.get("ok")) and final_state == "acknowledged",
        "assignment_id": envelope.assignment_id,
        "attempt_id": envelope.attempt_id,
        "payload_digest": envelope.digest(),
        "controller_epoch": controller_epoch,
        "bca_reservation": reservation,
        "bca_terminal_receipt": terminal_receipt,
        "result": result,
        "delivery_state": final_state,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch one already-authorized COS task slice")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--controller-epoch", type=int, required=True)
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--authorization-limit", action="append", dest="limits", default=[])
    parser.add_argument(
        "--worker-receipt-adapter",
        required=True,
        metavar="MODULE:CALLABLE",
        help="enrolled worker adapter returning a completion sink; never controller credentials",
    )
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        config = CoordConfig.load(expected_principal_id=manifest.controller_coord_agent_id)
        module_name, separator, callable_name = args.worker_receipt_adapter.partition(":")
        if not separator or not module_name or not callable_name:
            raise ContractError("worker receipt adapter must be MODULE:CALLABLE")
        adapter = getattr(importlib.import_module(module_name), callable_name, None)
        if not callable(adapter):
            raise ContractError("worker receipt adapter callable was not found")
        result = dispatch_task(
            client=CoordClient(config),
            manifest=manifest,
            task_id=args.task_id,
            worker_id=args.worker_id,
            controller_epoch=args.controller_epoch,
            generation=args.generation,
            authorization_limits=args.limits,
            worker_receipt=adapter,
        )
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
