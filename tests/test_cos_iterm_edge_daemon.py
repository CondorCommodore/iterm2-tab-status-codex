from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import c2_contract as c2  # noqa: E402
import cos_iterm_edge_daemon as edge_daemon  # noqa: E402
from c2_coord_client import LeaseHandle  # noqa: E402


def manifest(*, controller_visible: bool = True) -> c2.RunManifest:
    controller = {
        "controller_id": "cos",
        "host": "macbook",
        "runtime": "codex",
        "iterm_session_id": "iterm-cos",
        "tty": "/dev/ttys001",
        "cli_session_id": "cli-cos",
        "coord_session_id": "coord-cos",
        "coord_agent_id": "mikebook_codex",
    }
    if not controller_visible:
        controller.pop("iterm_session_id")
        controller.pop("tty")
    return c2.RunManifest.from_dict(
        {
            "manifest_id": "edge-test-v1",
            "controller": controller,
            "workers": [
                {
                    "worker_id": "worker-codex",
                    "host": "macbook",
                    "runtime": "codex",
                    "iterm_session_id": "iterm-worker",
                    "tty": "/dev/ttys003",
                    "cli_session_id": "cli-worker",
                    "coord_session_id": "coord-worker",
                    "coord_agent_id": "mikebook_codex",
                    "repositories": ["Condor/repo"],
                }
            ],
            "plan_paths": ["/plans/master.md"],
            "permitted_repositories": ["Condor/repo"],
            "permitted_actions": ["inspect", "test"],
            "dispatch_transport": "headless",
            "recovery_transport": "ab",
        }
    )


def envelope() -> dict[str, object]:
    return {
        "assignment_id": "assignment-1",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "worker_id": "worker-codex",
        "cli_session_id": "cli-worker",
        "coord_session_id": "coord-worker",
        "objective": "implement the bounded slice",
        "repo": "Condor/repo",
        "worktree": "/tmp/worktree",
        "scope": ["src/a.py"],
        "acceptance_tests": ["pytest tests/test_a.py"],
        "stopping_condition": "PR merged or durable blocker",
        "report_destination": "coord-api task task-1",
        "authorization_limits": ["no deploy"],
        "permitted_actions": ["inspect", "test"],
        "controller_epoch": 7,
        "idempotency_key": "dispatch-task-1-attempt-1",
    }


def visual_observation() -> dict[str, object]:
    return {
        "worker_id": "worker-codex",
        "iterm_session_id": "iterm-worker",
        "screenshot_sha256": "a" * 64,
        "captured_ts": 1_800_000_000.0,
        "summary": "Interactive choice blocks the worker",
        "controller_epoch": 7,
        "worker_epoch": 13,
    }


def visual_decision() -> dict[str, object]:
    observation = edge_daemon.VisualObservation.from_dict(visual_observation())
    return {
        "observation_digest": observation.digest(),
        "action": "press_enter",
        "text": "",
        "rationale": "Continue the selected bounded option",
        "decided_by": "llm:test-supervisor",
        "idempotency_key": "visual-edge-1",
    }


class FakeCoordClient:
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    def claim_resource(self, resource, **kwargs):
        self.events.append(("claim", resource))
        return LeaseHandle(
            resource=resource,
            holder="mikebook_codex",
            epoch=13,
            expires_at="2030-01-01T00:00:00Z",
            lease={},
        )

    def verify_live_epoch(self, resource, epoch):
        self.events.append(("verify", (resource, epoch)))

    def post_receipt(self, receipt):
        self.events.append(("post", receipt))

    def release_resource(self, handle):
        self.events.append(("release", handle.resource))


def make_daemon(tmp_path) -> edge_daemon.EdgeDaemon:
    daemon = object.__new__(edge_daemon.EdgeDaemon)
    daemon.connection = object()
    daemon.manifest = manifest()
    daemon.manifest_path = None
    daemon.manifest_sha256 = "a" * 64
    daemon.client = FakeCoordClient()
    daemon.dispatch_receipts = c2.ReceiptStore(tmp_path / "dispatch.jsonl")
    daemon.poke_receipts = c2.ReceiptStore(tmp_path / "poke.jsonl")
    daemon.dispatch_inflight = set()
    daemon.dispatch_guard = asyncio.Lock()
    daemon.target_locks = {}
    return daemon


