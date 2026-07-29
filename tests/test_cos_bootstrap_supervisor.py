from __future__ import annotations

import json
import sys
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

    armed = supervisor.arm(
        manifest=m, state_dir=tmp_path, validate_plan_paths=False
    )
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
        supervisor.run_tick(manifest=changed, client=FakeClient(), state_dir=tmp_path,
                            live_state_path=live, ownership="visible", wake=False)
