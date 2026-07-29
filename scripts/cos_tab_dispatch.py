#!/usr/bin/env python3
"""Safely dispatch one line of text to a target iTerm2 tab by TTY.

The dispatch path uses iTerm2's Python API instead of keyboard focus, escape,
or control-key automation. By default it only accepts ``/goal ...`` commands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from c2_contract import (
    ContractError,
    DispatchEnvelope,
    ReceiptStore,
    RunManifest,
    SUPERVISOR_RESOURCE,
    WorkerRegistration,
    load_envelope,
    load_manifest,
)
from c2_visual_decision import VisualDecision, VisualObservation
from cos_iterm_edge_client import dispatch_envelope as dispatch_envelope_via_iterm_api

TTY_RE = re.compile(r"^/dev/ttys[0-9A-Za-z_.-]+$")
FORBIDDEN_CONTROL_CHARS = {"\x03", "\x1b"}


@dataclass(frozen=True)
class DispatchRequest:
    tty: str
    text: str
    submit: bool = True
    require_goal: bool = True
    require_agent: bool = True


def validate_tty(tty: str) -> str:
    if not TTY_RE.match(tty):
        raise ValueError(f"unsafe tty target: {tty!r}")
    return tty


def validate_text(text: str, *, require_goal: bool = True) -> str:
    if any(char in text for char in FORBIDDEN_CONTROL_CHARS):
        raise ValueError("dispatch text must not contain Ctrl-C or Escape")
    if "\n" in text or "\r" in text:
        raise ValueError("dispatch text must be exactly one line")
    if require_goal and not text.startswith("/goal "):
        raise ValueError("dispatch text must start with '/goal '")
    if not text.strip():
        raise ValueError("dispatch text must not be empty")
    return text


def payload_for_request(request: DispatchRequest) -> str:
    validate_tty(request.tty)
    text = validate_text(request.text, require_goal=request.require_goal)
    return text + ("\n" if request.submit else "")


async def find_session_by_tty(connection: object, tty: str) -> object | None:
    import iterm2

    app = await iterm2.async_get_app(connection)
    for window in app.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                session_tty = await session.async_get_variable("tty")
                if session_tty == tty:
                    return session
    return None


async def find_session_by_id(connection: object, session_id: str) -> object | None:
    """Resolve one exact iTerm session identity, never a reusable TTY alone."""
    import iterm2

    app = await iterm2.async_get_app(connection)
    for window in app.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                observed = str(getattr(session, "session_id", "") or "")
                if observed == session_id:
                    return session
    return None


async def session_variables(session: object) -> dict[str, str]:
    names = (
        "tty",
        "jobName",
        "foregroundJobName",
        "name",
        "user.workerRuntime",
        "user.cosRole",
        "user.workerState",
        "user.workerReadiness",
        "user.cliSessionId",
        "user.coordSessionId",
        "session.isProcessing",
        "session.currentCommand",
    )
    values: dict[str, str] = {}
    for name in names:
        try:
            value = await session.async_get_variable(name)  # type: ignore[attr-defined]
        except Exception:
            value = ""
        values[name] = "" if value is None else str(value)
    return values


def looks_like_agent_session(values: dict[str, str]) -> bool:
    haystack = " ".join(values.values()).lower()
    return "codex" in haystack or "claude" in haystack


def foreground_matches_runtime(values: dict[str, str], runtime: str) -> bool:
    job = (
        values.get("foregroundJobName")
        or values.get("jobName")
        or ""
    ).rsplit("/", 1)[-1].lower()
    if runtime == "codex":
        return "codex" in job
    if runtime == "claude":
        return "claude" in job
    return False


def _command_is_runtime(command: str, runtime: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    executable = Path(argv[0]).name.lower()
    if executable == runtime:
        return True
    return executable in {"node", "nodejs"} and len(argv) > 1 and Path(argv[1]).name.lower() == runtime


def foreground_group_output_matches_runtime(output: str, runtime: str) -> bool:
    rows: list[tuple[int, int, str]] = []
    for raw in output.splitlines():
        parts = raw.strip().split(maxsplit=2)
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return any(
        pgid == tpgid and tpgid > 0 and _command_is_runtime(command, runtime)
        for pgid, tpgid, command in rows
    )


def tty_foreground_group_matches_runtime(tty: str, runtime: str) -> bool:
    """Confirm the runtime executable belongs to the kernel foreground group."""
    validate_tty(tty)
    result = subprocess.run(
        ["ps", "-t", Path(tty).name, "-o", "pgid=,tpgid=,command="],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    return result.returncode == 0 and foreground_group_output_matches_runtime(
        result.stdout, runtime
    )


async def dispatch(connection: object, request: DispatchRequest) -> dict[str, Any]:
    payload = payload_for_request(request)
    session = await find_session_by_tty(connection, request.tty)
    if session is None:
        return {"ok": False, "tty": request.tty, "error": "target tty not found"}
    values = await session_variables(session)
    if request.require_agent and not looks_like_agent_session(values):
        return {
            "ok": False,
            "tty": request.tty,
            "error": "target session does not look like codex/claude agent",
            "session": values,
        }
    await session.async_send_text(payload)
    return {
        "ok": True,
        "tty": request.tty,
        "bytes_sent": len(payload.encode("utf-8")),
        "submitted": request.submit,
    }


def render_dispatch_prompt(envelope: DispatchEnvelope) -> str:
    """Render the complete envelope as one safe, auditable agent prompt."""
    payload = envelope.canonical_json()
    text = f"/goal C2_DISPATCH {payload}"
    return validate_text(text)


async def send_prompt_with_crlf(session: object, prompt: str) -> None:
    """Write the prompt, CR, and LF separately so paste mode cannot absorb submit."""
    await session.async_send_text(prompt)  # type: ignore[attr-defined]
    # iTerm can deliver back-to-back writes as one paste transaction.  Give the
    # TUI time to close bracketed-paste mode before sending the proven CR/LF
    # character path.  A live Codex completion screen can absorb CR while
    # leaving the prompt queued; LF is therefore a required, separate write.
    await asyncio.sleep(0.15)
    await session.async_send_text("\r")  # type: ignore[attr-defined]
    await asyncio.sleep(0.05)
    await session.async_send_text("\n")  # type: ignore[attr-defined]


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_applescript(
    worker: WorkerRegistration, text: str, *, require_goal: bool = True
) -> str:
    """Build the proven no-focus CR/LF submit path for a registered session."""
    validate_tty(worker.tty)
    validate_text(text, require_goal=require_goal)
    session_id = _escape_applescript(worker.iterm_session_id)
    tty = _escape_applescript(worker.tty)
    payload = _escape_applescript(text)
    return f'''tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if unique ID of s is "{session_id}" then
          if tty of s is not "{tty}" then error "registered tty mismatch"
          tell s to write text "{payload}" without newline
          tell s to write text (character id 13) without newline
          tell s to write text (character id 10) without newline
          return "sent"
        end if
      end repeat
    end repeat
  end repeat
  error "registered iTerm session not found"
end tell'''


def _validate_authoritative_target(
    manifest: RunManifest,
    envelope: DispatchEnvelope,
    receipts: ReceiptStore,
) -> WorkerRegistration:
    worker = envelope.validate_for(manifest)
    if worker.role != "worker":
        raise ContractError("assignments may target only registered worker roles")
    if worker.iterm_session_id == manifest.controller_iterm_session_id:
        raise ContractError("self-dispatch is forbidden")
    if worker.tty == manifest.controller_tty:
        raise ContractError("self-dispatch tty is forbidden")
    if receipts.has_idempotency_key(envelope.idempotency_key):
        raise ContractError(
            f"duplicate dispatch idempotency key: {envelope.idempotency_key}"
        )
    return worker


def _receipt(
    *,
    envelope: DispatchEnvelope,
    worker: WorkerRegistration,
    submit_method: str,
    observed_ack: bool,
    metrics: dict[str, Any] | None = None,
    reservation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "receipt_version": 1,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "assignment_id": envelope.assignment_id,
        "task_id": envelope.task_id,
        "attempt_id": envelope.attempt_id,
        "worker_id": worker.worker_id,
        "target_iterm_session_id": worker.iterm_session_id,
        "target_tty": worker.tty,
        "target_runtime": worker.runtime,
        "payload_digest": envelope.digest(),
        "controller_epoch": envelope.controller_epoch,
        "idempotency_key": envelope.idempotency_key,
        "submit_method": submit_method,
        "observed_ack": observed_ack,
        "metrics": metrics or {},
        "reservation": reservation,
    }


def _ack_transitioned(before: dict[str, str], after: dict[str, str]) -> bool:
    """Require evidence created by this submit, never an ambient active state."""
    before_state = (
        before.get("user.workerReadiness") or before.get("user.workerState") or ""
    ).lower()
    after_state = (
        after.get("user.workerReadiness") or after.get("user.workerState") or ""
    ).lower()
    before_processing = before.get("session.isProcessing", "").lower()
    after_processing = after.get("session.isProcessing", "").lower()
    before_command = before.get("session.currentCommand", "")
    after_command = after.get("session.currentCommand", "")
    active_states = {"running", "reserved", "queued", "processing"}
    processing_values = {"1", "true", "yes"}
    return (
        after_state != before_state and after_state in active_states
    ) or (
        after_processing != before_processing
        and after_processing in processing_values
    ) or (
        after_command != before_command and bool(after_command)
    )


async def _session_acknowledged(
    session: object,
    *,
    before: dict[str, str],
    attempts: int = 4,
) -> tuple[bool, dict[str, str]]:
    latest = before
    for attempt in range(max(1, attempts)):
        latest = await session_variables(session)
        if _ack_transitioned(before, latest):
            return True, latest
        if attempt + 1 < attempts:
            await asyncio.sleep(0.25)
    return False, latest


async def _submit_with_bounded_recovery(
    session: object,
    *,
    prompt: str,
    before: dict[str, str],
    verify_epoch: Any,
    controller_epoch: int,
    ack_attempts: int,
) -> tuple[bool, bool]:
    """Submit once, then issue at most one re-fenced CR if no start is observed."""
    await send_prompt_with_crlf(session, prompt)
    observed_ack, latest = await _session_acknowledged(
        session, before=before, attempts=ack_attempts
    )
    if observed_ack:
        return True, False

    # Re-read immediately before recovery. If the original submit started since
    # the last poll, do not send anything else. Otherwise a single CR can submit
    # the still-queued input, but cannot re-enter the already-consumed prompt.
    current = await session_variables(session)
    if _ack_transitioned(before, current):
        return True, False
    verify_epoch(SUPERVISOR_RESOURCE, controller_epoch)
    await session.async_send_text("\r")  # type: ignore[attr-defined]
    observed_ack, _latest = await _session_acknowledged(
        session, before=current, attempts=ack_attempts
    )
    return observed_ack, True


async def dispatch_registered(
    connection: object,
    *,
    manifest: RunManifest,
    envelope: DispatchEnvelope,
    verify_epoch: Any,
    receipts: ReceiptStore,
    ack_attempts: int = 4,
    reservation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch through iTerm2 after exact registration and last-moment fencing."""
    worker = _validate_authoritative_target(manifest, envelope, receipts)
    session = await find_session_by_id(connection, worker.iterm_session_id)
    if session is None:
        return {"ok": False, "error": "registered iTerm session not found"}
    values = await session_variables(session)
    if values.get("tty") != worker.tty:
        return {"ok": False, "error": "registered tty mismatch", "session": values}
    foreground_owned = foreground_matches_runtime(
        values, worker.runtime
    ) or tty_foreground_group_matches_runtime(worker.tty, worker.runtime)
    if not looks_like_agent_session(values) and not foreground_owned:
        return {"ok": False, "error": "registered target is not an agent", "session": values}
    if not foreground_owned:
        return {
            "ok": False,
            "error": "registered agent does not own the terminal foreground",
            "session": values,
        }
    observed_runtime = str(values.get("user.workerRuntime") or "").lower()
    if observed_runtime in {"codex", "claude"} and observed_runtime != worker.runtime:
        return {"ok": False, "error": "registered runtime mismatch", "session": values}
    observed_cli = str(values.get("user.cliSessionId") or "")
    observed_coord = str(values.get("user.coordSessionId") or "")
    if observed_cli and observed_cli != worker.cli_session_id:
        return {"ok": False, "error": "stale cli session identity", "session": values}
    if observed_coord and observed_coord != worker.coord_session_id:
        return {"ok": False, "error": "stale coord session identity", "session": values}

    prompt = render_dispatch_prompt(envelope)

    def verify_dispatch_fences(_resource: str, _epoch: int) -> None:
        verify_epoch(SUPERVISOR_RESOURCE, envelope.controller_epoch)
        if reservation is not None:
            verify_epoch(str(reservation["resource"]), int(reservation["epoch"]))

    verify_dispatch_fences(SUPERVISOR_RESOURCE, envelope.controller_epoch)
    observed_ack, recovery_submitted = await _submit_with_bounded_recovery(
        session,
        prompt=prompt,
        before=values,
        verify_epoch=verify_dispatch_fences,
        controller_epoch=envelope.controller_epoch,
        ack_attempts=ack_attempts,
    )
    receipt = _receipt(
        envelope=envelope,
        worker=worker,
        submit_method="iterm2-python-api-crlf",
        observed_ack=observed_ack,
        reservation=reservation,
        metrics={"recovery_submitted": recovery_submitted},
    )
    receipts.append(receipt)
    if not observed_ack:
        return {
            "ok": False,
            "error": "registered target did not acknowledge dispatch",
            "receipt": receipt,
        }
    return {"ok": True, "receipt": receipt}


