#!/usr/bin/env python3
"""Write deployment metadata for the runtime /deployment-meta.json endpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--git-hash", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--output",
        default="public/deployment-meta.json",
        help="Path to the deployment metadata JSON file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"git_hash": args.git_hash, "date": args.date}
    output.write_text(json.dumps(payload, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
