from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_installer_renders_selected_manifest_state_and_log_paths(tmp_path):
    home = tmp_path / "home"
    manifest = tmp_path / "selected & manifest.json"
    state_dir = tmp_path / "selected & state"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    manifest.write_text("{}\n", encoding="utf-8")
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        }
    )

    result = subprocess.run(
        [
            "/bin/bash",
            str(ROOT / "launchd/install-cos-bootstrap-watchdog-launchd.sh"),
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
    rendered = home / "Library/LaunchAgents/com.local.cos-bootstrap-watchdog.plist"
    with rendered.open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["ProgramArguments"] == [
        "/usr/bin/python3",
        str(ROOT / "scripts/cos_bootstrap_watchdog.py"),
        "--manifest",
        str(manifest),
        "--state-dir",
        str(state_dir),
    ]
    assert plist["StandardOutPath"] == str(state_dir / "watchdog.log")
    assert plist["StandardErrorPath"] == str(state_dir / "watchdog.log")
