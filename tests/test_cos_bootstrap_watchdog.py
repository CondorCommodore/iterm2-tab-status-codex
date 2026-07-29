from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_bootstrap_watchdog as watchdog  # noqa: E402
from c2_coord_client import CoordError  # noqa: E402


def write_manifest(path: Path, *, recovery="ab"):
    path.write_text(
        json.dumps(
            {
                "manifest_id": "test",
                "controller": {
                    "controller_id": "cos",
                    "host": "macbook",
                    "runtime": "codex",
                    "iterm_session_id": "iterm-cos",
                    "tty": "/dev/ttys001",
                    "cli_session_id": "cli-cos",
                    "coord_session_id": "coord-cos",
                    "coord_agent_id": "mikebook_codex",
                },
                "workers": [
                    {
                        "worker_id": "worker",
                        "host": "macbook",
                        "runtime": "codex",
                        "iterm_session_id": "iterm-worker",
                        "tty": "/dev/ttys003",
                        "cli_session_id": "cli-worker",
                        "coord_session_id": "coord-worker",
                        "coord_agent_id": "mikebook_codex",
                    }
                ],
                "plan_paths": ["/plan"],
                "permitted_repositories": ["Condor/repo"],
                "permitted_actions": ["inspect"],
                "recovery_transport": recovery,
            }
        ),
        encoding="utf-8",
    )


class Config:
    principal_id = "mikebook_codex"


class Client:
    config = Config()

    def __init__(self, lease):
        self.lease = lease

    def get_resource(self, resource):
        return self.lease


class CountingClient(Client):
    def __init__(self, lease):
        super().__init__(lease)
        self.calls = 0

    def get_resource(self, resource):
        self.calls += 1
        return super().get_resource(resource)


def arm_stale(state_dir: Path):
    (state_dir / "ARMED").write_text("armed\n", encoding="utf-8")
    (state_dir / "supervisor-state.json").write_text(
        json.dumps({"mode": "bootstrap-authoritative"}), encoding="utf-8"
    )
    (state_dir / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 1}), encoding="utf-8"
    )


def write_headless_authority(state_dir: Path, *, recorded_ts=501, epoch=8):
    (state_dir / "supervisor-heartbeat.json").write_text(
        json.dumps(
            {
                "recorded_ts": recorded_ts,
                "authority": True,
                "ownership": "headless",
                "controller_epoch": epoch,
            }
        ),
        encoding="utf-8",
    )


def test_unarmed_watchdog_is_inert_without_coord_provider(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    factory_calls = []

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        client_factory=lambda: factory_calls.append(True),
        now_ts=500,
    )

    assert result == {"ok": True, "armed": False, "action": "inert"}
    assert factory_calls == []


def test_edge_health_rejects_loaded_manifest_drift(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"manifest_id":"current"}\n', encoding="utf-8")
    monkeypatch.setattr(
        watchdog,
        "request_edge",
        lambda *_args, **_kwargs: {
            "ok": True,
            "manifest_id": "current",
            "manifest_sha256": "0" * 64,
        },
    )

    result = watchdog.edge_health(manifest)

    assert result["ok"] is False
    assert "does not match" in result["error"]
    assert result["observed_manifest_sha256"] == "0" * 64
    assert result["expected_manifest_sha256"] != "0" * 64


def test_edge_health_accepts_exact_loaded_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"manifest_id":"current"}\n', encoding="utf-8")
    digest = watchdog.hashlib.sha256(manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(
        watchdog,
        "request_edge",
        lambda *_args, **_kwargs: {
            "ok": True,
            "manifest_id": "current",
            "manifest_sha256": digest,
        },
    )

    assert watchdog.edge_health(manifest)["ok"] is True


def test_fresh_heartbeat_is_healthy_without_coord_provider(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        client_factory=lambda: (_ for _ in ()).throw(CoordError("offline")),
        now_ts=500,
    )

    assert result["action"] == "healthy"
    assert result["heartbeat_age"] == 10


def test_transient_edge_failure_recovers_without_restart(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )
    restarts = []

    degraded = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": False, "error": "socket timeout"},
        edge_restart_fn=lambda: restarts.append(True) or {"ok": True},
        now_ts=500,
    )
    healthy = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": True},
        edge_restart_fn=lambda: restarts.append(True) or {"ok": True},
        now_ts=501,
    )

    assert degraded["action"] == "edge-health-degraded"
    assert healthy["action"] == "healthy"
    assert restarts == []
    state = json.loads((tmp_path / "watchdog-state.json").read_text())
    assert state["edge_health_failures"] == 0


