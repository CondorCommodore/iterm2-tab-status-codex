#!/usr/bin/env python3
"""Worker-authenticated BCA receipt adapter for durable COS dispatch."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from c2_contract import ContractError, DispatchEnvelope
from c2_coord_client import CoordClient, CoordConfig


def _session_capability_path(session_id: str) -> Path:
    return Path.home() / ".coordination" / "session-capabilities" / session_id


def load_session_capability(session_id: str) -> str:
    env_session_id = str(os.environ.get("COORD_SESSION_ID") or "").strip()
    env_capability = str(os.environ.get("COORD_SESSION_CAPABILITY") or "").strip()
    if env_capability and env_session_id == session_id:
        return env_capability
    try:
        return _session_capability_path(session_id).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ContractError(
            f"worker session capability unavailable for session {session_id}"
        ) from exc


def _receipt_outcome(result: dict[str, Any]) -> tuple[str, str | None]:
    if not isinstance(result, dict):
        raise ContractError("edge dispatch result must be an object")
    if result.get("ok") is True:
        return "acknowledged", None
    reason = str(result.get("error") or "edge dispatch failed").strip()
    return "refused", reason


def bca_receipt_adapter(
    envelope: DispatchEnvelope,
    *,
    config_loader: Callable[..., CoordConfig] = CoordConfig.load,
    client_factory: Callable[[CoordConfig], CoordClient] = CoordClient,
    session_capability_loader: Callable[[str], str] = load_session_capability,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    config = config_loader(expected_principal_id=envelope.coord_agent_id)
    if config.principal_id != envelope.coord_agent_id:
        raise ContractError("worker receipt adapter principal does not match envelope worker")
    session_capability = session_capability_loader(envelope.coord_session_id).strip()
    if not session_capability:
        raise ContractError(
            f"worker session capability unavailable for session {envelope.coord_session_id}"
        )
    client = client_factory(config)

    def complete(result: dict[str, Any]) -> dict[str, Any]:
        outcome, reason = _receipt_outcome(result)
        return client.post_bca_receipt(
            envelope.idempotency_key,
            outcome=outcome,
            attempt_number=1,
            worker_id=envelope.coord_agent_id,
            session_id=envelope.coord_session_id,
            payload_digest=envelope.digest(),
            session_capability=session_capability,
            reason=reason,
        )

    return complete
