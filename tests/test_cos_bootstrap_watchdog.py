from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_bootstrap_watchdog as watchdog  # noqa: E402
import cos_current_actions as current_actions  # noqa: E402
from c2_contract import load_manifest  # noqa: E402
from c2_coord_client import CoordError  # noqa: E402


def write_manifest(path: Path, *, recovery="ab", controller_visible: bool = True):
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
    path.write_text(
        json.dumps(
            {
                "manifest_id": "test",
                "controller": controller,
                "workers": [
                    {
                        "worker_id": "worker",
                        "host": "macbook",
                        "runtime": "codex",
                        "iterm_session_id": "iterm-worker",
                        "tty": "/dev/ttys003",
                        "cli_session_id": "cli-worker",
                        "coord_session_id": "coord-worker",
                        "coord_agent_id": "mikebook_codex",
                    }
                ],
                "plan_paths": ["/plan"],
                "permitted_repositories": ["Condor/repo"],
                "permitted_actions": ["inspect"],
                "recovery_transport": recovery,
            }
        ),
        encoding="utf-8",
    )


class Config:
    principal_id = "mikebook_codex"


class Client:
    config = Config()

    def __init__(self, lease, *, durable_readback=True, post_error=False):
        self.lease = lease
        self.durable_readback = durable_readback
        self.post_error = post_error

    def get_resource(self, resource):
        return self.lease

    def verify_receipt_readback(self, receipt, message_id):
        if not self.durable_readback:
            raise CoordError("durable readback unavailable")
        return {"id": message_id, "accepted": True}

    def verify_live_epoch(self, resource, epoch):
        if not isinstance(self.lease, dict) or self.lease.get("epoch") != epoch:
            raise CoordError("live epoch unavailable")
        return self.lease

    def post_receipt(self, receipt):
        if self.post_error:
            raise CoordError("coord receipt unavailable")
        return {"id": 45, "accepted": True}


class CountingClient(Client):
    def __init__(self, lease):
        super().__init__(lease)
        self.calls = 0

    def get_resource(self, resource):
        self.calls += 1
        return super().get_resource(resource)


def arm_stale(state_dir: Path):
    (state_dir / "ARMED").write_text("armed\n", encoding="utf-8")
    (state_dir / "supervisor-state.json").write_text(
        json.dumps({"mode": "bootstrap-authoritative"}), encoding="utf-8"
    )
    (state_dir / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 1}), encoding="utf-8"
    )


def write_headless_authority(state_dir: Path, *, recorded_ts=501, epoch=8):
    (state_dir / "supervisor-heartbeat.json").write_text(
        json.dumps(
            {
                "recorded_ts": recorded_ts,
                "authority": True,
                "ownership": "headless",
                "controller_epoch": epoch,
            }
        ),
        encoding="utf-8",
    )


def publish_headless_checkpoint(state_dir: Path, manifest_path: Path, *, coord_accept: bool = True):
    m = load_manifest(manifest_path)
    path = state_dir / "current-actions.txt"
    seeded = current_actions.seed_actions(
        manifest=m,
        path=path,
        decision_digest="a" * 64,
        epoch=8,
        now_ts=501,
    )
    rebound = current_actions.rebind_actions(
        current=seeded,
        path=path,
        manifest=m,
        decision_digest="a" * 64,
        epoch=8,
        ownership="headless",
        now_ts=502,
    )
    source = state_dir / "headless-next-actions.txt"
    header = {
        **rebound.header,
        "generation": rebound.generation + 1,
        "previous_action_digest": rebound.digest,
        "written_at": "1970-01-01T00:08:23Z",
        "next_check_at": "1970-01-01T00:13:23Z",
    }
    source.write_text(
        f"--- {current_actions.SCHEMA}\n"
        f"{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
        f"---\n{rebound.body}\n",
        encoding="utf-8",
    )
    checkpoint = current_actions.checkpoint_actions(
        source=source,
        destination=path,
        manifest=m,
        live_epoch=8,
        receipts_path=state_dir / "action-receipts.jsonl",
        expected_decision_digest="a" * 64,
    )
    if coord_accept:
        current_actions.record_coord_acceptance(
            checkpoint_receipt=checkpoint,
            coord_response={"id": 42, "accepted": True},
            receipts_path=state_dir / "action-receipts.jsonl",
        )


