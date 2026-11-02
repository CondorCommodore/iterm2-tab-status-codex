#!/usr/bin/env python3
"""Install and verify COS iTerm API scripts."""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ITERM_SUPPORT = Path.home() / "Library" / "Application Support" / "iTerm2"

AUTOLAUNCH_SCRIPTS = (
    "cos_iterm_overlay.py",
    "cos_iterm_daemon.py",
)
LEGACY_AUTOLAUNCH_SCRIPTS = ("cos_iterm_edge_daemon.py",)
LEGACY_LAUNCH_AGENT_LABEL = "com.local.cos-iterm-edge"
MENU_SCRIPTS = (
    "cos_iterm_readback.py",
    "cos_iterm_daemon.py",
    "cos_assignment_policy.py",
    "cos_dashboard.py",
    "cos_report_parser.py",
    "c2_contract.py",
    "c2_coord_client.py",
    "c2_runtime_observation.py",
    "c2_runtime_hook.py",
    "c2_visual_decision.py",
)


def _run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def _launchctl_reports_missing_service(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout or ''}\n{result.stderr or ''}".casefold()
    return result.returncode != 0 and "could not find service" in output


def retire_legacy_edge(
    launch_agents_dir: Path | None = None,
    *,
    launchctl_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    uid: int | None = None,
) -> dict[str, object]:
    """Unload a prior KeepAlive edge job and remove its user LaunchAgent plist.

    The job can remain loaded after its source plist has disappeared, so inspect
    and unload by its fixed label even when no AutoLaunch copy or plist exists.
    Unknown launchctl errors fail closed; only the explicit not-loaded response
    is treated as an idempotent no-op.
    """
    agents_dir = launch_agents_dir or (Path.home() / "Library" / "LaunchAgents")
    runner = launchctl_runner or _run_launchctl
    target = f"gui/{os.getuid() if uid is None else uid}/{LEGACY_LAUNCH_AGENT_LABEL}"
    inspection = runner(["print", target])
    if inspection.returncode == 0:
        loaded = True
    elif _launchctl_reports_missing_service(inspection):
        loaded = False
    else:
        detail = (inspection.stderr or inspection.stdout or "unknown launchctl error").strip()
        raise RuntimeError(f"unable to inspect legacy LaunchAgent {target}: {detail}")

    if loaded:
        bootout = runner(["bootout", target])
        if bootout.returncode != 0 and not _launchctl_reports_missing_service(bootout):
            detail = (bootout.stderr or bootout.stdout or "unknown launchctl error").strip()
            raise RuntimeError(f"unable to unload legacy LaunchAgent {target}: {detail}")

    plist = agents_dir / f"{LEGACY_LAUNCH_AGENT_LABEL}.plist"
    removed = False
    if plist.exists() or plist.is_symlink():
        plist.unlink()
        removed = True
    return {"loaded": loaded, "stopped": True, "plist_removed": removed}


def install_scripts(
    *,
    repo_root: Path = REPO_ROOT,
    iterm_support: Path = DEFAULT_ITERM_SUPPORT,
    launch_agents_dir: Path | None = None,
    launchctl_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, object]:
    scripts_dir = repo_root / "scripts"
    autolaunch_dir = iterm_support / "Scripts" / "AutoLaunch"
    menu_dir = iterm_support / "Scripts"
    autolaunch_dir.mkdir(parents=True, exist_ok=True)
    menu_dir.mkdir(parents=True, exist_ok=True)
    removed: list[str] = []
    launch_agent_retirement = retire_legacy_edge(
        launch_agents_dir, launchctl_runner=launchctl_runner
    )
    for name in LEGACY_AUTOLAUNCH_SCRIPTS:
        legacy = autolaunch_dir / name
        if legacy.exists() or legacy.is_symlink():
            legacy.unlink()
            removed.append(str(legacy))
    installed: list[dict[str, object]] = []
    for name in AUTOLAUNCH_SCRIPTS:
        src = scripts_dir / name
        dst = autolaunch_dir / name
        shutil.copy2(src, dst)
        installed.append(
            {
                "name": name,
                "path": str(dst),
                "matches": filecmp.cmp(src, dst, shallow=False),
            }
        )
    for name in MENU_SCRIPTS:
        src = scripts_dir / name
        dst = menu_dir / name
        shutil.copy2(src, dst)
        installed.append(
            {
                "name": name,
                "path": str(dst),
                "matches": filecmp.cmp(src, dst, shallow=False),
            }
        )
    return {
        "ok": all(item["matches"] for item in installed),
        "installed": installed,
        "removed_legacy_autolaunch": removed,
        "legacy_launch_agent_retirement": launch_agent_retirement,
        "reload_note": (
            "Restart iTerm2 or run the scripts from iTerm2 Scripts menu to load new API code."
        ),
        "readback_script": str(menu_dir / "cos_iterm_readback.py"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install COS iTerm API scripts.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--iterm-support", type=Path, default=DEFAULT_ITERM_SUPPORT)
    parser.add_argument(
        "--retire-legacy-launch-agent",
        action="store_true",
        help="unload the retired edge LaunchAgent and remove its fixed plist",
    )
    args = parser.parse_args(argv)
    if args.retire_legacy_launch_agent:
        result = retire_legacy_edge()
        ok = result["stopped"]
    else:
        result = install_scripts(repo_root=args.repo_root, iterm_support=args.iterm_support)
        ok = result["ok"]
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
