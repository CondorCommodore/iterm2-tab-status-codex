"""Persist or inspect versioned COS directions through coord-api."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from c2_coord_client import CoordClient, CoordConfig, CoordError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="COS durable direction adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    post = sub.add_parser("post")
    post.add_argument("--file", type=Path, required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--plan-id", required=True)
    listing.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        client = CoordClient(CoordConfig.load())
        if args.command == "post":
            direction = json.loads(args.file.read_text(encoding="utf-8"))
            result = client.post_direction(direction)
        else:
            result = client.directions(args.plan_id, limit=args.limit)
    except (CoordError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