def test_unarmed_watchdog_is_inert_without_coord_provider(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    factory_calls = []

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        client_factory=lambda: factory_calls.append(True),
        now_ts=500,
    )

    assert result == {"ok": True, "armed": False, "action": "inert"}
    assert factory_calls == []


def test_edge_health_rejects_loaded_manifest_drift(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"manifest_id":"current"}\n', encoding="utf-8")
    monkeypatch.setattr(
        watchdog,
        "request_edge",
        lambda *_args, **_kwargs: {
            "ok": True,
            "manifest_id": "current",
            "manifest_sha256": "0" * 64,
        },
    )

    result = watchdog.edge_health(manifest)

    assert result["ok"] is False
    assert "does not match" in result["error"]
    assert result["observed_manifest_sha256"] == "0" * 64
    assert result["expected_manifest_sha256"] != "0" * 64


def test_edge_health_accepts_exact_loaded_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"manifest_id":"current"}\n', encoding="utf-8")
    digest = watchdog.hashlib.sha256(manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(
        watchdog,
        "request_edge",
        lambda *_args, **_kwargs: {
            "ok": True,
            "manifest_id": "current",
            "manifest_sha256": digest,
        },
    )

    assert watchdog.edge_health(manifest)["ok"] is True


def test_fresh_heartbeat_is_healthy_without_coord_provider(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        client_factory=lambda: (_ for _ in ()).throw(CoordError("offline")),
        now_ts=500,
    )

    assert result["action"] == "healthy"
    assert result["heartbeat_age"] == 10


def test_transient_edge_failure_recovers_without_restart(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )
    restarts = []

    degraded = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": False, "error": "socket timeout"},
        edge_restart_fn=lambda: restarts.append(True) or {"ok": True},
        now_ts=500,
    )
    healthy = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": True},
        edge_restart_fn=lambda: restarts.append(True) or {"ok": True},
        now_ts=501,
    )

    assert degraded["action"] == "edge-health-degraded"
    assert healthy["action"] == "healthy"
    assert restarts == []
    state = json.loads((tmp_path / "watchdog-state.json").read_text())
    assert state["edge_health_failures"] == 0


def test_two_edge_health_failures_trigger_bounded_restart_receipt(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )
    restarts = []

    first = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": False, "error": "hung"},
        edge_restart_fn=lambda: restarts.append(True) or {"ok": True},
        now_ts=500,
    )
    second = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": False, "error": "hung"},
        edge_restart_fn=lambda: restarts.append(True) or {"ok": True},
        now_ts=501,
    )

    assert first["action"] == "edge-health-degraded"
    assert second["action"] == "edge-restarted"
    assert second["receipt"]["health_failures"] == 2
    assert restarts == [True]
    receipts = (tmp_path / "edge-recovery-receipts.jsonl").read_text()
    assert '"kind":"edge-recovery"' in receipts


def test_edge_restart_targets_only_registered_launchagent(monkeypatch):
    seen = []
    monkeypatch.setattr(watchdog.os, "getuid", lambda: 501)

    result = watchdog.restart_edge(
        run=lambda command, **kwargs: (
            seen.append((command, kwargs)) or subprocess.CompletedProcess(command, 0, "", "")
        )
    )

    assert result["ok"] is True
    assert seen[0][0] == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/com.local.cos-iterm-edge",
    ]
    assert seen[0][1]["timeout"] == 10