async def send_controller_poke(
    connection: object,
    *,
    manifest: RunManifest,
    text: str,
    controller_epoch: int,
    idempotency_key: str,
    verify_epoch: Any,
    ack_attempts: int = 4,
) -> dict[str, Any]:
    """Wake the exact registered COS session through the iTerm2 Python API."""
    prompt = validate_text(text, require_goal=False)
    session = await find_session_by_id(connection, manifest.controller_iterm_session_id)
    if session is None:
        return {"ok": False, "error": "registered COS iTerm session not found"}
    values = await session_variables(session)
    if values.get("tty") != manifest.controller_tty:
        return {"ok": False, "error": "registered COS tty mismatch", "session": values}
    observed_runtime = str(values.get("user.workerRuntime") or "").lower()
    if observed_runtime in {"codex", "claude"} and observed_runtime != manifest.controller_runtime:
        return {"ok": False, "error": "registered COS runtime mismatch", "session": values}
    if not foreground_matches_runtime(values, manifest.controller_runtime):
        return {
            "ok": False,
            "error": "registered COS does not own the terminal foreground",
            "session": values,
        }
    verify_epoch(SUPERVISOR_RESOURCE, controller_epoch)
    observed_ack, recovery_submitted = await _submit_with_bounded_recovery(
        session,
        prompt=prompt,
        before=values,
        verify_epoch=verify_epoch,
        controller_epoch=controller_epoch,
        ack_attempts=ack_attempts,
    )
    return {
        "ok": observed_ack,
        "target_iterm_session_id": manifest.controller_iterm_session_id,
        "target_tty": manifest.controller_tty,
        "controller_epoch": controller_epoch,
        "idempotency_key": idempotency_key,
        "submit_method": "iterm2-python-api-crlf",
        "observed_ack": observed_ack,
        "metrics": {"recovery_submitted": recovery_submitted},
        **({} if observed_ack else {"error": "registered COS did not acknowledge poke"}),
    }


