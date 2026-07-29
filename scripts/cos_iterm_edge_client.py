#!/usr/bin/env python3
"""Same-user client for the iTerm2 Python API C2 edge daemon."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


DEFAULT_SOCKET_PATH = Path.home() / ".cache" / "cos-c2" / "iterm-edge.sock"
MAX_RESPONSE_BYTES = 1_048_576


class EdgeClientError(RuntimeError):
    pass


def request_edge(
    request: dict[str, Any],
    *,
    socket_path: Path = DEFAULT_SOCKET_PATH,
    timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    payload = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_RESPONSE_BYTES:
        raise EdgeClientError("edge request exceeds size limit")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_seconds)
    try:
        client.connect(str(socket_path))
        client.sendall(payload)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise EdgeClientError("edge response exceeds size limit")
            if b"\n" in chunk:
                break
    except (OSError, TimeoutError) as exc:
        raise EdgeClientError(f"iTerm edge unavailable at {socket_path}: {exc}") from exc
    finally:
        client.close()
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EdgeClientError("iTerm edge returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise EdgeClientError("iTerm edge response must be an object")
    return response


def execute_visual_action(
    observation: dict[str, Any],
    decision: dict[str, Any],
    *,
    socket_path: Path = DEFAULT_SOCKET_PATH,
) -> dict[str, Any]:
    return request_edge(
        {
            "protocol": "cos-c2-iterm-edge-v1",
            "op": "visual_action",
            "observation": observation,
            "decision": decision,
        },
        socket_path=socket_path,
    )


def dispatch_envelope(
    envelope: dict[str, Any],
    *,
    socket_path: Path = DEFAULT_SOCKET_PATH,
    timeout_seconds: float = 1810.0,
) -> dict[str, Any]:
    """Allow the daemon's 1800-second bounded headless turn plus framing slack."""
    return request_edge(
        {"protocol": "cos-c2-iterm-edge-v1", "op": "dispatch", "envelope": envelope},
        socket_path=socket_path,
        timeout_seconds=timeout_seconds,
    )


def poke_controller(
    *,
    text: str,
    controller_epoch: int,
    idempotency_key: str,
    socket_path: Path = DEFAULT_SOCKET_PATH,
) -> dict[str, Any]:
    return request_edge(
        {
            "protocol": "cos-c2-iterm-edge-v1",
            "op": "poke",
            "text": text,
            "controller_epoch": controller_epoch,
            "idempotency_key": idempotency_key,
        },
        socket_path=socket_path,
    )
