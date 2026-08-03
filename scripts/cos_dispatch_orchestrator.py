#!/usr/bin/env python3
"""COS-side worker selection and dispatch orchestration."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cos_assignment_coordinator
import cos_assignment_policy
import cos_dashboard
from c2_contract import ContractError, DispatchEnvelope, RunManifest, load_envelope, load_manifest
from cos_current_actions import parse_current_focus_projection
from cos_iterm_edge_client import dispatch_envelope as dispatch_envelope_via_edge


@dataclass(frozen=True)
class DispatchPlan:
    ok: bool
    tty: str
    text: str
    reason: str
    dashboard_action: str
    dry_run_payload: str
    envelope_digest: str = ""
    transport: str = "legacy"


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _focus_task(
    decision: dict[str, object], *, focus_kind: str, focus_ref: str
) -> dict[str, object]:
    items = decision.get("actionable_items")
    if not isinstance(items, list):
        return {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if focus_kind == "task" and str(item.get("task_id") or item.get("id") or "") == focus_ref:
            return item
        if focus_kind == "pr" and str(item.get("pr_url") or item.get("id") or "") == focus_ref:
            return item
        if (
            focus_kind == "message"
            and str(item.get("message_id") or item.get("id") or "") == focus_ref
        ):
            return item
    return {}


def _worker_for_task(
    *,
    manifest: RunManifest,
    task: dict[str, object],
    dashboard: dict[str, object],
    target_host: str = "",
) -> tuple[str, str]:
    tabs = []
    for tab in dashboard["tabs"]["tabs"]:
        next_tab = dict(tab)
        for worker in manifest.workers:
            if worker.tty == next_tab.get("tty"):
                next_tab["registered"] = True
                next_tab["worker_id"] = worker.worker_id
                next_tab["coord_agent_id"] = worker.coord_agent_id
                next_tab["repositories"] = list(worker.repositories)
                break
        else:
            next_tab["registered"] = False
        tabs.append(next_tab)
    to_agent = str(task.get("to_agent") or "").strip()
    if to_agent:
        assigned_tabs = [
            tab for tab in tabs if str(tab.get("coord_agent_id") or "").strip() == to_agent
        ]
        assignment = cos_assignment_policy.choose_worker(
            assigned_tabs,
            target_host=target_host,
        )
        if assignment is None and str(task.get("status") or "").strip().lower() == "in_progress":
            running_tabs = [
                tab
                for tab in assigned_tabs
                if bool(tab.get("registered", True))
                and str(tab.get("state") or "unknown") == "running"
            ]
            if running_tabs:
                selected = sorted(
                    running_tabs,
                    key=lambda tab: cos_assignment_policy.rank_tab(
                        tab,
                        cos_assignment_policy.DEFAULT_POLICY,
                        target_host=target_host,
                    ),
                )[0]
                assignment = cos_assignment_policy.Assignment(
                    tty=str(selected["tty"]),
                    reason=(
                        f"task already in_progress on assigned running worker principal {to_agent}"
                    ),
                    tab=selected,
                )
        if assignment is None:
            raise ContractError(
                f"assigned worker principal {to_agent} has no live eligible registered tab"
            )
        worker_id = str(assignment.tab.get("worker_id") or "").strip()
        if not worker_id:
            raise ContractError("assigned dashboard worker is not registered in the manifest")
        return worker_id, f"task already assigned to worker principal {to_agent}"
    assignment = cos_assignment_policy.choose_worker(tabs, target_host=target_host)
    if assignment is None:
        raise ContractError("no eligible registered worker tab")
    worker_id = str(assignment.tab.get("worker_id") or "").strip()
    if not worker_id:
        raise ContractError("selected dashboard worker is not registered in the manifest")
    return worker_id, assignment.reason


def build_goal_text(goal: str) -> str:
    goal = goal.strip()
    if goal.startswith("/goal "):
        return goal
    return f"/goal {goal}"


def build_dispatch_plan(
    *,
    goal: str,
    state_path: Path,
    report_dir: Path,
    target_host: str = "",
    cos_tty: str = "",
) -> DispatchPlan:
    dashboard = cos_dashboard.build_dashboard(
        state_path=state_path,
        iterm_live_state_path=state_path,
        report_dir=report_dir,
    )
    tabs = []
    for tab in dashboard["tabs"]["tabs"]:
        next_tab = dict(tab)
        if cos_tty and next_tab.get("tty") == cos_tty:
            next_tab["role"] = "cos"
        tabs.append(next_tab)
    assignment = cos_assignment_policy.choose_worker(
        tabs,
        target_host=target_host,
    )
    if assignment is None:
        return DispatchPlan(
            ok=False,
            tty="",
            text=build_goal_text(goal),
            reason="no eligible worker tab",
            dashboard_action="; ".join(dashboard["recommended_actions"]),
            dry_run_payload="",
        )
    text = build_goal_text(goal)
    return DispatchPlan(
        ok=True,
        tty=assignment.tty,
        text=text,
        reason=assignment.reason,
        dashboard_action="; ".join(dashboard["recommended_actions"]),
        dry_run_payload=repr(f"{text}\n"),
        transport="legacy-dry-run",
    )


def build_envelope_dispatch_plan(
    *,
    envelope_path: Path,
    manifest_path: Path,
    report_dir: Path,
    state_path: Path,
) -> tuple[DispatchPlan, DispatchEnvelope, RunManifest]:
    """Validate a complete V1 envelope before asking the edge to dispatch it."""
    manifest = load_manifest(manifest_path)
    envelope = load_envelope(envelope_path)
    worker = envelope.validate_for(manifest)
    dashboard = cos_dashboard.build_dashboard(
        state_path=state_path,
        iterm_live_state_path=state_path,
        report_dir=report_dir,
    )
    return (
        DispatchPlan(
            ok=True,
            tty=worker.tty,
            text=envelope.objective,
            reason=(
                f"validated envelope assignment={envelope.assignment_id} worker={worker.worker_id}"
            ),
            dashboard_action="; ".join(dashboard["recommended_actions"]),
            dry_run_payload=envelope.canonical_json(),
            envelope_digest=envelope.digest(),
            transport=manifest.transport_for(envelope.assignment_id),
        ),
        envelope,
        manifest,
    )


def build_focus_dispatch_plan(
    *,
    manifest_path: Path,
    current_focus_path: Path,
    decision_path: Path,
    report_dir: Path,
    state_path: Path,
    target_host: str = "",
    authorization_limits: tuple[str, ...] = ("no-deploy", "no-merge"),
) -> tuple[DispatchPlan, DispatchEnvelope, RunManifest]:
    manifest = load_manifest(manifest_path)
    focus = parse_current_focus_projection(current_focus_path, manifest=manifest)
    decision = _load_json(decision_path)
    dashboard = cos_dashboard.build_dashboard(
        state_path=state_path,
        iterm_live_state_path=state_path,
        report_dir=report_dir,
    )
    if focus.focus_kind != "task":
        raise ContractError(
            f"current focus kind {focus.focus_kind!r} is not dispatchable as a task envelope"
        )
    task = _focus_task(decision, focus_kind=focus.focus_kind, focus_ref=focus.focus_ref)
    if not task:
        raise ContractError("current focus task is missing from the ordered actionable feed")
    worker_id, selection_reason = _worker_for_task(
        manifest=manifest,
        task=task,
        dashboard=dashboard,
        target_host=target_host,
    )
    cos_assignment_coordinator.preflight_candidate(
        manifest=manifest,
        task=task,
        worker_id=worker_id,
    )
    generation = int(
        focus.header.get("plan_generation") or focus.header.get("action_generation") or 1
    )
    envelope = cos_assignment_coordinator.build_envelope(
        manifest=manifest,
        task=task,
        worker_id=worker_id,
        controller_epoch=focus.controller_epoch,
        generation=generation,
        authorization_limits=list(authorization_limits),
        plan_id=str(focus.header.get("focus_source") or "current-focus"),
        direction_digest=str(focus.header.get("direction_digest") or ""),
    )
    worker = envelope.validate_for(manifest)
    return (
        DispatchPlan(
            ok=True,
            tty=worker.tty,
            text=envelope.objective,
            reason=f"focused dispatch selected {worker.worker_id}; {selection_reason}",
            dashboard_action="; ".join(dashboard["recommended_actions"]),
            dry_run_payload=envelope.canonical_json(),
            envelope_digest=envelope.digest(),
            transport=manifest.transport_for(envelope.assignment_id),
        ),
        envelope,
        manifest,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or run COS worker dispatch.")
    parser.add_argument("--goal", default="")
    parser.add_argument("--state-path", type=Path, default=cos_dashboard.DEFAULT_STATE_PATH)
    parser.add_argument("--report-dir", type=Path, default=cos_dashboard.DEFAULT_REPORT_DIR)
    parser.add_argument("--target-host", default="")
    parser.add_argument("--cos-tty", default="")
    parser.add_argument(
        "--envelope",
        type=Path,
        help="complete V1 dispatch envelope; required for live dispatch",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="run manifest used to validate --envelope",
    )
    parser.add_argument(
        "--current-focus",
        type=Path,
        help="validated current-focus projection",
    )
    parser.add_argument(
        "--decision",
        type=Path,
        help="decision-current.json for ordered actionable feed",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    envelope = None
    manifest = None
    if args.envelope:
        if not args.manifest:
            print(json.dumps({"ok": False, "error": "--manifest is required with --envelope"}))
            return 2
        try:
            plan, envelope, manifest = build_envelope_dispatch_plan(
                envelope_path=args.envelope,
                manifest_path=args.manifest,
                report_dir=args.report_dir,
                state_path=args.state_path,
            )
        except ContractError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 2
    elif args.current_focus or args.decision:
        if not args.current_focus or not args.decision or not args.manifest:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "--current-focus, --decision, and --manifest must be provided together"
                        ),
                    }
                )
            )
            return 2
        try:
            plan, envelope, manifest = build_focus_dispatch_plan(
                manifest_path=args.manifest,
                current_focus_path=args.current_focus,
                decision_path=args.decision,
                report_dir=args.report_dir,
                state_path=args.state_path,
                target_host=args.target_host,
            )
        except ContractError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 2
    else:
        if not args.goal.strip():
            print(json.dumps({"ok": False, "error": "--goal is required without --envelope"}))
            return 2
        plan = build_dispatch_plan(
            goal=args.goal,
            state_path=args.state_path,
            report_dir=args.report_dir,
            target_host=args.target_host,
            cos_tty=args.cos_tty,
        )
        if not args.dry_run:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "live dispatch requires --envelope and --manifest; "
                            "legacy goal dispatch is dry-run only"
                        ),
                        "plan": asdict(plan),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
    if args.dry_run or not plan.ok:
        print(json.dumps(asdict(plan), indent=2, sort_keys=True))
        return 0 if plan.ok else 1

    assert envelope is not None and manifest is not None
    try:
        result = dispatch_envelope_via_edge(envelope.__dict__)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps({"plan": asdict(plan), "dispatch": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
