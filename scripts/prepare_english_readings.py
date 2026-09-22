#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from english_pipeline.reading_preparation import DEFAULT_OUTPUT, build, query, verify


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and query answer-safe English reading preparation.")
    parser.add_argument("--repo-root", type=Path, default=REPO)
    parser.add_argument("--output", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build")
    commands.add_parser("verify")
    lookup = commands.add_parser("query")
    lookup.add_argument("--source-id", required=True)
    lookup.add_argument("--unit-id")
    args = parser.parse_args()
    repo = args.repo_root.resolve(strict=True)
    output = (args.output or (repo / DEFAULT_OUTPUT)).resolve()
    if args.command == "build":
        result = build(repo, output)
    elif args.command == "verify":
        result = verify(repo, output)
    else:
        result = query(output, source_id=args.source_id, unit_id=args.unit_id, repo_root=repo)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
