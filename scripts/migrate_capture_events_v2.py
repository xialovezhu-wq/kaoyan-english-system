#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from english_pipeline.migrations import write_migration_receipt
from english_pipeline.util import file_sha256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("event", type=Path, nargs="+")
    args = parser.parse_args(argv)
    root = args.state_dir.resolve() / "migrations" / "event-v2"
    receipts = []
    for event_path in sorted(path.resolve() for path in args.event):
        target, receipt = write_migration_receipt(event_path, root)
        receipts.append(
            {
                "source_event_id": receipt["source_event_id"],
                "migration_id": receipt["migration_id"],
                "path": str(target),
                "sha256": file_sha256(target),
                "effective_event_sha256": receipt["effective_event_sha256"],
            }
        )
    print(
        json.dumps(
            {
                "schema_version": "english_capture_migration_batch_receipt_v1",
                "status": "PASS",
                "receipt_count": len(receipts),
                "receipts": receipts,
                "model_call_count": 0,
                "formal_write_count": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
