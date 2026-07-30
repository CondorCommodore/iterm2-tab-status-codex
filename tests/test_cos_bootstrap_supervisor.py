from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_bootstrap_supervisor as supervisor  # noqa: E402
from c2_contract import ContractError, RunManifest  # noqa: E402
from c2_coord_client import LeaseHandle  # noqa: E402


def manifest(*, controller_visible: bool = True, plan_paths: list[str] | None = None):
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
    return RunManifest.from_dict(
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
            "plan_paths": plan_paths or ["/plan"],
            "permitted_repositories": ["Condor/repo"],
            "permitted_actions": ["inspect"],
        }
    )


def test_reconcile_wakes_for_idle_refill_and_exact_session():
    decision = supervisor.reconcile(
        manifest=manifest(),
        actionable={"items": [{"kind": "task", "task_id": "task-1"}]},
        live_state={
            "generated_ts": 100,
            "sessions": [
                {
                    "iterm_session_id": "iterm-worker",
                    "tty": "/dev/ttys003",
                    "runtime": "codex",
                    "readiness": "ready",
                }
            ],
        },
        now_ts=100,
    )

    assert decision["idle_worker_ids"] == ["worker"]
    assert decision["wake_required"] is True
    assert "assignment decision" in decision["wake_reasons"][0]


def test_reconcile_marks_reused_tty_with_wrong_session_lost():
    decision = supervisor.reconcile(
        manifest=manifest(),
        actionable={"items": []},
        live_state={
            "generated_ts": 100,
            "sessions": [
                {
                    "iterm_session_id": "successor",
                    "tty": "/dev/ttys003",
                    "runtime": "codex",
                    "readiness": "idle",
                }
            ],
        },
        now_ts=100,
    )

    assert decision["workers"][0]["state"] == "lost"
    assert decision["wake_required"] is True


def test_decision_digest_ignores_screen_churn_but_tracks_worker_state():
    base = {
        "manifest_id": "test",
        "workers": [
            {
                "worker_id": "worker",
                "host": "macbook",
                "runtime": "codex",
                "iterm_session_id": "iterm-worker",
                "tty": "/dev/ttys003",
                "state": "running",
                "observed": {"screen_tail": "first", "generated_ts": 100},
            }
        ],
        "actionable_items": [],
        "idle_worker_ids": [],
        "exception_worker_ids": [],
        "wake_required": False,
        "wake_reasons": [],
    }
    churn = json.loads(json.dumps(base))
    churn["workers"][0]["observed"] = {"screen_tail": "second", "generated_ts": 101}
    changed = json.loads(json.dumps(base))
    changed["workers"][0]["state"] = "idle"

    assert supervisor.decision_digest(base) == supervisor.decision_digest(churn)
    assert supervisor.decision_digest(base) != supervisor.decision_digest(changed)


def test_decision_digest_tracks_actionable_payload_without_exposing_it():
    base = {
        "manifest_id": "test",
        "workers": [],
        "actionable_items": [
            {
                "kind": "message",
                "message_id": "M-1",
                "subject": "Immediate review",
                "content": "Inspect exact head abc",
                "scope": ["repo-a"],
                "fetched_at": 100,
            }
        ],
        "idle_worker_ids": [],
        "exception_worker_ids": [],
        "wake_required": True,
        "wake_reasons": ["actionable coordination message requires model decision"],
    }
    volatile = json.loads(json.dumps(base))
    volatile["actionable_items"][0]["fetched_at"] = 200
    changed = json.loads(json.dumps(base))
    changed["actionable_items"][0]["content"] = "Inspect exact head def"
    authorization_changed = json.loads(json.dumps(base))
    authorization_changed["actionable_items"][0]["authorization_limits"] = ["no-merge"]

    assert supervisor.decision_digest(base) == supervisor.decision_digest(volatile)
    assert supervisor.decision_digest(base) != supervisor.decision_digest(changed)
    assert supervisor.decision_digest(base) != supervisor.decision_digest(authorization_changed)


