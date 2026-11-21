"""Lightweight entry for the existing read-only personal-summary command.

The full CLI shares these arguments and execution path. Reading a teaching
summary does not need to import capture, archive or formal-writer workflows.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .constants import DEFAULT_STATE_DIR, REPO_ROOT
from .errors import PipelineError


def add_summary_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--key", action="append", required=True)
    parser.add_argument("--question-review", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=8192)
    parser.add_argument("--view", choices=("full", "teaching"), default="full")


def run_summary_query(args: argparse.Namespace) -> dict[str, Any]:
    from .personal_summary import query_personal_summary

    # Preserve both path normalization steps of the full CLI. No state or
    # directory is created, including when the requested index is absent.
    args.repo_root.resolve()
    result = query_personal_summary(args.state_dir.resolve(), keys=args.key,
                                   question_review=args.question_review,
                                   max_bytes=args.max_bytes, view=args.view)
    # The lightweight summary has no current sentence/question identity. Never
    # infer it from a historical observation or expand this read into the bank.
    try:
        from .runtime_paths import bridge_root
        bridge = str(bridge_root())
        if bridge not in sys.path:
            sys.path.insert(0, bridge)
        from study_visual_dispatch import attach_protocol
        return attach_protocol(result, "english", current_id=None,
                               source_version=result.get("summary_sha256") or result.get("source_version"),
                               max_bytes=args.max_bytes)
    except (ImportError, OSError, ValueError, TypeError, RuntimeError):
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="english_learning_pipeline.py")
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("query-personal-summary",
        help="Hash diagnosed point keys and read only the matching precomputed summaries.")
    summary.add_argument("--repo-root", type=Path, default=REPO_ROOT,
                        help=f"English repository root (default: {REPO_ROOT})")
    summary.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help=f"Canonical pipeline state root (default: {DEFAULT_STATE_DIR})")
    add_summary_arguments(summary)
    args = parser.parse_args(argv)
    try:
        result = run_summary_query(args)
    except (PipelineError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": "english_pipeline_error_v1", "status": "ERROR",
                          "error_type": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True), end="")
    return 0
