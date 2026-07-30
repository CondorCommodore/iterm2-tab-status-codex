from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import c2_coord_client as coord  # noqa: E402


def config():
    return coord.CoordConfig(
        api_url="http://coord",
        read_token="read",
        principal_token="write",
        agent_id="mikebook_codex",
        principal_id="mikebook_codex",
    )


def test_load_selects_manifest_principal_and_matching_token(tmp_path):
    agent_path = tmp_path / "agent.json"
    secrets_path = tmp_path / "env"
    agent_path.write_text(
        json.dumps(
            {
                "api_url": "http://coord",
                "api_key": "read",
                "agent_id": "mikebook-01",
                "principal_id": "mikebook-01",
                "principal_token": "host-token",
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text("MIKEBOOK_CODEX_TOKEN=codex-token\n", encoding="utf-8")

    loaded = coord.CoordConfig.load(
        agent_path,
        expected_principal_id="mikebook_codex",
        secrets_path=secrets_path,
    )

    assert loaded.agent_id == "mikebook_codex"
    assert loaded.principal_id == "mikebook_codex"
    assert loaded.principal_token == "codex-token"


def test_load_does_not_reuse_host_token_for_another_principal(tmp_path):
    agent_path = tmp_path / "agent.json"
    agent_path.write_text(
        json.dumps(
            {
                "api_url": "http://coord",
                "api_key": "read",
                "agent_id": "mikebook-01",
                "principal_id": "mikebook-01",
                "principal_token": "host-token",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(coord.CoordError, match="principal-bound"):
        coord.CoordConfig.load(
            agent_path,
            expected_principal_id="mikebook_codex",
            secrets_path=tmp_path / "missing-env",
        )


def test_claim_and_renew_preserve_epoch_and_principal_headers():
    calls = []

    def request(method, url, headers, body, timeout):
        calls.append((method, url, headers, body, timeout))
        return 200, {
            "status": "granted" if url.endswith("/claim") else "renewed",
            "lease": {"holder": "mikebook_codex", "epoch": 9, "expires_at": "2099-01-01T00:00:00Z"},
        }

    client = coord.CoordClient(config(), request=request)
    handle = client.claim_resource(
        "workspace:mikebook:c2-supervisor", ttl_seconds=180, producer={"kind": "c2"}
    )
    renewed = client.renew_resource(handle)

    assert renewed.epoch == 9
    assert calls[0][2]["X-Principal-Id"] == "mikebook_codex"
    assert "%3A" in calls[0][1]


def test_claim_distinguishes_live_holder_contention_from_contract_rejection():
    responses = iter(
        [
            (409, {"status": "blocked", "current_holder": "control-room"}),
            (
                409,
                {
                    "status": "blocked",
                    "current_holder": "mikebook_codex",
                    "reason": "controller_instance_mismatch",
                    "detail": "controller instance does not match the live lease",
                },
            ),
            (
                409,
                {
                    "status": "blocked",
                    "reason": "controller_instance_binding_invalid",
                    "detail": (
                        "c2-supervisor producer requires a complete controller instance binding"
                    ),
                },
            ),
        ]
    )

    def request(method, url, headers, body, timeout):
        return next(responses)

    client = coord.CoordClient(config(), request=request)
    with pytest.raises(coord.LeaseBlocked, match="held by control-room") as contention:
        client.claim_resource("workspace:mikebook:c2-supervisor", ttl_seconds=180, producer={})
    assert contention.value.payload["current_holder"] == "control-room"

    with pytest.raises(
        coord.LeaseRejected,
        match="controller_instance_mismatch.*does not match the live lease",
    ) as rejection:
        client.claim_resource("workspace:mikebook:c2-supervisor", ttl_seconds=180, producer={})
    assert rejection.value.payload["current_holder"] == "mikebook_codex"
    assert rejection.value.payload["reason"] == "controller_instance_mismatch"

    with pytest.raises(
        coord.LeaseRejected,
        match="controller_instance_binding_invalid.*complete controller instance binding",
    ):
        client.claim_resource("workspace:mikebook:c2-supervisor", ttl_seconds=180, producer={})


def test_c2_claim_renew_and_release_preserve_instance_expectation():
    calls = []
    producer = {
        "kind": "c2-supervisor",
        "manifest_id": "fleet-v1",
        "controller_id": "cos",
        "controller_host": "mikebook",
        "controller_runtime": "codex",
        "controller_cli_session_id": "cli-a",
        "controller_coord_session_id": "coord-a",
        "controller_presentation": "headless",
        "ownership": "headless",
    }

    def request(method, url, headers, body, timeout):
        request_payload = json.loads(body) if body else None
        calls.append((method, url, request_payload))
        if url.endswith("/renew") and request_payload.get("expected_epoch") != 9:
            return 409, {"reason": "controller_instance_expectation_required"}
        return 200, {
            "status": "ok",
            "lease": {
                "holder": "mikebook_codex",
                "epoch": 9,
                "expires_at": "2099-01-01T00:00:00Z",
            },
        }

    client = coord.CoordClient(config(), request=request)
    handle = client.claim_resource(
        "workspace:mikebook:c2-supervisor", ttl_seconds=180, producer=producer
    )
    renewed = client.renew_resource(handle)
    assert client.release_resource(renewed) is True

    expected = {key: value for key, value in producer.items() if key != "kind"}
    assert calls[1][2]["expected_controller_instance"] == expected
    assert calls[1][2]["expected_epoch"] == 9
    assert calls[2][2]["expected_controller_instance"] == expected
    assert calls[2][2]["expected_epoch"] == 9


def test_generic_renew_and_release_omit_controller_expectation():
    calls = []

    def request(method, url, headers, body, timeout):
        calls.append(json.loads(body) if body else None)
        return 200, {
            "status": "ok",
            "lease": {
                "holder": "mikebook_codex",
                "epoch": 9,
                "expires_at": "2099-01-01T00:00:00Z",
            },
        }

    client = coord.CoordClient(config(), request=request)
    handle = client.claim_resource("generic", ttl_seconds=180, producer={"kind": "generic"})
    renewed = client.renew_resource(handle)
    assert client.release_resource(renewed) is True

    assert "expected_controller_instance" not in calls[1]
    assert "expected_controller_instance" not in calls[2]


def test_runtime_observation_challenge_and_verification_use_broker_endpoints():
    calls = []

    def request(method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        calls.append((method, url, headers, payload, timeout))
        if url.endswith("/challenges"):
            return 201, {"challenge": {"challenge_id": "challenge-1", "issued_at": 1001.0}}
        if url.endswith("/arm"):
            return 200, {"challenge": {"challenge_id": "challenge-1", "armed": True}}
        return 200, {
            "verification": {
                "verified": True,
                "observation_digest": "a" * 64,
            }
        }

    client = coord.CoordClient(config(), request=request)
    challenge = client.create_runtime_interrupt_challenge(
        {"idempotency_key": "interrupt-1:challenge", "worker_id": "worker"}
    )
    armed = client.arm_runtime_interrupt_challenge(
        {
            "challenge_id": "challenge-1",
            "idempotency_key": "interrupt-1:challenge-arm",
        }
    )
    verification = client.verify_runtime_observation(
        {"event_id": "event-1", "signature": "broker-signature"}
    )

    assert challenge["challenge_id"] == "challenge-1"
    assert armed["armed"] is True
    assert verification["verified"] is True
    assert calls[0][1].endswith("/c2/runtime-observations/challenges")
    assert calls[0][2]["Idempotency-Key"] == "interrupt-1:challenge"
    assert calls[1][1].endswith("/c2/runtime-observations/challenges/challenge-1/arm")
    assert calls[2][1].endswith("/c2/runtime-observations/verify")
    assert calls[2][3]["observation"]["event_id"] == "event-1"


@pytest.mark.parametrize(
    "response",
    [
        {"challenge": {"challenge_id": "", "issued_at": 1001.0}},
        {"challenge": {"challenge_id": "challenge-1", "issued_at": float("nan")}},
        {},
    ],
)
def test_runtime_interrupt_challenge_rejects_invalid_broker_response(response):
    client = coord.CoordClient(config(), request=lambda *_args: (200, response))
    with pytest.raises(coord.CoordError, match="challenge"):
        client.create_runtime_interrupt_challenge({"idempotency_key": "challenge-1"})


def test_renew_rejects_successor_epoch():
    def request(method, url, headers, body, timeout):
        return 200, {"status": "renewed", "lease": {"holder": "mikebook_codex", "epoch": 10}}

    client = coord.CoordClient(config(), request=request)
    handle = coord.LeaseHandle(
        "resource", "mikebook_codex", 9, None, {"holder": "mikebook_codex", "epoch": 9}
    )

    with pytest.raises(coord.LeaseLost, match="epoch changed"):
        client.renew_resource(handle)


def test_verify_epoch_rejects_wrong_holder_epoch_and_expiry():
    payload = {"holder": "other", "epoch": 7, "expires_at": "2099-01-01T00:00:00Z"}

    def request(method, url, headers, body, timeout):
        return 200, payload

    client = coord.CoordClient(config(), request=request)
    with pytest.raises(coord.LeaseLost, match="holder mismatch"):
        client.verify_live_epoch("resource", 7)

    payload.update(holder="mikebook_codex", epoch=8)
    with pytest.raises(coord.LeaseLost, match="epoch mismatch"):
        client.verify_live_epoch("resource", 7)

    payload.update(
        epoch=7, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    )
    with pytest.raises(coord.LeaseLost, match="expired"):
        client.verify_live_epoch("resource", 7)


@pytest.mark.parametrize("expires_at", [None, "", "not-a-date"])
def test_verify_epoch_rejects_missing_or_malformed_expiry(expires_at):
    payload = {"holder": "mikebook_codex", "epoch": 7, "expires_at": expires_at}
    client = coord.CoordClient(config(), request=lambda *_args: (200, payload))
    with pytest.raises(coord.LeaseLost, match="invalid expiry"):
        client.verify_live_epoch("resource", 7)


def test_post_receipt_uses_coord_supported_activity_message_type():
    calls = []
    receipt = {"idempotency_key": "dispatch-1", "ok": False}
    content = '{"c2_dispatch_receipt":{"idempotency_key":"dispatch-1","ok":false}}'

    def request(method, url, headers, body, timeout):
        calls.append((method, url, json.loads(body) if body else None))
        if method == "POST":
            return 201, {"id": 42}
        return 200, {
            "id": 42,
            "from_agent": "mikebook_codex",
            "to_agent": "mikebook_codex",
            "msg_type": "activity",
            "content": content,
            "accepted": True,
            "acknowledged_by": "coord-api",
        }

    client = coord.CoordClient(config(), request=request)
    result = client.post_receipt(receipt)

    assert result["id"] == 42
    assert calls == [
        (
            "POST",
            "http://coord/messages",
            {
                "from_agent": "mikebook_codex",
                "to_agent": "mikebook_codex",
                "msg_type": "activity",
                "content": content,
                "provenance_source": "dispatch",
            },
        ),
        ("GET", "http://coord/messages/42", None),
    ]


def test_message_delivery_shadow_uses_dedicated_supported_routes():
    calls = []
    run = {"run_id": "phase2:run-1", "artifact_sha256": "a" * 64}

    def request(method, url, headers, body, timeout):
        calls.append((method, url, headers, json.loads(body) if body else None))
        return (201 if method == "POST" else 200), {"item": run, "shadow": True}

    client = coord.CoordClient(config(), request=request)
    assert client.post_message_delivery_shadow_run(run) == run
    assert client.message_delivery_shadow_run("phase2:run-1") == run

    assert calls[0][0:2] == ("POST", "http://coord/message-delivery-shadow/runs")
    assert calls[0][2]["Idempotency-Key"] == "phase2:run-1"
    assert calls[1][0:2] == (
        "GET",
        "http://coord/message-delivery-shadow/runs/phase2%3Arun-1",
    )
    assert calls[1][2]["X-Principal-Id"] == "mikebook_codex"


@pytest.mark.parametrize("run_id", ["", ":leading", "space is invalid", "x" * 129])
def test_message_delivery_shadow_read_rejects_invalid_run_id(run_id):
    client = coord.CoordClient(config(), request=lambda *_args: (500, {}))
    with pytest.raises(coord.CoordError, match="invalid syntax"):
        client.message_delivery_shadow_run(run_id)
    with pytest.raises(coord.CoordError, match="invalid syntax"):
        client.post_message_delivery_shadow_run({"run_id": run_id})
