from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_tab_trigger_event as trigger  # noqa: E402


def test_classify_line_matches_worker_outcomes():
    assert trigger.classify_line("DONE: report written") == "done"
    assert trigger.classify_line("BLOCKED on auth") == "blocked"
    assert trigger.classify_line("APPROVE") == "approve"
    assert trigger.classify_line("REJECT: unsafe diff") == "reject"
    assert trigger.classify_line("Report: /tmp/x.md") == "report"


def test_classify_line_matches_failure_signals():
    assert trigger.classify_line("Traceback (most recent call last)") == "traceback"
    assert trigger.classify_line("FAILED tests/test_x.py") == "failed"
    assert trigger.classify_line("hit rate limit 403") == "rate_limit"
    assert trigger.classify_line("CONFLICT (content): Merge conflict") == "merge_conflict"


def test_build_event_truncates_line_and_includes_context():
    line = "DONE " + ("x" * 700)
    event = trigger.build_event(line, tty="/dev/ttys001", cwd="/Users/mikebook/code", now_ts=1)

    assert event is not None
    assert event["trigger"] == "done"
    assert event["tty"] == "/dev/ttys001"
    assert event["cwd"] == "/Users/mikebook/code"
    assert len(event["line"]) == 500


def test_append_event_writes_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    trigger.append_event(path, {"event": "trigger_match", "trigger": "done"})

    assert json.loads(path.read_text(encoding="utf-8"))["trigger"] == "done"
