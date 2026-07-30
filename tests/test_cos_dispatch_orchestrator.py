from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_dispatch_orchestrator as orchestrator  # noqa: E402


def test_build_dispatch_plan_selects_idle_worker(tmp_path):
    state_path = tmp_path / "state.json"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "tabs": [
                    {"tty": "/dev/ttys001", "state": "idle", "role": "cos"},
                    {"tty": "/dev/ttys002", "state": "idle", "role": "worker"},
                ]
            }
        ),
        encoding="utf-8",
    )

    plan = orchestrator.build_dispatch_plan(
        goal="do useful work",
        state_path=state_path,
        report_dir=report_dir,
        cos_tty="/dev/ttys001",
    )

    assert plan.ok is True
    assert plan.tty == "/dev/ttys002"
    assert plan.text == "/goal do useful work"
    assert plan.dry_run_payload == "'/goal do useful work\\n'"
    assert plan.transport == "legacy-dry-run"


def test_build_dispatch_plan_reports_no_worker(tmp_path):
    state_path = tmp_path / "state.json"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    state_path.write_text(json.dumps({"tabs": []}), encoding="utf-8")

    plan = orchestrator.build_dispatch_plan(
        goal="/goal work",
        state_path=state_path,
        report_dir=report_dir,
    )

    assert plan.ok is False
    assert plan.reason == "no eligible worker tab"


def test_live_goal_dispatch_is_denied_without_envelope(tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "state.json"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    state_path.write_text(json.dumps({"tabs": []}), encoding="utf-8")

    result = orchestrator.main(
        [
            "--goal",
            "do work",
            "--state-path",
            str(state_path),
            "--report-dir",
            str(report_dir),
        ]
    )

    assert result == 2
    assert "live dispatch requires --envelope" in capsys.readouterr().out


def test_envelope_plan_validates_worker_and_manifest(tmp_path):
    manifest = {
        "manifest_id": "m1",
        "controller": {
            "controller_id": "cos",
            "host": "mikebook",
            "runtime": "codex",
            "cli_session_id": "controller-cli",
            "coord_session_id": "controller-coord",
            "coord_agent_id": "mikebook_codex",
        },
        "workers": [
            {
                "worker_id": "w1",
                "host": "mikebook",
                "runtime": "codex",
                "iterm_session_id": "iterm-w1",
                "tty": "/dev/ttys003",
                "cli_session_id": "cli-w1",
                "coord_session_id": "coord-w1",
                "coord_agent_id": "mikebook_codex",
                "repositories": ["CondorCommodore/home-lab"],
            }
        ],
        "plan_paths": [str(tmp_path / "plan.md")],
        "permitted_repositories": ["CondorCommodore/home-lab"],
        "permitted_actions": ["inspect", "test"],
    }
    envelope = {
        "assignment_id": "a1",
        "task_id": "T-1",
        "attempt_id": "A-1",
        "worker_id": "w1",
        "cli_session_id": "cli-w1",
        "coord_session_id": "coord-w1",
        "objective": "inspect",
        "repo": "CondorCommodore/home-lab",
        "worktree": str(tmp_path),
        "scope": ["README.md"],
        "acceptance_tests": ["true"],
        "stopping_condition": "report",
        "report_destination": "coord-api",
        "authorization_limits": ["no deploy"],
        "permitted_actions": ["inspect"],
        "controller_epoch": 3,
        "idempotency_key": "a1-v1",
    }
    manifest_path = tmp_path / "manifest.json"
    envelope_path = tmp_path / "envelope.json"
    state_path = tmp_path / "state.json"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    state_path.write_text(json.dumps({"tabs": []}), encoding="utf-8")

    plan, parsed, _ = orchestrator.build_envelope_dispatch_plan(
        envelope_path=envelope_path,
        manifest_path=manifest_path,
        report_dir=report_dir,
        state_path=state_path,
    )

    assert plan.ok is True
    assert plan.tty == "/dev/ttys003"
    assert plan.transport == "tab"
    assert plan.envelope_digest == parsed.digest()
