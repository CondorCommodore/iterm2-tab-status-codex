#!/usr/bin/env python3
"""Bootstrap COS supervisor lifecycle and deterministic reconciliation loop.

The supervisor is inert until ``arm`` writes its machine-local marker.  Its
only authority comes from the coord-api C2 resource lease; local state never
grants dispatch permission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import cos_dispatch_orchestrator
from c2_contract import (
    CONTROLLER_MODES,
    RECONCILE_SECONDS,
    SUPERVISOR_RENEW_SECONDS,
    SUPERVISOR_RESOURCE,
    SUPERVISOR_TTL_SECONDS,
    ContractError,
    ReceiptStore,
    RunManifest,
    arm_marker_status,
    load_manifest,
    manifest_contract_sha256,
    manifest_file_sha256,
    normalize_worker_state,
    render_arm_marker,
)
from c2_coord_client import (
    CoordClient,
    CoordConfig,
    CoordError,
    LeaseBlocked,
    LeaseHandle,
    LeaseLost,
)
from cos_current_actions import (
    acknowledge_actions,
    checkpoint_actions,
    commit_action_ack,
    parse_actions,
    parse_current_focus_projection,
    parse_program_projection,
    rebind_actions,
    record_coord_acceptance,
    seed_actions,
    update_projection_header,
)
from cos_external_state_sweep import sweep as sweep_external_state
from cos_iterm_edge_client import poke_controller, request_edge

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "cos-c2"
DEFAULT_MANIFEST = Path.home() / ".config" / "cos-c2" / "run-manifest.json"
DEFAULT_LIVE_STATE = Path.home() / ".claude" / "plans" / "fleet-reports" / "iterm-live-state.json"
REQUIRED_LAUNCHD_SERVICES = {
    "watchdog": "com.local.cos-bootstrap-watchdog",
    "terminal_edge": "com.local.cos-iterm-edge",
}
COS_DIRECTION_SCHEMA = "cos.direction.v1"
COS_WORK_KINDS = {"task", "pr"}


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def _load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {} if default is None else dict(default)
    return value if isinstance(value, dict) else ({} if default is None else dict(default))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _next_receipt_sequence(path: Path, prefix: str) -> int:
    highest = -1
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return 0
    for line in lines:
        try:
            value = json.loads(line)
            key = str(value.get("idempotency_key") or "") if isinstance(value, dict) else ""
            if key.startswith(prefix):
                highest = max(highest, int(key[len(prefix) :].split(":", 1)[0]))
        except (json.JSONDecodeError, ValueError):
            continue
    return highest + 1


def state_paths(state_dir: Path) -> dict[str, Path]:
    return {
        "armed": state_dir / "ARMED",
        "state": state_dir / "supervisor-state.json",
        "heartbeat": state_dir / "supervisor-heartbeat.json",
        "decision": state_dir / "decision-current.json",
        "program": state_dir / "program.md",
        "current_focus": state_dir / "current-focus.md",
        "actions": state_dir / "current-actions.txt",
        "action_progress": state_dir / "action-progress.json",
        "action_receipts": state_dir / "action-receipts.jsonl",
        "recovery_hold": state_dir / "recovery-hold.json",
        "external_state_sweep": state_dir / "external-state-sweep.json",
        "pokes": state_dir / "poke-receipts.jsonl",
    }


def _marker_status(path: Path, *, manifest: RunManifest, manifest_sha256: str) -> dict[str, Any]:
    try:
        marker_text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {
            "valid": False,
            "reason": "arm-marker-missing",
            "requires_explicit_rearm": True,
        }
    return arm_marker_status(
        marker_text,
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest_sha256,
    )


def _require_arm_marker(
    path: Path, *, manifest: RunManifest, manifest_sha256: str
) -> dict[str, Any]:
    observed = _marker_status(path, manifest=manifest, manifest_sha256=manifest_sha256)
    if observed.get("valid") is not True:
        raise ContractError(
            f"armed marker is not current ({observed.get('reason')}); explicit re-arm required"
        )
    return observed


def service_readiness(
    *,
    run: Any = subprocess.run,
    system: str | None = None,
    uid: int | None = None,
) -> dict[str, Any]:
    """Read launchd registration without starting, stopping, or kicking a service."""
    observed_system = system or platform.system()
    if observed_system != "Darwin":
        return {
            "supported": False,
            "ready": False,
            "reason": f"unsupported platform: {observed_system}",
            "services": {},
        }
    observed_uid = os.getuid() if uid is None else uid
    services: dict[str, dict[str, Any]] = {}
    for name, label in REQUIRED_LAUNCHD_SERVICES.items():
        try:
            result = run(
                ["launchctl", "print", f"gui/{observed_uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            loaded = result.returncode == 0
            detail = (
                "loaded" if loaded else (result.stderr or result.stdout or "not loaded").strip()
            )
        except (OSError, subprocess.SubprocessError) as exc:
            loaded = False
            detail = str(exc)
        services[name] = {
            "label": label,
            "loaded": loaded,
            "detail": detail[:240],
        }
    return {
        "supported": True,
        "ready": all(item["loaded"] for item in services.values()),
        "services": services,
    }


def _handle_from_state(state: dict[str, Any]) -> LeaseHandle | None:
    lease = state.get("lease")
    if not isinstance(lease, dict):
        return None
    epoch = lease.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        return None
    return LeaseHandle(
        resource=SUPERVISOR_RESOURCE,
        holder=str(lease.get("holder") or ""),
        epoch=epoch,
        expires_at=str(lease.get("expires_at")) if lease.get("expires_at") else None,
        lease=lease,
    )


def _save_authority(
    *,
    paths: dict[str, Path],
    state: dict[str, Any],
    handle: LeaseHandle,
    ownership: str,
) -> dict[str, Any]:
    updated = {
        **state,
        "authority": True,
        "resource": SUPERVISOR_RESOURCE,
        "lease": handle.lease,
        "controller_epoch": handle.epoch,
        "ownership": ownership,
        "last_renewed_at": _iso(),
        "last_renewed_ts": time.time(),
        "last_error": None,
    }
    _atomic_json(paths["state"], updated)
    return updated


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _write_program_projection(
    *,
    path: Path,
    manifest: RunManifest,
    decision: dict[str, Any],
    current_actions: Any,
    ownership: str,
    epoch: int,
) -> dict[str, Any]:
    payload = render_program_projection(
        manifest=manifest,
        decision=decision,
        current_actions=current_actions,
        ownership=ownership,
        epoch=epoch,
    )
    _atomic_text(path, payload)
    projection = parse_program_projection(path, manifest=manifest)
    if projection.action_digest != current_actions.digest:
        raise ContractError("program projection action_digest does not match current actions")
    if projection.decision_digest != str(decision.get("decision_digest") or ""):
        raise ContractError("program projection decision_digest does not match decision")
    if projection.controller_epoch != epoch:
        raise ContractError("program projection controller_epoch does not match live epoch")
    return {
        "digest": projection.digest,
        "written_at": projection.header["written_at"],
    }


def _focus_entry(decision: dict[str, Any]) -> dict[str, Any]:
    items = decision.get("actionable_items", [])
    if not isinstance(items, list):
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"task", "pr", "message"}:
            continue
        if kind == "task":
            ref = str(item.get("task_id") or item.get("id") or "").strip()
            display = str(item.get("display_id") or ref or "task").strip()
        elif kind == "pr":
            ref = str(item.get("pr_url") or item.get("id") or "").strip()
            display = str(item.get("display_id") or ref or "pr").strip()
        else:
            ref = str(item.get("message_id") or item.get("id") or "").strip()
            display = str(item.get("display_id") or ref or "message").strip()
        if not ref:
            continue
        source = (
            "cos_work_order"
            if _item_matches_work_order(item, decision.get("cos_work_order"))
            else "actionable_feed"
        )
        return {
            "kind": kind,
            "ref": ref,
            "display": display,
            "status": str(item.get("status") or "unknown"),
            "owner_session_id": str(item.get("to_session_id") or ""),
            "known_gate": str(item.get("status") or "unknown"),
            "source": source,
        }
    return {
        "kind": "none",
        "ref": "",
        "display": "none",
        "status": "none",
        "owner_session_id": "",
        "known_gate": "none",
        "source": "none",
    }


def _item_matches_work_order(item: dict[str, Any], work_order: dict[str, Any] | None) -> bool:
    if not work_order:
        return False
    for entry in work_order.get("work_order", []):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        ref = str(entry.get("ref") or "").strip()
        if kind == "task" and str(item.get("task_id") or "") == ref:
            return True
        if kind == "pr" and str(item.get("pr_url") or "") == ref:
            return True
    return False


def _write_current_focus_projection(
    *,
    path: Path,
    manifest: RunManifest,
    decision: dict[str, Any],
    current_actions: Any,
    ownership: str,
    epoch: int,
) -> dict[str, Any]:
    payload = render_current_focus_projection(
        manifest=manifest,
        decision=decision,
        current_actions=current_actions,
        ownership=ownership,
        epoch=epoch,
    )
    _atomic_text(path, payload)
    projection = parse_current_focus_projection(path, manifest=manifest)
    if projection.action_digest != current_actions.digest:
        raise ContractError("current focus projection action_digest does not match current actions")
    if projection.decision_digest != str(decision.get("decision_digest") or ""):
        raise ContractError("current focus projection decision_digest does not match decision")
    if projection.controller_epoch != epoch:
        raise ContractError("current focus projection controller_epoch does not match live epoch")
    return {
        "digest": projection.digest,
        "written_at": projection.header["written_at"],
        "focus_kind": projection.focus_kind,
        "focus_ref": projection.focus_ref,
    }


def render_current_focus_projection(
    *,
    manifest: RunManifest,
    decision: dict[str, Any],
    current_actions: Any,
    ownership: str,
    epoch: int,
) -> str:
    latest_direction = decision.get("latest_direction") or {}
    focus = _focus_entry(decision)
    header = {
        "schema": "c2-current-focus-v1",
        "manifest_id": manifest.manifest_id,
        "controller_id": manifest.controller_id,
        "controller_cli_session_id": manifest.controller_cli_session_id,
        "controller_coord_session_id": manifest.controller_coord_session_id,
        "controller_iterm_session_id": manifest.controller_iterm_session_id,
        "controller_epoch": epoch,
        "ownership": ownership,
        "decision_digest": str(decision.get("decision_digest") or ""),
        "action_digest": current_actions.digest,
        "action_generation": current_actions.generation,
        "status": current_actions.status,
        "written_at": decision.get("generated_at") or _iso(),
        "next_check_at": current_actions.header.get("next_check_at"),
        "references": list(manifest.plan_paths),
        "focus_kind": focus["kind"],
        "focus_ref": focus["ref"],
        "focus_source": focus["source"],
        "owner_session_id": focus["owner_session_id"],
        "known_gate": focus["known_gate"],
        "direction_message_id": latest_direction.get("message_id"),
        "direction_digest": latest_direction.get("digest") or "",
        "plan_generation": int(latest_direction.get("generation") or 0),
    }
    body = "\n".join(
        [
            "## Current objective",
            f"- objective={focus['display']}",
            f"- focus_kind={header['focus_kind']}",
            f"- focus_ref={header['focus_ref'] or 'none'}",
            f"- focus_source={header['focus_source']}",
            "",
            "## Selected focus",
            f"- selected_status={focus['status']}",
            f"- owner_session_id={header['owner_session_id'] or 'none'}",
            (f"- next_reconciliation={'; '.join(decision.get('wake_reasons', [])) or 'none'}"),
            "",
            "## Expected report or gate",
            f"- action_digest={current_actions.digest}",
            f"- known_gate={header['known_gate'] or 'none'}",
            f"- direction_message_id={latest_direction.get('message_id') or 'none'}",
            "",
            "## Boundaries",
            "- This projection is bounded recovery guidance, not durable task authority.",
            (
                "- Claims, leases, merges, and transport remain fenced by "
                "coord-api and the live epoch."
            ),
            "",
            "## Durable references",
            *[f"- plan_path={path}" for path in manifest.plan_paths],
            "",
            "## Rewrite or stop condition",
            "- Rewrite after a focus, worker, gate, or direction transition.",
            (
                "- Stop automatic work only when a later checkpoint marks the "
                "current actions complete."
            ),
        ]
    )
    return (
        "--- c2-current-focus-v1\n"
        f"{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
        f"---\n{body}\n"
    )


def render_program_projection(
    *,
    manifest: RunManifest,
    decision: dict[str, Any],
    current_actions: Any,
    ownership: str,
    epoch: int,
) -> str:
    latest_direction = decision.get("latest_direction") or {}
    header = {
        "schema": "c2-program-projection-v1",
        "manifest_id": manifest.manifest_id,
        "controller_id": manifest.controller_id,
        "controller_cli_session_id": manifest.controller_cli_session_id,
        "controller_coord_session_id": manifest.controller_coord_session_id,
        "controller_iterm_session_id": manifest.controller_iterm_session_id,
        "controller_epoch": epoch,
        "ownership": ownership,
        "decision_digest": str(decision.get("decision_digest") or ""),
        "action_digest": current_actions.digest,
        "action_generation": current_actions.generation,
        "status": current_actions.status,
        "written_at": decision.get("generated_at") or _iso(),
        "next_check_at": current_actions.header.get("next_check_at"),
        "references": list(manifest.plan_paths),
        "direction_message_id": latest_direction.get("message_id"),
        "direction_digest": latest_direction.get("digest") or "",
        "plan_generation": int(latest_direction.get("generation") or 0),
    }
    worker_lines = [
        f"- {item['worker_id']}: {item['state']} ({item['runtime']} {item['tty']})"
        for item in decision.get("workers", [])
    ] or ["- none"]
    actionable_lines = []
    for item in decision.get("actionable_items", [])[:10]:
        kind = str(item.get("kind") or "unknown")
        identifier = (
            item.get("display_id")
            or item.get("task_id")
            or item.get("message_id")
            or item.get("id")
            or "unknown"
        )
        status = item.get("status") or "unknown"
        actionable_lines.append(f"- {kind} {identifier} [{status}]")
    if not actionable_lines:
        actionable_lines = ["- none"]
    direction_lines = [
        f"- plan_generation={header['plan_generation']}",
        f"- direction_message_id={header['direction_message_id'] or 'none'}",
        f"- direction_digest={header['direction_digest'] or 'none'}",
    ]
    body = "\n".join(
        [
            "## Current portfolio",
            f"- wake_required={bool(decision.get('wake_required'))}",
            f"- wake_reasons={'; '.join(decision.get('wake_reasons', [])) or 'none'}",
            f"- action_digest={current_actions.digest}",
            f"- next_check_at={header['next_check_at']}",
            "",
            "## Worker roster",
            *worker_lines,
            "",
            "## Ordered actionable items",
            *actionable_lines,
            "",
            "## Durable direction and references",
            *direction_lines,
            *[f"- plan_path={path}" for path in manifest.plan_paths],
            "",
            "## Boundaries",
            (
                "- This projection is recovery guidance only and grants no authority "
                "without coord-api readback."
            ),
            "- Do not treat local projections as durable task truth or historical record.",
            "",
            "## Rewrite or stop condition",
            (
                "- Rewrite after any material worker, message, PR, evidence, lease, "
                "or direction transition."
            ),
            (
                "- Stop automatic work only when a later checkpoint marks the current "
                "actions complete."
            ),
        ]
    )
    return (
        "--- c2-program-projection-v1\n"
        f"{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
        f"---\n{body}\n"
    )


def ensure_authority(
    *,
    client: CoordClient,
    manifest: RunManifest,
    paths: dict[str, Path],
    state: dict[str, Any],
    ownership: str,
) -> tuple[dict[str, Any], LeaseHandle]:
    if client.config.principal_id != manifest.controller_coord_agent_id:
        raise ContractError("coord principal does not match the registered controller identity")
    handle = _handle_from_state(state)
    expected_producer = manifest.controller_producer(ownership)
    if handle is not None:
        stored_producer = handle.lease.get("producer")
        if not manifest.controller_producer_matches(stored_producer, ownership):
            raise ContractError(
                "stored controller lease binding does not match requested ownership"
            )
        last_renewed_ts = state.get("last_renewed_ts")
        if isinstance(last_renewed_ts, (int, float)) and (
            time.time() - float(last_renewed_ts) < SUPERVISOR_RENEW_SECONDS
        ):
            client.verify_live_epoch(SUPERVISOR_RESOURCE, handle.epoch)
            return state, handle
        try:
            renewed = client.renew_resource(handle)
            return _save_authority(
                paths=paths, state=state, handle=renewed, ownership=ownership
            ), renewed
        except LeaseLost:
            state = {**state, "authority": False, "lease": None}
    handle = client.claim_resource(
        SUPERVISOR_RESOURCE,
        ttl_seconds=SUPERVISOR_TTL_SECONDS,
        producer=expected_producer,
    )
    return _save_authority(paths=paths, state=state, handle=handle, ownership=ownership), handle


def classify_registered_workers(
    manifest: RunManifest,
    live_state: dict[str, Any],
    *,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    now_ts = time.time() if now_ts is None else now_ts
    generated_ts = live_state.get("generated_ts")
    age = None
    if isinstance(generated_ts, (int, float)):
        age = max(0.0, now_ts - float(generated_ts))
    sessions = {
        str(item.get("iterm_session_id") or ""): item
        for item in live_state.get("sessions", [])
        if isinstance(item, dict) and item.get("iterm_session_id")
    }
    observations: list[dict[str, Any]] = []
    for worker in manifest.workers:
        observed = sessions.get(worker.iterm_session_id)
        state = normalize_worker_state(
            observed.get("readiness") if observed else None,
            age_seconds=age,
            present=observed is not None,
        )
        if observed and observed.get("tty") != worker.tty:
            state = "lost"
        if observed and observed.get("runtime") not in {worker.runtime, "unknown"}:
            state = "lost"
        observations.append(
            {
                "worker_id": worker.worker_id,
                "host": worker.host,
                "runtime": worker.runtime,
                "iterm_session_id": worker.iterm_session_id,
                "tty": worker.tty,
                "cli_session_id": worker.cli_session_id,
                "coord_session_id": worker.coord_session_id,
                "observation_profile_id": worker.observation_profile_id,
                "observation_profile_version": worker.observation_profile_version,
                "state": state,
                "observed": observed or None,
            }
        )
    return observations


def latest_cos_work_order(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract the newest valid ordered-work projection from COS messages.

    Direction messages travel through the existing actionable feed.  This
    projection is read-only: it influences the next decision, never task
    status, ownership, claim, lease, or terminal delivery state.
    """
    candidates: list[dict[str, Any]] = []
    for item in items:
        if item.get("kind") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("schema") != COS_DIRECTION_SCHEMA:
            continue
        generation = payload.get("generation")
        order = payload.get("work_order", [])
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            continue
        if not isinstance(order, list):
            continue
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        valid = True
        for raw in order:
            if not isinstance(raw, dict):
                valid = False
                break
            kind = str(raw.get("kind") or "").strip().lower()
            ref = str(raw.get("ref") or "").strip()
            key = (kind, ref)
            if kind not in COS_WORK_KINDS or not ref or len(ref) > 512 or key in seen:
                valid = False
                break
            seen.add(key)
            entry = {"kind": kind, "ref": ref}
            if raw.get("reason") is not None:
                entry["reason"] = str(raw["reason"]).strip()[:2_000]
            normalized.append(entry)
        if not valid:
            continue
        candidate = {
            "schema": COS_DIRECTION_SCHEMA,
            "direction_id": str(payload.get("direction_id") or ""),
            "plan_id": str(payload.get("plan_id") or ""),
            "generation": generation,
            "work_order": normalized,
            "source_message_id": item.get("message_id"),
        }
        if not candidate["direction_id"] or not candidate["plan_id"]:
            continue
        if item.get("provenance_source") != "cos":
            continue
        if item.get("external_id") != (
            f"cos-direction:{candidate['plan_id']}:{candidate['generation']}"
        ):
            continue
        if item.get("correlation_id") != candidate["plan_id"]:
            continue
        candidates.append(candidate)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda value: (value["generation"], str(value.get("source_message_id") or "")),
    )