def test_persistent_edge_restart_backoff_is_exponential_capped_and_health_reset(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )
    now_ts = 500
    observed = []

    for _ in range(6):
        (tmp_path / "supervisor-heartbeat.json").write_text(
            json.dumps({"recorded_ts": now_ts - 10}), encoding="utf-8"
        )
        degraded = watchdog.run_once(
            manifest_path=manifest,
            state_dir=tmp_path,
            client=None,
            edge_health_fn=lambda: {"ok": False, "error": "still hung"},
            edge_restart_fn=lambda: {"ok": True},
            now_ts=now_ts,
        )
        assert degraded["action"] == "edge-health-degraded"
        restarted = watchdog.run_once(
            manifest_path=manifest,
            state_dir=tmp_path,
            client=None,
            edge_health_fn=lambda: {"ok": False, "error": "still hung"},
            edge_restart_fn=lambda: {"ok": True},
            now_ts=now_ts + 1,
        )
        assert restarted["action"] == "edge-restarted"
        backoff = restarted["receipt"]["backoff_seconds"]
        observed.append(backoff)
        now_ts += 1 + backoff

    assert observed == [60, 120, 240, 480, 900, 900]
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": now_ts}), encoding="utf-8"
    )
    healthy = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": True},
        edge_restart_fn=lambda: {"ok": True},
        now_ts=now_ts,
    )
    assert healthy["action"] == "healthy"
    state = json.loads((tmp_path / "watchdog-state.json").read_text())
    assert state["edge_restart_attempts"] == 0
    assert state["edge_restart_backoff_until"] is None


def test_stale_heartbeat_checks_lease_even_when_edge_is_unhealthy(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="tab")
    arm_stale(tmp_path)
    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        edge_health_fn=lambda: {"ok": False, "error": "edge unavailable"},
        edge_restart_fn=lambda: {"ok": False, "error": "restart failed"},
        now_ts=500,
    )
    assert result["action"] == "awaiting-visible-lease-expiry-for-headless-trial"
    assert result["live_epoch"] == 7


def test_headless_controller_never_triggers_tab_poke(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="tab", controller_visible=False)
    arm_stale(tmp_path)
    seen = []

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        edge_health_fn=lambda: {"ok": True},
        edge_restart_fn=lambda: {"ok": True},
        now_ts=500,
        poke_fn=lambda **kwargs: seen.append(kwargs) or {"ok": True},
    )

    assert result["action"] == "awaiting-visible-lease-expiry-for-headless-trial"
    assert seen == []


def test_edge_backoff_keeps_probing_without_restarting(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )
    restarts = []
    kwargs = {
        "manifest_path": manifest,
        "state_dir": tmp_path,
        "client": None,
        "edge_health_fn": lambda: {"ok": False, "error": "hung"},
        "edge_restart_fn": lambda: restarts.append(True) or {"ok": True},
    }
    watchdog.run_once(**kwargs, now_ts=500)
    watchdog.run_once(**kwargs, now_ts=501)
    watchdog.run_once(**kwargs, now_ts=510)
    backed_off = watchdog.run_once(**kwargs, now_ts=511)

    assert backed_off["action"] == "edge-restart-backoff-health-only"
    assert backed_off["backoff_seconds"] == 50
    assert restarts == [True]


def test_tab_recovery_uses_iterm_api_edge_poke(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="tab")
    arm_stale(tmp_path)
    seen = []

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=500,
        poke_fn=lambda **kwargs: (
            seen.append(kwargs) or {"ok": True, "submit_method": "iterm2-python-api-crlf"}
        ),
    )

    assert result["action"] == "tab-poke"
    assert seen[0]["controller_epoch"] == 7


def test_failed_tab_poke_does_not_create_pending_recovery(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="tab")
    arm_stale(tmp_path)
    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=500,
        poke_fn=lambda **_kwargs: {"ok": False, "error": "no acknowledgment"},
    )
    state = json.loads((tmp_path / "watchdog-state.json").read_text())
    assert result["ok"] is False
    assert state["pending_since"] is None
    assert state["pending_key"] is None
    assert state["pending_transport"] is None


def test_headless_trial_waits_for_epoch_expiry_then_resumes_same_uuid(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)
    commands = []

    def resume(command, **kwargs):
        commands.append(command)
        write_headless_authority(tmp_path)
        publish_headless_checkpoint(tmp_path, manifest)
        return subprocess.CompletedProcess(command, 0, "done", "")

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client(None),
        now_ts=500,
        run=resume,
    )

    assert result["action"] == "headless-resume"
    assert result["receipt"]["success"] is True
    assert result["receipt"]["headless_turn_success"] is True
    assert result["receipt"]["recovery_state"] == "bounded-turn-complete"
    assert commands[0][:4] == ["codex", "exec", "resume", "cli-cos"]
    assert result["receipt"]["visible_reattach_required"] is False
    assert result["receipt"]["authority_acquired"] is True
    assert result["receipt"]["controller_epoch"] == 8