def dispatch_registered_applescript(
    *,
    manifest: RunManifest,
    envelope: DispatchEnvelope,
    verify_epoch: Any,
    receipts: ReceiptStore,
    run: Any = subprocess.run,
) -> dict[str, Any]:
    """Normal-Python edge path used by launchd and the bootstrap CLI."""
    worker = _validate_authoritative_target(manifest, envelope, receipts)
    prompt = render_dispatch_prompt(envelope)
    script = build_applescript(worker, prompt)
    verify_epoch(SUPERVISOR_RESOURCE, envelope.controller_epoch)
    result = run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0 or "sent" not in result.stdout:
        return {
            "ok": False,
            "error": (result.stderr or result.stdout or "AppleScript dispatch failed").strip(),
        }
    receipt = _receipt(
        envelope=envelope,
        worker=worker,
        submit_method="applescript-character-13-10",
        observed_ack=False,
    )
    receipts.append(receipt)
    return {
        "ok": False,
        "error": "AppleScript injection has no target acknowledgment",
        "receipt": receipt,
    }


async def execute_visual_decision(
    connection: object,
    *,
    manifest: RunManifest,
    observation: VisualObservation,
    decision: VisualDecision,
    verify_epoch: Any,
    receipts: ReceiptStore,
    ack_attempts: int = 4,
) -> dict[str, Any]:
    """Execute one LLM-authored, screenshot-bound action on an owned worker."""
    observation.validate_for(manifest)
    decision.validate_for(observation)
    if receipts.has_idempotency_key(decision.idempotency_key):
        raise ContractError(
            f"duplicate visual decision idempotency key: {decision.idempotency_key}"
        )
    worker = manifest.worker(observation.worker_id)
    if worker.role != "worker":
        raise ContractError("visual decisions may target only registered workers")
    if worker.iterm_session_id == manifest.controller_iterm_session_id:
        raise ContractError("self-dispatch is forbidden")

    session = await find_session_by_id(connection, worker.iterm_session_id)
    if session is None:
        return {"ok": False, "error": "registered iTerm session not found"}
    before = await session_variables(session)
    if before.get("tty") != worker.tty:
        return {"ok": False, "error": "registered tty mismatch", "session": before}
    foreground_owned = foreground_matches_runtime(
        before, worker.runtime
    ) or tty_foreground_group_matches_runtime(worker.tty, worker.runtime)
    if not foreground_owned:
        return {
            "ok": False,
            "error": "registered agent does not own the terminal foreground",
            "session": before,
        }
    observed_runtime = str(before.get("user.workerRuntime") or "").lower()
    if observed_runtime in {"codex", "claude"} and observed_runtime != worker.runtime:
        return {"ok": False, "error": "registered runtime mismatch", "session": before}
    observed_cli = str(before.get("user.cliSessionId") or "")
    observed_coord = str(before.get("user.coordSessionId") or "")
    if observed_cli and observed_cli != worker.cli_session_id:
        return {"ok": False, "error": "stale cli session identity", "session": before}
    if observed_coord and observed_coord != worker.coord_session_id:
        return {"ok": False, "error": "stale coord session identity", "session": before}

    worker_resource = f"workspace:mikebook:c2-worker:{worker.worker_id}"
    verify_epoch(SUPERVISOR_RESOURCE, observation.controller_epoch)
    verify_epoch(worker_resource, observation.worker_epoch)
    await session.async_send_text(decision.terminal_text())  # type: ignore[attr-defined]
    observed_ack, _after = await _session_acknowledged(
        session, before=before, attempts=ack_attempts
    )
    receipt = {
        "receipt_version": 1,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": "visual-decision",
        "worker_id": worker.worker_id,
        "target_iterm_session_id": worker.iterm_session_id,
        "target_tty": worker.tty,
        "target_runtime": worker.runtime,
        "observation_digest": observation.digest(),
        "screenshot_sha256": observation.screenshot_sha256,
        "decision_action": decision.action,
        "decision_rationale": decision.rationale,
        "decided_by": decision.decided_by,
        "controller_epoch": observation.controller_epoch,
        "worker_epoch": observation.worker_epoch,
        "idempotency_key": decision.idempotency_key,
        "submit_method": "iterm2-python-api-visual-action",
        "observed_ack": observed_ack,
        "post_visual_verification_required": True,
    }
    receipts.append(receipt)
    return {"ok": True, "receipt": receipt}


