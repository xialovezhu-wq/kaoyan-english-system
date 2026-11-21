"""Native memory commands. Teaching queries never build or repair an index."""
from __future__ import annotations

import argparse
import json
import re
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
    query.add_argument("--known-publication", help="Pin an exact ready English publication for a history/audit replay with --as-of.")
    query.add_argument("--max-bytes", type=int)
    query.add_argument("--include-evidence", action="store_true",
                       help="Resolve the selected exact-unit episode's original messages within the response budget.")
    query.add_argument("--question-id")
    query.add_argument("--unlocked-question-id", action="append", default=[])
    query.add_argument("--backend", choices=("auto", "native", "basic-memory", "qmd"), default="auto")
    query.add_argument("--prepare-diagram", type=Path, metavar="FILE.context.json")
    query.add_argument("--focus-key", help="Place the current diagram's main observation before supporting keys.")
    query.add_argument("--diagram-text", help="Current sentence text when an indexed reading unit is unavailable.")
    query.add_argument("--diagram-vocab", action="append", default=[], metavar="ITEM|SOURCE_FORM[|MODE]")
    query.add_argument("--diagram-brief", action="store_true",
                       help="Keep the full verified context on disk and return a compact sense list to the teaching turn.")

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


def _selected_backend(state: Path) -> str:
    path = state / "memory" / "backend-selection.json"
    if not path.is_file():
        return "native"
    row = load_json(path)
    backend = row.get("backend", "native")
    if backend not in {"native", "basic-memory", "qmd"}:
        raise ValidationError("invalid memory backend selection")
    return backend


def _budget(result: dict[str, Any], maximum: int) -> dict[str, Any]:
    if len(canonical_bytes(result)) <= maximum:
        return result
    # Diagnostics are optional; evidence is never cut inside a complete record.
    result = dict(result)
    result.pop("backend", None)
    if len(canonical_bytes(result)) <= maximum:
        return result
    ambiguous = result.get("coverage", {}).get("observation_order_ambiguous_count")
    return {"schema_version": "english_memory_context_v1", "status": "partial",
            "matched": [], "coverage": {"complete": False, "reason": "response_budget",
                                        **({"observation_order_ambiguous_count": ambiguous} if ambiguous else {})},
            "formal_write_count": 0}


def _external_mirror(state: Path, current: dict[str, Any]) -> Path | None:
    """Only a lifecycle-filtered, current mirror may reach a local embedder."""
    from .memory_store import lifecycle_fingerprint, read_lifecycle
    pointer_path = state / "memory/backend-mirror.json"
    if not pointer_path.is_file():
        return None
    pointer = load_json(pointer_path)
    summary = state / "personal-summary/current.json"
    claims = state / "memory/claims.json"
    if (pointer.get("generation") != current.get("generation")
            or not summary.is_file()
            or pointer.get("summary_index_sha256") != file_sha256(summary)
            or pointer.get("lifecycle_sha256") != lifecycle_fingerprint(read_lifecycle(state))
            or pointer.get("claims_sha256") != (file_sha256(claims) if claims.is_file() else None)):
        return None
    path = Path(str(pointer.get("mirror_path", ""))).resolve()
    try:
        path.relative_to((state / "memory/exports").resolve())
    except ValueError:
        return None
    return path if path.is_dir() else None


def _brief_diagram_result(result: dict[str, Any]) -> dict[str, Any]:
    """Reduce dictionary transport only. The native memory receipt and render context stay intact."""
    references = []
    for target in result["vocabulary"]:
        ref = target["reference"]
        if ref.get("status") != "ready":
            references.append(target)
            continue
        compact = {name: ref[name] for name in (
            "status", "item", "translations", "english_definitions", "dictionary_status",
            "dictionary_reference_form", "dictionary_source", "ambiguous_forms",
            "count_mode", "counts", "source_form_counts", "coverage", "counting_note",
            "corpus_version", "match_type", "scope",
        ) if name in ref}
        senses = []
        for sense in ref.get("senses", []):
            examples = sense.get("examples") or []
            # Prefer a clause-sized candidate. Semantic suitability is checked by the teacher.
            candidate = next((ex for ex in examples if isinstance(ex, str) and len(ex.split()) >= 4), None)
            if candidate is None and examples:
                candidate = examples[0]
            senses.append([sense.get("id"), sense.get("pos"), sense.get("definition"), candidate])
        compact["sense_rows"] = senses
        references.append({"target": target["target"], "reference": compact})
    return {**result, "vocabulary": references, "sense_fields": ["id", "pos", "definition", "example_candidate"],
            "dictionary_view": "all_senses_one_candidate_each", "complete_dictionary_in_context": True}