class FakeClient:
    def __init__(self):
        self.config = type("Config", (), {"principal_id": "mikebook_codex"})()
        self.released = []
        self.claimed = 0
        self.claim_producers = []

    def claim_resource(self, resource, **kwargs):
        self.claimed += 1
        self.claim_producers.append(kwargs["producer"])
        lease = {
            "holder": "mikebook_codex",
            "epoch": 7,
            "expires_at": "2099-01-01T00:00:00Z",
            "producer": kwargs["producer"],
        }
        return LeaseHandle(resource, "mikebook_codex", 7, lease["expires_at"], lease)

    def renew_resource(self, handle):
        return handle

    def verify_live_epoch(self, _resource, _epoch):
        return {"holder": "mikebook_codex", "epoch": 7, "expires_at": "2099-01-01T00:00:00Z"}

    def release_resource(self, handle):
        self.released.append(handle.epoch)
        return True

    def actionable(self, agent_id):
        return {"items": []}

    def get_resource(self, resource):
        return None


def launchctl_runner(*loaded_labels):
    loaded = set(loaded_labels)

    def run(command, **_kwargs):
        label = command[-1].rsplit("/", 1)[-1]
        is_loaded = label in loaded
        return subprocess.CompletedProcess(
            command,
            0 if is_loaded else 113,
            stdout="service = loaded" if is_loaded else "",
            stderr="Could not find service" if not is_loaded else "",
        )

    return run


def readiness(*, watchdog: bool, edge: bool):
    labels = []
    if watchdog:
        labels.append("com.local.cos-bootstrap-watchdog")
    if edge:
        labels.append("com.local.cos-iterm-edge")
    return supervisor.service_readiness(run=launchctl_runner(*labels), system="Darwin", uid=501)


def test_service_readiness_requires_both_loaded_services():
    absent = readiness(watchdog=False, edge=False)
    partial = readiness(watchdog=True, edge=False)
    ready = readiness(watchdog=True, edge=True)

    assert absent["ready"] is False
    assert partial["ready"] is False
    assert partial["services"]["watchdog"]["loaded"] is True
    assert partial["services"]["terminal_edge"]["loaded"] is False
    assert ready["ready"] is True


def test_service_readiness_is_fail_closed_off_darwin():
    observed = supervisor.service_readiness(system="Linux")

    assert observed == {
        "supported": False,
        "ready": False,
        "reason": "unsupported platform: Linux",
        "services": {},
    }


def test_cli_arm_refuses_without_services_and_does_not_write_marker(tmp_path):
    with pytest.raises(ContractError, match="arm readiness refused"):
        supervisor.arm_from_cli(
            manifest=manifest(),
            state_dir=tmp_path,
            readiness=readiness(watchdog=False, edge=False),
        )

    assert not (tmp_path / "ARMED").exists()
    assert not (tmp_path / "supervisor-state.json").exists()


def test_cli_arm_succeeds_only_with_ready_services(tmp_path):
    observed = readiness(watchdog=True, edge=True)
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    result = supervisor.arm_from_cli(
        manifest=manifest(plan_paths=[str(plan)]),
        state_dir=tmp_path,
        readiness=observed,
    )

    assert result["armed"] is True
    assert result["service_readiness"] == observed
    assert (tmp_path / "ARMED").is_file()


def test_status_reports_armed_but_unserviced_without_mutating_state(tmp_path):
    supervisor.arm(manifest=manifest(), state_dir=tmp_path, validate_plan_paths=False)
    marker_before = (tmp_path / "ARMED").read_bytes()

    result = supervisor.status(
        client=FakeClient(),
        state_dir=tmp_path,
        readiness=readiness(watchdog=True, edge=False),
    )

    assert result["armed"] is True
    assert result["armed_but_unserviced"] is True
    assert result["service_readiness"]["ready"] is False
    assert (tmp_path / "ARMED").read_bytes() == marker_before


