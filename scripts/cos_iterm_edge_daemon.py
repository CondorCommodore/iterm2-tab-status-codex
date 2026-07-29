#!/usr/bin/env python3
"""iTerm2 Python API edge daemon for fenced C2 tab transport.

Run inside iTerm2's Python runtime.  Local callers use a mode-0600 Unix socket;
the daemon itself resolves the exact registered session and verifies the live
coord-api epoch immediately before sending terminal bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PARENT_SCRIPTS_DIR = SCRIPT_DIR.parent
if str(PARENT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_SCRIPTS_DIR))

from c2_contract import ContractError, DispatchEnvelope, ReceiptStore, load_manifest
from c2_visual_decision import VisualDecision, VisualObservation
from c2_coord_client import CoordClient, CoordConfig, LeaseBlocked
from cos_iterm_edge_client import DEFAULT_SOCKET_PATH, MAX_RESPONSE_BYTES
from cos_tab_dispatch import (
    dispatch_registered,
    dispatch_registered_headless,
    execute_visual_decision,
    send_controller_poke,
)


DEFAULT_MANIFEST = Path.home() / ".config" / "cos-c2" / "run-manifest.json"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "cos-c2"


def acquire_edge_lock(socket_path: Path) -> int:
    """Hold a machine-local single-instance lock for one edge socket."""
    lock_path = socket_path.with_name(f"{socket_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RuntimeError(f"another iTerm edge owns {socket_path}") from exc
    return fd


class EdgeDaemon:
    def __init__(
        self,
        connection: Any,
        *,
        manifest_path: Path,
        state_dir: Path,
    ):
        self.connection = connection
        self.manifest_path: Path | None = manifest_path
        self.manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        self.manifest = load_manifest(manifest_path)
        self.client = CoordClient(
            CoordConfig.load(
                expected_principal_id=self.manifest.controller_coord_agent_id
            )
        )
        self.dispatch_receipts = ReceiptStore(state_dir / "dispatch-receipts.jsonl")
        self.poke_receipts = ReceiptStore(state_dir / "poke-receipts.jsonl")
        self.dispatch_inflight: set[str] = set()
        self.dispatch_guard = asyncio.Lock()

    def disk_manifest_sha256(self) -> str | None:
        if self.manifest_path is None:
            return self.manifest_sha256
        try:
            return hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        except OSError:
            return None

    def rejection_receipt(
        self,
        *,
        envelope: DispatchEnvelope,
        transport: str,
        error: str,
        reservation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        worker = self.manifest.worker(envelope.worker_id)
        return {
            "receipt_version": 1,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "assignment_id": envelope.assignment_id,
            "task_id": envelope.task_id,
            "attempt_id": envelope.attempt_id,
            "worker_id": worker.worker_id,
            "target_iterm_session_id": worker.iterm_session_id,
            "target_tty": worker.tty,
            "target_runtime": worker.runtime,
            "payload_digest": envelope.digest(),
            "controller_epoch": envelope.controller_epoch,
            "idempotency_key": envelope.idempotency_key,
            "submit_method": f"{transport}-rejected-before-injection",
            "observed_ack": False,
            "transport": transport,
            "error": error,
            "reservation": reservation,
        }

    async def audit_receipt(
        self, result: dict[str, Any], receipt: dict[str, Any]
    ) -> None:
        try:
            await asyncio.to_thread(self.client.post_receipt, receipt)
        except Exception as exc:
            result["coord_audit_error"] = str(exc)

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("protocol") != "cos-c2-iterm-edge-v1":
            return {"ok": False, "error": "unsupported edge protocol"}
        operation = request.get("op")
        disk_manifest_sha256 = self.disk_manifest_sha256()
        if operation in {"dispatch", "poke", "visual_action"} and (
            disk_manifest_sha256 != self.manifest_sha256
        ):
            return {
                "ok": False,
                "error": "edge manifest changed since load; reload required",
                "loaded_manifest_sha256": self.manifest_sha256,
                "disk_manifest_sha256": disk_manifest_sha256,
            }
        if operation == "dispatch":
            raw = request.get("envelope")
            if not isinstance(raw, dict):
                return {"ok": False, "error": "dispatch envelope must be an object"}
            envelope = DispatchEnvelope.from_dict(raw)
            transport = self.manifest.transport_for(envelope.assignment_id)
            worker = envelope.validate_for(self.manifest)
            if worker.role != "worker":
                raise ContractError("assignments may target only registered worker roles")
            if worker.iterm_session_id == self.manifest.controller_iterm_session_id:
                raise ContractError("self-dispatch is forbidden")
            if worker.tty == self.manifest.controller_tty:
                raise ContractError("self-dispatch tty is forbidden")
            async with self.dispatch_guard:
                if self.dispatch_receipts.has_idempotency_key(envelope.idempotency_key):
                    return {
                        "ok": False,
                        "error": f"duplicate dispatch idempotency key: {envelope.idempotency_key}",
                        "transport": transport,
                    }
                if envelope.idempotency_key in self.dispatch_inflight:
                    return {
                        "ok": False,
                        "error": f"dispatch already in flight: {envelope.idempotency_key}",
                        "transport": transport,
                        "in_flight": True,
                    }
                self.dispatch_inflight.add(envelope.idempotency_key)
            reservation_resource = (
                f"workspace:mikebook:c2-worker:{envelope.worker_id}"
            )
            reservation = None
            reservation_receipt = None
            try:
                try:
                    reservation = await asyncio.to_thread(
                        self.client.claim_resource,
                        reservation_resource,
                        ttl_seconds=300,
                        producer={
                            "kind": "c2-worker-reservation",
                            "assignment_id": envelope.assignment_id,
                            "task_id": envelope.task_id,
                            "attempt_id": envelope.attempt_id,
                            "controller_epoch": envelope.controller_epoch,
                            "worker_id": envelope.worker_id,
                        },
                        idempotency_key=f"c2-reserve:{envelope.idempotency_key}",
                    )
                except LeaseBlocked as exc:
                    result = {
                        "ok": False,
                        "error": str(exc),
                        "transport": transport,
                        "reservation": exc.payload,
                    }
                    receipt = self.rejection_receipt(
                        envelope=envelope,
                        transport=transport,
                        error=str(exc),
                        reservation=exc.payload,
                    )
                    self.dispatch_receipts.append(receipt)
                    result["receipt"] = receipt
                    await self.audit_receipt(result, receipt)
                    return result
                reservation_receipt = {
                    "resource": reservation.resource,
                    "epoch": reservation.epoch,
                    "expires_at": reservation.expires_at,
                }
                if transport == "headless":
                    result = await asyncio.to_thread(
                        dispatch_registered_headless,
                        manifest=self.manifest,
                        envelope=envelope,
                        verify_epoch=self.client.verify_live_epoch,
                        receipts=self.dispatch_receipts,
                        reservation=reservation_receipt,
                    )
                else:
                    result = await dispatch_registered(
                        self.connection,
                        manifest=self.manifest,
                        envelope=envelope,
                        verify_epoch=self.client.verify_live_epoch,
                        receipts=self.dispatch_receipts,
                        reservation=reservation_receipt,
                    )
                result["transport"] = transport
                if not result.get("ok"):
                    await asyncio.to_thread(self.client.release_resource, reservation)
                    reservation = None
                    if "receipt" not in result:
                        receipt = self.rejection_receipt(
                            envelope=envelope,
                            transport=transport,
                            error=str(result.get("error") or "dispatch failed"),
                            reservation=reservation_receipt,
                        )
                        self.dispatch_receipts.append(receipt)
                        result["receipt"] = receipt
                if isinstance(result.get("receipt"), dict):
                    await self.audit_receipt(result, result["receipt"])
                return result
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "transport": transport}
                if reservation is not None:
                    try:
                        await asyncio.to_thread(
                            self.client.release_resource, reservation
                        )
                        reservation = None
                    except Exception as release_exc:
                        result["reservation_release_error"] = str(release_exc)
                if not self.dispatch_receipts.has_idempotency_key(
                    envelope.idempotency_key
                ):
                    receipt = self.rejection_receipt(
                        envelope=envelope,
                        transport=transport,
                        error=str(exc),
                        reservation=reservation_receipt,
                    )
                    self.dispatch_receipts.append(receipt)
                    result["receipt"] = receipt
                    await self.audit_receipt(result, receipt)
                return result
            finally:
                async with self.dispatch_guard:
                    self.dispatch_inflight.discard(envelope.idempotency_key)
        if operation == "poke":
            epoch = request.get("controller_epoch")
            if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
                return {"ok": False, "error": "controller_epoch must be a positive integer"}
            key = str(request.get("idempotency_key") or "").strip()
            if not key:
                return {"ok": False, "error": "idempotency_key is required"}
            if self.poke_receipts.has_idempotency_key(key):
                return {"ok": False, "error": f"duplicate poke idempotency key: {key}"}
            async with self.dispatch_guard:
                if key in self.dispatch_inflight:
                    return {"ok": False, "error": f"poke already in flight: {key}", "in_flight": True}
                self.dispatch_inflight.add(key)
            try:
                result = await send_controller_poke(
                    self.connection,
                    manifest=self.manifest,
                    text=str(request.get("text") or ""),
                    controller_epoch=epoch,
                    idempotency_key=key,
                    verify_epoch=self.client.verify_live_epoch,
                )
                if result.get("injection_attempted"):
                    receipt = {**result, "receipt_version": 1}
                    self.poke_receipts.append(receipt)
                    try:
                        await asyncio.to_thread(self.client.post_receipt, receipt)
                    except Exception as exc:
                        result["coord_audit_error"] = str(exc)
                return result
            finally:
                async with self.dispatch_guard:
                    self.dispatch_inflight.discard(key)
        if operation == "visual_action":
            raw_observation = request.get("observation")
            raw_decision = request.get("decision")
            if not isinstance(raw_observation, dict):
                return {"ok": False, "error": "visual observation must be an object"}
            if not isinstance(raw_decision, dict):
                return {"ok": False, "error": "visual decision must be an object"}
            observation = VisualObservation.from_dict(raw_observation)
            decision = VisualDecision.from_dict(raw_decision)
            async with self.dispatch_guard:
                if self.dispatch_receipts.has_idempotency_key(decision.idempotency_key):
                    return {
                        "ok": False,
                        "error": f"duplicate visual action idempotency key: {decision.idempotency_key}",
                    }
                if decision.idempotency_key in self.dispatch_inflight:
                    return {
                        "ok": False,
                        "error": f"visual action already in flight: {decision.idempotency_key}",
                        "in_flight": True,
                    }
                self.dispatch_inflight.add(decision.idempotency_key)
            try:
                result = await execute_visual_decision(
                    self.connection,
                    manifest=self.manifest,
                    observation=observation,
                    decision=decision,
                    verify_epoch=self.client.verify_live_epoch,
                    receipts=self.dispatch_receipts,
                )
                if isinstance(result.get("receipt"), dict):
                    await self.audit_receipt(result, result["receipt"])
                return result
            finally:
                async with self.dispatch_guard:
                    self.dispatch_inflight.discard(decision.idempotency_key)
        if operation == "health":
            manifest_current = disk_manifest_sha256 == self.manifest_sha256
            return {
                "ok": manifest_current,
                "protocol": "cos-c2-iterm-edge-v1",
                "manifest_id": self.manifest.manifest_id,
                "manifest_sha256": self.manifest_sha256,
                "disk_manifest_sha256": disk_manifest_sha256,
                "pid": os.getpid(),
                **(
                    {}
                    if manifest_current
                    else {"error": "edge manifest changed since load; reload required"}
                ),
            }
        return {"ok": False, "error": f"unsupported edge operation: {operation!r}"}


async def serve(
    connection: Any,
    *,
    socket_path: Path,
    manifest_path: Path,
    state_dir: Path,
) -> None:
    lock_fd = acquire_edge_lock(socket_path)
    try:
        daemon = EdgeDaemon(
            connection, manifest_path=manifest_path, state_dir=state_dir
        )
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(socket_path.parent, 0o700)
        if socket_path.exists() or socket_path.is_symlink():
            socket_path.unlink()
    except Exception:
        os.close(lock_fd)
        raise

    async def on_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readline()
            if len(raw) > MAX_RESPONSE_BYTES:
                response = {"ok": False, "error": "edge request exceeds size limit"}
            else:
                try:
                    request = json.loads(raw.decode("utf-8"))
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                    response = await daemon.handle(request)
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
            writer.write(
                json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        server = await asyncio.start_unix_server(on_client, path=str(socket_path))
        os.chmod(socket_path, 0o600)
        async with server:
            await server.serve_forever()
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run fenced iTerm C2 edge daemon")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)
    try:
        import iterm2
    except ImportError:
        print(json.dumps({"ok": False, "error": "run inside iTerm2 Python runtime"}))
        return 2

    async def entry(connection: Any) -> None:
        await serve(
            connection,
            socket_path=args.socket,
            manifest_path=args.manifest,
            state_dir=args.state_dir,
        )

    iterm2.run_forever(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
