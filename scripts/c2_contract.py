#!/usr/bin/env python3
"""Shared C2 contract for bootstrap COS and future Control Room adapters.

The module intentionally contains no scheduler database.  Durable task,
attempt, lease, message, result, and evidence state belongs to coord-api.
Machine-local state is limited to the run manifest and append-only dispatch
receipts used to fail closed during a local retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SUPERVISOR_RESOURCE = "workspace:mikebook:c2-supervisor"
SUPERVISOR_TTL_SECONDS = 180
SUPERVISOR_RENEW_SECONDS = 60
RECONCILE_SECONDS = 30

WORKER_STATES = {
    "idle",
    "reserved",
    "running",
    "needs_input",
    "blocked",
    "stale",
    "lost",
    "unknown",
}
CONTROLLER_MODES = {
    "bootstrap-authoritative",
    "control-room-shadow",
    "control-room-authoritative",
    "bootstrap-standby",
}
RUNTIMES = {"codex", "claude"}
DISPATCH_TRANSPORTS = {"tab", "headless", "ab"}
TTY_RE = re.compile(r"^/dev/ttys[0-9A-Za-z_.-]+$")


class ContractError(ValueError):
    """Raised when a manifest or dispatch envelope violates the C2 contract."""


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{name} is required")
    return text


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{name} must be a list")
    result = tuple(str(item).strip() for item in value if str(item).strip())
    if not result:
        raise ContractError(f"{name} must not be empty")
    return result


@dataclass(frozen=True)
class WorkerRegistration:
    worker_id: str
    host: str
    runtime: str
    iterm_session_id: str
    tty: str
    cli_session_id: str
    coord_session_id: str
    coord_agent_id: str
    capabilities: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()
    role: str = "worker"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkerRegistration":
        runtime = _required(value.get("runtime"), "worker.runtime").lower()
        if runtime not in RUNTIMES:
            raise ContractError(f"unsupported worker runtime: {runtime}")
        tty = _required(value.get("tty"), "worker.tty")
        if not TTY_RE.match(tty):
            raise ContractError(f"unsafe worker tty: {tty!r}")
        role = str(value.get("role") or "worker").strip().lower()
        if role not in {"worker", "cos"}:
            raise ContractError(f"unsupported worker role: {role}")
        return cls(
            worker_id=_required(value.get("worker_id"), "worker.worker_id"),
            host=_required(value.get("host"), "worker.host"),
            runtime=runtime,
            iterm_session_id=_required(value.get("iterm_session_id"), "worker.iterm_session_id"),
            tty=tty,
            cli_session_id=_required(value.get("cli_session_id"), "worker.cli_session_id"),
            coord_session_id=_required(value.get("coord_session_id"), "worker.coord_session_id"),
            coord_agent_id=_required(value.get("coord_agent_id"), "worker.coord_agent_id"),
            capabilities=tuple(str(item) for item in value.get("capabilities", []) if str(item)),
            repositories=tuple(str(item) for item in value.get("repositories", []) if str(item)),
            role=role,
        )


@dataclass(frozen=True)
class RunManifest:
    manifest_id: str
    controller_id: str
    controller_host: str
    controller_runtime: str
    controller_iterm_session_id: str
    controller_tty: str
    controller_cli_session_id: str
    controller_coord_session_id: str
    controller_coord_agent_id: str
    workers: tuple[WorkerRegistration, ...]
    plan_paths: tuple[str, ...]
    permitted_repositories: tuple[str, ...]
    permitted_actions: tuple[str, ...]
    dispatch_transport: str = "ab"
    recovery_transport: str = "ab"
    ci_policy: dict[str, Any] = field(default_factory=dict)
    merge_policy: dict[str, Any] = field(default_factory=dict)
    hard_boundaries: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunManifest":
        controller = value.get("controller")
        if not isinstance(controller, dict):
            raise ContractError("controller must be an object")
        runtime = _required(controller.get("runtime"), "controller.runtime").lower()
        if runtime not in RUNTIMES:
            raise ContractError(f"unsupported controller runtime: {runtime}")
        controller_session = str(controller.get("iterm_session_id") or "").strip()
        controller_tty = str(controller.get("tty") or "").strip()
        if bool(controller_session) != bool(controller_tty):
            raise ContractError(
                "controller iterm_session_id and tty must either both be present or both be omitted"
            )
        if controller_tty and not TTY_RE.match(controller_tty):
            raise ContractError(f"unsafe controller tty: {controller_tty!r}")
        raw_workers = value.get("workers")
        if not isinstance(raw_workers, list) or not raw_workers:
            raise ContractError("workers must contain at least one registered worker")
        workers = tuple(WorkerRegistration.from_dict(item) for item in raw_workers)
        ids = [item.worker_id for item in workers]
        sessions = [item.iterm_session_id for item in workers]
        if len(ids) != len(set(ids)):
            raise ContractError("worker_id values must be unique")
        if len(sessions) != len(set(sessions)):
            raise ContractError("worker iterm_session_id values must be unique")
        if controller_session and controller_session in set(sessions):
            raise ContractError("controller session must not also be registered as a worker")
        dispatch_transport = str(value.get("dispatch_transport") or "ab").strip().lower()
        if dispatch_transport not in DISPATCH_TRANSPORTS:
            raise ContractError(f"unsupported dispatch_transport: {dispatch_transport}")
        recovery_transport = str(value.get("recovery_transport") or "ab").strip().lower()
        if recovery_transport not in DISPATCH_TRANSPORTS:
            raise ContractError(f"unsupported recovery_transport: {recovery_transport}")
        manifest = cls(
            manifest_id=_required(value.get("manifest_id"), "manifest_id"),
            controller_id=_required(controller.get("controller_id"), "controller.controller_id"),
            controller_host=_required(controller.get("host"), "controller.host"),
            controller_runtime=runtime,
            controller_iterm_session_id=controller_session,
            controller_tty=controller_tty,
            controller_cli_session_id=_required(
                controller.get("cli_session_id"), "controller.cli_session_id"
            ),
            controller_coord_session_id=_required(
                controller.get("coord_session_id"), "controller.coord_session_id"
            ),
            controller_coord_agent_id=_required(
                controller.get("coord_agent_id"), "controller.coord_agent_id"
            ),
            workers=workers,
            plan_paths=_strings(value.get("plan_paths"), "plan_paths"),
            permitted_repositories=_strings(
                value.get("permitted_repositories"), "permitted_repositories"
            ),
            permitted_actions=_strings(value.get("permitted_actions"), "permitted_actions"),
            dispatch_transport=dispatch_transport,
            recovery_transport=recovery_transport,
            ci_policy=dict(value.get("ci_policy") or {}),
            merge_policy=dict(value.get("merge_policy") or {}),
            hard_boundaries=tuple(str(item) for item in value.get("hard_boundaries", [])),
        )
        for worker in workers:
            collisions = manifest.controller_collides_with_worker(worker)
            if collisions:
                raise ContractError(
                    "controller session identities must not also be registered as a worker: "
                    + ", ".join(collisions)
                )
        return manifest

    def controller_has_visible_terminal(self) -> bool:
        return bool(self.controller_iterm_session_id and self.controller_tty)

    @property
    def controller_presentation(self) -> str:
        return "visible" if self.controller_has_visible_terminal() else "headless"

    def controller_collides_with_worker(self, worker: WorkerRegistration) -> list[str]:
        collisions: list[str] = []
        if worker.cli_session_id == self.controller_cli_session_id:
            collisions.append("cli_session_id")
        if worker.coord_session_id == self.controller_coord_session_id:
            collisions.append("coord_session_id")
        return collisions

    def transport_for(self, assignment_id: str) -> str:
        if self.dispatch_transport != "ab":
            return self.dispatch_transport
        digest = hashlib.sha256(f"{self.manifest_id}:{assignment_id}".encode("utf-8")).digest()
        return "tab" if digest[0] % 2 == 0 else "headless"

    def recovery_for(self, sequence: int) -> str:
        if self.recovery_transport != "ab":
            return self.recovery_transport
        return "tab" if sequence % 2 == 0 else "headless"

    def worker(self, worker_id: str) -> WorkerRegistration:
        for worker in self.workers:
            if worker.worker_id == worker_id:
                return worker
        raise ContractError(f"unregistered worker: {worker_id}")

    def permits(self, repo: str, actions: Iterable[str]) -> None:
        if repo not in self.permitted_repositories:
            raise ContractError(f"repository is outside run manifest: {repo}")
        denied = sorted(set(actions) - set(self.permitted_actions))
        if denied:
            raise ContractError(f"actions are outside run manifest: {', '.join(denied)}")


@dataclass(frozen=True)
class DispatchEnvelope:
    assignment_id: str
    task_id: str
    attempt_id: str
    worker_id: str
    cli_session_id: str
    coord_session_id: str
    objective: str
    repo: str
    worktree: str
    scope: tuple[str, ...]
    acceptance_tests: tuple[str, ...]
    stopping_condition: str
    report_destination: str
    authorization_limits: tuple[str, ...]
    permitted_actions: tuple[str, ...]
    controller_epoch: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DispatchEnvelope":
        raw_epoch = value.get("controller_epoch")
        if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int) or raw_epoch < 1:
            raise ContractError("controller_epoch must be a positive integer")
        return cls(
            assignment_id=_required(value.get("assignment_id"), "assignment_id"),
            task_id=_required(value.get("task_id"), "task_id"),
            attempt_id=_required(value.get("attempt_id"), "attempt_id"),
            worker_id=_required(value.get("worker_id"), "worker_id"),
            cli_session_id=_required(value.get("cli_session_id"), "cli_session_id"),
            coord_session_id=_required(value.get("coord_session_id"), "coord_session_id"),
            objective=_required(value.get("objective"), "objective"),
            repo=_required(value.get("repo"), "repo"),
            worktree=_required(value.get("worktree"), "worktree"),
            scope=_strings(value.get("scope"), "scope"),
            acceptance_tests=_strings(value.get("acceptance_tests"), "acceptance_tests"),
            stopping_condition=_required(value.get("stopping_condition"), "stopping_condition"),
            report_destination=_required(value.get("report_destination"), "report_destination"),
            authorization_limits=_strings(
                value.get("authorization_limits"), "authorization_limits"
            ),
            permitted_actions=_strings(value.get("permitted_actions"), "permitted_actions"),
            controller_epoch=raw_epoch,
            idempotency_key=_required(value.get("idempotency_key"), "idempotency_key"),
        )

    def validate_for(self, manifest: RunManifest) -> WorkerRegistration:
        worker = manifest.worker(self.worker_id)
        if self.cli_session_id != worker.cli_session_id:
            raise ContractError("dispatch cli_session_id does not match registration")
        if self.coord_session_id != worker.coord_session_id:
            raise ContractError("dispatch coord_session_id does not match registration")
        collisions = manifest.controller_collides_with_worker(worker)
        if collisions:
            raise ContractError(
                "controller session identities must not also be registered as a worker: "
                + ", ".join(collisions)
            )
        manifest.permits(self.repo, self.permitted_actions)
        if worker.repositories and self.repo not in worker.repositories:
            raise ContractError(f"worker is not registered for repository: {self.repo}")
        return worker

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def load_manifest(path: Path) -> RunManifest:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"run manifest not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"run manifest is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("run manifest root must be an object")
    return RunManifest.from_dict(value)


def load_envelope(path: Path) -> DispatchEnvelope:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"dispatch envelope is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("dispatch envelope root must be an object")
    return DispatchEnvelope.from_dict(value)


def normalize_worker_state(
    value: object,
    *,
    age_seconds: float | None = None,
    stale_after_seconds: float = 180.0,
    present: bool = True,
) -> str:
    if not present:
        return "lost"
    if age_seconds is not None and age_seconds > stale_after_seconds:
        return "stale"
    state = str(value or "").strip().lower()
    aliases = {
        "ready": "idle",
        "attention": "needs_input",
        "queued": "reserved",
        "processing": "running",
    }
    state = aliases.get(state, state)
    return state if state in WORKER_STATES else "unknown"


class ReceiptStore:
    """Append-only local receipt cache; coord-api remains the durable truth."""

    def __init__(self, path: Path):
        self.path = path

    def records(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        result: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    def has_idempotency_key(self, key: str) -> bool:
        return any(item.get("idempotency_key") == key for item in self.records())

    def append(self, receipt: dict[str, Any]) -> None:
        key = _required(receipt.get("idempotency_key"), "receipt.idempotency_key")
        if self.has_idempotency_key(key):
            raise ContractError(f"duplicate dispatch idempotency key: {key}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
