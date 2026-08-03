"""Compose durable task/attempt state with the fenced terminal edge."""

from __future__ import annotations

import argparse
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


def build_envelope(
    *,
    manifest: RunManifest,
    task: dict[str, Any],
    worker_id: str,
    controller_epoch: int,
    generation: int,
    authorization_limits: list[str],
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
    return DispatchEnvelope(
        assignment_id=assignment_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worker_id=worker.worker_id,
        cli_session_id=worker.cli_session_id,
        coord_session_id=worker.coord_session_id,
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
    edge_dispatch: Callable[[dict[str, Any]], dict[str, Any]] = default_edge_dispatch,
) -> dict[str, Any]:
    client.verify_live_epoch(SUPERVISOR_RESOURCE, controller_epoch)
    task = client.task(task_id)
    if not task:
        raise ContractError(f"task not found: {task_id}")
    envelope = build_envelope(
        manifest=manifest,
        task=task,
        worker_id=worker_id,
        controller_epoch=controller_epoch,
        generation=generation,
        authorization_limits=authorization_limits,
    )
    worker = manifest.worker(worker_id)
    client.ensure_attempt(
        attempt_id=envelope.attempt_id,
        task_id=envelope.task_id,
        session_id=worker.coord_session_id,
    )
    # The edge performs the final supervisor and worker-reservation fencing.
    result = edge_dispatch(envelope=json.loads(envelope.canonical_json()))
    return {
        "ok": bool(result.get("ok")),
        "assignment_id": envelope.assignment_id,
        "attempt_id": envelope.attempt_id,
        "payload_digest": envelope.digest(),
        "controller_epoch": controller_epoch,
        "result": result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch one already-authorized COS task slice")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--controller-epoch", type=int, required=True)
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--authorization-limit", action="append", dest="limits", default=[])
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        config = CoordConfig.load(expected_principal_id=manifest.controller_coord_agent_id)
        result = dispatch_task(
            client=CoordClient(config),
            manifest=manifest,
            task_id=args.task_id,
            worker_id=args.worker_id,
            controller_epoch=args.controller_epoch,
            generation=args.generation,
            authorization_limits=args.limits,
        )
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
