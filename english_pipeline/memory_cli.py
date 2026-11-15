"""Native memory commands. Teaching queries never build or repair an index."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .constants import DEFAULT_STATE_DIR, REPO_ROOT
from .errors import PipelineError, ValidationError
from .util import atomic_write_json, canonical_bytes, file_sha256, load_json, utc_now


COMMANDS = {"query-memory-context", "build-memory-index", "memory-status", "recover-memory-closeout"}


def add_memory_commands(subparsers: Any, *, parents: list[argparse.ArgumentParser]) -> None:
    query = subparsers.add_parser("query-memory-context", parents=parents,
        help="Read scoped, source-backed English memory without changing learning facts.")
    query.add_argument("--key", action="append", default=[])
    query.add_argument("--id", action="append", default=[])
    query.add_argument("--query")
    query.add_argument("--source-id")
    query.add_argument("--unit-id")
    query.add_argument("--view", choices=("teaching", "history", "audit"), default="teaching")
    query.add_argument("--as-of", help="Occurred-date cutoff, interpreted using currently accepted evidence.")
    query.add_argument("--max-bytes", type=int)
    query.add_argument("--question-id")
    query.add_argument("--unlocked-question-id", action="append", default=[])
    query.add_argument("--backend", choices=("auto", "native"), default="auto")

    build = subparsers.add_parser("build-memory-index", parents=parents,
        help="Audit and incrementally rebuild derived memory; never writes formal learning facts.")
    target = build.add_mutually_exclusive_group()
    target.add_argument("--key", action="append")
    target.add_argument("--source-path", type=Path, action="append",
                        help="Refresh only the points depending on a known changed source.")
    build.add_argument("--audit-only", action="store_true")
    build.add_argument("--publish-pages", action="store_true")
    build.add_argument("--enable", action="store_true",
        help="Enable post-formal memory closeout after the initial projection succeeds.")

    subparsers.add_parser("memory-status", parents=parents,
        help="Read memory generations, pending closeouts, and selected backend.")
    recover = subparsers.add_parser("recover-memory-closeout", parents=parents,
        help="Recover only pending derived memory; never reruns the formal writer.")
    recover.add_argument("--writer-receipt", type=Path)


def _budget(result: dict[str, Any], maximum: int) -> dict[str, Any]:
    if len(canonical_bytes(result)) <= maximum:
        return result
    # Diagnostics are optional; evidence is never cut inside a complete record.
    result = dict(result)
    result.pop("backend", None)
    if len(canonical_bytes(result)) <= maximum:
        return result
    return {"schema_version": "english_memory_context_v1", "status": "partial",
            "matched": [], "coverage": {"complete": False, "reason": "response_budget"},
            "formal_write_count": 0}


def run_memory_command(args: argparse.Namespace) -> dict[str, Any]:
    repo, state = args.repo_root.resolve(), args.state_dir.resolve()
    if args.command == "query-memory-context":
        from .memory_query import query_memory_context
        maximum = args.max_bytes if args.max_bytes is not None else (6144 if args.view == "teaching" else 16384)
        if not 512 <= maximum <= 16384:
            raise ValidationError("memory query budget must be between 512 and 16384 bytes")
        diagnostic = {"name": "native", "api_requests": 0}
        result = query_memory_context(repo, state, keys=args.key, ids=args.id, query=args.query,
            source_id=args.source_id, unit_id=args.unit_id, view=args.view, as_of=args.as_of,
            max_bytes=max(512, maximum - 384), question_id=args.question_id,
            unlocked_question_ids=args.unlocked_question_id)
        result["backend"] = diagnostic
        return _budget(result, maximum)

    if args.command == "build-memory-index":
        from .memory_store import build_memory_projection, export_safe_mirror, publish_managed_pages, source_dependents
        if args.audit_only and (args.enable or args.publish_pages):
            raise ValidationError("an audit-only query cannot enable memory or publish pages")
        sources = getattr(args, "source_path", None)
        keys = source_dependents(repo, state, sources) if sources else args.key
        result = build_memory_projection(repo, state, keys=keys, publish=not args.audit_only, audit_sources=True)
        if sources:
            result["affected_source_count"] = len(sources)
            result["source_dependent_key_count"] = len(keys)
        if not args.audit_only:
            result["backend_mirror"] = export_safe_mirror(repo, state)
        if args.publish_pages:
            result["managed_pages"] = publish_managed_pages(repo, state)
        if args.enable:
            current = state / "memory" / "current.json"
            if not current.is_file():
                raise ValidationError("memory activation requires a published projection")
            activation = state / "memory" / "enabled.json"
            if not activation.is_file():
                atomic_write_json(activation, {"schema_version": "english_memory_activation_v1",
                    "enabled": True, "activated_at": utc_now(),
                    "initial_generation_sha256": file_sha256(current), "formal_write_count": 0})
            result["activation"] = load_json(activation)
        return result

    if args.command == "memory-status":
        from .memory_lifecycle import memory_closeout_status
        current = state / "memory" / "current.json"
        return {"schema_version": "english_memory_status_v1", "closeout": memory_closeout_status(state),
                "current": load_json(current) if current.is_file() else None,
                "backend": "native", "formal_write_count": 0}

    if args.command == "recover-memory-closeout":
        from .memory_lifecycle import recover_memory_closeout
        receipt_id = None
        if args.writer_receipt:
            from .cli import build_parser, run
            parsed = build_parser().parse_args(["validate-formal-receipt", "--repo-root", str(repo),
                "--state-dir", str(state), "--writer-receipt", str(args.writer_receipt)])
            receipt_id = run(parsed)["receipt_id"]
        return recover_memory_closeout(repo, state, receipt_id=receipt_id)
    raise ValidationError("unknown memory command")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="english_learning_pipeline.py")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    common.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    add_memory_commands(parser.add_subparsers(dest="command", required=True), parents=[common])
    args = parser.parse_args(argv)
    try:
        result = run_memory_command(args)
    except (PipelineError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": "english_pipeline_error_v1", "status": "ERROR",
            "error_type": type(exc).__name__, "message": str(exc), "formal_write_count": 0}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
          end="" if args.command == "query-memory-context" else "\n")
    return 2 if result.get("status") in {"ERROR", "FAILED", "CAS_CONFLICT"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
