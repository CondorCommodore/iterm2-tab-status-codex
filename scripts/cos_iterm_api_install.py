#!/usr/bin/env python3
"""Install and verify COS iTerm API scripts."""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Callable

from cos_iterm_edge_client import EdgeClientError, request_edge

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ITERM_SUPPORT = Path.home() / "Library" / "Application Support" / "iTerm2"

AUTOLAUNCH_SCRIPTS = (
    "cos_iterm_overlay.py",
    "cos_iterm_daemon.py",
)
LEGACY_AUTOLAUNCH_SCRIPTS = ("cos_iterm_edge_daemon.py",)
DEFAULT_EDGE_SOCKET = Path.home() / ".cache" / "cos-c2" / "iterm-edge.sock"
MENU_SCRIPTS = (
    "cos_iterm_readback.py",
    "cos_iterm_daemon.py",
    "cos_tab_dispatch.py",
    "cos_dispatch_orchestrator.py",
    "cos_assignment_policy.py",
    "cos_dashboard.py",
    "cos_report_parser.py",
    "c2_contract.py",
    "c2_coord_client.py",
    "c2_visual_decision.py",
    "cos_iterm_edge_client.py",
    "cos_iterm_edge_daemon.py",
)


def retire_legacy_edge(socket_path: Path = DEFAULT_EDGE_SOCKET) -> dict[str, object]:
    """Stop a same-user legacy edge after proving its protocol and PID."""
    try:
        health = request_edge(
            {"protocol": "cos-c2-iterm-edge-v1", "op": "health"},
            socket_path=socket_path,
            timeout_seconds=2.0,
        )
    except EdgeClientError:
        return {"running": False, "stopped": True}
    pid = health.get("pid")
    if (
        health.get("protocol") != "cos-c2-iterm-edge-v1"
        or isinstance(pid, bool)
        or not isinstance(pid, int)
    ):
        raise RuntimeError("refusing to stop unverified process behind legacy edge socket")
    if pid == os.getpid():
        raise RuntimeError("refusing to stop installer process")
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {"running": True, "stopped": True, "pid": pid}
        time.sleep(0.05)
    raise RuntimeError(f"legacy edge pid {pid} did not stop")


def install_scripts(
    *,
    repo_root: Path = REPO_ROOT,
    iterm_support: Path = DEFAULT_ITERM_SUPPORT,
    retire_legacy: Callable[[], dict[str, object]] = retire_legacy_edge,
) -> dict[str, object]:
    scripts_dir = repo_root / "scripts"
    autolaunch_dir = iterm_support / "Scripts" / "AutoLaunch"
    menu_dir = iterm_support / "Scripts"
    autolaunch_dir.mkdir(parents=True, exist_ok=True)
    menu_dir.mkdir(parents=True, exist_ok=True)
    removed: list[str] = []
    legacy_retirement: dict[str, object] = {"running": False, "stopped": True}
    if any(
        (autolaunch_dir / name).exists() or (autolaunch_dir / name).is_symlink()
        for name in LEGACY_AUTOLAUNCH_SCRIPTS
    ):
        legacy_retirement = retire_legacy()
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
        "legacy_edge_retirement": legacy_retirement,
        "reload_note": (
            "Restart iTerm2 or run the scripts from iTerm2 Scripts menu to load new API code."
        ),
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
