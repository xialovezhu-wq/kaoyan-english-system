#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from english_pipeline.constants import MASTERED_HEADER, MASTER_HEADER
from english_pipeline.events import effective_sentence_events, load_events
from english_pipeline.formal import read_csv
from english_pipeline.review_status import (
    build_review_status_proposals,
    effective_review_status,
    load_review_status_ledger,
)
from english_pipeline.util import file_sha256, object_sha256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--study-date", required=True)
    parser.add_argument("--event-id", action="append", default=[])
    parser.add_argument("--aliases-json", type=Path)
    args = parser.parse_args(argv)

    repo = args.repo_root.resolve()
    state = args.state_dir.resolve()
    events = effective_sentence_events(load_events(state), study_date=args.study_date)
    requested = set(args.event_id)
    if requested:
        events = [event for event in events if event.get("event_id") in requested]
        if {event.get("event_id") for event in events} != requested:
            raise SystemExit("requested event set is not fully available")
    bank_path = repo / "bank" / "master_bank.csv"
    mastered_path = repo / "bank" / "mastered_items.csv"
    ledger_path = repo / "bank" / "review_exclusion_ledger.jsonl"
    bank_rows = read_csv(bank_path, MASTER_HEADER)
    mastered_rows = read_csv(mastered_path, MASTERED_HEADER)
    ledger_records = load_review_status_ledger(ledger_path)
    aliases = {}
    if args.aliases_json:
        aliases = json.loads(args.aliases_json.read_text(encoding="utf-8"))
        if not isinstance(aliases, dict):
            raise SystemExit("aliases JSON must be an object")
    result = build_review_status_proposals(
        events,
        bank_rows,
        study_date=args.study_date,
        current_status=effective_review_status(ledger_records),
        mastered_items=mastered_rows,
        aliases=aliases,
    )
    result["source_hashes"] = {
        "master_bank": file_sha256(bank_path),
        "mastered_items": file_sha256(mastered_path),
        "review_exclusion_ledger": (
            file_sha256(ledger_path) if ledger_path.exists() else object_sha256([])
        ),
    }
    result["formal_write_count"] = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