def test_headless_recovery_does_not_block_on_visible_reattach(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)

    def resume(command, **kwargs):
        write_headless_authority(tmp_path)
        publish_headless_checkpoint(tmp_path, manifest)
        return subprocess.CompletedProcess(command, 0, "done", "")

    watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client(None),
        now_ts=500,
        run=resume,
    )
    pending = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client(None),
        now_ts=502,
    )
    assert pending["action"] == "healthy"
    assert pending["ok"] is True


def test_headless_local_acceptance_marker_without_coord_readback_is_not_success(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)

    def resume(command, **_kwargs):
        write_headless_authority(tmp_path)
        publish_headless_checkpoint(tmp_path, manifest)
        return subprocess.CompletedProcess(command, 0, "done", "")

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client(None, durable_readback=False),
        now_ts=500,
        run=resume,
    )

    assert result["receipt"]["success"] is False
    assert result["receipt"]["model_checkpoint_published"] is True
    assert result["receipt"]["model_checkpoint_durable"] is False


def prepare_action_loop(tmp_path, *, now_ts=100, next_decision=None):
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, recovery="headless")
    m = load_manifest(manifest_path)
    actions = current_actions.seed_actions(
        manifest=m,
        path=tmp_path / "current-actions.txt",
        decision_digest="a" * 64,
        epoch=7,
        now_ts=now_ts,
    )
    (tmp_path / "ARMED").write_text("armed\n", encoding="utf-8")
    (tmp_path / "supervisor-state.json").write_text(
        json.dumps({"mode": "bootstrap-authoritative"}), encoding="utf-8"
    )
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )
    (tmp_path / "decision-current.json").write_text(
        json.dumps(
            {
                "decision_digest": next_decision or "a" * 64,
                "wake_required": True,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, actions


def test_fresh_process_heartbeat_does_not_hide_unacknowledged_action(tmp_path):
    manifest, actions = prepare_action_loop(tmp_path)
    seen = []

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=500,
        poke_fn=lambda **kwargs: (
            seen.append(kwargs) or {"ok": False, "injection_attempted": True, "observed_ack": False}
        ),
    )

    assert result["action"] == "action-wake"
    assert result["ok"] is True
    assert result["awaiting_model_ack"] is True
    assert str(tmp_path / "current-actions.txt") in seen[0]["text"]
    assert actions.digest in seen[0]["text"]


def test_edge_false_negative_then_model_ack_does_not_duplicate(tmp_path):
    manifest, actions = prepare_action_loop(tmp_path)
    seen = []
    kwargs = {
        "manifest_path": manifest,
        "state_dir": tmp_path,
        "client": Client({"holder": "mikebook_codex", "epoch": 7}),
        "poke_fn": lambda **call: (
            seen.append(call) or {"ok": False, "injection_attempted": True, "observed_ack": False}
        ),
    }
    watchdog.run_once(**kwargs, now_ts=500)
    waiting = watchdog.run_once(**kwargs, now_ts=501)
    assert waiting["action"] == "awaiting-action-ack"
    ack = current_actions.acknowledge_actions(
        actions_path=tmp_path / "current-actions.txt",
        receipts_path=tmp_path / "action-receipts.jsonl",
        manifest=load_manifest(manifest),
        digest=actions.digest,
        generation=actions.generation,
        epoch=7,
        ownership="visible",
    )
    current_actions.commit_action_ack(
        ack_receipt=ack,
        coord_response={"id": 43, "accepted": True},
        progress_path=tmp_path / "action-progress.json",
        receipts_path=tmp_path / "action-receipts.jsonl",
    )
    acknowledged = watchdog.run_once(**kwargs, now_ts=503)
    assert acknowledged["action"] == "action-acknowledged"
    assert len(seen) == 1


def test_local_only_action_progress_cannot_acknowledge_wake(tmp_path):
    manifest, actions = prepare_action_loop(tmp_path)
    client = Client({"holder": "mikebook_codex", "epoch": 7})
    kwargs = {
        "manifest_path": manifest,
        "state_dir": tmp_path,
        "client": client,
        "poke_fn": lambda **_call: {"ok": True, "injection_attempted": True},
    }
    watchdog.run_once(**kwargs, now_ts=500)
    (tmp_path / "action-progress.json").write_text(
        json.dumps(
            {
                "kind": "action-ack",
                "action_digest": actions.digest,
                "generation": actions.generation,
                "controller_epoch": 7,
                "coord_accepted_ts": 502,
                "coord_message_id": 999,
            }
        ),
        encoding="utf-8",
    )

    result = watchdog.run_once(**kwargs, now_ts=503)

    assert result["action"] == "awaiting-action-ack"


def test_pending_action_ack_fails_closed_after_epoch_loss(tmp_path):
    manifest, _actions = prepare_action_loop(tmp_path)
    client = Client({"holder": "mikebook_codex", "epoch": 7})
    kwargs = {
        "manifest_path": manifest,
        "state_dir": tmp_path,
        "client": client,
        "poke_fn": lambda **_call: {"ok": True, "injection_attempted": True},
    }
    watchdog.run_once(**kwargs, now_ts=500)
    client.lease = None

    result = watchdog.run_once(**kwargs, now_ts=501)

    assert result["ok"] is False
    assert result["action"] == "action-ack-epoch-lost"


def test_two_expired_model_ack_windows_request_epoch_yield(tmp_path):
    manifest, _actions = prepare_action_loop(tmp_path)
    kwargs = {
        "manifest_path": manifest,
        "state_dir": tmp_path,
        "client": Client({"holder": "mikebook_codex", "epoch": 7}),
        "poke_fn": lambda **_call: {"ok": True, "injection_attempted": True},
    }
    first = watchdog.run_once(**kwargs, now_ts=500)
    second = watchdog.run_once(**kwargs, now_ts=591)
    yielded = watchdog.run_once(**kwargs, now_ts=682)

    assert [first["action"], second["action"], yielded["action"]] == [
        "action-wake",
        "action-wake",
        "yield-requested",
    ]
    hold = json.loads((tmp_path / "recovery-hold.json").read_text())
    assert hold["controller_epoch"] == 7


def test_changed_decision_wakes_before_action_deadline(tmp_path):
    manifest, _actions = prepare_action_loop(tmp_path, next_decision="b" * 64)
    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=200,
        poke_fn=lambda **_call: {"ok": True, "injection_attempted": True},
    )
    assert result["action"] == "action-wake"
    assert result["wake_reason"] == "deterministic decision changed"


def test_published_successor_checkpoint_waits_for_its_declared_deadline(tmp_path):
    manifest_path, first = prepare_action_loop(tmp_path)
    m = load_manifest(manifest_path)
    source = tmp_path / "next-actions.txt"
    source.write_bytes(first.raw)
    parsed = current_actions.parse_actions(source, manifest=m)
    header = {
        **parsed.header,
        "generation": 2,
        "previous_action_digest": first.digest,
        "written_at": "1970-01-01T00:03:20Z",
        "next_check_at": "1970-01-01T00:08:20Z",
    }
    source.write_text(
        f"--- {current_actions.SCHEMA}\n"
        f"{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
        f"---\n{parsed.body}\n",
        encoding="utf-8",
    )
    checkpoint = current_actions.checkpoint_actions(
        source=source,
        destination=tmp_path / "current-actions.txt",
        manifest=m,
        live_epoch=7,
        receipts_path=tmp_path / "action-receipts.jsonl",
    )
    current_actions.record_coord_acceptance(
        checkpoint_receipt=checkpoint,
        coord_response={"id": 44, "accepted": True},
        receipts_path=tmp_path / "action-receipts.jsonl",
    )

    result = watchdog.run_once(
        manifest_path=manifest_path,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=250,
    )
    assert result["action"] == "healthy"


def test_watchdog_retries_local_only_checkpoint_receipt_to_durable_readback(tmp_path):
    manifest_path, first = prepare_action_loop(tmp_path)
    m = load_manifest(manifest_path)
    source = tmp_path / "next-actions.txt"
    source.write_bytes(first.raw)
    parsed = current_actions.parse_actions(source, manifest=m)
    header = {
        **parsed.header,
        "generation": 2,
        "previous_action_digest": first.digest,
        "written_at": "1970-01-01T00:03:20Z",
        "next_check_at": "1970-01-01T00:08:20Z",
    }
    source.write_text(
        f"--- {current_actions.SCHEMA}\n"
        f"{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
        f"---\n{parsed.body}\n",
        encoding="utf-8",
    )
    current_actions.checkpoint_actions(
        source=source,
        destination=tmp_path / "current-actions.txt",
        manifest=m,
        live_epoch=7,
        receipts_path=tmp_path / "action-receipts.jsonl",
    )

    result = watchdog.run_once(
        manifest_path=manifest_path,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=250,
    )

    assert result["action"] == "healthy"
    assert any(
        receipt.get("kind") == "action-checkpoint-coord-accepted"
        for receipt in current_actions.ReceiptStore(tmp_path / "action-receipts.jsonl").records()
    )


def test_recovery_hold_runs_one_headless_turn_then_waits_for_next_deadline(tmp_path):
    manifest_path, actions = prepare_action_loop(tmp_path)
    m = load_manifest(manifest_path)
    (tmp_path / "recovery-hold.json").write_text(
        json.dumps(
            {
                "controller_epoch": 7,
                "action_digest": actions.digest,
                "reason": "two missed acknowledgments",
            }
        ),
        encoding="utf-8",
    )

    def resume(command, **_kwargs):
        write_headless_authority(tmp_path, recorded_ts=501, epoch=8)
        current = current_actions.parse_actions(tmp_path / "current-actions.txt", manifest=m)
        rebound = current_actions.rebind_actions(
            current=current,
            path=tmp_path / "current-actions.txt",
            manifest=m,
            decision_digest="a" * 64,
            epoch=8,
            ownership="headless",
            now_ts=501,
        )
        source = tmp_path / "headless-next-actions.txt"
        header = {
            **rebound.header,
            "generation": rebound.generation + 1,
            "previous_action_digest": rebound.digest,
            "written_at": "1970-01-01T00:08:22Z",
            "next_check_at": "1970-01-01T00:13:22Z",
        }
        source.write_text(
            f"--- {current_actions.SCHEMA}\n"
            f"{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
            f"---\n{rebound.body}\n",
            encoding="utf-8",
        )
        checkpoint = current_actions.checkpoint_actions(
            source=source,
            destination=tmp_path / "current-actions.txt",
            manifest=m,
            live_epoch=8,
            receipts_path=tmp_path / "action-receipts.jsonl",
            expected_decision_digest="a" * 64,
        )
        current_actions.record_coord_acceptance(
            checkpoint_receipt=checkpoint,
            coord_response={"id": 42, "accepted": True},
            receipts_path=tmp_path / "action-receipts.jsonl",
        )
        return subprocess.CompletedProcess(command, 0, "done", "")

    completed = watchdog.run_once(
        manifest_path=manifest_path,
        state_dir=tmp_path,
        client=Client(None),
        now_ts=500,
        run=resume,
    )
    waiting = watchdog.run_once(
        manifest_path=manifest_path,
        state_dir=tmp_path,
        client=Client(None),
        now_ts=502,
    )

    assert completed["receipt"]["success"] is True
    assert completed["receipt"]["checkpoint_advanced"] is True
    assert completed["receipt"]["model_checkpoint_published"] is True
    assert completed["receipt"]["model_checkpoint_durable"] is True
    assert completed["receipt"]["epoch_released"] is True
    assert waiting["action"] == "headless-waiting"


def test_headless_rebind_without_model_checkpoint_is_not_recovery(tmp_path):
    manifest_path, actions = prepare_action_loop(tmp_path)
    m = load_manifest(manifest_path)
    (tmp_path / "recovery-hold.json").write_text(
        json.dumps({"controller_epoch": 7, "action_digest": actions.digest}),
        encoding="utf-8",
    )

    def resume(command, **_kwargs):
        write_headless_authority(tmp_path, recorded_ts=501, epoch=8)
        current = current_actions.parse_actions(tmp_path / "current-actions.txt", manifest=m)
        current_actions.rebind_actions(
            current=current,
            path=tmp_path / "current-actions.txt",
            manifest=m,
            decision_digest="a" * 64,
            epoch=8,
            ownership="headless",
            now_ts=501,
        )
        return subprocess.CompletedProcess(command, 0, "done", "")

    result = watchdog.run_once(
        manifest_path=manifest_path,
        state_dir=tmp_path,
        client=Client(None),
        now_ts=500,
        run=resume,
    )

    assert result["receipt"]["success"] is False
    assert result["receipt"]["checkpoint_advanced"] is True
    assert result["receipt"]["model_checkpoint_published"] is False


def test_headless_waiting_fails_closed_if_any_live_lease_appears(tmp_path):
    manifest_path, actions = prepare_action_loop(tmp_path)
    (tmp_path / "recovery-hold.json").write_text(
        json.dumps({"controller_epoch": 7, "action_digest": actions.digest}),
        encoding="utf-8",
    )
    (tmp_path / "watchdog-state.json").write_text(
        json.dumps({"last_headless_checkpoint_digest": actions.digest}),
        encoding="utf-8",
    )

    result = watchdog.run_once(
        manifest_path=manifest_path,
        state_dir=tmp_path,
        client=Client({"holder": "another-controller", "epoch": 9}),
        now_ts=200,
    )

    assert result["ok"] is False
    assert result["action"] == "headless-waiting-live-lease-present"


def test_headless_trial_never_starts_while_visible_epoch_is_live(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=500,
    )

    assert result["action"] == "awaiting-visible-lease-expiry-for-headless-trial"


def test_two_fenced_tab_pokes_then_headless_waits_for_lease_expiry(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="tab")
    arm_stale(tmp_path)
    seen = []
    client = Client({"holder": "mikebook_codex", "epoch": 7})

    first = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=client,
        now_ts=500,
        poke_fn=lambda **kwargs: seen.append(kwargs) or {"ok": True},
    )
    second = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=client,
        now_ts=501,
        poke_fn=lambda **kwargs: seen.append(kwargs) or {"ok": True},
    )
    third = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=client,
        now_ts=502,
        poke_fn=lambda **kwargs: seen.append(kwargs) or {"ok": True},
    )

    assert [first["action"], second["action"], third["action"]] == [
        "tab-poke",
        "tab-poke",
        "awaiting-visible-lease-expiry-for-headless-trial",
    ]
    assert len(seen) == 2
    assert seen[0]["idempotency_key"] != seen[1]["idempotency_key"]
    assert all(item["controller_epoch"] == 7 for item in seen)


