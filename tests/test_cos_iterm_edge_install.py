from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_installer_pins_runtime_manifest_state_socket_and_keepalive(tmp_path):
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
            str(ROOT / "launchd/install-cos-iterm-edge-launchd.sh"),
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
        str(ROOT / "scripts/cos_iterm_edge_daemon.py"),
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


def test_installer_discovers_highest_semantic_iterm_runtime(tmp_path):
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
            str(ROOT / "launchd/install-cos-iterm-edge-launchd.sh"),
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
