#!/usr/bin/env python3
"""Durable-vs-world sweep for tracked COS PR and branch coordinates."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from typing import Any, Callable

ProbeFn = Callable[[str, dict[str, Any]], dict[str, Any]]


def _first(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _task_target(item: dict[str, Any]) -> dict[str, Any]:
    repo = _first(item.get("repo"), item.get("branch_repo"), item.get("pr_repo"))
    branch = _first(item.get("branch_name"), item.get("branch"))
    pr_url = _first(item.get("pr_url"))
    pr_number = _first(item.get("pr_number"))
    expected_head = _first(
        item.get("head_sha"),
        item.get("head_oid"),
        item.get("headRefOid"),
        item.get("pr_head_sha"),
    )
    return {
        "kind": "task",
        "task_id": _first(item.get("task_id"), item.get("id")),
        "repo": repo,
        "branch": branch,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "expected_head": expected_head,
    }


def extract_targets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        kind = str(item.get("kind") or "").strip().lower()
        if kind == "pr":
            pr_url = _first(item.get("pr_url"), item.get("ref"))
            if not pr_url:
                continue
            target = {"kind": "pr", "pr_url": pr_url}
            key = ("pr", pr_url, "")
        elif kind == "task":
            target = _task_target(item)
            if not any((target["repo"], target["branch"], target["pr_url"])):
                continue
            key = (
                "task",
                target["task_id"],
                target["pr_url"] or f"{target['repo']}:{target['branch']}",
            )
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)
    return targets


def gh_probe(kind: str, target: dict[str, Any]) -> dict[str, Any]:
    if kind == "pr":
        pr_url = target["pr_url"]
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state,headRefOid,url"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return {"ok": False, "exists": False, "error": (result.stderr or result.stdout).strip()}
        payload = json.loads(result.stdout or "{}")
        return {
            "ok": True,
            "exists": True,
            "state": payload.get("state"),
            "head_oid": payload.get("headRefOid"),
            "url": payload.get("url") or pr_url,
        }
    if kind == "branch":
        repo = target["repo"]
        branch = target["branch"]
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/branches/{branch}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            lowered = detail.lower()
            if "not found" in lowered or "404" in lowered:
                return {"ok": True, "exists": False}
            return {"ok": False, "exists": False, "error": detail}
        payload = json.loads(result.stdout or "{}")
        commit = payload.get("commit") if isinstance(payload, dict) else {}
        return {
            "ok": True,
            "exists": True,
            "head_oid": commit.get("sha") if isinstance(commit, dict) else None,
        }
    raise ValueError(f"unsupported probe kind: {kind}")


def sweep(
    *,
    items: list[dict[str, Any]],
    probe: ProbeFn = gh_probe,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now_ts is None else now_ts
    findings: list[dict[str, Any]] = []
    for target in extract_targets(items):
        pr_url = target.get("pr_url") or ""
        if pr_url:
            pr_state = probe("pr", target)
            if pr_state.get("ok") and pr_state.get("exists") and pr_state.get("state") == "CLOSED":
                findings.append(
                    {
                        "kind": "tracked_pr_closed_unattributed",
                        "severity": "error",
                        "task_id": target.get("task_id"),
                        "pr_url": pr_url,
                        "observed_state": pr_state.get("state"),
                    }
                )
            elif pr_state.get("ok") and pr_state.get("exists") is False:
                findings.append(
                    {
                        "kind": "tracked_pr_missing_unattributed",
                        "severity": "error",
                        "task_id": target.get("task_id"),
                        "pr_url": pr_url,
                    }
                )
            expected_head = target.get("expected_head") or ""
            observed_head = _first(pr_state.get("head_oid"))
            if expected_head and observed_head and observed_head != expected_head:
                findings.append(
                    {
                        "kind": "tracked_head_drift_unattributed",
                        "severity": "error",
                        "task_id": target.get("task_id"),
                        "pr_url": pr_url,
                        "expected_head": expected_head,
                        "observed_head": observed_head,
                    }
                )
        repo = target.get("repo") or ""
        branch = target.get("branch") or ""
        if repo and branch:
            branch_state = probe("branch", target)
            if branch_state.get("ok") and branch_state.get("exists") is False:
                findings.append(
                    {
                        "kind": "tracked_branch_missing_unattributed",
                        "severity": "error",
                        "task_id": target.get("task_id"),
                        "repo": repo,
                        "branch": branch,
                    }
                )
            expected_head = target.get("expected_head") or ""
            observed_head = _first(branch_state.get("head_oid"))
            if expected_head and observed_head and observed_head != expected_head:
                findings.append(
                    {
                        "kind": "tracked_head_drift_unattributed",
                        "severity": "error",
                        "task_id": target.get("task_id"),
                        "repo": repo,
                        "branch": branch,
                        "expected_head": expected_head,
                        "observed_head": observed_head,
                    }
                )
    findings.sort(
        key=lambda item: (
            str(item.get("kind") or ""),
            str(item.get("task_id") or ""),
            str(item.get("pr_url") or ""),
            str(item.get("branch") or ""),
        )
    )
    digest = hashlib.sha256(
        json.dumps(findings, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        "generated_ts": now_ts,
        "finding_count": len(findings),
        "blocked": bool(findings),
        "findings_digest": digest,
        "findings": findings,
    }
