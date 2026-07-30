#!/usr/bin/env python3
"""Publish inert Codex/Claude lifecycle observations for the iTerm C2 daemon.

This adapter is source-only until an operator separately installs a runtime
hook. It never sends terminal input and never claims that the TUI input buffer
is empty; all published records use ``input_buffer_state=unknown``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import secrets
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from c2_contract import RUNTIME_OBSERVATION_PROFILES, TTY_RE, ContractError
from c2_runtime_observation import RuntimeObservation

RUNTIME_HOOK_SCHEMA_VERSION = 1
DEFAULT_STATE_DIR = Path.home() / ".local/state/cos-c2/runtime-observations"
DEFAULT_MAX_AGE_SECONDS = 120.0
EVENT_PROMPT_STATES = {
    "session-start": "running",
    "turn-start": "running",
    "post-tool": "running",
    "stop": "ready",
    "turn-ended": "ready",
}
PROFILE_BY_RUNTIME = {
    "codex": ("codex-cli", 1),
    "claude": ("claude-code", 1),
}

HOOK_SCHEMA_VERSION = 1
MAX_HOOK_AGE_SECONDS = 15.0
BrokerVerifier = Callable[[dict[str, Any]], dict[str, Any]]


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{name} is required")
    return text


def _payload_session_id(payload: dict[str, Any]) -> str:
    for name in ("session_id", "sessionId", "thread_id", "conversation_id"):
        value = str(payload.get(name) or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class RuntimeHookRecord:
    schema_version: int
    runtime: str
    profile_id: str
    profile_version: int
    event: str
    prompt_state: str
    input_buffer_state: str
    iterm_session_id: str
    tty: str
    cli_session_id: str
    coord_session_id: str
    observed_ts: float

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeHookRecord":
        schema_version = value.get("schema_version")
        if schema_version != RUNTIME_HOOK_SCHEMA_VERSION:
            raise ContractError(f"unsupported runtime hook schema: {schema_version!r}")
        runtime = _required(value.get("runtime"), "runtime").lower()
        profile_id = _required(value.get("profile_id"), "profile_id").lower()
        profile_version = value.get("profile_version")
        if isinstance(profile_version, bool) or not isinstance(profile_version, int):
            raise ContractError("profile_version must be an integer")
        if (runtime, profile_id, profile_version) not in RUNTIME_OBSERVATION_PROFILES:
            raise ContractError("unsupported runtime hook profile")
        event = _required(value.get("event"), "event").lower()
        if event not in EVENT_PROMPT_STATES:
            raise ContractError(f"unsupported runtime hook event: {event}")
        prompt_state = _required(value.get("prompt_state"), "prompt_state").lower()
        if prompt_state != EVENT_PROMPT_STATES[event]:
            raise ContractError("runtime hook prompt_state does not match event")
        if value.get("input_buffer_state") != "unknown":
            raise ContractError("runtime hook cannot assert input-buffer certainty")
        tty = _required(value.get("tty"), "tty")
        if not TTY_RE.match(tty):
            raise ContractError(f"unsafe runtime hook tty: {tty!r}")
        observed_ts = value.get("observed_ts")
        if not isinstance(observed_ts, (int, float)) or not math.isfinite(float(observed_ts)):
            raise ContractError("observed_ts must be finite")
        return cls(
            schema_version=schema_version,
            runtime=runtime,
            profile_id=profile_id,
            profile_version=profile_version,
            event=event,
            prompt_state=prompt_state,
            input_buffer_state="unknown",
            iterm_session_id=_required(value.get("iterm_session_id"), "iterm_session_id"),
            tty=tty,
            cli_session_id=_required(value.get("cli_session_id"), "cli_session_id"),
            coord_session_id=_required(value.get("coord_session_id"), "coord_session_id"),
            observed_ts=float(observed_ts),
        )

    @classmethod
    def from_hook(
        cls,
        *,
        runtime: str,
        event: str,
        iterm_session_id: str,
        tty: str,
        cli_session_id: str,
        coord_session_id: str,
        payload: dict[str, Any],
        observed_ts: float | None = None,
    ) -> "RuntimeHookRecord":
        runtime = runtime.strip().lower()
        if runtime not in PROFILE_BY_RUNTIME:
            raise ContractError(f"unsupported runtime: {runtime}")
        payload_session_id = _payload_session_id(payload)
        if not payload_session_id:
            raise ContractError("hook payload has no runtime session identity")
        if payload_session_id != cli_session_id:
            raise ContractError("hook payload targets a different cli session")
        profile_id, profile_version = PROFILE_BY_RUNTIME[runtime]
        return cls.from_dict(
            {
                "schema_version": RUNTIME_HOOK_SCHEMA_VERSION,
                "runtime": runtime,
                "profile_id": profile_id,
                "profile_version": profile_version,
                "event": event,
                "prompt_state": EVENT_PROMPT_STATES.get(event),
                "input_buffer_state": "unknown",
                "iterm_session_id": iterm_session_id,
                "tty": tty,
                "cli_session_id": cli_session_id,
                "coord_session_id": coord_session_id,
                "observed_ts": time.time() if observed_ts is None else observed_ts,
            }
        )

    def state_path(self, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
        digest = hashlib.sha256(self.iterm_session_id.encode("utf-8")).hexdigest()
        return state_dir / f"{digest}.json"

    def is_fresh(
        self,
        *,
        now_ts: float | None = None,
        max_age: float = DEFAULT_MAX_AGE_SECONDS,
    ) -> bool:
        age = (time.time() if now_ts is None else now_ts) - self.observed_ts
        return 0 <= age <= max_age


def write_record(record: RuntimeHookRecord, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = state_dir.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or state_dir.is_symlink():
        raise ContractError("runtime hook state directory must be a real directory")
    if metadata.st_uid != os.getuid():
        raise ContractError("runtime hook state directory must be owned by the current user")
    os.chmod(state_dir, 0o700, follow_symlinks=False)
    target = record.state_path(state_dir)
    directory_fd = os.open(
        state_dir,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        temporary = f".{target.name}.{secrets.token_hex(8)}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(asdict(record), handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary,
                target.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except Exception:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(directory_fd)
    return target


def load_record(
    state_dir: Path,
    *,
    iterm_session_id: str,
    tty: str,
    now_ts: float | None = None,
    max_age: float = DEFAULT_MAX_AGE_SECONDS,
) -> RuntimeHookRecord | None:
    digest = hashlib.sha256(iterm_session_id.encode("utf-8")).hexdigest()
    filename = f"{digest}.json"
    try:
        metadata = state_dir.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or state_dir.is_symlink()
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            return None
        directory_fd = os.open(
            state_dir,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            fd = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                file_metadata = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(file_metadata.st_mode)
                    or file_metadata.st_uid != os.getuid()
                    or stat.S_IMODE(file_metadata.st_mode) != 0o600
                ):
                    return None
                value = json.load(handle)
        finally:
            os.close(directory_fd)
        if not isinstance(value, dict):
            return None
        record = RuntimeHookRecord.from_dict(value)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ContractError):
        return None
    if record.iterm_session_id != iterm_session_id or record.tty != tty:
        return None
    return record if record.is_fresh(now_ts=now_ts, max_age=max_age) else None


@dataclass(frozen=True)
class SignedRuntimeHookObservation:
    """An observer-authored report verified by the coord broker.

    This is deliberately separate from ``RuntimeHookRecord``. The local hook
    record is useful presentation evidence but is never sufficient authority
    for an interrupt delivery transaction.
    """

    hook_schema_version: int
    runtime_observation: RuntimeObservation
    iterm_session_id: str
    sequence: int
    observed_at: float
    event_id: str
    challenge_id: str
    signature: str

    @classmethod
    def from_session_variables(cls, values: dict[str, str]) -> "SignedRuntimeHookObservation":
        observed = RuntimeObservation.from_session_variables(values)
        if observed is None:
            raise ContractError("trusted runtime observation variables are missing or unsupported")
        try:
            schema = int(values.get("user.workerHookSchemaVersion") or "")
            sequence = int(values.get("user.workerHookSequence") or "")
            observed_at = float(values.get("user.workerHookObservedAt") or "")
        except ValueError as exc:
            raise ContractError("runtime hook coordinates are malformed") from exc
        if schema != HOOK_SCHEMA_VERSION:
            raise ContractError(f"unsupported runtime hook schema: {schema}")
        if sequence < 1:
            raise ContractError("runtime hook sequence must be positive")
        if not math.isfinite(observed_at):
            raise ContractError("runtime hook observed_at must be finite")
        event_id = str(values.get("user.workerHookEventId") or "").strip()
        challenge_id = str(values.get("user.workerHookChallengeId") or "").strip()
        signature = str(values.get("user.workerHookSignature") or "").strip()
        iterm_session_id = str(values.get("user.workerHookItermSessionId") or "").strip()
        if not event_id or not signature or not iterm_session_id:
            raise ContractError("runtime hook identity/signature is incomplete")
        return cls(
            schema,
            observed,
            iterm_session_id,
            sequence,
            observed_at,
            event_id,
            challenge_id,
            signature,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "hook_schema_version": self.hook_schema_version,
            "runtime_observation": asdict(self.runtime_observation),
            "iterm_session_id": self.iterm_session_id,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "event_id": self.event_id,
            "challenge_id": self.challenge_id,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def verify(
        self,
        verify_authenticity: BrokerVerifier,
        *,
        runtime: str,
        profile_id: str,
        profile_version: int,
        cli_session_id: str,
        coord_session_id: str,
        iterm_session_id: str,
        now_ts: float | None = None,
        after_sequence: int | None = None,
        min_observed_at: float | None = None,
        expected_challenge_id: str | None = None,
    ) -> None:
        self.runtime_observation.validate_registration(
            runtime=runtime,
            profile_id=profile_id,
            profile_version=profile_version,
            cli_session_id=cli_session_id,
            coord_session_id=coord_session_id,
        )
        if self.iterm_session_id != iterm_session_id:
            raise ContractError("runtime hook targets stale iTerm identity")
        age = (time.time() if now_ts is None else now_ts) - self.observed_at
        if age < 0 or age > MAX_HOOK_AGE_SECONDS:
            raise ContractError("runtime hook observation is stale")
        if after_sequence is not None and self.sequence <= after_sequence:
            raise ContractError("runtime hook observation did not advance")
        if min_observed_at is not None and self.observed_at < min_observed_at:
            raise ContractError("runtime hook observation predates terminal action")
        if expected_challenge_id is not None and self.challenge_id != expected_challenge_id:
            raise ContractError("runtime hook observation does not bind the active challenge")
        verification = verify_authenticity({**self.canonical_dict(), "signature": self.signature})
        if not isinstance(verification, dict) or verification.get("verified") is not True:
            raise ContractError("coord broker rejected runtime hook authenticity")
        if verification.get("observation_digest") != self.digest():
            raise ContractError("coord broker verified a different runtime hook observation")


def session_variable_values(
    observation: SignedRuntimeHookObservation,
) -> dict[str, str]:
    runtime = observation.runtime_observation
    return {
        "workerRuntime": runtime.runtime,
        "workerObservationProfile": runtime.profile_id,
        "workerObservationProfileVersion": str(runtime.profile_version),
        "workerPromptState": runtime.prompt_state,
        "workerInputBufferState": runtime.input_buffer_state,
        "cliSessionId": runtime.cli_session_id,
        "coordSessionId": runtime.coord_session_id,
        "workerHookSchemaVersion": str(observation.hook_schema_version),
        "workerHookItermSessionId": observation.iterm_session_id,
        "workerHookSequence": str(observation.sequence),
        "workerHookObservedAt": str(observation.observed_at),
        "workerHookEventId": observation.event_id,
        "workerHookChallengeId": observation.challenge_id,
        "workerHookSignature": observation.signature,
    }


def render_iterm_user_variables(observation: SignedRuntimeHookObservation) -> str:
    chunks = []
    for name, value in session_variable_values(observation).items():
        encoded = base64.b64encode(value.encode()).decode()
        chunks.append(f"\033]1337;SetUserVar={name}={encoded}\007")
    return "".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish an inert C2 runtime observation")
    parser.add_argument("--runtime", required=True, choices=sorted(PROFILE_BY_RUNTIME))
    parser.add_argument("--event", required=True, choices=sorted(EVENT_PROMPT_STATES))
    parser.add_argument("--iterm-session-id", required=True)
    parser.add_argument("--tty", required=True)
    parser.add_argument("--cli-session-id", required=True)
    parser.add_argument("--coord-session-id", required=True)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)
    try:
        payload = json.load(os.sys.stdin)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid hook JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("hook JSON must be an object")
    record = RuntimeHookRecord.from_hook(
        runtime=args.runtime,
        event=args.event,
        iterm_session_id=args.iterm_session_id,
        tty=args.tty,
        cli_session_id=args.cli_session_id,
        coord_session_id=args.coord_session_id,
        payload=payload,
    )
    write_record(record, args.state_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