def test_two_edge_health_failures_trigger_bounded_restart_receipt(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )
    restarts = []

    first = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": False, "error": "hung"},
        edge_restart_fn=lambda: restarts.append(True) or {"ok": True},
        now_ts=500,
    )
    second = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": False, "error": "hung"},
        edge_restart_fn=lambda: restarts.append(True) or {"ok": True},
        now_ts=501,
    )

    assert first["action"] == "edge-health-degraded"
    assert second["action"] == "edge-restarted"
    assert second["receipt"]["health_failures"] == 2
    assert restarts == [True]
    receipts = (tmp_path / "edge-recovery-receipts.jsonl").read_text()
    assert '"kind":"edge-recovery"' in receipts


def test_edge_restart_targets_only_registered_launchagent(monkeypatch):
    seen = []
    monkeypatch.setattr(watchdog.os, "getuid", lambda: 501)

    result = watchdog.restart_edge(
        run=lambda command, **kwargs: seen.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0, "", "")
    )

    assert result["ok"] is True
    assert seen[0][0] == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/com.local.cos-iterm-edge",
    ]
    assert seen[0][1]["timeout"] == 10


def test_persistent_edge_restart_backoff_is_exponential_capped_and_health_reset(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )
    now_ts = 500
    observed = []

    for _ in range(6):
        (tmp_path / "supervisor-heartbeat.json").write_text(
            json.dumps({"recorded_ts": now_ts - 10}), encoding="utf-8"
        )
        degraded = watchdog.run_once(
            manifest_path=manifest,
            state_dir=tmp_path,
            client=None,
            edge_health_fn=lambda: {"ok": False, "error": "still hung"},
            edge_restart_fn=lambda: {"ok": True},
            now_ts=now_ts,
        )
        assert degraded["action"] == "edge-health-degraded"
        restarted = watchdog.run_once(
            manifest_path=manifest,
            state_dir=tmp_path,
            client=None,
            edge_health_fn=lambda: {"ok": False, "error": "still hung"},
            edge_restart_fn=lambda: {"ok": True},
            now_ts=now_ts + 1,
        )
        assert restarted["action"] == "edge-restarted"
        backoff = restarted["receipt"]["backoff_seconds"]
        observed.append(backoff)
        now_ts += 1 + backoff

    assert observed == [60, 120, 240, 480, 900, 900]
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": now_ts}), encoding="utf-8"
    )
    healthy = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=None,
        edge_health_fn=lambda: {"ok": True},
        edge_restart_fn=lambda: {"ok": True},
        now_ts=now_ts,
    )
    assert healthy["action"] == "healthy"
    state = json.loads((tmp_path / "watchdog-state.json").read_text())
    assert state["edge_restart_attempts"] == 0
    assert state["edge_restart_backoff_until"] is None


def test_stale_heartbeat_checks_lease_even_when_edge_is_unhealthy(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="tab")
    arm_stale(tmp_path)
    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        edge_health_fn=lambda: {"ok": False, "error": "edge unavailable"},
        edge_restart_fn=lambda: {"ok": False, "error": "restart failed"},
        now_ts=500,
    )
    assert result["action"] == "awaiting-visible-lease-expiry-for-headless-trial"
    assert result["live_epoch"] == 7


def test_edge_backoff_keeps_probing_without_restarting(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    arm_stale(tmp_path)
    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 490}), encoding="utf-8"
    )
    restarts = []
    kwargs = {
        "manifest_path": manifest,
        "state_dir": tmp_path,
        "client": None,
        "edge_health_fn": lambda: {"ok": False, "error": "hung"},
        "edge_restart_fn": lambda: restarts.append(True) or {"ok": True},
    }
    watchdog.run_once(**kwargs, now_ts=500)
    watchdog.run_once(**kwargs, now_ts=501)
    watchdog.run_once(**kwargs, now_ts=510)
    backed_off = watchdog.run_once(**kwargs, now_ts=511)

    assert backed_off["action"] == "edge-restart-backoff-health-only"
    assert backed_off["backoff_seconds"] == 50
    assert restarts == [True]


