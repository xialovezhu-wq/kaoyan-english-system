from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .archive import (
    VolumeContract,
    archive_committed_batch,
    validate_archive_locator_binding,
)
from .errors import AuthorizationError, IdempotencyConflict, ValidationError
from .display_assets import validate_display_asset_closure
from .nightly import freeze_nightly
from .packages import (
    _attachment_inputs,
    _normalize_messages,
    _source_document,
    validate_canonical_writer_closeout,
    validate_conversation_package,
    validate_receipt_resolved_evidence,
)
from .util import (
    atomic_write_json,
    exclusive_lock,
    file_sha256,
    load_json,
    object_sha256,
    parse_iso_date,
    utc_now,
)
from .writer import (
    OPEN_TRANSACTION_STATUSES,
    _validated_journal_records,
    apply_nightly,
    recover_nightly,
)
from .sentence_support import (
    preflight_sentence_support,
    refresh_sentence_support,
    sentence_support_required,
    support_receipt_path,
    validate_sentence_support_refresh,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
BACKLOG_PLAN_SCHEMA_VERSION = "english_package_backlog_plan_v1"
BACKLOG_GATE_SCHEMA_VERSION = "english_package_backlog_global_gate_v1"
LEGACY_AUDIT_SCHEMA_VERSION = "english_legacy_capture_reachability_audit_v2"
LEGACY_EVIDENCE_SCHEMA_VERSION = "english_legacy_package_migration_evidence_v1"
LEGACY_RETIREMENT_SCHEMA_VERSION = "english_legacy_event_retirement_v1"
_PACKAGE_ID_PREFIX = "EN-PKG-"
_PACKAGE_ID_RE = re.compile(r"^EN-PKG-([0-9]{8})-[A-F0-9]{16}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_EVENT_ID_RE = re.compile(r"^EVT-([0-9]{8})-[A-F0-9]{16}$")
_LEGACY_RETIREMENT_PREFIX = "EN-LEGACY-RETIRE-"


def shanghai_current_date(now: datetime | None = None) -> str:
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    return current.astimezone(SHANGHAI).date().isoformat()


def _validate_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError(f"invalid cutoff date: {value}") from exc
    return parsed.date().isoformat()


def _capture_receipt_for_event(state_dir: Path, event: dict[str, Any]) -> Path:
    day = parse_iso_date(str(event.get("occurred_at", "")))
    event_id = str(event.get("event_id", ""))
    return state_dir / "receipts" / "capture" / day / f"CAPTURE-{event_id[4:]}.json"


def _legacy_event_study_date(event_id: str) -> str:
    match = _LEGACY_EVENT_ID_RE.fullmatch(event_id)
    if match is None:
        raise ValidationError(f"invalid legacy capture event id: {event_id}")
    token = match.group(1)
    return _validate_date(f"{token[:4]}-{token[4:6]}-{token[6:]}")


def _legacy_event_binding(state_dir: Path, event_id: str) -> dict[str, Any]:
    state_root = state_dir.resolve()
    study_date = _legacy_event_study_date(event_id)
    event_path = state_root / "events" / study_date / f"{event_id}.json"
    if event_path.is_symlink() or not event_path.is_file():
        raise ValidationError(f"canonical legacy event is missing: {event_id}")
    try:
        event = load_json(event_path)
        occurred_date = parse_iso_date(str(event.get("occurred_at", "")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"canonical legacy event is invalid: {event_id}") from exc
    if (
        event.get("schema_version")
        not in {"english_capture_event_v1", "english_capture_event_v2"}
        or event.get("event_id") != event_id
        or event.get("event_type") != "sentence_captured"
        or occurred_date != study_date
        or event.get("formal_write_count") != 0
    ):
        raise ValidationError(f"canonical legacy event identity mismatch: {event_id}")
    event_object_sha = object_sha256(event)
    receipt_path = _capture_receipt_for_event(state_root, event)
    expected_receipt_path = (
        state_root / "receipts" / "capture" / study_date
        / f"CAPTURE-{event_id[4:]}.json"
    )
    if receipt_path != expected_receipt_path:
        raise ValidationError(f"canonical capture receipt date mismatch: {event_id}")
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValidationError(f"canonical capture receipt is missing: {event_id}")
    try:
        receipt = load_json(receipt_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"canonical capture receipt is invalid: {event_id}") from exc
    if (
        receipt.get("schema_version")
        not in {"english_capture_receipt_v1", "english_capture_receipt_v2"}
        or receipt.get("capture_id") != event_id
        or receipt.get("event_sha256") != event_object_sha
        or receipt.get("formal_write_count") != 0
    ):
        raise ValidationError(f"canonical capture receipt binding mismatch: {event_id}")
    return {
        "event_id": event_id,
        "study_date": study_date,
        "event_path": event_path.relative_to(state_root).as_posix(),
        "event_object_sha256": event_object_sha,
        "event_file_sha256": file_sha256(event_path),
        "capture_receipt_path": receipt_path.relative_to(state_root).as_posix(),
        "capture_receipt_sha256": file_sha256(receipt_path),
    }


def _normalize_legacy_event_ids(event_ids: list[str]) -> list[str]:
    if not event_ids:
        raise ValidationError("retirement requires at least one explicit --event-id")
    normalized = [str(value) for value in event_ids]
    if len(normalized) != len(set(normalized)):
        raise ValidationError("retirement event ids must not contain duplicates")
    for event_id in normalized:
        _legacy_event_study_date(event_id)
    return sorted(normalized)


def _legacy_retirement_core(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": LEGACY_RETIREMENT_SCHEMA_VERSION,
        "disposition": "user_removed",
        "authority": "explicit_user_request",
        "events": bindings,
        "event_count": len(bindings),
        "package_write_count": 0,
        "formal_write_count": 0,
        "migration_performed": False,
        "background_processing": "none",
    }


def _new_legacy_retirement_document(
    bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    core = _legacy_retirement_core(bindings)
    digest = object_sha256(core)
    return {
        **core,
        "retirement_id": f"{_LEGACY_RETIREMENT_PREFIX}{digest}",
        "retirement_sha256": digest,
        "created_at": utc_now(),
    }


def _validate_legacy_retirement_document(
    state_dir: Path,
    registry_path: Path,
    document: dict[str, Any],
) -> dict[str, Any]:
    state_root = state_dir.resolve()
    registry_root = state_root / "legacy-event-retirements"
    try:
        relative_registry_path = registry_path.resolve(strict=True).relative_to(
            registry_root.resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise ValidationError("legacy retirement registry path is not canonical") from exc
    if len(relative_registry_path.parts) != 1 or registry_path.is_symlink():
        raise ValidationError("legacy retirement registry must be a direct regular file")
    bindings = document.get("events")
    if not isinstance(bindings, list) or not bindings:
        raise ValidationError("legacy retirement registry must bind explicit events")
    if any(not isinstance(row, dict) for row in bindings):
        raise ValidationError("legacy retirement event binding must be an object")
    event_ids = [str(row.get("event_id", "")) for row in bindings]
    if event_ids != sorted(event_ids) or len(event_ids) != len(set(event_ids)):
        raise ValidationError("legacy retirement event bindings are not uniquely sorted")
    expected_core = _legacy_retirement_core(bindings)
    digest = object_sha256(expected_core)
    retirement_id = f"{_LEGACY_RETIREMENT_PREFIX}{digest}"
    if (
        document.get("schema_version") != LEGACY_RETIREMENT_SCHEMA_VERSION
        or document.get("disposition") != "user_removed"
        or document.get("authority") != "explicit_user_request"
        or document.get("event_count") != len(bindings)
        or document.get("package_write_count") != 0
        or document.get("formal_write_count") != 0
        or document.get("migration_performed") is not False
        or document.get("background_processing") != "none"
        or document.get("retirement_sha256") != digest
        or document.get("retirement_id") != retirement_id
        or registry_path.name != f"{retirement_id}.json"
    ):
        raise ValidationError("legacy retirement registry identity or hash mismatch")
    created_at = document.get("created_at")
    if not isinstance(created_at, str):
        raise ValidationError("legacy retirement registry created_at is invalid")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("legacy retirement registry created_at is invalid") from exc
    if created.tzinfo is None:
        raise ValidationError("legacy retirement registry created_at must include timezone")
    allowed_keys = set(expected_core) | {
        "retirement_id", "retirement_sha256", "created_at",
    }
    if set(document) != allowed_keys:
        raise ValidationError("legacy retirement registry contains unexpected fields")
    for binding in bindings:
        expected_binding = _legacy_event_binding(state_root, str(binding["event_id"]))
        if binding != expected_binding:
            raise ValidationError(
                f"legacy retirement source binding drift: {binding['event_id']}"
            )
    return document


def _load_legacy_event_retirement_pairs(
    state_dir: Path,
) -> list[tuple[Path, dict[str, Any]]]:
    state_root = state_dir.resolve()
    registry_root = state_root / "legacy-event-retirements"
    if not registry_root.exists():
        return []
    if registry_root.is_symlink() or not registry_root.is_dir():
        raise ValidationError("legacy retirement registry root is invalid")
    rows: list[tuple[Path, dict[str, Any]]] = []
    claimed: dict[str, Path] = {}
    for path in sorted(registry_root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ValidationError("legacy retirement registry entry is invalid")
        try:
            document = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"legacy retirement registry is unreadable: {path.name}"
            ) from exc
        validated = _validate_legacy_retirement_document(state_root, path, document)
        for binding in validated["events"]:
            event_id = binding["event_id"]
            if event_id in claimed:
                raise IdempotencyConflict(
                    f"legacy event appears in overlapping retirement registries: {event_id}"
                )
            claimed[event_id] = path
        rows.append((path, validated))
    return rows


def load_legacy_event_retirements(state_dir: Path) -> list[dict[str, Any]]:
    """Load and fully verify every immutable legacy-event retirement receipt."""

    return [document for _, document in _load_legacy_event_retirement_pairs(state_dir)]


def _prepare_legacy_event_retirement(
    state_dir: Path,
    event_ids: list[str],
) -> tuple[Path, dict[str, Any], bool]:
    state_root = state_dir.resolve()
    normalized = _normalize_legacy_event_ids(event_ids)
    bindings = [_legacy_event_binding(state_root, event_id) for event_id in normalized]
    proposed = _new_legacy_retirement_document(bindings)
    target = (
        state_root / "legacy-event-retirements"
        / f"{proposed['retirement_id']}.json"
    )
    selected = set(normalized)
    existing_exact: dict[str, Any] | None = None
    for path, existing in _load_legacy_event_retirement_pairs(state_root):
        existing_ids = {row["event_id"] for row in existing["events"]}
        overlap = selected & existing_ids
        if not overlap:
            continue
        if path == target and existing_ids == selected:
            if {
                key: existing[key]
                for key in _legacy_retirement_core(bindings)
            } != _legacy_retirement_core(bindings):
                raise IdempotencyConflict(
                    "content-addressed legacy retirement path binds different content"
                )
            existing_exact = existing
            continue
        raise IdempotencyConflict(
            "legacy retirement overlaps an already retired event: "
            + ", ".join(sorted(overlap))
        )
    return target, existing_exact or proposed, existing_exact is not None


def _legacy_retirement_result(
    document: dict[str, Any],
    registry_path: Path,
    *,
    mode: str,
    idempotent: bool,
) -> dict[str, Any]:
    return {
        **document,
        "mode": mode,
        "registry_path": str(registry_path),
        "registry_file_sha256": (
            file_sha256(registry_path) if registry_path.is_file() else None
        ),
        "idempotent": idempotent,
    }


def preview_legacy_event_retirement(
    state_dir: Path,
    event_ids: list[str],
) -> dict[str, Any]:
    """Derive an exact retirement identity without writing any state."""

    target, document, already_applied = _prepare_legacy_event_retirement(
        state_dir, event_ids
    )
    return _legacy_retirement_result(
        document,
        target,
        mode="preview",
        idempotent=already_applied,
    )


def retire_legacy_capture_events(
    state_dir: Path,
    event_ids: list[str],
    *,
    apply: bool = False,
    authorization: str | None = None,
) -> dict[str, Any]:
    """Preview or atomically publish an exact legacy-event retirement batch."""

    target, document, already_applied = _prepare_legacy_event_retirement(
        state_dir, event_ids
    )
    if not apply:
        return _legacy_retirement_result(
            document,
            target,
            mode="preview",
            idempotent=already_applied,
        )
    if authorization != document["retirement_id"]:
        raise AuthorizationError(
            "apply requires --authorization exactly equal to preview retirement_id"
        )
    state_root = state_dir.resolve()
    lock_path = state_root / "legacy-event-retirements" / ".retirement.lock"
    with exclusive_lock(lock_path):
        target, document, already_applied = _prepare_legacy_event_retirement(
            state_root, event_ids
        )
        if authorization != document["retirement_id"]:
            raise AuthorizationError(
                "legacy event or capture receipt drifted after preview authorization"
            )
        if already_applied:
            return _legacy_retirement_result(
                document,
                target,
                mode="apply",
                idempotent=True,
            )
        if target.exists():
            raise IdempotencyConflict(
                "content-addressed legacy retirement path already exists"
            )
        atomic_write_json(target, document)
        try:
            reopened = load_json(target)
            _validate_legacy_retirement_document(state_root, target, reopened)
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            raise ValidationError(
                "legacy retirement registry failed atomic reopen verification"
            ) from exc
        if reopened != document:
            raise IdempotencyConflict(
                "legacy retirement registry changed during atomic publication"
            )
        return _legacy_retirement_result(
            reopened,
            target,
            mode="apply",
            idempotent=False,
        )


def _legacy_retirement_index_for_audit(
    state_dir: Path,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], int]:
    """Load registry state without letting corrupt entries hide source events."""

    state_root = state_dir.resolve()
    registry_root = state_root / "legacy-event-retirements"
    if not registry_root.exists():
        return {}, [], 0
    if registry_root.is_symlink() or not registry_root.is_dir():
        return {}, [{
            "registry_path": "legacy-event-retirements",
            "reason": "registry_root_invalid",
        }], 0
    index: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    valid_registry_count = 0
    for path in sorted(registry_root.glob("*.json")):
        relative_path = path.relative_to(state_root).as_posix()
        try:
            document = load_json(path)
            _validate_legacy_retirement_document(state_root, path, document)
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            errors.append(
                {
                    "registry_path": relative_path,
                    "reason": f"invalid_registry:{type(exc).__name__}:{exc}",
                }
            )
            continue
        valid_registry_count += 1
        for binding in document["events"]:
            event_id = binding["event_id"]
            if event_id in index:
                errors.append(
                    {
                        "registry_path": relative_path,
                        "reason": f"overlapping_registry:{event_id}",
                    }
                )
                continue
            index[event_id] = {
                "retirement_id": document["retirement_id"],
                "retirement_sha256": document["retirement_sha256"],
                "retirement_path": relative_path,
                "disposition": document["disposition"],
                "authority": document["authority"],
            }
    if errors:
        return {}, errors, valid_registry_count
    return index, [], valid_registry_count


def _legacy_evidence_result(
    state_dir: Path,
    event_path: Path,
    event: dict[str, Any],
) -> dict[str, Any]:
    event_id = str(event.get("event_id", ""))
    study_date = parse_iso_date(str(event.get("occurred_at", "")))
    result = {
        "event_id": event_id,
        "study_date": study_date,
        "event_path": str(event_path),
        "event_sha256": object_sha256(event),
        "status": "needs_user",
        "reason": "authoritative_complete_conversation_evidence_missing",
        "evidence_path": None,
        "migration_performed": False,
    }
    receipt_path = _capture_receipt_for_event(state_dir, event)
    if not receipt_path.is_file():
        result["reason"] = "canonical_capture_receipt_missing"
        return result
    try:
        receipt = load_json(receipt_path)
    except (OSError, ValueError, json.JSONDecodeError):
        result["reason"] = "canonical_capture_receipt_invalid"
        return result
    if (
        receipt.get("capture_id") != event_id
        or receipt.get("event_sha256") != result["event_sha256"]
        or receipt.get("formal_write_count") != 0
    ):
        result["reason"] = "canonical_capture_receipt_binding_mismatch"
        return result
    evidence_path = (
        state_dir / "legacy-package-evidence" / study_date / f"{event_id}.json"
    )
    result["evidence_path"] = str(evidence_path)
    if not evidence_path.is_file():
        return result
    try:
        evidence = load_json(evidence_path)
    except (OSError, ValueError, json.JSONDecodeError):
        result["reason"] = "authoritative_evidence_invalid_json"
        return result
    evidence_core = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    if (
        evidence.get("schema_version") != LEGACY_EVIDENCE_SCHEMA_VERSION
        or evidence.get("authority") != "authoritative_preserved_evidence"
        or evidence.get("source_event_id") != event_id
        or evidence.get("source_event_sha256") != result["event_sha256"]
        or evidence.get("evidence_sha256") != object_sha256(evidence_core)
        or evidence.get("complete_conversation") is not True
    ):
        result["reason"] = "authoritative_evidence_binding_mismatch"
        return result
    try:
        messages = _normalize_messages(evidence.get("conversation"))
        source, source_missing = _source_document(
            {"source": evidence.get("source"), "missing_fields": []}
        )
        attachments = _attachment_inputs(evidence.get("attachments", []))
    except (OSError, ValueError, ValidationError):
        result["reason"] = "authoritative_evidence_cannot_form_package"
        return result
    if source_missing:
        result["reason"] = "authoritative_source_identity_incomplete"
        return result
    declared_attachments = evidence.get("attachments", [])
    for declared, normalized in zip(declared_attachments, attachments):
        path = normalized["path"]
        if (
            declared.get("sha256") != file_sha256(path)
            or declared.get("bytes") != path.stat().st_size
        ):
            result["reason"] = "authoritative_attachment_binding_mismatch"
            return result
    if not isinstance(evidence.get("thread_ref"), str) or not evidence["thread_ref"].strip():
        result["reason"] = "authoritative_thread_ref_missing"
        return result
    if not isinstance(evidence.get("segment_key"), str) or not evidence["segment_key"].strip():
        result["reason"] = "authoritative_segment_key_missing"
        return result
    result.update(
        status="eligible-for-explicit-migration",
        reason="complete_authoritative_evidence_verified",
        message_count=len(messages),
        attachment_count=len(attachments),
        source_id=source["identity"]["source_id"],
    )
    return result


def audit_legacy_capture_events(
    state_dir: Path,
    *,
    cutoff_date: str | None = None,
) -> dict[str, Any]:
    state_root = state_dir.resolve()
    cutoff = _validate_date(cutoff_date or shanghai_current_date())
    retirement_index, registry_errors, registry_count = (
        _legacy_retirement_index_for_audit(state_root)
    )
    results: list[dict[str, Any]] = []
    for event_path in sorted((state_root / "events").glob("*/*.json")):
        try:
            event = load_json(event_path)
            study_date = parse_iso_date(str(event.get("occurred_at", "")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            results.append(
                {
                    "event_id": event_path.stem,
                    "study_date": event_path.parent.name,
                    "event_path": str(event_path),
                    "status": "needs_user",
                    "reason": f"event_unreadable:{type(exc).__name__}",
                    "actionable": False,
                    "migration_performed": False,
                }
            )
            continue
        if study_date > cutoff:
            continue
        result = _legacy_evidence_result(state_root, event_path, event)
        event_id = str(event.get("event_id", ""))
        if registry_errors:
            result.update(
                status="needs_user",
                reason="legacy_retirement_registry_invalid_or_source_drift",
                actionable=False,
            )
        elif event_id in retirement_index:
            retirement = retirement_index[event_id]
            result.update(
                status="user_removed",
                reason="future_legacy_migration_retired_by_explicit_user_request",
                actionable=False,
                **retirement,
            )
        else:
            result["actionable"] = (
                result["status"] == "eligible-for-explicit-migration"
            )
        results.append(result)
    results.sort(key=lambda row: (row["study_date"], row["event_id"]))
    counts = {
        "eligible": sum(
            row["status"] == "eligible-for-explicit-migration" for row in results
        ),
        "needs_user": sum(row["status"] == "needs_user" for row in results),
        "user_removed": sum(row["status"] == "user_removed" for row in results),
    }
    core = {
        "schema_version": LEGACY_AUDIT_SCHEMA_VERSION,
        "cutoff_date": cutoff,
        "source_event_count": len(results),
        "event_count": len(results),
        "actionable_event_count": sum(bool(row["actionable"]) for row in results),
        "counts": counts,
        "events": results,
        "retirement_registry_count": registry_count,
        "retirement_registry_errors": registry_errors,
        "migration_performed": False,
        "formal_write_count": 0,
        "background_processing": "none",
    }
    return {**core, "audit_sha256": object_sha256(core)}


def _open_transactions_by_package(state_dir: Path) -> dict[str, list[dict[str, Any]]]:
    mapping: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((state_dir / "nightly").glob("*/*/transactions/*/transaction.json")):
        try:
            transaction = load_json(path)
        except Exception:
            continue
        if transaction.get("status") not in OPEN_TRANSACTION_STATUSES:
            continue
        manifest_path = (
            state_dir / "nightly" / str(transaction.get("study_date", ""))
            / f"{transaction.get('batch_id')}.manifest.json"
        )
        if not manifest_path.is_file():
            continue
        try:
            manifest = load_json(manifest_path)
        except Exception:
            continue
        descriptor = {
            "transaction_path": str(path),
            "transaction_status": transaction.get("status"),
            "batch_id": transaction.get("batch_id"),
            "manifest_path": str(manifest_path),
        }
        for package_id in manifest.get("package_ids", []):
            mapping.setdefault(str(package_id), []).append(descriptor)
    return mapping


def _trusted_closeouts_for_package(
    state_dir: Path,
    package_id: str,
    package_sha256: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((state_dir / "receipts" / "nightly").glob("*/*.json")):
        try:
            raw = load_json(path)
        except Exception:
            continue
        ids = raw.get("package_ids", [])
        shas = raw.get("package_sha256s", [])
        if package_id not in ids:
            continue
        index = ids.index(package_id)
        if index >= len(shas) or shas[index] != package_sha256:
            continue
        try:
            receipt, manifest = validate_canonical_writer_closeout(state_dir, path)
        except (OSError, ValueError, json.JSONDecodeError, ValidationError):
            try:
                receipt, manifest = _validate_target_closeout_after_partial_cleanup(
                    state_dir,
                    path,
                    package_id,
                )
            except (OSError, ValueError, json.JSONDecodeError, ValidationError):
                continue
        rows.append(
            {
                "receipt_path": str(path),
                "receipt_id": receipt["receipt_id"],
                "batch_id": receipt["batch_id"],
                "manifest_path": str(
                    state_dir / "nightly" / manifest["study_date"]
                    / f"{manifest['batch_id']}.manifest.json"
                ),
                "status": receipt.get("status"),
                "disposition": receipt.get("package_dispositions", {}).get(package_id),
            }
        )
    return rows


def _validate_target_closeout_after_partial_cleanup(
    state_dir: Path,
    receipt_path: Path,
    package_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt_path = receipt_path.resolve(strict=True)
    receipt = load_json(receipt_path)
    receipt_id = str(receipt.get("receipt_id", ""))
    batch_id = str(receipt.get("batch_id", ""))
    if (
        receipt.get("schema_version") != "english_apply_receipt_v1"
        or receipt.get("mode") != "apply"
        or receipt.get("status") not in {"APPLIED", "PARTIAL", "NO_ACTION"}
        or package_id not in receipt.get("package_ids", [])
    ):
        raise ValidationError("target closeout receipt identity is invalid")
    manifest_matches = list((state_dir / "nightly").glob(f"*/{batch_id}.manifest.json"))
    if len(manifest_matches) != 1:
        raise ValidationError("target closeout manifest is not unique")
    manifest_path = manifest_matches[0].resolve(strict=True)
    manifest = load_json(manifest_path)
    study_date = str(manifest.get("study_date", ""))
    canonical_receipt = (
        state_dir / "receipts" / "nightly" / study_date / f"{receipt_id}.json"
    ).resolve()
    journal_path = (
        state_dir / "nightly" / study_date / f"{batch_id}.journal.jsonl"
    ).resolve()
    transaction_path = (
        state_dir / "nightly" / study_date / batch_id / "transactions"
        / receipt_id / "transaction.json"
    ).resolve()
    if (
        receipt_path != canonical_receipt
        or receipt.get("manifest_sha256") != file_sha256(manifest_path)
        or Path(str(receipt.get("journal_path", ""))).resolve() != journal_path
        or not transaction_path.is_file()
    ):
        raise ValidationError("target closeout canonical path binding mismatch")
    transaction = load_json(transaction_path)
    if (
        transaction.get("status") != "closed"
        or transaction.get("receipt_sha256") != file_sha256(receipt_path)
    ):
        raise ValidationError("target closeout transaction is not closed")
    records = _validated_journal_records(journal_path)
    close_rows = [
        row for row in records
        if row.get("receipt_id") == receipt_id and row.get("event_type") == "receipt_closed"
    ]
    if len(close_rows) != 1 or close_rows[0].get("data") != {
        "receipt_path": str(receipt_path),
        "receipt_sha256": file_sha256(receipt_path),
    }:
        raise ValidationError("target closeout receipt_closed binding mismatch")
    filtered_results = [
        result
        for result in receipt.get("action_results", [])
        if any(
            isinstance(ref, dict) and ref.get("package_id") == package_id
            for ref in result.get("evidence_refs", [])
        )
    ]
    if not filtered_results:
        raise ValidationError("target closeout has no package evidence")
    package_root_overrides: dict[str, Path] = {}
    # One reviewed action may bind both the original learning and its web supplement.
    # Reopen only those exact dependencies when either package has been archived.
    referenced_ids = sorted({ref["package_id"] for result in filtered_results
                             for ref in result.get("evidence_refs", []) if isinstance(ref, dict)})
    for dependency_id in referenced_ids:
        package_id = dependency_id
        package_record = next(
            (
                row
                for row in manifest.get("package_documents", [])
                if isinstance(row, dict) and row.get("package_id") == package_id
            ),
            None,
        )
        if package_record is None:
            raise ValidationError("target closeout manifest lacks package record")
        local_package_root = Path(str(package_record.get("path", "")))
        if not local_package_root.is_dir():
            archive_receipt_path = (
                state_dir
                / "receipts"
                / "archive"
                / study_date
                / f"{batch_id}.json"
            )
            archive_root_value: str | None = None
            archive_relative_value: str | None = None
            try:
                archive_receipt = load_json(archive_receipt_path)
                archived_result = next(
                    (
                        row
                        for row in archive_receipt.get("package_results", [])
                        if isinstance(row, dict)
                        and row.get("package_id") == package_id
                        and row.get("package_sha256")
                        == package_record.get("package_sha256")
                    ),
                    None,
                )
                if (
                    archive_receipt.get("schema_version")
                    == "english_package_archive_receipt_v2"
                    and archive_receipt.get("status") == "PASS"
                    and archive_receipt.get("batch_id") == batch_id
                    and archive_receipt.get("manifest_sha256")
                    == file_sha256(manifest_path)
                    and archive_receipt.get("writer_receipt_path")
                    == str(receipt_path)
                    and archive_receipt.get("writer_receipt_sha256")
                    == file_sha256(receipt_path)
                    and archived_result is not None
                ):
                    archive_root_value = str(archive_receipt.get("archive_root", ""))
                    archive_relative_value = str(
                        archived_result.get("archive_relative_path", "")
                    )
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            if not archive_root_value or not archive_relative_value:
                cleanup_intent_path = (
                    state_dir
                    / "cleanup-intents"
                    / study_date
                    / f"{package_id}.json"
                )
                if not cleanup_intent_path.is_file():
                    raise ValidationError(
                        "target closeout package is absent without archive recovery evidence"
                    )
                cleanup_intent = load_json(cleanup_intent_path)
                if (
                    cleanup_intent.get("schema_version")
                    != "english_archive_cleanup_intent_v1"
                    or cleanup_intent.get("package_id") != package_id
                    or cleanup_intent.get("package_sha256")
                    != package_record.get("package_sha256")
                    or cleanup_intent.get("local_package_path")
                    != str(local_package_root)
                    or cleanup_intent.get("cleanup_authorized") is not True
                ):
                    raise ValidationError(
                        "target closeout cleanup intent binding mismatch"
                    )
                archive_root_value = str(cleanup_intent.get("archive_root", ""))
                archive_relative_value = str(
                    cleanup_intent.get("archive_relative_path", "")
                )
            archive_root = Path(archive_root_value).resolve(strict=True)
            archive_relative_path = Path(archive_relative_value)
            if (
                archive_relative_path.is_absolute()
                or ".." in archive_relative_path.parts
            ):
                raise ValidationError("target closeout archive path is unsafe")
            archived_root = (archive_root / archive_relative_path).resolve(strict=True)
            try:
                archived_root.relative_to(archive_root)
            except ValueError as exc:
                raise ValidationError(
                    "target closeout archive path escapes archive root"
                ) from exc
            archived_manifest = validate_conversation_package(archived_root)
            if (
                archived_manifest.get("package_id") != package_id
                or archived_manifest.get("package_canonical_sha256")
                != package_record.get("package_sha256")
            ):
                raise ValidationError("target closeout archived package drift")
            package_root_overrides[package_id] = archived_root
    validate_receipt_resolved_evidence(
        {**receipt, "action_results": filtered_results},
        manifest,
        package_root_overrides=package_root_overrides,
    )
    return receipt, manifest


def _trusted_consumed_pointer(
    state_dir: Path,
    study_date: str,
    package_id: str,
    package_sha256: str | None = None,
) -> dict[str, Any] | None:
    state_root = state_dir.resolve()
    pointer_path = (
        state_root / "archive-pointers" / study_date / f"{package_id}.json"
    )
    cleanup_path = (
        state_root
        / "receipts"
        / "archive-cleanup"
        / study_date
        / f"{package_id}.json"
    )
    cleanup_intent_path = (
        state_root / "cleanup-intents" / study_date / f"{package_id}.json"
    )
    local_root = state_root / "packages" / study_date / package_id
    required_files = (pointer_path, cleanup_path, cleanup_intent_path)
    if local_root.exists() or not all(
        path.is_file() and not path.is_symlink() for path in required_files
    ):
        return None
    try:
        pointer = load_json(pointer_path)
        cleanup = load_json(cleanup_path)
        cleanup_intent = load_json(cleanup_intent_path)
        archive_receipt_path = Path(
            str(pointer.get("archive_receipt_path", ""))
        ).resolve(strict=True)
        archive_receipt = load_json(archive_receipt_path)
        locator_path = Path(
            str(pointer.get("archive_locator_path", ""))
        ).resolve(strict=True)
        archive_intent_path = Path(
            str(archive_receipt.get("archive_intent_path", ""))
        ).resolve(strict=True)
        archive_intent = load_json(archive_intent_path)
        writer_receipt_path = Path(
            str(archive_receipt.get("writer_receipt_path", ""))
        ).resolve(strict=True)
        archive_root = Path(
            str(archive_receipt.get("archive_root", ""))
        ).resolve(strict=True)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    digest = str(pointer.get("package_sha256", ""))
    package_results = archive_receipt.get("package_results", [])
    matches = [
        row
        for row in package_results
            if isinstance(row, dict)
            and row.get("package_id") == package_id
            and row.get("package_sha256") == digest
    ] if isinstance(package_results, list) else []
    package_result = matches[0] if len(matches) == 1 else None
    display_closure = (
        package_result.get("display_asset_closure")
        if isinstance(package_result, dict)
        else None
    )
    display_binding = {
        "display_closure_receipt_id": display_closure.get("receipt_id"),
        "display_closure_receipt_path": display_closure.get("receipt_path"),
        "display_closure_receipt_sha256": display_closure.get("receipt_sha256"),
        "formal_reference_scan_sha256": display_closure.get("formal_reference_scan_sha256"),
        "stable_asset_count": display_closure.get("stable_asset_count"),
        "no_display": display_closure.get("no_display"),
    } if isinstance(display_closure, dict) else {}
    support_closure = (
        package_result.get("sentence_support_closure")
        if isinstance(package_result, dict)
        else None
    )
    support_keys = {
        "support_refresh_receipt_id",
        "support_refresh_receipt_path",
        "support_refresh_receipt_sha256",
        "index_snapshot_path",
        "index_snapshot_sha256",
        "support_key",
        "record_kind",
        "record_path",
        "record_sha256",
        "old_record_sha256",
        "new_record_sha256",
        "published_record_path",
        "published_record_sha256",
        "record_bytes",
        "resolved_formal_ids",
        "formal_locator_policy",
        "dependency_locator_sha256",
    }
    support_binding = (
        {key: support_closure.get(key) for key in sorted(support_keys)}
        if isinstance(support_closure, dict)
        else {}
    )
    batch_id = str(archive_receipt.get("batch_id", ""))
    canonical_manifest_path = (
        state_root / "nightly" / study_date / f"{batch_id}.manifest.json"
    )
    try:
        support_required_for_batch = sentence_support_required(
            load_json(canonical_manifest_path)
        )
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return None
    expected_support_status = (
        "closed" if support_required_for_batch else "legacy_support_not_required"
    )
    archive_support_status = archive_receipt.get("sentence_support_status")
    intent_support_status = archive_intent.get("sentence_support_status")
    if not support_required_for_batch:
        archive_support_status = archive_support_status or expected_support_status
        intent_support_status = intent_support_status or expected_support_status
    subject_root = str(archive_receipt.get("subject_relative_root", ""))
    expected_archive_relative_path = (
        f"{subject_root}/{study_date}/{package_id}" if subject_root else ""
    )
    expected_archive_receipt_path = (
        state_root / "receipts" / "archive" / study_date / f"{batch_id}.json"
    ).resolve()
    expected_archive_intent_path = (
        state_root / "archive-intents" / study_date / f"{batch_id}.json"
    ).resolve()
    expected_locator_path = (
        state_root.parent / "wiki" / "raw_archives" / f"{package_id}.md"
    ).resolve()
    archive_receipt_core = {
        key: value
        for key, value in archive_receipt.items()
        if key != "receipt_id"
    }
    completed_closeouts = [
        row
        for row in _trusted_closeouts_for_package(state_root, package_id, digest)
        if row.get("disposition") == "completed"
        and Path(str(row.get("receipt_path", ""))).resolve()
        == writer_receipt_path
        and row.get("batch_id") == batch_id
    ]
    if (
        pointer.get("schema_version") != "english_archive_pointer_v1"
        or pointer.get("package_id") != package_id
        or (package_sha256 is not None and digest != package_sha256)
        or pointer.get("archive_root") != str(archive_root)
        or pointer.get("archive_relative_path") != expected_archive_relative_path
        or pointer.get("archive_receipt_path") != str(archive_receipt_path)
        or pointer.get("archive_locator_path") != str(locator_path)
        or cleanup.get("schema_version")
        != "english_archive_cleanup_receipt_v1"
        or cleanup.get("status") != "PASS"
        or cleanup.get("package_id") != package_id
        or cleanup.get("package_sha256") != digest
        or cleanup.get("archive_root") != str(archive_root)
        or cleanup.get("local_package_removed") is not True
        or cleanup.get("pointer_path") != str(pointer_path)
        or cleanup.get("archive_receipt_path") != str(archive_receipt_path)
        or cleanup.get("locator_path") != str(locator_path)
        or not display_binding
        or any(
            pointer.get(key) != value
            or cleanup.get(key) != value
            or cleanup_intent.get(key) != value
            for key, value in display_binding.items()
        )
        or (
            support_required_for_batch
            and (
                not support_binding
                or any(
                    pointer.get(key) != value
                    or cleanup.get(key) != value
                    or cleanup_intent.get(key) != value
                    for key, value in support_binding.items()
                )
            )
        )
        or pointer.get("pending_component") != "cleanup"
        or cleanup_intent.get("pending_component") != "cleanup"
        or cleanup.get("pending_component") is not None
        or cleanup_intent.get("schema_version")
        != "english_archive_cleanup_intent_v1"
        or cleanup_intent.get("package_id") != package_id
        or cleanup_intent.get("package_sha256") != digest
        or cleanup_intent.get("local_package_path") != str(local_root)
        or cleanup_intent.get("archive_root") != str(archive_root)
        or cleanup_intent.get("archive_relative_path")
        != expected_archive_relative_path
        or cleanup_intent.get("archive_receipt_path")
        != str(archive_receipt_path)
        or cleanup_intent.get("archive_receipt_sha256")
        != file_sha256(archive_receipt_path)
        or cleanup_intent.get("locator_path") != str(locator_path)
        or cleanup_intent.get("locator_sha256") != file_sha256(locator_path)
        or cleanup_intent.get("cleanup_authorized") is not True
        or archive_receipt_path != expected_archive_receipt_path
        or archive_receipt.get("schema_version")
        != "english_package_archive_receipt_v2"
        or archive_receipt.get("status") != "PASS"
        or archive_receipt.get("receipt_id")
        != "EN-ARCHIVE-" + object_sha256(archive_receipt_core)[:16].upper()
        or archive_receipt.get("formal_write_count") != 0
        or archive_receipt.get("local_cleanup_authorized") is not True
        or archive_receipt.get("archive_root") != str(archive_root)
        or display_closure not in archive_receipt.get("display_closures", [])
        or archive_support_status != expected_support_status
        or intent_support_status != archive_support_status
        or (
            support_required_for_batch
            and support_closure
            not in archive_receipt.get("sentence_support_closures", [])
        )
        or archive_receipt.get("sentence_support_refresh")
        != archive_intent.get("sentence_support_refresh")
        or (
            support_required_for_batch
            and support_closure
            not in archive_intent.get("sentence_support_closures", [])
        )
        or archive_receipt.get("archive_intent_sha256")
        != file_sha256(archive_intent_path)
        or archive_receipt.get("writer_receipt_sha256")
        != file_sha256(writer_receipt_path)
        or package_result is None
        or package_result.get("archive_root") != str(archive_root)
        or package_result.get("archive_relative_path")
        != expected_archive_relative_path
        or package_result.get("locator_path") != str(locator_path)
        or package_result.get("verification") != "PASS"
        or package_result.get("local_path") != str(local_root)
        or package_id in archive_receipt.get("retained_package_ids", [])
        or archive_intent_path != expected_archive_intent_path
        or archive_intent.get("schema_version") != "english_archive_intent_v1"
        or archive_intent.get("batch_id") != batch_id
        or archive_intent.get("manifest_sha256")
        != archive_receipt.get("manifest_sha256")
        or archive_intent.get("writer_receipt_sha256")
        != archive_receipt.get("writer_receipt_sha256")
        or display_closure not in archive_intent.get("display_closures", [])
        or package_id not in archive_intent.get("completed_package_ids", [])
        or package_id in archive_intent.get("retained_package_ids", [])
        or locator_path != expected_locator_path
        or not completed_closeouts
        or archive_receipt.get("manifest_sha256")
        != file_sha256(Path(completed_closeouts[0]["manifest_path"]))
    ):
        return None
    try:
        archive_relative_path = Path(expected_archive_relative_path)
        if (
            archive_relative_path.is_absolute()
            or ".." in archive_relative_path.parts
        ):
            return None
        archived_root = (archive_root / archive_relative_path).resolve(strict=True)
        archived_root.relative_to(archive_root)
        archived_manifest = validate_conversation_package(archived_root)
        if (
            archived_manifest.get("package_id") != package_id
            or archived_manifest.get("package_canonical_sha256") != digest
            or file_sha256(archived_root / "manifest.json")
            != package_result.get("archive_manifest_sha256")
        ):
            return None
        locator_verification = validate_archive_locator_binding(
            locator_path,
            package_id=package_id,
            formal_ids=package_result.get("formal_ids", []),
            archive_relative_path=expected_archive_relative_path,
            manifest_sha256=package_result.get("archive_manifest_sha256", ""),
            package_sha256=digest,
            verified_at=str(archive_receipt.get("verified_at", "")),
            retrieval_keys=package_result.get("retrieval_keys", []),
            expected_locator_sha256=package_result.get("locator_sha256"),
        )
        display_verification = validate_display_asset_closure(
            state_root,
            state_root.parent,
            archived_root,
            Path(str(display_closure.get("receipt_path", ""))),
        )
        if display_verification != display_closure:
            return None
        if support_required_for_batch:
            support_receipt_file = Path(
                str(support_closure.get("support_refresh_receipt_path", ""))
            ).resolve(strict=True)
            support_receipt, validated_support_closure = validate_sentence_support_refresh(
                state_root,
                manifest_path=Path(completed_closeouts[0]["manifest_path"]),
                writer_receipt_path=writer_receipt_path,
                receipt_path=support_receipt_file,
                package_id=package_id,
            )
            if (
                validated_support_closure != support_closure
                or support_receipt.get("receipt_id")
                != support_binding["support_refresh_receipt_id"]
                or file_sha256(support_receipt_file)
                != support_binding["support_refresh_receipt_sha256"]
            ):
                return None
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return None
    return {
        "pointer_path": str(pointer_path),
        "cleanup_receipt_path": str(cleanup_path),
        "archive_receipt_path": str(archive_receipt_path),
        "locator_path": locator_verification["locator_path"],
        "locator_sha256": locator_verification["locator_sha256"],
        "package_sha256": digest,
        "sentence_support_status": (
            expected_support_status
        ),
    }


def _local_package_record(
    state_dir: Path,
    root: Path,
    open_transactions: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    study_date = root.parent.name
    package_id = root.name
    base = {
        "package_id": package_id,
        "study_date": study_date,
        "path": str(root),
        "status": "failed",
        "reason": None,
        "package_sha256": None,
        "manifest_sha256": None,
        "trusted_closeouts": [],
        "recovery": open_transactions.get(package_id, []),
    }
    try:
        manifest = validate_conversation_package(root)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        return {**base, "reason": f"damaged_package:{type(exc).__name__}:{exc}"}
    package_sha = manifest["package_canonical_sha256"]
    base.update(
        package_sha256=package_sha,
        manifest_sha256=file_sha256(root / "manifest.json"),
    )
    if base["recovery"]:
        return {**base, "status": "recovery_required", "reason": "open_formal_transaction"}
    closeouts = _trusted_closeouts_for_package(state_dir, package_id, package_sha)
    base["trusted_closeouts"] = closeouts
    completed = [row for row in closeouts if row["disposition"] == "completed"]
    if completed:
        consumed = _trusted_consumed_pointer(state_dir, study_date, package_id, package_sha)
        if consumed:
            return {**base, "status": "already_consumed", "reason": "trusted_archive_cleanup", "consumed": consumed}
        return {
            **base,
            "status": "archive_pending",
            "reason": "trusted_formal_closeout_local_package_retained",
            "pending_component": _pending_component_for_closeout(
                state_dir,
                study_date,
                package_id,
                completed[-1],
            ),
        }
    if any(row["disposition"] == "needs_user" for row in closeouts):
        return {**base, "status": "needs_user", "reason": "trusted_partial_closeout_needs_user"}
    from .web_review_gate import managed_package
    if managed_package(state_dir, manifest):
        return {**base, "status": "waiting_web_review", "reason": "web_review_route"}
    return {**base, "status": "pending", "reason": "no_trusted_formal_terminal"}


def _pending_component_for_closeout(
    state_dir: Path,
    study_date: str,
    package_id: str,
    closeout: dict[str, Any],
) -> str:
    state_root = state_dir.resolve()
    batch_id = str(closeout.get("batch_id") or "")
    manifest_path = Path(str(closeout.get("manifest_path") or ""))
    writer_receipt_path = Path(str(closeout.get("receipt_path") or ""))
    try:
        support_required = sentence_support_required(load_json(manifest_path))
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return "support_refresh"
    if support_required:
        support_path = support_receipt_path(state_root, study_date, batch_id)
        if not support_path.is_file():
            return "support_refresh"
        try:
            validate_sentence_support_refresh(
                state_root,
                manifest_path=manifest_path,
                writer_receipt_path=writer_receipt_path,
                receipt_path=support_path,
                package_id=package_id,
            )
        except (OSError, ValueError, json.JSONDecodeError, ValidationError):
            return "support_refresh"
    display_receipt = (
        state_root / "receipts" / "display-assets" / study_date / f"{package_id}.json"
    )
    if not display_receipt.is_file():
        return "display_assets"
    archive_receipt_path = state_root / "receipts" / "archive" / study_date / f"{batch_id}.json"
    if not archive_receipt_path.is_file():
        return "archive"
    try:
        archive_receipt = load_json(archive_receipt_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return "archive"
    package_result = next(
        (
            row for row in archive_receipt.get("package_results", [])
            if isinstance(row, dict) and row.get("package_id") == package_id
        ),
        None,
    )
    if package_result is None:
        return "archive"
    locator = Path(str(package_result.get("locator_path") or ""))
    if not locator.is_file():
        return "locator"
    return "cleanup"


def _all_local_package_rows(state_dir: Path) -> list[dict[str, Any]]:
    open_transactions = _open_transactions_by_package(state_dir)
    rows: list[dict[str, Any]] = []
    for root in sorted((state_dir / "packages").glob("*/*")):
        if not root.is_dir() or not root.name.startswith(_PACKAGE_ID_PREFIX):
            continue
        rows.append(_local_package_record(state_dir, root, open_transactions))
    return rows


def _postformal_support_debt_rows(
    state_dir: Path,
    *,
    existing_ids: set[str],
) -> list[dict[str, Any]]:
    """Discover committed support debt even when no local package is enumerable.

    New manifests make sentence-support an explicit postformal closure.  This
    bounded scan covers immutable nightly manifests and canonical closeouts,
    not Obsidian or the archive volume.  A valid support receipt removes the
    debt; a missing or drifted receipt keeps the package visible to planning.
    """

    state_root = state_dir.resolve()
    rows: list[dict[str, Any]] = []
    for manifest_path in sorted((state_root / "nightly").glob("*/*.manifest.json")):
        try:
            manifest = load_json(manifest_path)
            if not sentence_support_required(manifest):
                continue
        except (OSError, ValueError, json.JSONDecodeError, ValidationError):
            continue
        study_date = str(manifest.get("study_date", ""))
        batch_id = str(manifest.get("batch_id", ""))
        for package_record in manifest.get("package_documents", []):
            if not isinstance(package_record, dict):
                continue
            package_id = str(package_record.get("package_id", ""))
            package_sha = str(package_record.get("package_sha256", ""))
            if package_id in existing_ids or not package_id or not _SHA256_RE.fullmatch(package_sha):
                continue
            closeouts = _trusted_closeouts_for_package(
                state_root, package_id, package_sha
            )
            completed = [
                row for row in closeouts if row.get("disposition") == "completed"
            ]
            if not completed:
                continue
            closeout = completed[-1]
            # A package can occur in unused freezes or an earlier batch that
            # deferred it. Its committed closeout owns the required support
            # receipt; an unrelated freeze cannot create postformal debt.
            if (
                closeout.get("batch_id") != batch_id
                or Path(str(closeout.get("manifest_path", ""))).resolve()
                != manifest_path.resolve()
            ):
                continue
            receipt_path = support_receipt_path(state_root, study_date, batch_id)
            try:
                validate_sentence_support_refresh(
                    state_root,
                    manifest_path=manifest_path,
                    writer_receipt_path=Path(closeout["receipt_path"]),
                    receipt_path=receipt_path,
                    package_id=package_id,
                )
                continue
            except (OSError, ValueError, json.JSONDecodeError, ValidationError):
                pass
            rows.append(
                {
                    "package_id": package_id,
                    "study_date": study_date,
                    "path": None,
                    "status": "archive_pending",
                    "reason": "trusted_formal_closeout_sentence_support_incomplete",
                    "pending_component": "support_refresh",
                    "package_sha256": package_sha,
                    "manifest_sha256": package_record.get("manifest_sha256"),
                    "trusted_closeouts": completed,
                    "recovery": [],
                }
            )
    rows.sort(key=lambda row: (row["study_date"], row["package_id"]))
    return rows


def _subset_consumed_rows(
    state_dir: Path,
    package_ids: set[str],
    existing_ids: set[str],
    cutoff_date: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pointer_path in sorted((state_dir / "archive-pointers").glob("*/*.json")):
        package_id = pointer_path.stem
        study_date = pointer_path.parent.name
        if package_id not in package_ids or package_id in existing_ids or study_date > cutoff_date:
            continue
        consumed = _trusted_consumed_pointer(state_dir, study_date, package_id)
        if consumed:
            rows.append(
                {
                    "package_id": package_id,
                    "study_date": study_date,
                    "path": None,
                    "status": "already_consumed",
                    "reason": "trusted_archive_cleanup",
                    "package_sha256": consumed["package_sha256"],
                    "manifest_sha256": None,
                    "trusted_closeouts": [],
                    "recovery": [],
                    "consumed": consumed,
                }
            )
    return rows


def build_backlog_plan(
    state_dir: Path,
    repo_root: Path,
    *,
    cutoff_date: str | None = None,
    only_today: bool = False,
    package_ids: list[str] | None = None,
    include_legacy_audit: bool = True,
    persist: bool = True,
    output: Path | None = None,
    local_review_id: str | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    del repo_root  # Reserved for future formal-snapshot bindings; planning is read-only.
    state_root = state_dir.resolve()
    cutoff = _validate_date(cutoff_date or shanghai_current_date())
    subset = set(package_ids or [])
    if local_review_id:
        from .local_review import local_authorization
        allowed = local_authorization(state_root, local_review_id)
        if subset != {p["package_id"] for p in allowed["packages"]}:
            raise ValidationError("native local backlog plan requires its exact authorized package set")
    if any(_PACKAGE_ID_RE.fullmatch(item) is None for item in subset):
        raise ValidationError("package subset contains an invalid English package id")
    all_rows = _all_local_package_rows(state_root)
    all_rows.extend(
        _postformal_support_debt_rows(
            state_root,
            existing_ids={row["package_id"] for row in all_rows},
        )
    )
    future_excluded = [
        {"package_id": row["package_id"], "study_date": row["study_date"]}
        for row in all_rows
        if row["study_date"] > cutoff
        and (not subset or row["package_id"] in subset)
    ]
    rows = [row for row in all_rows if row["study_date"] <= cutoff]
    if only_today:
        rows = [row for row in rows if row["study_date"] == cutoff]
    if subset:
        rows = [row for row in rows if row["package_id"] in subset]
        rows.extend(
            _subset_consumed_rows(
                state_root,
                subset,
                {row["package_id"] for row in rows},
                cutoff,
            )
        )
        found = {row["package_id"] for row in rows}
        future_ids = {row["package_id"] for row in future_excluded}
        for package_id in sorted(subset - found - future_ids):
            match = _PACKAGE_ID_RE.fullmatch(package_id)
            assert match is not None
            token = match.group(1)
            study_date = f"{token[:4]}-{token[4:6]}-{token[6:]}"
            if study_date > cutoff:
                future_excluded.append(
                    {"package_id": package_id, "study_date": study_date}
                )
                continue
            rows.append(
                {
                    "package_id": package_id,
                    "study_date": study_date,
                    "path": None,
                    "status": "failed",
                    "reason": "exact_subset_package_not_found",
                    "package_sha256": None,
                    "manifest_sha256": None,
                    "trusted_closeouts": [],
                    "recovery": [],
                }
            )
    rows.sort(key=lambda row: (row["study_date"], row["package_id"]))
    if local_review_id:
        from .local_review import local_pending_record
        rows = [local_pending_record(state_root, row, local_review_id) for row in rows]
    selected = [
        row for row in rows
        if row["status"] != "waiting_web_review"
        and (row["status"] != "already_consumed" or bool(subset))
    ]
    daily: list[dict[str, Any]] = []
    for study_date in sorted({row["study_date"] for row in selected}):
        daily.append(
            {
                "study_date": study_date,
                "package_ids": [
                    row["package_id"] for row in selected if row["study_date"] == study_date
                ],
                "package_statuses": {
                    row["package_id"]: row["status"]
                    for row in selected if row["study_date"] == study_date
                },
            }
        )
    legacy = (
        audit_legacy_capture_events(state_root, cutoff_date=cutoff)
        if include_legacy_audit
        else None
    )
    core = {
        "schema_version": BACKLOG_PLAN_SCHEMA_VERSION,
        "status": "NOOP" if not selected else "READY",
        "cutoff_date": cutoff,
        "timezone": "Asia/Shanghai",
        "selection": {
            "mode": "package_subset" if subset else "only_today" if only_today else "through_cutoff",
            "only_today": only_today,
            "package_ids": sorted(subset),
        },
        "daily_batches": daily,
        "packages": selected,
        **({"waiting_web_review": [row for row in rows if row["status"] == "waiting_web_review"]}
           if any(row["status"] == "waiting_web_review" for row in rows) else {}),
        "future_excluded": sorted(future_excluded, key=lambda row: (row["study_date"], row["package_id"])),
        "legacy_reachability_audit": legacy,
        "formal_write_count": 0,
        "background_processing": "none",
        **({"local_review_id": local_review_id} if local_review_id else {}),
    }
    canonical_sha = object_sha256(core)
    plan_id = f"EN-BACKLOG-{cutoff.replace('-', '')}-{canonical_sha[:16].upper()}"
    document = {
        **core,
        "plan_id": plan_id,
        "plan_canonical_sha256": canonical_sha,
        "created_at": utc_now(),
    }
    if not persist:
        return None, document
    target = output or state_root / "backlog" / "plans" / cutoff / f"{plan_id}.json"
    if target.exists():
        existing = load_json(target)
        if _plan_core(existing) != core or existing.get("plan_canonical_sha256") != canonical_sha:
            raise IdempotencyConflict("canonical backlog plan path binds different content")
        return target, existing
    atomic_write_json(target, document)
    reopened = load_json(target)
    if _plan_core(reopened) != core:
        raise ValidationError("canonical backlog plan failed reopen verification")
    return target, reopened


def _plan_core(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if key not in {"plan_id", "plan_canonical_sha256", "created_at", "plan_path", "plan_file_sha256"}
    }


def load_backlog_plan(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    plan = load_json(resolved)
    if (
        plan.get("schema_version") != BACKLOG_PLAN_SCHEMA_VERSION
        or object_sha256(_plan_core(plan)) != plan.get("plan_canonical_sha256")
    ):
        raise ValidationError("backlog plan canonical SHA mismatch")
    return {**plan, "plan_path": str(resolved), "plan_file_sha256": file_sha256(resolved)}


def _existing_closeout_for_batch(
    state_dir: Path,
    manifest: dict[str, Any],
    action_set_id: str | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    for path in sorted((state_dir / "receipts" / "nightly" / manifest["study_date"]).glob("*.json")):
        try:
            receipt, _ = validate_canonical_writer_closeout(
                state_dir,
                path,
                expected_manifest_path=(
                    state_dir / "nightly" / manifest["study_date"]
                    / f"{manifest['batch_id']}.manifest.json"
                ),
            )
        except (OSError, ValueError, json.JSONDecodeError, ValidationError):
            continue
        if (
            receipt.get("batch_id") == manifest["batch_id"]
            and receipt.get("mode") == "apply"
            and receipt.get("status") in {"APPLIED", "PARTIAL", "NO_ACTION"}
            and (action_set_id is None or receipt.get("action_set_id") == action_set_id)
        ):
            return path, receipt
    return None


def _closeout_for_archive_pending(
    state_dir: Path,
    row: dict[str, Any],
) -> tuple[Path, Path] | None:
    for closeout in reversed(row.get("trusted_closeouts", [])):
        if closeout.get("disposition") != "completed":
            continue
        return Path(closeout["manifest_path"]), Path(closeout["receipt_path"])
    return None


ActionsProvider = Callable[[str, Path, dict[str, Any]], Path | None]
SupportProvider = Callable[
    [str, Path, dict[str, Any], Path, dict[str, Any]],
    Path | None,
]


def _resolve_support_proposal(
    *,
    study_date: str,
    manifest_path: Path,
    manifest: dict[str, Any],
    writer_receipt_path: Path,
    writer_receipt: dict[str, Any],
    support_provider: SupportProvider | None,
    support_dir: Path | None,
    actions_path: Path | None = None,
) -> Path | None:
    if support_provider is not None:
        provided = support_provider(
            study_date,
            manifest_path,
            manifest,
            writer_receipt_path,
            writer_receipt,
        )
        return provided.resolve(strict=True) if provided is not None else None
    candidates: list[Path] = []
    if support_dir is not None:
        candidates.extend(
            [
                support_dir / f"{manifest['batch_id']}.support.json",
                support_dir / f"{manifest['batch_id']}.sentence-support.json",
                support_dir / f"{manifest['batch_id']}.json",
            ]
        )
    if actions_path is not None:
        name = actions_path.name
        if name.endswith(".actions.json"):
            candidates.append(actions_path.with_name(name[: -len(".actions.json")] + ".support.json"))
        candidates.append(actions_path.with_name(f"{manifest['batch_id']}.support.json"))
    existing: list[Path] = []
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
            if resolved not in existing:
                existing.append(resolved)
    if not existing:
        return None
    if len({file_sha256(path) for path in existing}) != 1:
        raise IdempotencyConflict("multiple sentence-support proposals bind different bytes")
    return existing[0]


def _ensure_support_refresh(
    state_root: Path,
    repo: Path,
    *,
    study_date: str,
    manifest_path: Path,
    writer_receipt_path: Path,
    support_provider: SupportProvider | None,
    support_dir: Path | None,
    actions_path: Path | None = None,
    proposal_path_override: Path | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    manifest = load_json(manifest_path)
    writer_receipt = load_json(writer_receipt_path)
    if not sentence_support_required(manifest):
        return None, {
            "status": "legacy_support_not_required",
            "batch_id": manifest.get("batch_id"),
        }
    receipt_path = support_receipt_path(
        state_root, study_date, str(manifest.get("batch_id", ""))
    )
    if receipt_path.is_file():
        receipt, _ = validate_sentence_support_refresh(
            state_root,
            manifest_path=manifest_path,
            writer_receipt_path=writer_receipt_path,
            receipt_path=receipt_path,
        )
        return receipt_path, receipt
    preflight_path = Path(str(writer_receipt.get("support_preflight_receipt_path") or ""))
    if (preflight_path.is_symlink() or not preflight_path.is_file()
            or file_sha256(preflight_path) != writer_receipt.get("support_preflight_receipt_sha256")):
        raise ValidationError("writer-bound sentence-support preflight receipt is unavailable")
    preflight = load_json(preflight_path)
    proposal_path = (state_root / str(preflight.get("proposal_path") or "")).resolve(strict=True)
    if file_sha256(proposal_path) != preflight.get("proposal_sha256"):
        raise ValidationError("writer-bound sentence-support proposal drift")
    if proposal_path_override is not None and file_sha256(proposal_path_override.resolve(strict=True)) not in {
        preflight.get("proposal_input_sha256"), preflight.get("proposal_sha256"),
    }:
        raise IdempotencyConflict("post-apply sentence-support proposal differs from preflight")
    return refresh_sentence_support(
        state_root,
        repo,
        manifest_path=manifest_path,
        writer_receipt_path=writer_receipt_path,
        proposal_path=proposal_path,
    )


def execute_backlog_plan(
    state_dir: Path,
    repo_root: Path,
    *,
    plan_path: Path,
    actions_provider: ActionsProvider | None = None,
    actions_dir: Path | None = None,
    support_provider: SupportProvider | None = None,
    support_dir: Path | None = None,
    archive_contract: VolumeContract = VolumeContract(),
    archive: bool = True,
    apply: bool = False,
    authorization: str | None = None,
) -> dict[str, Any]:
    state_root = state_dir.resolve()
    repo = repo_root.resolve()
    plan = load_backlog_plan(plan_path)
    if apply and authorization != plan["plan_id"]:
        raise ValidationError("run-backlog --apply requires --authorization equal to plan_id")
    apply_invocations: list[str] = []
    reused_closeouts: list[str] = []
    day_results: list[dict[str, Any]] = []
    already_at_start = {
        row["package_id"]
        for row in plan["packages"]
        if _current_state_for_planned_row(state_root, row)["status"] == "already_consumed"
    }
    rows_by_id = {row["package_id"]: row for row in plan["packages"]}
    for day in plan["daily_batches"]:
        study_date = day["study_date"]
        selected_ids = set(day["package_ids"])
        result: dict[str, Any] = {
            "study_date": study_date,
            "package_ids": sorted(selected_ids),
            "recovery": None,
            "manifest_path": None,
            "writer_receipt_path": None,
            "writer_status": None,
            "support_refresh_receipt_path": None,
            "support_status": None,
            "support_preflight": None,
            "archive_status": None,
            "errors": [],
        }
        current_rows = {
            package_id: _current_state_for_planned_row(state_root, rows_by_id[package_id])
            for package_id in selected_ids
        }
        result["would_freeze"] = sorted(
            package_id
            for package_id, row in current_rows.items()
            if row["status"] == "pending"
        )
        result["would_resume"] = sorted(
            package_id
            for package_id, row in current_rows.items()
            if row["status"] in {"recovery_required", "archive_pending"}
        )
        if not apply:
            day_results.append(result)
            continue
        recovery_rows = [row for row in current_rows.values() if row["status"] == "recovery_required"]
        if recovery_rows and apply:
            try:
                transaction_paths = sorted(
                    {
                        item["transaction_path"]
                        for row in recovery_rows
                        for item in row.get("recovery", [])
                    }
                )
                result["recovery"] = [
                    recover_nightly(
                        state_root,
                        repo,
                        transaction_path=Path(transaction_path),
                    )
                    for transaction_path in transaction_paths
                ]
            except Exception as exc:
                result["errors"].append(f"recovery_failed:{type(exc).__name__}:{exc}")
                day_results.append(result)
                continue
            current_rows = {
                package_id: _current_state_for_planned_row(
                    state_root, rows_by_id[package_id]
                )
                for package_id in selected_ids
            }
            recovery_rows = [
                row
                for row in current_rows.values()
                if row["status"] == "recovery_required"
            ]
        if recovery_rows:
            result["errors"].append("recovery_required")
            day_results.append(result)
            continue
        for row in current_rows.values():
            if row["status"] != "archive_pending" or not apply:
                continue
            binding = _closeout_for_archive_pending(state_root, row)
            if binding is None:
                result["errors"].append(f"archive_closeout_missing:{row['package_id']}")
                continue
            try:
                support_path, support_receipt = _ensure_support_refresh(
                    state_root,
                    repo,
                    study_date=study_date,
                    manifest_path=binding[0],
                    writer_receipt_path=binding[1],
                    support_provider=support_provider,
                    support_dir=support_dir,
                )
                result["support_refresh_receipt_path"] = (
                    str(support_path) if support_path is not None else None
                )
                result["support_status"] = support_receipt["status"]
            except Exception as exc:
                result["errors"].append(
                    f"support_refresh_failed:{type(exc).__name__}:{exc}"
                )
                continue
            if archive:
                try:
                    archive_receipt = archive_committed_batch(
                        state_root,
                        repo,
                        manifest_path=binding[0],
                        writer_receipt_path=binding[1],
                        contract=archive_contract,
                    )
                    result["archive_status"] = archive_receipt["status"]
                except Exception as exc:
                    result["errors"].append(f"archive_failed:{type(exc).__name__}:{exc}")
        pending_ids: set[str] = set()
        for package_id, row in current_rows.items():
            if row["status"] != "pending":
                continue
            planned_receipts = {
                item.get("receipt_id")
                for item in rows_by_id[package_id].get("trusted_closeouts", [])
            }
            current_receipts = {
                item.get("receipt_id") for item in row.get("trusted_closeouts", [])
            }
            if current_receipts - planned_receipts:
                continue
            pending_ids.add(package_id)
        if not pending_ids:
            day_results.append(result)
            continue
        try:
            manifest_path, manifest = freeze_nightly(
                state_root,
                repo,
                study_date=study_date,
                package_ids=pending_ids,
                local_review_id=plan.get("local_review_id"),
            )
        except Exception as exc:
            result["errors"].append(f"freeze_failed:{type(exc).__name__}:{exc}")
            day_results.append(result)
            continue
        result["manifest_path"] = str(manifest_path)
        if manifest["status"] == "NOOP":
            day_results.append(result)
            continue
        actions_path: Path | None = None
        preflight_proposal_path: Path | None = None
        try:
            if actions_provider is not None:
                actions_path = actions_provider(study_date, manifest_path, manifest)
            elif actions_dir is not None:
                for candidate in (
                    actions_dir / f"{manifest['batch_id']}.actions.json",
                    actions_dir / f"{manifest['batch_id']}.json",
                ):
                    if candidate.is_file():
                        actions_path = candidate
                        break
        except Exception as exc:
            result["errors"].append(f"actions_provider_failed:{type(exc).__name__}:{exc}")
            day_results.append(result)
            continue
        if actions_path is None:
            result["errors"].append("actions_required")
            day_results.append(result)
            continue
        try:
            actions_doc = load_json(actions_path)
            existing = _existing_closeout_for_batch(
                state_root,
                manifest,
                str(actions_doc.get("action_set_id", "")),
            )
            if existing is not None:
                writer_path, writer_receipt = existing
                reused_closeouts.append(writer_receipt["receipt_id"])
                from .publication import complete_formal_closeout
                complete_formal_closeout(repo, state_root, writer_receipt)
            else:
                dry_path, dry_receipt = apply_nightly(
                    state_root,
                    repo,
                    manifest_path=manifest_path,
                    actions_path=actions_path,
                    apply=False,
                )
                if dry_receipt["status"] == "CAS_CONFLICT":
                    result["errors"].append("dry_run_cas_conflict")
                    day_results.append(result)
                    continue
                result["writer_status"] = dry_receipt["status"]
                try:
                    preflight_proposal_path = _resolve_support_proposal(
                        study_date=study_date,
                        manifest_path=manifest_path,
                        manifest=manifest,
                        writer_receipt_path=dry_path,
                        writer_receipt=dry_receipt,
                        support_provider=support_provider,
                        support_dir=support_dir,
                        actions_path=actions_path,
                    )
                    if preflight_proposal_path is None:
                        raise ValidationError(
                            "sentence-support proposal is required before formal apply"
                        )
                    expected_completed = {
                        package_id
                        for package_id in manifest.get("package_ids", [])
                        if any(
                            result_row.get("result")
                            in {"would_apply", "applied", "skipped"}
                            and any(
                                isinstance(ref, dict)
                                and ref.get("package_id") == package_id
                                for ref in result_row.get("evidence_refs", [])
                            )
                            for result_row in dry_receipt.get("action_results", [])
                        )
                        and not any(
                            result_row.get("result") == "needs_user"
                            and any(
                                isinstance(ref, dict)
                                and ref.get("package_id") == package_id
                                for ref in result_row.get("evidence_refs", [])
                            )
                            for result_row in dry_receipt.get("action_results", [])
                        )
                    }
                    preflight_path, preflight_receipt = preflight_sentence_support(
                        state_root,
                        manifest_path=manifest_path,
                        proposal_path=preflight_proposal_path,
                        dry_run_receipt_path=dry_path,
                        expected_completed_ids=expected_completed,
                    )
                    result["support_preflight"] = {
                        **preflight_receipt,
                        "receipt_path": str(preflight_path),
                    }
                except Exception as exc:
                    result["errors"].append(
                        f"support_preflight_failed:{type(exc).__name__}:{exc}"
                    )
                    day_results.append(result)
                    continue
                writer_path, writer_receipt = apply_nightly(
                    state_root,
                    repo,
                    manifest_path=manifest_path,
                    actions_path=actions_path,
                    apply=True,
                    authorization=manifest["batch_id"],
                    support_preflight_path=preflight_path,
                )
                apply_invocations.append(writer_receipt["receipt_id"])
        except Exception as exc:
            result["errors"].append(f"writer_failed:{type(exc).__name__}:{exc}")
            day_results.append(result)
            continue
        result["writer_receipt_path"] = str(writer_path)
        result["writer_status"] = writer_receipt["status"]
        try:
            support_path, support_receipt = _ensure_support_refresh(
                state_root,
                repo,
                study_date=study_date,
                manifest_path=manifest_path,
                writer_receipt_path=writer_path,
                support_provider=support_provider,
                support_dir=support_dir,
                actions_path=actions_path,
                proposal_path_override=preflight_proposal_path,
            )
            result["support_refresh_receipt_path"] = (
                str(support_path) if support_path is not None else None
            )
            result["support_status"] = support_receipt["status"]
        except Exception as exc:
            result["support_status"] = "FORMAL_COMMITTED_SUPPORT_PENDING"
            result["errors"].append(
                f"support_refresh_failed:{type(exc).__name__}:{exc}"
            )
            day_results.append(result)
            continue
        if archive and apply:
            try:
                archive_receipt = archive_committed_batch(
                    state_root,
                    repo,
                    manifest_path=manifest_path,
                    writer_receipt_path=writer_path,
                    contract=archive_contract,
                )
                result["archive_status"] = archive_receipt["status"]
            except Exception as exc:
                result["errors"].append(f"archive_failed:{type(exc).__name__}:{exc}")
        day_results.append(result)
    gate = evaluate_backlog_gate(
        state_root,
        plan,
        already_consumed_at_start=already_at_start,
    )
    execution_failed: dict[str, str] = {}
    for day_result in day_results:
        hard_errors = [
            error for error in day_result["errors"]
            if error.startswith(("freeze_failed:", "actions_provider_failed:", "support_preflight_failed:", "writer_failed:", "dry_run_cas_conflict"))
        ]
        if not hard_errors:
            continue
        for package_id in day_result["package_ids"]:
            execution_failed[package_id] = ";".join(hard_errors)
    if execution_failed:
        retained_needs_user = []
        for row in gate["needs_user"]:
            error = execution_failed.get(row["package_id"])
            if error is None:
                retained_needs_user.append(row)
            else:
                gate["failed"].append({**row, "reason": error})
        gate["needs_user"] = retained_needs_user
        gate["failed"].sort(key=lambda row: (row["study_date"], row["package_id"]))
        gate["status"] = "PARTIAL"
        gate_core = {key: value for key, value in gate.items() if key != "gate_sha256"}
        gate["gate_sha256"] = object_sha256(gate_core)
    return {
        "schema_version": "english_backlog_execution_receipt_v1",
        "status": gate["status"],
        "plan_id": plan["plan_id"],
        "plan_canonical_sha256": plan["plan_canonical_sha256"],
        "plan_file_sha256": plan["plan_file_sha256"],
        "day_results": day_results,
        "apply_invocations": apply_invocations,
        "reused_closeouts": reused_closeouts,
        "support_refresh_status": (
            "NOOP_NO_PACKAGES"
            if not plan.get("packages")
            else "NOT_RUN"
            if not apply
            else (
                "PENDING"
                if any(
                    error.startswith("support_refresh_failed:")
                    for row in day_results
                    for error in row["errors"]
                )
                else "PASS"
            )
        ),
        "mode": "apply" if apply else "dry_run",
        "legacy_reachability_audit": plan.get("legacy_reachability_audit"),
        "global_gate": gate,
    }


def _current_state_for_planned_row(state_dir: Path, planned: dict[str, Any]) -> dict[str, Any]:
    package_id = planned["package_id"]
    study_date = planned["study_date"]
    root = state_dir / "packages" / study_date / package_id
    if root.is_dir():
        row = _local_package_record(
            state_dir,
            root,
            _open_transactions_by_package(state_dir),
        )
        from .local_review import local_pending_record
        return local_pending_record(state_dir, row, planned.get("local_review_id"))
    consumed = _trusted_consumed_pointer(
        state_dir,
        study_date,
        package_id,
        planned.get("package_sha256"),
    )
    if consumed:
        return {**planned, "status": "already_consumed", "consumed": consumed}
    package_sha256 = planned.get("package_sha256")
    if isinstance(package_sha256, str):
        closeouts = _trusted_closeouts_for_package(
            state_dir,
            package_id,
            package_sha256,
        )
        if any(row.get("disposition") == "completed" for row in closeouts):
            return {
                **planned,
                "status": "archive_pending",
                "reason": "trusted_formal_closeout_archive_evidence_incomplete",
                "trusted_closeouts": closeouts,
                "pending_component": _pending_component_for_closeout(
                    state_dir,
                    study_date,
                    package_id,
                    next(
                        row for row in reversed(closeouts)
                        if row.get("disposition") == "completed"
                    ),
                ),
            }
    return {**planned, "status": "failed", "reason": "planned_package_missing_without_trusted_cleanup"}


def evaluate_backlog_gate(
    state_dir: Path,
    plan: dict[str, Any],
    *,
    already_consumed_at_start: set[str] | None = None,
) -> dict[str, Any]:
    state_root = state_dir.resolve()
    initial_consumed = already_consumed_at_start or set()
    categories: dict[str, list[dict[str, Any]]] = {
        "completed": [],
        "already_consumed": [],
        "needs_user": [],
        "failed": [],
        "archive_pending": [],
    }
    for planned in plan.get("packages", []):
        current = _current_state_for_planned_row(state_root, planned)
        status = current["status"]
        summary = {
            "package_id": planned["package_id"],
            "study_date": planned["study_date"],
            "reason": current.get("reason"),
            "pending_component": current.get("pending_component"),
        }
        if status == "already_consumed":
            category = (
                "already_consumed"
                if planned["status"] == "already_consumed" or planned["package_id"] in initial_consumed
                else "completed"
            )
        elif status == "archive_pending":
            category = "archive_pending"
        elif status in {"needs_user", "pending"}:
            category = "needs_user"
            if status == "pending":
                summary["reason"] = "no_formal_terminal_after_execution"
        else:
            category = "failed"
        categories[category].append(summary)
    for rows in categories.values():
        rows.sort(key=lambda row: (row["study_date"], row["package_id"]))
    status = "NOOP" if not plan.get("packages") else (
        "COMPLETE"
        if not categories["needs_user"]
        and not categories["failed"]
        and not categories["archive_pending"]
        else "PARTIAL"
    )
    core = {
        "schema_version": BACKLOG_GATE_SCHEMA_VERSION,
        "status": status,
        "plan_id": plan["plan_id"],
        "plan_canonical_sha256": plan["plan_canonical_sha256"],
        "cutoff_date": plan["cutoff_date"],
        "legacy_reachability_audit": plan.get("legacy_reachability_audit"),
        **categories,
    }
    return {**core, "gate_sha256": object_sha256(core)}


def write_backlog_gate(
    state_dir: Path,
    *,
    plan_path: Path,
) -> tuple[Path, dict[str, Any]]:
    plan = load_backlog_plan(plan_path)
    gate = evaluate_backlog_gate(state_dir, plan)
    target = (
        state_dir.resolve() / "backlog" / "gates" / plan["plan_id"]
        / f"{gate['gate_sha256']}.json"
    )
    if target.exists():
        existing = load_json(target)
        if existing != gate:
            raise IdempotencyConflict("backlog gate path binds different content")
        return target, existing
    atomic_write_json(target, gate)
    return target, load_json(target)
