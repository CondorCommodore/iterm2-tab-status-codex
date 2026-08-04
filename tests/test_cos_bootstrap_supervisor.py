from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_bootstrap_supervisor as supervisor  # noqa: E402
from c2_contract import ContractError, RunManifest, WorkerRegistration  # noqa: E402
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


def test_reconcile_extracts_latest_durable_cos_direction():
    decision = supervisor.reconcile(
        manifest=manifest(),
        actionable={
            "items": [
                {
                    "kind": "message",
                    "message_id": 11,
                    "provenance_source": "cos",
                    "content": json.dumps(
                        {
                            "schema": "cos.direction.v1",
                            "direction_id": "dir-2",
                            "plan_id": "plan-1",
                            "generation": 2,
                            "precedence": "priority",
                        }
                    ),
                }
            ]
        },
        live_state={"generated_ts": 100, "sessions": []},
        now_ts=100,
    )
    assert decision["latest_direction"]["generation"] == 2
    assert decision["latest_direction"]["message_id"] == 11
    assert "durable COS direction" in decision["wake_reasons"][-1]


def test_reconcile_projects_latest_cos_order_without_mutating_tasks():
    older = {
        "kind": "message",
        "message_id": 10,
        "provenance_source": "cos",
        "external_id": "cos-direction:p:1",
        "correlation_id": "p",
        "content": json.dumps(
            {
                "schema": "cos.direction.v1",
                "direction_id": "d1",
                "plan_id": "p",
                "generation": 1,
                "work_order": [{"kind": "task", "ref": "task-old"}],
            }
        ),
    }
    newer = {
        "kind": "message",
        "message_id": 11,
        "provenance_source": "cos",
        "external_id": "cos-direction:p:2",
        "correlation_id": "p",
        "content": json.dumps(
            {
                "schema": "cos.direction.v1",
                "direction_id": "d2",
                "plan_id": "p",
                "generation": 2,
                "work_order": [
                    {"kind": "pr", "ref": "https://github.com/acme/repo/pull/21"},
                    {"kind": "task", "ref": "task-new"},
                ],
            }
        ),
    }
    decision = supervisor.reconcile(
        manifest=manifest(),
        actionable={
            "items": [
                {"kind": "task", "task_id": "task-new"},
                older,
                newer,
            ]
        },
        live_state={"generated_ts": 100, "sessions": []},
        now_ts=100,
    )
    assert decision["cos_work_order"]["generation"] == 2
    assert [item["kind"] for item in decision["cos_work_order"]["work_order"]] == ["pr", "task"]
    assert decision["actionable_items"][0]["task_id"] == "task-new"


def test_latest_cos_work_order_ignores_malformed_or_duplicate_entries():
    items = [
        {
            "kind": "message",
            "message_id": 12,
            "provenance_source": "cos",
            "external_id": "cos-direction:p:3",
            "correlation_id": "p",
            "content": json.dumps(
                {
                    "schema": "cos.direction.v1",
                    "direction_id": "d",
                    "plan_id": "p",
                    "generation": 3,
                    "work_order": [
                        {"kind": "task", "ref": "same"},
                        {"kind": "task", "ref": "same"},
                    ],
                }
            ),
        }
    ]
    assert supervisor.latest_cos_work_order(items) is None


def test_latest_cos_work_order_rejects_forged_instruction_metadata():
    item = {
        "kind": "message",
        "message_id": 13,
        "provenance_source": "dispatch",
        "external_id": "cos-direction:p:4",
        "correlation_id": "p",
        "content": json.dumps(
            {
                "schema": "cos.direction.v1",
                "direction_id": "d",
                "plan_id": "p",
                "generation": 4,
                "work_order": [{"kind": "task", "ref": "forged"}],
            }
        ),
    }
    assert supervisor.latest_cos_work_order([item]) is None


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


def test_dispatch_current_focus_requires_live_authority(tmp_path):
    with pytest.raises(ContractError, match="dispatch-focus requires live supervisor authority"):
        supervisor.dispatch_current_focus(
            manifest=manifest(),
            manifest_path=tmp_path / "manifest.json",
            state_dir=tmp_path,
            live_state_path=tmp_path / "iterm-live-state.json",
            client=object(),
            worker_receipt_adapter="receipt_module:commit_receipt",
        )


