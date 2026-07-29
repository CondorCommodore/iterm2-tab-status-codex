#!/usr/bin/env python3
"""Small principal-bound coord-api client for the bootstrap C2 adapter."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class CoordError(RuntimeError):
    pass


class LeaseBlocked(CoordError):
    def __init__(self, resource: str, payload: dict[str, Any]):
        super().__init__(
            f"lease {resource!r} held by {payload.get('current_holder') or 'unknown'}"
        )
        self.resource = resource
        self.payload = payload


class LeaseLost(CoordError):
    pass


RequestFn = Callable[
    [str, str, dict[str, str], bytes | None, float], tuple[int, Any]
]


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
        configured_agent = str(
            runtime.get("COORD_AGENT_ID") or value.get("agent_id") or ""
        ).strip()
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
                runtime.get("COORD_PRINCIPAL_TOKEN")
                or value.get("principal_token")
                or ""
            ).strip()
        config = cls(
            api_url=str(
                runtime.get("COORD_API_URL")
                or value.get("api_url")
                or "http://127.0.0.1:8800"
            ).rstrip("/"),
            read_token=str(
                runtime.get("COORD_API_KEY") or value.get("api_key") or ""
            ).strip(),
            principal_token=principal_token,
            agent_id=principal_id,
            principal_id=principal_id,
        )
        if not all(
            (config.api_url, config.read_token, config.principal_token, config.agent_id, config.principal_id)
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
        allowed: tuple[int, ...] = (200,),
    ) -> tuple[int, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        status, response = self.request_fn(
            method,
            f"{self.config.api_url}{path}",
            self._headers(write=write, idempotency_key=idempotency_key),
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
            raise LeaseBlocked(resource, payload)
        return self._handle(resource, payload.get("lease"))

    def renew_resource(self, handle: LeaseHandle) -> LeaseHandle:
        status, response = self.call(
            "POST",
            self.lease_path(handle.resource, "/renew"),
            payload={"holder": self.config.principal_id},
            write=True,
            allowed=(200, 409),
        )
        payload = response if isinstance(response, dict) else {}
        if status == 409:
            raise LeaseLost(
                f"lease {handle.resource!r} lost to {payload.get('current_holder') or 'unknown'}"
            )
        renewed = self._handle(handle.resource, payload.get("lease"))
        if renewed.epoch != handle.epoch:
            raise LeaseLost(
                f"lease epoch changed during renew: expected={handle.epoch} observed={renewed.epoch}"
            )
        return renewed

    def get_resource(self, resource: str) -> dict[str, Any] | None:
        status, payload = self.call(
            "GET", self.lease_path(resource), allowed=(200, 404)
        )
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
            raise LeaseLost(
                f"lease {resource!r} holder mismatch: expected={self.config.principal_id} observed={holder}"
            )
        if epoch != expected_epoch:
            raise LeaseLost(
                f"lease {resource!r} epoch mismatch: expected={expected_epoch} observed={epoch}"
            )
        if expiry is not None and expiry <= datetime.now(timezone.utc):
            raise LeaseLost(f"lease {resource!r} expired at {expiry.isoformat()}")
        return lease

    def release_resource(self, handle: LeaseHandle) -> bool:
        status, _payload = self.call(
            "DELETE",
            self.lease_path(handle.resource),
            payload={
                "holder": self.config.principal_id,
                "expected_epoch": handle.epoch,
            },
            write=True,
            allowed=(200, 404, 409),
        )
        if status == 409:
            raise LeaseLost(f"refusing to release successor epoch for {handle.resource!r}")
        return status == 200

    def actionable(self, agent_id: str | None = None) -> dict[str, Any]:
        _status, payload = self.call(
            "GET", f"/agents/{urllib.parse.quote(agent_id or self.config.agent_id, safe='')}/actionable"
        )
        return payload if isinstance(payload, dict) else {"items": []}

    def task(self, task_id: str) -> dict[str, Any]:
        _status, payload = self.call(
            "GET", f"/tasks/{urllib.parse.quote(task_id, safe='')}"
        )
        return payload if isinstance(payload, dict) else {}

    def post_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        content = json.dumps({"c2_dispatch_receipt": receipt}, sort_keys=True, separators=(",", ":"))
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
        return payload if isinstance(payload, dict) else {}

    def _handle(self, resource: str, raw: object) -> LeaseHandle:
        lease = raw if isinstance(raw, dict) else {}
        epoch = lease.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise CoordError(f"coord-api returned invalid lease epoch for {resource!r}: {epoch!r}")
        holder = str(lease.get("holder") or "")
        if holder != self.config.principal_id:
            raise CoordError(
                f"coord-api returned lease for unexpected holder: {holder!r}"
            )
        return LeaseHandle(
            resource=resource,
            holder=holder,
            epoch=epoch,
            expires_at=str(lease.get("expires_at")) if lease.get("expires_at") else None,
            lease=lease,
        )