def test_status_includes_read_only_fleet_snapshot(tmp_path):
    supervisor.arm(manifest=manifest(), state_dir=tmp_path, validate_plan_paths=False)
    live_state = tmp_path / "live.json"
    live_state.write_text(
        json.dumps(
            {
                "generated_ts": time.time(),
                "sessions": [
                    {
                        "iterm_session_id": "iterm-worker",
                        "tty": "/dev/ttys003",
                        "runtime": "codex",
                        "readiness": "idle",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = supervisor.status(
        client=FakeClient(),
        state_dir=tmp_path,
        manifest=manifest(),
        readiness=readiness(watchdog=True, edge=True),
        live_state_path=live_state,
    )

    snapshot = result["fleet_snapshot"]
    assert snapshot["error"] is None
    assert snapshot["workers"][0]["worker_id"] == "worker"
    assert snapshot["workers"][0]["state"] == "idle"
    assert snapshot["wake_required"] is False


def test_arm_run_no_wake_standby_and_stop_are_explicit(tmp_path):
    m = manifest()
    client = FakeClient()
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": 100, "sessions": []}), encoding="utf-8")

    armed = supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)
    tick = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership="visible",
        wake=False,
    )
    standby = supervisor.set_standby(client=client, state_dir=tmp_path)
    stopped = supervisor.stop(client=client, state_dir=tmp_path)

    assert armed["armed"] is True
    assert tick["authority"] is True and tick["controller_epoch"] == 7
    assert standby["mode"] == "bootstrap-standby" and standby["released"] is True
    assert stopped["armed"] is False


@pytest.mark.parametrize("ownership", ["visible", "headless"])
def test_run_tick_claims_coord_compatible_controller_producer(tmp_path, ownership):
    m = manifest()
    client = FakeClient()
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": 100, "sessions": []}), encoding="utf-8")
    supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)

    tick = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership=ownership,
        wake=False,
    )

    assert tick["authority"] is True
    assert client.claim_producers == [m.controller_producer(ownership)]


@pytest.mark.parametrize(
    ("first_ownership", "successor_ownership"),
    [("visible", "headless"), ("headless", "visible")],
)
def test_run_tick_rejects_stored_lease_from_other_presentation(
    tmp_path, first_ownership, successor_ownership
):
    m = manifest()
    client = FakeClient()
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": 100, "sessions": []}), encoding="utf-8")
    supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)
    supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership=first_ownership,
        wake=False,
    )

    with pytest.raises(ContractError, match="does not match requested ownership"):
        supervisor.run_tick(
            manifest=m,
            client=client,
            state_dir=tmp_path,
            live_state_path=live,
            ownership=successor_ownership,
            wake=False,
        )

    assert client.claimed == 1


def test_run_tick_rejects_manifest_changed_after_arm(tmp_path):
    original = manifest()
    supervisor.arm(manifest=original, state_dir=tmp_path, validate_plan_paths=False)
    changed = type(original)(**{**original.__dict__, "manifest_id": "changed"})
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": 100, "sessions": []}), encoding="utf-8")
    with pytest.raises(ContractError, match="explicit re-arm"):
        supervisor.run_tick(
            manifest=changed,
            client=FakeClient(),
            state_dir=tmp_path,
            live_state_path=live,
            ownership="visible",
            wake=False,
        )


