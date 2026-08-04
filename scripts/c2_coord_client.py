#!/usr/bin/env python3
"""Small principal-bound coord-api client for the bootstrap C2 adapter."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from c2_contract import ContractError
from c2_runtime_hook import interrupt_challenge_binding_sha256


class CoordError(RuntimeError):
    pass


class LeaseBlocked(CoordError):
    def __init__(self, resource: str, payload: dict[str, Any]):
        super().__init__(f"lease {resource!r} held by {payload.get('current_holder') or 'unknown'}")
        self.resource = resource
        self.payload = payload


class LeaseRejected(CoordError):
    def __init__(self, resource: str, payload: dict[str, Any]):
        reason = str(payload.get("reason") or "lease_claim_rejected")
        detail = str(payload.get("detail") or payload.get("status") or "coord-api rejected claim")
        super().__init__(f"lease {resource!r} claim rejected: {reason}: {detail}")
        self.resource = resource
        self.payload = payload


class LeaseLost(CoordError):
    pass


RequestFn = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, Any]]
SHADOW_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
RUNTIME_PROOF_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
PENDING_RUNTIME_CHALLENGE_FIELDS = frozenset(
    {
        "challenge_id",
        "worker_id",
        "iterm_session_id",
        "cli_session_id",
        "coord_session_id",
        "controller_epoch",
        "worker_epoch",
        "binding_sha256",
        "armed_at",
        "expires_at",
        "runtime",
        "profile_id",
        "profile_version",
    }
)
BCA_CORRELATION_FIELDS = (
    "idempotency_key",
    "assignment_id",
    "task_id",
    "attempt_id",
    "worker_id",
    "session_id",
    "controller_epoch",
    "plan_id",
    "generation",
    "direction_digest",
    "payload_digest",
)
BCA_ROW_BINDING_FIELDS = (
    "idempotency_key",
    "assignment_id",
    "task_id",
    "attempt_id",
    "worker_id",
    "session_id",
    "controller_epoch",
    "payload_digest",
)
_BCA_TERMINAL_STATES = {"acknowledged", "refused", "dead_lettered"}


def _request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload
    except (OSError, urllib.error.URLError) as exc:
        raise CoordError(f"{method} {url} failed: {exc}") from exc


def _parse_expiry(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class CoordConfig:
    api_url: str
    read_token: str
    principal_token: str
    agent_id: str
    principal_id: str

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        expected_principal_id: str | None = None,
        secrets_path: Path | None = None,
    ) -> "CoordConfig":
        path = path or Path.home() / ".coordination" / "agent.json"
        secrets_path = secrets_path or Path.home() / ".secrets" / "env"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CoordError(f"coord config is unreadable: {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise CoordError("coord config root must be an object")
        file_env = _load_env_file(secrets_path)
        runtime = {**file_env, **os.environ}
        configured_agent = str(runtime.get("COORD_AGENT_ID") or value.get("agent_id") or "").strip()
        principal_id = str(
            expected_principal_id
            or runtime.get("COORD_PRINCIPAL_ID")
            or value.get("principal_id")
            or configured_agent
        ).strip()
        token_name = _principal_token_env_name(principal_id)
        principal_token = str(runtime.get(token_name) or "").strip()
        configured_principal = str(value.get("principal_id") or configured_agent).strip()
        if not principal_token and principal_id == configured_principal:
            principal_token = str(
                runtime.get("COORD_PRINCIPAL_TOKEN") or value.get("principal_token") or ""
            ).strip()
        config = cls(
            api_url=str(
                runtime.get("COORD_API_URL") or value.get("api_url") or "http://127.0.0.1:8800"
            ).rstrip("/"),
            read_token=str(runtime.get("COORD_API_KEY") or value.get("api_key") or "").strip(),
            principal_token=principal_token,
            agent_id=principal_id,
            principal_id=principal_id,
        )
        if not all(
            (
                config.api_url,
                config.read_token,
                config.principal_token,
                config.agent_id,
                config.principal_id,
            )
        ):
            raise CoordError("coord config lacks principal-bound read/write identity")
        return config


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        item = raw_value.strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in {"'", '"'}:
            item = item[1:-1]
        if key:
            values[key] = item
    return values


def _principal_token_env_name(principal_id: str) -> str:
    key = re.sub(r"[^A-Za-z0-9]+", "_", principal_id).strip("_").upper()
    return f"{key}_TOKEN" if key else ""


@dataclass(frozen=True)
class LeaseHandle:
    resource: str
    holder: str
    epoch: int
    expires_at: str | None
    lease: dict[str, Any]


class CoordClient:
    def __init__(
        self,
        config: CoordConfig,
        *,
        request: RequestFn = _request,
        timeout_seconds: float = 10.0,
    ):
        self.config = config
        self.request_fn = request
        self.timeout_seconds = timeout_seconds

    def _headers(self, *, write: bool, idempotency_key: str | None = None) -> dict[str, str]:
        token = self.config.principal_token if write else self.config.read_token
        actor = self.config.principal_id if write else self.config.agent_id
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": actor,
            "Content-Type": "application/json",
        }
        if write:
            headers["X-Principal-Id"] = self.config.principal_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        write: bool = False,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        allowed: tuple[int, ...] = (200,),
    ) -> tuple[int, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = self._headers(write=write, idempotency_key=idempotency_key)
        if extra_headers:
            headers.update(extra_headers)
        status, response = self.request_fn(
            method,
            f"{self.config.api_url}{path}",
            headers,
            body,
            self.timeout_seconds,
        )
        if status not in allowed:
            raise CoordError(f"{method} {path} -> HTTP {status}: {response}")
        return status, response

    @staticmethod
    def lease_path(resource: str, suffix: str = "") -> str:
        return f"/lease/{urllib.parse.quote(resource, safe='')}{suffix}"

    def claim_resource(
        self,
        resource: str,
        *,
        ttl_seconds: int,
        producer: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> LeaseHandle:
        status, response = self.call(
            "POST",
            self.lease_path(resource, "/claim"),
            payload={
                "holder": self.config.principal_id,
                "ttl_seconds": ttl_seconds,
                "force": False,
                "producer": producer,
            },
            write=True,
            idempotency_key=idempotency_key,
            allowed=(200, 409),
        )
        payload = response if isinstance(response, dict) else {}
        if status == 409:
            current_holder = str(payload.get("current_holder") or "")
            if current_holder and current_holder != self.config.principal_id:
                raise LeaseBlocked(resource, payload)
            raise LeaseRejected(resource, payload)
        lease = payload.get("lease")
        if isinstance(lease, dict) and "producer" not in lease:
            lease = {**lease, "producer": producer}
        return self._handle(resource, lease)

    @staticmethod
    def _expected_controller_instance(handle: LeaseHandle) -> dict[str, Any] | None:
        producer = handle.lease.get("producer")
        if not isinstance(producer, dict) or producer.get("kind") != "c2-supervisor":
            return None
        expected = dict(producer)
        expected.pop("kind", None)
        return expected

    def renew_resource(self, handle: LeaseHandle) -> LeaseHandle:
        request_payload: dict[str, Any] = {"holder": self.config.principal_id}
        expected = self._expected_controller_instance(handle)
        if expected is not None:
            request_payload["expected_controller_instance"] = expected
            request_payload["expected_epoch"] = handle.epoch
        status, response = self.call(
            "POST",
            self.lease_path(handle.resource, "/renew"),
            payload=request_payload,
            write=True,
            allowed=(200, 409),
        )
        payload = response if isinstance(response, dict) else {}
        if status == 409:
            raise LeaseLost(
                f"lease {handle.resource!r} lost to {payload.get('current_holder') or 'unknown'}"
            )
        lease = payload.get("lease")
        if (
            isinstance(lease, dict)
            and "producer" not in lease
            and isinstance(handle.lease.get("producer"), dict)
        ):
            lease = {**lease, "producer": handle.lease["producer"]}
        renewed = self._handle(handle.resource, lease)
        if renewed.epoch != handle.epoch:
            message = (
                f"lease epoch changed during renew: expected={handle.epoch} "
                f"observed={renewed.epoch}"
            )
            raise LeaseLost(message)
        return renewed

    def get_resource(self, resource: str) -> dict[str, Any] | None:
        status, payload = self.call("GET", self.lease_path(resource), allowed=(200, 404))
        if status == 404:
            return None
        return payload if isinstance(payload, dict) else None

    def verify_live_epoch(self, resource: str, expected_epoch: int) -> dict[str, Any]:
        lease = self.get_resource(resource)
        if not lease:
            raise LeaseLost(f"lease {resource!r} is absent")
        holder = str(lease.get("actual_holder") or lease.get("holder") or "")
        epoch = lease.get("epoch")
        expiry = _parse_expiry(lease.get("expires_at"))
        if holder != self.config.principal_id:
            message = (
                f"lease {resource!r} holder mismatch: "
                f"expected={self.config.principal_id} observed={holder}"
            )
            raise LeaseLost(message)
        if epoch != expected_epoch:
            raise LeaseLost(
                f"lease {resource!r} epoch mismatch: expected={expected_epoch} observed={epoch}"
            )
        if expiry is None:
            raise LeaseLost(f"lease {resource!r} has missing or invalid expiry")
        if expiry <= datetime.now(timezone.utc):
            raise LeaseLost(f"lease {resource!r} expired at {expiry.isoformat()}")
        return lease

    def release_resource(self, handle: LeaseHandle) -> bool:
        request_payload: dict[str, Any] = {
            "holder": self.config.principal_id,
            "expected_epoch": handle.epoch,
        }
        expected = self._expected_controller_instance(handle)
        if expected is not None:
            request_payload["expected_controller_instance"] = expected
        status, _payload = self.call(
            "DELETE",
            self.lease_path(handle.resource),
            payload=request_payload,
            write=True,
            allowed=(200, 404, 409),
        )
        if status == 409:
            raise LeaseLost(f"refusing to release successor epoch for {handle.resource!r}")
        return status == 200

    def actionable(self, agent_id: str | None = None) -> dict[str, Any]:
        _status, payload = self.call(
            "GET",
            f"/agents/{urllib.parse.quote(agent_id or self.config.agent_id, safe='')}/actionable",
        )
        return payload if isinstance(payload, dict) else {"items": []}

    def task(self, task_id: str) -> dict[str, Any]:
        _status, payload = self.call("GET", f"/tasks/{urllib.parse.quote(task_id, safe='')}")
        return payload if isinstance(payload, dict) else {}

    def post_claim_request(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Ask the registered worker session to perform its own task claim.

        The controller never impersonates the worker principal.  The worker's
        watcher consumes this durable instruction and calls the exact-session
        task claim endpoint; the controller subsequently verifies the task
        readback before creating an attempt or reserving delivery.
        """
        assignment_id = str(envelope.get("assignment_id") or "").strip()
        task_id = str(envelope.get("task_id") or "").strip()
        worker_id = str(envelope.get("worker_id") or "").strip()
        if not assignment_id or not task_id or not worker_id:
            raise CoordError("claim request requires assignment, task, and worker identities")
        content = json.dumps(
            {"schema": "cos.claim-request.v1", **envelope},
            sort_keys=True,
            separators=(",", ":"),
        )
        external_id = f"cos-claim:{assignment_id}"
        _status, payload = self.call(
            "POST",
            "/messages",
            payload={
                "from_agent": self.config.principal_id,
                "to_agent": worker_id,
                "msg_type": "instruction",
                "subject": "COS claim request",
                "content": content,
                "provenance_source": "cos",
                "external_id": external_id,
                "correlation_id": task_id,
                "intent": "cos-claim-request",
                "required_ack": False,
            },
            write=True,
            idempotency_key=external_id,
            allowed=(200, 201),
        )
        return payload if isinstance(payload, dict) else {}

    def read_claim(self, *, task_id: str, worker_id: str, session_id: str) -> dict[str, Any]:
        task = self.task(task_id)
        if not task:
            raise CoordError(f"task not found after claim request: {task_id}")
        if task.get("claimed_by") != worker_id or task.get("claimed_by_session") != session_id:
            raise CoordError("worker claim readback does not match the requested principal/session")
        if task.get("status") != "in_progress":
            raise CoordError("worker claim readback is not in_progress")
        return task

    def wait_for_claim(
        self,
        *,
        task_id: str,
        worker_id: str,
        session_id: str,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_error: CoordError | None = None
        while time.monotonic() <= deadline:
            try:
                return self.read_claim(task_id=task_id, worker_id=worker_id, session_id=session_id)
            except CoordError as exc:
                last_error = exc
                time.sleep(poll_seconds)
        raise CoordError(f"worker claim readback timed out: {last_error}") from last_error

    def claim_task(
        self,
        task_id: str,
        *,
        session_id: str,
        session_capability: str,
        envelope_ref: str | None = None,
        execution_host: str | None = None,
        runtime_hint: str | None = None,
    ) -> dict[str, Any]:
        """Perform the worker-side exact-session claim handshake."""
        if not session_id.strip() or not session_capability.strip():
            raise CoordError("worker claim requires session id and capability")
        payload = {
            key: value
            for key, value in {
                "session_id": session_id,
                "execution_host": execution_host,
                "runtime_hint": runtime_hint,
                "envelope_ref": envelope_ref,
            }.items()
            if value
        }
        _status, response = self.call(
            "POST",
            f"/tasks/{urllib.parse.quote(task_id, safe='')}/claim",
            payload=payload,
            write=True,
            idempotency_key=f"claim:{task_id}:{session_id}",
            extra_headers={
                "X-Session-Id": session_id,
                "X-Session-Capability": session_capability,
            },
            allowed=(200, 409),
        )
        return response if isinstance(response, dict) else {}

    def reserve_bca(
        self, envelope: dict[str, Any], *, expires_at: float | None = None
    ) -> dict[str, Any]:
        payload = {
            "assignment_id": envelope["assignment_id"],
            "task_id": envelope["task_id"],
            "attempt_id": envelope["attempt_id"],
            "worker_id": envelope["worker_id"],
            "session_id": envelope["coord_session_id"],
            "controller_epoch": envelope["controller_epoch"],
            "plan_id": envelope["plan_id"],
            "generation": envelope["generation"],
            "direction_digest": envelope["direction_digest"],
            "idempotency_key": envelope["idempotency_key"],
            "payload_digest": hashlib.sha256(
                json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "capability_ref": f"session:{envelope['coord_session_id']}",
            "expires_at": expires_at if expires_at is not None else time.time() + 1800,
            "max_attempts": 2,
        }
        _status, response = self.call(
            "POST",
            "/c2/bca-delivery/shadow/dispatches",
            payload=payload,
            write=True,
            idempotency_key=str(payload["idempotency_key"]),
            allowed=(201,),
        )
        return response if isinstance(response, dict) else {}

    def bca_correlation_tuple(self, envelope: dict[str, Any]) -> dict[str, Any]:
        correlation = {field: envelope[field] for field in BCA_CORRELATION_FIELDS}
        if not SHA256_RE.fullmatch(str(correlation["direction_digest"])):
            raise CoordError("BCA direction_digest must be a SHA-256 hex digest")
        if not SHA256_RE.fullmatch(str(correlation["payload_digest"])):
            raise CoordError("BCA payload_digest must be a SHA-256 hex digest")
        return correlation

    def read_bca(self, idempotency_key: str) -> dict[str, Any]:
        _status, response = self.call(
            "GET",
            f"/c2/bca-delivery/shadow/dispatches/{urllib.parse.quote(idempotency_key, safe='')}",
        )
        return response if isinstance(response, dict) else {}

    def verify_bca_readback(
        self,
        readback: dict[str, Any],
        *,
        expected_correlation: dict[str, Any],
    ) -> dict[str, Any]:
        events = readback.get("events") if isinstance(readback, dict) else None
        if not isinstance(events, list) or not events:
            raise CoordError("BCA delivery readback returned no events")
        reserved = next(
            (
                event
                for event in events
                if isinstance(event, dict) and event.get("event_type") == "reserved"
            ),
            None,
        )
        if not isinstance(reserved, dict):
            raise CoordError("BCA delivery readback lacks reservation event")
        reserved_payload = reserved.get("event_payload")
        if not isinstance(reserved_payload, dict):
            raise CoordError("BCA delivery reservation payload is malformed")
        correlated_chain = "correlation" in reserved_payload
        if correlated_chain and reserved_payload.get("correlation") != expected_correlation:
            raise CoordError("BCA reservation correlation does not match the dispatched envelope")
        for field in BCA_ROW_BINDING_FIELDS:
            if reserved.get(field) != expected_correlation[field]:
                raise CoordError(f"BCA reservation readback mismatch: {field}")
        terminal_events: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                raise CoordError("BCA delivery readback contains a malformed event")
            payload = event.get("event_payload")
            if not isinstance(payload, dict):
                raise CoordError("BCA delivery event payload is malformed")
            state = payload.get("delivery_state")
            if event.get("event_type") != "reserved":
                if correlated_chain:
                    if payload.get("correlation") != expected_correlation:
                        raise CoordError(
                            "BCA delivery chain mixed or mismatched correlation tuples"
                        )
                elif "correlation" in payload:
                    raise CoordError("legacy BCA delivery chain unexpectedly gained correlation")
            if state in _BCA_TERMINAL_STATES:
                for field in BCA_ROW_BINDING_FIELDS:
                    if event.get(field) != expected_correlation[field]:
                        raise CoordError(f"BCA terminal readback mismatch: {field}")
                terminal_events.append(event)
        if not terminal_events:
            raise CoordError("BCA delivery readback has no terminal worker receipt")
        if len(terminal_events) > 1:
            raise CoordError("BCA delivery readback has multiple terminal worker receipts")
        return terminal_events[0]

    def wait_for_bca_terminal_receipt(
        self,
        idempotency_key: str,
        *,
        expected_correlation: dict[str, Any],
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.5,
    ) -> dict[str, Any]:
        readback = self.wait_for_bca_terminal(
            idempotency_key,
            expected_correlation=expected_correlation,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
        return self.verify_bca_readback(readback, expected_correlation=expected_correlation)

    def wait_for_bca_terminal(
        self,
        idempotency_key: str,
        *,
        expected_correlation: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last = {}
        strict_binding = expected_correlation is not None and all(
            field in expected_correlation for field in BCA_CORRELATION_FIELDS
        )
        while time.monotonic() <= deadline:
            last = self.read_bca(idempotency_key)
            if strict_binding:
                try:
                    terminal = self.verify_bca_readback(
                        last,
                        expected_correlation=expected_correlation,
                    )
                except CoordError as exc:
                    if "no terminal worker receipt" not in str(exc):
                        raise
                else:
                    if (
                        terminal.get("event_payload", {}).get("delivery_state")
                        in _BCA_TERMINAL_STATES
                    ):
                        return last
            else:
                events = last.get("events") if isinstance(last, dict) else []
                if any(
                    isinstance(event, dict)
                    and (event.get("event_payload") or {}).get("delivery_state")
                    in _BCA_TERMINAL_STATES
                    for event in (events or [])
                ):
                    return last
            time.sleep(poll_seconds)
        raise CoordError("BCA delivery readback timed out before a worker receipt")

    def post_bca_receipt(
        self,
        idempotency_key: str,
        *,
        outcome: str,
        attempt_number: int,
        worker_id: str,
        session_id: str,
        payload_digest: str,
        session_capability: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Worker-originated receipt path; caller must be the reserved worker."""
        if self.config.principal_id != worker_id:
            raise CoordError("BCA receipt must be submitted by the reserved worker principal")
        payload = {
            "outcome": outcome,
            "attempt_number": attempt_number,
            "worker_id": worker_id,
            "session_id": session_id,
            "payload_digest": payload_digest,
            "reason": reason,
        }
        _status, response = self.call(
            "POST",
            (
                "/c2/bca-delivery/shadow/dispatches/"
                f"{urllib.parse.quote(idempotency_key, safe='')}/receipts"
            ),
            payload=payload,
            write=True,
            idempotency_key=f"{idempotency_key}:receipt:{attempt_number}",
            extra_headers={
                "X-Session-Id": session_id,
                "X-Session-Capability": session_capability,
            },
            allowed=(200,),
        )
        return response if isinstance(response, dict) else {}

    def post_transport_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """Append-only controller transport evidence, distinct from BCA worker receipt."""
        return self.post_receipt(receipt)

    def message(self, message_id: int) -> dict[str, Any]:
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 1:
            raise CoordError("coord message id must be a positive integer")
        _status, payload = self.call("GET", f"/messages/{message_id}")
        return payload if isinstance(payload, dict) else {}

    def post_direction(self, direction: dict[str, Any]) -> dict[str, Any]:
        """Persist one versioned COS direction through the existing message API."""
        if not isinstance(direction, dict) or direction.get("schema") != "cos.direction.v1":
            raise CoordError("direction must be a cos.direction.v1 object")
        plan_id = str(direction.get("plan_id") or "").strip()
        generation = direction.get("generation")
        direction_id = str(direction.get("direction_id") or "").strip()
        if (
            not plan_id
            or not direction_id
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise CoordError("direction requires plan_id, direction_id, and positive generation")
        external_id = f"cos-direction:{plan_id}:{generation}"
        content = json.dumps(direction, sort_keys=True, separators=(",", ":"))
        _status, payload = self.call(
            "POST",
            "/messages",
            payload={
                "from_agent": self.config.principal_id,
                "to_agent": self.config.principal_id,
                "msg_type": "instruction",
                "subject": "COS direction",
                "content": content,
                "provenance_source": "cos",
                "external_id": external_id,
                "correlation_id": plan_id,
                "intent": "cos-direction",
                "required_ack": False,
            },
            write=True,
            idempotency_key=external_id,
            allowed=(200, 201),
        )
        response = payload if isinstance(payload, dict) else {}
        if response.get("external_id") != external_id:
            raise CoordError("coord-api direction response lost external_id")
        return response

    def directions(self, plan_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not plan_id.strip():
            raise CoordError("plan_id is required")
        _status, payload = self.call(
            "GET",
            "/messages?"
            + urllib.parse.urlencode(
                {"correlation_id": plan_id, "msg_type": "instruction", "limit": limit}
            ),
        )
        return (
            [dict(item) for item in payload if isinstance(item, dict)]
            if isinstance(payload, list)
            else []
        )

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        status, payload = self.call(
            "GET",
            f"/attempts/{urllib.parse.quote(attempt_id, safe='')}",
            allowed=(200, 404),
        )
        if status == 404:
            return None
        return payload if isinstance(payload, dict) else {}

    def ensure_attempt(
        self, *, attempt_id: str, task_id: str, session_id: str, context: str | None = None
    ) -> dict[str, Any]:
        existing = self.get_attempt(attempt_id)
        if existing is not None:
            if existing.get("task_id") != task_id or existing.get("session_id") != session_id:
                raise CoordError("attempt id is already bound to a different task/session")
            return existing
        _status, payload = self.call(
            "POST",
            "/attempts",
            payload={
                "attempt_id": attempt_id,
                "task_id": task_id,
                "session_id": session_id,
                **({"context": context} if context is not None else {}),
            },
            write=True,
            idempotency_key=f"attempt:{attempt_id}",
            allowed=(201, 409),
        )
        if isinstance(payload, dict) and payload.get("attempt_id"):
            return payload
        existing = self.get_attempt(attempt_id)
        if existing is None:
            raise CoordError("coord-api did not return or persist the attempt")
        return existing

    def end_attempt(
        self,
        attempt_id: str,
        *,
        outcome: str,
        error: str | None = None,
        files_changed: list[str] | None = None,
    ) -> dict[str, Any]:
        _status, payload = self.call(
            "PATCH",
            f"/attempts/{urllib.parse.quote(attempt_id, safe='')}/end",
            payload={"outcome": outcome, "error": error, "files_changed": files_changed or []},
            write=True,
            idempotency_key=f"attempt-end:{attempt_id}:{outcome}",
            allowed=(200, 409),
        )
        return payload if isinstance(payload, dict) else {}

    def verify_receipt_readback(self, receipt: dict[str, Any], message_id: int) -> dict[str, Any]:
        expected_content = json.dumps(
            {"c2_dispatch_receipt": receipt}, sort_keys=True, separators=(",", ":")
        )
        message = self.message(message_id)
        expected = {
            "id": message_id,
            "from_agent": self.config.principal_id,
            "to_agent": self.config.principal_id,
            "msg_type": "activity",
            "content": expected_content,
        }
        for field, value in expected.items():
            if message.get(field) != value:
                raise CoordError(f"coord receipt readback mismatch: {field}")
        if message.get("accepted") is not True or message.get("acknowledged_by") != "coord-api":
            raise CoordError("coord receipt readback lacks server acceptance")
        return message

    def post_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        content = json.dumps(
            {"c2_dispatch_receipt": receipt}, sort_keys=True, separators=(",", ":")
        )
        _status, payload = self.call(
            "POST",
            "/messages",
            payload={
                "from_agent": self.config.principal_id,
                "to_agent": self.config.principal_id,
                "msg_type": "activity",
                "content": content,
                "provenance_source": "dispatch",
            },
            write=True,
            idempotency_key=str(receipt.get("idempotency_key") or ""),
            allowed=(200, 201),
        )
        response = payload if isinstance(payload, dict) else {}
        message_id = response.get("id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 1:
            raise CoordError("coord receipt POST returned no durable message id")
        return self.verify_receipt_readback(receipt, message_id)

    def create_runtime_interrupt_challenge(self, request: dict[str, Any]) -> dict[str, Any]:
        idempotency_key = str(request.get("idempotency_key") or "")
        if not idempotency_key:
            raise CoordError("runtime interrupt challenge requires idempotency_key")
        _status, payload = self.call(
            "POST",
            "/c2/runtime-observations/challenges",
            payload=request,
            write=True,
            idempotency_key=idempotency_key,
            allowed=(200, 201),
        )
        challenge = payload.get("challenge") if isinstance(payload, dict) else None
        if not isinstance(challenge, dict):
            raise CoordError("coord broker returned no runtime interrupt challenge")
        challenge_id = challenge.get("challenge_id")
        issued_at = challenge.get("issued_at")
        if not isinstance(challenge_id, str) or not challenge_id:
            raise CoordError("coord broker returned invalid runtime challenge identity")
        if (
            isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
            or not math.isfinite(float(issued_at))
        ):
            raise CoordError("coord broker returned invalid runtime challenge timestamp")
        try:
            expected_binding = interrupt_challenge_binding_sha256(request)
        except ContractError as exc:
            raise CoordError(str(exc)) from exc
        if challenge.get("binding_sha256") != expected_binding:
            raise CoordError("coord broker returned a differently bound runtime challenge")
        return challenge

    def arm_runtime_interrupt_challenge(self, request: dict[str, Any]) -> dict[str, Any]:
        challenge_id = str(request.get("challenge_id") or "")
        idempotency_key = str(request.get("idempotency_key") or "")
        binding_sha256 = str(request.get("binding_sha256") or "")
        if not challenge_id or not idempotency_key or not SHA256_RE.fullmatch(binding_sha256):
            raise CoordError(
                "arming runtime challenge requires challenge, binding, and idempotency identities"
            )
        _status, payload = self.call(
            "POST",
            "/c2/runtime-observations/challenges/"
            + urllib.parse.quote(challenge_id, safe="")
            + "/arm",
            payload=request,
            write=True,
            idempotency_key=idempotency_key,
            allowed=(200,),
        )
        challenge = payload.get("challenge") if isinstance(payload, dict) else None
        if (
            not isinstance(challenge, dict)
            or challenge.get("challenge_id") != challenge_id
            or challenge.get("armed") is not True
            or challenge.get("binding_sha256") != binding_sha256
        ):
            raise CoordError("coord broker did not arm the exact runtime challenge")
        return challenge

    def verify_runtime_observation(self, report: dict[str, Any]) -> dict[str, Any]:
        _status, payload = self.call(
            "POST",
            "/c2/runtime-observations/verify",
            payload={"observation": report},
            write=True,
            allowed=(200,),
        )
        verification = payload.get("verification") if isinstance(payload, dict) else None
        if not isinstance(verification, dict):
            raise CoordError("coord broker returned no runtime observation verification")
        return verification

    def pending_runtime_observation_challenges(self, *, limit: int = 16) -> list[dict[str, Any]]:
        """Read only this enrolled observer principal's redacted work feed."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise CoordError("runtime observation pending limit must be between 1 and 64")
        _status, payload = self.call(
            "GET",
            f"/c2/runtime-observations/challenges/pending?limit={limit}",
            write=True,
        )
        challenges = payload.get("challenges") if isinstance(payload, dict) else None
        if not isinstance(challenges, list):
            raise CoordError("coord broker returned no pending runtime challenge list")
        result: list[dict[str, Any]] = []
        for raw in challenges:
            if not isinstance(raw, dict) or set(raw) != PENDING_RUNTIME_CHALLENGE_FIELDS:
                raise CoordError("coord broker returned malformed pending runtime challenge")
            challenge_id = raw.get("challenge_id")
            try:
                canonical_id = str(uuid.UUID(str(challenge_id)))
            except (ValueError, TypeError, AttributeError) as exc:
                raise CoordError(
                    "coord broker returned invalid pending challenge identity"
                ) from exc
            if challenge_id != canonical_id:
                raise CoordError("coord broker returned non-canonical pending challenge identity")
            if not SHA256_RE.fullmatch(str(raw.get("binding_sha256") or "")):
                raise CoordError("coord broker returned invalid pending challenge binding")
            for field in ("controller_epoch", "worker_epoch", "profile_version"):
                value = raw.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise CoordError(f"coord broker returned invalid pending {field}")
            for field in (
                "worker_id",
                "iterm_session_id",
                "cli_session_id",
                "coord_session_id",
                "runtime",
                "profile_id",
                "armed_at",
                "expires_at",
            ):
                if not isinstance(raw.get(field), str) or not str(raw[field]).strip():
                    raise CoordError(f"coord broker returned invalid pending {field}")
            result.append(dict(raw))
        return result

    def publish_runtime_observation(
        self,
        report: dict[str, Any],
        *,
        expected_binding_sha256: str,
    ) -> dict[str, Any]:
        """Publish an opaque signed report and require exact durable readback coordinates."""
        challenge_id = str(report.get("challenge_id") or "")
        signature = report.get("signature")
        if not challenge_id or not RUNTIME_PROOF_RE.fullmatch(str(signature or "")):
            raise CoordError("runtime observation report lacks challenge identity or opaque proof")
        if not SHA256_RE.fullmatch(expected_binding_sha256):
            raise CoordError("runtime observation expected binding is invalid")
        try:
            canonical = dict(report)
            canonical.pop("signature")
            expected_digest = hashlib.sha256(
                json.dumps(
                    canonical,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
        except (TypeError, ValueError) as exc:
            raise CoordError("runtime observation report is not canonical JSON") from exc
        try:
            _status, payload = self.call(
                "POST",
                "/c2/runtime-observations",
                payload=report,
                write=True,
                allowed=(200, 201),
            )
        except CoordError:
            # A broker error body must never become a path for reflecting the proof token.
            raise CoordError("runtime observation publication failed") from None
        observation = payload.get("observation") if isinstance(payload, dict) else None
        expected = {
            "observation_digest": expected_digest,
            "challenge_id": challenge_id,
            "challenge_binding_sha256": expected_binding_sha256,
            "observer_principal": self.config.principal_id,
        }
        if not isinstance(observation, dict) or observation != expected:
            raise CoordError("coord broker returned mismatched runtime observation coordinates")
        return observation

    def post_message_delivery_shadow_run(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not SHADOW_RUN_ID_RE.fullmatch(run_id):
            raise CoordError("coord shadow run_id has invalid syntax")
        _status, payload = self.call(
            "POST",
            "/message-delivery-shadow/runs",
            payload=run,
            write=True,
            idempotency_key=run_id,
            allowed=(200, 201),
        )
        response = payload if isinstance(payload, dict) else {}
        item = response.get("item")
        if not isinstance(item, dict) or item.get("run_id") != run.get("run_id"):
            raise CoordError("coord shadow-run POST returned no matching durable item")
        return item

    def message_delivery_shadow_run(self, run_id: str) -> dict[str, Any]:
        if not SHADOW_RUN_ID_RE.fullmatch(run_id):
            raise CoordError("coord shadow run_id has invalid syntax")
        _status, payload = self.call(
            "GET",
            "/message-delivery-shadow/runs/" + urllib.parse.quote(run_id, safe=""),
            write=True,
        )
        response = payload if isinstance(payload, dict) else {}
        item = response.get("item")
        if not isinstance(item, dict) or item.get("run_id") != run_id:
            raise CoordError("coord shadow-run readback returned no matching item")
        return item

    def _handle(self, resource: str, raw: object) -> LeaseHandle:
        lease = raw if isinstance(raw, dict) else {}
        epoch = lease.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise CoordError(f"coord-api returned invalid lease epoch for {resource!r}: {epoch!r}")
        holder = str(lease.get("holder") or "")
        if holder != self.config.principal_id:
            raise CoordError(f"coord-api returned lease for unexpected holder: {holder!r}")
        return LeaseHandle(
            resource=resource,
            holder=holder,
            epoch=epoch,
            expires_at=str(lease.get("expires_at")) if lease.get("expires_at") else None,
            lease=lease,
        )
