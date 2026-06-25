from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cos_iterm_api_install as installer  # noqa: E402


def test_install_scripts_copies_and_verifies(tmp_path):
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in installer.AUTOLAUNCH_SCRIPTS + installer.MENU_SCRIPTS:
        (scripts_dir / name).write_text(f"#!/usr/bin/env python3\n# {name}\n", encoding="utf-8")

    result = installer.install_scripts(
        repo_root=repo_root,
        iterm_support=tmp_path / "iterm",
    )

    assert result["ok"] is True
    assert len(result["installed"]) == 7
    assert (tmp_path / "iterm" / "Scripts" / "AutoLaunch" / "cos_iterm_overlay.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "cos_tab_dispatch.py").exists()
    assert (tmp_path / "iterm" / "Scripts" / "cos_dashboard.py").exists()