def test_failed_wake_is_retried_for_same_decision(monkeypatch, tmp_path):
    m = manifest()
    client = FakeClient()
    client.actionable = lambda _agent: {"items": [{"kind": "task", "task_id": "task-1"}]}
    live = tmp_path / "live.json"
    live.write_text(
        json.dumps(
            {
                "generated_ts": time.time(),
                "sessions": [
                    {
                        "iterm_session_id": "iterm-worker",
                        "tty": "/dev/ttys003",
                        "runtime": "codex",
                        "readiness": "idle",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)
    calls = []
    monkeypatch.setattr(
        supervisor,
        "poke_controller",
        lambda **kwargs: calls.append(kwargs) or {"ok": len(calls) > 1},
    )
    first = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership="visible",
        wake=True,
    )
    second = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership="visible",
        wake=True,
    )
    assert first["poked"] is False
    assert second["poked"] is True
    assert len(calls) == 2


def test_arm_clears_stale_heartbeat_decision_and_watchdog_state(tmp_path):
    for name in ("supervisor-heartbeat.json", "decision-current.json", "watchdog-state.json"):
        (tmp_path / name).write_text('{"stale":true}\n', encoding="utf-8")
    supervisor.arm(manifest=manifest(), state_dir=tmp_path, validate_plan_paths=False)
    for name in ("supervisor-heartbeat.json", "decision-current.json"):
        assert not (tmp_path / name).exists()
    watchdog = json.loads((tmp_path / "watchdog-state.json").read_text())
    assert watchdog["pending_since"] is None
    assert watchdog["edge_health_failures"] == 0


def test_arm_preserves_and_derives_recovery_receipt_sequences(tmp_path):
    (tmp_path / "watchdog-state.json").write_text(
        json.dumps({"recovery_sequence": 3, "edge_restart_sequence": 2}), encoding="utf-8"
    )
    (tmp_path / "recovery-receipts.jsonl").write_text(
        json.dumps({"idempotency_key": "c2-recovery:4:headless"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "edge-recovery-receipts.jsonl").write_text(
        json.dumps({"idempotency_key": "edge-recovery:6"}) + "\n", encoding="utf-8"
    )
    supervisor.arm(manifest=manifest(), state_dir=tmp_path, validate_plan_paths=False)
    watchdog = json.loads((tmp_path / "watchdog-state.json").read_text())
    assert watchdog["recovery_sequence"] == 5
    assert watchdog["edge_restart_sequence"] == 7


def test_visible_recovery_hold_releases_epoch_and_headless_rebinds_actions(tmp_path):
    m = manifest()
    client = FakeClient()
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": time.time(), "sessions": []}), encoding="utf-8")
    supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)
    first = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership="visible",
        wake=False,
    )
    (tmp_path / "recovery-hold.json").write_text(
        json.dumps(
            {
                "controller_epoch": 7,
                "action_digest": first["action_digest"],
                "reason": "missed acknowledgments",
            }
        ),
        encoding="utf-8",
    )

    held = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership="visible",
        wake=False,
    )
    resumed = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership="headless",
        wake=False,
    )

    assert held["action"] == "recovery-hold"
    assert held["authority"] is False
    assert client.released == [7]
    assert resumed["authority"] is True
    rebound = supervisor.parse_actions(tmp_path / "current-actions.txt", manifest=m)
    assert rebound.header["ownership"] == "headless"
    assert rebound.generation == 2


def test_existing_malformed_actions_fail_closed_without_reseed(tmp_path):
    m = manifest()
    client = FakeClient()
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": time.time(), "sessions": []}), encoding="utf-8")
    supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)
    malformed = b"corrupt-current-actions\n"
    (tmp_path / "current-actions.txt").write_bytes(malformed)

    with pytest.raises(ContractError, match="versioned JSON header"):
        supervisor.run_tick(
            manifest=m,
            client=client,
            state_dir=tmp_path,
            live_state_path=live,
            ownership="visible",
            wake=False,
        )

    assert (tmp_path / "current-actions.txt").read_bytes() == malformed


def test_headless_controller_skips_wake_poke(tmp_path, monkeypatch):
    m = manifest(controller_visible=False)
    client = FakeClient()
    client.actionable = lambda _agent: {"items": [{"kind": "task", "task_id": "task-1"}]}
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": time.time(), "sessions": []}), encoding="utf-8")
    supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)
    pokes = []
    monkeypatch.setattr(
        supervisor,
        "poke_controller",
        lambda **kwargs: pokes.append(kwargs) or {"ok": True},
    )

    result = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership="headless",
        wake=True,
    )

    assert result["wake_required"] is True
    assert result["poked"] is False
    assert pokes == []
