from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import IdempotencyConflict, ValidationError
from .display_assets import (
    publish_display_assets,
    validate_display_asset_closure,
)
from .packages import (
    validate_canonical_writer_closeout,
    validate_conversation_package,
)
from .sentence_support import (
    sentence_support_required,
    sentence_support_closure_map,
    support_receipt_path,
    validate_sentence_support_refresh,
)
from .util import (
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    load_json,
    object_sha256,
    utc_now,
)


T9_ARCHIVE_ROOT = Path("/Volumes/T9-Data")
ENGLISH_ARCHIVE_RELATIVE_ROOT = Path("02_英语/资料库/原始会话资料")
SENTINEL_RELATIVE_PATH = Path("00_迁移管理/状态/volume-sentinel.json")
T9_SENTINEL_SHA256 = "f086b32b29b2b38f1a28dffb8fde4fa850078332d8a23ae269cc8e857a6bd2ca"
T9_VOLUME_NAME = "T9-Data"
T9_VOLUME_UUID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class VolumeContract:
    archive_root: Path = T9_ARCHIVE_ROOT
    subject_relative_root: Path = ENGLISH_ARCHIVE_RELATIVE_ROOT
    sentinel_relative_path: Path = SENTINEL_RELATIVE_PATH
    sentinel_sha256: str = T9_SENTINEL_SHA256
    volume_name: str = T9_VOLUME_NAME
    volume_uuid: str = T9_VOLUME_UUID
    rejected_mount_path: Path = Path("/Volumes/T9-Data 1")


class SimulatedArchiveFailure(RuntimeError):
    """Test-only failure injected at a durable archive boundary."""


def verify_volume_contract(contract: VolumeContract) -> dict[str, Any]:
    if contract.subject_relative_root.is_absolute() or ".." in contract.subject_relative_root.parts:
        raise ValidationError("archive subject path must be relative and cannot contain '..'")
    if contract.rejected_mount_path.exists():
        raise ValidationError(f"ambiguous rejected archive mount exists: {contract.rejected_mount_path}")
    root = contract.archive_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValidationError("archive root must be an existing directory")
    sentinel_path = (root / contract.sentinel_relative_path).resolve(strict=True)
    try:
        sentinel_path.relative_to(root)
    except ValueError as exc:
        raise ValidationError("archive sentinel escapes configured archive root") from exc
    if not sentinel_path.is_file() or file_sha256(sentinel_path) != contract.sentinel_sha256:
        raise ValidationError("archive sentinel SHA-256 mismatch")
    sentinel = load_json(sentinel_path)
    if sentinel.get("volume_name") != contract.volume_name:
        raise ValidationError("archive sentinel volume_name mismatch")
    if sentinel.get("volume_uuid") != contract.volume_uuid:
        raise ValidationError("archive sentinel volume UUID mismatch")
    expected_mount = sentinel.get("expected_mount_point")
    if expected_mount and Path(str(expected_mount)) != contract.archive_root:
        raise ValidationError("archive sentinel expected_mount_point mismatch")
    stop_suffix = sentinel.get("stop_if_mount_suffix_exists")
    if stop_suffix and Path(str(stop_suffix)).exists():
        raise ValidationError(f"archive sentinel rejected mount exists: {stop_suffix}")
    subject_root = (root / contract.subject_relative_root).resolve()
    try:
        subject_root.relative_to(root)
    except ValueError as exc:
        raise ValidationError("archive subject path escapes configured archive root") from exc
    return {
        "archive_root": root,
        "subject_root": subject_root,
        "sentinel_path": sentinel_path,
        "sentinel_sha256": contract.sentinel_sha256,
        "volume_name": contract.volume_name,
        "volume_uuid": contract.volume_uuid,
    }