def run_memory_command(args: argparse.Namespace) -> dict[str, Any]:
    repo, state = args.repo_root.resolve(), args.state_dir.resolve()
    if args.command == "query-memory-context":
        from .memory_query import query_memory_context
        maximum = args.max_bytes if args.max_bytes is not None else (6144 if args.view == "teaching" else 16384)
        if not 512 <= maximum <= 16384:
            raise ValidationError("memory query budget must be between 512 and 16384 bytes")
        diagram_path = getattr(args, "prepare_diagram", None)
        diagram_terms = getattr(args, "diagram_vocab", [])
        focus = getattr(args, "focus_key", None)
        diagram_text = getattr(args, "diagram_text", None)
        if diagram_path is not None:
            if (args.view != "teaching" or args.as_of or args.question_id or args.unlocked_question_id
                    or getattr(args, "known_publication", None) or args.backend not in {"auto", "native"}):
                raise ValidationError("prepared diagrams use current answer-free native teaching")
            from .diagram_context import prepare_diagram_context

            vocabulary = []
            for term in diagram_terms:
                parts = [part.strip() for part in term.split("|")]
                if len(parts) not in {2, 3} or not all(parts):
                    raise ValidationError("diagram vocabulary requires ITEM|SOURCE_FORM[|MODE]")
                vocabulary.append(dict(zip(("item", "source_form", "frequency_mode"), parts)))
            result = prepare_diagram_context(repo, state, diagram_path, source_id=args.source_id, unit_id=args.unit_id,
                text=diagram_text, keys=args.key, ids=args.id, query=args.query, focus_key=focus,
                vocabulary=vocabulary, max_bytes=maximum, include_evidence=getattr(args, "include_evidence", False))
            return _brief_diagram_result(result) if getattr(args, "diagram_brief", False) else result
        if diagram_terms or focus or diagram_text is not None or getattr(args, "diagram_brief", False):
            raise ValidationError("diagram-specific input requires --prepare-diagram")
        backend = _selected_backend(state) if args.backend == "auto" else args.backend
        candidates = None
        diagnostic: dict[str, Any] = {"name": "native", "api_requests": 0}
        # An explicit identity never broadens to a natural-language search.
        known_publication = getattr(args, "known_publication", None)
        if args.query and not (args.key or args.id) and not known_publication and backend != "native":
            from .memory_backends import search_candidates
            current_path = state / "memory" / "current.json"
            current = load_json(current_path) if current_path.is_file() else {}
            generation = current.get("generation", "")
            if generation and not re.fullmatch(r"[a-f0-9]{64}", str(generation)):
                raise ValidationError("invalid memory projection generation")
            mirror = _external_mirror(state, current)
            try:
                filters = {name: value for name, value in
                           (("source_id", args.source_id), ("unit_id", args.unit_id), ("as_of", args.as_of)) if value}
                candidate_result = (search_candidates(backend, args.query, mirror, timeout=2.0, limit=8, filters=filters)
                    if mirror is not None else {"status": "degraded", "degraded": "current_safe_mirror_unavailable", "candidate_keys": []})
                candidates = candidate_result.get("candidate_keys", [])
                diagnostic = {"name": backend, "status": candidate_result.get("status"),
                              "degraded": candidate_result.get("degraded"),
                              "elapsed_ms": candidate_result.get("elapsed_ms"), "api_requests": 0}
            except (OSError, ValueError, RuntimeError, PipelineError) as exc:
                diagnostic = {"name": backend, "status": "degraded", "degraded": type(exc).__name__, "api_requests": 0}
        result = query_memory_context(repo, state, keys=args.key, ids=args.id, query=args.query,
            source_id=args.source_id, unit_id=args.unit_id, view=args.view, as_of=args.as_of,
            max_bytes=max(512, maximum - 384), question_id=args.question_id,
            unlocked_question_ids=args.unlocked_question_id, candidate_keys=candidates,
            known_publication=known_publication, include_evidence=getattr(args, "include_evidence", False))
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
                "backend": _selected_backend(state), "formal_write_count": 0}

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
