from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .archive import archive_committed_batch, reopen_archived_package
from .backlog import (
    audit_legacy_capture_events,
    build_backlog_plan,
    execute_backlog_plan,
    retire_legacy_capture_events,
    write_backlog_gate,
)
from .constants import DEFAULT_STATE_DIR, REPO_ROOT
from .errors import PipelineError, SourceHashMismatch, ValidationError
from .display_assets import publish_display_assets
from .nightly import freeze_nightly, pipeline_status
from .packages import (
    SegmentCapturePending,
    complete_article_packages,
    create_conversation_package,
    recover_segment_gate,
)
from .sentence_support import (
    query_sentence_support,
    refresh_sentence_support,
)
from .util import atomic_write_json, file_sha256, load_json, parse_iso_date
from .writer import apply_nightly, recover_nightly


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help=f"English repository root (default: {REPO_ROOT})",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Canonical pipeline state root (default: {DEFAULT_STATE_DIR})",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="english_learning_pipeline.py",
        description="Append-only English learning capture and deterministic nightly formal writer.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture",
        parents=[common],
        help="Seal one immutable English conversation package without analysis or background dispatch.",
    )
    capture.add_argument(
        "--input-json",
        type=Path,
        help="Complete conversation-package request JSON; direct flags are ignored.",
    )
    capture.add_argument(
        "--conversation-json",
        type=Path,
        help="JSON array or object with messages preserving every user/assistant turn in order.",
    )
    capture.add_argument(
        "--message-json",
        action="append",
        default=[],
        help="Inline JSON message object; repeat in conversation order.",
    )
    capture.add_argument(
        "--attachment-json",
        action="append",
        default=[],
        help="Inline JSON attachment object with path, role and optional message_sequence.",
    )
    capture.add_argument(
        "--expected-attachment-role",
        action="append",
        default=[],
        choices=[
            "question_image", "solution_image", "explanation_image",
            "user_work_image", "source_article_image", "other_attachment",
        ],
    )
    capture.add_argument("--idempotency-key")
    capture.add_argument(
        "--segment-key",
        help="Stable key for this exact segment; must equal --idempotency-key.",
    )
    capture.add_argument(
        "--thread-ref",
        help="Stable private thread reference used only through its SHA-256 frontier identity.",
    )
    capture.add_argument(
        "--previous-token-json",
        type=Path,
        help="Complete continuation token returned by the immediately preceding segment.",
    )
    capture.add_argument("--article-id", help="Compatibility alias; when supplied it must equal --source-id.")
    capture.add_argument("--source-article")
    capture.add_argument("--source-id")
    capture.add_argument("--reference-id", default="")
    capture.add_argument("--title", default="")
    capture.add_argument("--article-sha256", help="Required SHA-256 from the answer-free canonical article handoff; never inferred from article Markdown.")
    capture.add_argument("--sentence-id")
    capture.add_argument("--paragraph-id")
    capture.add_argument("--knowledge-point-id")
    capture.add_argument("--question-id")
    capture.add_argument("--source-sentence")
    capture.add_argument("--sentence-sha256", help="SHA-256 of the normalized exact source sentence.")
    capture.add_argument(
        "--source-kind",
        choices=["article", "question", "option", "explanation", "user_provided"],
    )
    capture.add_argument(
        "--answer-exposure",
        choices=["answer_free", "protected"],
        help="Explicit package-level answer exposure; never inferred from source kind.",
    )
    capture.add_argument("--occurred-at", help="ISO-8601 timestamp; defaults to current UTC time.")
    capture.add_argument("--study-date", help="Original learning day for a hash-bound A-return supplement; capture time is preserved separately.")
    capture.add_argument("--review-route", choices=["web", "local"], help="web holds this immutable segment for GPT-6 Pro review before formal intake.")
    capture.add_argument("--budget-ms", type=int, default=5000, help="Remaining local capture budget; 1 through 40000 milliseconds.")

    source = subparsers.add_parser("resolve-source", parents=[common], help="Read the existing answer-free article identity without changing its text.")
    source.add_argument("--source-article")
    source.add_argument("--all", action="store_true", help="Return the complete deterministic article source catalog.")

    subparsers.add_parser("export-publication-sources", parents=[common], help="Return complete hash-bound English sources without publishing or changing formal data.")
    validate_package = subparsers.add_parser("validate-package", parents=[common], help="Read and validate one complete immutable package without writing or importing it.")
    validate_package.add_argument("--package-root", type=Path, required=True)
    validate_formal = subparsers.add_parser("validate-formal-receipt", parents=[common], help="Read an existing canonical writer closeout and its recorded A decisions without applying or publishing.")
    validate_formal.add_argument("--writer-receipt", type=Path, required=True)

    learning = subparsers.add_parser("query-learning-context", parents=[common], help="Read current English formal state and bounded relevant history; never serves cached mastery.")
    learning.add_argument("--source-id")
    learning.add_argument("--sentence-id")
    learning.add_argument("--source-article")
    learning.add_argument("--source-hash")
    learning.add_argument("--sentence-sha256")
    learning.add_argument("--bank-id", action="append", default=[])
    learning.add_argument("--sp-id", action="append", default=[])
    learning.add_argument("--concept-id", action="append", default=[])
    learning.add_argument("--alias", action="append", default=[])
    learning.add_argument("--max-bytes", type=int, default=16 * 1024)
    learning.add_argument("--question-review", action="store_true", help="Only for the user's explicitly authorized answer/analysis review.")

    summary = subparsers.add_parser("query-personal-summary", parents=[common], help="Hash diagnosed point keys and read only the matching precomputed summaries.")
    summary.add_argument("--key", action="append", required=True)
    summary.add_argument("--question-review", action="store_true")
    summary.add_argument("--max-bytes", type=int, default=8192)

    publish_summary = subparsers.add_parser("publish-personal-summaries", parents=[common], help="Update point summaries only after formal intake; never from raw Capture.")
    publish_summary.add_argument("--input-json", type=Path, required=True)
    publication_basis = publish_summary.add_mutually_exclusive_group(required=True)
    publication_basis.add_argument("--writer-receipt", type=Path)
    publication_basis.add_argument("--bootstrap-existing-formal", action="store_true")

    dialogue = subparsers.add_parser("export-unit-dialogue", parents=[common], help="Export exact selected turns at unit close; never creates a Capture.")
    dialogue.add_argument("--rollout", type=Path, required=True)
    dialogue.add_argument("--turn-id", action="append", required=True)
    dialogue.add_argument("--output", type=Path, required=True)

    complete = subparsers.add_parser("complete-article", parents=[common], help="Freeze one article's immutable package snapshot for output-only review.")
    complete.add_argument("--source-id", help="Canonical source identity.")
    complete.add_argument("--article-id", help="Compatibility alias; must equal --source-id when both are supplied.")
    complete.add_argument("--idempotency-key", required=True)
    complete.add_argument("--date", dest="study_date")
    complete.add_argument("--output-dir", type=Path)

    freeze = subparsers.add_parser("freeze-nightly", parents=[common], help="Freeze unprocessed immutable local conversation packages for one date.")
    freeze.add_argument("--date", dest="study_date", required=True)
    freeze.add_argument("--output", type=Path)
    freeze.add_argument("--package-id", action="append", default=[])
    freeze.add_argument("--local-review-id")

    backlog_plan = subparsers.add_parser(
        "plan-backlog",
        parents=[common],
        help="Create a canonical pending-package plan through a Shanghai cutoff date.",
    )
    backlog_plan.add_argument("--cutoff-date")
    backlog_plan.add_argument("--only-today", action="store_true")
    backlog_plan.add_argument("--package-id", action="append", default=[])
    backlog_plan.add_argument("--local-review-id")
    backlog_plan.add_argument("--output", type=Path)
    backlog_plan.add_argument("--no-legacy-audit", action="store_true")
    backlog_plan.add_argument("--no-persist", action="store_true")

    legacy_audit = subparsers.add_parser(
        "audit-legacy-events",
        parents=[common],
        help="Read-only reachability audit for legacy capture events; never migrates them.",
    )
    legacy_audit.add_argument("--cutoff-date")

    legacy_retirement = subparsers.add_parser(
        "retire-legacy-events",
        parents=[common],
        help=(
            "Preview or atomically register explicit legacy capture events as "
            "user-removed future migration tasks."
        ),
    )
    legacy_retirement.add_argument(
        "--event-id",
        action="append",
        required=True,
        help="Exact legacy EVT id; repeat for every event in this immutable batch.",
    )
    legacy_retirement.add_argument(
        "--apply",
        action="store_true",
        help="Publish the content-addressed registry receipt; preview is the default.",
    )
    legacy_retirement.add_argument(
        "--authorization",
        help="Exact retirement_id returned by preview; required with --apply.",
    )

    backlog_run = subparsers.add_parser(
        "run-backlog",
        parents=[common],
        help="Replay one canonical backlog plan in study-date order using existing actions files.",
    )
    backlog_run.add_argument("--plan", type=Path, required=True)
    backlog_run.add_argument("--actions-dir", type=Path)
    backlog_run.add_argument(
        "--support-dir",
        type=Path,
        help="Directory containing BATCH_ID.support.json sentence-support proposals.",
    )
    backlog_run.add_argument("--no-archive", action="store_true")
    backlog_run.add_argument("--apply", action="store_true")
    backlog_run.add_argument("--authorization")

    backlog_gate = subparsers.add_parser(
        "finalize-backlog-gate",
        parents=[common],
        help="Write a canonical global gate for one backlog plan without applying actions.",
    )
    backlog_gate.add_argument("--plan", type=Path, required=True)

    apply_parser = subparsers.add_parser("apply-nightly", parents=[common], help="Validate and simulate/apply typed Sol actions through the deterministic writer.")
    apply_parser.add_argument("--manifest", type=Path, required=True)
    apply_parser.add_argument("--actions", type=Path, required=True)
    mode = apply_parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Explicit dry-run; this is also the default.")
    mode.add_argument("--apply", action="store_true", help="Apply safe actions; requires exact --authorization batch id.")
    apply_parser.add_argument("--authorization", help="Exact frozen batch id; required only with --apply.")
    apply_parser.add_argument("--authorized-at", help="ISO-8601 authorization time recorded in receipt.")
    apply_parser.add_argument(
        "--support-preflight",
        type=Path,
        help="Canonical sentence-support preflight receipt required by v3 apply.",
    )

    support_refresh = subparsers.add_parser(
        "refresh-sentence-support",
        parents=[common],
        help=(
            "Refresh the bounded sentence-support projection after a canonical writer "
            "closeout and before archive/display cleanup."
        ),
    )
    support_refresh.add_argument("--manifest", type=Path, required=True)
    support_refresh.add_argument("--writer-receipt", type=Path, required=True)
    support_refresh.add_argument("--proposal", type=Path, required=True)

    support_query = subparsers.add_parser(
        "query-sentence-support",
        parents=[common],
        help=(
            "Read one exact sentence or one bounded source set from the current "
            "answer-safe sentence-support index without scanning the vault."
        ),
    )
    support_query.add_argument("--source-id", required=True)
    support_query.add_argument("--sentence-id")
    support_query.add_argument("--source-hash")
    support_query.add_argument("--sentence-sha256")
    support_query.add_argument("--max-records", type=int, default=64)
    support_query.add_argument("--max-bytes", type=int, default=16 * 1024)

    recover = subparsers.add_parser("recover-nightly", parents=[common], help="Recover an interrupted prepared formal transaction under the writer lock.")
    recover.add_argument("--retry-publication", action="store_true", help="Explicitly retry the existing final publication without repeating formal apply.")
    recover.add_argument("--transaction", type=Path, help="Specific transaction.json; defaults to all pending transactions.")

    recover_segment = subparsers.add_parser(
        "recover-segment-gate",
        parents=[common],
        help="Precisely recover one thread's package/frontier continuation gate.",
    )
    recover_segment.add_argument("--thread-ref", required=True)
    recover_segment.add_argument("--budget-ms", type=int, default=5000)

    publish_assets = subparsers.add_parser(
        "publish-display-assets",
        parents=[common],
        help=(
            "Promote one verified package's display images into deterministic Obsidian "
            "assets and write a package-external closure receipt."
        ),
    )
    publish_assets.add_argument("--package-root", type=Path, required=True)

    archive = subparsers.add_parser(
        "archive-nightly",
        parents=[common],
        help=(
            "Resume the fixed T9-Data English archive workflow after a committed writer receipt; "
            "never reruns apply."
        ),
    )
    archive.add_argument("--retry-publication", action="store_true", help="Retry the existing final publication; completed support and cleanup are not repeated.")
    archive.add_argument("--manifest", type=Path, required=True)
    archive.add_argument("--writer-receipt", type=Path, required=True)
    archive.add_argument(
        "--retain-local",
        action="store_true",
        help="Verify archive and locator receipts but retain local packages.",
    )

    reopen = subparsers.add_parser(
        "reopen-archive",
        parents=[common],
        help="Verify and reopen one exact archived package through its locator note; never scans the volume.",
    )
    reopen.add_argument("--package-id", required=True)
    reopen.add_argument("--message-sequence", type=int, action="append", default=[])
    reopen.add_argument(
        "--attachment-role",
        action="append",
        default=[],
        choices=[
            "question_image", "solution_image", "explanation_image",
            "user_work_image", "source_article_image", "other_attachment",
        ],
    )

    subparsers.add_parser("status", parents=[common], help="Report event, candidate, manifest and receipt counts.")
    return parser


