from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_assignment_policy as policy  # noqa: E402


def test_choose_worker_prefers_idle_non_cos():
    assignment = policy.choose_worker(
        [
            {"tty": "/dev/ttys001", "state": "idle", "role": "cos"},
            {"tty": "/dev/ttys002", "state": "running", "role": "worker"},
            {"tty": "/dev/ttys003", "state": "idle", "role": "worker"},
        ]
    )

    assert assignment is not None
    assert assignment.tty == "/dev/ttys003"


def test_choose_worker_avoids_attention_tabs():
    assignment = policy.choose_worker(
        [
            {"tty": "/dev/ttys001", "state": "attention", "role": "worker"},
            {"tty": "/dev/ttys002", "state": "idle", "role": "worker"},
        ]
    )

    assert assignment is not None
    assert assignment.tty == "/dev/ttys002"


def test_choose_worker_can_bias_target_host():
    assignment = policy.choose_worker(
        [
            {"tty": "/dev/ttys001", "state": "idle", "role": "worker", "project": "macbook"},
            {"tty": "/dev/ttys002", "state": "idle", "role": "worker", "project": "forge"},
        ],
        target_host="forge",
    )

    assert assignment is not None
    assert assignment.tty == "/dev/ttys002"
