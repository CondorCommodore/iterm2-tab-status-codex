#!/usr/bin/env python3
"""Assignment policy for COS worker tabs."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_POLICY = {
    "host_priority": ["forge", "aurora", "macbook", "local"],
    "prefer_states": ["idle"],
    "avoid_states": ["running", "attention", "unknown"],
    "role_field": "role",
}


@dataclass(frozen=True)
class Assignment:
    tty: str
    reason: str
    tab: dict[str, Any]


def load_policy(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        return dict(DEFAULT_POLICY)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(DEFAULT_POLICY)
    policy = dict(DEFAULT_POLICY)
    if isinstance(payload, dict):
        policy.update(payload)
    return policy


def _tab_host(tab: dict[str, Any]) -> str:
    text = " ".join(
        str(tab.get(key) or "")
        for key in ("project", "cwd", "message", "tty")
    ).lower()
    for host in ("forge", "aurora", "macbook"):
        if host in text:
            return host
    return "local"


def _is_worker(tab: dict[str, Any]) -> bool:
    return str(tab.get("role") or tab.get("user.cosRole") or "worker") != "cos"


def rank_tab(
    tab: dict[str, Any],
    policy: dict[str, Any],
    *,
    target_host: str = "",
) -> tuple[int, int, str]:
    state = str(tab.get("state") or "unknown")
    prefer_states = list(policy.get("prefer_states") or [])
    avoid_states = set(policy.get("avoid_states") or [])
    host_priority = list(policy.get("host_priority") or [])
    host = _tab_host(tab)
    state_rank = prefer_states.index(state) if state in prefer_states else 50
    if state in avoid_states:
        state_rank += 100
    if target_host and host != target_host:
        host_rank = 50 + (host_priority.index(host) if host in host_priority else 99)
    else:
        host_rank = host_priority.index(host) if host in host_priority else 99
    return (state_rank, host_rank, str(tab.get("tty") or ""))


def choose_worker(
    tabs: list[dict[str, Any]],
    *,
    policy: dict[str, Any] | None = None,
    target_host: str = "",
) -> Assignment | None:
    policy = dict(DEFAULT_POLICY) if policy is None else policy
    candidates = [
        tab
        for tab in tabs
        if isinstance(tab, dict)
        and tab.get("tty")
        and _is_worker(tab)
        and str(tab.get("state") or "unknown") not in {"attention"}
    ]
    if not candidates:
        return None
    selected = sorted(
        candidates,
        key=lambda tab: rank_tab(tab, policy, target_host=target_host),
    )[0]
    return Assignment(
        tty=str(selected["tty"]),
        reason=(
            f"selected state={selected.get('state')} "
            f"host={_tab_host(selected)} "
            f"target_host={target_host or '*'}"
        ),
        tab=selected,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Choose a worker tab for a COS assignment."
    )
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--policy-path", type=Path)
    parser.add_argument("--target-host", default="")
    args = parser.parse_args(argv)
    state = json.loads(args.state_path.read_text(encoding="utf-8"))
    assignment = choose_worker(
        state.get("tabs", []),
        policy=load_policy(args.policy_path),
        target_host=args.target_host,
    )
    print(
        json.dumps(
            None if assignment is None else assignment.__dict__,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