def order_actionable_items(
    items: list[dict[str, Any]], work_order: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Put matching task feed entries in COS order, preserving the remainder."""
    if not work_order:
        return list(items)
    remaining = list(items)
    ordered: list[dict[str, Any]] = []
    for entry in work_order.get("work_order", []):
        kind = entry.get("kind") if isinstance(entry, dict) else None
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if not isinstance(kind, str) or not isinstance(ref, str):
            continue
        index = next(
            (
                index
                for index, item in enumerate(remaining)
                if (kind == "task" and str(item.get("task_id") or "") == ref)
                or (kind == "pr" and str(item.get("pr_url") or "") == ref)
            ),
            None,
        )
        if index is not None:
            ordered.append(remaining.pop(index))
    return ordered + remaining


def reconcile(
    *,
    manifest: RunManifest,
    actionable: dict[str, Any],
    live_state: dict[str, Any],
    now_ts: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now_ts is None else now_ts
    workers = classify_registered_workers(manifest, live_state, now_ts=now_ts)
    items = [item for item in actionable.get("items", []) if isinstance(item, dict)]
    directions = []
    for item in items:
        if item.get("kind") not in {"message", "direction"}:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        if payload.get("schema") != "cos.direction.v1" or item.get("provenance_source") not in {
            "cos",
            None,
        }:
            continue
        generation = payload.get("generation")
        if isinstance(generation, int) and generation > 0:
            directions.append(
                {
                    "message_id": item.get("message_id") or item.get("id"),
                    "direction_id": payload.get("direction_id"),
                    "plan_id": payload.get("plan_id"),
                    "generation": generation,
                    "precedence": payload.get("precedence"),
                    "digest": hashlib.sha256(
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
            )
    directions.sort(key=lambda item: (str(item.get("plan_id") or ""), int(item["generation"])))
    idle = [item for item in workers if item["state"] == "idle"]
    exceptions = [
        item
        for item in workers
        if item["state"] in {"needs_input", "blocked", "stale", "lost", "unknown"}
    ]
    task_items = [item for item in items if item.get("kind") == "task"]
    message_items = [item for item in items if item.get("kind") != "task"]
    work_order = latest_cos_work_order(items)
    ordered_items = order_actionable_items(items, work_order)
    reasons: list[str] = []
    if idle and task_items:
        reasons.append("idle worker and actionable task require assignment decision")
    if exceptions:
        reasons.append("worker exception requires recovery decision")
    if message_items:
        reasons.append("actionable coordination message requires model decision")
    if directions:
        reasons.append("new durable COS direction requires reconciliation")
    work_order = latest_cos_work_order(items)
    ordered_items = order_actionable_items(items, work_order)
    return {
        "generated_at": _iso(now_ts),
        "generated_ts": now_ts,
        "manifest_id": manifest.manifest_id,
        "workers": workers,
        "actionable_items": ordered_items,
        "directions": directions,
        "latest_direction": directions[-1] if directions else None,
        "cos_work_order": work_order,
        "idle_worker_ids": [item["worker_id"] for item in idle],
        "exception_worker_ids": [item["worker_id"] for item in exceptions],
        "wake_required": bool(reasons),
        "wake_reasons": reasons,
    }


def decision_digest(decision: dict[str, Any]) -> str:
    worker_fields = (
        "worker_id",
        "host",
        "runtime",
        "iterm_session_id",
        "tty",
        "state",
    )
    item_fields = (
        "kind",
        "id",
        "display_id",
        "task_id",
        "message_id",
        "attempt_id",
        "status",
        "priority",
        "required_ack",
        "to_session_id",
    )
    volatile_item_fields = {
        "fetched_at",
        "fetched_ts",
        "generated_at",
        "generated_ts",
        "last_seen_at",
        "last_seen_ts",
        "observed_at",
        "observed_ts",
        "screen_tail",
        "terminal_tail",
    }

    def stable_item(item: dict[str, Any]) -> dict[str, Any]:
        result = {field: item.get(field) for field in item_fields if field in item}
        payload = {
            field: value
            for field, value in item.items()
            if field not in volatile_item_fields and field not in item_fields
        }
        result["payload_digest"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return result

    stable = {
        "manifest_id": decision.get("manifest_id"),
        "workers": [
            {field: worker.get(field) for field in worker_fields if field in worker}
            for worker in decision.get("workers", [])
            if isinstance(worker, dict)
        ],
        "actionable_items": [
            stable_item(item)
            for item in decision.get("actionable_items", [])
            if isinstance(item, dict)
        ],
        "cos_work_order": decision.get("cos_work_order"),
        "idle_worker_ids": decision.get("idle_worker_ids", []),
        "exception_worker_ids": decision.get("exception_worker_ids", []),
        "wake_required": decision.get("wake_required"),
        "wake_reasons": decision.get("wake_reasons", []),
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run_tick(
    *,
    manifest: RunManifest,
    client: CoordClient,
    state_dir: Path,
    live_state_path: Path,
    ownership: str,
    wake: bool,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    paths = state_paths(state_dir)
    state = _load_json(
        paths["state"],
        {"mode": "bootstrap-authoritative", "authority": False, "lease": None},
    )
    if not paths["armed"].exists():
        return {"ok": True, "armed": False, "action": "inert"}
    effective_manifest_sha256 = manifest_sha256 or manifest_contract_sha256(manifest)
    _require_arm_marker(
        paths["armed"],
        manifest=manifest,
        manifest_sha256=effective_manifest_sha256,
    )
    mode = str(state.get("mode") or "bootstrap-authoritative")
    if mode not in CONTROLLER_MODES:
        raise ContractError(f"invalid controller mode: {mode}")
    if mode != "bootstrap-authoritative":
        heartbeat = {
            "recorded_at": _iso(),
            "recorded_ts": time.time(),
            "mode": mode,
            "authority": False,
            "ownership": ownership,
        }
        _atomic_json(paths["heartbeat"], heartbeat)
        return {"ok": True, "armed": True, **heartbeat, "action": "observe-only"}
    recovery_hold = _load_json(paths["recovery_hold"])
    if recovery_hold and ownership == "visible":
        handle = _handle_from_state(state)
        released = False
        if handle is not None:
            try:
                released = client.release_resource(handle)
            except LeaseLost:
                released = False
        state = {
            **state,
            "authority": False,
            "lease": None,
            "recovery_hold": True,
            "last_error": None,
        }
        _atomic_json(paths["state"], state)
        heartbeat = {
            "recorded_at": _iso(),
            "recorded_ts": time.time(),
            "mode": mode,
            "authority": False,
            "ownership": ownership,
            "recovery_hold": True,
            "released": released,
            "held_epoch": recovery_hold.get("controller_epoch"),
            "action_digest": recovery_hold.get("action_digest"),
        }
        _atomic_json(paths["heartbeat"], heartbeat)
        return {"ok": True, "armed": True, **heartbeat, "action": "recovery-hold"}
    state, handle = ensure_authority(
        client=client,
        manifest=manifest,
        paths=paths,
        state=state,
        ownership=ownership,
    )
    actionable = client.actionable(manifest.controller_coord_agent_id)
    live_state = _load_json(live_state_path)
    decision = reconcile(manifest=manifest, actionable=actionable, live_state=live_state)
    digest = decision_digest(decision)
    external_state = sweep_external_state(items=decision["actionable_items"])
    previous = _load_json(paths["decision"])
    decision["decision_digest"] = digest
    decision["controller_epoch"] = handle.epoch
    decision["wake_delivered"] = False
    decision["external_state_sweep"] = {
        "finding_count": external_state["finding_count"],
        "blocked": external_state["blocked"],
        "findings_digest": external_state["findings_digest"],
    }
    _atomic_json(paths["decision"], decision)
    _atomic_json(paths["external_state_sweep"], external_state)
    if paths["actions"].exists():
        current_actions = parse_actions(paths["actions"], manifest=manifest)
    else:
        current_actions = seed_actions(
            manifest=manifest,
            path=paths["actions"],
            decision_digest=digest,
            epoch=handle.epoch,
        )
    if (
        current_actions.controller_epoch != handle.epoch
        or current_actions.header.get("ownership") != ownership
    ):
        current_actions = rebind_actions(
            current=current_actions,
            path=paths["actions"],
            manifest=manifest,
            decision_digest=digest,
            epoch=handle.epoch,
            ownership=ownership,
        )
    latest_direction = decision.get("latest_direction") or {}
    if latest_direction:
        action_header = dict(current_actions.header)
        action_header.update(
            {
                "direction_message_id": latest_direction.get("message_id"),
                "direction_digest": latest_direction.get("digest") or "",
                "plan_generation": int(latest_direction.get("generation") or 0),
            }
        )
        if action_header != current_actions.header:
            current_actions = rebind_actions(
                current=current_actions,
                path=paths["actions"],
                manifest=manifest,
                decision_digest=digest,
                epoch=handle.epoch,
                ownership=ownership,
            )
            # rebind_actions intentionally preserves the prior header; attach
            # the direction projection atomically while retaining its chain.
            current_actions = update_projection_header(
                current=current_actions,
                path=paths["actions"],
                manifest=manifest,
                updates=action_header,
            )
    current_focus = _write_current_focus_projection(
        path=paths["current_focus"],
        manifest=manifest,
        decision=decision,
        current_actions=current_actions,
        ownership=ownership,
        epoch=handle.epoch,
    )
    program_projection = _write_program_projection(
        path=paths["program"],
        manifest=manifest,
        decision=decision,
        current_actions=current_actions,
        ownership=ownership,
        epoch=handle.epoch,
    )
    poked = False
    poke_result: dict[str, Any] | None = None
    already_delivered = (
        previous.get("decision_digest") == digest and previous.get("wake_delivered") is True
    )
    if (
        wake
        and decision["wake_required"]
        and not external_state["blocked"]
        and not already_delivered
        and manifest.controller_has_visible_terminal()
    ):
        prompt = (
            f"/goal C2_CONTINUE actions={paths['actions']} sha256={current_actions.digest} "
            f"generation={current_actions.generation} decision={digest} epoch={handle.epoch}. "
            "First acknowledge this exact action version, then read it, execute its bounded next "
            "actions, and publish a new checkpoint before ending the turn."
        )
        poke_key = f"c2-wake:{handle.epoch}:{digest}"
        poke_result = poke_controller(
            text=prompt,
            controller_epoch=handle.epoch,
            idempotency_key=poke_key,
        )
        if poke_result.get("ok"):
            poked = True
            decision["wake_delivered"] = True
            _atomic_json(paths["decision"], decision)
    heartbeat = {
        "recorded_at": _iso(),
        "recorded_ts": time.time(),
        "mode": mode,
        "authority": True,
        "controller_epoch": handle.epoch,
        "ownership": ownership,
        "decision_digest": digest,
        "wake_required": decision["wake_required"],
        "poked": poked,
        "action_digest": current_actions.digest,
        "action_generation": current_actions.generation,
        "action_next_check_ts": current_actions.next_check_ts,
        "current_focus_digest": current_focus["digest"],
        "current_focus_kind": current_focus["focus_kind"],
        "current_focus_ref": current_focus["focus_ref"],
        "program_digest": program_projection["digest"],
        "external_state_blocked": external_state["blocked"],
        "external_state_findings_digest": external_state["findings_digest"],
        "external_state_finding_count": external_state["finding_count"],
    }
    _atomic_json(paths["heartbeat"], heartbeat)
    action = "external-divergence" if external_state["blocked"] else None
    return {
        "ok": True,
        "armed": True,
        **heartbeat,
        "action": action,
        "external_state_sweep": external_state,
        "poke_result": poke_result,
    }


def arm(
    *,
    manifest: RunManifest,
    state_dir: Path,
    validate_plan_paths: bool = True,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    if validate_plan_paths:
        missing = [path for path in manifest.plan_paths if not Path(path).is_file()]
        if missing:
            raise ContractError("authoritative plan paths are missing: " + ", ".join(missing))
    paths = state_paths(state_dir)
    paths["armed"].parent.mkdir(parents=True, exist_ok=True)
    watchdog_path = state_dir / "watchdog-state.json"
    prior_watchdog = _load_json(watchdog_path)
    for stale_path in (
        paths["heartbeat"],
        paths["decision"],
        paths["program"],
        paths["current_focus"],
        paths["actions"],
        paths["action_progress"],
        paths["recovery_hold"],
        paths["external_state_sweep"],
    ):
        stale_path.unlink(missing_ok=True)
    recovery_sequence = max(
        int(prior_watchdog.get("recovery_sequence") or 0),
        _next_receipt_sequence(state_dir / "recovery-receipts.jsonl", "c2-recovery:"),
    )
    edge_restart_sequence = max(
        int(prior_watchdog.get("edge_restart_sequence") or 0),
        _next_receipt_sequence(state_dir / "edge-recovery-receipts.jsonl", "edge-recovery:"),
    )
    _atomic_json(
        watchdog_path,
        {
            "recovery_sequence": recovery_sequence,
            "edge_restart_sequence": edge_restart_sequence,
            "tab_pokes": 0,
            "provider_failures": 0,
            "edge_health_failures": 0,
            "edge_restart_attempts": 0,
            "pending_since": None,
            "pending_key": None,
            "pending_transport": None,
            "backoff_until": None,
            "edge_restart_backoff_until": None,
            "action_pending_since": None,
            "action_pending_digest": None,
            "action_pending_generation": None,
            "action_pending_epoch": None,
            "action_wake_attempts": 0,
            "last_headless_checkpoint_digest": None,
        },
    )
    effective_manifest_sha256 = manifest_sha256 or manifest_contract_sha256(manifest)
    paths["armed"].write_text(
        render_arm_marker(
            manifest_id=manifest.manifest_id,
            manifest_sha256=effective_manifest_sha256,
        ),
        encoding="utf-8",
    )
    os.chmod(paths["armed"], 0o600)
    state = {
        "mode": "bootstrap-authoritative",
        "authority": False,
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": effective_manifest_sha256,
        "lease": None,
        "armed_at": _iso(),
    }
    _atomic_json(paths["state"], state)
    return {"ok": True, "armed": True, **state}


def arm_from_cli(
    *,
    manifest: RunManifest,
    state_dir: Path,
    readiness: dict[str, Any] | None = None,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Arm only after the machine-local recovery and terminal edge are loaded."""
    observed = readiness if readiness is not None else service_readiness()
    if observed.get("ready") is not True:
        missing = [
            item.get("label") or name
            for name, item in (observed.get("services") or {}).items()
            if item.get("loaded") is not True
        ]
        reason = str(observed.get("reason") or "required services are not loaded")
        if missing:
            reason = "required services are not loaded: " + ", ".join(missing)
        raise ContractError(f"arm readiness refused: {reason}")
    result = arm(
        manifest=manifest,
        state_dir=state_dir,
        manifest_sha256=manifest_sha256,
    )
    return {**result, "service_readiness": observed}


def set_standby(*, client: CoordClient, state_dir: Path) -> dict[str, Any]:
    paths = state_paths(state_dir)
    state = _load_json(paths["state"])
    handle = _handle_from_state(state)
    released = False
    if handle is not None:
        try:
            released = client.release_resource(handle)
        except LeaseLost:
            released = False
    updated = {
        **state,
        "mode": "bootstrap-standby",
        "authority": False,
        "lease": None,
        "standby_at": _iso(),
    }
    _atomic_json(paths["state"], updated)
    return {"ok": True, "armed": paths["armed"].exists(), "released": released, **updated}


def stop(*, client: CoordClient, state_dir: Path) -> dict[str, Any]:
    paths = state_paths(state_dir)
    state = _load_json(paths["state"])
    handle = _handle_from_state(state)
    released = False
    if handle is not None:
        try:
            released = client.release_resource(handle)
        except LeaseLost:
            released = False
    paths["armed"].unlink(missing_ok=True)
    updated = {
        **state,
        "authority": False,
        "lease": None,
        "stopped_at": _iso(),
    }
    _atomic_json(paths["state"], updated)
    return {"ok": True, "armed": False, "released": released, **updated}


def finish_headless_turn(
    *, client: CoordClient, manifest: RunManifest, state_dir: Path, digest: str
) -> dict[str, Any]:
    paths = state_paths(state_dir)
    state = _load_json(paths["state"])
    if state.get("ownership") != "headless" or state.get("authority") is not True:
        raise ContractError("finish-turn requires the authoritative headless owner")
    actions = parse_actions(paths["actions"], manifest=manifest)
    if actions.digest != digest or actions.header.get("ownership") != "headless":
        raise ContractError("finish-turn digest must name the headless checkpoint")
    handle = _handle_from_state(state)
    if handle is None:
        raise ContractError("finish-turn has no live lease handle")
    released = client.release_resource(handle)
    updated = {
        **state,
        "authority": False,
        "lease": None,
        "headless_finished_at": _iso(),
        "headless_checkpoint_digest": digest,
    }
    _atomic_json(paths["state"], updated)
    receipt = {
        "idempotency_key": f"c2-headless-finish:{handle.epoch}:{actions.generation}:{digest}",
        "kind": "headless-finish",
        "recorded_at": _iso(),
        "recorded_ts": time.time(),
        "controller_epoch": handle.epoch,
        "generation": actions.generation,
        "action_digest": digest,
        "released": released,
    }
    store = ReceiptStore(paths["action_receipts"])
    if not store.has_idempotency_key(receipt["idempotency_key"]):
        store.append(receipt)
    client.post_receipt(receipt)
    return {"ok": released, **receipt}


def reattach_visible(
    *, client: CoordClient, manifest: RunManifest, state_dir: Path, digest: str
) -> dict[str, Any]:
    paths = state_paths(state_dir)
    actions = parse_actions(paths["actions"], manifest=manifest)
    if actions.digest != digest:
        raise ContractError("reattach digest does not match current actions")
    if client.get_resource(SUPERVISOR_RESOURCE) is not None:
        raise ContractError("reattach requires the prior controller epoch to be absent")
    paths["recovery_hold"].unlink(missing_ok=True)
    state = {
        **_load_json(paths["state"]),
        "authority": False,
        "lease": None,
        "ownership": "visible",
        "recovery_hold": False,
        "reattached_at": _iso(),
    }
    _atomic_json(paths["state"], state)
    return {"ok": True, "action_digest": digest, "ownership": "visible"}


def status(
    *,
    client: CoordClient | None,
    state_dir: Path,
    readiness: dict[str, Any] | None = None,
    manifest: RunManifest | None = None,
    manifest_sha256: str | None = None,
    live_state_path: Path = DEFAULT_LIVE_STATE,
) -> dict[str, Any]:
    paths = state_paths(state_dir)
    state = _load_json(paths["state"])
    heartbeat = _load_json(paths["heartbeat"])
    lease: dict[str, Any] | None = None
    error: str | None = None
    if client is not None:
        try:
            lease = client.get_resource(SUPERVISOR_RESOURCE)
        except CoordError as exc:
            error = str(exc)
    armed = paths["armed"].exists()
    observed_readiness = readiness if readiness is not None else service_readiness()
    marker = None
    fleet_snapshot: dict[str, Any] | None = None
    fleet_error: str | None = None
    external_state_live: dict[str, Any] | None = None
    if manifest is not None:
        effective_manifest_sha256 = manifest_sha256 or manifest_contract_sha256(manifest)
        marker = _marker_status(
            paths["armed"],
            manifest=manifest,
            manifest_sha256=effective_manifest_sha256,
        )
        actionable: dict[str, Any] = {"items": []}
        if client is not None:
            try:
                actionable = client.actionable(manifest.controller_coord_agent_id)
            except CoordError as exc:
                fleet_error = str(exc)
        fleet_decision = reconcile(
            manifest=manifest,
            actionable=actionable,
            live_state=_load_json(live_state_path),
        )
        fleet_snapshot = {
            "decision_digest": decision_digest(fleet_decision),
            "workers": fleet_decision["workers"],
            "actionable_items": fleet_decision["actionable_items"],
            "wake_required": fleet_decision["wake_required"],
            "wake_reasons": fleet_decision["wake_reasons"],
            "error": fleet_error,
        }
        external_state_live = sweep_external_state(items=fleet_decision["actionable_items"])
    marker_checked = marker is not None
    marker_valid = bool(marker and marker.get("valid") is True) if marker_checked else None
    return {
        # `armed` preserves the physical marker fact for compatibility.  The
        # effective fields make the safety decision explicit: a stale or
        # malformed marker is never an armed supervisor, even if the file was
        # left behind by an older process.
        "armed": armed,
        "arm_marker_valid": marker_valid if armed else False,
        "effective_armed": (bool(armed and marker_valid) if marker_checked else None),
        "service_readiness": observed_readiness,
        "armed_but_unserviced": armed and observed_readiness.get("ready") is not True,
        "armed_but_invalid": bool(armed and marker_checked and not marker_valid),
        "arm_marker": marker,
        "requires_explicit_rearm": bool(armed and marker and marker.get("requires_explicit_rearm")),
        "state": state,
        "heartbeat": heartbeat,
        "current_actions": (
            {
                "digest": actions.digest,
                "generation": actions.generation,
                "status": actions.status,
                "decision_digest": actions.decision_digest,
                "controller_epoch": actions.controller_epoch,
                "ownership": actions.header.get("ownership"),
                "next_check_ts": actions.next_check_ts,
            }
            if (actions := _status_actions(paths["actions"], manifest=manifest)) is not None
            else None
        ),
        "current_focus": (
            {
                "digest": focus.digest,
                "action_digest": focus.action_digest,
                "focus_kind": focus.focus_kind,
                "focus_ref": focus.focus_ref,
                "status": focus.header.get("status"),
                "decision_digest": focus.decision_digest,
                "controller_epoch": focus.controller_epoch,
                "ownership": focus.header.get("ownership"),
                "focus_source": focus.header.get("focus_source"),
            }
            if (focus := _status_focus(paths["current_focus"], manifest=manifest)) is not None
            else None
        ),
        "program_projection": (
            {
                "digest": projection.digest,
                "action_digest": projection.action_digest,
                "decision_digest": projection.decision_digest,
                "controller_epoch": projection.controller_epoch,
                "ownership": projection.header.get("ownership"),
            }
            if (projection := _status_program(paths["program"], manifest=manifest)) is not None
            else None
        ),
        "action_progress": _load_json(paths["action_progress"]),
        "recovery_hold": _load_json(paths["recovery_hold"]),
        "external_state_sweep": _load_json(paths["external_state_sweep"]),
        "external_state_sweep_live": external_state_live,
        "live_lease": lease,
        "coord_error": error,
        "fleet_snapshot": fleet_snapshot,
    }


def preflight(
    *,
    manifest: RunManifest,
    manifest_path: Path,
    live_state_path: Path = DEFAULT_LIVE_STATE,
    readiness: dict[str, Any] | None = None,
    edge_probe: Any = request_edge,
) -> dict[str, Any]:
    """Run read-only gates required before an authorized terminal experiment."""
    observed_readiness = readiness if readiness is not None else service_readiness()
    plan_missing = [path for path in manifest.plan_paths if not Path(path).is_file()]
    manifest_sha256 = manifest_file_sha256(manifest_path)
    live_state = _load_json(live_state_path)
    workers = classify_registered_workers(manifest, live_state)
    idle_workers = [worker for worker in workers if worker["state"] == "idle"]
    registered_ttys = {worker.tty for worker in manifest.workers}
    registered_sessions = {worker.iterm_session_id for worker in manifest.workers}
    identity_drift: list[dict[str, Any]] = []
    for session in live_state.get("sessions", []):
        if (
            not isinstance(session, dict)
            or session.get("tty") not in registered_ttys
            or session.get("iterm_session_id") in registered_sessions
        ):
            continue
        expected_worker = next(
            worker for worker in manifest.workers if worker.tty == session.get("tty")
        )
        required_fields = [
            "iterm_session_id",
            "tty",
            "runtime",
            "cli_session_id",
            "coord_session_id",
        ]
        if expected_worker.observation_profile_id:
            required_fields.extend(("observation_profile_id", "observation_profile_version"))
        expected_bindings = {field: getattr(expected_worker, field) for field in required_fields}
        observed_bindings = {field: session.get(field) for field in required_fields}
        identity_drift.append(
            {
                "worker_id": expected_worker.worker_id,
                "drifted_fields": [
                    field
                    for field in required_fields
                    if observed_bindings[field] != expected_bindings[field]
                ],
                "expected_bindings": expected_bindings,
                "observed_bindings": observed_bindings,
                "tty": str(session.get("tty") or ""),
                "observed_session_id": str(session.get("iterm_session_id") or ""),
                "runtime": str(session.get("runtime") or "unknown"),
                "readiness": str(session.get("readiness") or "unknown"),
            }
        )
    for worker in workers:
        observed = worker.get("observed")
        if not isinstance(observed, dict):
            continue
        required_fields = [
            "iterm_session_id",
            "tty",
            "runtime",
            "cli_session_id",
            "coord_session_id",
        ]
        if worker["observation_profile_id"]:
            required_fields.extend(("observation_profile_id", "observation_profile_version"))
        drifted_fields = [
            field for field in required_fields if observed.get(field) != worker.get(field)
        ]
        if drifted_fields:
            identity_drift.append(
                {
                    "worker_id": worker["worker_id"],
                    "drifted_fields": drifted_fields,
                    "expected_bindings": {field: worker.get(field) for field in required_fields},
                    "observed_bindings": {field: observed.get(field) for field in required_fields},
                    "expected_session_id": worker["iterm_session_id"],
                    "expected_tty": worker["tty"],
                    "expected_runtime": worker["runtime"],
                    "observed_session_id": observed.get("iterm_session_id"),
                    "observed_tty": observed.get("tty"),
                    "observed_runtime": observed.get("runtime"),
                    "readiness": observed.get("readiness"),
                }
            )
    edge: dict[str, Any]
    if not manifest.terminal_actions_enabled:
        edge = {
            "ready": False,
            "reason": "terminal_actions_disabled_in_manifest",
        }
    else:
        try:
            response = edge_probe(
                {"protocol": "cos-c2-iterm-edge-v1", "op": "health"},
                timeout_seconds=2.0,
            )
            edge = {
                "ready": bool(response.get("ok"))
                and response.get("manifest_sha256") == manifest_sha256,
                "response": response,
                "manifest_sha256": manifest_sha256,
            }
            if edge["ready"] is not True:
                edge["reason"] = "edge_health_or_manifest_digest_failed"
        except Exception as exc:
            edge = {
                "ready": False,
                "reason": "edge_unavailable",
                "error": f"{type(exc).__name__}: {exc}",
            }
    blockers: list[dict[str, Any]] = []
    if observed_readiness.get("ready") is not True:
        blockers.append(
            {
                "code": "service_not_ready",
                "detail": observed_readiness.get("reason") or "required services are not ready",
                "action": "inspect service registration; do not arm or dispatch",
            }
        )
    if plan_missing:
        blockers.append(
            {
                "code": "plan_missing",
                "paths": plan_missing,
                "action": "restore or replace plan paths, then re-run preflight",
            }
        )
    if not manifest.terminal_actions_enabled:
        blockers.append(
            {
                "code": "terminal_actions_disabled",
                "action": "operator must explicitly edit the manifest and re-arm",
            }
        )
    if identity_drift:
        blockers.append(
            {
                "code": "identity_drift",
                "sessions": identity_drift,
                "action": (
                    "inspect roster-proposal; adoption requires explicit manifest edit and re-arm"
                ),
            }
        )
    if not idle_workers:
        blockers.append(
            {
                "code": "no_idle_registered_worker",
                "action": (
                    "wait for or explicitly enroll an idle registered worker; "
                    "never inject into a replacement"
                ),
            }
        )
    if edge.get("ready") is not True:
        blockers.append(
            {
                "code": "edge_not_ready",
                "detail": edge.get("reason") or "terminal edge is not ready",
                "action": "repair or load the pinned edge; do not use a fallback transport",
            }
        )
    return {
        "ready": (
            observed_readiness.get("ready") is True
            and not plan_missing
            and bool(idle_workers)
            and not identity_drift
            and edge.get("ready") is True
        ),
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest_sha256,
        "missing_plan_paths": plan_missing,
        "workers": workers,
        "idle_worker_ids": [worker["worker_id"] for worker in idle_workers],
        "worker_roster_ready": bool(idle_workers),
        "identity_drift": identity_drift,
        "service_readiness": observed_readiness,
        "edge": edge,
        "terminal_actions_enabled": manifest.terminal_actions_enabled,
        "blockers": blockers,
    }


def roster_proposal(*, manifest: RunManifest, live_state_path: Path) -> dict[str, Any]:
    """Build a non-authoritative expected/observed roster reconciliation."""
    live_state = _load_json(live_state_path)
    sessions = [
        session
        for session in live_state.get("sessions", [])
        if isinstance(session, dict) and session.get("iterm_session_id")
    ]
    expected_sessions = {worker.iterm_session_id for worker in manifest.workers}
    workers: list[dict[str, Any]] = []
    for worker in manifest.workers:
        candidates = [session for session in sessions if session.get("tty") == worker.tty]
        expected = next(
            (
                session
                for session in candidates
                if session.get("iterm_session_id") == worker.iterm_session_id
            ),
            None,
        )
        required_fields = [
            "iterm_session_id",
            "tty",
            "runtime",
            "cli_session_id",
            "coord_session_id",
        ]
        if worker.observation_profile_id:
            required_fields.extend(("observation_profile_id", "observation_profile_version"))
        expected_bindings = {field: getattr(worker, field) for field in required_fields}
        observed_bindings = [
            {field: session.get(field) for field in required_fields} for session in candidates
        ]
        drifted_fields = sorted(
            {
                field
                for observed_candidate in observed_bindings
                for field in required_fields
                if observed_candidate.get(field) != expected_bindings[field]
            }
        )
        workers.append(
            {
                "worker_id": worker.worker_id,
                "expected": {
                    "iterm_session_id": worker.iterm_session_id,
                    "tty": worker.tty,
                    "runtime": worker.runtime,
                },
                "expected_bindings": expected_bindings,
                "observed_bindings": observed_bindings,
                "drifted_fields": drifted_fields,
                "observed_on_expected_tty": [
                    {
                        "iterm_session_id": str(session.get("iterm_session_id")),
                        "runtime": str(session.get("runtime") or "unknown"),
                        "readiness": str(session.get("readiness") or "unknown"),
                    }
                    for session in candidates
                ],
                "status": (
                    "binding-drift"
                    if expected is not None and drifted_fields
                    else "unchanged"
                    if expected is not None
                    else "replacement-on-tty"
                    if candidates
                    else "missing"
                ),
            }
        )
    unregistered = [
        {
            "iterm_session_id": str(session.get("iterm_session_id")),
            "tty": str(session.get("tty") or ""),
            "runtime": str(session.get("runtime") or "unknown"),
            "readiness": str(session.get("readiness") or "unknown"),
        }
        for session in sessions
        if session.get("iterm_session_id") not in expected_sessions
    ]
    return {
        "manifest_id": manifest.manifest_id,
        "requires_explicit_rearm": True,
        "workers": workers,
        "unregistered_live_sessions": unregistered,
    }


def _status_actions(path: Path, *, manifest: RunManifest | None = None):
    try:
        return parse_actions(path, manifest=manifest)
    except ContractError:
        return None


def _status_program(path: Path, *, manifest: RunManifest | None = None):
    try:
        return parse_program_projection(path, manifest=manifest)
    except ContractError:
        return None


def _status_focus(path: Path, *, manifest: RunManifest | None = None):
    try:
        return parse_current_focus_projection(path, manifest=manifest)
    except ContractError:
        return None


def dispatch_current_focus(
    *,
    manifest: RunManifest,
    manifest_path: Path,
    state_dir: Path,
    live_state_path: Path,
    client: CoordClient,
    worker_receipt_adapter: str,
) -> dict[str, Any]:
    paths = state_paths(state_dir)
    state = _load_json(paths["state"])
    epoch = state.get("controller_epoch")
    if state.get("authority") is not True or not isinstance(epoch, int):
        raise ContractError("dispatch-focus requires live supervisor authority")
    if not worker_receipt_adapter.strip():
        raise ContractError("dispatch-focus requires --worker-receipt-adapter")
    if not paths["current_focus"].is_file():
        raise ContractError("dispatch-focus requires current-focus.md")
    if not paths["decision"].is_file():
        raise ContractError("dispatch-focus requires decision-current.json")
    client.verify_live_epoch(SUPERVISOR_RESOURCE, epoch)
    decision = _load_json(paths["decision"])
    external_state = decision.get("external_state_sweep") or {}
    if isinstance(external_state, dict) and external_state.get("blocked") is True:
        raise ContractError("dispatch-focus is blocked by external state divergence")
    _plan, envelope, parsed_manifest = cos_dispatch_orchestrator.build_focus_dispatch_plan(
        manifest_path=manifest_path,
        current_focus_path=paths["current_focus"],
        decision_path=paths["decision"],
        report_dir=cos_dispatch_orchestrator.cos_dashboard.DEFAULT_REPORT_DIR,
        state_path=live_state_path,
    )
    return cos_dispatch_orchestrator.dispatch_focus_plan(
        manifest=parsed_manifest,
        envelope=envelope,
        worker_receipt_adapter=worker_receipt_adapter.strip(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap COS C2 lifecycle")
    parser.add_argument(
        "command",
        choices=(
            "arm",
            "status",
            "preflight",
            "roster-proposal",
            "run",
            "poke",
            "standby",
            "stop",
            "checkpoint",
            "ack",
            "finish-turn",
            "reattach",
            "dispatch-focus",
        ),
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--live-state", type=Path, default=DEFAULT_LIVE_STATE)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--no-wake",
        action="store_true",
        help="deprecated compatibility flag; automatic wakes belong to the watchdog",
    )
    parser.add_argument("--ownership", choices=("visible", "headless"), default="visible")
    parser.add_argument("--from-file", type=Path)
    parser.add_argument("--digest")
    parser.add_argument("--generation", type=int)
    parser.add_argument("--epoch", type=int)
    parser.add_argument(
        "--worker-receipt-adapter",
        default="",
        help="MODULE:CALLABLE worker-authenticated receipt adapter for durable live dispatch",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    selected_manifest_sha256 = manifest_file_sha256(args.manifest)
    if args.command == "preflight":
        result = preflight(
            manifest=manifest,
            manifest_path=args.manifest,
            live_state_path=args.live_state,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ready"] else 1
    if args.command == "roster-proposal":
        print(
            json.dumps(
                roster_proposal(manifest=manifest, live_state_path=args.live_state),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "arm":
        print(
            json.dumps(
                arm_from_cli(
                    manifest=manifest,
                    state_dir=args.state_dir,
                    manifest_sha256=selected_manifest_sha256,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    client: CoordClient | None = None
    try:
        client = CoordClient(
            CoordConfig.load(expected_principal_id=manifest.controller_coord_agent_id)
        )
    except CoordError:
        if args.command != "status":
            raise
    if args.command == "status":
        print(
            json.dumps(
                status(
                    client=client,
                    state_dir=args.state_dir,
                    manifest=manifest,
                    manifest_sha256=selected_manifest_sha256,
                    live_state_path=args.live_state,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    assert client is not None
    if args.command not in {"standby", "stop"}:
        _require_arm_marker(
            state_paths(args.state_dir)["armed"],
            manifest=manifest,
            manifest_sha256=selected_manifest_sha256,
        )
    if args.command == "checkpoint":
        if args.from_file is None:
            parser.error("checkpoint requires --from-file")
        state = _load_json(state_paths(args.state_dir)["state"])
        epoch = state.get("controller_epoch")
        if not isinstance(epoch, int) or state.get("authority") is not True:
            raise ContractError("checkpoint requires live supervisor authority")
        client.verify_live_epoch(SUPERVISOR_RESOURCE, epoch)
        decision = _load_json(state_paths(args.state_dir)["decision"])
        current_decision_digest = str(decision.get("decision_digest") or "")
        if not current_decision_digest:
            raise ContractError("checkpoint requires a current deterministic decision")
        receipt = checkpoint_actions(
            source=args.from_file,
            destination=state_paths(args.state_dir)["actions"],
            manifest=manifest,
            live_epoch=epoch,
            receipts_path=state_paths(args.state_dir)["action_receipts"],
            expected_decision_digest=current_decision_digest,
            allow_complete=decision.get("wake_required") is False,
        )
        coord_response = client.post_receipt(receipt)
        record_coord_acceptance(
            checkpoint_receipt=receipt,
            coord_response=coord_response,
            receipts_path=state_paths(args.state_dir)["action_receipts"],
        )
        result = {"ok": True, **receipt}
    elif args.command == "ack":
        if not args.digest or args.generation is None or args.epoch is None:
            parser.error("ack requires --digest, --generation, and --epoch")
        state = _load_json(state_paths(args.state_dir)["state"])
        if (
            state.get("authority") is not True
            or state.get("controller_epoch") != args.epoch
            or state.get("ownership") != args.ownership
        ):
            raise ContractError("ack requires the matching authoritative controller owner")
        client.verify_live_epoch(SUPERVISOR_RESOURCE, args.epoch)
        result = acknowledge_actions(
            actions_path=state_paths(args.state_dir)["actions"],
            receipts_path=state_paths(args.state_dir)["action_receipts"],
            manifest=manifest,
            digest=args.digest,
            generation=args.generation,
            epoch=args.epoch,
            ownership=args.ownership,
        )
        coord_response = client.post_receipt(result)
        result = commit_action_ack(
            ack_receipt=result,
            coord_response=coord_response,
            progress_path=state_paths(args.state_dir)["action_progress"],
            receipts_path=state_paths(args.state_dir)["action_receipts"],
        )
        result = {"ok": True, **result}
    elif args.command == "finish-turn":
        if not args.digest:
            parser.error("finish-turn requires --digest")
        result = finish_headless_turn(
            client=client, manifest=manifest, state_dir=args.state_dir, digest=args.digest
        )
    elif args.command == "reattach":
        if not args.digest:
            parser.error("reattach requires --digest")
        result = reattach_visible(
            client=client, manifest=manifest, state_dir=args.state_dir, digest=args.digest
        )
    elif args.command == "dispatch-focus":
        result = dispatch_current_focus(
            manifest=manifest,
            manifest_path=args.manifest,
            state_dir=args.state_dir,
            live_state_path=args.live_state,
            client=client,
            worker_receipt_adapter=args.worker_receipt_adapter,
        )
    elif args.command == "standby":
        result = set_standby(client=client, state_dir=args.state_dir)
    elif args.command == "stop":
        result = stop(client=client, state_dir=args.state_dir)
    elif args.command == "poke":
        result = run_tick(
            manifest=manifest,
            client=client,
            state_dir=args.state_dir,
            live_state_path=args.live_state,
            ownership=args.ownership,
            wake=True,
            manifest_sha256=selected_manifest_sha256,
        )
    else:
        while True:
            try:
                result = run_tick(
                    manifest=manifest,
                    client=client,
                    state_dir=args.state_dir,
                    live_state_path=args.live_state,
                    ownership=args.ownership,
                    wake=False,
                    manifest_sha256=selected_manifest_sha256,
                )
            except LeaseBlocked as exc:
                result = {
                    "ok": True,
                    "armed": True,
                    "authority": False,
                    "action": "lease-blocked-read-only",
                    "current_holder": exc.payload.get("current_holder"),
                }
            if args.once:
                break
            time.sleep(RECONCILE_SECONDS)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
