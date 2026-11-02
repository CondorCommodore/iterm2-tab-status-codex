from __future__ import annotations

import os
import subprocess
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
    assert len(result["installed"]) == 13
    assert daemon_source.exists()
    assert not (tmp_path / "iterm" / "Scripts" / "cos_iterm_edge_daemon.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "AutoLaunch" / "cos_iterm_overlay.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "AutoLaunch" / "cos_iterm_daemon.py").exists()
    assert result["removed_legacy_autolaunch"] == []
    assert result["legacy_edge_retirement"] == {"running": False, "stopped": True}
    assert (tmp_path / "iterm" / "Scripts" / "cos_iterm_daemon.py").exists()
    assert not (tmp_path / "iterm" / "Scripts" / "cos_tab_dispatch.py").exists()
    assert not (tmp_path / "iterm" / "Scripts" / "cos_dispatch_orchestrator.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "cos_dashboard.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "c2_contract.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "c2_runtime_observation.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "c2_runtime_hook.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "c2_visual_decision.py").exists()


def test_install_scripts_skips_retired_dispatcher_sources(tmp_path):
    repo_root = _make_installer_repo(tmp_path)
    scripts_dir = repo_root / "scripts"
    for name in ("cos_tab_dispatch.py", "cos_dispatch_orchestrator.py"):
        (scripts_dir / name).write_text("dispatcher source\n", encoding="utf-8")

    support = tmp_path / "iterm"
    result = installer.install_scripts(repo_root=repo_root, iterm_support=support)

    installed_names = {item["name"] for item in result["installed"]}
    assert result["ok"] is True
    assert not {"cos_tab_dispatch.py", "cos_dispatch_orchestrator.py"} & installed_names
    assert not (support / "Scripts" / "cos_tab_dispatch.py").exists()
    assert not (support / "Scripts" / "cos_dispatch_orchestrator.py").exists()


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


@pytest.fixture
def fake_launchctl(tmp_path, monkeypatch):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    command = fake_bin / "launchctl"
    command.write_text(
        """#!/bin/sh
set -eu
echo "$*" >> "$FAKE_LAUNCHCTL_LOG"
if [ "${FAKE_LAUNCHCTL_ERROR:-0}" = 1 ]; then echo "permission denied" >&2; exit 1; fi
case "$1" in
  print)
    if [ -f "$FAKE_LAUNCHCTL_LOADED" ]; then exit 0; fi
    echo "Could not find service com.local.cos-iterm-edge" >&2; exit 113 ;;
  bootout)
    if [ "${FAKE_LAUNCHCTL_BOOTOUT_ERROR:-0}" = 1 ]; then
      echo "operation not permitted" >&2
      exit 1
    fi
    if [ -f "$FAKE_LAUNCHCTL_LOADED" ]; then rm -f "$FAKE_LAUNCHCTL_LOADED"; exit 0; fi
    echo "Could not find service com.local.cos-iterm-edge" >&2; exit 113 ;;
  *) echo "unexpected launchctl command" >&2; exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    command.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    loaded = tmp_path / "loaded-service"
    calls = tmp_path / "launchctl-calls.log"
    calls.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_LAUNCHCTL_LOADED", str(loaded))
    monkeypatch.setenv("FAKE_LAUNCHCTL_LOG", str(calls))
    monkeypatch.delenv("FAKE_LAUNCHCTL_ERROR", raising=False)
    monkeypatch.delenv("FAKE_LAUNCHCTL_BOOTOUT_ERROR", raising=False)
    return {"loaded": loaded, "calls": calls}


@pytest.fixture(autouse=True)
def _never_use_real_launchctl(fake_launchctl):
    # Every installer test in this module resolves only the temporary fake.
    return None


def test_install_scripts_retires_loaded_legacy_launchagent_without_autolaunch_copy(
    tmp_path, fake_launchctl
):
    fake_launchctl["loaded"].write_text("loaded\\n", encoding="utf-8")
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist = launch_agents / f"{installer.LEGACY_LAUNCH_AGENT_LABEL}.plist"
    plist.write_text("old KeepAlive job\\n", encoding="utf-8")
    support = tmp_path / "iterm"

    result = installer.install_scripts(
        repo_root=_make_installer_repo(tmp_path),
        iterm_support=support,
        launch_agents_dir=launch_agents,
    )

    target = f"gui/{os.getuid()}/{installer.LEGACY_LAUNCH_AGENT_LABEL}"
    assert fake_launchctl["calls"].read_text().splitlines() == [
        f"print {target}",
        f"bootout {target}",
    ]
    assert not fake_launchctl["loaded"].exists()
    assert not plist.exists()
    assert not (support / "Scripts" / "AutoLaunch" / "cos_iterm_edge_daemon.py").exists()
    assert result["legacy_launch_agent_retirement"] == {
        "loaded": True,
        "stopped": True,
        "plist_removed": True,
    }
    assert result["legacy_edge_retirement"] == {"running": False, "stopped": True}


def test_install_scripts_removes_unloaded_legacy_launchagent_and_plist(tmp_path, fake_launchctl):
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist = launch_agents / f"{installer.LEGACY_LAUNCH_AGENT_LABEL}.plist"
    plist.write_text("old KeepAlive job\\n", encoding="utf-8")
    result = installer.install_scripts(
        repo_root=_make_installer_repo(tmp_path),
        iterm_support=tmp_path / "iterm",
        launch_agents_dir=launch_agents,
    )

    target = f"gui/{os.getuid()}/{installer.LEGACY_LAUNCH_AGENT_LABEL}"
    assert fake_launchctl["calls"].read_text().splitlines() == [f"print {target}"]
    assert not plist.exists()
    assert result["legacy_launch_agent_retirement"] == {
        "loaded": False,
        "stopped": True,
        "plist_removed": True,
    }


def test_install_scripts_retires_loaded_launchagent_when_plist_absent(tmp_path, fake_launchctl):
    fake_launchctl["loaded"].write_text("loaded\\n", encoding="utf-8")
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    result = installer.install_scripts(
        repo_root=_make_installer_repo(tmp_path),
        iterm_support=tmp_path / "iterm",
        launch_agents_dir=launch_agents,
    )

    target = f"gui/{os.getuid()}/{installer.LEGACY_LAUNCH_AGENT_LABEL}"
    assert fake_launchctl["calls"].read_text().splitlines() == [
        f"print {target}",
        f"bootout {target}",
    ]
    assert not fake_launchctl["loaded"].exists()
    assert result["legacy_launch_agent_retirement"] == {
        "loaded": True,
        "stopped": True,
        "plist_removed": False,
    }


def test_install_scripts_fails_closed_on_launchctl_inspection_error(
    tmp_path, monkeypatch, fake_launchctl
):
    monkeypatch.setenv("FAKE_LAUNCHCTL_ERROR", "1")
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist = launch_agents / f"{installer.LEGACY_LAUNCH_AGENT_LABEL}.plist"
    plist.write_text("old KeepAlive job\\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unable to inspect legacy LaunchAgent"):
        installer.install_scripts(
            repo_root=_make_installer_repo(tmp_path),
            iterm_support=tmp_path / "iterm",
            launch_agents_dir=launch_agents,
        )
    assert plist.exists()
    assert len(fake_launchctl["calls"].read_text().splitlines()) == 1


def test_install_scripts_keeps_plist_when_launchctl_bootout_fails(
    tmp_path, monkeypatch, fake_launchctl
):
    fake_launchctl["loaded"].write_text("loaded\\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_LAUNCHCTL_BOOTOUT_ERROR", "1")
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist = launch_agents / f"{installer.LEGACY_LAUNCH_AGENT_LABEL}.plist"
    plist.write_text("old KeepAlive job\\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unable to unload legacy LaunchAgent"):
        installer.install_scripts(
            repo_root=_make_installer_repo(tmp_path),
            iterm_support=tmp_path / "iterm",
            launch_agents_dir=launch_agents,
        )
    assert plist.exists()
    assert fake_launchctl["loaded"].exists()
    assert fake_launchctl["calls"].read_text().splitlines()[-1].startswith("bootout ")


def test_legacy_autolaunch_cleanup_list_keeps_daemon_name():
    assert installer.LEGACY_AUTOLAUNCH_SCRIPTS == ("cos_iterm_edge_daemon.py",)


@pytest.mark.parametrize("legacy_kind", ["file", "symlink"])
def test_bootstrap_removes_preexisting_legacy_autolaunch(tmp_path, legacy_kind):
    bootstrap_script = SCRIPTS / "bootstrap.sh"
    cleanup_command = 'rm -f "$AUTOLAUNCH/cos_iterm_edge_daemon.py"'
    assert cleanup_command in bootstrap_script.read_text(encoding="utf-8")
    bootstrap_home = tmp_path / "bootstrap-home"
    bootstrap_autolaunch = (
        bootstrap_home / "Library" / "Application Support" / "iTerm2" / "Scripts" / "AutoLaunch"
    )
    bootstrap_autolaunch.mkdir(parents=True)
    bootstrap_legacy = bootstrap_autolaunch / "cos_iterm_edge_daemon.py"
    if legacy_kind == "symlink":
        target = bootstrap_home / "old-edge-daemon.py"
        target.write_text("legacy daemon\n", encoding="utf-8")
        bootstrap_legacy.symlink_to(target)
    else:
        bootstrap_legacy.write_text("legacy daemon\n", encoding="utf-8")
    bootstrap_env = os.environ.copy()
    bootstrap_env.update(HOME=str(bootstrap_home), AUTOLAUNCH=str(bootstrap_autolaunch))
    subprocess.run(["bash", "-c", cleanup_command], env=bootstrap_env, check=True)
    assert not bootstrap_legacy.exists()
    assert not bootstrap_legacy.is_symlink()


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
