from __future__ import annotations

import ast
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def installer_fixture(tmp_path, *, include_dispatcher: bool) -> Path:
    repo = tmp_path / "installer-repo"
    launchd = repo / "launchd"
    scripts = repo / "scripts"
    launchd.mkdir(parents=True)
    scripts.mkdir()
    shutil.copy2(ROOT / "launchd/install-cos-iterm-edge-launchd.sh", launchd)
    shutil.copy2(ROOT / "launchd/com.local.cos-iterm-edge.plist", launchd)
    shutil.copy2(ROOT / "scripts/cos_iterm_edge_daemon.py", scripts)
    if include_dispatcher:
        (scripts / "cos_tab_dispatch.py").write_text(
            "# installer dependency fixture\n", encoding="utf-8"
        )
    return repo


def test_installer_pins_runtime_manifest_state_socket_and_keepalive(tmp_path):
    repo = installer_fixture(tmp_path, include_dispatcher=True)
    home = tmp_path / "home"
    manifest = tmp_path / "selected & manifest.json"
    state_dir = tmp_path / "selected & state"
    socket_path = tmp_path / "selected & socket" / "edge.sock"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    manifest.write_text("{}\n", encoding="utf-8")
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.symlink_to("/usr/bin/python3")
    env = os.environ.copy()
    env.update({"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"})

    result = subprocess.run(
        [
            "/bin/bash",
            str(repo / "launchd/install-cos-iterm-edge-launchd.sh"),
            "--manifest",
            str(manifest),
            "--state-dir",
            str(state_dir),
            "--socket",
            str(socket_path),
            "--python",
            str(fake_python),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = home / "Library/LaunchAgents/com.local.cos-iterm-edge.plist"
    with rendered.open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["ProgramArguments"] == [
        str(fake_python),
        str(repo / "scripts/cos_iterm_edge_daemon.py"),
        "--manifest",
        str(manifest),
        "--state-dir",
        str(state_dir),
        "--socket",
        str(socket_path),
    ]
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] == 10
    assert plist["StandardOutPath"] == str(state_dir / "iterm-edge.log")
    assert plist["StandardErrorPath"] == str(state_dir / "iterm-edge.log")


def test_installer_refuses_daemon_with_retired_dispatcher_before_launchctl(tmp_path):
    repo = installer_fixture(tmp_path, include_dispatcher=False)
    daemon_path = repo / "scripts/cos_iterm_edge_daemon.py"
    daemon_source = daemon_path.read_text(encoding="utf-8")
    daemon_ast = ast.parse(daemon_source)
    assert any(
        isinstance(node, ast.ImportFrom) and node.module == "cos_tab_dispatch"
        for node in ast.walk(daemon_ast)
    ), "fixture no longer captures the daemon's retired dispatcher dependency"
    home = tmp_path / "home"
    manifest = tmp_path / "manifest.json"
    state_dir = tmp_path / "state"
    socket_path = tmp_path / "socket" / "edge.sock"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    manifest.write_text("{}\n", encoding="utf-8")
    launchctl_calls = tmp_path / "launchctl-calls.txt"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$LAUNCHCTL_CALLS"\nexit 0\n',
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.symlink_to("/usr/bin/python3")
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    env["LAUNCHCTL_CALLS"] = str(launchctl_calls)

    result = subprocess.run(
        [
            "/bin/bash",
            str(repo / "launchd/install-cos-iterm-edge-launchd.sh"),
            "--manifest",
            str(manifest),
            "--state-dir",
            str(state_dir),
            "--socket",
            str(socket_path),
            "--python",
            str(fake_python),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0, result.stdout
    assert "cos_tab_dispatch.py" in result.stderr
    assert "daemon cannot start" in result.stderr
    assert not launchctl_calls.exists(), (
        "installer reached launchctl despite missing daemon dependency"
    )
    assert not (home / "Library/LaunchAgents/com.local.cos-iterm-edge.plist").exists()
    assert not state_dir.exists()
    assert not socket_path.parent.exists()


def test_installer_discovers_highest_semantic_iterm_runtime(tmp_path):
    repo = installer_fixture(tmp_path, include_dispatcher=True)
    home = tmp_path / "home"
    manifest = tmp_path / "manifest.json"
    state_dir = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    manifest.write_text("{}\n", encoding="utf-8")
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    for version in ("3.8.19", "3.10.19", "3.11.9"):
        runtime = (
            home / "Library/Application Support/iTerm2/iterm2env/versions" / version / "bin/python3"
        )
        runtime.parent.mkdir(parents=True)
        import_status = "1" if version == "3.11.9" else "0"
        runtime.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ] && [ "$2" = "import iterm2" ]; then '
            f"exit {import_status}; fi\n"
            'exec /usr/bin/python3 "$@"\n',
            encoding="utf-8",
        )
        runtime.chmod(0o755)
    env = os.environ.copy()
    env.update({"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"})

    result = subprocess.run(
        [
            "/bin/bash",
            str(repo / "launchd/install-cos-iterm-edge-launchd.sh"),
            "--manifest",
            str(manifest),
            "--state-dir",
            str(state_dir),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = home / "Library/LaunchAgents/com.local.cos-iterm-edge.plist"
    with rendered.open("rb") as handle:
        plist = plistlib.load(handle)
    assert "/versions/3.10.19/bin/python3" in plist["ProgramArguments"][0]
