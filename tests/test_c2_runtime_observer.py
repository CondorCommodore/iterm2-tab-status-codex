from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from c2_contract import ContractError, WorkerRegistration  # noqa: E402
from c2_coord_client import CoordConfig, CoordError  # noqa: E402
from c2_runtime_hook import HOOK_SCHEMA_VERSION, SignedRuntimeHookObservation  # noqa: E402
from c2_runtime_observation import RuntimeObservation  # noqa: E402
from c2_runtime_observer import (  # noqa: E402
    ObserverCursor,
    PendingRuntimeChallenge,
    RuntimeObserverCycle,
)

CHALLENGE_ID = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 7, 30, 12, 0, 10, tzinfo=timezone.utc).timestamp()


def worker(runtime="codex"):
    profile = "codex-cli" if runtime == "codex" else "claude-code"
    return WorkerRegistration.from_dict(
        {
            "worker_id": f"worker-{runtime}",
            "host": "mikebook",
            "runtime": runtime,
            "iterm_session_id": f"iterm-{runtime}",
            "tty": "/dev/ttys003",
            "cli_session_id": f"cli-{runtime}",
            "coord_session_id": f"coord-{runtime}",
            "coord_agent_id": f"agent-{runtime}",
            "observation_profile_id": profile,
            "observation_profile_version": 1,
        }
    )


def pending(runtime="codex", **overrides):
    target = worker(runtime)
    value = {
        "challenge_id": CHALLENGE_ID,
        "worker_id": target.worker_id,
        "iterm_session_id": target.iterm_session_id,
        "cli_session_id": target.cli_session_id,
        "coord_session_id": target.coord_session_id,
        "controller_epoch": 7,
        "worker_epoch": 13,
        "binding_sha256": "a" * 64,
        "armed_at": "2026-07-30T12:00:00Z",
        "expires_at": "2026-07-30T12:00:30Z",
        "runtime": target.runtime,
        "profile_id": target.observation_profile_id,
        "profile_version": target.observation_profile_version,
        **overrides,
    }
    return PendingRuntimeChallenge.from_broker(value)


def signed(runtime="codex", **overrides):
    target = worker(runtime)
    observation = SignedRuntimeHookObservation(
        hook_schema_version=HOOK_SCHEMA_VERSION,
        runtime_observation=RuntimeObservation.from_dict(
            {
                "runtime": target.runtime,
                "profile_id": target.observation_profile_id,
                "profile_version": target.observation_profile_version,
                "prompt_state": overrides.pop("prompt_state", "ready"),
                "input_buffer_state": overrides.pop("input_buffer_state", "empty"),
                "cli_session_id": overrides.pop("cli_session_id", target.cli_session_id),
                "coord_session_id": overrides.pop("coord_session_id", target.coord_session_id),
            }
        ),
        iterm_session_id=overrides.pop("iterm_session_id", target.iterm_session_id),
        sequence=overrides.pop("sequence", 4),
        observed_at=overrides.pop("observed_at", NOW - 1),
        event_id=overrides.pop("event_id", "event-4"),
        challenge_id=overrides.pop("challenge_id", CHALLENGE_ID),
        signature=overrides.pop("signature", "proof_token_abcdefghijklmnopqrstuvwxyz123456"),
    )
    assert not overrides
    return observation


class FakeClient:
    def __init__(self, *, principal="observer-1", response_mutation=None, error=None):
        self.config = CoordConfig("http://coord", "read", "write", principal, principal)
        self.response_mutation = response_mutation or {}
        self.error = error
        self.published = []
        self.pending_rows = []

    def pending_runtime_observation_challenges(self, *, limit=16):
        if self.error:
            raise self.error
        return self.pending_rows[:limit]

    def publish_runtime_observation(self, report, *, expected_binding_sha256):
        if self.error:
            raise self.error
        self.published.append(report)
        canonical = dict(report)
        canonical.pop("signature")
        digest = hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        return {
            "observation_digest": digest,
            "challenge_id": report["challenge_id"],
            "challenge_binding_sha256": expected_binding_sha256,
            "observer_principal": self.config.principal_id,
            **self.response_mutation,
        }


_DEFAULT_REPORT = object()


def cycle(client=None, report=_DEFAULT_REPORT, writes=None, cursor=None):
    client = client or FakeClient()
    writes = [] if writes is None else writes
    report = signed() if report is _DEFAULT_REPORT else report
    return (
        RuntimeObserverCycle(
            client,
            observer_principal="observer-1",
            sense=lambda _worker, _challenge: report,
            publish_to_session=writes.append,
            cursor=cursor,
            now=lambda: NOW,
        ),
        client,
        writes,
    )


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_observer_cycle_publishes_broker_accepted_exact_report_for_both_runtimes(runtime):
    target = worker(runtime)
    report = signed(runtime)
    writes = []
    client = FakeClient()
    observer = RuntimeObserverCycle(
        client,
        observer_principal="observer-1",
        sense=lambda actual_worker, challenge: (
            report if actual_worker == target and challenge.challenge_id == CHALLENGE_ID else None
        ),
        publish_to_session=writes.append,
        cursor=ObserverCursor(),
        now=lambda: NOW,
    )

    result = observer.process(
        pending(runtime),
        target,
        expected_controller_epoch=7,
        expected_worker_epoch=13,
    )

    assert client.published == [{**report.canonical_dict(), "signature": report.signature}]
    assert writes == [result]
    assert result.observer_principal == "observer-1"
    assert result.challenge_binding_sha256 == "a" * 64
    assert result.session_variables()["workerHookChallengeId"] == CHALLENGE_ID


