#!/usr/bin/env python3
"""Print a compact COS dashboard from tab state and fleet reports."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import cos_report_parser

DEFAULT_STATE_PATH = (
    Path.home() / ".claude" / "plans" / "fleet-reports" / "tab-state-current.json"
)
DEFAULT_ITERM_LIVE_STATE_PATH = (
    Path.home() / ".claude" / "plans" / "fleet-reports" / "iterm-live-state.json"
)
DEFAULT_REPORT_DIR = Path.home() / ".claude" / "plans" / "fleet-reports"


def load_tab_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"tabs": [], "summary": {}}
    return payload if isinstance(payload, dict) else {"tabs": [], "summary": {}}


def summarize_tabs(state: dict[str, Any]) -> dict[str, Any]:
    tabs = [tab for tab in state.get("tabs", []) if isinstance(tab, dict)]
    if not tabs and isinstance(state.get("sessions"), list):
        tabs = [
            {
                "tty": session.get("tty", ""),
                "state": session.get("readiness", "unknown"),
                "runtime": session.get("runtime", "unknown"),
                "role": session.get("role", "worker"),
                "project": Path(str(session.get("cwd") or "")).name,
                "cwd": session.get("cwd", ""),
                "age_seconds": None,
            }
            for session in state.get("sessions", [])
            if isinstance(session, dict)
        ]
    by_state: dict[str, int] = {}
    for tab in tabs:
        tab_state = str(tab.get("state") or "unknown")
        by_state[tab_state] = by_state.get(tab_state, 0) + 1
    return {
        "active_tabs": len(tabs),
        "by_state": by_state,
        "tabs": [
            {
                "tty": tab.get("tty", ""),
                "state": tab.get("state", "unknown"),
                "runtime": tab.get("runtime", "unknown"),
                "role": tab.get("role", tab.get("user.cosRole", "worker")),
                "project": tab.get("project", ""),
                "cwd": tab.get("cwd", ""),
                "age_seconds": tab.get("age_seconds"),
            }
            for tab in tabs
        ],
    }


def recommended_actions(dashboard: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    reports = dashboard["reports"]
    tabs = dashboard["tabs"]["tabs"]
    if any(report["status"] == "blocked" for report in reports):
        actions.append("review blocked fleet reports")
    if any(report["needs_decision"] for report in reports):
        actions.append("answer operator-decision report")
    if any(tab["state"] == "attention" for tab in tabs):
        actions.append("inspect attention tab")
    idle_workers = [
        tab
        for tab in tabs
        if tab["state"] == "idle" and str(tab.get("role") or "worker") != "cos"
    ]
    if idle_workers:
        actions.append(f"dispatch next /goal to {idle_workers[0]['tty']}")
    if not actions:
        actions.append("monitor; no immediate COS action inferred")
    return actions


def build_dashboard(
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    iterm_live_state_path: Path = DEFAULT_ITERM_LIVE_STATE_PATH,
    report_dir: Path = DEFAULT_REPORT_DIR,
    report_limit: int = 10,
) -> dict[str, Any]:
    state = load_tab_state(iterm_live_state_path)
    selected_state_path = iterm_live_state_path
    if not state.get("sessions"):
        state = load_tab_state(state_path)
        selected_state_path = state_path
    reports = [asdict(report) for report in cos_report_parser.recent_reports(report_dir, limit=report_limit)]
    dashboard = {
        "state_path": str(selected_state_path),
        "report_dir": str(report_dir),
        "generated_at": state.get("generated_at"),
        "tabs": summarize_tabs(state),
        "reports": reports,
    }
    dashboard["recommended_actions"] = recommended_actions(dashboard)
    return dashboard


def render_text(dashboard: dict[str, Any]) -> str:
    lines = [
        f"COS dashboard generated_at={dashboard.get('generated_at')}",
        f"tabs active={dashboard['tabs']['active_tabs']} by_state={dashboard['tabs']['by_state']}",
    ]
    for tab in dashboard["tabs"]["tabs"]:
        lines.append(
            "tab {tty} {state} {runtime} project={project} age={age_seconds}".format(**tab)
        )
    lines.append("recent reports:")
    for report in dashboard["reports"][:5]:
        lines.append(
            "- {status} {name} prs={prs} tasks={tasks} decision={needs_decision}".format(
                **report
            )
        )
    lines.append("recommended actions:")
    for action in dashboard["recommended_actions"]:
        lines.append(f"- {action}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a COS tab/report dashboard.")
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--iterm-live-state-path", type=Path, default=DEFAULT_ITERM_LIVE_STATE_PATH)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--report-limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    dashboard = build_dashboard(
        state_path=args.state_path,
        iterm_live_state_path=args.iterm_live_state_path,
        report_dir=args.report_dir,
        report_limit=args.report_limit,
    )
    if args.json:
        print(json.dumps(dashboard, indent=2, sort_keys=True))
    else:
        print(render_text(dashboard))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
