"""Formal-closure gate for a new DeepSeek article vocabulary export task."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .archive import VolumeContract, _parse_locator, resolve_archived_package_from_pointer
from .backlog import (_local_package_record, _open_transactions_by_package,
                      _trusted_consumed_pointer)
from .constants import MASTERED_HEADER, MASTER_HEADER
from .errors import IdempotencyConflict, ValidationError
from .formal import read_csv
from .learning_state import formal_versions
from .packages import complete_article_packages, validate_conversation_package
from .review_status import (effective_mastered_ids, effective_mastered_norms,
                            effective_review_status, load_review_status_ledger)
from .sources import build_source_catalog
from .util import atomic_write_json, load_json, object_sha256


SCHEMA = "english_vocab_formal_handoff_v1"


def _source_binding(repo: Path, source_id: str) -> dict[str, Any]:
    matches = [row for row in build_source_catalog(repo)["entries"] if row["source_id"] == source_id]
    if len(matches) != 1:
        raise ValidationError("vocab handoff source_id is missing or ambiguous")
    return matches[0]


def _package_rows(repo: Path, state: Path, source_id: str,
                  archive_contract: VolumeContract | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    open_transactions = _open_transactions_by_package(state)
    for root in sorted((state / "packages").glob("*/*")):
        if not root.is_dir():
            continue
        manifest = validate_conversation_package(root)
        if manifest.get("source_identity", {}).get("source_id") != source_id:
            continue
        record = _local_package_record(state, root, open_transactions)
        rows.append({"package_id": manifest["package_id"],
                     "package_sha256": manifest["package_canonical_sha256"],
                     "study_date": manifest["study_date"],
                     "review_route": manifest.get("source_identity", {}).get("review_route") or "legacy_unspecified",
                     "closure_status": record["status"], "closure_reason": record["reason"],
                     "pending_component": record.get("pending_component"),
                     "evidence_location": "local"})
        seen.add(manifest["package_id"])
    for pointer_path in sorted((state / "archive-pointers").glob("*/*.json")):
        package_id = pointer_path.stem
        if package_id in seen:
            continue
        locator = repo / "wiki" / "raw_archives" / f"{package_id}.md"
        if not locator.is_file():
            continue
        try:
            locator_data = _parse_locator(locator.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError):
            continue
        if source_id not in locator_data.get("retrieval_keys", []):
            continue
        pointer = load_json(pointer_path)
        digest = str(pointer.get("package_sha256", ""))
        consumed = _trusted_consumed_pointer(state, pointer_path.parent.name, package_id, digest)
        if consumed is None:
            rows.append({"package_id": package_id, "package_sha256": digest,
                         "study_date": pointer_path.parent.name, "review_route": "unknown_archived",
                         "closure_status": "archive_incomplete",
                         "closure_reason": "trusted archive/formal/support closure did not revalidate",
                         "pending_component": "trusted_archive_closure",
                         "evidence_location": "archive_pointer"})
            continue
        from .archive import verify_volume_contract
        volume = verify_volume_contract(archive_contract or VolumeContract())
        root = resolve_archived_package_from_pointer(
            state, repo, study_date=pointer_path.parent.name, package_id=package_id,
            expected_package_sha256=digest,
        )
        root.relative_to(volume["subject_root"])
        manifest = validate_conversation_package(root)
        if (manifest.get("source_identity", {}).get("source_id") != source_id
                or manifest["package_canonical_sha256"] != digest):
            raise ValidationError("archived vocab handoff package differs from its source binding")
        rows.append({"package_id": package_id, "package_sha256": digest,
                     "study_date": manifest["study_date"],
                     "review_route": manifest.get("source_identity", {}).get("review_route") or "legacy_unspecified",
                     "closure_status": "already_consumed", "closure_reason": "trusted_archive_cleanup",
                     "pending_component": None,
                     "evidence_location": "verified_archive"})
        seen.add(package_id)
    return sorted(rows, key=lambda row: (row["study_date"], row["package_id"]))


def _formal_context(repo: Path) -> dict[str, Any]:
    versions = formal_versions(repo)
    bank = read_csv(repo / "bank/master_bank.csv", MASTER_HEADER)
    mastered = read_csv(repo / "bank/mastered_items.csv", MASTERED_HEADER)
    review_path = repo / "bank/review_exclusion_ledger.jsonl"
    reviews = load_review_status_ledger(review_path)
    if formal_versions(repo) != versions:
        raise ValidationError("formal sources changed while building vocab handoff")
    mastered_norms = effective_mastered_norms(mastered, reviews)
    mastered_ids = effective_mastered_ids(mastered, reviews)
    current_review = effective_review_status(reviews)
    review_excluded = {
        item for item, row in current_review.items()
        if row["status"] in {"mastered_sentence_nonreport", "independent_correct_use"}
    }
    return {
        "formal_version": object_sha256(versions),
        "formal_source_hashes": versions,
        "master_bank_rows": bank,
        "current_mastered_items": sorted(mastered_norms),
        "current_mastered_ids": sorted(mastered_ids),
        "current_review_status": current_review,
        "current_output_exclusions": sorted(mastered_norms | review_excluded),
        "matching_policy": {
            "current_mastered": "exclude from this article output",
            "same_sense_existing_bank": "reuse existing bank IDs and keep the article occurrence; do not blanket-exclude",
            "different_sense_existing_spelling": "treat as a separate sense only when exact source evidence supports it",
        },
    }


def create_vocab_handoff(repo_root: Path, state_dir: Path, *, source_id: str,
                         output_dir: Path | None = None,
                         archive_contract: VolumeContract | None = None) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    state = state_dir.resolve()
    binding = _source_binding(repo, source_id)
    rows = _package_rows(repo, state, source_id, archive_contract)
    if not rows:
        raise ValidationError("VOCAB_HANDOFF_PENDING: no exact conversation packages for this source")
    web_rows = [row for row in rows if row["review_route"] == "web"]
    if not web_rows:
        raise ValidationError("VOCAB_HANDOFF_PENDING: no new web-review packages for this source")
    incomplete = [row for row in web_rows if row["closure_status"] != "already_consumed"]
    if incomplete:
        detail = ", ".join(
            f"{row['package_id']}:{row['closure_status']}"
            + (f"/{row['pending_component']}" if row.get("pending_component") else "")
            for row in incomplete
        )
        raise ValidationError(f"VOCAB_HANDOFF_PENDING: formal/support/archive closure incomplete: {detail}")
    package_binding = [{"package_id": row["package_id"], "package_sha256": row["package_sha256"]}
                       for row in rows]
    completion_key = "vocab-handoff:" + source_id + ":" + object_sha256(package_binding)
    completion = complete_article_packages(
        state, source_id=source_id, idempotency_key=completion_key,
        repo_root=repo, archive_contract=archive_contract,
    )
    if (completion.get("schema_version") != "english_article_completion_receipt_v2"
            or completion.get("status") not in {"created", "idempotent_noop"}
            or list(zip(completion.get("package_ids", []), completion.get("package_sha256s", [])))
            != [(row["package_id"], row["package_sha256"]) for row in rows]):
        raise ValidationError("article completion does not bind the exact gated package set")
    completion_binding = {
        key: value for key, value in completion.items() if key != "status"
    }
    completion_binding["status"] = "verified"
    formal = _formal_context(repo)
    core = {
        "schema_version": SCHEMA, "source_id": source_id,
        "source_hash": binding["source_hash"], "source_article": binding["source_article"],
        "article_completion_receipt": completion_binding,
        "packages": rows, "required_web_package_count": len(web_rows),
        "legacy_evidence": [row for row in rows if row["review_route"] != "web"],
        "formal_context": formal,
        "task_entry": {
            "task_kind": "new_deepseek_vocab_export",
            "model": "DeepSeek V4.1 Flash",
            "provider_policy": "preserve the user's selected official or OpenCode provider identity; no override or fallback",
            "skill": "$kaoyan-english-vocab-export",
            "instruction": "Read this exact handoff and its completion snapshot, then produce the current full BBDC A/B/C output. Do not write formal data.",
        },
        "formal_write_count": 0, "background_processing": "none",
    }
    handoff_id = "EN-VOCAB-HANDOFF-" + object_sha256(core)[:24].upper()
    receipt = {**core, "handoff_id": handoff_id}
    destination = output_dir or state / "vocab-handoffs" / source_id
    target = destination / f"{handoff_id}.json"
    if target.exists():
        if load_json(target) != receipt:
            raise IdempotencyConflict("vocab handoff receipt differs at its deterministic identity")
        status = "idempotent_noop"
    else:
        atomic_write_json(target, receipt)
        status = "created"
    return {"status": status, "handoff_id": handoff_id, "handoff_path": str(target),
            "article_snapshot_path": completion["export_json"],
            "completion_receipt_path": str(
                state / "receipts" / "completion" / completion["study_date"]
                / f"{completion['receipt_id']}.json"
            ),
            "new_task_model": "DeepSeek V4.1 Flash",
            "new_task_prompt": f"Use $kaoyan-english-vocab-export and read {target} to produce the full BBDC output.",
            "package_count": len(rows), "required_web_package_count": len(web_rows),
            "formal_version": formal["formal_version"], "formal_write_count": 0}