def test_dispatch_current_focus_uses_focused_durable_orchestrator(tmp_path, monkeypatch):
    paths = supervisor.state_paths(tmp_path)
    paths["state"].write_text(
        json.dumps({"authority": True, "controller_epoch": 7}),
        encoding="utf-8",
    )
    paths["current_focus"].write_text("focus", encoding="utf-8")
    paths["decision"].write_text(
        json.dumps({"external_state_sweep": {"blocked": False}}),
        encoding="utf-8",
    )
    live_state_path = tmp_path / "iterm-live-state.json"
    live_state_path.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    envelope = type(
        "Envelope",
        (),
        {
            "task_id": "task-1",
            "worker_id": "worker",
            "controller_epoch": 7,
            "generation": 3,
            "authorization_limits": ("no-deploy", "no-merge"),
            "plan_id": "cos_work_order",
            "direction_digest": "d" * 64,
        },
    )()
    calls: list[tuple[str, object]] = []

    class Client:
        def verify_live_epoch(self, resource, epoch):
            calls.append(("verify", {"resource": resource, "epoch": epoch}))

    monkeypatch.setattr(
        supervisor.cos_dispatch_orchestrator,
        "build_focus_dispatch_plan",
        lambda **kwargs: calls.append(("build", kwargs)) or (object(), envelope, manifest()),
    )
    monkeypatch.setattr(
        supervisor.cos_dispatch_orchestrator,
        "dispatch_focus_plan",
        lambda **kwargs: (
            calls.append(("dispatch", kwargs))
            or {"ok": True, "assignment_id": "assignment:task-1:3:worker"}
        ),
    )

    result = supervisor.dispatch_current_focus(
        manifest=manifest(),
        manifest_path=manifest_path,
        state_dir=tmp_path,
        live_state_path=live_state_path,
        client=Client(),
        worker_receipt_adapter="receipt_module:commit_receipt",
    )

    assert result["ok"] is True
    assert calls[0] == (
        "verify",
        {"resource": "workspace:mikebook:c2-supervisor", "epoch": 7},
    )
    assert calls[1][0] == "build"
    assert calls[1][1]["manifest_path"] == manifest_path
    assert calls[1][1]["current_focus_path"] == paths["current_focus"]
    assert calls[1][1]["decision_path"] == paths["decision"]
    assert calls[1][1]["state_path"] == live_state_path
    assert calls[2] == (
        "dispatch",
        {
            "manifest": manifest(),
            "envelope": envelope,
            "worker_receipt_adapter": "receipt_module:commit_receipt",
        },
    )


def test_dispatch_current_focus_rejects_external_divergence(tmp_path):
    paths = supervisor.state_paths(tmp_path)
    paths["state"].write_text(
        json.dumps({"authority": True, "controller_epoch": 7}),
        encoding="utf-8",
    )
    paths["current_focus"].write_text("focus", encoding="utf-8")
    paths["decision"].write_text(
        json.dumps({"external_state_sweep": {"blocked": True}}),
        encoding="utf-8",
    )

    class Client:
        def verify_live_epoch(self, resource, epoch):
            return None

    with pytest.raises(ContractError, match="external state divergence"):
        supervisor.dispatch_current_focus(
            manifest=manifest(),
            manifest_path=tmp_path / "manifest.json",
            state_dir=tmp_path,
            live_state_path=tmp_path / "iterm-live-state.json",
            client=Client(),
            worker_receipt_adapter="receipt_module:commit_receipt",
        )


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


