#!/usr/bin/env python3
"""Install and verify COS iTerm API scripts."""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ITERM_SUPPORT = Path.home() / "Library" / "Application Support" / "iTerm2"

AUTOLAUNCH_SCRIPTS = ("cos_iterm_overlay.py",)
MENU_SCRIPTS = (
    "cos_iterm_readback.py",
    "cos_tab_dispatch.py",
    "cos_dispatch_orchestrator.py",
    "cos_assignment_policy.py",
    "cos_dashboard.py",
    "cos_report_parser.py",
)


def install_scripts(
    *,
    repo_root: Path = REPO_ROOT,
    iterm_support: Path = DEFAULT_ITERM_SUPPORT,
) -> dict[str, object]:
    scripts_dir = repo_root / "scripts"
    autolaunch_dir = iterm_support / "Scripts" / "AutoLaunch"
    menu_dir = iterm_support / "Scripts"
    autolaunch_dir.mkdir(parents=True, exist_ok=True)
    menu_dir.mkdir(parents=True, exist_ok=True)
    installed: list[dict[str, object]] = []
    for name in AUTOLAUNCH_SCRIPTS:
        src = scripts_dir / name
        dst = autolaunch_dir / name
        shutil.copy2(src, dst)
        installed.append({"name": name, "path": str(dst), "matches": filecmp.cmp(src, dst, shallow=False)})
    for name in MENU_SCRIPTS:
        src = scripts_dir / name
        dst = menu_dir / name
        shutil.copy2(src, dst)
        installed.append({"name": name, "path": str(dst), "matches": filecmp.cmp(src, dst, shallow=False)})
    return {
        "ok": all(item["matches"] for item in installed),
        "installed": installed,
        "reload_note": "Restart iTerm2 or run the scripts from iTerm2 Scripts menu to load new API code.",
        "readback_script": str(menu_dir / "cos_iterm_readback.py"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install COS iTerm API scripts.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--iterm-support", type=Path, default=DEFAULT_ITERM_SUPPORT)
    args = parser.parse_args(argv)
    result = install_scripts(repo_root=args.repo_root, iterm_support=args.iterm_support)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
