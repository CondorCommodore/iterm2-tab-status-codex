#!/usr/bin/env python3
"""Inert independent-observer client flow for broker-fenced runtime evidence.

This module deliberately does not sense a terminal, enroll a principal, install
hooks, run a daemon, or write iTerm state itself.  A later bounded experiment
must inject both an independent sensing provider and a local presentation
adapter.  Until then, missing or ambiguous evidence fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from c2_contract import ContractError, WorkerRegistration
from c2_coord_client import CoordClient, CoordError
from c2_runtime_hook import SignedRuntimeHookObservation, session_variable_values


def _timestamp(value: object, name: str) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"pending runtime challenge {name} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"pending runtime challenge {name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"pending runtime challenge {name} lacks timezone")
    return parsed.astimezone(timezone.utc).timestamp()


@dataclass(frozen=True)
class PendingRuntimeChallenge:
    challenge_id: str
    worker_id: str
    iterm_session_id: str
    cli_session_id: str
    coord_session_id: str
    controller_epoch: int
    worker_epoch: int
    binding_sha256: str
    armed_at: float
    expires_at: float
    runtime: str
    profile_id: str
    profile_version: int

    @classmethod
    def from_broker(cls, value: dict[str, object]) -> "PendingRuntimeChallenge":
        return cls(
            challenge_id=str(value["challenge_id"]),
            worker_id=str(value["worker_id"]),
            iterm_session_id=str(value["iterm_session_id"]),
            cli_session_id=str(value["cli_session_id"]),
            coord_session_id=str(value["coord_session_id"]),
            controller_epoch=int(value["controller_epoch"]),
            worker_epoch=int(value["worker_epoch"]),
            binding_sha256=str(value["binding_sha256"]),
            armed_at=_timestamp(value["armed_at"], "armed_at"),
            expires_at=_timestamp(value["expires_at"], "expires_at"),
            runtime=str(value["runtime"]),
            profile_id=str(value["profile_id"]),
            profile_version=int(value["profile_version"]),
        )

    def validate_target(
        self,
        worker: WorkerRegistration,
        *,
        expected_controller_epoch: int,
        expected_worker_epoch: int,
        now_ts: float,
    ) -> None:
        expected = {
            "worker_id": worker.worker_id,
            "iterm_session_id": worker.iterm_session_id,
            "cli_session_id": worker.cli_session_id,
            "coord_session_id": worker.coord_session_id,
            "runtime": worker.runtime,
            "profile_id": worker.observation_profile_id,
            "profile_version": worker.observation_profile_version,
            "controller_epoch": expected_controller_epoch,
            "worker_epoch": expected_worker_epoch,
        }
        actual = {name: getattr(self, name) for name in expected}
        mismatches = [name for name in expected if actual[name] != expected[name]]
        if mismatches:
            raise ContractError(
                "pending runtime challenge targets stale registration: " + ", ".join(mismatches)
            )
        if not worker.observation_profile_id:
            raise ContractError("worker has no registered runtime observation profile")
        if self.armed_at > now_ts:
            raise ContractError("pending runtime challenge is not yet armed")
        if self.expires_at <= now_ts:
            raise ContractError("pending runtime challenge is expired")


@dataclass(frozen=True)
class ObserverPublication:
    report: SignedRuntimeHookObservation
    observation_digest: str
    challenge_id: str
    challenge_binding_sha256: str
    observer_principal: str

    def session_variables(self) -> dict[str, str]:
        """Return only the existing hook variables from the broker-accepted report."""
        return session_variable_values(self.report)


@dataclass
class ObserverCursor:
    """Machine-local replay cache; coord-api remains durable authority."""

    processed_challenges: dict[str, str] = field(default_factory=dict)
    sequence_by_iterm_session: dict[str, int] = field(default_factory=dict)


SenseFn = Callable[[WorkerRegistration, PendingRuntimeChallenge], SignedRuntimeHookObservation]


class RuntimeObserverCycle:
    def __init__(
        self,
        client: CoordClient,
        *,
        observer_principal: str,
        sense: SenseFn,
        publish_to_session: Callable[[ObserverPublication], None],
        cursor: ObserverCursor | None = None,
        now: Callable[[], float],
    ) -> None:
        if client.config.principal_id != observer_principal:
            raise ContractError(
                "runtime observer client principal does not match observer identity"
            )
        self.client = client
        self.observer_principal = observer_principal
        self.sense = sense
        self.publish_to_session = publish_to_session
        self.cursor = cursor or ObserverCursor()
        self.now = now

    def pending(self, *, limit: int = 16) -> list[PendingRuntimeChallenge]:
        return [
            PendingRuntimeChallenge.from_broker(value)
            for value in self.client.pending_runtime_observation_challenges(limit=limit)
        ]

    def process(
        self,
        challenge: PendingRuntimeChallenge,
        worker: WorkerRegistration,
        *,
        expected_controller_epoch: int,
        expected_worker_epoch: int,
    ) -> ObserverPublication:
        now_ts = self.now()
        challenge.validate_target(
            worker,
            expected_controller_epoch=expected_controller_epoch,
            expected_worker_epoch=expected_worker_epoch,
            now_ts=now_ts,
        )
        prior_digest = self.cursor.processed_challenges.get(challenge.challenge_id)
        if prior_digest is not None:
            raise ContractError("pending runtime challenge was already processed locally")
        report = self.sense(worker, challenge)
        if not isinstance(report, SignedRuntimeHookObservation):
            raise ContractError("independent runtime sensing returned no signed observation")
        observation = report.runtime_observation
        observation.validate_registration(
            runtime=worker.runtime,
            profile_id=worker.observation_profile_id,
            profile_version=worker.observation_profile_version,
            cli_session_id=worker.cli_session_id,
            coord_session_id=worker.coord_session_id,
        )
        if report.iterm_session_id != worker.iterm_session_id:
            raise ContractError("runtime observation targets stale iTerm identity")
        if report.challenge_id != challenge.challenge_id:
            raise ContractError("runtime observation targets another challenge")
        if observation.prompt_state == "unknown" or observation.input_buffer_state == "unknown":
            raise ContractError("runtime observation state is unknown")
        if observation.input_buffer_state != "empty":
            raise ContractError("runtime observation input buffer is nonempty")
        if observation.prompt_state != "ready":
            raise ContractError("runtime observation is not prompt-ready")
        if report.observed_at < challenge.armed_at or report.observed_at > now_ts:
            raise ContractError("runtime observation is outside the armed causal interval")
        if report.observed_at >= challenge.expires_at:
            raise ContractError("runtime observation is outside the challenge lifetime")
        previous_sequence = self.cursor.sequence_by_iterm_session.get(worker.iterm_session_id, 0)
        if report.sequence <= previous_sequence:
            raise ContractError("runtime observation sequence is duplicate or reordered")
        coordinates = self.client.publish_runtime_observation(
            {**report.canonical_dict(), "signature": report.signature},
            expected_binding_sha256=challenge.binding_sha256,
        )
        publication = ObserverPublication(report=report, **coordinates)
        if (
            publication.challenge_id != challenge.challenge_id
            or publication.observation_digest != report.digest()
            or publication.challenge_binding_sha256 != challenge.binding_sha256
            or publication.observer_principal != self.observer_principal
        ):
            raise CoordError("broker publication returned mismatched durable coordinates")
        self.publish_to_session(publication)
        self.cursor.processed_challenges[challenge.challenge_id] = publication.observation_digest
        self.cursor.sequence_by_iterm_session[worker.iterm_session_id] = report.sequence
        return publication
