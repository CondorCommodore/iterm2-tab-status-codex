#!/usr/bin/env python3
"""Recover an armed bootstrap COS and record tab-vs-headless outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from c2_contract import SUPERVISOR_RESOURCE, ReceiptStore, load_manifest
from c2_coord_client import CoordClient, CoordConfig, CoordError
from cos_bootstrap_supervisor import (
    DEFAULT_MANIFEST,
    DEFAULT_STATE_DIR,
    _atomic_json,
    _iso,
    _load_json,
    _marker_status,
    manifest_contract_sha256,
    state_paths,
)
from cos_current_actions import action_wake_due, parse_actions, record_coord_acceptance
from cos_iterm_edge_client import poke_controller, request_edge

HEARTBEAT_STALE_SECONDS = 180
MAX_TAB_POKES = 2
MAX_BACKOFF_SECONDS = 900
MAX_EDGE_HEALTH_FAILURES = 2
ACTION_ACK_SECONDS = 90
MAX_ACTION_WAKE_ATTEMPTS = 2
EDGE_LAUNCHD_LABEL = "com.local.cos-iterm-edge"


def edge_health(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    result = request_edge(
        {"protocol": "cos-c2-iterm-edge-v1", "op": "health"},
        timeout_seconds=2.0,
    )
    expected_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    observed_digest = str(result.get("manifest_sha256") or "")
    if not result.get("ok"):
        return result
    if observed_digest != expected_digest:
        return {
            **result,
            "ok": False,
            "error": "edge loaded manifest digest does not match disk",
            "expected_manifest_sha256": expected_digest,
            "observed_manifest_sha256": observed_digest,
        }
    return result


def restart_edge(*, run: Any = subprocess.run) -> dict[str, Any]:
    target = f"gui/{os.getuid()}/{EDGE_LAUNCHD_LABEL}"
    result = run(
        ["launchctl", "kickstart", "-k", target],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return {
        "ok": result.returncode == 0,
        "target": target,
        "exit_code": result.returncode,
        "stderr_tail": result.stderr[-500:],
    }


def heartbeat_age(heartbeat: dict[str, Any], *, now_ts: float | None = None) -> float | None:
    now_ts = time.time() if now_ts is None else now_ts
    recorded = heartbeat.get("recorded_ts")
    if not isinstance(recorded, (int, float)):
        return None
    return max(0.0, now_ts - float(recorded))


def headless_resume_command(
    *,
    manifest_path: Path,
    state_dir: Path,
    runtime: str,
    session_id: str,
    actions_path: Path | None = None,
    action_digest: str | None = None,
) -> list[str]:
    supervisor = Path(__file__).resolve().with_name("cos_bootstrap_supervisor.py")
    bootstrap = (
        f"First run: python3 {supervisor} run --once --ownership headless "
        f"--manifest {manifest_path} --state-dir {state_dir}. Continue only if it reports "
        "authority=true. Then read every plan path in the run manifest and the actionable coord "
        "feed, resolve the current C2 decision, and record durable results through coord-api. "
        + (
            f"Then read {actions_path}; it was resumed from digest {action_digest}. A new epoch "
            "will rebind it to a new digest. Read the rebound header, then run "
            f"python3 {supervisor} ack --manifest {manifest_path} --state-dir {state_dir} "
            "--digest <rebound-sha256> --generation <rebound-generation> "
            "--epoch <live-epoch> --ownership headless. After acting, stage a chained next "
            f"generation and run python3 {supervisor} checkpoint --manifest {manifest_path} "
            f"--state-dir {state_dir} --from-file <staged-path>. Finally run python3 "
            f"{supervisor} finish-turn --manifest {manifest_path} --state-dir {state_dir} "
            "--digest <published-sha256> --ownership headless. "
            if actions_path is not None and action_digest
            else ""
        )
        + "Do not dispatch if the supervisor epoch cannot be verified."
    )
    if runtime == "codex":
        return ["codex", "exec", "resume", session_id, bootstrap]
    if runtime == "claude":
        return ["claude", "--resume", session_id, "--print", bootstrap]
    raise ValueError(f"unsupported controller runtime: {runtime}")


def _record_outcome(path: Path, value: dict[str, Any]) -> None:
    ReceiptStore(path).append(value)


def _headless_authority(heartbeat_path: Path, *, attempted_at: float) -> dict[str, Any] | None:
    heartbeat = _load_json(heartbeat_path)
    recorded_ts = heartbeat.get("recorded_ts")
    if (
        heartbeat.get("authority") is True
        and heartbeat.get("ownership") == "headless"
        and isinstance(heartbeat.get("controller_epoch"), int)
        and isinstance(recorded_ts, (int, float))
        and float(recorded_ts) > attempted_at
    ):
        return heartbeat
    return None


def _action_ack_matches(
    progress: dict[str, Any],
    *,
    digest: str,
    generation: int,
    epoch: int,
    after_ts: float,
    client: CoordClient,
    receipts_path: Path,
) -> bool:
    local_match = bool(
        progress.get("kind") == "action-ack"
        and progress.get("action_digest") == digest
        and progress.get("generation") == generation
        and progress.get("controller_epoch") == epoch
        and isinstance(progress.get("coord_accepted_ts"), (int, float))
        and float(progress["coord_accepted_ts"]) > after_ts
    )
    if not local_match:
        return False
    ack_key = f"c2-action-ack:{epoch}:{generation}:{digest}"
    ack_receipt = next(
        (
            receipt
            for receipt in ReceiptStore(receipts_path).records()
            if receipt.get("kind") == "action-ack" and receipt.get("idempotency_key") == ack_key
        ),
        None,
    )
    if ack_receipt is None:
        return False
    try:
        client.verify_receipt_readback(ack_receipt, int(progress.get("coord_message_id") or 0))
    except (CoordError, TypeError, ValueError):
        return False
    return True


def _checkpoint_has_durable_readback(
    actions: Any,
    *,
    client: CoordClient,
    receipts_path: Path,
) -> bool:
    records = ReceiptStore(receipts_path).records()
    checkpoint = next(
        (
            receipt
            for receipt in records
            if receipt.get("kind") == "action-checkpoint"
            and receipt.get("action_digest") == actions.digest
            and receipt.get("generation") == actions.generation
            and receipt.get("controller_epoch") == actions.controller_epoch
        ),
        None,
    )
    if checkpoint is None:
        return False
    acceptance = next(
        (
            receipt
            for receipt in records
            if receipt.get("kind") == "action-checkpoint-coord-accepted"
            and receipt.get("source_idempotency_key") == checkpoint.get("idempotency_key")
        ),
        None,
    )
    if acceptance is None:
        return False
    try:
        client.verify_receipt_readback(
            checkpoint,
            int(acceptance.get("coord_message_id") or 0),
        )
    except (CoordError, TypeError, ValueError):
        return False
    return True


def _action_prompt(
    *, path: Path, digest: str, generation: int, decision_digest: str, epoch: int
) -> str:
    return (
        f"/goal C2_CONTINUE actions={path} sha256={digest} generation={generation} "
        f"decision={decision_digest} epoch={epoch}. First run cosctl ack for this exact "
        "digest/generation/epoch, then read the file, execute its bounded next actions, and "
        "publish a new checkpoint before ending the turn."
    )


def run_once(
    *,
    manifest_path: Path,
    state_dir: Path,
    client: CoordClient | None,
    client_factory: Callable[[], CoordClient] | None = None,
    run: Any = subprocess.run,
    poke_fn: Any = poke_controller,
    edge_health_fn: Any | None = None,
    edge_restart_fn: Any | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now_ts is None else now_ts
    manifest = load_manifest(manifest_path)
    paths = state_paths(state_dir)
    watchdog_path = state_dir / "watchdog-state.json"
    outcomes_path = state_dir / "recovery-receipts.jsonl"
    if not paths["armed"].exists():
        return {"ok": True, "armed": False, "action": "inert"}
    marker = _marker_status(
        paths["armed"],
        manifest=manifest,
        manifest_sha256=manifest_contract_sha256(manifest),
    )
    if marker.get("valid") is not True:
        # A leftover marker is diagnostic state, not authority.  Refuse all
        # watchdog work before touching the edge, coord-api, or recovery state.
        return {
            "ok": False,
            "armed": True,
            "effective_armed": False,
            "action": "requires-explicit-rearm",
            "arm_marker": marker,
        }
    supervisor_state = _load_json(paths["state"])
    if supervisor_state.get("mode") == "bootstrap-standby":
        return {"ok": True, "armed": True, "action": "standby"}
    heartbeat = _load_json(paths["heartbeat"])
    age = heartbeat_age(heartbeat, now_ts=now_ts)
    watchdog = _load_json(
        watchdog_path,
        {
            "recovery_sequence": 0,
            "tab_pokes": 0,
            "provider_failures": 0,
            "edge_health_failures": 0,
        },
    )
    recovery_hold = _load_json(paths["recovery_hold"])
    edge_available = True

    if edge_health_fn is not None:
        try:
            edge_result = edge_health_fn()
            edge_ok = bool(edge_result.get("ok"))
            edge_error = str(edge_result.get("error") or "")
        except Exception as exc:
            edge_ok = False
            edge_error = f"{type(exc).__name__}: {exc}"
        if edge_ok:
            if any(
                int(watchdog.get(name) or 0)
                for name in ("edge_health_failures", "edge_restart_attempts")
            ) or watchdog.get("edge_restart_backoff_until"):
                watchdog["edge_health_failures"] = 0
                watchdog["edge_restart_attempts"] = 0
                watchdog["edge_restart_backoff_until"] = None
                watchdog["last_edge_healthy_at"] = _iso(now_ts)
                _atomic_json(watchdog_path, watchdog)
        else:
            edge_available = False
            failures = int(watchdog.get("edge_health_failures") or 0) + 1
            watchdog["edge_health_failures"] = failures
            watchdog["last_edge_error"] = edge_error or "edge health returned ok=false"
            watchdog["last_edge_failure_at"] = _iso(now_ts)
            _atomic_json(watchdog_path, watchdog)
            if (
                failures < MAX_EDGE_HEALTH_FAILURES
                and age is not None
                and age < HEARTBEAT_STALE_SECONDS
            ):
                return {
                    "ok": True,
                    "armed": True,
                    "action": "edge-health-degraded",
                    "edge_health_failures": failures,
                    "error": watchdog["last_edge_error"],
                }
            edge_backoff_until = watchdog.get("edge_restart_backoff_until")
            if (
                isinstance(edge_backoff_until, (int, float))
                and now_ts < edge_backoff_until
                and age is not None
                and age < HEARTBEAT_STALE_SECONDS
            ):
                return {
                    "ok": True,
                    "armed": True,
                    "action": "edge-restart-backoff-health-only",
                    "edge_health_failures": failures,
                    "backoff_seconds": int(edge_backoff_until - now_ts),
                    "error": watchdog["last_edge_error"],
                }
            if edge_restart_fn is None and age is not None and age < HEARTBEAT_STALE_SECONDS:
                return {
                    "ok": False,
                    "armed": True,
                    "action": "edge-restart-unavailable",
                    "edge_health_failures": failures,
                }
            if edge_restart_fn is None:
                restart_result = {"ok": False, "error": "edge restart unavailable"}
            else:
                try:
                    restart_result = edge_restart_fn()
                except Exception as exc:
                    restart_result = {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            restart_ok = bool(restart_result.get("ok"))
            restart_attempts = int(watchdog.get("edge_restart_attempts") or 0) + 1
            restart_backoff = min(
                MAX_BACKOFF_SECONDS,
                60 * (2 ** min(restart_attempts - 1, 4)),
            )
            edge_sequence = int(watchdog.get("edge_restart_sequence") or 0)
            receipt = {
                "idempotency_key": f"edge-recovery:{edge_sequence}",
                "recorded_at": _iso(now_ts),
                "kind": "edge-recovery",
                "success": restart_ok,
                "health_failures": failures,
                "health_error": watchdog["last_edge_error"],
                "restart": restart_result,
                "restart_attempt": restart_attempts,
                "backoff_seconds": restart_backoff,
            }
            _record_outcome(state_dir / "edge-recovery-receipts.jsonl", receipt)
            watchdog["edge_restart_sequence"] = int(watchdog.get("edge_restart_sequence") or 0) + 1
            watchdog["edge_health_failures"] = 0
            watchdog["edge_restart_attempts"] = restart_attempts
            watchdog["edge_restart_backoff_until"] = now_ts + restart_backoff
            watchdog["last_edge_restart_at"] = _iso(now_ts)
            _atomic_json(watchdog_path, watchdog)
            if age is not None and age < HEARTBEAT_STALE_SECONDS:
                return {
                    "ok": restart_ok,
                    "armed": True,
                    "action": "edge-restarted" if restart_ok else "edge-restart-failed",
                    "receipt": receipt,
                }

    decision = _load_json(paths["decision"])
    decision_digest = str(decision.get("decision_digest") or "")
    actions = None
    action_error = None
    try:
        actions = parse_actions(paths["actions"], manifest=manifest)
    except Exception as exc:
        action_error = str(exc)

    action_pending_since = watchdog.get("action_pending_since")
    action_pending_digest = str(watchdog.get("action_pending_digest") or "")
    action_pending_generation = watchdog.get("action_pending_generation")
    action_pending_epoch = watchdog.get("action_pending_epoch")
    action_attempts = int(watchdog.get("action_wake_attempts") or 0)
    if isinstance(action_pending_since, (int, float)) and action_pending_digest:
        if client is None:
            if client_factory is None:
                raise CoordError("coord client is required while awaiting action acknowledgment")
            client = client_factory()
        pending_lease = client.get_resource(SUPERVISOR_RESOURCE)
        pending_live_epoch = pending_lease.get("epoch") if isinstance(pending_lease, dict) else None
        pending_holder = (
            str(pending_lease.get("actual_holder") or pending_lease.get("holder") or "")
            if isinstance(pending_lease, dict)
            else ""
        )
        if (
            pending_live_epoch != action_pending_epoch
            or pending_holder != client.config.principal_id
        ):
            watchdog.update(
                {
                    "action_pending_since": None,
                    "action_pending_digest": None,
                    "action_pending_generation": None,
                    "action_pending_epoch": None,
                    "action_wake_attempts": 0,
                    "last_action_error": "live epoch lost while awaiting model acknowledgment",
                }
            )
            _atomic_json(watchdog_path, watchdog)
            return {
                "ok": False,
                "armed": True,
                "action": "action-ack-epoch-lost",
                "expected_epoch": action_pending_epoch,
                "live_epoch": pending_live_epoch,
            }
        progress = _load_json(paths["action_progress"])
        if _action_ack_matches(
            progress,
            digest=action_pending_digest,
            generation=int(action_pending_generation or 0),
            epoch=int(action_pending_epoch or 0),
            after_ts=float(action_pending_since),
            client=client,
            receipts_path=paths["action_receipts"],
        ):
            watchdog.update(
                {
                    "action_pending_since": None,
                    "action_pending_digest": None,
                    "action_pending_generation": None,
                    "action_pending_epoch": None,
                    "action_wake_attempts": 0,
                    "last_action_ack_at": _iso(now_ts),
                }
            )
            _atomic_json(watchdog_path, watchdog)
            return {
                "ok": True,
                "armed": True,
                "action": "action-acknowledged",
                "action_digest": action_pending_digest,
            }
        pending_age = now_ts - float(action_pending_since)
        if pending_age < ACTION_ACK_SECONDS:
            return {
                "ok": True,
                "armed": True,
                "action": "awaiting-action-ack",
                "ack_wait_seconds": int(ACTION_ACK_SECONDS - pending_age),
                "action_digest": action_pending_digest,
            }
        if action_attempts >= MAX_ACTION_WAKE_ATTEMPTS:
            if client is None and client_factory is not None:
                client = client_factory()
            hold = {
                "recorded_at": _iso(now_ts),
                "recorded_ts": now_ts,
                "reason": "two exact action wake acknowledgments expired",
                "controller_epoch": action_pending_epoch,
                "action_digest": action_pending_digest,
                "generation": action_pending_generation,
            }
            _atomic_json(paths["recovery_hold"], hold)
            hold_receipt = {
                **hold,
                "idempotency_key": (
                    f"c2-action-yield:{action_pending_epoch}:"
                    f"{action_pending_generation}:{action_pending_digest}"
                ),
                "kind": "action-yield",
            }
            store = ReceiptStore(paths["action_receipts"])
            if not store.has_idempotency_key(hold_receipt["idempotency_key"]):
                store.append(hold_receipt)
            if client is not None:
                try:
                    client.post_receipt(hold_receipt)
                except Exception as exc:
                    hold["coord_audit_error"] = str(exc)
            watchdog.update(
                {
                    "action_pending_since": None,
                    "action_pending_digest": None,
                    "action_pending_generation": None,
                    "action_pending_epoch": None,
                    "last_yield_requested_at": _iso(now_ts),
                }
            )
            _atomic_json(watchdog_path, watchdog)
            return {"ok": True, "armed": True, "action": "yield-requested", "hold": hold}

    action_due = False
    action_reason = ""
    if actions is not None and decision_digest:
        action_due, action_reason = action_wake_due(
            actions, decision_digest=decision_digest, now_ts=now_ts
        )
        progress = _load_json(paths["action_progress"])
        checkpoint_published = False
        action_receipts = ReceiptStore(paths["action_receipts"])
        receipt_records = action_receipts.records()
        local_checkpoint = next(
            (
                receipt
                for receipt in receipt_records
                if receipt.get("kind") == "action-checkpoint"
                and receipt.get("action_digest") == actions.digest
                and receipt.get("generation") == actions.generation
                and receipt.get("controller_epoch") == actions.controller_epoch
            ),
            None,
        )
        acceptance_present = any(
            receipt.get("kind") == "action-checkpoint-coord-accepted"
            and receipt.get("action_digest") == actions.digest
            and receipt.get("generation") == actions.generation
            and receipt.get("controller_epoch") == actions.controller_epoch
            for receipt in receipt_records
        )
        if local_checkpoint is not None:
            if client is None:
                if client_factory is None:
                    raise CoordError("coord client is required to verify checkpoint durability")
                client = client_factory()
            if not acceptance_present:
                try:
                    client.verify_live_epoch(SUPERVISOR_RESOURCE, actions.controller_epoch)
                    coord_response = client.post_receipt(local_checkpoint)
                    record_coord_acceptance(
                        checkpoint_receipt=local_checkpoint,
                        coord_response=coord_response,
                        receipts_path=paths["action_receipts"],
                    )
                except CoordError:
                    pass
            checkpoint_published = _checkpoint_has_durable_readback(
                actions,
                client=client,
                receipts_path=paths["action_receipts"],
            )
        never_acknowledged = not (
            progress.get("action_digest") == actions.digest
            and progress.get("generation") == actions.generation
            and progress.get("controller_epoch") == actions.controller_epoch
        )
        if (
            not action_due
            and never_acknowledged
            and decision.get("wake_required") is True
            and not checkpoint_published
            and not (
                recovery_hold and watchdog.get("last_headless_checkpoint_digest") == actions.digest
            )
        ):
            action_due = True
            action_reason = "current action generation has not been acknowledged"

    if (
        recovery_hold
        and actions is not None
        and not action_due
        and watchdog.get("last_headless_checkpoint_digest") == actions.digest
    ):
        if client is None:
            if client_factory is None:
                raise CoordError("coord client is required during headless recovery hold")
            client = client_factory()
        live_lease = client.get_resource(SUPERVISOR_RESOURCE)
        if live_lease is not None:
            return {
                "ok": False,
                "armed": True,
                "action": "headless-waiting-live-lease-present",
                "live_epoch": live_lease.get("epoch") if isinstance(live_lease, dict) else None,
            }
        return {
            "ok": True,
            "armed": True,
            "action": "headless-waiting",
            "action_digest": actions.digest,
            "next_check_ts": actions.next_check_ts,
        }

    if (
        manifest.controller_has_visible_terminal()
        and (action_due or isinstance(action_pending_since, (int, float)))
        and not recovery_hold
    ):
        if actions is None:
            return {
                "ok": False,
                "armed": True,
                "action": "invalid-action-checkpoint",
                "error": action_error,
            }
        if client is None:
            if client_factory is None:
                raise CoordError("coord client is required for an action wake")
            client = client_factory()
        lease = client.get_resource(SUPERVISOR_RESOURCE)
        epoch = lease.get("epoch") if isinstance(lease, dict) else None
        holder = (
            str(lease.get("actual_holder") or lease.get("holder") or "")
            if isinstance(lease, dict)
            else ""
        )
        if epoch != actions.controller_epoch or holder != client.config.principal_id:
            return {
                "ok": True,
                "armed": True,
                "action": "awaiting-live-epoch-for-action-wake",
                "checkpoint_epoch": actions.controller_epoch,
                "live_epoch": epoch,
            }
        attempt = action_attempts + 1
        if isinstance(action_pending_since, (int, float)):
            latest_progress = _load_json(paths["action_progress"])
            if _action_ack_matches(
                latest_progress,
                digest=actions.digest,
                generation=actions.generation,
                epoch=actions.controller_epoch,
                after_ts=float(action_pending_since),
                client=client,
                receipts_path=paths["action_receipts"],
            ):
                watchdog.update(
                    {
                        "action_pending_since": None,
                        "action_pending_digest": None,
                        "action_pending_generation": None,
                        "action_pending_epoch": None,
                        "action_wake_attempts": 0,
                        "last_action_ack_at": _iso(now_ts),
                    }
                )
                _atomic_json(watchdog_path, watchdog)
                return {
                    "ok": True,
                    "armed": True,
                    "action": "action-acknowledged-before-retry",
                    "action_digest": actions.digest,
                }
        key = f"c2-action-wake:{epoch}:{actions.generation}:{actions.digest}:{attempt}"
        result = poke_fn(
            text=_action_prompt(
                path=paths["actions"],
                digest=actions.digest,
                generation=actions.generation,
                decision_digest=decision_digest,
                epoch=epoch,
            ),
            controller_epoch=epoch,
            idempotency_key=key,
        )
        attempted = bool(result.get("injection_attempted") or result.get("ok"))
        if attempted:
            watchdog.update(
                {
                    "action_pending_since": now_ts,
                    "action_pending_digest": actions.digest,
                    "action_pending_generation": actions.generation,
                    "action_pending_epoch": epoch,
                    "action_wake_attempts": attempt,
                    "last_action_wake_at": _iso(now_ts),
                    "last_action_wake_reason": action_reason,
                }
            )
            _atomic_json(watchdog_path, watchdog)
        return {
            **result,
            "ok": attempted,
            "armed": True,
            "action": "action-wake",
            "wake_reason": action_reason,
            "awaiting_model_ack": attempted,
        }

    pending_since = watchdog.get("pending_since")
    pending_transport = watchdog.get("pending_transport")
    pending_headless_without_visible_reattach = (
        manifest.controller_has_visible_terminal()
        and pending_transport == "headless"
        and heartbeat.get("ownership") != "visible"
    )
    if (
        isinstance(pending_since, (int, float))
        and isinstance(heartbeat.get("recorded_ts"), (int, float))
        and heartbeat["recorded_ts"] > pending_since
        and not pending_headless_without_visible_reattach
    ):
        receipt = {
            "idempotency_key": (
                f"{watchdog.get('pending_key')}:visible-reattach"
                if pending_transport == "headless"
                else str(watchdog.get("pending_key") or f"recovery:{pending_since}")
            ),
            "recorded_at": _iso(now_ts),
            "transport": watchdog.get("pending_transport"),
            "success": True,
            "ack_latency_ms": int((heartbeat["recorded_ts"] - pending_since) * 1000),
            "controller_epoch": heartbeat.get("controller_epoch"),
            "visible_reattach_required": (
                manifest.controller_has_visible_terminal()
                and watchdog.get("pending_transport") == "headless"
            ),
        }
        _record_outcome(outcomes_path, receipt)
        watchdog = {
            **watchdog,
            "tab_pokes": 0,
            "provider_failures": 0,
            "pending_since": None,
            "pending_key": None,
            "pending_transport": None,
            "last_success_at": _iso(now_ts),
        }
        _atomic_json(watchdog_path, watchdog)
        return {"ok": True, "armed": True, "action": "recovered", "receipt": receipt}

    if pending_headless_without_visible_reattach:
        return {
            "ok": False,
            "armed": True,
            "action": "awaiting-visible-reattach",
            "headless_authority_active": heartbeat.get("ownership") == "headless",
        }

    if age is not None and age < HEARTBEAT_STALE_SECONDS and not recovery_hold:
        return {"ok": True, "armed": True, "action": "healthy", "heartbeat_age": age}

    sequence = int(watchdog.get("recovery_sequence") or 0)
    primary = "headless" if recovery_hold else manifest.recovery_for(sequence)
    tab_pokes = int(watchdog.get("tab_pokes") or 0)
    if client is None:
        if client_factory is None:
            raise CoordError("coord client is required for stale-heartbeat recovery")
        client = client_factory()
    lease = client.get_resource(SUPERVISOR_RESOURCE)
    epoch = lease.get("epoch") if isinstance(lease, dict) else None
    holder = (
        str(lease.get("actual_holder") or lease.get("holder") or "")
        if isinstance(lease, dict)
        else ""
    )

    backoff_until = watchdog.get("backoff_until")
    if isinstance(backoff_until, (int, float)) and now_ts < backoff_until:
        return {
            "ok": True,
            "armed": True,
            "action": "provider-backoff-health-only",
            "backoff_seconds": int(backoff_until - now_ts),
            "lease_checked": True,
            "live_epoch": epoch,
        }

    use_tab = (
        manifest.controller_has_visible_terminal()
        and edge_available
        and primary == "tab"
        and tab_pokes < MAX_TAB_POKES
    )
    if use_tab:
        if not isinstance(epoch, int) or holder != client.config.principal_id:
            return {
                "ok": True,
                "armed": True,
                "action": "awaiting-live-epoch-for-tab-trial",
            }
        key = f"c2-recovery:{sequence}:tab:{tab_pokes + 1}:{epoch}"
        result = poke_fn(
            text=(
                f"/goal C2_RECOVERY_POKE sequence={sequence} epoch={epoch}. Run the bootstrap "
                "supervisor tick, read the current decision and actionable feed, then continue "
                "bounded supervision only while that epoch remains live."
            ),
            controller_epoch=epoch,
            idempotency_key=key,
        )
        watchdog.update(
            {
                "tab_pokes": tab_pokes + 1,
                "pending_since": now_ts if result.get("ok") else None,
                "pending_key": key if result.get("ok") else None,
                "pending_transport": "tab" if result.get("ok") else None,
                "last_attempt_at": _iso(now_ts),
            }
        )
        _atomic_json(watchdog_path, watchdog)
        return {"ok": bool(result.get("ok")), "armed": True, "action": "tab-poke", **result}

    # A headless resume must become the sole epoch owner.  Never start it while
    # the visible owner still has a live epoch; a later watchdog tick will retry
    # after coord-api expiry.
    if lease is not None:
        return {
            "ok": True,
            "armed": True,
            "action": "awaiting-visible-lease-expiry-for-headless-trial",
            "live_epoch": epoch,
            "primary_transport": primary,
            "tab_pokes": tab_pokes,
        }

    key = f"c2-recovery:{sequence}:headless"
    command = headless_resume_command(
        manifest_path=manifest_path,
        state_dir=state_dir,
        runtime=manifest.controller_runtime,
        session_id=manifest.controller_cli_session_id,
        actions_path=(paths["actions"] if actions is not None else None),
        action_digest=(actions.digest if actions is not None else None),
    )
    started = time.monotonic()
    before_action_digest = actions.digest if actions is not None else None
    try:
        result = run(command, capture_output=True, text=True, timeout=1800)
        duration_ms = int((time.monotonic() - started) * 1000)
        authority = _headless_authority(paths["heartbeat"], attempted_at=now_ts)
        after_actions = None
        try:
            after_actions = parse_actions(paths["actions"], manifest=manifest)
        except Exception:
            pass
        checkpoint_advanced = bool(
            after_actions is not None
            and (before_action_digest is None or after_actions.digest != before_action_digest)
            and after_actions.header.get("ownership") == "headless"
        )
        checkpoint_receipt = next(
            (
                receipt
                for receipt in reversed(ReceiptStore(paths["action_receipts"]).records())
                if receipt.get("kind") == "action-checkpoint"
                and after_actions is not None
                and receipt.get("action_digest") == after_actions.digest
                and receipt.get("generation") == after_actions.generation
                and receipt.get("controller_epoch") == after_actions.controller_epoch
                and float(receipt.get("recorded_ts") or 0) > now_ts
            ),
            None,
        )
        coord_acceptance = next(
            (
                receipt
                for receipt in reversed(ReceiptStore(paths["action_receipts"]).records())
                if receipt.get("kind") == "action-checkpoint-coord-accepted"
                and checkpoint_receipt is not None
                and receipt.get("source_idempotency_key")
                == checkpoint_receipt.get("idempotency_key")
                and receipt.get("action_digest") == after_actions.digest
                and receipt.get("controller_epoch") == after_actions.controller_epoch
                and float(receipt.get("recorded_ts") or 0) > now_ts
            ),
            None,
        )
        model_checkpoint_published = checkpoint_receipt is not None
        coord_readback_error = None
        model_checkpoint_durable = False
        if checkpoint_receipt is not None and coord_acceptance is not None and client is not None:
            try:
                client.verify_receipt_readback(
                    checkpoint_receipt,
                    int(coord_acceptance.get("coord_message_id") or 0),
                )
                model_checkpoint_durable = True
            except (CoordError, TypeError, ValueError) as exc:
                coord_readback_error = str(exc)
        lease_after = client.get_resource(SUPERVISOR_RESOURCE) if client is not None else None
        epoch_released = lease_after is None
        headless_turn_success = bool(
            result.returncode == 0
            and authority is not None
            and checkpoint_advanced
            and model_checkpoint_published
            and model_checkpoint_durable
            and epoch_released
        )
        success = headless_turn_success
        receipt = {
            "idempotency_key": key,
            "recorded_at": _iso(now_ts),
            "transport": "headless",
            "primary_transport": primary,
            "success": success,
            "headless_turn_success": headless_turn_success,
            "duration_ms": duration_ms,
            "exit_code": result.returncode,
            "authority_acquired": authority is not None,
            "checkpoint_advanced": checkpoint_advanced,
            "model_checkpoint_published": model_checkpoint_published,
            "model_checkpoint_durable": model_checkpoint_durable,
            "coord_readback_error": coord_readback_error,
            "checkpoint_digest": after_actions.digest if after_actions is not None else None,
            "epoch_released": epoch_released,
            "controller_epoch": (authority.get("controller_epoch") if authority else None),
            "visible_reattach_required": False,
            "recovery_state": (
                "bounded-turn-complete" if headless_turn_success else "headless-failed"
            ),
            "stdout_tail": result.stdout[-500:],
            "stderr_tail": result.stderr[-500:],
        }
        _record_outcome(outcomes_path, receipt)
    except (OSError, subprocess.TimeoutExpired) as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        success = False
        receipt = {
            "idempotency_key": key,
            "recorded_at": _iso(now_ts),
            "transport": "headless",
            "primary_transport": primary,
            "success": False,
            "duration_ms": duration_ms,
            "timed_out": isinstance(exc, subprocess.TimeoutExpired),
            "provider_error": type(exc).__name__,
            "visible_reattach_required": False,
            "stdout_tail": str(getattr(exc, "stdout", "") or "")[-500:],
            "stderr_tail": str(getattr(exc, "stderr", "") or "")[-500:],
        }
        _record_outcome(outcomes_path, receipt)

    provider_succeeded = bool(receipt.get("headless_turn_success"))
    failures = 0 if provider_succeeded else int(watchdog.get("provider_failures") or 0) + 1
    backoff = (
        0 if provider_succeeded else min(MAX_BACKOFF_SECONDS, 60 * (2 ** min(failures - 1, 4)))
    )
    watchdog.update(
        {
            "recovery_sequence": sequence + 1,
            "tab_pokes": 0,
            "provider_failures": failures,
            "backoff_until": now_ts + backoff,
            "pending_since": None,
            "pending_key": None,
            "pending_transport": None,
            "last_attempt_at": _iso(now_ts),
            "last_headless_checkpoint_digest": (
                receipt.get("checkpoint_digest") if provider_succeeded else None
            ),
        }
    )
    _atomic_json(watchdog_path, watchdog)
    return {
        "ok": success,
        "armed": True,
        "action": "headless-resume",
        "receipt": receipt,
        "backoff_seconds": backoff,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Armed bootstrap COS watchdog")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        result = run_once(
            manifest_path=args.manifest,
            state_dir=args.state_dir,
            client=None,
            client_factory=lambda: CoordClient(
                CoordConfig.load(expected_principal_id=manifest.controller_coord_agent_id)
            ),
            edge_health_fn=lambda: edge_health(args.manifest),
            edge_restart_fn=restart_edge,
        )
    except CoordError as exc:
        result = {"ok": False, "action": "coord-api-unavailable-health-only", "error": str(exc)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