_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_HANDOFF_SCHEMA = "english-learning-pipeline-handoff-v1"
_HANDOFF_NORMALIZATION = (
    "UTF-8; LF line endings; trailing whitespace removed per line; no final LF"
)


def _resolve_repo_file(repo_root: Path, locator: str, *, label: str) -> tuple[Path, str]:
    if not isinstance(locator, str) or not locator.strip():
        raise ValidationError(f"{label} locator is required")
    root = repo_root.resolve(strict=True)
    supplied = Path(locator)
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValidationError(f"{label} locator must resolve to a repository file") from exc
    if not resolved.is_file():
        raise ValidationError(f"{label} locator must resolve to a regular file")
    return resolved, relative.as_posix()


def _article_metadata_value(article_text: str, key: str) -> str:
    pattern = re.compile(
        rf"^\s*-\s*{re.escape(key)}\s*[：:]\s*`?([^`\r\n]+?)`?\s*$",
        re.MULTILINE,
    )
    values = [match.group(1).strip() for match in pattern.finditer(article_text)]
    if len(values) != 1 or not values[0]:
        raise ValidationError(f"article page must contain exactly one {key} binding")
    return values[0]


def _resolve_canonical_payload(
    repo_root: Path,
    handoff_path: Path,
    locator: str,
) -> Path:
    if not isinstance(locator, str) or not locator.strip():
        raise ValidationError("canonical payload locator must be repository-contained and relative")
    relative_locator = Path(locator)
    if relative_locator.is_absolute() or ".." in relative_locator.parts:
        raise ValidationError("canonical payload locator must be repository-contained and relative")
    root = repo_root.resolve(strict=True)
    candidates: set[Path] = set()
    base = handoff_path.parent
    while True:
        try:
            resolved = (base / relative_locator).resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            pass
        else:
            if resolved.is_file():
                candidates.add(resolved)
        if base == root:
            break
        try:
            base.relative_to(root)
        except ValueError as exc:
            raise ValidationError("handoff file is outside repository root") from exc
        base = base.parent
    if len(candidates) != 1:
        raise ValidationError(
            "canonical payload locator must resolve to exactly one repository file"
        )
    return next(iter(candidates))