def manifest_with_colliding_worker(field: str) -> c2.RunManifest:
    base = manifest(controller_visible=False)
    worker = replace(
        base.workers[0],
        **{field: getattr(base, f"controller_{field}")},
    )
    return replace(base, workers=(worker,))


def test_health_reports_loaded_manifest_digest_and_process_identity(tmp_path):
    daemon = make_daemon(tmp_path)

    result = asyncio.run(daemon.handle({"protocol": "cos-c2-iterm-edge-v1", "op": "health"}))

    assert result["ok"] is True
    assert result["manifest_id"] == "edge-test-v1"
    assert result["manifest_sha256"] == "a" * 64
    assert result["disk_manifest_sha256"] == "a" * 64
    assert result["pid"] == os.getpid()


def test_manifest_drift_fails_closed_before_any_terminal_operation(tmp_path):
    daemon = make_daemon(tmp_path)
    manifest_path = tmp_path / "live-manifest.json"
    manifest_path.write_text("changed\n", encoding="utf-8")
    daemon.manifest_path = manifest_path

    for operation in ("dispatch", "poke", "visual_action"):
        result = asyncio.run(daemon.handle({"protocol": "cos-c2-iterm-edge-v1", "op": operation}))
        assert result["ok"] is False
        assert "reload required" in result["error"]
    health = asyncio.run(daemon.handle({"protocol": "cos-c2-iterm-edge-v1", "op": "health"}))
    assert health["ok"] is False
    assert health["disk_manifest_sha256"] != health["manifest_sha256"]


def test_edge_socket_lock_rejects_second_authority(tmp_path):
    socket_path = tmp_path / "edge.sock"
    first = edge_daemon.acquire_edge_lock(socket_path)
    try:
        with pytest.raises(RuntimeError, match="another iTerm edge owns"):
            edge_daemon.acquire_edge_lock(socket_path)
    finally:
        os.close(first)
    successor = edge_daemon.acquire_edge_lock(socket_path)
    os.close(successor)


def test_dispatch_reserves_worker_before_headless_transport(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)

    def fake_dispatch(**kwargs):
        assert daemon.client.events[0] == (
            "claim",
            "workspace:mikebook:c2-worker:worker-codex",
        )
        receipt = {
            "idempotency_key": kwargs["envelope"].idempotency_key,
            "reservation": kwargs["reservation"],
        }
        kwargs["receipts"].append(receipt)
        return {"ok": True, "receipt": receipt}

    monkeypatch.setattr(edge_daemon, "dispatch_registered_headless", fake_dispatch)
    result = asyncio.run(
        daemon.handle(
            {
                "protocol": "cos-c2-iterm-edge-v1",
                "op": "dispatch",
                "envelope": envelope(),
            }
        )
    )

    assert result["ok"] is True
    assert result["transport"] == "headless"
    assert result["receipt"]["reservation"] == {
        "resource": "workspace:mikebook:c2-worker:worker-codex",
        "epoch": 13,
        "expires_at": "2030-01-01T00:00:00Z",
    }
    assert daemon.client.events[-1][0] == "post"
    assert not any(event[0] == "release" for event in daemon.client.events)


def test_failed_dispatch_releases_worker_reservation(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)
    monkeypatch.setattr(
        edge_daemon,
        "dispatch_registered_headless",
        lambda **kwargs: {"ok": False, "error": "provider failure"},
    )

    result = asyncio.run(
        daemon.handle(
            {
                "protocol": "cos-c2-iterm-edge-v1",
                "op": "dispatch",
                "envelope": envelope(),
            }
        )
    )

    assert result["ok"] is False
    assert ("release", "workspace:mikebook:c2-worker:worker-codex") in daemon.client.events
    assert result["receipt"]["submit_method"] == "headless-rejected-before-injection"
    assert result["receipt"]["observed_ack"] is False
    assert daemon.client.events[-1] == (
        "post",
        result["receipt"],
    )


