from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_iterm_edge_client as edge  # noqa: E402


def test_visual_action_client_uses_bounded_protocol(monkeypatch):
    captured = {}

    def fake_request(request, **kwargs):
        captured.update(request)
        return {"ok": True}

    monkeypatch.setattr(edge, "request_edge", fake_request)
    result = edge.execute_visual_action(
        {"worker_id": "worker"},
        {"action": "press_enter"},
    )

    assert result == {"ok": True}
    assert captured == {
        "protocol": "cos-c2-iterm-edge-v1",
        "op": "visual_action",
        "observation": {"worker_id": "worker"},
        "decision": {"action": "press_enter"},
    }


def test_unix_socket_edge_protocol_round_trip(tmp_path):
    socket_path = Path("/private/tmp") / f"cos-edge-test-{os.getpid()}.sock"
    socket_path.unlink(missing_ok=True)
    ready = threading.Event()

    def server():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(socket_path))
        sock.listen(1)
        ready.set()
        conn, _ = sock.accept()
        request = json.loads(conn.recv(65536).decode("utf-8"))
        conn.sendall(
            json.dumps({"ok": True, "op": request["op"]}).encode("utf-8") + b"\n"
        )
        conn.close()
        sock.close()

    thread = threading.Thread(target=server)
    thread.start()
    assert ready.wait(timeout=2)

    result = edge.poke_controller(
        text="/goal wake",
        controller_epoch=7,
        idempotency_key="poke-1",
        socket_path=socket_path,
    )
    thread.join(timeout=2)
    socket_path.unlink(missing_ok=True)

    assert result == {"ok": True, "op": "poke"}


def test_dispatch_client_timeout_covers_bounded_headless_turn(monkeypatch):
    seen = {}

    def fake_request(_payload, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(edge, "request_edge", fake_request)
    edge.dispatch_envelope({"assignment_id": "a-1"})
    assert seen["timeout_seconds"] > 1800
