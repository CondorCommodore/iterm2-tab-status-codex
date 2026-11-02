from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


def _make_installer_repo(tmp_path):
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in installer.AUTOLAUNCH_SCRIPTS + installer.MENU_SCRIPTS:
        (scripts_dir / name).write_text(f"#!/usr/bin/env python3\n# {name}\n", encoding="utf-8")
    (scripts_dir / "cos_iterm_edge_daemon.py").write_text("legacy source\n", encoding="utf-8")
    return repo_root


def test_install_scripts_does_not_copy_legacy_daemon(tmp_path):
    repo_root = _make_installer_repo(tmp_path)
    support = tmp_path / "iterm"
    result = installer.install_scripts(repo_root=repo_root, iterm_support=support)

    assert result["ok"] is True
    assert (repo_root / "scripts" / "cos_iterm_edge_daemon.py").exists()
    assert not (support / "Scripts" / "AutoLaunch" / "cos_iterm_edge_daemon.py").exists()
    assert not (support / "Scripts" / "cos_iterm_edge_daemon.py").exists()


@pytest.mark.parametrize("legacy_kind", ["file", "symlink"])
def test_install_scripts_retires_and_unlinks_legacy_daemon(tmp_path, monkeypatch, legacy_kind):
    repo_root = _make_installer_repo(tmp_path)
    support = tmp_path / "iterm"
    legacy = support / "Scripts" / "AutoLaunch" / "cos_iterm_edge_daemon.py"
    legacy.parent.mkdir(parents=True)
    if legacy_kind == "symlink":
        target = tmp_path / "old-daemon.py"
        target.write_text("legacy daemon\n", encoding="utf-8")
        legacy.symlink_to(target)
    else:
        legacy.write_text("legacy daemon\n", encoding="utf-8")

    monkeypatch.setattr(
        installer,
        "request_edge",
        lambda *_args, **_kwargs: {"protocol": "cos-c2-iterm-edge-v1", "pid": 43210},
    )
    kill_calls = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(installer.os, "kill", fake_kill)
    result = installer.install_scripts(repo_root=repo_root, iterm_support=support)

    assert result["ok"] is True
    assert not legacy.exists()
    assert not legacy.is_symlink()
    assert result["removed_legacy_autolaunch"] == [str(legacy)]
    assert result["legacy_edge_retirement"] == {"running": True, "stopped": True, "pid": 43210}
    assert kill_calls[0] == (43210, installer.signal.SIGTERM)


def test_install_scripts_without_legacy_copy_skips_retirement(tmp_path):
    repo_root = _make_installer_repo(tmp_path)

    def unexpected_retirement():
        raise AssertionError("retirement must be skipped when no legacy copy exists")

    result = installer.install_scripts(
        repo_root=repo_root,
        iterm_support=tmp_path / "iterm",
        retire_legacy=unexpected_retirement,
    )

    assert result["ok"] is True
    assert result["removed_legacy_autolaunch"] == []
    assert result["legacy_edge_retirement"] == {"running": False, "stopped": True}
