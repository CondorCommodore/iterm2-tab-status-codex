from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_iterm_api_install as installer  # noqa: E402


def test_retire_legacy_edge_requires_verified_protocol(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "request_edge", lambda *_args, **_kwargs: {"pid": 123})
    try:
        installer.retire_legacy_edge(tmp_path / "edge.sock")
    except RuntimeError as exc:
        assert "unverified process" in str(exc)
    else:
        raise AssertionError("unverified legacy process was accepted")


def test_retire_legacy_edge_terminates_verified_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(
        installer,
        "request_edge",
        lambda *_args, **_kwargs: {
            "protocol": "cos-c2-iterm-edge-v1",
            "pid": 43210,
        },
    )
    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(installer.os, "kill", fake_kill)
    result = installer.retire_legacy_edge(tmp_path / "edge.sock")
    assert result == {"running": True, "stopped": True, "pid": 43210}
    assert calls[0] == (43210, installer.signal.SIGTERM)


def test_install_scripts_copies_and_verifies(tmp_path):
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in installer.AUTOLAUNCH_SCRIPTS + installer.MENU_SCRIPTS:
        (scripts_dir / name).write_text(f"#!/usr/bin/env python3\n# {name}\n", encoding="utf-8")
    daemon_source = scripts_dir / "cos_iterm_edge_daemon.py"
    daemon_source.write_text(
        "#!/usr/bin/env python3\n# source remains for slice 2a-ii\n", encoding="utf-8"
    )

    def unexpected_retirement():
        raise AssertionError("packaging-only slice must not stop a daemon")

    result = installer.install_scripts(
        repo_root=repo_root,
        iterm_support=tmp_path / "iterm",
        retire_legacy=unexpected_retirement,
    )

    assert result["ok"] is True
    assert len(result["installed"]) == 15
    assert daemon_source.exists()
    assert not (tmp_path / "iterm" / "Scripts" / "cos_iterm_edge_daemon.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "AutoLaunch" / "cos_iterm_overlay.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "AutoLaunch" / "cos_iterm_daemon.py").exists()
    assert result["removed_legacy_autolaunch"] == []
    assert result["legacy_edge_retirement"] == {"running": False, "stopped": True}
    assert (tmp_path / "iterm" / "Scripts" / "cos_iterm_daemon.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "cos_tab_dispatch.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "cos_dashboard.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "c2_contract.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "c2_runtime_observation.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "c2_runtime_hook.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "c2_visual_decision.py").exists()
    assert not (tmp_path / "iterm" / "Scripts" / "cos_iterm_edge_daemon.py").exists()
