#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from english_pipeline.vocab_handoff import create_vocab_handoff


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a formal-ready English vocabulary handoff.")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--repo-root", type=Path, default=REPO)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    repo = args.repo_root.resolve(strict=True)
    result = create_vocab_handoff(repo, args.state_dir or repo / "intake",
                                  source_id=args.source_id, output_dir=args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