def test_tab_recovery_uses_iterm_api_edge_poke(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="tab")
    arm_stale(tmp_path)
    seen = []

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=500,
        poke_fn=lambda **kwargs: seen.append(kwargs) or {"ok": True, "submit_method": "iterm2-python-api-crlf"},
    )

    assert result["action"] == "tab-poke"
    assert seen[0]["controller_epoch"] == 7


def test_failed_tab_poke_does_not_create_pending_recovery(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="tab")
    arm_stale(tmp_path)
    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=500,
        poke_fn=lambda **_kwargs: {"ok": False, "error": "no acknowledgment"},
    )
    state = json.loads((tmp_path / "watchdog-state.json").read_text())
    assert result["ok"] is False
    assert state["pending_since"] is None
    assert state["pending_key"] is None
    assert state["pending_transport"] is None


def test_headless_trial_waits_for_epoch_expiry_then_resumes_same_uuid(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)
    commands = []

    def resume(command, **kwargs):
        commands.append(command)
        write_headless_authority(tmp_path)
        return subprocess.CompletedProcess(command, 0, "done", "")

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client(None),
        now_ts=500,
        run=resume,
    )

    assert result["action"] == "headless-resume"
    assert result["receipt"]["success"] is True
    assert commands[0][:4] == ["codex", "exec", "resume", "cli-cos"]
    assert result["receipt"]["visible_reattach_required"] is True
    assert result["receipt"]["authority_acquired"] is True
    assert result["receipt"]["controller_epoch"] == 8


def test_headless_trial_never_starts_while_visible_epoch_is_live(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client({"holder": "mikebook_codex", "epoch": 7}),
        now_ts=500,
    )

    assert result["action"] == "awaiting-visible-lease-expiry-for-headless-trial"


def test_two_fenced_tab_pokes_then_headless_waits_for_lease_expiry(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="tab")
    arm_stale(tmp_path)
    seen = []
    client = Client({"holder": "mikebook_codex", "epoch": 7})

    first = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=client,
        now_ts=500,
        poke_fn=lambda **kwargs: seen.append(kwargs) or {"ok": True},
    )
    second = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=client,
        now_ts=501,
        poke_fn=lambda **kwargs: seen.append(kwargs) or {"ok": True},
    )
    third = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=client,
        now_ts=502,
        poke_fn=lambda **kwargs: seen.append(kwargs) or {"ok": True},
    )

    assert [first["action"], second["action"], third["action"]] == [
        "tab-poke",
        "tab-poke",
        "awaiting-visible-lease-expiry-for-headless-trial",
    ]
    assert len(seen) == 2
    assert seen[0]["idempotency_key"] != seen[1]["idempotency_key"]
    assert all(item["controller_epoch"] == 7 for item in seen)


def test_zero_exit_without_headless_authority_fails_closed(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)

    result = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=Client(None),
        now_ts=500,
        run=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "done", ""
        ),
    )

    assert result["ok"] is False
    assert result["receipt"]["authority_acquired"] is False
    assert result["backoff_seconds"] == 60


def test_provider_backoff_still_checks_lease_and_fresh_heartbeat(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)
    first_client = CountingClient(None)

    failed = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=first_client,
        now_ts=500,
        run=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("provider unavailable")
        ),
    )
    assert failed["backoff_seconds"] == 60
    assert failed["receipt"]["provider_error"] == "OSError"

    backoff_client = CountingClient(None)
    backed_off = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=backoff_client,
        now_ts=501,
    )
    assert backed_off["action"] == "provider-backoff-health-only"
    assert backed_off["lease_checked"] is True
    assert backoff_client.calls == 1

    (tmp_path / "supervisor-heartbeat.json").write_text(
        json.dumps({"recorded_ts": 502}), encoding="utf-8"
    )
    healthy_client = CountingClient(None)
    healthy = watchdog.run_once(
        manifest_path=manifest,
        state_dir=tmp_path,
        client=healthy_client,
        now_ts=503,
    )
    assert healthy["action"] == "healthy"
    assert healthy_client.calls == 0


def test_provider_failure_backoff_is_exponential_and_capped(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, recovery="headless")
    arm_stale(tmp_path)
    now_ts = 500
    observed = []

    for _ in range(6):
        result = watchdog.run_once(
            manifest_path=manifest,
            state_dir=tmp_path,
            client=Client(None),
            now_ts=now_ts,
            run=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("provider unavailable")
            ),
        )
        observed.append(result["backoff_seconds"])
        now_ts += result["backoff_seconds"]

    assert observed == [60, 120, 240, 480, 900, 900]