def test_zero_exit_without_headless_authority_fails_closed(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client(None),
        now_ts=500,
        run=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "done", ""),
    )

    assert result["ok"] is False
    assert result["receipt"]["authority_acquired"] is False
    assert result["backoff_seconds"] == 60


def test_provider_backoff_still_checks_lease_and_fresh_heartbeat(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)
    first_client = CountingClient(None)

    failed = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=first_client,
        now_ts=500,
        run=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("provider unavailable")),
    )
    assert failed["backoff_seconds"] == 60
    assert failed["receipt"]["provider_error"] == "OSError"

    backoff_client = CountingClient(None)
    backed_off = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=backoff_client,
        now_ts=501,
    )
    assert backed_off["action"] == "provider-backoff-health-only"
    assert backed_off["lease_checked"] is True
    assert backoff_client.calls == 1

    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 502}), encoding="utf-8"
    )
    healthy_client = CountingClient(None)
    healthy = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=healthy_client,
        now_ts=503,
    )
    assert healthy["action"] == "healthy"
    assert healthy_client.calls == 0


def test_provider_failure_backoff_is_exponential_and_capped(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)
    now_ts = 500
    observed = []

    for _ in range(6):
        result = watchdog.run_once(
            manifest_path=manifest,
            state_dir=tmp_path,
            client=Client(None),
            now_ts=now_ts,
            run=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("provider unavailable")),
        )
        observed.append(result["backoff_seconds"])
        now_ts += result["backoff_seconds"]

    assert observed == [60, 120, 240, 480, 900, 900]