def _copy_file_exact(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or file_sha256(destination) != file_sha256(source):
            raise IdempotencyConflict(f"archive stage file differs: {destination}")
        return
    from .packages import _write_immutable

    _write_immutable(destination, source.read_bytes())


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _stage_and_finalize_package(
    local_root: Path,
    final_root: Path,
    *,
    batch_id: str,
    expected_package_sha256: str,
    fault_at: str | None = None,
) -> tuple[dict[str, str], str]:
    local_manifest = validate_conversation_package(local_root)
    if local_manifest["package_canonical_sha256"] != expected_package_sha256:
        raise ValidationError("local package differs from frozen package SHA")
    local_hashes = _tree_hashes(local_root)
    if final_root.exists():
        archived_manifest = validate_conversation_package(final_root)
        if (
            archived_manifest["package_canonical_sha256"] != expected_package_sha256
            or _tree_hashes(final_root) != local_hashes
        ):
            raise IdempotencyConflict(f"final archive path contains different bytes: {final_root}")
        return local_hashes, "idempotent_noop"

    stage_root = final_root.parent / f".{final_root.name}.stage-{batch_id}"
    stage_root.mkdir(parents=True, exist_ok=True)
    for source in sorted(path for path in local_root.rglob("*") if path.is_file()):
        _copy_file_exact(source, stage_root / source.relative_to(local_root))
    staged_manifest = validate_conversation_package(stage_root)
    if (
        staged_manifest["package_canonical_sha256"] != expected_package_sha256
        or _tree_hashes(stage_root) != local_hashes
    ):
        raise ValidationError(f"archive stage verification failed: {stage_root}")
    if fault_at == "after_stage_verify":
        raise SimulatedArchiveFailure(str(stage_root))
    final_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(stage_root, final_root)
    except OSError as exc:
        raise ValidationError(f"atomic archive finalization failed: {final_root}") from exc
    final_manifest = validate_conversation_package(final_root)
    if (
        final_manifest["package_canonical_sha256"] != expected_package_sha256
        or _tree_hashes(final_root) != local_hashes
    ):
        raise ValidationError(f"final archive verification failed: {final_root}")
    return local_hashes, "created"


def _quote_yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_locator(
    *,
    package_id: str,
    formal_ids: list[str],
    archive_relative_path: str,
    manifest_sha256: str,
    package_sha256: str,
    verified_at: str,
    retrieval_keys: list[str],
) -> str:
    formal_id_lines = (
        ["formal_ids:", *[f"  - {_quote_yaml(value)}" for value in formal_ids]]
        if formal_ids
        else ["formal_ids: []"]
    )
    retrieval_key_lines = (
        ["retrieval_keys:", *[f"  - {_quote_yaml(value)}" for value in retrieval_keys]]
        if retrieval_keys
        else ["retrieval_keys: []"]
    )
    lines = [
        "---",
        "type: raw_archive_locator",
        "subject: english",
        f"package_id: {_quote_yaml(package_id)}",
        *formal_id_lines,
        *retrieval_key_lines,
        "archive_volume: T9-Data",
        f"raw_archive_relpath: {_quote_yaml(archive_relative_path)}",
        f"raw_archive_manifest_sha256: {_quote_yaml(manifest_sha256)}",
        f"raw_archive_package_sha256: {_quote_yaml(package_sha256)}",
        f"archive_verified_at: {_quote_yaml(verified_at)}",
        "archive_status: verified",
        "---",
        "",
        "该页只保存已验真的数据盘相对定位；完整原始会话包保存在 T9-Data。",
        "",
    ]
    return "\n".join(lines)


def _parse_locator(text: str) -> dict[str, Any]:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValidationError("archive locator frontmatter is missing")
    frontmatter = text.split("\n---\n", 1)[0].splitlines()[1:]
    scalar: dict[str, str] = {}
    list_values: dict[str, list[str]] = {"formal_ids": [], "retrieval_keys": []}
    active_list: str | None = None
    for line in frontmatter:
        if line in {"formal_ids:", "retrieval_keys:"}:
            active_list = line[:-1]
            continue
        if active_list and line.startswith("  - "):
            list_values[active_list].append(json.loads(line[4:]))
            continue
        active_list = None
        if line in {"formal_ids: []", "retrieval_keys: []"}:
            scalar[line.split(":", 1)[0]] = []
            continue
        if ": " in line:
            key, value = line.split(": ", 1)
            scalar[key] = json.loads(value) if value.startswith('"') else value
    for key, values in list_values.items():
        if values:
            scalar[key] = values
    return scalar


def validate_archive_locator_binding(
    locator_path: Path,
    *,
    package_id: str,
    formal_ids: list[str],
    archive_relative_path: str,
    manifest_sha256: str,
    package_sha256: str,
    verified_at: str,
    retrieval_keys: list[str] | None = None,
    expected_locator_sha256: str | None = None,
) -> dict[str, Any]:
    """Reread one exact locator and verify every archive identity binding."""

    if locator_path.is_symlink() or not locator_path.is_file():
        raise ValidationError("archive locator must be a regular file")
    resolved = locator_path.resolve(strict=True)
    parsed = _parse_locator(resolved.read_text(encoding="utf-8"))
    required = {
        "type": "raw_archive_locator",
        "subject": "english",
        "package_id": package_id,
        "formal_ids": formal_ids,
        "archive_volume": "T9-Data",
        "raw_archive_relpath": archive_relative_path,
        "raw_archive_manifest_sha256": manifest_sha256,
        "raw_archive_package_sha256": package_sha256,
        "archive_verified_at": verified_at,
        "archive_status": "verified",
    }
    if retrieval_keys is not None:
        required["retrieval_keys"] = retrieval_keys
    for key, value in required.items():
        if parsed.get(key) != value:
            raise ValidationError(f"archive locator field mismatch: {key}")
    locator_sha256 = file_sha256(resolved)
    if (
        expected_locator_sha256 is not None
        and locator_sha256 != expected_locator_sha256
    ):
        raise ValidationError("archive locator SHA-256 mismatch")
    return {
        "locator_path": str(resolved),
        "locator_sha256": locator_sha256,
        "locator": parsed,
    }


def write_and_verify_locator(
    repo_root: Path,
    *,
    package_id: str,
    formal_ids: list[str],
    archive_relative_path: str,
    manifest_sha256: str,
    package_sha256: str,
    verified_at: str,
    retrieval_keys: list[str],
) -> tuple[Path, str]:
    locator_path = repo_root.resolve() / "wiki" / "raw_archives" / f"{package_id}.md"
    expected = _render_locator(
        package_id=package_id,
        formal_ids=formal_ids,
        archive_relative_path=archive_relative_path,
        manifest_sha256=manifest_sha256,
        package_sha256=package_sha256,
        verified_at=verified_at,
        retrieval_keys=retrieval_keys,
    )
    if locator_path.exists():
        existing = locator_path.read_text(encoding="utf-8")
        parsed = _parse_locator(existing)
        expected_parsed = _parse_locator(expected)
        comparable = {
            key: value for key, value in parsed.items() if key != "archive_verified_at"
        }
        comparable_expected = {
            key: value for key, value in expected_parsed.items() if key != "archive_verified_at"
        }
        if comparable != comparable_expected:
            raise IdempotencyConflict(f"archive locator already binds different evidence: {locator_path}")
    else:
        atomic_write_text(locator_path, expected)
    verification = validate_archive_locator_binding(
        locator_path,
        package_id=package_id,
        formal_ids=formal_ids,
        archive_relative_path=archive_relative_path,
        manifest_sha256=manifest_sha256,
        package_sha256=package_sha256,
        verified_at=verified_at,
        retrieval_keys=retrieval_keys,
    )
    return locator_path, verification["locator_sha256"]


def _formal_ids_by_package(
    writer_receipt: dict[str, Any],
    package_ids: list[str],
) -> dict[str, list[str]]:
    result = {package_id: [] for package_id in package_ids}
    for row in writer_receipt.get("action_results", []):
        assigned_id = row.get("assigned_id")
        if not assigned_id:
            continue
        evidence_refs = row.get("resolved_evidence", [])
        for ref in evidence_refs:
            package_id = ref.get("package_id") if isinstance(ref, dict) else None
            if package_id in result and assigned_id not in result[package_id]:
                result[package_id].append(str(assigned_id))
    return {key: sorted(values) for key, values in result.items()}


def _retrieval_keys_for_package(
    package_root: Path,
    formal_ids: list[str],
) -> list[str]:
    manifest = validate_conversation_package(package_root)
    source = load_json(package_root / "source.json").get("identity", {})
    values = {
        manifest["package_id"],
        manifest["study_date"],
        *formal_ids,
    }
    if isinstance(source, dict):
        for field in (
            "source_id",
            "article_id",
            "paragraph_id",
            "sentence_id",
            "knowledge_point_id",
            "question_id",
        ):
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                values.add(value.strip())
    return sorted(values)


def resolve_archived_package_from_pointer(
    state_dir: Path,
    repo_root: Path,
    *,
    study_date: str,
    package_id: str,
    expected_package_sha256: str | None = None,
    expected_manifest_sha256: str | None = None,
) -> Path:
    """Resolve one exact local pointer -> locator -> archived package chain without scanning T9."""
    state_root = state_dir.resolve()
    repo = repo_root.resolve(strict=True)
    pointer_path = state_root / "archive-pointers" / study_date / f"{package_id}.json"
    if not pointer_path.is_file():
        raise ValidationError("exact archive pointer is missing")
    pointer = load_json(pointer_path)
    if (
        pointer.get("schema_version") != "english_archive_pointer_v1"
        or pointer.get("package_id") != package_id
        or (
            expected_package_sha256 is not None
            and pointer.get("package_sha256") != expected_package_sha256
        )
    ):
        raise ValidationError("exact archive pointer identity mismatch")
    locator_path = Path(str(pointer.get("archive_locator_path", ""))).resolve(strict=True)
    expected_locator = (repo / "wiki" / "raw_archives" / f"{package_id}.md").resolve(strict=True)
    if locator_path != expected_locator:
        raise ValidationError("exact archive pointer locator is not canonical")
    locator = _parse_locator(locator_path.read_text(encoding="utf-8"))
    relative_value = locator.get("raw_archive_relpath")
    relative = Path(str(relative_value or ""))
    archive_root = Path(str(pointer.get("archive_root", ""))).resolve(strict=True)
    if (
        locator.get("type") != "raw_archive_locator"
        or locator.get("subject") != "english"
        or locator.get("package_id") != package_id
        or locator.get("raw_archive_package_sha256") != pointer.get("package_sha256")
        or not isinstance(locator.get("retrieval_keys"), list)
        or package_id not in locator.get("retrieval_keys", [])
        or study_date not in locator.get("retrieval_keys", [])
        or relative.is_absolute()
        or ".." in relative.parts
        or pointer.get("archive_relative_path") != relative.as_posix()
    ):
        raise ValidationError("exact archive locator binding mismatch")
    archived_root = (archive_root / relative).resolve(strict=True)
    try:
        archived_root.relative_to(archive_root)
    except ValueError as exc:
        raise ValidationError("exact archived package escapes its pointer root") from exc
    if archived_root.name != package_id:
        raise ValidationError("exact archived package terminal identity mismatch")
    manifest = validate_conversation_package(archived_root)
    if (
        manifest.get("package_id") != package_id
        or manifest.get("study_date") != study_date
        or manifest.get("package_canonical_sha256") != pointer.get("package_sha256")
        or file_sha256(archived_root / "manifest.json")
        != locator.get("raw_archive_manifest_sha256")
        or (
            expected_manifest_sha256 is not None
            and file_sha256(archived_root / "manifest.json") != expected_manifest_sha256
        )
    ):
        raise ValidationError("exact archived package bytes differ from pointer/locator")
    return archived_root


def _terminal_outcomes_from_writer(
    writer_receipt: dict[str, Any],
    package_ids: list[str],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    dispositions: dict[str, str] = {}
    terminal_outcomes: dict[str, list[str]] = {}
    for package_id in package_ids:
        rows = [
            row
            for row in writer_receipt.get("action_results", [])
            if any(
                isinstance(ref, dict) and ref.get("package_id") == package_id
                for ref in row.get("resolved_evidence", [])
            )
        ]
        if any(row.get("result") == "failed" for row in rows):
            dispositions[package_id] = "failed"
            terminal_outcomes[package_id] = ["failed"]
            continue
        if any(row.get("result") == "needs_user" for row in rows):
            dispositions[package_id] = "needs_user"
            terminal_outcomes[package_id] = ["needs_user"]
            continue
        completed = [row for row in rows if row.get("result") in {"applied", "skipped"}]
        if not completed:
            dispositions[package_id] = "incomplete"
            terminal_outcomes[package_id] = ["incomplete"]
            continue
        outcomes: set[str] = set()
        for row in completed:
            action_type = row.get("action_type")
            if action_type == "skip_duplicate":
                outcomes.add("duplicate")
            elif row.get("result") == "skipped":
                outcomes.add("already_current")
            elif action_type in {
                "master_bank_insert", "mastered_insert", "sentence_pattern_append",
                "review_exclusion_append", "review_reactivation_append",
            }:
                outcomes.add("created")
            elif action_type in {"master_bank_update", "sentence_pattern_merge"}:
                outcomes.add("updated")
            else:
                outcomes.add("curated")
        dispositions[package_id] = "completed"
        terminal_outcomes[package_id] = sorted(outcomes)
    recorded = writer_receipt.get("package_dispositions")
    if not isinstance(recorded, dict) or any(
        recorded.get(package_id) != dispositions[package_id]
        for package_id in package_ids
    ):
        raise ValidationError("writer receipt package dispositions do not match action results")
    return dispositions, terminal_outcomes


def _archive_intent(
    state_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    writer_receipt_path: Path,
    *,
    completed_package_ids: list[str],
    retained_package_ids: list[str],
    display_closures: dict[str, dict[str, Any]],
    sentence_support_status: str,
    sentence_support_refresh: dict[str, Any] | None,
    sentence_support_closures: dict[str, dict[str, Any]],
) -> tuple[Path, dict[str, Any]]:
    path = (
        state_dir.resolve()
        / "archive-intents"
        / manifest["study_date"]
        / f"{manifest['batch_id']}.json"
    )
    binding = {
        "schema_version": "english_archive_intent_v1",
        "batch_id": manifest["batch_id"],
        "manifest_sha256": file_sha256(manifest_path),
        "writer_receipt_sha256": file_sha256(writer_receipt_path),
        "completed_package_ids": completed_package_ids,
        "retained_package_ids": retained_package_ids,
        "display_closures": [display_closures[package_id] for package_id in completed_package_ids],
        "sentence_support_status": sentence_support_status,
        "sentence_support_refresh": sentence_support_refresh,
        "sentence_support_closures": (
            [
                sentence_support_closures[package_id]
                for package_id in completed_package_ids
            ]
            if sentence_support_status == "closed"
            else []
        ),
    }
    if path.exists():
        existing = load_json(path)
        comparable = {key: value for key, value in existing.items() if key != "verified_at"}
        comparable_binding = dict(binding)
        if sentence_support_status == "legacy_support_not_required":
            for key in (
                "sentence_support_status",
                "sentence_support_refresh",
                "sentence_support_closures",
            ):
                comparable.pop(key, None)
                comparable_binding.pop(key, None)
        if comparable != comparable_binding:
            raise IdempotencyConflict("archive intent already binds different committed evidence")
        return path, existing
    intent = {**binding, "verified_at": utc_now()}
    atomic_write_json(path, intent)
    reopened = load_json(path)
    if reopened != intent:
        raise ValidationError("archive intent failed byte-stable reopen")
    return path, intent


def _display_cleanup_binding(package_result: dict[str, Any]) -> dict[str, Any]:
    closure = package_result.get("display_asset_closure")
    if not isinstance(closure, dict):
        raise ValidationError("archive package result lacks display asset closure")
    required = {
        "receipt_id",
        "receipt_path",
        "receipt_sha256",
        "formal_reference_scan_sha256",
        "stable_asset_count",
        "no_display",
    }
    if not required.issubset(closure):
        raise ValidationError("display asset closure binding is incomplete")
    support = package_result.get("sentence_support_closure")
    support_required = {
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
    legacy_support = package_result.get("sentence_support_status") == "legacy_support_not_required"
    if not legacy_support and (
        not isinstance(support, dict) or not support_required.issubset(support)
    ):
        raise ValidationError("archive package result lacks sentence-support closure")
    binding = {
        "display_closure_receipt_id": closure["receipt_id"],
        "display_closure_receipt_path": closure["receipt_path"],
        "display_closure_receipt_sha256": closure["receipt_sha256"],
        "formal_reference_scan_sha256": closure["formal_reference_scan_sha256"],
        "stable_asset_count": closure["stable_asset_count"],
        "no_display": closure["no_display"],
    }
    if not legacy_support:
        assert isinstance(support, dict)
        binding.update({key: support[key] for key in sorted(support_required)})
    return binding


def _cleanup_local_package(
    state_dir: Path,
    local_root: Path,
    *,
    package_result: dict[str, Any],
    archive_receipt_path: Path,
    locator_path: Path,
    fault_at: str | None = None,
) -> tuple[Path, Path]:
    state_root = state_dir.resolve()
    local_root = local_root.resolve(strict=True)
    packages_root = state_root / "packages"
    try:
        relative = local_root.relative_to(packages_root)
    except ValueError as exc:
        raise ValidationError("local cleanup target is outside the package staging root") from exc
    if len(relative.parts) != 2 or relative.parts[1] != package_result["package_id"]:
        raise ValidationError("local cleanup target identity is invalid")
    display_verification = validate_display_asset_closure(
        state_root,
        state_root.parent,
        local_root,
        Path(str(package_result["display_asset_closure"]["receipt_path"])),
    )
    if display_verification != package_result["display_asset_closure"]:
        raise ValidationError("display asset closure changed before local cleanup")
    pointer_path = (
        state_root
        / "archive-pointers"
        / relative.parts[0]
        / f"{package_result['package_id']}.json"
    )
    cleanup_intent_path = (
        state_root
        / "cleanup-intents"
        / relative.parts[0]
        / f"{package_result['package_id']}.json"
    )
    cleanup_binding = {
        "schema_version": "english_archive_cleanup_intent_v1",
        "package_id": package_result["package_id"],
        "package_sha256": package_result["package_sha256"],
        "local_package_path": str(local_root),
        "archive_root": package_result["archive_root"],
        "archive_relative_path": package_result["archive_relative_path"],
        "archive_receipt_path": str(archive_receipt_path),
        "archive_receipt_sha256": file_sha256(archive_receipt_path),
        "locator_path": str(locator_path),
        "locator_sha256": file_sha256(locator_path),
        **_display_cleanup_binding(package_result),
        "pending_component": "cleanup",
        "cleanup_authorized": True,
    }
    if cleanup_intent_path.exists():
        cleanup_intent = load_json(cleanup_intent_path)
        comparable = {
            key: value for key, value in cleanup_intent.items()
            if key != "pointer_created_at"
        }
        if comparable != cleanup_binding:
            raise IdempotencyConflict("cleanup intent already binds different archive evidence")
    else:
        cleanup_intent = {**cleanup_binding, "pointer_created_at": utc_now()}
        atomic_write_json(cleanup_intent_path, cleanup_intent)
    if load_json(cleanup_intent_path) != cleanup_intent:
        raise ValidationError("cleanup intent failed durable reopen")
    if fault_at == "after_cleanup_intent":
        raise SimulatedArchiveFailure(str(cleanup_intent_path))
    pointer = {
        "schema_version": "english_archive_pointer_v1",
        "package_id": package_result["package_id"],
        "package_sha256": package_result["package_sha256"],
        "archive_root": package_result["archive_root"],
        "archive_relative_path": package_result["archive_relative_path"],
        "archive_receipt_path": str(archive_receipt_path),
        "archive_locator_path": str(locator_path),
        **_display_cleanup_binding(package_result),
        "pending_component": "cleanup",
        "created_at": cleanup_intent["pointer_created_at"],
    }
    if pointer_path.exists():
        if load_json(pointer_path) != pointer:
            raise IdempotencyConflict("archive pointer already binds different cleanup evidence")
    else:
        atomic_write_json(pointer_path, pointer)
    reopened = load_json(pointer_path)
    if reopened["package_sha256"] != package_result["package_sha256"]:
        raise ValidationError("archive pointer verification failed")
    if fault_at == "after_pointer":
        raise SimulatedArchiveFailure(str(pointer_path))
    shutil.rmtree(local_root)
    if local_root.exists():
        raise ValidationError("local package cleanup did not complete")
    if fault_at == "after_local_delete":
        raise SimulatedArchiveFailure(str(local_root))
    cleanup_receipt_path = (
        state_root
        / "receipts"
        / "archive-cleanup"
        / relative.parts[0]
        / f"{package_result['package_id']}.json"
    )
    cleanup_receipt = {
        "schema_version": "english_archive_cleanup_receipt_v1",
        "status": "PASS",
        "package_id": package_result["package_id"],
        "package_sha256": package_result["package_sha256"],
        "archive_root": package_result["archive_root"],
        "pointer_path": str(pointer_path),
        "archive_receipt_path": str(archive_receipt_path),
        "locator_path": str(locator_path),
        **_display_cleanup_binding(package_result),
        "pending_component": None,
        "local_package_removed": True,
        "completed_at": utc_now(),
    }
    atomic_write_json(cleanup_receipt_path, cleanup_receipt)
    if load_json(cleanup_receipt_path) != cleanup_receipt:
        raise ValidationError("archive cleanup receipt failed durable reopen")
    return pointer_path, cleanup_receipt_path


def _resume_cleanup_after_local_delete(
    state_dir: Path,
    study_date: str,
    package_result: dict[str, Any],
    *,
    archive_receipt_path: Path,
) -> tuple[Path, Path]:
    state_root = state_dir.resolve()
    package_id = package_result["package_id"]
    pointer_path = state_root / "archive-pointers" / study_date / f"{package_id}.json"
    intent_path = state_root / "cleanup-intents" / study_date / f"{package_id}.json"
    if not pointer_path.is_file() or not intent_path.is_file():
        raise ValidationError("local package is absent without durable pointer and cleanup intent")
    pointer = load_json(pointer_path)
    intent = load_json(intent_path)
    if (
        pointer.get("schema_version") != "english_archive_pointer_v1"
        or pointer.get("package_id") != package_id
        or pointer.get("package_sha256") != package_result["package_sha256"]
        or pointer.get("archive_root") != package_result["archive_root"]
        or pointer.get("archive_relative_path") != package_result["archive_relative_path"]
        or pointer.get("archive_receipt_path") != str(archive_receipt_path)
        or pointer.get("archive_locator_path") != package_result["locator_path"]
        or intent.get("schema_version") != "english_archive_cleanup_intent_v1"
        or intent.get("package_id") != package_id
        or intent.get("package_sha256") != package_result["package_sha256"]
        or intent.get("archive_root") != package_result["archive_root"]
        or intent.get("archive_relative_path") != package_result["archive_relative_path"]
        or intent.get("archive_receipt_path") != str(archive_receipt_path)
        or intent.get("archive_receipt_sha256") != file_sha256(archive_receipt_path)
        or intent.get("locator_path") != package_result["locator_path"]
        or intent.get("locator_sha256") != file_sha256(Path(package_result["locator_path"]))
        or any(
            pointer.get(key) != value or intent.get(key) != value
            for key, value in _display_cleanup_binding(package_result).items()
        )
        or pointer.get("pending_component") != "cleanup"
        or intent.get("pending_component") != "cleanup"
        or intent.get("cleanup_authorized") is not True
    ):
        raise ValidationError("cleanup resume evidence binding mismatch")
    cleanup_receipt_path = (
        state_root / "receipts" / "archive-cleanup" / study_date / f"{package_id}.json"
    )
    if not cleanup_receipt_path.exists():
        atomic_write_json(
            cleanup_receipt_path,
            {
                "schema_version": "english_archive_cleanup_receipt_v1",
                "status": "PASS",
                "package_id": package_id,
                "package_sha256": package_result["package_sha256"],
                "archive_root": package_result["archive_root"],
                "pointer_path": str(pointer_path),
                "archive_receipt_path": str(archive_receipt_path),
                "locator_path": package_result["locator_path"],
                **_display_cleanup_binding(package_result),
                "pending_component": None,
                "local_package_removed": True,
                "completed_at": utc_now(),
                "resumed_from_cleanup_intent": True,
            },
        )
    reopened_cleanup = load_json(cleanup_receipt_path)
    expected_cleanup = {
        "schema_version": "english_archive_cleanup_receipt_v1",
        "status": "PASS",
        "package_id": package_id,
        "package_sha256": package_result["package_sha256"],
        "archive_root": package_result["archive_root"],
        "pointer_path": str(pointer_path),
        "archive_receipt_path": str(archive_receipt_path),
        "locator_path": package_result["locator_path"],
        **_display_cleanup_binding(package_result),
        "pending_component": None,
        "local_package_removed": True,
    }
    if any(reopened_cleanup.get(key) != value for key, value in expected_cleanup.items()):
        raise ValidationError("cleanup resume receipt binding mismatch")
    if not isinstance(reopened_cleanup.get("completed_at"), str):
        raise ValidationError("cleanup resume receipt lacks completed_at")
    return pointer_path, cleanup_receipt_path


def _archive_publication_basis(receipt: dict[str, Any], cleaned_package_ids: list[str]) -> dict[str, Any]:
    return {
        "manifest_sha256": receipt["manifest_sha256"],
        "writer_receipt_sha256": receipt["writer_receipt_sha256"],
        "package_hashes": {
            row["package_id"]: {
                "package_sha256": row["package_sha256"],
                "archive_manifest_sha256": row["archive_manifest_sha256"],
                "tree_sha256": row["tree_sha256"],
                "display_receipt_sha256": row["display_asset_closure"]["receipt_sha256"],
                "support_hashes": {
                    key: value for key, value in (row.get("sentence_support_closure") or {}).items()
                    if key.endswith("_sha256")
                },
            }
            for row in receipt["package_results"]
        },
        "cleaned_package_ids": sorted(cleaned_package_ids),
    }


def retry_archived_publication(
    state_dir: Path, repo_root: Path, writer_receipt: dict[str, Any], *,
    writer_receipt_path: Path, cleanup_local: bool | None = None,
    publication_runner: Callable[..., dict[str, Any]] | None = None,
    retry_publication: bool = False,
) -> dict[str, Any] | None:
    """Reopen an existing final event; never refresh support or repeat cleanup.

    The caller has validated the canonical writer closeout. Reconstructing the
    content basis also supports hooks written before that basis was stored.
    """
    day = writer_receipt_path.parent.name
    archive_path = state_dir / "receipts/archive" / day / f"{writer_receipt['batch_id']}.json"
    if not archive_path.is_file():
        return None
    receipt = load_json(archive_path)
    if (receipt.get("status") != "PASS" or receipt.get("batch_id") != writer_receipt["batch_id"]
            or receipt.get("manifest_sha256") != writer_receipt["manifest_sha256"]
            or receipt.get("writer_receipt_sha256") != file_sha256(writer_receipt_path)):
        raise ValidationError("archive publication writer binding mismatch")
    cleaned = []
    for row in receipt["package_results"]:
        cleanup = state_dir / "receipts/archive-cleanup" / day / f"{row['package_id']}.json"
        if cleanup.is_file():
            value = load_json(cleanup)
            if (value.get("status") != "PASS" or value.get("package_sha256") != row["package_sha256"]
                    or value.get("archive_receipt_path") != str(archive_path)
                    or value.get("local_package_removed") is not True
                    or Path(row["local_path"]).exists()):
                raise ValidationError("archive publication cleanup binding mismatch")
            cleaned.append(row["package_id"])
    if cleanup_local is True and len(cleaned) != len(receipt["package_results"]):
        return None
    # An explicit retain-local closeout has a different stable publication basis.
    basis = _archive_publication_basis(receipt, [] if cleanup_local is False else cleaned)
    digest = object_sha256(basis)
    event_id = writer_receipt["receipt_id"] + ":closeout:" + digest[:24]
    target = state_dir / "publication" / f"{event_id}.json"
    if not target.is_file():
        return None
    previous = load_json(target)
    if (previous.get("event_id") != event_id or previous.get("schema_version") != "english_publication_hook_receipt_v1"
            or previous.get("archive_basis_sha256", digest) != digest):
        raise ValidationError("archive publication event binding mismatch")
    from .publication import complete_formal_closeout
    publication = complete_formal_closeout(repo_root, state_dir, writer_receipt,
        runner=publication_runner, retry_publication=retry_publication, archive_basis_sha256=digest)
    return {"schema_version": "english_archive_workflow_receipt_v1",
            "status": "PARTIAL" if receipt["retained_package_ids"] else ("COMPLETE" if len(cleaned) == len(receipt["package_results"]) else "ARCHIVE_VERIFIED"),
            "batch_id": receipt["batch_id"], "archive_receipt_path": str(archive_path),
            "archive_receipt_sha256": file_sha256(archive_path), "package_results": receipt["package_results"],
            "cleanup_results": [{"package_id": key, "status": "idempotent_noop"} for key in cleaned],
            "retained_package_ids": receipt["retained_package_ids"], "publication": publication,
            "publication_only": True, "formal_write_count": 0}


def archive_committed_batch(
    state_dir: Path,
    repo_root: Path,
    *,
    manifest_path: Path,
    writer_receipt_path: Path,
    contract: VolumeContract = VolumeContract(),
    cleanup_local: bool = True,
    fault_at: str | None = None,
    publication_runner: Callable[..., dict[str, Any]] | None = None,
    retry_publication: bool = False,
) -> dict[str, Any]:
    volume = verify_volume_contract(contract)
    manifest_path = manifest_path.resolve(strict=True)
    writer_receipt_path = writer_receipt_path.resolve(strict=True)
    manifest = load_json(manifest_path)
    writer_receipt = load_json(writer_receipt_path)
    if manifest.get("schema_version") not in {
        "english_nightly_manifest_v2",
        "english_nightly_manifest_v3",
    }:
        raise ValidationError("archive requires a supported package manifest")
    if writer_receipt.get("batch_id") != manifest.get("batch_id"):
        raise ValidationError("writer receipt batch does not match archive manifest")
    if writer_receipt.get("mode") != "apply" or writer_receipt.get("status") not in {
        "APPLIED", "PARTIAL", "NO_ACTION",
    }:
        raise ValidationError("archive requires a successful committed formal writer receipt")
    if writer_receipt.get("package_sha256s") != manifest.get("package_sha256s"):
        raise ValidationError("writer receipt package binding mismatch")
    package_ids = list(manifest.get("package_ids", []))
    archive_receipt_path = (
        state_dir.resolve()
        / "receipts"
        / "archive"
        / manifest["study_date"]
        / f"{manifest['batch_id']}.json"
    )
    package_root_overrides: dict[str, Path] = {}
    if archive_receipt_path.exists():
        existing_archive_receipt = load_json(archive_receipt_path)
        for row in existing_archive_receipt.get("package_results", []):
            if not isinstance(row, dict) or row.get("package_id") not in package_ids:
                continue
            relative = Path(str(row.get("archive_relative_path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValidationError("archive resume package override path is unsafe")
            archived_root = (volume["archive_root"] / relative).resolve(strict=True)
            try:
                archived_root.relative_to(volume["subject_root"])
            except ValueError as exc:
                raise ValidationError("archive resume package override escapes subject root") from exc
            package_root_overrides[row["package_id"]] = archived_root
    validated_receipt, validated_manifest = validate_canonical_writer_closeout(
        state_dir,
        writer_receipt_path,
        expected_manifest_path=manifest_path,
        package_root_overrides=package_root_overrides,
    )
    if validated_receipt != writer_receipt or validated_manifest != manifest:
        raise ValidationError("canonical writer closeout reopen differs from loaded evidence")
    if retry_publication:
        publication_retry = retry_archived_publication(
            state_dir.resolve(), repo_root, writer_receipt, writer_receipt_path=writer_receipt_path,
            cleanup_local=cleanup_local, publication_runner=publication_runner, retry_publication=True)
        if publication_retry is not None:
            return publication_retry
    dispositions, terminal_outcomes = _terminal_outcomes_from_writer(
        writer_receipt, package_ids
    )
    completed_package_ids = [
        package_id for package_id in package_ids
        if dispositions.get(package_id) == "completed"
    ]
    retained_package_ids = [
        package_id for package_id in package_ids
        if package_id not in completed_package_ids
    ]
    formal_ids = _formal_ids_by_package(writer_receipt, package_ids)
    support_required_for_batch = sentence_support_required(manifest)
    sentence_support_status = (
        "closed" if support_required_for_batch else "legacy_support_not_required"
    )
    sentence_support_refresh: dict[str, Any] | None = None
    sentence_support_closures: dict[str, dict[str, Any]] = {}
    if support_required_for_batch:
        support_path = support_receipt_path(
            state_dir.resolve(), manifest["study_date"], manifest["batch_id"]
        )
        support_receipt, _ = validate_sentence_support_refresh(
            state_dir,
            manifest_path=manifest_path,
            writer_receipt_path=writer_receipt_path,
            receipt_path=support_path,
        )
        sentence_support_refresh = {
            "receipt_id": support_receipt["receipt_id"],
            "receipt_path": str(support_path.resolve(strict=True)),
            "receipt_sha256": file_sha256(support_path),
            "index_snapshot_path": support_receipt["index_snapshot_path"],
            "index_snapshot_sha256": support_receipt["index_snapshot_sha256"],
            "history_complete_through": support_receipt["history_complete_through"],
        }
        all_support_closures = sentence_support_closure_map(
            support_receipt, support_path
        )
        for package_id in completed_package_ids:
            closure = all_support_closures.get(package_id)
            if closure is None:
                raise ValidationError(
                    "completed package lacks sentence-support closure"
                )
            sentence_support_closures[package_id] = closure
    display_closures: dict[str, dict[str, Any]] = {}
    records_by_id = {
        record["package_id"]: record for record in manifest.get("package_documents", [])
    }
    for package_id in completed_package_ids:
        record = records_by_id.get(package_id)
        if record is None:
            raise ValidationError("completed package is absent from frozen manifest")
        package_root = package_root_overrides.get(package_id, Path(record["path"]))
        receipt_path = (
            state_dir.resolve()
            / "receipts"
            / "display-assets"
            / manifest["study_date"]
            / f"{package_id}.json"
        )
        if receipt_path.is_file():
            closure = validate_display_asset_closure(
                state_dir,
                repo_root,
                package_root,
                receipt_path,
            )
        else:
            receipt_path, _ = publish_display_assets(
                state_dir,
                repo_root,
                package_root,
                fault_at=(
                    fault_at
                    if fault_at in {
                        "after_stable_copy",
                        "after_markdown_write",
                        "after_display_receipt",
                    }
                    else None
                ),
            )
            closure = validate_display_asset_closure(
                state_dir,
                repo_root,
                package_root,
                receipt_path,
            )
        display_closures[package_id] = closure
    if fault_at == "after_display_assets":
        raise SimulatedArchiveFailure("display assets closed before archive")
    archive_intent_path, archive_intent = _archive_intent(
        state_dir,
        manifest,
        manifest_path,
        writer_receipt_path,
        completed_package_ids=completed_package_ids,
        retained_package_ids=retained_package_ids,
        display_closures=display_closures,
        sentence_support_status=sentence_support_status,
        sentence_support_refresh=sentence_support_refresh,
        sentence_support_closures=sentence_support_closures,
    )
    verified_at = archive_intent["verified_at"]
    package_results: list[dict[str, Any]] = []
    if archive_receipt_path.exists():
        existing_receipt = load_json(archive_receipt_path)
        if (
            existing_receipt.get("status") != "PASS"
            or existing_receipt.get("batch_id") != manifest["batch_id"]
            or existing_receipt.get("manifest_sha256") != file_sha256(manifest_path)
            or existing_receipt.get("writer_receipt_sha256") != file_sha256(writer_receipt_path)
        ):
            raise IdempotencyConflict("existing archive receipt does not bind this committed batch")
        package_results = existing_receipt.get("package_results", [])
        if not support_required_for_batch:
            package_results = [
                {
                    **row,
                    "sentence_support_status": row.get("sentence_support_status")
                    or "legacy_support_not_required",
                }
                for row in package_results
            ]
        if [row.get("package_id") for row in package_results] != completed_package_ids:
            raise ValidationError("existing archive receipt package set mismatch")
        for result in package_results:
            final_root = volume["archive_root"] / result["archive_relative_path"]
            archived = validate_conversation_package(final_root)
            if archived["package_canonical_sha256"] != result["package_sha256"]:
                raise ValidationError("existing archived package failed resume verification")
            locator = Path(result["locator_path"])
            if not locator.is_file() or file_sha256(locator) != result["locator_sha256"]:
                raise ValidationError("existing locator failed resume verification")
            closure = display_closures.get(result["package_id"])
            if result.get("display_asset_closure") != closure:
                raise ValidationError("existing archive receipt display closure binding mismatch")
            if support_required_for_batch and (
                result.get("sentence_support_closure")
                != sentence_support_closures.get(result["package_id"])
            ):
                raise ValidationError("existing archive receipt sentence-support closure binding mismatch")
            if (
                result.get("sentence_support_status")
                or (
                    "legacy_support_not_required"
                    if not support_required_for_batch
                    else None
                )
            ) != sentence_support_status:
                raise ValidationError("existing archive receipt sentence-support status mismatch")
    else:
        for record in manifest.get("package_documents", []):
            if record["package_id"] not in completed_package_ids:
                continue
            local_root = Path(record["path"])
            if not local_root.is_dir():
                raise ValidationError("local package is missing before archive receipt creation")
            local_root = local_root.resolve(strict=True)
            final_root = (
                volume["subject_root"]
                / manifest["study_date"]
                / record["package_id"]
            )
            tree_hashes, copy_status = _stage_and_finalize_package(
                local_root,
                final_root,
                batch_id=manifest["batch_id"],
                expected_package_sha256=record["package_sha256"],
                fault_at=fault_at,
            )
            archive_relpath = final_root.relative_to(volume["archive_root"]).as_posix()
            locator_path, locator_sha = write_and_verify_locator(
                repo_root,
                package_id=record["package_id"],
                formal_ids=formal_ids[record["package_id"]],
                archive_relative_path=archive_relpath,
                manifest_sha256=file_sha256(final_root / "manifest.json"),
                package_sha256=record["package_sha256"],
                verified_at=verified_at,
                retrieval_keys=_retrieval_keys_for_package(
                    final_root,
                    formal_ids[record["package_id"]],
                ),
            )
            package_results.append(
                {
                    "package_id": record["package_id"],
                    "package_sha256": record["package_sha256"],
                    "archive_root": str(volume["archive_root"]),
                    "archive_relative_path": archive_relpath,
                    "archive_manifest_sha256": file_sha256(final_root / "manifest.json"),
                    "tree_sha256": object_sha256(tree_hashes),
                    "locator_path": str(locator_path),
                    "locator_sha256": locator_sha,
                    "formal_ids": formal_ids[record["package_id"]],
                    "retrieval_keys": _retrieval_keys_for_package(
                        final_root,
                        formal_ids[record["package_id"]],
                    ),
                    "display_asset_closure": display_closures[record["package_id"]],
                    "sentence_support_status": sentence_support_status,
                    "sentence_support_closure": sentence_support_closures.get(record["package_id"]),
                    "terminal_outcomes": terminal_outcomes[record["package_id"]],
                    "copy_status": copy_status,
                    "verification": "PASS",
                    "local_path": str(local_root),
                }
            )
            if fault_at == "after_locator":
                raise SimulatedArchiveFailure(str(locator_path))
    receipt_core = {
        "schema_version": "english_package_archive_receipt_v2",
        "status": "PASS",
        "batch_id": manifest["batch_id"],
        "manifest_sha256": file_sha256(manifest_path),
        "writer_receipt_path": str(writer_receipt_path),
        "writer_receipt_sha256": file_sha256(writer_receipt_path),
        "archive_intent_path": str(archive_intent_path),
        "archive_intent_sha256": file_sha256(archive_intent_path),
        "archive_root": str(volume["archive_root"]),
        "volume_name": volume["volume_name"],
        "volume_uuid": volume["volume_uuid"],
        "sentinel_sha256": volume["sentinel_sha256"],
        "subject_relative_root": contract.subject_relative_root.as_posix(),
        "package_results": package_results,
        "retained_package_ids": retained_package_ids,
        "display_closures": [display_closures[package_id] for package_id in completed_package_ids],
        "sentence_support_status": sentence_support_status,
        "sentence_support_refresh": sentence_support_refresh,
        "sentence_support_closures": (
            [
                sentence_support_closures[package_id]
                for package_id in completed_package_ids
            ]
            if sentence_support_status == "closed"
            else []
        ),
        "verified_at": verified_at,
        "formal_write_count": 0,
        "local_cleanup_authorized": True,
    }
    receipt = {
        **receipt_core,
        "receipt_id": "EN-ARCHIVE-" + object_sha256(receipt_core)[:16].upper(),
    }
    if archive_receipt_path.exists():
        receipt = load_json(archive_receipt_path)
    else:
        atomic_write_json(archive_receipt_path, receipt)
    reopened_receipt = load_json(archive_receipt_path)
    if reopened_receipt.get("status") != "PASS":
        raise ValidationError("archive receipt did not reopen as PASS")
    if fault_at == "after_archive_receipt":
        raise SimulatedArchiveFailure(str(archive_receipt_path))

    cleanup_results: list[dict[str, Any]] = []
    if cleanup_local:
        for result in package_results:
            local_root = Path(result["local_path"])
            if not local_root.exists():
                pointer, cleanup_receipt_path = _resume_cleanup_after_local_delete(
                    state_dir,
                    manifest["study_date"],
                    result,
                    archive_receipt_path=archive_receipt_path,
                )
                cleanup_results.append(
                    {
                        "package_id": result["package_id"],
                        "status": "idempotent_noop",
                        "pointer_path": str(pointer),
                        "cleanup_receipt_path": str(cleanup_receipt_path),
                    }
                )
                continue
            pointer_path, cleanup_receipt_path = _cleanup_local_package(
                state_dir,
                local_root,
                package_result=result,
                archive_receipt_path=archive_receipt_path,
                locator_path=Path(result["locator_path"]),
                fault_at=fault_at,
            )
            cleanup_results.append(
                {
                    "package_id": result["package_id"],
                    "status": "cleaned",
                    "pointer_path": str(pointer_path),
                    "cleanup_receipt_path": str(cleanup_receipt_path),
                }
            )
    # All support/display/archive/cleanup evidence is now durable and verified.
    # Use only existing content/dependency hashes and cleanup membership; retries
    # do not gain a new publication identity from timestamps or copy status.
    publication_basis = _archive_publication_basis(receipt, [row["package_id"] for row in cleanup_results])
    from .publication import complete_formal_closeout
    publication = complete_formal_closeout(
        repo_root, state_dir.resolve(), writer_receipt, runner=publication_runner,
        archive_basis_sha256=object_sha256(publication_basis), retry_publication=retry_publication,
    )
    return {
        "schema_version": "english_archive_workflow_receipt_v1",
        "status": (
            "PARTIAL"
            if retained_package_ids
            else ("COMPLETE" if cleanup_local else "ARCHIVE_VERIFIED")
        ),
        "batch_id": manifest["batch_id"],
        "archive_receipt_path": str(archive_receipt_path),
        "archive_receipt_sha256": file_sha256(archive_receipt_path),
        "package_results": package_results,
        "cleanup_results": cleanup_results,
        "retained_package_ids": retained_package_ids,
        "publication": publication,
        "formal_write_count": 0,
    }


def reopen_archived_package(
    repo_root: Path,
    *,
    package_id: str,
    message_sequences: list[int] | None = None,
    attachment_roles: list[str] | None = None,
    contract: VolumeContract = VolumeContract(),
) -> dict[str, Any]:
    if not re.fullmatch(r"EN-PKG-[0-9]{8}-[A-F0-9]{16}", package_id):
        raise ValidationError("archive reopen requires an exact package_id")
    volume = verify_volume_contract(contract)
    locator_path = repo_root.resolve() / "wiki" / "raw_archives" / f"{package_id}.md"
    if not locator_path.is_file():
        raise ValidationError("exact raw archive locator note is missing")
    locator_text = locator_path.read_text(encoding="utf-8")
    locator = _parse_locator(locator_text)
    required = {
        "type": "raw_archive_locator",
        "subject": "english",
        "package_id": package_id,
        "archive_volume": "T9-Data",
        "archive_status": "verified",
    }
    for key, value in required.items():
        if locator.get(key) != value:
            raise ValidationError(f"raw archive locator field mismatch: {key}")
    relative_value = locator.get("raw_archive_relpath")
    if not isinstance(relative_value, str) or not relative_value:
        raise ValidationError("raw archive locator lacks relative path")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("raw archive locator path is not safe relative data")
    archived_root = (volume["archive_root"] / relative).resolve(strict=True)
    try:
        archived_root.relative_to(volume["subject_root"])
    except ValueError as exc:
        raise ValidationError("raw archive locator escapes the English archive root") from exc
    if archived_root.name != package_id:
        raise ValidationError("raw archive locator terminal package identity mismatch")
    manifest = validate_conversation_package(archived_root)
    if manifest["package_id"] != package_id:
        raise ValidationError("archived package ID mismatch")
    if file_sha256(archived_root / "manifest.json") != locator.get("raw_archive_manifest_sha256"):
        raise ValidationError("archived manifest SHA does not match locator")
    if manifest["package_canonical_sha256"] != locator.get("raw_archive_package_sha256"):
        raise ValidationError("archived package SHA does not match locator")
    retrieval_keys = locator.get("retrieval_keys")
    if (
        not isinstance(retrieval_keys, list)
        or package_id not in retrieval_keys
        or manifest.get("study_date") not in retrieval_keys
    ):
        raise ValidationError("archived package locator retrieval_keys are incomplete")

    selected_messages: list[dict[str, Any]] = []
    requested_sequences = message_sequences or []
    if any(not isinstance(value, int) or value < 1 for value in requested_sequences):
        raise ValidationError("message sequences must be positive integers")
    if requested_sequences:
        conversation = load_json(archived_root / "conversation.json")
        by_sequence = {
            row["sequence"]: row for row in conversation.get("messages", [])
            if isinstance(row, dict)
        }
        missing = [value for value in requested_sequences if value not in by_sequence]
        if missing:
            raise ValidationError(f"requested archived message sequences are missing: {missing}")
        selected_messages = [by_sequence[value] for value in requested_sequences]

    allowed_roles = {
        "question_image", "solution_image", "explanation_image",
        "user_work_image", "source_article_image", "other_attachment",
    }
    requested_roles = attachment_roles or []
    if any(role not in allowed_roles for role in requested_roles):
        raise ValidationError("requested archived attachment role is invalid")
    selected_attachments: list[dict[str, Any]] = []
    if requested_roles:
        for row in manifest.get("attachments", []):
            if row.get("role") not in requested_roles:
                continue
            path = archived_root / row["path"]
            selected_attachments.append(
                {
                    **row,
                    "archive_relative_path": path.relative_to(volume["archive_root"]).as_posix(),
                    "protected": row.get("role") in {"solution_image", "explanation_image"},
                    "content_returned": False,
                }
            )
    return {
        "schema_version": "english_archive_reopen_receipt_v1",
        "status": "PASS",
        "package_id": package_id,
        "package_sha256": manifest["package_canonical_sha256"],
        "manifest_sha256": file_sha256(archived_root / "manifest.json"),
        "locator_path": str(locator_path),
        "locator_sha256": file_sha256(locator_path),
        "archive_relative_path": relative.as_posix(),
        "retrieval_keys": retrieval_keys,
        "selected_messages": selected_messages,
        "selected_attachments": selected_attachments,
        "whole_volume_scanned": False,
        "formal_write_count": 0,
    }
