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
import time
from pathlib import Path
from typing import Any

from c2_contract import (
    CONTROLLER_MODES,
    RECONCILE_SECONDS,
    SUPERVISOR_RENEW_SECONDS,
    SUPERVISOR_RESOURCE,
    SUPERVISOR_TTL_SECONDS,
    ContractError,
    RunManifest,
    load_manifest,
    normalize_worker_state,
)
from c2_coord_client import (
    CoordClient,
    CoordConfig,
    CoordError,
    LeaseBlocked,
    LeaseHandle,
    LeaseLost,
)
from cos_iterm_edge_client import poke_controller

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "cos-c2"
DEFAULT_MANIFEST = Path.home() / ".config" / "cos-c2" / "run-manifest.json"
DEFAULT_LIVE_STATE = Path.home() / ".claude" / "plans" / "fleet-reports" / "iterm-live-state.json"


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
        "pokes": state_dir / "poke-receipts.jsonl",
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
    if handle is not None:
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
        producer={
            "kind": "c2-supervisor",
            "manifest_id": manifest.manifest_id,
            "controller_id": manifest.controller_id,
            "controller_runtime": manifest.controller_runtime,
            "controller_session_id": manifest.controller_cli_session_id,
            "ownership": ownership,
        },
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
                "state": state,
                "observed": observed or None,
            }
        )
    return observations


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
    idle = [item for item in workers if item["state"] == "idle"]
    exceptions = [
        item
        for item in workers
        if item["state"] in {"needs_input", "blocked", "stale", "lost", "unknown"}
    ]
    task_items = [item for item in items if item.get("kind") == "task"]
    message_items = [item for item in items if item.get("kind") != "task"]
    reasons: list[str] = []
    if idle and task_items:
        reasons.append("idle worker and actionable task require assignment decision")
    if exceptions:
        reasons.append("worker exception requires recovery decision")
    if message_items:
        reasons.append("actionable coordination message requires model decision")
    return {
        "generated_at": _iso(now_ts),
        "generated_ts": now_ts,
        "manifest_id": manifest.manifest_id,
        "workers": workers,
        "actionable_items": items,
        "idle_worker_ids": [item["worker_id"] for item in idle],
        "exception_worker_ids": [item["worker_id"] for item in exceptions],
        "wake_required": bool(reasons),
        "wake_reasons": reasons,
    }


def decision_digest(decision: dict[str, Any]) -> str:
    stable = dict(decision)
    stable.pop("generated_at", None)
    stable.pop("generated_ts", None)
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
) -> dict[str, Any]:
    paths = state_paths(state_dir)
    state = _load_json(
        paths["state"],
        {"mode": "bootstrap-authoritative", "authority": False, "lease": None},
    )
    if not paths["armed"].exists():
        return {"ok": True, "armed": False, "action": "inert"}
    armed_marker = paths["armed"].read_text(encoding="utf-8").strip()
    if armed_marker != f"manifest_id={manifest.manifest_id}":
        raise ContractError("armed marker does not match loaded manifest; explicit re-arm required")
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
    previous = _load_json(paths["decision"])
    decision["decision_digest"] = digest
    decision["controller_epoch"] = handle.epoch
    decision["wake_delivered"] = False
    _atomic_json(paths["decision"], decision)
    poked = False
    poke_result: dict[str, Any] | None = None
    already_delivered = (
        previous.get("decision_digest") == digest and previous.get("wake_delivered") is True
    )
    if wake and decision["wake_required"] and not already_delivered:
        prompt = (
            "/goal C2_WAKE decision="
            f"{paths['decision']} epoch={handle.epoch}. Read every manifest plan path and the "
            "actionable coord feed; resolve exceptions, assign complete bounded slices through "
            "fenced dispatch envelopes, and record actions through coord-api."
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
    }
    _atomic_json(paths["heartbeat"], heartbeat)
    return {"ok": True, "armed": True, **heartbeat, "poke_result": poke_result}


def arm(
    *, manifest: RunManifest, state_dir: Path, validate_plan_paths: bool = True
) -> dict[str, Any]:
    if validate_plan_paths:
        missing = [path for path in manifest.plan_paths if not Path(path).is_file()]
        if missing:
            raise ContractError("authoritative plan paths are missing: " + ", ".join(missing))
    paths = state_paths(state_dir)
    paths["armed"].parent.mkdir(parents=True, exist_ok=True)
    watchdog_path = state_dir / "watchdog-state.json"
    prior_watchdog = _load_json(watchdog_path)
    for stale_path in (paths["heartbeat"], paths["decision"]):
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
        },
    )
    paths["armed"].write_text(f"manifest_id={manifest.manifest_id}\n", encoding="utf-8")
    os.chmod(paths["armed"], 0o600)
    state = {
        "mode": "bootstrap-authoritative",
        "authority": False,
        "manifest_id": manifest.manifest_id,
        "lease": None,
        "armed_at": _iso(),
    }
    _atomic_json(paths["state"], state)
    return {"ok": True, "armed": True, **state}


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


def status(*, client: CoordClient | None, state_dir: Path) -> dict[str, Any]:
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
    return {
        "armed": paths["armed"].exists(),
        "state": state,
        "heartbeat": heartbeat,
        "live_lease": lease,
        "coord_error": error,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap COS C2 lifecycle")
    parser.add_argument("command", choices=("arm", "status", "run", "poke", "standby", "stop"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--live-state", type=Path, default=DEFAULT_LIVE_STATE)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-wake", action="store_true")
    parser.add_argument("--ownership", choices=("visible", "headless"), default="visible")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.command == "arm":
        print(
            json.dumps(arm(manifest=manifest, state_dir=args.state_dir), indent=2, sort_keys=True)
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
        print(json.dumps(status(client=client, state_dir=args.state_dir), indent=2, sort_keys=True))
        return 0
    assert client is not None
    if args.command == "standby":
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
                    wake=not args.no_wake,
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