def test_status_distinguishes_valid_arm_from_stale_marker(tmp_path):
    supervisor.arm(manifest=manifest(), state_dir=tmp_path, validate_plan_paths=False)
    valid = supervisor.status(
        client=FakeClient(),
        state_dir=tmp_path,
        manifest=manifest(),
        readiness=readiness(watchdog=True, edge=True),
    )

    assert valid["armed"] is True
    assert valid["arm_marker_valid"] is True
    assert valid["effective_armed"] is True
    assert valid["armed_but_invalid"] is False

    (tmp_path / "ARMED").write_text("legacy marker\n", encoding="utf-8")
    stale = supervisor.status(
        client=FakeClient(),
        state_dir=tmp_path,
        manifest=manifest(),
        readiness=readiness(watchdog=True, edge=True),
    )

    assert stale["armed"] is True  # physical marker remains observable
    assert stale["arm_marker_valid"] is False
    assert stale["effective_armed"] is False
    assert stale["armed_but_invalid"] is True
    assert stale["requires_explicit_rearm"] is True


def test_status_does_not_claim_effective_arm_without_manifest(tmp_path):
    supervisor.arm(manifest=manifest(), state_dir=tmp_path, validate_plan_paths=False)

    result = supervisor.status(
        client=FakeClient(),
        state_dir=tmp_path,
        readiness=readiness(watchdog=True, edge=True),
    )

    assert result["armed"] is True
    assert result["arm_marker_valid"] is None
    assert result["effective_armed"] is None
    assert result["armed_but_invalid"] is False


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
                        "cli_session_id": "cli-worker",
                        "coord_session_id": "coord-worker",
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


