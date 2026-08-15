#!/usr/bin/env python3
"""cos_tabs — stable tab addressing for the COS conductor.

The failure this fixes (2026-08-15): the conductor addressed tabs by
window/tab POSITION, which drifts on every tab move, window split, or
restart, while the operator counts what they see. Numbers were wrong
several times in one day.

The model: a tab NUMBER is an IDENTITY LABEL, not a position.
  baseline   walks the visible layout once, stamps user.cosTab=N on each
             session, sets the visible tab title to "N·<hint>" (so the
             operator and the conductor share one map — dragging a tab
             takes its number with it), and writes a registry keyed by
             iTerm's stable session_id.
  list       registry vs live: resolves every entry by session_id, shows
             current position, job, and a status line; flags dead entries.
  peek N     last screen lines of tab N.
  send N     types text into tab N and submits it robustly (extra bare CR
             after a delay — codex bracketed-paste stages large pastes and
             needs the second CR; harmless for claude/shell).

Resolution order for N: live user.cosTab match -> registry session_id ->
error advising re-baseline. Both survive tab reordering; only a closed
window kills a number (shown as DEAD in list).

Runs under the iTerm2 bundled venv python:
  "$(ls ~/Library/Application\\ Support/iTerm2/iterm2env/versions/*/bin/python3 | head -1)" cos_tabs.py <cmd>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

import iterm2  # type: ignore

REGISTRY = pathlib.Path.home() / ".claude" / "cos-tab-registry.json"


def load_registry() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text())
        except Exception:
            return {}
    return {}


def save_registry(reg: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2, sort_keys=True))
    os.replace(tmp, REGISTRY)


async def iter_sessions(app):
    """Yield (window_idx, tab_idx, tab, session) in visible order."""
    for w_i, window in enumerate(app.windows):
        for t_i, tab in enumerate(window.tabs):
            session = tab.current_session or tab.sessions[0]
            yield w_i, t_i, tab, session


async def describe(session) -> dict:
    tty = await session.async_get_variable("tty") or ""
    job = await session.async_get_variable("jobName") or ""
    cwd = await session.async_get_variable("path") or ""
    return {"tty": tty, "job": job, "cwd": cwd}


async def status_line(session) -> str:
    try:
        c = await session.async_get_screen_contents()
    except Exception:
        return "(unreadable)"
    lines = [c.line(i).string.rstrip() for i in range(c.number_of_lines)]
    lines = [l for l in lines if l]
    for l in reversed(lines):
        if any(k in l for k in ("Context", "context", "ctx:", "weekly", "Working", "esc to")):
            return l.strip()[:140]
    return (lines[-1][:140] if lines else "(blank)")


async def resolve(app, n: str):
    """Resolve tab number -> session. Live variable first, then registry."""
    async for _, _, _tab, session in aiter_wrap(iter_sessions(app)):
        val = await session.async_get_variable("user.cosTab")
        if val is not None and str(val) == str(n):
            return session
    reg = load_registry()
    ent = reg.get(str(n))
    if ent:
        found = app.get_session_by_id(ent["session_id"])
        if found:
            return found
    return None


async def aiter_wrap(agen):
    async for item in agen:
        yield item


async def cmd_baseline(conn, args) -> None:
    app = await iterm2.async_get_app(conn)
    reg: dict = {}
    n = 0
    async for w_i, t_i, tab, session in aiter_wrap(iter_sessions(app)):
        n += 1
        d = await describe(session)
        hint = args.hints.get(str(n), "") if args.hints else ""
        if not hint:
            job = d["job"].lower()
            hint = "codex" if "codex" in job else ("claude" if "python" in job else (d["job"] or "sh"))
        await session.async_set_variable("user.cosTab", str(n))
        try:
            await tab.async_set_title(f"{n}·{hint}")
        except Exception:
            pass
        reg[str(n)] = {
            "session_id": str(session.session_id),
            "tty": d["tty"],
            "hint": hint,
            "window": w_i,
            "tab_index_at_baseline": t_i,
        }
        print(f"tab {n}: tty={d['tty']} job={d['job']} -> titled '{n}·{hint}'")
    save_registry(reg)
    print(f"registry written: {REGISTRY} ({n} tabs)")


async def cmd_list(conn, _args) -> None:
    app = await iterm2.async_get_app(conn)
    reg = load_registry()
    live_by_sid: dict[str, tuple] = {}
    async for w_i, t_i, tab, session in aiter_wrap(iter_sessions(app)):
        live_by_sid[str(session.session_id)] = (w_i, t_i, session)
    for n in sorted(reg, key=lambda x: int(x)):
        ent = reg[n]
        hit = live_by_sid.get(ent["session_id"])
        if not hit:
            print(f"tab {n} [{ent.get('hint','')}] DEAD (session gone) last-tty={ent.get('tty')}")
            continue
        w_i, t_i, session = hit
        d = await describe(session)
        s = await status_line(session)
        moved = "" if t_i == ent.get("tab_index_at_baseline") and w_i == ent.get("window") else f" (now w{w_i}.pos{t_i})"
        print(f"tab {n} [{ent.get('hint','')}]{moved} tty={d['tty']} job={d['job']}")
        print(f"    {s}")


async def cmd_peek(conn, args) -> None:
    app = await iterm2.async_get_app(conn)
    session = await resolve(app, args.n)
    if session is None:
        sys.exit(f"tab {args.n}: not found (re-run baseline?)")
    c = await session.async_get_screen_contents()
    lines = [c.line(i).string.rstrip() for i in range(c.number_of_lines)]
    lines = [l for l in lines if l]
    print("\n".join(lines[-args.lines:]))


async def cmd_send(conn, args) -> None:
    app = await iterm2.async_get_app(conn)
    session = await resolve(app, args.n)
    if session is None:
        sys.exit(f"tab {args.n}: not found (re-run baseline?)")
    text = args.text
    if args.file:
        text = pathlib.Path(args.file).read_text()
    if not text:
        sys.exit("nothing to send (--text or --file)")
    await session.async_send_text(text)
    if not args.no_submit:
        await asyncio.sleep(args.settle)
        await session.async_send_text("\r")
        # codex bracketed-paste stages large pastes; a second bare CR after a
        # beat submits the staged buffer. Harmless elsewhere (empty prompt).
        await asyncio.sleep(args.settle)
        await session.async_send_text("\r")
    d = await describe(session)
    print(f"sent {len(text)} chars to tab {args.n} (tty={d['tty']}, submit={not args.no_submit})")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("baseline", help="stamp numbers onto visible tabs + write registry")
    b.add_argument("--hint", action="append", default=[],
                   help="N=label override, e.g. --hint 3=t2mount (repeatable)")

    sub.add_parser("list", help="registry vs live state")

    pk = sub.add_parser("peek", help="tail of tab N's screen")
    pk.add_argument("n")
    pk.add_argument("--lines", type=int, default=12)

    sd = sub.add_parser("send", help="type + submit into tab N")
    sd.add_argument("n")
    sd.add_argument("--text", default="")
    sd.add_argument("--file", default="")
    sd.add_argument("--no-submit", action="store_true")
    sd.add_argument("--settle", type=float, default=1.5)

    args = p.parse_args()
    if args.cmd == "baseline":
        args.hints = dict(h.split("=", 1) for h in args.hint) if args.hint else {}

    async def runner(conn):
        if args.cmd == "baseline":
            await cmd_baseline(conn, args)
        elif args.cmd == "list":
            await cmd_list(conn, args)
        elif args.cmd == "peek":
            await cmd_peek(conn, args)
        elif args.cmd == "send":
            await cmd_send(conn, args)

    iterm2.run_until_complete(runner)


if __name__ == "__main__":
    main()
