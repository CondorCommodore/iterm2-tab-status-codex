from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_report_parser as parser  # noqa: E402


def test_parse_report_extracts_status_entities_and_next_step(tmp_path):
    report = tmp_path / "worker-ttys003-20260625.md"
    report.write_text(
        """# Task Done

Merged PR #123 at abcdef123456.
Task T-3345 complete.

## Blocker
None.

## Next Step
Deploy on MacBook.
""",
        encoding="utf-8",
    )

    parsed = parser.parse_report(report)

    assert parsed.status == "complete"
    assert parsed.prs == ["123"]
    assert "T-3345" in parsed.tasks
    assert parsed.tty == "ttys003"
    assert parsed.next_step == "Deploy on MacBook."


def test_parse_report_detects_decision_and_blocked(tmp_path):
    report = tmp_path / "decision.md"
    report.write_text("BLOCKED: needs operator decision on PR #9", encoding="utf-8")

    parsed = parser.parse_report(report)

    assert parsed.status == "blocked"
    assert parsed.needs_decision is True
    assert parsed.prs == ["9"]
