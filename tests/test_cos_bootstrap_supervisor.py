from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_bootstrap_supervisor as supervisor  # noqa: E402
from c2_contract import ContractError, RunManifest  # noqa: E402
from c2_coord_client import LeaseHandle  # noqa: E402


def manifest():
    return RunManifest.from_dict(
        {
            "manifest_id": "test",
            "controller": {
                "controller_id": "cos",
                "host": "macbook",
                "runtime": "codex",
                "iterm_session_id": "iterm-cos",
                "tty": "/dev/ttys001",
                "cli_session_id": "cli-cos",
                "coord_session_id": "coord-cos",
                "coord_agent_id": "mikebook_codex",
            },
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

    def claim_resource(self, resource, **kwargs):
        self.claimed += 1
        lease = {"holder": "mikebook_codex", "epoch": 7, "expires_at": "2099-01-01T00:00:00Z"}
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
