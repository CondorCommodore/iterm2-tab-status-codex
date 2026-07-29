#!/usr/bin/env python3
"""Machine-local COS action checkpoint and model-progress receipts.

The checkpoint is a recovery prompt, not task authority.  All referenced work,
ownership, attempts, and evidence remain durable in coord-api.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from c2_contract import ContractError, ReceiptStore, RunManifest

SCHEMA = "c2-current-actions-v1"
STATUSES = {"active", "waiting", "blocked", "complete"}
MIN_NEXT_CHECK_SECONDS = 60
MAX_NEXT_CHECK_SECONDS = 1800
DEFAULT_NEXT_CHECK_SECONDS = 300
MAX_ACTION_BYTES = 65_536
MAX_CLOCK_SKEW_SECONDS = 30
HEX_256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_HEADINGS = (
    "## Current state",
    "## Next actions",
    "## Expected observations",
    "## Boundaries",
    "## Durable references",
    "## Rewrite or stop condition",
)


def _iso(ts: float | None = None) -> str:
    value = time.time() if ts is None else ts
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: object, field: str) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


@dataclass(frozen=True)
class CurrentActions:
    path: Path
    raw: bytes
    header: dict[str, Any]
    body: str
    digest: str
    written_ts: float
    next_check_ts: float

    @property
    def generation(self) -> int:
        return int(self.header["generation"])

    @property
    def controller_epoch(self) -> int:
        return int(self.header["controller_epoch"])

    @property
    def decision_digest(self) -> str:
        return str(self.header["decision_digest"])

    @property
    def status(self) -> str:
        return str(self.header["status"])


def parse_actions(
    path: Path,
    *,
    manifest: RunManifest | None = None,
    now_ts: float | None = None,
) -> CurrentActions:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"current actions are unreadable: {path}: {exc}") from exc
    if not raw or len(raw) > MAX_ACTION_BYTES:
        raise ContractError("current actions must be non-empty and at most 65536 bytes")
    if b"\x00" in raw or b"\r" in raw:
        raise ContractError("current actions contain forbidden control characters")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("current actions must be UTF-8") from exc
    lines = text.splitlines()
    if len(lines) < 4 or lines[0] != f"--- {SCHEMA}" or lines[2] != "---":
        raise ContractError("current actions must start with the versioned JSON header")
    try:
        header = json.loads(lines[1])
    except json.JSONDecodeError as exc:
        raise ContractError("current actions header must be one JSON object") from exc
    if not isinstance(header, dict):
        raise ContractError("current actions header must be one JSON object")
    required = {
        "manifest_id",
        "controller_id",
        "controller_cli_session_id",
        "controller_coord_session_id",
        "controller_iterm_session_id",
        "controller_epoch",
        "ownership",
        "generation",
        "decision_digest",
        "previous_action_digest",
        "status",
        "written_at",
        "next_check_at",
        "references",
    }
    missing = sorted(required - set(header))
    if missing:
        raise ContractError("current actions header missing: " + ", ".join(missing))
    epoch = header["controller_epoch"]
    generation = header["generation"]
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ContractError("current actions controller_epoch must be positive")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ContractError("current actions generation must be positive")
    if header["ownership"] not in {"visible", "headless"}:
        raise ContractError("current actions ownership must be visible or headless")
    if header["status"] not in STATUSES:
        raise ContractError("current actions status is unsupported")
    if not HEX_256.match(str(header["decision_digest"])):
        raise ContractError("current actions decision_digest must be lowercase SHA-256")
    previous = str(header["previous_action_digest"] or "")
    if previous and not HEX_256.match(previous):
        raise ContractError("previous_action_digest must be empty or lowercase SHA-256")
    if not isinstance(header["references"], list):
        raise ContractError("current actions references must be a list")
    written_ts = _timestamp(header["written_at"], "written_at")
    now_ts = time.time() if now_ts is None else now_ts
    if written_ts > now_ts + MAX_CLOCK_SKEW_SECONDS:
        raise ContractError("current actions written_at is too far in the future")
    next_check_ts = _timestamp(header["next_check_at"], "next_check_at")
    delay = next_check_ts - written_ts
    if not MIN_NEXT_CHECK_SECONDS <= delay <= MAX_NEXT_CHECK_SECONDS:
        raise ContractError("next_check_at must be 60 to 1800 seconds after written_at")
    body = "\n".join(lines[3:]).strip()
    missing_headings = [heading for heading in REQUIRED_HEADINGS if heading not in body]
    if missing_headings:
        raise ContractError("current actions body missing: " + ", ".join(missing_headings))
    if header["status"] == "complete":
        completion_refs = header.get("completion_refs")
        if not isinstance(completion_refs, list) or not completion_refs:
            raise ContractError("complete current actions require durable completion_refs")
    if manifest is not None:
        expected = {
            "manifest_id": manifest.manifest_id,
            "controller_id": manifest.controller_id,
            "controller_cli_session_id": manifest.controller_cli_session_id,
            "controller_coord_session_id": manifest.controller_coord_session_id,
            "controller_iterm_session_id": manifest.controller_iterm_session_id,
        }
        for field, value in expected.items():
            if header[field] != value:
                raise ContractError(f"current actions {field} does not match manifest")
    return CurrentActions(
        path=path,
        raw=raw,
        header=header,
        body=body,
        digest=hashlib.sha256(raw).hexdigest(),
        written_ts=written_ts,
        next_check_ts=next_check_ts,
    )


def seed_actions(
    *,
    manifest: RunManifest,
    path: Path,
    decision_digest: str,
    epoch: int,
    now_ts: float | None = None,
) -> CurrentActions:
    now_ts = time.time() if now_ts is None else now_ts
    header = {
        "manifest_id": manifest.manifest_id,
        "controller_id": manifest.controller_id,
        "controller_cli_session_id": manifest.controller_cli_session_id,
        "controller_coord_session_id": manifest.controller_coord_session_id,
        "controller_iterm_session_id": manifest.controller_iterm_session_id,
        "controller_epoch": epoch,
        "ownership": "visible",
        "generation": 1,
        "decision_digest": decision_digest,
        "previous_action_digest": "",
        "status": "active",
        "written_at": _iso(now_ts),
        "next_check_at": _iso(now_ts + DEFAULT_NEXT_CHECK_SECONDS),
        "references": list(manifest.plan_paths),
    }
    body = """## Current state
