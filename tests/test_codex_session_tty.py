"""Regression tests for per-process Codex tty lookup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_session as cs  # noqa: E402


def test_live_codex_process_discovery_reads_tty_per_pid(monkeypatch):
    bulk_ps = (
        "  1111 Mon May 28 19:20:01 2026 codex\n"
        "  2222 Mon May 28 19:21:01 2026 codex --resume foo\n"
    )
    calls = []

    def fake_check_output(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["ps", "-axo", "pid=,lstart=,command="]:
            return bulk_ps
        if cmd == ["ps", "-p", "1111", "-o", "tty="]:
            return "ttys010\n"
        if cmd == ["ps", "-p", "2222", "-o", "tty="]:
            return "ttys011\n"
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(cs.subprocess, "check_output", fake_check_output)

    procs = cs._ps_codex_procs()

    assert [(p.pid, p.tty) for p in procs] == [
        (1111, "/dev/ttys010"),
        (2222, "/dev/ttys011"),
    ]
    assert ["ps", "-p", "1111", "-o", "tty="] in calls
    assert ["ps", "-p", "2222", "-o", "tty="] in calls


@pytest.mark.parametrize("raw_tty", ["?", "??", "-", ""])
def test_live_codex_process_discovery_skips_pid_without_tty(monkeypatch, raw_tty):
    bulk_ps = "  3333 Mon May 28 19:22:01 2026 codex\n"

    def fake_check_output(cmd, **kwargs):
        if cmd == ["ps", "-axo", "pid=,lstart=,command="]:
            return bulk_ps
        if cmd == ["ps", "-p", "3333", "-o", "tty="]:
            return raw_tty + "\n"
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(cs.subprocess, "check_output", fake_check_output)

    assert cs._ps_codex_procs() == []