def test_preflight_fails_closed_when_terminal_actions_are_disabled(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    m = manifest(plan_paths=[str(plan)])

    result = supervisor.preflight(
        manifest=m,
        manifest_path=manifest_path,
        readiness=readiness(watchdog=True, edge=True),
        edge_probe=lambda *_args, **_kwargs: {"ok": True},
    )

    assert result["ready"] is False
    assert result["edge"]["reason"] == "terminal_actions_disabled_in_manifest"
    assert [item["code"] for item in result["blockers"]] == [
        "terminal_actions_disabled",
        "no_idle_registered_worker",
        "edge_not_ready",
    ]


def test_preflight_accepts_matching_manifest_and_healthy_edge(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    m = manifest(plan_paths=[str(plan)])
    m = type(m)(**{**m.__dict__, "terminal_actions_enabled": True})
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("manifest-bytes\n", encoding="utf-8")
    digest = supervisor.manifest_file_sha256(manifest_path)
    live_state_path = tmp_path / "live.json"
    live_state_path.write_text(
        json.dumps(
            {
                "generated_ts": time.time(),
                "sessions": [
                    {
                        "iterm_session_id": "iterm-worker",
                        "tty": "/dev/ttys003",
                        "runtime": "codex",
                        "cli_session_id": "cli-worker",
                        "coord_session_id": "coord-worker",
                        "readiness": "idle",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = supervisor.preflight(
        manifest=m,
        manifest_path=manifest_path,
        live_state_path=live_state_path,
        readiness=readiness(watchdog=True, edge=True),
        edge_probe=lambda *_args, **_kwargs: {
            "ok": True,
            "manifest_sha256": digest,
        },
    )

    assert result["ready"] is True
    assert result["idle_worker_ids"] == ["worker"]
    assert result["blockers"] == []


def test_preflight_rejects_identity_drift_even_with_another_idle_worker(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    base = manifest(plan_paths=[str(plan)])
    second = WorkerRegistration(
        worker_id="worker-2",
        host="macbook",
        runtime="codex",
        iterm_session_id="iterm-worker-2",
        tty="/dev/ttys004",
        cli_session_id="cli-worker-2",
        coord_session_id="coord-worker-2",
        coord_agent_id="mikebook_codex",
    )
    third = WorkerRegistration(
        worker_id="worker-3",
        host="macbook",
        runtime="codex",
        iterm_session_id="iterm-worker-3",
        tty="/dev/ttys005",
        cli_session_id="cli-worker-3",
        coord_session_id="coord-worker-3",
        coord_agent_id="mikebook_codex",
    )
    m = replace(
        base,
        workers=(base.workers[0], second, third),
        terminal_actions_enabled=True,
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("manifest-bytes\n", encoding="utf-8")
    digest = supervisor.manifest_file_sha256(manifest_path)
    live_state_path = tmp_path / "live.json"
    live_state_path.write_text(
        json.dumps(
            {
                "generated_ts": time.time(),
                "sessions": [
                    {
                        "iterm_session_id": "replacement-session",
                        "tty": "/dev/ttys003",
                        "runtime": "codex",
                        "readiness": "unknown",
                    },
                    {
                        "iterm_session_id": "iterm-worker-2",
                        "tty": "/dev/ttys004",
                        "runtime": "codex",
                        "cli_session_id": "stale-cli-worker-2",
                        "coord_session_id": "stale-coord-worker-2",
                        "readiness": "idle",
                    },
                    {
                        "iterm_session_id": "iterm-worker-3",
                        "tty": "/dev/ttys005",
                        "runtime": "codex",
                        "cli_session_id": "cli-worker-3",
                        "coord_session_id": "coord-worker-3",
                        "readiness": "idle",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = supervisor.preflight(
        manifest=m,
        manifest_path=manifest_path,
        live_state_path=live_state_path,
        readiness=readiness(watchdog=True, edge=True),
        edge_probe=lambda *_args, **_kwargs: {
            "ok": True,
            "manifest_sha256": digest,
        },
    )

    assert result["idle_worker_ids"] == ["worker-2", "worker-3"]
    assert result["identity_drift"]
    assert result["ready"] is False
    assert "identity_drift" in {item["code"] for item in result["blockers"]}
    worker_two_drift = next(
        item for item in result["identity_drift"] if item.get("worker_id") == "worker-2"
    )
    assert set(worker_two_drift["drifted_fields"]) >= {
        "cli_session_id",
        "coord_session_id",
    }


def test_preflight_reports_same_tty_session_replacement_without_rebinding(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    m = manifest(plan_paths=[str(plan)])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("manifest-bytes\n", encoding="utf-8")
    live_state_path = tmp_path / "live.json"
    live_state_path.write_text(
        json.dumps(
            {
                "generated_ts": time.time(),
                "sessions": [
                    {
                        "iterm_session_id": "replacement-session",
                        "tty": "/dev/ttys003",
                        "runtime": "codex",
                        "readiness": "idle",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = supervisor.preflight(
        manifest=m,
        manifest_path=manifest_path,
        live_state_path=live_state_path,
        readiness=readiness(watchdog=True, edge=True),
    )

    assert result["ready"] is False
    assert result["worker_roster_ready"] is False
    replacement = result["identity_drift"][0]
    assert replacement["worker_id"] == "worker"
    assert replacement["expected_bindings"] == {
        "iterm_session_id": "iterm-worker",
        "tty": "/dev/ttys003",
        "runtime": "codex",
        "cli_session_id": "cli-worker",
        "coord_session_id": "coord-worker",
    }
    assert replacement["observed_bindings"] == {
        "iterm_session_id": "replacement-session",
        "tty": "/dev/ttys003",
        "runtime": "codex",
        "cli_session_id": None,
        "coord_session_id": None,
    }
    assert set(replacement["drifted_fields"]) == {
        "cli_session_id",
        "coord_session_id",
        "iterm_session_id",
    }
    assert {item["code"] for item in result["blockers"]} >= {
        "identity_drift",
        "no_idle_registered_worker",
    }


def test_roster_proposal_is_read_only_and_requires_explicit_rearm(tmp_path):
    live_state_path = tmp_path / "live.json"
    live_state_path.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "iterm_session_id": "replacement-session",
                        "tty": "/dev/ttys003",
                        "runtime": "codex",
                        "readiness": "idle",
                    },
                    {
                        "iterm_session_id": "unregistered-session",
                        "tty": "/dev/ttys099",
                        "runtime": "codex",
                        "readiness": "running",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = supervisor.roster_proposal(
        manifest=manifest(),
        live_state_path=live_state_path,
    )

    assert result["requires_explicit_rearm"] is True
    assert result["workers"][0]["status"] == "replacement-on-tty"
    assert result["workers"][0]["observed_bindings"] == [
        {
            "iterm_session_id": "replacement-session",
            "tty": "/dev/ttys003",
            "runtime": "codex",
            "cli_session_id": None,
            "coord_session_id": None,
        }
    ]
    assert set(result["workers"][0]["drifted_fields"]) == {
        "cli_session_id",
        "coord_session_id",
        "iterm_session_id",
    }
    assert result["unregistered_live_sessions"] == [
        {
            "iterm_session_id": "replacement-session",
            "tty": "/dev/ttys003",
            "runtime": "codex",
            "readiness": "idle",
        },
        {
            "iterm_session_id": "unregistered-session",
            "tty": "/dev/ttys099",
            "runtime": "codex",
            "readiness": "running",
        },
    ]


def test_roster_proposal_surfaces_binding_drift_for_expected_session(tmp_path):
    live_state_path = tmp_path / "live.json"
    live_state_path.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "iterm_session_id": "iterm-worker",
                        "tty": "/dev/ttys003",
                        "runtime": "codex",
                        "cli_session_id": "replacement-cli",
                        "coord_session_id": "replacement-coord",
                        "readiness": "idle",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = supervisor.roster_proposal(
        manifest=manifest(),
        live_state_path=live_state_path,
    )

    worker = result["workers"][0]
    assert worker["status"] == "binding-drift"
    assert set(worker["drifted_fields"]) == {"cli_session_id", "coord_session_id"}
    assert worker["expected_bindings"]["cli_session_id"] == "cli-worker"
    assert worker["observed_bindings"][0]["coord_session_id"] == "replacement-coord"


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


def test_run_tick_writes_digest_bound_program_and_current_focus_projections(tmp_path):
    m = manifest()
    client = FakeClient()
    client.actionable = lambda _agent: {
        "items": [
            {"kind": "task", "task_id": "task-1", "status": "queued"},
            {
                "kind": "message",
                "message_id": 11,
                "provenance_source": "cos",
                "content": json.dumps(
                    {
                        "schema": "cos.direction.v1",
                        "direction_id": "dir-2",
                        "plan_id": "plan-1",
                        "generation": 2,
                        "precedence": "priority",
                    }
                ),
            },
        ]
    }
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": 100, "sessions": []}), encoding="utf-8")
    supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)

    tick = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership="visible",
        wake=False,
    )

    assert tick["authority"] is True
    actions_path = tmp_path / "current-actions.txt"
    focus_path = tmp_path / "current-focus.md"
    program_path = tmp_path / "program.md"
    assert focus_path.read_bytes() != actions_path.read_bytes()
    focus_lines = focus_path.read_text(encoding="utf-8").splitlines()
    assert focus_lines[0] == "--- c2-current-focus-v1"
    focus_header = json.loads(focus_lines[1])
    assert focus_header["decision_digest"] == tick["decision_digest"]
    assert focus_header["action_digest"] == tick["action_digest"]
    assert focus_header["focus_kind"] == "task"
    assert focus_header["focus_ref"] == "task-1"
    program_lines = program_path.read_text(encoding="utf-8").splitlines()
    assert program_lines[0] == "--- c2-program-projection-v1"
    header = json.loads(program_lines[1])
    assert header["decision_digest"] == tick["decision_digest"]
    assert header["action_digest"] == tick["action_digest"]
    assert header["direction_message_id"] == 11
    assert header["plan_generation"] == 2
    body = "\n".join(program_lines[3:])
    assert "## Current portfolio" in body
    assert "## Worker roster" in body
    assert "## Ordered actionable items" in body
    assert "## Durable direction and references" in body
    assert tick["program_digest"]


def test_status_includes_validated_program_and_current_focus_projections(tmp_path):
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
        ownership="visible",
        wake=False,
    )

    result = supervisor.status(
        client=client,
        state_dir=tmp_path,
        manifest=m,
        readiness=readiness(watchdog=True, edge=True),
        live_state_path=live,
    )

    assert result["current_focus"]["action_digest"] == result["current_actions"]["digest"]
    assert result["current_focus"]["focus_kind"] == "none"
    assert result["current_focus"]["focus_ref"] == ""
    assert result["program_projection"]["action_digest"] == result["current_actions"]["digest"]


def test_status_rejects_tampered_program_projection_summary(tmp_path):
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
        ownership="visible",
        wake=False,
    )
    program = tmp_path / "program.md"
    program.write_text(
        program.read_text(encoding="utf-8") + "\nfreeform drift outside bounded bullets\n",
        encoding="utf-8",
    )

    result = supervisor.status(
        client=client,
        state_dir=tmp_path,
        manifest=m,
        readiness=readiness(watchdog=True, edge=True),
        live_state_path=live,
    )

    assert result["current_actions"] is not None
    assert result["program_projection"] is None


def test_status_rejects_program_projection_for_other_manifest(tmp_path):
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
        ownership="visible",
        wake=False,
    )
    mismatched = replace(m, controller_cli_session_id="other-cli")

    result = supervisor.status(
        client=client,
        state_dir=tmp_path,
        manifest=mismatched,
        readiness=readiness(watchdog=True, edge=True),
        live_state_path=live,
    )

    assert result["program_projection"] is None


def test_run_tick_persists_external_divergence_and_suppresses_wake(tmp_path, monkeypatch):
    m = manifest()
    client = FakeClient()
    client.actionable = lambda _agent: {
        "items": [
            {
                "kind": "task",
                "task_id": "task-1",
                "status": "queued",
                "pr_url": "https://github.com/acme/repo/pull/21",
                "branch_repo": "acme/repo",
                "branch_name": "feature/test",
            }
        ]
    }
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": 100, "sessions": []}), encoding="utf-8")
    supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)
    monkeypatch.setattr(
        supervisor,
        "sweep_external_state",
        lambda **kwargs: {
            "generated_at": "1970-01-01T00:01:40Z",
            "generated_ts": 100,
            "finding_count": 1,
            "blocked": True,
            "findings_digest": "d" * 64,
            "findings": [
                {
                    "kind": "tracked_pr_closed_unattributed",
                    "task_id": "task-1",
                    "pr_url": "https://github.com/acme/repo/pull/21",
                }
            ],
        },
    )
    pokes = []
    monkeypatch.setattr(
        supervisor,
        "poke_controller",
        lambda **kwargs: pokes.append(kwargs) or {"ok": True},
    )

    tick = supervisor.run_tick(
        manifest=m,
        client=client,
        state_dir=tmp_path,
        live_state_path=live,
        ownership="visible",
        wake=True,
    )

    assert tick["action"] == "external-divergence"
    assert tick["poked"] is False
    assert pokes == []
    persisted = json.loads((tmp_path / "external-state-sweep.json").read_text(encoding="utf-8"))
    assert persisted["blocked"] is True
    assert persisted["findings"][0]["kind"] == "tracked_pr_closed_unattributed"


def test_status_reports_persisted_and_live_external_state_sweep(tmp_path, monkeypatch):
    m = manifest()
    client = FakeClient()
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"generated_ts": 100, "sessions": []}), encoding="utf-8")
    supervisor.arm(manifest=m, state_dir=tmp_path, validate_plan_paths=False)
    (tmp_path / "external-state-sweep.json").write_text(
        json.dumps(
            {
                "generated_at": "1970-01-01T00:01:40Z",
                "generated_ts": 100,
                "finding_count": 1,
                "blocked": True,
                "findings_digest": "d" * 64,
                "findings": [{"kind": "tracked_branch_missing_unattributed"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        supervisor,
        "sweep_external_state",
        lambda **kwargs: {
            "generated_at": "1970-01-01T00:03:20Z",
            "generated_ts": 200,
            "finding_count": 0,
            "blocked": False,
            "findings_digest": "e" * 64,
            "findings": [],
        },
    )

    result = supervisor.status(
        client=client,
        state_dir=tmp_path,
        manifest=m,
        readiness=readiness(watchdog=True, edge=True),
        live_state_path=live,
    )

    assert result["external_state_sweep"]["blocked"] is True
    assert result["external_state_sweep_live"]["blocked"] is False


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