Bootstrap COS is armed and must reconstruct current state from the referenced plans and coord feed.

## Next actions
1. Read every authoritative plan path and the actionable coord feed.
2. Reconcile registered workers and record the next bounded decisions.
3. Publish a new current-actions checkpoint before ending the turn.

## Expected observations
Exact worker identities, actionable tasks/messages, active PR transitions, and durable blockers.

## Boundaries
Apply the run manifest permissions and hard boundaries. Local files grant no task authority.

## Durable references
Use existing coord task, attempt, message, lease, result, and evidence identifiers.

## Rewrite or stop condition
Rewrite after every material transition; stop automatic work only when status is complete or
standby.
"""
    raw = (
        f"--- {SCHEMA}\n{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n---\n{body}"
    ).encode()
    _atomic_bytes(path, raw)
    return parse_actions(path, manifest=manifest)


def rebind_actions(
    *,
    current: CurrentActions,
    path: Path,
    manifest: RunManifest,
    decision_digest: str,
    epoch: int,
    ownership: str,
    now_ts: float | None = None,
) -> CurrentActions:
    """Carry the same bounded intent across a fenced controller epoch."""
    now_ts = time.time() if now_ts is None else now_ts
    header = {
        **current.header,
        "controller_epoch": epoch,
        "ownership": ownership,
        "generation": current.generation + 1,
        "decision_digest": decision_digest,
        "previous_action_digest": current.digest,
        "written_at": _iso(now_ts),
        "next_check_at": _iso(now_ts + DEFAULT_NEXT_CHECK_SECONDS),
    }
    raw = (
        f"--- {SCHEMA}\n{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
        f"---\n{current.body}\n"
    ).encode()
    _atomic_bytes(path, raw)
    return parse_actions(path, manifest=manifest)


def checkpoint_actions(
    *,
    source: Path,
    destination: Path,
    manifest: RunManifest,
    live_epoch: int,
    receipts_path: Path,
    expected_decision_digest: str | None = None,
    allow_complete: bool = False,
) -> dict[str, Any]:
    candidate = parse_actions(source, manifest=manifest)
    if candidate.controller_epoch != live_epoch:
        raise ContractError("current actions epoch is not the live controller epoch")
    if (
        expected_decision_digest is not None
        and candidate.decision_digest != expected_decision_digest
    ):
        raise ContractError("current actions decision_digest is not current")
    if candidate.status == "complete" and not allow_complete:
        raise ContractError("complete is forbidden while a deterministic wake is required")
    previous = None
    if destination.exists():
        previous = parse_actions(destination, manifest=manifest)
        if candidate.generation <= previous.generation:
            raise ContractError("current actions generation must increase")
        if candidate.header["previous_action_digest"] != previous.digest:
            raise ContractError("current actions previous_action_digest does not match")
    _atomic_bytes(destination, candidate.raw)
    published = parse_actions(destination, manifest=manifest)
    receipt = {
        "idempotency_key": (
            f"c2-action-checkpoint:{live_epoch}:{published.generation}:{published.digest}"
        ),
        "kind": "action-checkpoint",
        "recorded_at": _iso(),
        "recorded_ts": time.time(),
        "controller_epoch": live_epoch,
        "generation": published.generation,
        "action_digest": published.digest,
        "decision_digest": published.decision_digest,
        "status": published.status,
    }
    store = ReceiptStore(receipts_path)
    for existing in store.records():
        if existing.get("idempotency_key") == receipt["idempotency_key"]:
            return {**existing, "duplicate": True}
    store.append(receipt)
    return receipt


def record_coord_acceptance(
    *,
    checkpoint_receipt: dict[str, Any],
    coord_response: dict[str, Any],
    receipts_path: Path,
) -> dict[str, Any]:
    """Cache proof that coord-api accepted the checkpoint audit message."""
    checkpoint_key = str(checkpoint_receipt.get("idempotency_key") or "")
    if checkpoint_receipt.get("kind") != "action-checkpoint" or not checkpoint_key:
        raise ContractError("coord acceptance requires an action-checkpoint receipt")
    message_id = coord_response.get("id")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 1:
        raise ContractError("coord acceptance requires a server-owned message id")
    if coord_response.get("accepted") is not True:
        raise ContractError("coord acceptance requires verified server readback")
    receipt = {
        "idempotency_key": f"{checkpoint_key}:coord-accepted",
        "kind": "action-checkpoint-coord-accepted",
        "recorded_at": _iso(),
        "recorded_ts": time.time(),
        "controller_epoch": checkpoint_receipt.get("controller_epoch"),
        "generation": checkpoint_receipt.get("generation"),
        "action_digest": checkpoint_receipt.get("action_digest"),
        "checkpoint_idempotency_key": checkpoint_key,
        "coord_message_id": message_id,
        "coord_response_digest": hashlib.sha256(
            json.dumps(coord_response, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    store = ReceiptStore(receipts_path)
    for existing in store.records():
        if existing.get("idempotency_key") == receipt["idempotency_key"]:
            return {**existing, "duplicate": True}
    store.append(receipt)
    return receipt


def acknowledge_actions(
    *,
    actions_path: Path,
    progress_path: Path,
    receipts_path: Path,
    manifest: RunManifest,
    digest: str,
    generation: int,
    epoch: int,
    ownership: str,
) -> dict[str, Any]:
    actions = parse_actions(actions_path, manifest=manifest)
    if (digest, generation, epoch) != (
        actions.digest,
        actions.generation,
        actions.controller_epoch,
    ):
        raise ContractError("action acknowledgment does not match current generation")
    if ownership != actions.header["ownership"]:
        raise ContractError("action acknowledgment ownership does not match checkpoint")
    receipt = {
        "idempotency_key": f"c2-action-ack:{epoch}:{generation}:{digest}",
        "kind": "action-ack",
        "recorded_at": _iso(),
        "recorded_ts": time.time(),
        "controller_epoch": epoch,
        "generation": generation,
        "action_digest": digest,
        "decision_digest": actions.decision_digest,
        "ownership": ownership,
        "controller_cli_session_id": manifest.controller_cli_session_id,
        "controller_coord_session_id": manifest.controller_coord_session_id,
        "controller_iterm_session_id": manifest.controller_iterm_session_id,
    }
    store = ReceiptStore(receipts_path)
    for existing in store.records():
        if existing.get("idempotency_key") == receipt["idempotency_key"]:
            return {**existing, "duplicate": True}
    store.append(receipt)
    _atomic_json(progress_path, receipt)
    return receipt


def action_wake_due(
    actions: CurrentActions,
    *,
    decision_digest: str,
    now_ts: float | None = None,
) -> tuple[bool, str]:
    now_ts = time.time() if now_ts is None else now_ts
    if actions.decision_digest != decision_digest:
        return True, "deterministic decision changed"
    if actions.status == "complete":
        return False, "actions complete"
    if now_ts >= actions.next_check_ts:
        return True, "current action deadline reached"
    return False, "current actions not due"
