#!/usr/bin/env python3
"""COS-side worker selection and dispatch orchestration."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cos_assignment_policy
import cos_dashboard
import cos_tab_dispatch


@dataclass(frozen=True)
class DispatchPlan:
    ok: bool
    tty: str
    text: str
    reason: str
    dashboard_action: str
    dry_run_payload: str


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
    request = cos_tab_dispatch.DispatchRequest(
        tty=assignment.tty,
        text=text,
    )
    return DispatchPlan(
        ok=True,
        tty=assignment.tty,
        text=text,
        reason=assignment.reason,
        dashboard_action="; ".join(dashboard["recommended_actions"]),
        dry_run_payload=repr(cos_tab_dispatch.payload_for_request(request)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or run COS worker dispatch.")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--state-path", type=Path, default=cos_dashboard.DEFAULT_STATE_PATH)
    parser.add_argument("--report-dir", type=Path, default=cos_dashboard.DEFAULT_REPORT_DIR)
    parser.add_argument("--target-host", default="")
    parser.add_argument("--cos-tty", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    plan = build_dispatch_plan(
        goal=args.goal,
        state_path=args.state_path,
        report_dir=args.report_dir,
        target_host=args.target_host,
        cos_tty=args.cos_tty,
    )
    if args.dry_run or not plan.ok:
        print(json.dumps(asdict(plan), indent=2, sort_keys=True))
        return 0 if plan.ok else 1

    # Live dispatch must be run from iTerm2's Python runtime.
    try:
        import iterm2
    except ImportError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "iterm2 module unavailable; rerun inside iTerm2 Python runtime",
                    "plan": asdict(plan),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    async def _run(connection: object) -> None:
        result = await cos_tab_dispatch.dispatch(
            connection,
            cos_tab_dispatch.DispatchRequest(
                tty=plan.tty,
                text=plan.text,
            ),
        )
        print(json.dumps({"plan": asdict(plan), "dispatch": result}, indent=2, sort_keys=True))

    iterm2.run_until_complete(_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
