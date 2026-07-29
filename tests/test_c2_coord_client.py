from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

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
    handle = client.claim_resource("workspace:mikebook:c2-supervisor", ttl_seconds=180, producer={"kind": "c2"})
    renewed = client.renew_resource(handle)

    assert renewed.epoch == 9
    assert calls[0][2]["X-Principal-Id"] == "mikebook_codex"
    assert "%3A" in calls[0][1]


def test_renew_rejects_successor_epoch():
    def request(method, url, headers, body, timeout):
        return 200, {"status": "renewed", "lease": {"holder": "mikebook_codex", "epoch": 10}}

    client = coord.CoordClient(config(), request=request)
    handle = coord.LeaseHandle("resource", "mikebook_codex", 9, None, {"holder": "mikebook_codex", "epoch": 9})

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

    payload.update(epoch=7, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
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

    def request(method, url, headers, body, timeout):
        calls.append(json.loads(body))
        return 201, {"id": 42}

    client = coord.CoordClient(config(), request=request)
    result = client.post_receipt({"idempotency_key": "dispatch-1", "ok": False})

    assert result == {"id": 42}
    assert calls == [
        {
            "from_agent": "mikebook_codex",
            "to_agent": "mikebook_codex",
            "msg_type": "activity",
            "content": '{"c2_dispatch_receipt":{"idempotency_key":"dispatch-1","ok":false}}',
            "provenance_source": "dispatch",
        }
    ]
