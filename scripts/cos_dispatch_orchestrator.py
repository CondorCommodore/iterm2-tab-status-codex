#!/usr/bin/env python3
"""COS-side worker selection and dispatch orchestration."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cos_assignment_policy
import cos_dashboard
from c2_contract import ContractError, DispatchEnvelope, RunManifest, load_envelope, load_manifest
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
            reason=f"validated envelope assignment={envelope.assignment_id} worker={worker.worker_id}",
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
                        "error": "live dispatch requires --envelope and --manifest; legacy goal dispatch is dry-run only",
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
