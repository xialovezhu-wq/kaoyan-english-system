from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import IdempotencyConflict, ValidationError
from .formal import formal_snapshot
from .review_status import load_review_status_ledger
from .packages import package_records
from .util import atomic_write_json, bytes_sha256, file_sha256, load_json, object_sha256, utc_now


def freeze_nightly(
    state_dir: Path,
    repo_root: Path,
    *,
    study_date: str,
    output: Path | None = None,
    package_ids: set[str] | None = None,
    web_review_id: str | None = None,
    local_review_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    if local_review_id and (web_review_id or package_ids is None):
        raise ValidationError("native local review requires an explicit package set and no web review")
    all_documents = package_records(state_dir, study_date)
    documents = list(all_documents)
    requested_package_ids = set(package_ids) if package_ids is not None else None
    if requested_package_ids is not None:
        documents = [
            row for row in documents if row["package_id"] in requested_package_ids
        ]
    from .web_review_gate import managed_package, allowed_packages
    from .packages import validate_conversation_package
    allowed = allowed_packages(state_dir, web_review_id) if web_review_id else {}
    if local_review_id:
        from .local_review import validate_local_packages
        validate_local_packages(state_dir, local_review_id, documents, study_date=study_date)
        if requested_package_ids != {p["package_id"] for p in documents}:
            raise ValidationError("native local review requested package set differs")
        allowed = {p["package_id"]: p["package_sha256"] for p in documents}
    waiting = []
    eligible = []
    for row in documents:
        package = validate_conversation_package(Path(row["path"]))
        if managed_package(state_dir, package) and allowed.get(row["package_id"]) != row["package_sha256"]:
            waiting.append(row["package_id"])
        else:
            eligible.append(row)
    if waiting and requested_package_ids is not None:
        raise ValidationError("WAITING_WEB_REVIEW: " + ", ".join(waiting))
    documents = eligible
    package_ids = [row["package_id"] for row in documents]
    package_sha256s = [row["package_sha256"] for row in documents]
    source_snapshot: dict[str, dict[str, Any]] = {}
    for row in documents:
        source = load_json(Path(row["path"]) / "source.json")
        source_snapshot[row["package_id"]] = source.get("identity", {})
    snapshot = formal_snapshot(repo_root)
    ledger_path = repo_root / "bank" / "review_exclusion_ledger.jsonl"
    ledger_records = load_review_status_ledger(ledger_path)
    review_status_proposals = {
        "schema_version": "english_review_status_proposals_v1",
        "review_exclusion_proposals": [],
        "reactivation_proposals": [],
        "source": "conversation_packages_require_sol_review",
    }
    snapshot["review_exclusion_ledger"] = {
        "path": str(ledger_path.resolve()),
        "exists": ledger_path.exists(),
        "sha256": (
            file_sha256(ledger_path)
            if ledger_path.exists()
            else bytes_sha256(b"")
        ),
        "event_count": len(ledger_records),
    }
    from .learning_state import learning_events_path, load_learning_events
    event_path = learning_events_path(repo_root)
    learning_events = load_learning_events(event_path)
    snapshot["learning_events"] = {
        "path": str(event_path.resolve()), "exists": event_path.exists(),
        "sha256": file_sha256(event_path) if event_path.exists() else bytes_sha256(b""),
        "event_count": len(learning_events),
    }
    review_status_source_hashes = {
        "master_bank": snapshot["master_bank"]["sha256"],
        "mastered_items": snapshot["mastered_items"]["sha256"],
        "review_exclusion_ledger": snapshot["review_exclusion_ledger"]["sha256"],
    }
    required_postformal_closures = [
        "sentence_support",
        "display_assets",
        "archive",
        "locator",
        "cleanup",
    ]
    sentence_support_history_scope = {
        "mode": (
            "full_local_date"
            if {row["package_id"] for row in documents}
            == {row["package_id"] for row in all_documents}
            else "package_subset"
        ),
        "selected_package_ids": package_ids,
        "all_local_package_ids": [row["package_id"] for row in all_documents],
    }
    batch_identity = object_sha256(
        {
            "study_date": study_date,
            "package_documents": documents,
            "package_ids": package_ids,
            "package_sha256s": package_sha256s,
            "source_snapshot": source_snapshot,
            "review_status_proposals": review_status_proposals,
            "review_status_source_hashes": review_status_source_hashes,
            "required_postformal_closures": required_postformal_closures,
            "postformal_contract_version": "sentence-support-v1",
            "sentence_support_history_scope": sentence_support_history_scope,
            "formal_hashes": {name: row["sha256"] for name, row in snapshot.items()},
            **({"web_review_id": web_review_id} if web_review_id else {}),
            **({"local_review_id": local_review_id} if local_review_id else {}),
        }
    )[:12].upper()
    batch_id = f"EN-BATCH-{study_date.replace('-', '')}-{batch_identity}"
    manifest = {
        "schema_version": "english_nightly_manifest_v3",
        "batch_id": batch_id,
        "study_date": study_date,
        "frozen_at": utc_now(),
        "package_documents": documents,
        "package_ids": package_ids,
        "package_sha256s": package_sha256s,
        "source_snapshot": source_snapshot,
        "review_status_proposals": review_status_proposals,
        "review_status_proposals_sha256": object_sha256(
            review_status_proposals
        ),
        "review_status_source_hashes": review_status_source_hashes,
        "required_postformal_closures": required_postformal_closures,
        "postformal_contract_version": "sentence-support-v1",
        "sentence_support_history_scope": sentence_support_history_scope,
        "formal_snapshot": snapshot,
        "authorization": {
            "command": "apply-nightly",
            "batch_id": batch_id,
            "study_date": study_date,
            "authorized": False,
            "authorized_at": None,
            "scope": [
                "master_bank", "mastered_items", "sentence_patterns",
                "review_exclusion_ledger",
                "learning_events",
            ],
        },
        "status": "frozen" if documents else "NOOP",
        **({"web_review_id": web_review_id} if web_review_id else {}),
        **({"local_review_id": local_review_id} if local_review_id else {}),
    }
    target = output or state_dir / "nightly" / study_date / f"{batch_id}.manifest.json"
    if target.exists():
        existing = load_json(target)
        comparable_existing = {key: value for key, value in existing.items() if key != "frozen_at"}
        comparable_new = {key: value for key, value in manifest.items() if key != "frozen_at"}
        if comparable_existing != comparable_new:
            raise IdempotencyConflict(f"immutable nightly manifest already exists with different content: {target}")
        return target, existing
    atomic_write_json(target, manifest)
    return target, manifest


def pipeline_status(state_dir: Path) -> dict[str, Any]:
    package_roots = sorted((state_dir / "packages").glob("*/*/manifest.json"))
    archive_pointers = sorted((state_dir / "archive-pointers").glob("*/*.json"))
    manifest_files = sorted((state_dir / "nightly").glob("**/*.manifest.json"))
    receipt_files = sorted((state_dir / "receipts").glob("**/*.json"))
    latest_receipts: list[dict[str, Any]] = []
    for path in receipt_files[-10:]:
        try:
            receipt = load_json(path)
        except Exception:
            continue
        latest_receipts.append(
            {
                "path": str(path),
                "receipt_id": receipt.get("receipt_id"),
                "status": receipt.get("status"),
            }
        )
    return {
        "schema_version": "english_pipeline_status_v2",
        "state_dir": str(state_dir.resolve()),
        "local_package_count": len(package_roots),
        "archive_pointer_count": len(archive_pointers),
        "nightly_manifest_count": len(manifest_files),
        "receipt_count": len(receipt_files),
        "latest_receipts": latest_receipts,
        "package_route": "direct_local_staging",
    }


def validate_events_report(state_dir: Path, *, study_date: str | None = None) -> dict[str, Any]:
    from .events import load_events, validate_event
    from .util import object_sha256, parse_iso_date

    records: list[dict[str, Any]] = []
    for event in load_events(state_dir):
        event_date = parse_iso_date(str(event["occurred_at"]))
        if study_date is not None and event_date != study_date:
            continue
        validate_event(event)
        records.append(
            {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "study_date": event_date,
                "event_sha256": object_sha256(event),
                "source_id": event["article"]["source_id"],
                "article_source_hash": event["article"]["source_hash"],
                "sentence_hash": event.get("source", {}).get("sentence_hash"),
                "evidence_origin": event.get("learning", {}).get("evidence_origin"),
            }
        )
    return {
        "schema_version": "english_event_validation_report_v1",
        "status": "PASS",
        "study_date": study_date,
        "validated_event_count": len(records),
        "events": records,
        "formal_write_count": 0,
    }
