"""Native commands for current answer-free English teaching views."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .constants import DEFAULT_STATE_DIR, REPO_ROOT
from .errors import ValidationError

COMMANDS = {"query-sentence-visual", "query-concordance", "rebuild-sentence-teaching"}


def add_commands(subparsers, *, common):
    visual = subparsers.add_parser("query-sentence-visual", parents=[common],
                                  help="Read one verified offline visual or the exact source fallback.")
    visual.add_argument("--source-id", required=True)
    visual.add_argument("--sentence-id", required=True)
    visual.add_argument("--source-hash")
    visual.add_argument("--sentence-sha256")
    visual.add_argument("--include-evidence", action="store_true")
    visual.add_argument("--display-only", action="store_true",
                        help="After a successful export, return just the verified display receipt.")
    visual.add_argument("--max-evidence-bytes", type=int, default=4096)

    comparison = subparsers.add_parser("query-concordance", parents=[common],
                                       help="Compare up to four authorized source sentences by exact word position.")
    comparison.add_argument("--phrase", required=True)
    comparison.add_argument("--source-id")
    comparison.add_argument("--sentence-id")
    comparison.add_argument("--limit", type=int, default=2)

    for parser in (visual, comparison):
        parser.add_argument("--output", type=Path)
        parser.add_argument("--inline-root", type=Path)
        parser.add_argument("--lesson-page", action="store_true",
                            help="Prepare the sandboxed browser preview of the same authorized inline lesson.")

    rebuild = subparsers.add_parser("rebuild-sentence-teaching", parents=[common],
                                    help="Rebuild verified sentence views without replaying formal intake.")
    rebuild.add_argument("--writer-receipt", type=Path, help="Resume only the derived stage for this committed receipt.")


def run(args):
    from . import concordance, sentence_visuals
    repo, state = args.repo_root.resolve(), args.state_dir.resolve()
    if args.command == "rebuild-sentence-teaching":
        if args.writer_receipt:
            path = args.writer_receipt.resolve(strict=True)
            row = sentence_visuals.load_json(path)
            day = sentence_visuals.writer_receipt_day(row)
            expected = state / "receipts" / "nightly" / day / (row["receipt_id"] + ".json")
            if path != expected or not path.is_relative_to(state / "receipts" / "nightly"):
                raise ValidationError("derived recovery requires the canonical writer receipt path")
            return sentence_visuals.refresh_after_support(repo, state, row, force_rebuild=True)
        return sentence_visuals.rebuild(repo, state)
    if getattr(args, "lesson_page", False) and (not args.output or not args.inline_root):
        raise ValidationError("--lesson-page requires --output and --inline-root")
    if bool(args.source_id) != bool(args.sentence_id):
        raise ValidationError("the current sentence requires both --source-id and --sentence-id")
    if args.source_id and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", args.source_id)
                           or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", args.sentence_id)):
        raise ValidationError("invalid exact English source or sentence identity")
    if args.source_id and args.source_id.startswith("EXAM-READING-"):
        from .reading_preparation import DEFAULT_OUTPUT, query as read_unit
        original = read_unit(repo / DEFAULT_OUTPUT, repo_root=repo, source_id=args.source_id,
                             unit_id=args.sentence_id)
        args.source_id = original["source_id"]
    if args.command == "query-concordance":
        current = (args.source_id, args.sentence_id) if args.source_id else None
        result = concordance.query(repo, state, args.phrase, current=current, limit=args.limit)
    else:
        if not 512 <= args.max_evidence_bytes <= 16384:
            raise ValidationError("evidence budget must be between 512 and 16384 bytes")
        if getattr(args, "display_only", False) and (args.output is None or args.include_evidence):
            raise ValidationError("display-only requires an output and cannot omit requested evidence")
        result = sentence_visuals.query_sentence(repo, state, args.source_id, args.sentence_id,
            source_hash=args.source_hash, sentence_hash=args.sentence_sha256,
            include_evidence=args.include_evidence, max_bytes=args.max_evidence_bytes)
    if args.output:
        result = sentence_visuals.export_result(state, result, args.output, args.inline_root,
                                                lesson_page=getattr(args, "lesson_page", False))
        if getattr(args, "lesson_page", False) and args.inline_root and not result.get("output_path"):
            from .lesson_display import export_reviewed_text
            result = export_reviewed_text(result, args.output, args.inline_root)
        if getattr(args, "display_only", False) and result.get("status") == "ready":
            result = {key: result[key] for key in (
                "schema_version", "source_id", "sentence_id", "status", "freshness", "reason",
                "html_sha256", "output_path", "visualize_reference", "open_in_codex",
                "panel_status", "panel_reason", "panel_url", "panel_revision", "revision_sha256", "formal_write_count",
                "learning_event_write_count", "model_requests",
            ) if key in result}
    return result


def main(argv=None):
    from .sentence_visuals import ERRORS
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    common.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser = argparse.ArgumentParser(prog="english_learning_pipeline.py")
    add_commands(parser.add_subparsers(dest="command", required=True), common=common)
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except ERRORS as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc), "formal_write_count": 0}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if args.command == "rebuild-sentence-teaching" and result["status"] in {"pending", "waiting_for_support"} else 0
