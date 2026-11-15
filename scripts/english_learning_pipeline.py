#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if len(sys.argv) > 1 and sys.argv[1] in {"query-memory-context", "build-memory-index", "memory-status", "recover-memory-closeout"}:
    from english_pipeline.memory_cli import main
elif len(sys.argv) > 1 and sys.argv[1] == "query-personal-summary":
    from english_pipeline.teaching_query_cli import main
else:
    from english_pipeline.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