def _canonical_handoff_source_bytes(path: Path) -> bytes:
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError("canonical payload must be readable UTF-8 text") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n")).rstrip("\n")
    if not normalized:
        raise ValidationError("canonical payload must not be empty")
    return normalized.encode("utf-8")


def _validate_capture_source_object(
    repo_root: Path,
    request: dict[str, Any],
    *,
    allow_library_fallback: bool = True,
) -> dict[str, Any]:
    article = request.get("article")
    if not isinstance(article, dict):
        raise ValidationError("capture request article must be an object")
    source_id = article.get("source_id")
    source_hash = article.get("source_hash")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValidationError("capture request article.source_id is required")
    if not isinstance(source_hash, str) or not _SHA256_RE.fullmatch(source_hash):
        raise ValidationError("capture request article.source_hash must be lowercase SHA-256")

    article_path, article_locator = _resolve_repo_file(
        repo_root,
        article.get("source_article"),
        label="source_article",
    )
    try:
        article_text = article_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError("source_article must be readable UTF-8 text") from exc

    if allow_library_fallback and not re.search(r"^\s*-\s*pipeline_handoff\s*[：:]", article_text, re.MULTILINE):
        from .sources import resolve_article_source
        bound = resolve_article_source(repo_root, article_locator)
        if bound["source_id"] != source_id or bound["source_hash"] != source_hash:
            raise SourceHashMismatch("capture source differs from the current practice-safe library binding")
        article["source_article"] = article_locator
        article["source_hash"] = bound["source_hash"]
        return request

    page_source_id = _article_metadata_value(article_text, "source_id")
    page_source_hash = _article_metadata_value(article_text, "source_hash")
    handoff_locator = _article_metadata_value(article_text, "pipeline_handoff")
    if page_source_id != source_id:
        raise SourceHashMismatch(
            f"article source_id mismatch: expected {source_id}, got {page_source_id}"
        )
    if Path(handoff_locator).is_absolute() or ".." in Path(handoff_locator).parts:
        raise ValidationError("pipeline_handoff locator must be repository-relative")
    handoff_path, _ = _resolve_repo_file(
        repo_root,
        handoff_locator,
        label="pipeline_handoff",
    )
    try:
        handoff = load_json(handoff_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("pipeline handoff must be valid JSON") from exc
    canonical_payload = handoff.get("canonical_payload")
    if (
        handoff.get("schema_version") != _HANDOFF_SCHEMA
        or handoff.get("source_id") != source_id
        or not isinstance(canonical_payload, dict)
        or canonical_payload.get("visibility") != "practice_safe"
        or canonical_payload.get("normalization") != _HANDOFF_NORMALIZATION
    ):
        raise ValidationError("pipeline handoff identity or canonical payload contract is invalid")
    payload_path = _resolve_canonical_payload(
        repo_root,
        handoff_path,
        canonical_payload.get("path"),
    )
    actual_hash = hashlib.sha256(_canonical_handoff_source_bytes(payload_path)).hexdigest()
    expected_prefixed = f"sha256:{actual_hash}"
    declared_hashes = {
        "request": source_hash,
        "article": page_source_hash,
        "handoff": handoff.get("source_hash"),
    }
    if (
        declared_hashes["request"] != actual_hash
        or declared_hashes["article"] != expected_prefixed
        or declared_hashes["handoff"] != expected_prefixed
    ):
        raise SourceHashMismatch(
            "canonical article hash mismatch: "
            f"actual {actual_hash}, declarations {declared_hashes}"
        )
    article["source_article"] = article_locator
    article["source_hash"] = actual_hash
    return request


def _inline_json_objects(values: list[str], *, label: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, value in enumerate(values, start=1):
        loaded = json.loads(value)
        if not isinstance(loaded, dict):
            raise ValidationError(f"{label} {index} must be a JSON object")
        result.append(loaded)
    return result


def _capture_request(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    if args.input_json:
        request = load_json(args.input_json)
        for field in (
            "schema_version", "package_id", "package_canonical_sha256",
            "formal_write_count", "background_processing", "receipt",
        ):
            request.pop(field, None)
    else:
        if not args.idempotency_key or not args.segment_key or not args.thread_ref:
            raise ValidationError(
                "capture direct flags require --idempotency-key, --segment-key and --thread-ref"
            )
        if not args.source_kind or not args.answer_exposure:
            raise ValidationError(
                "capture direct flags require explicit --source-kind and --answer-exposure; neither is inferred"
            )
        messages: list[dict[str, Any]] = []
        if args.conversation_json:
            loaded = json.loads(args.conversation_json.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                loaded = loaded.get("messages")
            if not isinstance(loaded, list):
                raise ValidationError("--conversation-json must contain a messages array")
            messages.extend(loaded)
        messages.extend(_inline_json_objects(args.message_json, label="message"))
        if not messages:
            raise ValidationError(
                "foreground capture requires --conversation-json or at least one --message-json"
            )
        article_id = args.article_id or args.source_id
        if args.article_id and args.article_id != args.source_id:
            raise ValidationError("--article-id must equal canonical --source-id")
        source = {
            "article_id": article_id,
            "source_article": args.source_article,
            "source_id": args.source_id,
            "source_hash": args.article_sha256,
            "sentence_id": args.sentence_id,
            "paragraph_id": args.paragraph_id,
            "knowledge_point_id": args.knowledge_point_id,
            "question_id": args.question_id,
            "source_sentence": args.source_sentence,
            "sentence_hash": args.sentence_sha256,
            "source_kind": args.source_kind,
            "answer_exposure": args.answer_exposure,
        }
        for key in ("reference_id", "title"):
            value = getattr(args, key)
            if value:
                source[key] = value
        source = {key: value for key, value in source.items() if value not in {None, ""}}
        request = {
            "idempotency_key": args.idempotency_key,
            "segment_key": args.segment_key,
            "source": source,
            "conversation": messages,
            "attachments": _inline_json_objects(args.attachment_json, label="attachment"),
            "expected_attachment_roles": args.expected_attachment_role,
        }
        request["thread_ref"] = args.thread_ref
        if args.previous_token_json:
            request["previous_token"] = load_json(args.previous_token_json)
        if args.occurred_at:
            request["occurred_at"] = args.occurred_at
        if args.study_date:
            request["study_date"] = args.study_date
    if not isinstance(request.get("conversation"), (list, dict)):
        raise ValidationError("capture request must contain the complete conversation")
    source = request.get("source", {})
    if not isinstance(source, dict):
        raise ValidationError("capture request source must be an object")
    if args.review_route:
        source["review_route"] = args.review_route
    if source.get("review_route") not in {None, "web", "local"}:
        raise ValidationError("review_route must be web or local")
    if source.get("source_kind") not in {"article", "question", "option", "explanation", "user_provided"} or source.get("answer_exposure") not in {"answer_free", "protected"}:
        raise ValidationError("capture requires explicit source_kind and answer_exposure in both JSON and direct-flag routes")
    if source.get("source_article"):
        from .sources import resolve_article_source
        binding = resolve_article_source(repo_root, source["source_article"])
        if not source.get("source_id") or not source.get("source_hash"):
            for field in ("source_id", "source_hash"):
                if source.get(field) and str(source[field]).removeprefix("sha256:") != binding[field]:
                    raise SourceHashMismatch(f"capture {field} conflicts with canonical article")
                source[field] = binding[field]
        wrapper = {
            "article": {
                "article_id": source.get("article_id") or source["source_id"],
                "source_article": source["source_article"],
                "source_id": source["source_id"],
                "source_hash": str(source["source_hash"]).removeprefix("sha256:"),
            }
        }
        _validate_capture_source_object(repo_root, wrapper)
        source["source_article"] = wrapper["article"]["source_article"]
        source["source_hash"] = wrapper["article"]["source_hash"]
        source["source_binding"] = binding
    return request


def run(args: argparse.Namespace) -> dict[str, Any]:
    state_dir = args.state_dir.resolve()
    repo_root = args.repo_root.resolve()
    deadline = None
    if args.command in {"capture", "recover-segment-gate"}:
        if not 1 <= args.budget_ms <= 40000:
            raise ValidationError("local capture/recovery budget must be 1 through 40000 ms")
        deadline = time.monotonic() + args.budget_ms / 1000
    if args.command == "validate-package":
        from .packages import validate_conversation_package, read_package_receipt
        package = validate_conversation_package(args.package_root)
        receipt = read_package_receipt(args.package_root)
        return {"schema_version": "english_package_validation_v1", "status": "PASS", "subject": "english",
                "package_id": package["package_id"], "package_sha256": package["package_canonical_sha256"],
                "manifest_sha256": receipt["manifest_sha256"], "message_count": len(load_json(args.package_root / "conversation.json")["messages"]),
                "attachment_count": len(package.get("attachments", [])), "formal_write_count": 0}
    if args.command == "validate-formal-receipt":
        from .packages import validate_canonical_writer_closeout, frozen_package_overrides
        from .learning_state import learning_events_path, load_learning_events
        raw = load_json(args.writer_receipt)
        if not re.fullmatch(r"EN-BATCH-\d{8}-[0-9A-F]{12}", str(raw.get("batch_id", ""))) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.writer_receipt.parent.name):
            raise ValidationError("writer receipt batch/date locator is invalid")
        # The receipt's canonical location preserves the original study date.
        manifest_path = state_dir / "nightly" / args.writer_receipt.parent.name / f"{raw.get('batch_id', '')}.manifest.json"
        manifest = load_json(manifest_path)
        overrides = frozen_package_overrides(state_dir, repo_root, manifest)
        receipt, manifest = validate_canonical_writer_closeout(
            state_dir, args.writer_receipt, expected_manifest_path=manifest_path,
            package_root_overrides=overrides,
        )
        event_ids = {row.get("assigned_id"): row.get("learning_event_sha256") for row in receipt.get("action_results", [])
                     if row.get("action_type") == "learning_event_append" and row.get("result") in {"applied", "skipped"}}
        decisions = [row for row in load_learning_events(learning_events_path(repo_root))
                     if row["event_id"] in event_ids and row["event"]["kind"] == "adjudication"]
        if any(event_ids[row["event_id"]] != row["record_sha256"] for row in decisions):
            raise ValidationError("A decision event differs from its canonical writer receipt")
        verified_attachments = []
        package_documents = {row["package_id"]: row for row in manifest["package_documents"]}
        for decision in decisions:
            for ref in decision["evidence_refs"]:
                match = re.fullmatch(r"manifest.json#/attachments/(\d+)", str(ref.get("pointer", "")))
                if match is None:
                    continue
                package_id = ref["package_id"]
                root = overrides.get(package_id, Path(package_documents[package_id]["path"]))
                attachment = load_json(root / "manifest.json")["attachments"][int(match.group(1))]
                from .util import object_sha256
                if object_sha256(attachment) != ref.get("resolved_node_sha256"):
                    raise ValidationError("A decision attachment differs from its resolved evidence node")
                verified_attachments.append({"decision_event_id": decision["event_id"], "package_id": package_id,
                                             "pointer": ref["pointer"], "role": attachment["role"],
                                             "sha256": attachment["sha256"], "bytes": attachment["bytes"]})
        return {"schema_version": "english_formal_receipt_validation_v1", "status": "PASS", "subject": "english",
                "receipt_id": receipt["receipt_id"], "publication_event_id": receipt["receipt_id"],
                "writer_receipt_sha256": file_sha256(args.writer_receipt), "batch_id": manifest["batch_id"],
                "package_ids": manifest["package_ids"], "package_sha256s": manifest["package_sha256s"],
                "decisions": decisions, "verified_attachments": verified_attachments, "formal_write_count": 0}
    if args.command == "export-publication-sources":
        from .publication import build_source_descriptors
        return build_source_descriptors(repo_root)
    if args.command == "query-personal-summary":
        from .personal_summary import query_personal_summary
        return query_personal_summary(state_dir, keys=args.key,
                                      question_review=args.question_review, max_bytes=args.max_bytes)
    if args.command == "publish-personal-summaries":
        from .personal_summary import publish_personal_summaries
        receipt = None
        if args.writer_receipt:
            verified = build_parser().parse_args(["validate-formal-receipt", "--repo-root", str(repo_root),
                                                 "--state-dir", str(state_dir), "--writer-receipt", str(args.writer_receipt)])
            receipt = run(verified)
        return publish_personal_summaries(repo_root, state_dir, load_json(args.input_json),
                                          formal_receipt=receipt,
                                          bootstrap_existing_formal=args.bootstrap_existing_formal)
    if args.command == "export-unit-dialogue":
        from .unit_dialogue import export_unit_dialogue
        return export_unit_dialogue(args.rollout, turn_ids=args.turn_id, output=args.output)
    if args.command == "query-learning-context":
        from .learning_state import query_learning_context
        return query_learning_context(
            repo_root, state_dir, source_id=args.source_id, sentence_id=args.sentence_id,
            source_article=args.source_article, source_hash=args.source_hash,
            sentence_sha256_value=args.sentence_sha256, bank_ids=args.bank_id,
            sp_ids=args.sp_id, concept_ids=args.concept_id, aliases=args.alias,
            max_bytes=args.max_bytes, allow_protected=args.question_review,
        )
    if args.command == "resolve-source":
        from .sources import build_source_catalog, resolve_article_source
        if args.all:
            return build_source_catalog(repo_root)
        if not args.source_article:
            raise ValidationError("resolve-source requires --source-article or --all")
        return resolve_article_source(repo_root, args.source_article)
    if args.command == "capture":
        request = _capture_request(args, repo_root)
        _, receipt = create_conversation_package(state_dir, request, deadline=deadline)
        return receipt
    if args.command == "complete-article":
        source_id = args.source_id or args.article_id
        if not source_id:
            raise ValidationError("complete-article requires --source-id")
        if args.source_id and args.article_id and args.source_id != args.article_id:
            raise ValidationError("--article-id must equal canonical --source-id")
        return complete_article_packages(
            state_dir,
            source_id=source_id,
            idempotency_key=args.idempotency_key,
            study_date=args.study_date,
            output_dir=args.output_dir,
            repo_root=repo_root,
        )
    if args.command == "freeze-nightly":
        path, manifest = freeze_nightly(
            state_dir,
            repo_root,
            study_date=args.study_date,
            output=args.output,
            package_ids=set(args.package_id) if args.package_id else None,
            local_review_id=args.local_review_id,
        )
        return {
            "schema_version": "english_freeze_receipt_v1",
            "status": manifest["status"],
            "batch_id": manifest["batch_id"],
            "study_date": manifest["study_date"],
            "manifest": str(path),
            "package_count": len(manifest["package_documents"]),
            "package_ids": manifest["package_ids"],
            "package_sha256s": manifest["package_sha256s"],
            "formal_write_count": 0,
            "formal_writeback": "none",
        }
    if args.command == "plan-backlog":
        path, plan = build_backlog_plan(
            state_dir,
            repo_root,
            cutoff_date=args.cutoff_date,
            only_today=args.only_today,
            package_ids=args.package_id,
            local_review_id=args.local_review_id,
            include_legacy_audit=not args.no_legacy_audit,
            persist=not args.no_persist,
            output=args.output,
        )
        return {
            **plan,
            "plan_path": str(path) if path is not None else None,
            "plan_file_sha256": file_sha256(path) if path is not None else None,
        }
    if args.command == "audit-legacy-events":
        return audit_legacy_capture_events(
            state_dir,
            cutoff_date=args.cutoff_date,
        )
    if args.command == "retire-legacy-events":
        return retire_legacy_capture_events(
            state_dir,
            args.event_id,
            apply=args.apply,
            authorization=args.authorization,
        )
    if args.command == "run-backlog":
        return execute_backlog_plan(
            state_dir,
            repo_root,
            plan_path=args.plan,
            actions_dir=args.actions_dir,
            support_dir=args.support_dir,
            archive=not args.no_archive,
            apply=args.apply,
            authorization=args.authorization,
        )
    if args.command == "finalize-backlog-gate":
        path, gate = write_backlog_gate(state_dir, plan_path=args.plan)
        return {**gate, "gate_path": str(path), "gate_file_sha256": file_sha256(path)}
    if args.command == "apply-nightly":
        path, receipt = apply_nightly(
            state_dir,
            repo_root,
            manifest_path=args.manifest,
            actions_path=args.actions,
            apply=args.apply,
            authorization=args.authorization,
            authorized_at=args.authorized_at,
            support_preflight_path=args.support_preflight,
        )
        return {**receipt, "receipt_path": str(path)}
    if args.command == "refresh-sentence-support":
        path, receipt = refresh_sentence_support(
            state_dir,
            repo_root,
            manifest_path=args.manifest,
            writer_receipt_path=args.writer_receipt,
            proposal_path=args.proposal,
        )
        return {**receipt, "receipt_path": str(path)}
    if args.command == "query-sentence-support":
        return query_sentence_support(
            state_dir,
            source_id=args.source_id,
            sentence_id=args.sentence_id,
            source_hash=args.source_hash,
            sentence_sha256_value=args.sentence_sha256,
            max_records=args.max_records,
            max_bytes=args.max_bytes,
        )
    if args.command == "recover-nightly":
        return recover_nightly(state_dir, repo_root, transaction_path=args.transaction, retry_publication=args.retry_publication)
    if args.command == "recover-segment-gate":
        return recover_segment_gate(state_dir, thread_ref=args.thread_ref, deadline=deadline)
    if args.command == "publish-display-assets":
        path, receipt = publish_display_assets(
            state_dir,
            repo_root,
            args.package_root,
        )
        return {**receipt, "receipt_path": str(path)}
    if args.command == "archive-nightly":
        return archive_committed_batch(
            state_dir,
            repo_root,
            manifest_path=args.manifest,
            writer_receipt_path=args.writer_receipt,
            cleanup_local=not args.retain_local,
            retry_publication=args.retry_publication,
        )
    if args.command == "reopen-archive":
        return reopen_archived_package(
            repo_root,
            package_id=args.package_id,
            message_sequences=args.message_sequence,
            attachment_roles=args.attachment_role,
        )
    if args.command == "status":
        return pipeline_status(state_dir)
    raise ValidationError(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except SegmentCapturePending as exc:
        print(
            json.dumps(
                {
                    "schema_version": "english_pipeline_error_v1",
                    "status": "segment_capture_pending",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "package_id": exc.package_id,
                    "formal_write_count": 0,
                    "background_processing": "none",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    except (PipelineError, OSError, ValueError, json.JSONDecodeError) as exc:
        command = getattr(args, "command", None)
        status = (
            "FORMAL_COMMITTED_SUPPORT_PENDING"
            if command == "refresh-sentence-support"
            else
            "FORMAL_COMMITTED_ARCHIVE_PENDING"
            if command in {"archive-nightly", "publish-display-assets"}
            else "segment_capture_pending"
            if command == "capture"
            else "ERROR"
        )
        payload = {
            "schema_version": "english_pipeline_error_v1",
            "status": status,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        if command == "capture":
            payload.update(
                {"formal_write_count": 0, "background_processing": "none"}
            )
        print(
            json.dumps(payload, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") in {"CAS_CONFLICT", "FAILED", "capture_saved_but_projection_failed"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
