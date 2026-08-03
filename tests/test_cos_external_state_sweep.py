from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_external_state_sweep as sweep  # noqa: E402


def test_extract_targets_keeps_pr_and_task_coordinates():
    targets = sweep.extract_targets(
        [
            {"kind": "pr", "ref": "https://github.com/acme/repo/pull/21"},
            {
                "kind": "task",
                "task_id": "task-1",
                "pr_url": "https://github.com/acme/repo/pull/21",
                "branch_repo": "acme/repo",
                "branch_name": "feature/test",
                "head_sha": "a" * 40,
            },
        ]
    )

    assert targets[0] == {"kind": "pr", "pr_url": "https://github.com/acme/repo/pull/21"}
    assert targets[1]["task_id"] == "task-1"
    assert targets[1]["repo"] == "acme/repo"
    assert targets[1]["branch"] == "feature/test"


def test_sweep_reports_closed_pr_deleted_branch_and_head_drift():
    def probe(kind: str, target: dict[str, object]) -> dict[str, object]:
        if kind == "pr":
            return {"ok": True, "exists": True, "state": "CLOSED", "head_oid": "b" * 40}
        if kind == "branch":
            return {"ok": True, "exists": False}
        raise AssertionError(kind)

    result = sweep.sweep(
        items=[
            {
                "kind": "task",
                "task_id": "task-1",
                "pr_url": "https://github.com/acme/repo/pull/21",
                "branch_repo": "acme/repo",
                "branch_name": "feature/test",
                "head_sha": "a" * 40,
            }
        ],
        probe=probe,
        now_ts=100,
    )

    assert result["blocked"] is True
    assert result["finding_count"] == 3
    assert {item["kind"] for item in result["findings"]} == {
        "tracked_pr_closed_unattributed",
        "tracked_branch_missing_unattributed",
        "tracked_head_drift_unattributed",
    }


def test_sweep_is_clear_when_world_matches_tracked_coordinates():
    def probe(kind: str, target: dict[str, object]) -> dict[str, object]:
        if kind == "pr":
            return {"ok": True, "exists": True, "state": "OPEN", "head_oid": "a" * 40}
        if kind == "branch":
            return {"ok": True, "exists": True, "head_oid": "a" * 40}
        raise AssertionError(kind)

    result = sweep.sweep(
        items=[
            {
                "kind": "task",
                "task_id": "task-1",
                "pr_url": "https://github.com/acme/repo/pull/21",
                "branch_repo": "acme/repo",
                "branch_name": "feature/test",
                "head_sha": "a" * 40,
            }
        ],
        probe=probe,
        now_ts=100,
    )

    assert result["blocked"] is False
    assert result["findings"] == []