def test_pending_cycle_consumes_only_broker_feed():
    client = FakeClient()
    row = {
        "challenge_id": CHALLENGE_ID,
        "worker_id": "worker-codex",
        "iterm_session_id": "iterm-codex",
        "cli_session_id": "cli-codex",
        "coord_session_id": "coord-codex",
        "controller_epoch": 7,
        "worker_epoch": 13,
        "binding_sha256": "a" * 64,
        "armed_at": "2026-07-30T12:00:00Z",
        "expires_at": "2026-07-30T12:00:30Z",
        "runtime": "codex",
        "profile_id": "codex-cli",
        "profile_version": 1,
    }
    client.pending_rows = [row]
    observer, _, _ = cycle(client=client)
    assert observer.pending(limit=1) == [PendingRuntimeChallenge.from_broker(row)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_id", "stale-worker"),
        ("iterm_session_id", "reused-iterm"),
        ("cli_session_id", "stale-cli"),
        ("coord_session_id", "stale-coord"),
        ("runtime", "claude"),
        ("profile_id", "claude-code"),
        ("profile_version", 2),
        ("controller_epoch", 8),
        ("worker_epoch", 14),
    ],
)
def test_observer_cycle_rejects_stale_or_unregistered_target(field, value):
    observer, client, writes = cycle()
    with pytest.raises(ContractError, match="stale registration"):
        observer.process(
            replace(pending(), **{field: value}),
            worker(),
            expected_controller_epoch=7,
            expected_worker_epoch=13,
        )
    assert client.published == []
    assert writes == []


@pytest.mark.parametrize(
    ("report", "message"),
    [
        (None, "no signed observation"),
        (signed(prompt_state="unknown"), "state is unknown"),
        (signed(input_buffer_state="unknown"), "state is unknown"),
        (signed(input_buffer_state="nonempty"), "nonempty"),
        (signed(prompt_state="running"), "not prompt-ready"),
        (signed(challenge_id="00000000-0000-4000-8000-000000000002"), "another challenge"),
        (signed(iterm_session_id="reused"), "stale iTerm"),
    ],
)
def test_observer_cycle_rejects_missing_ambiguous_or_wrong_sensing(report, message):
    observer, client, writes = cycle(report=report)
    with pytest.raises(ContractError, match=message):
        observer.process(pending(), worker(), expected_controller_epoch=7, expected_worker_epoch=13)
    assert client.published == []
    assert writes == []


def test_observer_cycle_rejects_expired_reordered_and_duplicate_challenge():
    observer, client, writes = cycle()
    with pytest.raises(ContractError, match="expired"):
        observer.process(
            pending(expires_at="2026-07-30T12:00:09Z"),
            worker(),
            expected_controller_epoch=7,
            expected_worker_epoch=13,
        )

    cursor = ObserverCursor(sequence_by_iterm_session={"iterm-codex": 4})
    observer, client, writes = cycle(cursor=cursor)
    with pytest.raises(ContractError, match="duplicate or reordered"):
        observer.process(pending(), worker(), expected_controller_epoch=7, expected_worker_epoch=13)

    cursor = ObserverCursor(processed_challenges={CHALLENGE_ID: "a" * 64})
    observer, client, writes = cycle(cursor=cursor)
    with pytest.raises(ContractError, match="already processed"):
        observer.process(pending(), worker(), expected_controller_epoch=7, expected_worker_epoch=13)
    assert client.published == []
    assert writes == []


def test_observer_cycle_retries_exact_publish_without_duplicate_local_effect():
    first_writes = []
    first, client, _ = cycle(writes=first_writes)
    result = first.process(
        pending(), worker(), expected_controller_epoch=7, expected_worker_epoch=13
    )
    assert len(first_writes) == 1

    # A crash before the local cursor commit may replay the exact report. The
    # broker digest is identical, and the local adapter sees one result per run.
    retry_writes = []
    retry, _, _ = cycle(client=client, writes=retry_writes)
    retried = retry.process(
        pending(), worker(), expected_controller_epoch=7, expected_worker_epoch=13
    )
    assert retried.observation_digest == result.observation_digest
    assert len({item["event_id"] for item in client.published}) == 1
    assert len(retry_writes) == 1


def test_observer_cycle_fails_closed_on_api_outage_or_readback_mismatch():
    outage = FakeClient(error=CoordError("coord unavailable"))
    observer, _, writes = cycle(client=outage)
    with pytest.raises(CoordError, match="unavailable"):
        observer.process(pending(), worker(), expected_controller_epoch=7, expected_worker_epoch=13)
    assert writes == []

    mismatch = FakeClient(response_mutation={"observer_principal": "another-principal"})
    observer, _, writes = cycle(client=mismatch)
    with pytest.raises(CoordError, match="mismatched durable coordinates"):
        observer.process(pending(), worker(), expected_controller_epoch=7, expected_worker_epoch=13)
    assert writes == []


def test_observer_cycle_rejects_another_principal_before_network_or_sensing():
    client = FakeClient(principal="another-principal")
    with pytest.raises(ContractError, match="principal"):
        RuntimeObserverCycle(
            client,
            observer_principal="observer-1",
            sense=lambda *_args: signed(),
            publish_to_session=lambda _item: None,
            now=lambda: NOW,
        )
