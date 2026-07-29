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

from c2_contract import ReceiptStore, SUPERVISOR_RESOURCE, load_manifest
from c2_coord_client import CoordClient, CoordConfig, CoordError
from cos_bootstrap_supervisor import DEFAULT_MANIFEST, DEFAULT_STATE_DIR, _atomic_json, _iso, _load_json, state_paths
from cos_iterm_edge_client import poke_controller, request_edge


HEARTBEAT_STALE_SECONDS = 180
MAX_TAB_POKES = 2
MAX_BACKOFF_SECONDS = 900
MAX_EDGE_HEALTH_FAILURES = 2
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
    *, manifest_path: Path, state_dir: Path, runtime: str, session_id: str
) -> list[str]:
    supervisor = Path(__file__).resolve().with_name("cos_bootstrap_supervisor.py")
    bootstrap = (
        f"First run: python3 {supervisor} run --once --ownership headless "
        f"--manifest {manifest_path} --state-dir {state_dir}. Continue only if it reports "
        "authority=true. Then read every plan path in the run manifest and the actionable coord "
        "feed, resolve the current C2 decision, and record durable results through coord-api. "
        "Do not dispatch if the supervisor epoch cannot be verified."
    )
    if runtime == "codex":
        return ["codex", "exec", "resume", session_id, bootstrap]
    if runtime == "claude":
        return ["claude", "--resume", session_id, "--print", bootstrap]
    raise ValueError(f"unsupported controller runtime: {runtime}")


def _record_outcome(path: Path, value: dict[str, Any]) -> None:
    ReceiptStore(path).append(value)


def _headless_authority(
    heartbeat_path: Path, *, attempted_at: float
) -> dict[str, Any] | None:
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
            if failures < MAX_EDGE_HEALTH_FAILURES and age is not None and age < HEARTBEAT_STALE_SECONDS:
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
            receipt = {
                "idempotency_key": f"edge-recovery:{int(watchdog.get('edge_restart_sequence') or 0)}",
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
            watchdog["edge_restart_sequence"] = int(
                watchdog.get("edge_restart_sequence") or 0
            ) + 1
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

    pending_since = watchdog.get("pending_since")
    if isinstance(pending_since, (int, float)) and isinstance(
        heartbeat.get("recorded_ts"), (int, float)
    ) and heartbeat["recorded_ts"] > pending_since:
        receipt = {
            "idempotency_key": str(watchdog.get("pending_key") or f"recovery:{pending_since}"),
            "recorded_at": _iso(now_ts),
            "transport": watchdog.get("pending_transport"),
            "success": True,
            "ack_latency_ms": int((heartbeat["recorded_ts"] - pending_since) * 1000),
            "controller_epoch": heartbeat.get("controller_epoch"),
            "visible_reattach_required": watchdog.get("pending_transport") == "headless",
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

    if age is not None and age < HEARTBEAT_STALE_SECONDS:
        return {"ok": True, "armed": True, "action": "healthy", "heartbeat_age": age}

    sequence = int(watchdog.get("recovery_sequence") or 0)
    primary = manifest.recovery_for(sequence)
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

    use_tab = edge_available and primary == "tab" and tab_pokes < MAX_TAB_POKES
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
    )
    started = time.monotonic()
    try:
        result = run(command, capture_output=True, text=True, timeout=1800)
        duration_ms = int((time.monotonic() - started) * 1000)
        authority = _headless_authority(
            paths["heartbeat"], attempted_at=now_ts
        )
        success = result.returncode == 0 and authority is not None
        receipt = {
            "idempotency_key": key,
            "recorded_at": _iso(now_ts),
            "transport": "headless",
            "primary_transport": primary,
            "success": success,
            "duration_ms": duration_ms,
            "exit_code": result.returncode,
            "authority_acquired": authority is not None,
            "controller_epoch": (
                authority.get("controller_epoch") if authority else None
            ),
            "visible_reattach_required": True,
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
            "visible_reattach_required": True,
            "stdout_tail": str(getattr(exc, "stdout", "") or "")[-500:],
            "stderr_tail": str(getattr(exc, "stderr", "") or "")[-500:],
        }
        _record_outcome(outcomes_path, receipt)

    failures = 0 if success else int(watchdog.get("provider_failures") or 0) + 1
    backoff = 0 if success else min(MAX_BACKOFF_SECONDS, 60 * (2 ** min(failures - 1, 4)))
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
                CoordConfig.load(
                    expected_principal_id=manifest.controller_coord_agent_id
                )
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
