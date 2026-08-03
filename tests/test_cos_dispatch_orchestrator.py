from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_dispatch_orchestrator as orchestrator  # noqa: E402
from c2_contract import ContractError  # noqa: E402


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


def test_live_focus_dispatch_requires_worker_receipt_adapter(tmp_path, monkeypatch, capsys):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    current_focus_path = tmp_path / "current-focus.md"
    decision_path = tmp_path / "decision-current.json"
    state_path = tmp_path / "state.json"
    manifest_path.write_text("{}", encoding="utf-8")
    current_focus_path.write_text("focus", encoding="utf-8")
    decision_path.write_text("{}", encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")

    manifest = type("Manifest", (), {"controller_coord_agent_id": "mikebook_codex"})()
    envelope = type(
        "Envelope",
        (),
        {
            "task_id": "task-1",
            "worker_id": "worker-1",
            "controller_epoch": 3,
            "generation": 4,
            "authorization_limits": ("no-deploy",),
            "plan_id": "cos_work_order",
            "direction_digest": "d" * 64,
            "__dict__": {},
        },
    )()
    plan = orchestrator.DispatchPlan(
        ok=True,
        tty="/dev/ttys003",
        text="do work",
        reason="focused dispatch selected worker-1",
        dashboard_action="",
        dry_run_payload="{}",
    )
    monkeypatch.setattr(
        orchestrator,
        "build_focus_dispatch_plan",
        lambda **kwargs: (plan, envelope, manifest),
    )

    result = orchestrator.main(
        [
            "--manifest",
            str(manifest_path),
            "--current-focus",
            str(current_focus_path),
            "--decision",
            str(decision_path),
            "--state-path",
            str(state_path),
            "--report-dir",
            str(report_dir),
        ]
    )

    assert result == 2
    assert "focused live dispatch requires --worker-receipt-adapter" in capsys.readouterr().out


def test_live_focus_dispatch_uses_durable_assignment_path(tmp_path, monkeypatch, capsys):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    current_focus_path = tmp_path / "current-focus.md"
    decision_path = tmp_path / "decision-current.json"
    state_path = tmp_path / "state.json"
    manifest_path.write_text("{}", encoding="utf-8")
    current_focus_path.write_text("focus", encoding="utf-8")
    decision_path.write_text("{}", encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")

    manifest = type("Manifest", (), {"controller_coord_agent_id": "mikebook_codex"})()
    envelope = type(
        "Envelope",
        (),
        {
            "task_id": "task-1",
            "worker_id": "worker-1",
            "controller_epoch": 3,
            "generation": 4,
            "authorization_limits": ("no-deploy", "no-merge"),
            "plan_id": "cos_work_order",
            "direction_digest": "d" * 64,
            "__dict__": {},
        },
    )()
    plan = orchestrator.DispatchPlan(
        ok=True,
        tty="/dev/ttys003",
        text="do work",
        reason="focused dispatch selected worker-1",
        dashboard_action="",
        dry_run_payload="{}",
    )
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "build_focus_dispatch_plan",
        lambda **kwargs: (plan, envelope, manifest),
    )
    monkeypatch.setattr(
        orchestrator,
        "dispatch_focus_plan",
        lambda **kwargs: (
            calls.append(kwargs) or {"ok": True, "assignment_id": "assignment:task-1:4:worker-1"}
        ),
    )

    result = orchestrator.main(
        [
            "--manifest",
            str(manifest_path),
            "--current-focus",
            str(current_focus_path),
            "--decision",
            str(decision_path),
            "--state-path",
            str(state_path),
            "--report-dir",
            str(report_dir),
            "--worker-receipt-adapter",
            "receipt_module:commit_receipt",
        ]
    )

    assert result == 0
    assert calls == [
        {
            "manifest": manifest,
            "envelope": envelope,
            "worker_receipt_adapter": "receipt_module:commit_receipt",
        }
    ]
    payload = capsys.readouterr().out
    assert '"assignment_id": "assignment:task-1:4:worker-1"' in payload


def test_live_focus_dispatch_returns_nonzero_on_dispatch_error(tmp_path, monkeypatch, capsys):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    current_focus_path = tmp_path / "current-focus.md"
    decision_path = tmp_path / "decision-current.json"
    state_path = tmp_path / "state.json"
    manifest_path.write_text("{}", encoding="utf-8")
    current_focus_path.write_text("focus", encoding="utf-8")
    decision_path.write_text("{}", encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")

    manifest = type("Manifest", (), {"controller_coord_agent_id": "mikebook_codex"})()
    envelope = type(
        "Envelope",
        (),
        {
            "task_id": "task-1",
            "worker_id": "worker-1",
            "controller_epoch": 3,
            "generation": 4,
            "authorization_limits": ("no-deploy", "no-merge"),
            "plan_id": "cos_work_order",
            "direction_digest": "d" * 64,
            "__dict__": {},
        },
    )()
    plan = orchestrator.DispatchPlan(
        ok=True,
        tty="/dev/ttys003",
        text="do work",
        reason="focused dispatch selected worker-1",
        dashboard_action="",
        dry_run_payload="{}",
    )
    monkeypatch.setattr(
        orchestrator,
        "build_focus_dispatch_plan",
        lambda **kwargs: (plan, envelope, manifest),
    )
    monkeypatch.setattr(
        orchestrator,
        "dispatch_focus_plan",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("adapter import failed")),
    )

    result = orchestrator.main(
        [
            "--manifest",
            str(manifest_path),
            "--current-focus",
            str(current_focus_path),
            "--decision",
            str(decision_path),
            "--state-path",
            str(state_path),
            "--report-dir",
            str(report_dir),
            "--worker-receipt-adapter",
            "receipt_module:commit_receipt",
        ]
    )

    assert result == 1
    payload = capsys.readouterr().out
    assert '"ok": false' in payload.lower()
    assert "adapter import failed" in payload


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


def write_current_focus(path: Path, **changes) -> None:
    header = {
        "schema": "c2-current-focus-v1",
        "manifest_id": "m1",
        "controller_id": "cos",
        "controller_cli_session_id": "controller-cli",
        "controller_coord_session_id": "controller-coord",
        "controller_iterm_session_id": "",
        "controller_epoch": 3,
        "ownership": "headless",
        "decision_digest": "a" * 64,
        "action_digest": "b" * 64,
        "action_generation": 2,
        "status": "active",
        "written_at": "1970-01-01T00:03:20Z",
        "next_check_at": "1970-01-01T00:08:20Z",
        "references": ["/plan.md"],
        "focus_kind": "task",
        "focus_ref": "task-1",
        "focus_source": "cos_work_order",
        "owner_session_id": "coord-w1",
        "known_gate": "assigned",
        "direction_message_id": 11,
        "direction_digest": "c" * 64,
        "plan_generation": 3,
    }
    header.update(changes)
    body = f"""## Current objective
- objective={header["focus_ref"] or "none"}
- focus_kind={header["focus_kind"]}
- focus_ref={header["focus_ref"] or "none"}
- focus_source={header["focus_source"]}

## Selected focus
- selected_status={header["known_gate"] or "none"}
- owner_session_id={header["owner_session_id"] or "none"}
- next_reconciliation=idle worker and actionable task require assignment decision

## Expected report or gate
- action_digest={header["action_digest"]}
- known_gate={header["known_gate"] or "none"}
- direction_message_id={
        (header["direction_message_id"] if header["direction_message_id"] is not None else "none")
    }

## Boundaries
- This projection is bounded recovery guidance, not durable task authority.
- Claims, leases, merges, and transport remain fenced by coord-api and the live epoch.

## Durable references
- plan_path=/plan.md

## Rewrite or stop condition
- Rewrite after a focus, worker, gate, or direction transition.
- Stop automatic work only when a later checkpoint marks the current actions complete.
"""
    path.write_text(
        "--- c2-current-focus-v1\n"
        f"{json.dumps(header, sort_keys=True, separators=(',', ':'))}\n"
        f"---\n{body}",
        encoding="utf-8",
    )


def test_focus_dispatch_plan_builds_envelope_for_selected_task(tmp_path):
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
                "coord_agent_id": "worker-agent",
                "repositories": ["CondorCommodore/home-lab"],
            }
        ],
        "plan_paths": ["/plan.md"],
        "permitted_repositories": ["CondorCommodore/home-lab"],
        "permitted_actions": ["inspect"],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    current_focus_path = tmp_path / "current-focus.md"
    write_current_focus(current_focus_path)
    decision_path = tmp_path / "decision-current.json"
    decision_path.write_text(
        json.dumps(
            {
                "actionable_items": [
                    {
                        "kind": "task",
                        "task_id": "task-1",
                        "status": "assigned",
                        "to_agent": "worker-agent",
                        "repo": "CondorCommodore/home-lab",
                        "summary": "inspect the bounded slice",
                        "target_files": ["README.md"],
                        "acceptance_criteria": "durable report",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "tabs": [
                    {"tty": "/dev/ttys003", "state": "idle", "role": "worker", "registered": True}
                ]
            }
        ),
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    plan, envelope, _ = orchestrator.build_focus_dispatch_plan(
        manifest_path=manifest_path,
        current_focus_path=current_focus_path,
        decision_path=decision_path,
        report_dir=report_dir,
        state_path=state_path,
    )

    assert plan.ok is True
    assert plan.tty == "/dev/ttys003"
    assert envelope.task_id == "task-1"
    assert envelope.worker_id == "w1"
    assert envelope.controller_epoch == 3
    assert envelope.plan_id == "cos_work_order"


def test_focus_dispatch_plan_rejects_non_task_focus(tmp_path):
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
                "coord_agent_id": "worker-agent",
                "repositories": ["CondorCommodore/home-lab"],
            }
        ],
        "plan_paths": ["/plan.md"],
        "permitted_repositories": ["CondorCommodore/home-lab"],
        "permitted_actions": ["inspect"],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    current_focus_path = tmp_path / "current-focus.md"
    write_current_focus(
        current_focus_path,
        focus_kind="pr",
        focus_ref="https://github.com/acme/repo/pull/21",
    )
    decision_path = tmp_path / "decision-current.json"
    decision_path.write_text(json.dumps({"actionable_items": []}), encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"tabs": []}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    import pytest

    with pytest.raises(ContractError, match="is not dispatchable as a task envelope"):
        orchestrator.build_focus_dispatch_plan(
            manifest_path=manifest_path,
            current_focus_path=current_focus_path,
            decision_path=decision_path,
            report_dir=report_dir,
            state_path=state_path,
        )


def test_focus_dispatch_plan_rejects_assigned_worker_without_live_eligible_tab(tmp_path):
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
                "coord_agent_id": "worker-agent",
                "repositories": ["CondorCommodore/home-lab"],
            }
        ],
        "plan_paths": ["/plan.md"],
        "permitted_repositories": ["CondorCommodore/home-lab"],
        "permitted_actions": ["inspect"],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    current_focus_path = tmp_path / "current-focus.md"
    write_current_focus(current_focus_path)
    decision_path = tmp_path / "decision-current.json"
    decision_path.write_text(
        json.dumps(
            {
                "actionable_items": [
                    {
                        "kind": "task",
                        "task_id": "task-1",
                        "status": "assigned",
                        "to_agent": "worker-agent",
                        "repo": "CondorCommodore/home-lab",
                        "summary": "inspect the bounded slice",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"tabs": []}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    import pytest

    with pytest.raises(ContractError, match="has no live eligible registered tab"):
        orchestrator.build_focus_dispatch_plan(
            manifest_path=manifest_path,
            current_focus_path=current_focus_path,
            decision_path=decision_path,
            report_dir=report_dir,
            state_path=state_path,
        )


def test_focus_dispatch_plan_allows_in_progress_assigned_running_worker(tmp_path):
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
                "coord_agent_id": "worker-agent",
                "repositories": ["CondorCommodore/home-lab"],
            }
        ],
        "plan_paths": ["/plan.md"],
        "permitted_repositories": ["CondorCommodore/home-lab"],
        "permitted_actions": ["inspect"],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    current_focus_path = tmp_path / "current-focus.md"
    write_current_focus(current_focus_path)
    decision_path = tmp_path / "decision-current.json"
    decision_path.write_text(
        json.dumps(
            {
                "actionable_items": [
                    {
                        "kind": "task",
                        "task_id": "task-1",
                        "status": "in_progress",
                        "to_agent": "worker-agent",
                        "repo": "CondorCommodore/home-lab",
                        "summary": "resume bounded slice",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "tabs": [
                    {
                        "tty": "/dev/ttys003",
                        "state": "running",
                        "role": "worker",
                        "registered": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    plan, envelope, _ = orchestrator.build_focus_dispatch_plan(
        manifest_path=manifest_path,
        current_focus_path=current_focus_path,
        decision_path=decision_path,
        report_dir=report_dir,
        state_path=state_path,
    )

    assert plan.ok is True
    assert plan.tty == "/dev/ttys003"
    assert envelope.task_id == "task-1"
    assert envelope.worker_id == "w1"
