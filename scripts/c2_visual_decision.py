#!/usr/bin/env python3
"""Generic visual evidence and LLM-authored terminal action contracts."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from typing import Any

from c2_contract import ContractError, RunManifest
from c2_runtime_observation import OBSERVATION_SCHEMA_VERSION, RuntimeObservation

ALLOWED_VISUAL_ACTIONS = {
    "send_text",
    "press_enter",
    "press_escape",
    "press_tab",
    "clear_line",
}
MAX_OBSERVATION_AGE_SECONDS = 120


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{name} is required")
    return text


@dataclass(frozen=True)
class VisualObservation:
    observation_schema_version: int
    worker_id: str
    iterm_session_id: str
    runtime_observation: RuntimeObservation
    screenshot_sha256: str
    captured_ts: float
    summary: str
    controller_epoch: int
    worker_epoch: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VisualObservation":
        schema_version = value.get("observation_schema_version")
        if schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ContractError(
                f"unsupported observation_schema_version: {schema_version!r}"
            )
        digest = _required(value.get("screenshot_sha256"), "screenshot_sha256")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ContractError("screenshot_sha256 must be lowercase SHA-256")
        captured = value.get("captured_ts")
        epoch = value.get("controller_epoch")
        worker_epoch = value.get("worker_epoch")
        if not isinstance(captured, (int, float)) or not math.isfinite(float(captured)):
            raise ContractError("captured_ts must be a finite number")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ContractError("controller_epoch must be a positive integer")
        if isinstance(worker_epoch, bool) or not isinstance(worker_epoch, int) or worker_epoch < 1:
            raise ContractError("worker_epoch must be a positive integer")
        return cls(
            observation_schema_version=schema_version,
            worker_id=_required(value.get("worker_id"), "worker_id"),
            iterm_session_id=_required(value.get("iterm_session_id"), "iterm_session_id"),
            runtime_observation=RuntimeObservation.from_dict(
                {
                    "runtime": value.get("runtime"),
                    "profile_id": value.get("profile_id"),
                    "profile_version": value.get("profile_version"),
                    "prompt_state": value.get("prompt_state"),
                    "input_buffer_state": value.get("input_buffer_state"),
                    "cli_session_id": value.get("cli_session_id"),
                    "coord_session_id": value.get("coord_session_id"),
                }
            ),
            screenshot_sha256=digest,
            captured_ts=float(captured),
            summary=_required(value.get("summary"), "summary"),
            controller_epoch=epoch,
            worker_epoch=worker_epoch,
        )

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def validate_for(self, manifest: RunManifest, *, now_ts: float | None = None) -> None:
        worker = manifest.worker(self.worker_id)
        if worker.iterm_session_id != self.iterm_session_id:
            raise ContractError("visual observation targets stale iTerm identity")
        if not worker.observation_profile_id or worker.observation_profile_version < 1:
            raise ContractError("worker has no enrolled runtime observation profile")
        self.runtime_observation.validate_registration(
            runtime=worker.runtime,
            profile_id=worker.observation_profile_id,
            profile_version=worker.observation_profile_version,
            cli_session_id=worker.cli_session_id,
            coord_session_id=worker.coord_session_id,
        )
        if self.controller_epoch < 1:
            raise ContractError("invalid controller epoch")
        age = (time.time() if now_ts is None else now_ts) - self.captured_ts
        if age < 0 or age > MAX_OBSERVATION_AGE_SECONDS:
            raise ContractError("visual observation is stale")


@dataclass(frozen=True)
class VisualDecision:
    observation_digest: str
    action: str
    text: str
    rationale: str
    decided_by: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VisualDecision":
        action = _required(value.get("action"), "action")
        if action not in ALLOWED_VISUAL_ACTIONS:
            raise ContractError(f"unsupported visual action: {action}")
        decided_by = _required(value.get("decided_by"), "decided_by")
        if not decided_by.startswith("llm:"):
            raise ContractError("visual decision must identify its supervising LLM")
        text = str(value.get("text") or "")
        if action == "send_text" and not text:
            raise ContractError("send_text action requires text")
        if action == "send_text" and any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ContractError("send_text action cannot include terminal control characters")
        if action != "send_text" and text:
            raise ContractError(f"{action} action cannot include text")
        return cls(
            observation_digest=_required(value.get("observation_digest"), "observation_digest"),
            action=action,
            text=text,
            rationale=_required(value.get("rationale"), "rationale"),
            decided_by=decided_by,
            idempotency_key=_required(value.get("idempotency_key"), "idempotency_key"),
        )

    def validate_for(self, observation: VisualObservation) -> None:
        if self.observation_digest != observation.digest():
            raise ContractError("visual decision does not bind the observation")
        observation.runtime_observation.permits_action(self.action)

    def terminal_text(self) -> str:
        if self.action == "press_enter":
            return "\r"
        if self.action == "press_escape":
            return "\x1b"
        if self.action == "press_tab":
            return "\t"
        if self.action == "clear_line":
            return "\x15"
        return self.text
