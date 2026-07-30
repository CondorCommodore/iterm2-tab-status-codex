from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from c2_contract import ContractError  # noqa: E402
from c2_runtime_hook import RuntimeHookRecord, load_record, write_record  # noqa: E402


@pytest.mark.parametrize(
    ("runtime", "event", "payload", "profile", "prompt_state"),
    [
        ("codex", "turn-ended", {"thread_id": "cli-codex"}, "codex-cli", "ready"),
        ("claude", "stop", {"session_id": "cli-claude"}, "claude-code", "ready"),
        ("codex", "session-start", {"sessionId": "cli-codex"}, "codex-cli", "running"),
    ],
)
def test_hook_payloads_publish_inert_runtime_profiles(
    runtime, event, payload, profile, prompt_state
):
    cli_session_id = next(iter(payload.values()))
    record = RuntimeHookRecord.from_hook(
        runtime=runtime,
        event=event,
        iterm_session_id=f"iterm-{runtime}",
        tty="/dev/ttys003",
        cli_session_id=cli_session_id,
        coord_session_id=f"coord-{runtime}",
        payload=payload,
        observed_ts=1000.0,
    )

    assert record.profile_id == profile
    assert record.prompt_state == prompt_state
    assert record.input_buffer_state == "unknown"


def test_hook_rejects_missing_or_wrong_payload_session_identity():
    kwargs = {
        "runtime": "codex",
        "event": "turn-ended",
        "iterm_session_id": "iterm-codex",
        "tty": "/dev/ttys003",
        "cli_session_id": "cli-codex",
        "coord_session_id": "coord-codex",
        "observed_ts": 1000.0,
    }
    with pytest.raises(ContractError, match="no runtime session identity"):
        RuntimeHookRecord.from_hook(payload={}, **kwargs)
    with pytest.raises(ContractError, match="different cli session"):
        RuntimeHookRecord.from_hook(payload={"session_id": "reused-cli"}, **kwargs)


def test_hook_contract_cannot_assert_empty_input_buffer():
    record = RuntimeHookRecord.from_hook(
        runtime="codex",
        event="turn-ended",
        iterm_session_id="iterm-codex",
        tty="/dev/ttys003",
        cli_session_id="cli-codex",
        coord_session_id="coord-codex",
        payload={"session_id": "cli-codex"},
        observed_ts=1000.0,
    )
    value = asdict(record)
    value["input_buffer_state"] = "empty"
    with pytest.raises(ContractError, match="cannot assert"):
        RuntimeHookRecord.from_dict(value)


def test_atomic_cache_is_private_and_exactly_bound(tmp_path):
    record = RuntimeHookRecord.from_hook(
        runtime="claude",
        event="stop",
        iterm_session_id="iterm-claude",
        tty="/dev/ttys004",
        cli_session_id="cli-claude",
        coord_session_id="coord-claude",
        payload={"conversation_id": "cli-claude"},
        observed_ts=1000.0,
    )

    path = write_record(record, tmp_path)

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        load_record(
            tmp_path,
            iterm_session_id="iterm-claude",
            tty="/dev/ttys004",
            now_ts=1050.0,
        )
        == record
    )
    assert (
        load_record(
            tmp_path,
            iterm_session_id="iterm-claude",
            tty="/dev/ttys099",
            now_ts=1050.0,
        )
        is None
    )


def test_stale_corrupt_or_nonprivate_cache_is_ignored(tmp_path):
    record = RuntimeHookRecord.from_hook(
        runtime="codex",
        event="turn-ended",
        iterm_session_id="iterm-codex",
        tty="/dev/ttys003",
        cli_session_id="cli-codex",
        coord_session_id="coord-codex",
        payload={"session_id": "cli-codex"},
        observed_ts=1000.0,
    )
    path = write_record(record, tmp_path)
    assert (
        load_record(
            tmp_path,
            iterm_session_id="iterm-codex",
            tty="/dev/ttys003",
            now_ts=1201.0,
        )
        is None
    )

    path.write_text("not json\n", encoding="utf-8")
    assert (
        load_record(
            tmp_path,
            iterm_session_id="iterm-codex",
            tty="/dev/ttys003",
            now_ts=1050.0,
        )
        is None
    )

    path.write_text(json.dumps(asdict(record)), encoding="utf-8")
    os.chmod(path, 0o644)
    assert (
        load_record(
            tmp_path,
            iterm_session_id="iterm-codex",
            tty="/dev/ttys003",
            now_ts=1050.0,
        )
        is None
    )


def test_state_directory_symlink_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    record = RuntimeHookRecord.from_hook(
        runtime="codex",
        event="turn-ended",
        iterm_session_id="iterm-codex",
        tty="/dev/ttys003",
        cli_session_id="cli-codex",
        coord_session_id="coord-codex",
        payload={"session_id": "cli-codex"},
        observed_ts=1000.0,
    )

    with pytest.raises(ContractError, match="real directory"):
        write_record(record, link)
    assert (
        load_record(
            link,
            iterm_session_id="iterm-codex",
            tty="/dev/ttys003",
            now_ts=1050.0,
        )
        is None
    )
