#!/usr/bin/env python3
"""Versioned, fail-closed runtime observation profiles for terminal actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from c2_contract import RUNTIME_OBSERVATION_PROFILES, ContractError

OBSERVATION_SCHEMA_VERSION = 1
PROMPT_STATES = {"ready", "running", "needs_input", "unknown"}
INPUT_BUFFER_STATES = {"empty", "nonempty", "unknown"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name} must be a positive integer") from exc
    if result < 1 or str(result) != _text(value):
        raise ContractError(f"{name} must be a positive integer")
    return result


def profile_supported(runtime: str, profile_id: str, profile_version: int) -> bool:
    return (runtime, profile_id, profile_version) in RUNTIME_OBSERVATION_PROFILES


@dataclass(frozen=True)
class RuntimeObservation:
    runtime: str
    profile_id: str
    profile_version: int
    prompt_state: str
    input_buffer_state: str
    cli_session_id: str
    coord_session_id: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeObservation":
        runtime = _text(value.get("runtime")).lower()
        profile_id = _text(value.get("profile_id")).lower()
        profile_version = _positive_int(value.get("profile_version"), "profile_version")
        prompt_state = _text(value.get("prompt_state")).lower()
        input_buffer_state = _text(value.get("input_buffer_state")).lower()
        if prompt_state not in PROMPT_STATES:
            raise ContractError(f"unsupported prompt_state: {prompt_state or '<empty>'}")
        if input_buffer_state not in INPUT_BUFFER_STATES:
            raise ContractError(
                f"unsupported input_buffer_state: {input_buffer_state or '<empty>'}"
            )
        if not profile_supported(runtime, profile_id, profile_version):
            raise ContractError(
                "unsupported runtime observation profile: "
                f"{runtime}/{profile_id}/v{profile_version}"
            )
        cli_session_id = _text(value.get("cli_session_id"))
        coord_session_id = _text(value.get("coord_session_id"))
        if not cli_session_id:
            raise ContractError("runtime observation cli_session_id is required")
        if not coord_session_id:
            raise ContractError("runtime observation coord_session_id is required")
        return cls(
            runtime=runtime,
            profile_id=profile_id,
            profile_version=profile_version,
            prompt_state=prompt_state,
            input_buffer_state=input_buffer_state,
            cli_session_id=cli_session_id,
            coord_session_id=coord_session_id,
        )

    @classmethod
    def from_session_variables(
        cls,
        values: dict[str, str],
    ) -> "RuntimeObservation | None":
        raw = {
            "runtime": values.get("user.workerRuntime"),
            "profile_id": values.get("user.workerObservationProfile"),
            "profile_version": values.get("user.workerObservationProfileVersion"),
            "prompt_state": values.get("user.workerPromptState"),
            "input_buffer_state": values.get("user.workerInputBufferState"),
            "cli_session_id": values.get("user.cliSessionId"),
            "coord_session_id": values.get("user.coordSessionId"),
        }
        try:
            return cls.from_dict(raw)
        except ContractError:
            return None

    @property
    def prompt_ready(self) -> bool:
        return self.prompt_state == "ready" and self.input_buffer_state == "empty"

    def validate_registration(
        self,
        *,
        runtime: str,
        profile_id: str,
        profile_version: int,
        cli_session_id: str,
        coord_session_id: str,
    ) -> None:
        if self.runtime != runtime:
            raise ContractError("runtime observation does not match worker runtime")
        if self.profile_id != profile_id or self.profile_version != profile_version:
            raise ContractError("runtime observation profile does not match registration")
        if self.cli_session_id != cli_session_id:
            raise ContractError("runtime observation targets stale cli session identity")
        if self.coord_session_id != coord_session_id:
            raise ContractError("runtime observation targets stale coord session identity")

    def permits_action(self, action: str) -> None:
        if self.input_buffer_state != "empty":
            raise ContractError("terminal action requires a verified empty input buffer")
        if action == "press_escape":
            if self.prompt_state != "running":
                raise ContractError("Escape requires a verified running runtime")
            return
        if self.prompt_state != "ready":
            raise ContractError("terminal action requires a verified prompt-ready runtime")
