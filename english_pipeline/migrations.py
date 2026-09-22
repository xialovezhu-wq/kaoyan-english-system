from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .events import (
    EVENT_SCHEMA_VERSION,
    LEGACY_EVENT_SCHEMA_VERSION,
    _derive_signal_contract,
    validate_event,
)
from .review_status import canonical_machine_decision
from .util import atomic_write_json, file_sha256, load_json, object_sha256, utc_now


MIGRATION_VERSION = "english_capture_decision_enum_v1_to_v2"


def migrate_capture_event(event: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if event.get("schema_version") != LEGACY_EVENT_SCHEMA_VERSION:
        raise ValidationError("migration source must be english_capture_event_v1")
    effective = copy.deepcopy(event)
    mappings: list[dict[str, str]] = []
    for candidate in effective.get("candidates", []):
        before = str(candidate.get("decision", ""))
        after = canonical_machine_decision(before)
        if before != after:
            mappings.append({"from": before, "to": after})
            candidate["decision"] = after
    unique_mappings = sorted(
        {object_sha256(row): row for row in mappings}.values(),
        key=lambda row: (row["from"], row["to"]),
    )
    if unique_mappings != [{"from": "长期库候选", "to": "long_term_candidate"}]:
        raise ValidationError("migration lacks the unique authorized decision mapping")
    effective["schema_version"] = EVENT_SCHEMA_VERSION
    _derive_signal_contract(effective)
    validate_event(effective)
    return effective, unique_mappings


def write_migration_receipt(event_path: Path, migration_root: Path) -> tuple[Path, dict[str, Any]]:
    event_path = event_path.resolve()
    source = load_json(event_path)
    if not isinstance(source, dict):
        raise ValidationError("migration source event must be an object")
    effective, mappings = migrate_capture_event(source)
    semantic_identity = {
        "migration_version": MIGRATION_VERSION,
        "source_event_file_sha256": file_sha256(event_path),
        "source_event_object_sha256": object_sha256(source),
        "effective_event_sha256": object_sha256(effective),
        "mappings": mappings,
    }
    content_address = object_sha256(semantic_identity)
    receipt = {
        "schema_version": "english_capture_migration_receipt_v1",
        "migration_id": "EN-MIG-" + content_address[:24].upper(),
        "content_address": content_address,
        "migration_version": MIGRATION_VERSION,
        "created_at": utc_now(),
        "source_event_id": source["event_id"],
        "source_event_path": str(event_path),
        "source_event_file_sha256": semantic_identity["source_event_file_sha256"],
        "source_event_object_sha256": semantic_identity["source_event_object_sha256"],
        "source_schema_version": source["schema_version"],
        "mappings": mappings,
        "effective_event": effective,
        "effective_event_sha256": semantic_identity["effective_event_sha256"],
        "model_call_count": 0,
        "formal_write_count": 0,
    }
    target = migration_root.resolve() / f"{receipt['migration_id']}.json"
    if target.exists():
        existing = load_json(target)
        comparable_existing = {key: value for key, value in existing.items() if key != "created_at"}
        comparable_new = {key: value for key, value in receipt.items() if key != "created_at"}
        if comparable_existing != comparable_new:
            raise ValidationError("immutable migration receipt conflicts with existing content")
        return target, existing
    atomic_write_json(target, receipt)
    return target, receipt


def validate_migration_receipt(
    receipt: dict[str, Any],
    *,
    source_event: dict[str, Any],
    source_path: Path,
) -> dict[str, Any]:
    required = {
        "schema_version", "migration_id", "content_address", "migration_version",
        "created_at", "source_event_id", "source_event_path",
        "source_event_file_sha256", "source_event_object_sha256",
        "source_schema_version", "mappings", "effective_event",
        "effective_event_sha256", "model_call_count", "formal_write_count",
    }
    if set(receipt) != required:
        raise ValidationError("migration receipt fields do not match schema")
    if (
        receipt["schema_version"] != "english_capture_migration_receipt_v1"
        or receipt["migration_version"] != MIGRATION_VERSION
        or receipt["source_event_id"] != source_event.get("event_id")
        or receipt["source_event_file_sha256"] != file_sha256(source_path)
        or receipt["source_event_object_sha256"] != object_sha256(source_event)
        or receipt["source_schema_version"] != source_event.get("schema_version")
        or receipt["mappings"] != [{"from": "长期库候选", "to": "long_term_candidate"}]
        or receipt["model_call_count"] != 0
        or receipt["formal_write_count"] != 0
    ):
        raise ValidationError("migration receipt source binding is invalid")
    effective = receipt["effective_event"]
    if not isinstance(effective, dict) or receipt["effective_event_sha256"] != object_sha256(effective):
        raise ValidationError("migration effective event hash mismatch")
    semantic_identity = {
        "migration_version": receipt["migration_version"],
        "source_event_file_sha256": receipt["source_event_file_sha256"],
        "source_event_object_sha256": receipt["source_event_object_sha256"],
        "effective_event_sha256": receipt["effective_event_sha256"],
        "mappings": receipt["mappings"],
    }
    if receipt["content_address"] != object_sha256(semantic_identity):
        raise ValidationError("migration receipt content address mismatch")
    if receipt["migration_id"] != "EN-MIG-" + receipt["content_address"][:24].upper():
        raise ValidationError("migration receipt id mismatch")
    if effective.get("event_id") != source_event.get("event_id"):
        raise ValidationError("migration changed immutable event identity")
    validate_event(effective)
    return copy.deepcopy(effective)
