"""Unit tests for codex_session classifier + helpers.

Pure functions only — no iTerm2 or subprocess.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Make scripts/ importable.
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_session as cs  # noqa: E402


# --- classify_codex_state ----------------------------------------------------


def _ev(payload_type: str, ts_offset: float = 0.0, top: str = "event_msg") -> dict:
    ts = time.gmtime(time.time() + ts_offset)
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", ts),
        "type": top,
        "payload": {"type": payload_type},
    }


def test_empty_events_idle():
    assert cs.classify_codex_state([]) == cs.STATE_IDLE


def test_task_started_only_running():
    evs = [_ev("user_message", -2), _ev("task_started", -1)]
    assert cs.classify_codex_state(evs, now=time.time()) == cs.STATE_RUNNING


def test_task_complete_after_started_idle_when_quiet():
    # task_complete is recent but quiet for > idle_after.
    evs = [_ev("task_started", -120), _ev("task_complete", -100)]
    assert cs.classify_codex_state(evs, now=time.time(), idle_after=30) == cs.STATE_IDLE


def test_task_complete_recent_still_idle():
    # Even a recent task_complete reports idle (task is done).
    evs = [_ev("task_started", -10), _ev("task_complete", -1)]
    assert cs.classify_codex_state(evs, now=time.time(), idle_after=30) == cs.STATE_IDLE


def test_agent_message_recent_running():
    evs = [_ev("task_started", -5), _ev("agent_message", -1)]
    assert cs.classify_codex_state(evs, now=time.time()) == cs.STATE_RUNNING


def test_agent_message_stale_idle():
    evs = [_ev("task_started", -200), _ev("agent_message", -120)]
    assert cs.classify_codex_state(evs, now=time.time(), idle_after=30) == cs.STATE_IDLE


def test_turn_aborted_latest_maps_to_idle():
    # Aborted shouldn't be 'attention' (which means permission prompt).
    evs = [_ev("task_started", -5), _ev("turn_aborted", -1)]
    assert cs.classify_codex_state(evs, now=time.time()) == cs.STATE_IDLE


def test_started_after_complete_running():
    # New turn started after previous complete -> running.
    evs = [
        _ev("task_started", -100),
        _ev("task_complete", -90),
        _ev("user_message", -5),
        _ev("task_started", -4),
    ]
    assert cs.classify_codex_state(evs, now=time.time()) == cs.STATE_RUNNING


# --- _ps_codex_procs ---------------------------------------------------------


def test_ps_codex_procs_matches_bare_codex():
    ps_out = "  1234 ttys001 Mon May 28 19:20:01 2026 codex\n"
    procs = cs._ps_codex_procs(ps_out)
    assert len(procs) == 1
    assert procs[0].pid == 1234
    assert procs[0].tty == "/dev/ttys001"


def test_ps_codex_procs_matches_codex_with_args():
    ps_out = "  5678 ttys002 Mon May 28 19:21:01 2026 codex --resume foo\n"
    procs = cs._ps_codex_procs(ps_out)
    assert len(procs) == 1
    assert procs[0].pid == 5678


def test_ps_codex_procs_ignores_grep_and_editors():
    ps_out = (
        "  100 ttys001 Mon May 28 19:20:01 2026 grep codex\n"
        "  101 ttys002 Mon May 28 19:20:01 2026 vim codex_session.py\n"
        "  102 ttys003 Mon May 28 19:20:01 2026 /usr/local/bin/codex\n"
    )
    procs = cs._ps_codex_procs(ps_out)
    assert [p.pid for p in procs] == [102]


def test_ps_codex_procs_node_wrapping_codex():
    ps_out = "  200 ttys004 Mon May 28 19:20:01 2026 node /opt/x/codex --port 3000\n"
    procs = cs._ps_codex_procs(ps_out)
    assert [p.pid for p in procs] == [200]


def test_ps_codex_procs_skips_no_tty():
    ps_out = (
        "  300 ?? Mon May 28 19:20:01 2026 codex\n"
        "  301 ttys005 Mon May 28 19:20:01 2026 codex\n"
    )
    procs = cs._ps_codex_procs(ps_out)
    assert [p.pid for p in procs] == [301]


# --- rollout matching --------------------------------------------------------


def test_match_rollout_picks_newest_after_proc_start(tmp_path):
    older = (
        tmp_path
        / "rollout-2026-05-26T10-00-00-aaaaaaaa-1111-2222-3333-444444444444.jsonl"
    )
    newer = (
        tmp_path
        / "rollout-2026-05-26T12-00-00-bbbbbbbb-1111-2222-3333-444444444444.jsonl"
    )
    older.write_text("{}\n")
    newer.write_text("{}\n")
    older_mtime = time.time() - 3600
    newer_mtime = time.time() - 60
    import os as _os
    _os.utime(older, (older_mtime, older_mtime))
    _os.utime(newer, (newer_mtime, newer_mtime))

    proc = cs.CodexProc(pid=1, tty="/dev/ttys001", started=time.time() - 120)
    picked = cs.match_rollout_for_proc(proc, [newer, older])
    assert picked == newer


def test_match_rollout_none_if_all_older_than_proc(tmp_path):
    r = (
        tmp_path
        / "rollout-2026-05-26T10-00-00-aaaaaaaa-1111-2222-3333-444444444444.jsonl"
    )
    r.write_text("{}\n")
    import os as _os
    _os.utime(r, (time.time() - 3600, time.time() - 3600))
    proc = cs.CodexProc(pid=1, tty="/dev/ttys001", started=time.time() - 60)
    assert cs.match_rollout_for_proc(proc, [r]) is None


def test_match_rollout_falls_back_when_proc_start_unknown(tmp_path):
    r = (
        tmp_path
        / "rollout-2026-05-26T10-00-00-aaaaaaaa-1111-2222-3333-444444444444.jsonl"
    )
    r.write_text("{}\n")
    proc = cs.CodexProc(pid=1, tty="/dev/ttys001", started=0.0)
    assert cs.match_rollout_for_proc(proc, [r]) == r


# --- signal building ---------------------------------------------------------


def test_session_id_from_rollout():
    p = Path("rollout-2026-05-26T10-48-07-019e64c1-dcf5-7f11-b45c-e2c8ea952035.jsonl")
    assert (
        cs._session_id_from_rollout(p)
        == "codex-019e64c1-dcf5-7f11-b45c-e2c8ea952035"
    )


def test_build_signal_shape():
    proc = cs.CodexProc(pid=1234, tty="/dev/ttys001", started=time.time())
    rollout = Path(
        "rollout-2026-05-26T10-48-07-019e64c1-dcf5-7f11-b45c-e2c8ea952035.jsonl"
    )
    head = [
        {"type": "session_meta", "payload": {"cwd": "/Users/x/code/foo"}},
    ]
    sig = cs.build_signal(proc, rollout, cs.STATE_RUNNING, head)
    assert sig["session_id"] == "codex-019e64c1-dcf5-7f11-b45c-e2c8ea952035"
    assert sig["type"] == "running"
    assert sig["tty"] == "/dev/ttys001"
    assert sig["pid"] == "1234"
    assert sig["cwd"] == "/Users/x/code/foo"
    assert sig["project"] == "foo"
    assert sig["runtime"] == "codex"
    assert isinstance(sig["ts"], str)


def test_write_signal_atomic(tmp_path):
    proc = cs.CodexProc(pid=1, tty="/dev/ttys001", started=time.time())
    rollout = Path(
        "rollout-2026-05-26T10-48-07-019e64c1-dcf5-7f11-b45c-e2c8ea952035.jsonl"
    )
    sig = cs.build_signal(proc, rollout, cs.STATE_IDLE)
    out = cs.write_signal(str(tmp_path), sig)
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["session_id"] == sig["session_id"]
    assert data["type"] == "idle"


# --- end-to-end via fake rollout --------------------------------------------


def test_sweep_emits_signal_for_codex_proc(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions" / "2026" / "05" / "28"
    sessions.mkdir(parents=True)
    rollout = (
        sessions
        / "rollout-2026-05-28T10-00-00-019e64c1-dcf5-7f11-b45c-e2c8ea952035.jsonl"
    )
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    rollout.write_text(
        json.dumps({"timestamp": now_iso, "type": "session_meta",
                    "payload": {"cwd": "/Users/x/code/foo"}}) + "\n"
        + json.dumps({"timestamp": now_iso, "type": "event_msg",
                      "payload": {"type": "task_started"}}) + "\n"
    )
    fake_started = time.time() - 30
    started_text = time.strftime(
        "%a %b %d %H:%M:%S %Y",
        time.localtime(fake_started),
    )
    fake_ps = f"  9999 {started_text} codex\n"

    def fake_check_output(cmd, **kwargs):
        if cmd == ["ps", "-axo", "pid=,lstart=,command="]:
            return fake_ps
        if cmd == ["ps", "-p", "9999", "-o", "tty="]:
            return "ttys001\n"
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(cs.subprocess, "check_output", fake_check_output)
    signal_dir = tmp_path / "signals"
    written = cs.sweep_once(str(signal_dir), str(tmp_path / "sessions"))
    assert len(written) == 1
    data = json.loads(written[0].read_text())
    assert data["type"] == "running"
    assert data["session_id"].startswith("codex-019e64c1")
    assert data["tty"] == "/dev/ttys001"
    assert data["pid"] == "9999"