def headless_command(worker: WorkerRegistration, prompt: str) -> list[str]:
    validate_text(prompt)
    if worker.runtime == "codex":
        return ["codex", "exec", "resume", worker.cli_session_id, prompt]
    if worker.runtime == "claude":
        return ["claude", "--resume", worker.cli_session_id, "--print", prompt]
    raise ContractError(f"unsupported headless runtime: {worker.runtime}")


def dispatch_registered_headless(
    *,
    manifest: RunManifest,
    envelope: DispatchEnvelope,
    verify_epoch: Any,
    receipts: ReceiptStore,
    run: Any = subprocess.run,
    timeout_seconds: float = 1800.0,
    reservation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resume the same agent UUID for one bounded headless turn, then exit."""
    worker = _validate_authoritative_target(manifest, envelope, receipts)
    prompt = render_dispatch_prompt(envelope)
    command = headless_command(worker, prompt)
    verify_epoch(SUPERVISOR_RESOURCE, envelope.controller_epoch)
    if reservation is not None:
        verify_epoch(str(reservation["resource"]), int(reservation["epoch"]))
    started = time.monotonic()
    try:
        result = run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "error": f"headless resume timed out after {timeout_seconds}s",
            "metrics": {"duration_ms": duration_ms, "timed_out": True},
            "stdout_tail": str(exc.stdout or "")[-500:],
            "stderr_tail": str(exc.stderr or "")[-500:],
        }
    duration_ms = int((time.monotonic() - started) * 1000)
    metrics = {
        "duration_ms": duration_ms,
        "exit_code": result.returncode,
        "timed_out": timed_out,
        "completed_bounded_turn": result.returncode == 0,
    }
    receipt = _receipt(
        envelope=envelope,
        worker=worker,
        submit_method=f"headless-{worker.runtime}-resume",
        observed_ack=result.returncode == 0,
        metrics=metrics,
        reservation=reservation,
    )
    receipts.append(receipt)
    return {
        "ok": result.returncode == 0,
        "receipt": receipt,
        "stdout_tail": result.stdout[-500:],
        "stderr_tail": result.stderr[-500:],
    }


def dispatch_registered_by_policy(
    *,
    manifest: RunManifest,
    envelope: DispatchEnvelope,
    verify_epoch: Any,
    receipts: ReceiptStore,
    run: Any = subprocess.run,
    edge_dispatch: Any = dispatch_envelope_via_iterm_api,
) -> dict[str, Any]:
    return edge_dispatch(json.loads(envelope.canonical_json()))


def send_controller_poke_applescript(
    *,
    manifest: RunManifest,
    text: str,
    controller_epoch: int,
    idempotency_key: str,
    verify_epoch: Any,
    run: Any = subprocess.run,
) -> dict[str, Any]:
    """Wake the registered COS session; this is not a worker assignment."""
    controller = WorkerRegistration(
        worker_id=manifest.controller_id,
        host=manifest.controller_host,
        runtime=manifest.controller_runtime,
        iterm_session_id=manifest.controller_iterm_session_id,
        tty=manifest.controller_tty,
        cli_session_id=manifest.controller_cli_session_id,
        coord_session_id=manifest.controller_coord_session_id,
        coord_agent_id=manifest.controller_coord_agent_id,
        role="cos",
    )
    prompt = validate_text(text, require_goal=False)
    verify_epoch(SUPERVISOR_RESOURCE, controller_epoch)
    result = run(
        ["osascript", "-e", build_applescript(controller, prompt, require_goal=False)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    ok = result.returncode == 0 and "sent" in result.stdout
    return {
        "ok": ok,
        "target_iterm_session_id": controller.iterm_session_id,
        "target_tty": controller.tty,
        "controller_epoch": controller_epoch,
        "idempotency_key": idempotency_key,
        "submit_method": "applescript-character-13-10",
        "observed_ack": False,
        **({} if ok else {"error": (result.stderr or result.stdout).strip()}),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dispatch a safe command to an iTerm2 tab.")
    parser.add_argument("--tty", help="Target TTY, for example /dev/ttys003")
    parser.add_argument("--text", help="Single command line to send")
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help="Do not append Enter/newline after the command.",
    )
    parser.add_argument("--manifest", type=Path, help="Registered-worker run manifest")
    parser.add_argument("--envelope", type=Path, help="Complete C2 dispatch envelope")
    parser.add_argument(
        "--receipts",
        type=Path,
        default=Path.home() / ".local" / "state" / "cos-c2" / "dispatch-receipts.jsonl",
    )
    parser.add_argument(
        "--allow-non-goal",
        action="store_true",
        help="Allow commands that do not start with '/goal '.",
    )
    parser.add_argument(
        "--allow-shell-target",
        action="store_true",
        help="Do not require the target session to look like Codex or Claude.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the dispatch payload without sending.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if bool(args.manifest) != bool(args.envelope):
        raise SystemExit("--manifest and --envelope must be provided together")
    if args.manifest and args.envelope:
        manifest = load_manifest(args.manifest)
        envelope = load_envelope(args.envelope)
        receipts = ReceiptStore(args.receipts)
        worker = _validate_authoritative_target(manifest, envelope, receipts)
        if args.dry_run:
            transport = manifest.transport_for(envelope.assignment_id)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "worker_id": worker.worker_id,
                        "iterm_session_id": worker.iterm_session_id,
                        "tty": worker.tty,
                        "payload_digest": envelope.digest(),
                        "controller_epoch": envelope.controller_epoch,
                        "selected_transport": transport,
                        "submit_method": (
                            f"headless-{worker.runtime}-resume"
                            if transport == "headless"
                            else "iterm2-python-api-crlf"
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        result = dispatch_registered_by_policy(
            manifest=manifest,
            envelope=envelope,
            verify_epoch=None,
            receipts=receipts,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1

    if not args.tty or not args.text:
        raise SystemExit("legacy dispatch requires --tty and --text")
    request = DispatchRequest(
        tty=args.tty,
        text=args.text,
        submit=not args.no_submit,
        require_goal=not args.allow_non_goal,
        require_agent=not args.allow_shell_target,
    )
    payload = payload_for_request(request)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "tty": request.tty,
                    "payload_repr": repr(payload),
                    "require_agent": request.require_agent,
                    "submitted": request.submit,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    try:
        import iterm2
    except ImportError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "tty": request.tty,
                    "error": "iterm2 module unavailable; run inside iTerm2's Python runtime",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    async def _run(connection: object) -> None:
        print(json.dumps(await dispatch(connection, request), indent=2, sort_keys=True))

    iterm2.run_until_complete(_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