def test_headless_controller_manifest_still_dispatches_workers(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)
    daemon.manifest = manifest(controller_visible=False)

    def fake_dispatch(**kwargs):
        receipt = {
            "idempotency_key": kwargs["envelope"].idempotency_key,
            "reservation": kwargs["reservation"],
        }
        kwargs["receipts"].append(receipt)
        return {"ok": True, "receipt": receipt}

    monkeypatch.setattr(edge_daemon, "dispatch_registered_headless", fake_dispatch)
    result = asyncio.run(
        daemon.handle(
            {
                "protocol": "cos-c2-iterm-edge-v1",
                "op": "dispatch",
                "envelope": envelope(),
            }
        )
    )

    assert result["ok"] is True
    assert result["transport"] == "headless"


def test_dispatch_exception_releases_worker_reservation(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)

    def fail_after_claim(**_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(edge_daemon, "dispatch_registered_headless", fail_after_claim)
    result = asyncio.run(
        daemon.handle(
            {
                "protocol": "cos-c2-iterm-edge-v1",
                "op": "dispatch",
                "envelope": envelope(),
            }
        )
    )

    assert result["ok"] is False
    assert "provider exploded" in result["error"]
    assert (
        "release",
        "workspace:mikebook:c2-worker:worker-codex",
    ) in daemon.client.events
    assert result["receipt"]["observed_ack"] is False


@pytest.mark.parametrize("field", ["cli_session_id", "coord_session_id"])
def test_headless_dispatch_rejects_controller_session_identity_collision(field, tmp_path):
    daemon = make_daemon(tmp_path)
    daemon.manifest = manifest_with_colliding_worker(field)
    request = {
        "protocol": "cos-c2-iterm-edge-v1",
        "op": "dispatch",
        "envelope": {
            **envelope(),
            field: getattr(daemon.manifest, f"controller_{field}"),
        },
    }

    with pytest.raises(c2.ContractError, match="must not also be registered as a worker"):
        asyncio.run(
            daemon.handle(request)
        )


def test_invalid_envelope_is_rejected_before_worker_claim(tmp_path):
    daemon = make_daemon(tmp_path)
    invalid = envelope()
    invalid["repo"] = "outside/manifest"

    with pytest.raises(c2.ContractError, match="outside run manifest"):
        asyncio.run(
            daemon.handle(
                {
                    "protocol": "cos-c2-iterm-edge-v1",
                    "op": "dispatch",
                    "envelope": invalid,
                }
            )
        )
    assert not any(event[0] == "claim" for event in daemon.client.events)


def test_concurrent_duplicate_does_not_poison_winning_receipt(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)
    started = threading.Event()
    finish = threading.Event()

    def blocked_dispatch(**kwargs):
        started.set()
        assert finish.wait(timeout=2)
        receipt = {"idempotency_key": kwargs["envelope"].idempotency_key}
        kwargs["receipts"].append(receipt)
        return {"ok": True, "receipt": receipt}

    monkeypatch.setattr(edge_daemon, "dispatch_registered_headless", blocked_dispatch)

    async def scenario():
        request = {
            "protocol": "cos-c2-iterm-edge-v1",
            "op": "dispatch",
            "envelope": envelope(),
        }
        winner_task = asyncio.create_task(daemon.handle(request))
        assert await asyncio.to_thread(started.wait, 1)
        duplicate = await daemon.handle(request)
        finish.set()
        winner = await winner_task
        return winner, duplicate

    winner, duplicate = asyncio.run(scenario())

    assert winner["ok"] is True
    assert duplicate["ok"] is False
    assert duplicate["in_flight"] is True
    assert "receipt" not in duplicate
    assert len(daemon.dispatch_receipts.records()) == 1
    assert [event[0] for event in daemon.client.events].count("claim") == 1


def test_visual_action_is_parsed_executed_and_audited(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)

    async def fake_execute(_connection, **kwargs):
        assert kwargs["observation"].worker_epoch == 13
        assert kwargs["decision"].decided_by == "llm:test-supervisor"
        receipt = {"idempotency_key": kwargs["decision"].idempotency_key}
        kwargs["receipts"].append(receipt)
        return {"ok": True, "receipt": receipt}

    monkeypatch.setattr(edge_daemon, "execute_visual_decision", fake_execute)
    result = asyncio.run(
        daemon.handle(
            {
                "protocol": "cos-c2-iterm-edge-v1",
                "op": "visual_action",
                "observation": visual_observation(),
                "decision": visual_decision(),
            }
        )
    )

    assert result["ok"] is True
    assert daemon.client.events[-1] == ("post", result["receipt"])

    duplicate = asyncio.run(
        daemon.handle(
            {
                "protocol": "cos-c2-iterm-edge-v1",
                "op": "visual_action",
                "observation": visual_observation(),
                "decision": visual_decision(),
            }
        )
    )
    assert duplicate["ok"] is False
    assert "duplicate visual action" in duplicate["error"]


def test_concurrent_duplicate_visual_action_only_injects_once(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_execute(_connection, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"ok": True, "receipt": {"idempotency_key": kwargs["decision"].idempotency_key}}

    monkeypatch.setattr(edge_daemon, "execute_visual_decision", fake_execute)
    request = {
        "protocol": "cos-c2-iterm-edge-v1",
        "op": "visual_action",
        "observation": visual_observation(),
        "decision": visual_decision(),
    }

    async def exercise():
        first = asyncio.create_task(daemon.handle(request))
        await started.wait()
        duplicate = await daemon.handle(request)
        release.set()
        return await first, duplicate

    first, duplicate = asyncio.run(exercise())
    assert first["ok"] is True
    assert duplicate["ok"] is False
    assert duplicate["in_flight"] is True
    assert calls == 1


def test_distinct_visual_actions_serialize_per_target_session(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0

    async def fake_execute(_connection, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if kwargs["decision"].idempotency_key == "visual-edge-1":
            first_started.set()
            await release_first.wait()
        active -= 1
        return {"ok": False, "error": "verification pending"}

    monkeypatch.setattr(edge_daemon, "execute_visual_decision", fake_execute)
    first_request = {
        "protocol": "cos-c2-iterm-edge-v1",
        "op": "visual_action",
        "observation": visual_observation(),
        "decision": visual_decision(),
    }
    second_request = {
        **first_request,
        "decision": {**visual_decision(), "idempotency_key": "visual-2"},
    }

    async def exercise():
        first = asyncio.create_task(daemon.handle(first_request))
        await first_started.wait()
        second = asyncio.create_task(daemon.handle(second_request))
        await asyncio.sleep(0)
        assert max_active == 1
        release_first.set()
        await asyncio.gather(first, second)

    asyncio.run(exercise())
    assert max_active == 1


def test_concurrent_duplicate_poke_only_injects_once(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_poke(_connection, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"ok": True, "idempotency_key": kwargs["idempotency_key"]}

    monkeypatch.setattr(edge_daemon, "send_controller_poke", fake_poke)
    request = {
        "protocol": "cos-c2-iterm-edge-v1",
        "op": "poke",
        "controller_epoch": 7,
        "idempotency_key": "poke-once",
        "text": "/goal continue",
    }

    async def exercise():
        first = asyncio.create_task(daemon.handle(request))
        await started.wait()
        duplicate = await daemon.handle(request)
        release.set()
        return await first, duplicate

    first, duplicate = asyncio.run(exercise())
    assert first["ok"] is True
    assert duplicate["ok"] is False
    assert duplicate["in_flight"] is True
    assert calls == 1


def test_unacknowledged_injected_poke_is_persisted_for_idempotency(monkeypatch, tmp_path):
    daemon = make_daemon(tmp_path)

    async def fake_poke(_connection, **kwargs):
        return {
            "ok": False,
            "injection_attempted": True,
            "observed_ack": False,
            "idempotency_key": kwargs["idempotency_key"],
            "error": "not acknowledged",
        }

    monkeypatch.setattr(edge_daemon, "send_controller_poke", fake_poke)
    request = {
        "protocol": "cos-c2-iterm-edge-v1",
        "op": "poke",
        "controller_epoch": 7,
        "idempotency_key": "poke-failed",
        "text": "/goal continue",
    }
    first = asyncio.run(daemon.handle(request))
    duplicate = asyncio.run(daemon.handle(request))
    assert first["ok"] is False
    assert duplicate["ok"] is False
    assert "duplicate poke" in duplicate["error"]
    assert len(daemon.poke_receipts.records()) == 1
