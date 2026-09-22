from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .errors import IdempotencyConflict, ValidationError
from .util import (
    atomic_write_json,
    atomic_write_text,
    bytes_sha256,
    canonical_bytes,
    file_sha256,
    load_json,
    object_sha256,
    parse_iso_date,
    utc_now,
    exclusive_lock,
)


PACKAGE_SCHEMA_VERSION = "english_conversation_package_v1"
CONVERSATION_SCHEMA_VERSION = "english_conversation_v1"
SOURCE_SCHEMA_VERSION = "english_conversation_source_v1"
PACKAGE_RECEIPT_SCHEMA_VERSION = "english_package_receipt_v2"
UNIT_RECEIPT_SCHEMA_VERSION = "english_package_receipt_v3"
LEGACY_PACKAGE_RECEIPT_SCHEMA_VERSION = "english_package_receipt_v1"
SEGMENT_FRONTIER_SCHEMA_VERSION = "english_segment_frontier_event_v1"
SEGMENT_FRONTIER_CURRENT_SCHEMA_VERSION = "english_segment_frontier_current_v1"
CONTINUATION_TOKEN_SCHEMA_VERSION = "english_segment_continuation_token_v1"

_PACKAGE_ID_RE = re.compile(r"^EN-PKG-[0-9]{8}-[A-F0-9]{16}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MESSAGE_ROLES = {"user", "assistant"}
_ATTACHMENT_ROLES = {
    "question_image",
    "solution_image",
    "explanation_image",
    "user_work_image",
    "source_article_image",
    "other_attachment",
}


class SegmentCapturePending(ValidationError):
    """A sealed segment exists, but its continuation gate is not safe to use."""

    def __init__(self, message: str, *, package_id: str | None = None) -> None:
        super().__init__(message)
        self.package_id = package_id


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _coordinated_lock(path: Path, *, deadline: float | None = None) -> Iterable[Path]:
    """Combine a process-local mutex with the existing cross-process file lock."""
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        mutex = _PROCESS_LOCKS.setdefault(key, threading.Lock())
    acquired = mutex.acquire() if deadline is None else mutex.acquire(timeout=max(0, deadline - time.monotonic()))
    if not acquired:
        raise SegmentCapturePending("local capture budget expired while waiting for this thread")
    try:
        with exclusive_lock(path, deadline=deadline):
            yield path
    finally:
        mutex.release()


def _check_deadline(deadline: float | None, package_id: str | None = None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise SegmentCapturePending("local capture budget expired; preserve the exact segment for recovery", package_id=package_id)


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise IdempotencyConflict(f"immutable package file already exists: {path}") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_bytes(value: Any) -> bytes:
    return canonical_bytes(value)


def _safe_filename(index: int, original: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", Path(original).name).strip("._") or "attachment.bin"
    return f"{index:03d}-{cleaned}"


def _normalize_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("messages")
    if not isinstance(value, list) or not value:
        raise ValidationError("capture conversation must contain at least one user/assistant message")
    messages: list[dict[str, Any]] = []
    for sequence, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"conversation message {sequence} must be an object")
        role = raw.get("role")
        content = raw.get("content")
        if role not in _MESSAGE_ROLES:
            raise ValidationError(f"conversation message {sequence} role must be user or assistant")
        if not isinstance(content, str):
            raise ValidationError(f"conversation message {sequence} content must be a string")
        message: dict[str, Any] = {
            "sequence": sequence,
            "role": role,
            "content": content,
        }
        for field in ("message_id", "created_at"):
            if field in raw and raw[field] is not None:
                message[field] = raw[field]
        attachment_refs = raw.get("attachment_refs", [])
        if not isinstance(attachment_refs, list) or any(
            not isinstance(item, str) or not item for item in attachment_refs
        ):
            raise ValidationError(f"conversation message {sequence} attachment_refs are invalid")
        message["attachment_refs"] = list(attachment_refs)
        metadata = raw.get("metadata")
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise ValidationError(f"conversation message {sequence} metadata must be an object")
            message["metadata"] = metadata
        messages.append(message)
    return messages


def _validate_attachment_links(messages: list[dict[str, Any]], attachments: Any) -> None:
    """Validate declared links without inventing links for package-level evidence."""
    if not isinstance(attachments, list):
        raise ValidationError("conversation package attachments must be an array")
    by_id: dict[str, dict[str, Any]] = {}
    for index, attachment in enumerate(attachments, start=1):
        if not isinstance(attachment, dict):
            raise ValidationError(f"attachment {index} must be an object")
        attachment_id = attachment.get("attachment_id")
        if attachment_id is not None:
            if not isinstance(attachment_id, str) or not attachment_id:
                raise ValidationError(f"attachment {index} attachment_id is invalid")
            if attachment_id in by_id:
                raise ValidationError(f"duplicate attachment_id: {attachment_id}")
            by_id[attachment_id] = attachment
        sequence = attachment.get("message_sequence")
        if sequence is not None and (type(sequence) is not int or not 1 <= sequence <= len(messages)):
            raise ValidationError(f"attachment {index} message_sequence must be an integer in 1..{len(messages)}")
    referenced_by: dict[str, set[int]] = {}
    for message in messages:
        for attachment_id in message["attachment_refs"]:
            if attachment_id not in by_id:
                raise ValidationError(f"conversation message {message['sequence']} attachment_refs references unknown attachment: {attachment_id}")
            referenced_by.setdefault(attachment_id, set()).add(message["sequence"])
    for attachment_id, sequences in referenced_by.items():
        sequence = by_id[attachment_id].get("message_sequence")
        if sequence is not None and sequence not in sequences:
            raise ValidationError(f"attachment {attachment_id} message_sequence conflicts with attachment_refs")


def _attachment_inputs(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("attachments must be an array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"attachment {index} must be an object")
        path_value = item.get("path")
        role = item.get("role", "other_attachment")
        if not isinstance(path_value, str) or not path_value:
            raise ValidationError(f"attachment {index} path is required")
        if role not in _ATTACHMENT_ROLES:
            raise ValidationError(f"attachment {index} role is invalid")
        path = Path(path_value).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValidationError(f"attachment {index} must be a regular file")
        result.append({**item, "path": path, "role": role})
    return result


def _source_document(request: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    supplied = request.get("source")
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise ValidationError("capture source must be an object")
    source = {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "subject": "english",
        "identity": json.loads(json.dumps(supplied, ensure_ascii=False)),
    }
    missing = list(request.get("missing_fields", []))
    if any(not isinstance(item, str) or not item for item in missing):
        raise ValidationError("missing_fields must contain nonempty strings")
    identity = source["identity"]
    if not identity.get("source_id"):
        missing.append("source.source_id")
    if not identity.get("source_hash"):
        missing.append("source.source_hash")
    if not identity.get("source_kind"):
        missing.append("source.source_kind")
    if identity.get("answer_exposure") not in {"answer_free", "protected"}:
        missing.append("source.answer_exposure")
    if not any(
        identity.get(field)
        for field in ("paragraph_id", "sentence_id", "knowledge_point_id", "question_id")
    ):
        missing.append("source.segment_identity")
    return source, sorted(set(missing))


def _validate_state_root(state_dir: Path) -> Path:
    resolved = state_dir.expanduser().resolve()
    try:
        resolved.relative_to(Path("/Volumes"))
    except ValueError:
        return resolved
    raise ValidationError(
        "foreground packages must use local high-speed staging, not a mounted data volume"
    )


def _request_segment_identity(request: dict[str, Any]) -> tuple[str, str, str]:
    idempotency_key = request.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValidationError("idempotency_key is required")
    segment_key = request.get("segment_key")
    if not isinstance(segment_key, str) or not segment_key.strip():
        raise ValidationError("segment_key must be a nonempty string")
    if segment_key != idempotency_key:
        raise ValidationError("segment_key must equal idempotency_key")
    thread_ref = request.get("thread_ref")
    if not isinstance(thread_ref, str) or not thread_ref.strip():
        raise ValidationError("thread_ref must be a nonempty string")
    return idempotency_key.strip(), segment_key.strip(), bytes_sha256(thread_ref.strip().encode("utf-8"))


def _normalize_previous_token(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise ValidationError("previous_token must be the complete continuation-token object")
    required = {
        "schema_version", "package_id", "package_sha256", "manifest_sha256",
        "segment_key", "captured_at", "thread_ref_sha256", "previous_token_sha256",
    }
    if set(value) != required or value.get("schema_version") != CONTINUATION_TOKEN_SCHEMA_VERSION:
        raise ValidationError("previous_token shape or schema_version is invalid")
    for field in ("package_sha256", "manifest_sha256", "thread_ref_sha256"):
        if not _SHA256_RE.fullmatch(str(value.get(field, ""))):
            raise ValidationError(f"previous_token {field} is invalid")
    previous = value.get("previous_token_sha256")
    if previous is not None and not _SHA256_RE.fullmatch(str(previous)):
        raise ValidationError("previous_token previous_token_sha256 is invalid")
    return json.loads(json.dumps(value)), object_sha256(value)


def _frontier_log_paths(state_dir: Path, thread_ref_sha256: str) -> list[Path]:
    return sorted(
        (state_dir / "segment-frontier").glob(
            f"[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/{thread_ref_sha256}.jsonl"
        )
    )


def _validate_frontier_package_binding(
    state_dir: Path,
    event: dict[str, Any],
) -> None:
    package_root = (
        state_dir / "packages" / str(event.get("study_date", ""))
        / str(event.get("package_id", ""))
    )
    try:
        effective_root = package_root
        if not package_root.is_dir():
            from .archive import resolve_archived_package_from_pointer

            effective_root = resolve_archived_package_from_pointer(
                state_dir,
                state_dir.resolve().parent,
                study_date=str(event.get("study_date", "")),
                package_id=str(event.get("package_id", "")),
                expected_package_sha256=str(event.get("package_sha256", "")),
                expected_manifest_sha256=str(event.get("manifest_sha256", "")),
            )
        manifest = validate_conversation_package(effective_root)
        receipt = read_package_receipt(effective_root)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise SegmentCapturePending(
            f"segment frontier package cannot be revalidated: {package_root}",
            package_id=str(event.get("package_id") or "") or None,
        ) from exc
    expected = {
        "package_id": manifest["package_id"],
        "package_sha256": manifest["package_canonical_sha256"],
        "manifest_sha256": file_sha256(effective_root / "manifest.json"),
        "segment_key": manifest.get("segment_key"),
        "request_sha256": manifest.get("request_sha256"),
        "thread_ref_sha256": manifest.get("thread_ref_sha256"),
        "captured_at": manifest.get("created_at"),
        "previous_token_sha256": manifest.get("previous_token_sha256"),
        "continuation_token": receipt.get("continuation_token"),
        "token_sha256": receipt.get("token_sha256"),
    }
    if any(event.get(key) != value for key, value in expected.items()):
        raise SegmentCapturePending(
            "segment frontier package/manifest/receipt binding mismatch",
            package_id=manifest["package_id"],
        )


def _load_frontier_events(state_dir: Path, thread_ref_sha256: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    prior_sha: str | None = None
    seen_keys: dict[str, str] = {}
    for path in _frontier_log_paths(state_dir, thread_ref_sha256):
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.endswith("\n"):
                    raise SegmentCapturePending(
                        f"segment frontier has an incomplete final record: {path}:{line_number}"
                    )
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SegmentCapturePending(
                        f"segment frontier record is invalid JSON: {path}:{line_number}"
                    ) from exc
                if not isinstance(event, dict) or event.get("schema_version") != SEGMENT_FRONTIER_SCHEMA_VERSION:
                    raise SegmentCapturePending("segment frontier schema_version mismatch")
                if event.get("thread_ref_sha256") != thread_ref_sha256:
                    raise SegmentCapturePending("segment frontier thread binding mismatch")
                token = event.get("continuation_token")
                if not isinstance(token, dict) or object_sha256(token) != event.get("token_sha256"):
                    raise SegmentCapturePending("segment frontier token hash mismatch")
                if token.get("previous_token_sha256") != prior_sha:
                    raise SegmentCapturePending("segment frontier token chain is discontinuous")
                key = event.get("segment_key")
                request_sha = event.get("request_sha256")
                if not isinstance(key, str) or not _SHA256_RE.fullmatch(str(request_sha)):
                    raise SegmentCapturePending("segment frontier segment binding is invalid")
                if key in seen_keys:
                    raise SegmentCapturePending("segment frontier repeats a segment_key")
                _validate_frontier_package_binding(state_dir, event)
                seen_keys[key] = request_sha
                prior_sha = event["token_sha256"]
                events.append(event)
    return events


def _current_projection_path(state_dir: Path) -> Path:
    return state_dir / "segment-frontier" / "current.json"


def _projection_entry(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread_ref_sha256": event["thread_ref_sha256"],
        "study_date": event["study_date"],
        "segment_key": event["segment_key"],
        "request_sha256": event["request_sha256"],
        "package_id": event["package_id"],
        "package_sha256": event["package_sha256"],
        "manifest_sha256": event["manifest_sha256"],
        "captured_at": event["captured_at"],
        "token_sha256": event["token_sha256"],
        "continuation_token": event["continuation_token"],
    }


def _load_current_projection(state_dir: Path) -> dict[str, Any]:
    path = _current_projection_path(state_dir)
    if not path.is_file():
        return {
            "schema_version": SEGMENT_FRONTIER_CURRENT_SCHEMA_VERSION,
            "updated_at": None,
            "threads": {},
            "formal_write_count": 0,
            "background_processing": "none",
        }
    try:
        current = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SegmentCapturePending("segment frontier current projection is unreadable") from exc
    if (
        current.get("schema_version") != SEGMENT_FRONTIER_CURRENT_SCHEMA_VERSION
        or not isinstance(current.get("threads"), dict)
        or current.get("formal_write_count") != 0
        or current.get("background_processing") != "none"
    ):
        raise SegmentCapturePending("segment frontier current projection is invalid")
    return current


def _require_projection_matches(
    state_dir: Path,
    thread_ref_sha256: str,
    events: list[dict[str, Any]],
) -> None:
    path = _current_projection_path(state_dir)
    if not events:
        if path.is_file():
            _load_current_projection(state_dir)
        return
    current = _load_current_projection(state_dir)
    if current["threads"].get(thread_ref_sha256) != _projection_entry(events[-1]):
        raise SegmentCapturePending(
            "segment frontier current projection is missing or stale; run recover-segment-gate"
        )


def _write_current_projection(
    state_dir: Path,
    thread_ref_sha256: str,
    event: dict[str, Any],
    *,
    allow_reset: bool = False,
) -> None:
    try:
        current = _load_current_projection(state_dir)
    except SegmentCapturePending:
        if not allow_reset:
            raise
        current = {
            "schema_version": SEGMENT_FRONTIER_CURRENT_SCHEMA_VERSION,
            "updated_at": None,
            "threads": {},
            "formal_write_count": 0,
            "background_processing": "none",
        }
    current["threads"][thread_ref_sha256] = _projection_entry(event)
    current["updated_at"] = utc_now()
    atomic_write_json(_current_projection_path(state_dir), current)
    reread = _load_current_projection(state_dir)
    if reread["threads"].get(thread_ref_sha256) != _projection_entry(event):
        raise SegmentCapturePending("segment frontier current projection verification failed")


def _append_frontier_event(state_dir: Path, event: dict[str, Any]) -> Path:
    path = (
        state_dir / "segment-frontier" / event["study_date"]
        / f"{event['thread_ref_sha256']}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(event) + b"\n"
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("short append to segment frontier")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path


def _frontier_event_from_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    token = receipt["continuation_token"]
    return {
        "schema_version": SEGMENT_FRONTIER_SCHEMA_VERSION,
        "event_type": "segment_committed",
        "thread_ref_sha256": receipt["thread_ref_sha256"],
        "study_date": receipt["study_date"],
        "segment_key": receipt["segment_key"],
        "request_sha256": receipt["request_sha256"],
        "package_id": receipt["package_id"],
        "package_sha256": receipt["package_sha256"],
        "manifest_sha256": receipt["manifest_sha256"],
        "captured_at": receipt["captured_at"],
        "previous_token_sha256": receipt["previous_token_sha256"],
        "continuation_token": token,
        "token_sha256": object_sha256(token),
        "formal_write_count": 0,
        "background_processing": "none",
    }


def create_conversation_package(
    state_dir: Path,
    request: dict[str, Any],
    *,
    fault_after_package: bool = False,
    fault_after_frontier_append: bool = False,
    deadline: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    _check_deadline(deadline)
    resolved_state = _validate_state_root(state_dir)
    _, segment_key, thread_ref_sha256 = _request_segment_identity(request)
    capture_mode = request.get("capture_mode", "thread_segment")
    if capture_mode not in {"thread_segment", "independent_unit"}:
        raise ValidationError("unknown capture_mode")
    independent = capture_mode == "independent_unit"
    if independent and request.get("previous_token") is not None:
        raise ValidationError("an independent unit does not accept a continuation token")
    occurred_at = str(request.get("occurred_at") or utc_now())
    capture_date = parse_iso_date(occurred_at)
    study_date = str(request.get("study_date") or capture_date)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", study_date) or parse_iso_date(study_date) != study_date or study_date > capture_date:
        raise ValidationError("original study_date must be a valid date no later than actual capture time")
    messages = _normalize_messages(request.get("conversation"))
    attachments = _attachment_inputs(request.get("attachments"))
    source, missing_fields = _source_document(request)
    if independent:
        dates = []
        for message in messages:
            if message.get("created_at"):
                stamp = datetime.fromisoformat(message["created_at"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValidationError("unit message timestamps must include a timezone")
                dates.append(stamp.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat())
        if study_date != capture_date and (not dates or min(dates) != study_date):
            raise ValidationError("original unit study_date must match its actual message timestamps")
    elif study_date != capture_date:
        identity_source = source["identity"]
        supplements = identity_source.get("supplement_of")
        web = identity_source.get("web_result", {})
        if (not isinstance(supplements, list) or not supplements or len(supplements) > 128
                or not isinstance(web, dict) or web.get("origin") not in {"project-A", "project-E"}):
            raise ValidationError("a different original study date requires an exact A-return supplement binding")
        for original in supplements:
            if not isinstance(original, dict) or set(original) != {"package_id", "package_sha256"} or not _PACKAGE_ID_RE.fullmatch(str(original.get("package_id", ""))):
                raise ValidationError("original supplement package identity is invalid")
            original_root = resolved_state / "packages" / study_date / original["package_id"]
            original_manifest = validate_conversation_package(original_root)
            if (original_manifest["package_canonical_sha256"] != original["package_sha256"]
                    or original_manifest["study_date"] != study_date
                    or original_manifest.get("source_identity", {}).get("source_id") != identity_source.get("source_id")):
                raise ValidationError("A-return supplement differs from the original local package")
            if original_manifest.get("thread_ref_sha256") == thread_ref_sha256:
                raise ValidationError("A-return supplement uses its own stable processing thread, not the original learning thread")
    expected_attachments = request.get("expected_attachment_roles", [])
    if not isinstance(expected_attachments, list) or any(
        role not in _ATTACHMENT_ROLES for role in expected_attachments
    ):
        raise ValidationError("expected_attachment_roles contains an invalid role")
    present_roles = {item["role"] for item in attachments}
    missing_fields.extend(
        f"attachments.{role}" for role in expected_attachments if role not in present_roles
    )
    missing_fields = sorted(set(missing_fields))
    previous_token, previous_token_sha256 = _normalize_previous_token(request.get("previous_token"))

    attachment_descriptors: list[dict[str, Any]] = []
    attachment_payloads: list[tuple[Path, bytes]] = []
    for index, item in enumerate(attachments, start=1):
        _check_deadline(deadline)
        path = item["path"]
        data = path.read_bytes()
        _check_deadline(deadline)
        relative = Path("attachments") / _safe_filename(index, path.name)
        mime = item.get("mime_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        descriptor: dict[str, Any] = {
            "attachment_id": f"ATT-{index:03d}",
            "path": relative.as_posix(),
            "role": item["role"],
            "mime_type": mime,
            "bytes": len(data),
            "sha256": bytes_sha256(data),
        }
        if item.get("message_sequence") is not None:
            descriptor["message_sequence"] = item["message_sequence"]
        attachment_descriptors.append(descriptor)
        attachment_payloads.append((relative, data))

    _validate_attachment_links(messages, attachment_descriptors)
    conversation = {
        "schema_version": CONVERSATION_SCHEMA_VERSION,
        "messages": messages,
        "message_count": len(messages),
    }
    conversation_bytes = _json_bytes(conversation)
    source_bytes = _json_bytes(source)
    component_map = {
        "conversation.json": {
            "role": "conversation", "mime_type": "application/json",
            "bytes": len(conversation_bytes), "sha256": bytes_sha256(conversation_bytes),
        },
        "source.json": {
            "role": "source_identity", "mime_type": "application/json",
            "bytes": len(source_bytes), "sha256": bytes_sha256(source_bytes),
        },
        **{
            row["path"]: {
                "role": row["role"], "mime_type": row["mime_type"],
                "bytes": row["bytes"], "sha256": row["sha256"],
            }
            for row in attachment_descriptors
        },
    }
    request_sha256 = object_sha256(
        {
            "source": source,
            "conversation_sha256": component_map["conversation.json"]["sha256"],
            "attachments": attachment_descriptors,
            "missing_fields": missing_fields,
            **({"study_date": study_date} if independent or study_date != capture_date else {}),
            **({"capture_mode": capture_mode} if independent else {}),
        }
    )
    identity = object_sha256(
        {"thread_ref_sha256": thread_ref_sha256, "segment_key": segment_key,
         "request_sha256": request_sha256}
    )[:16].upper()
    package_id = f"EN-PKG-{study_date.replace('-', '')}-{identity}"
    package_sha256 = object_sha256(
        {
            "schema_version": PACKAGE_SCHEMA_VERSION, "package_id": package_id,
            "subject": "english", "study_date": study_date, "files": component_map,
        }
    )
    manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_id": package_id,
        "subject": "english",
        "study_date": study_date,
        "timezone": "Asia/Shanghai",
        "created_at": occurred_at,
        "segment_key": segment_key,
        "request_sha256": request_sha256,
        "thread_ref_sha256": thread_ref_sha256,
        "previous_token_sha256": previous_token_sha256,
        "source_identity": source["identity"],
        "files": component_map,
        "attachments": attachment_descriptors,
        "missing_fields": missing_fields,
        "package_canonical_sha256": package_sha256,
        "formal_write_count": 0,
        "background_processing": "none",
        "model_call_count": 0,
        "mcp_call_count": 0,
    }
    if independent:
        manifest["capture_mode"] = capture_mode
        del manifest["previous_token_sha256"]
    manifest_bytes = _json_bytes(manifest)
    manifest_sha256 = bytes_sha256(manifest_bytes)
    continuation_token = {
        "schema_version": CONTINUATION_TOKEN_SCHEMA_VERSION,
        "package_id": package_id,
        "package_sha256": package_sha256,
        "manifest_sha256": manifest_sha256,
        "segment_key": segment_key,
        "captured_at": occurred_at,
        "thread_ref_sha256": thread_ref_sha256,
        "previous_token_sha256": previous_token_sha256,
    }
    receipt = {
        "schema_version": PACKAGE_RECEIPT_SCHEMA_VERSION,
        "receipt_id": f"EN-PACKAGE-RECEIPT-{identity}",
        "package_id": package_id,
        "package_sha256": package_sha256,
        "manifest_sha256": manifest_sha256,
        "segment_key": segment_key,
        "request_sha256": request_sha256,
        "thread_ref_sha256": thread_ref_sha256,
        "captured_at": occurred_at,
        "previous_token_sha256": previous_token_sha256,
        "continuation_token": continuation_token,
        "token_sha256": object_sha256(continuation_token),
        "segment_gate_status": "package_sealed",
        "status": "created",
        "replayed": False,
        "study_date": study_date,
        "missing_fields": missing_fields,
        "formal_write_count": 0,
        "background_processing": "none",
    }
    if independent:
        receipt.update(schema_version=UNIT_RECEIPT_SCHEMA_VERSION,
                       capture_mode=capture_mode, segment_gate_status="ready")
        for key in ("previous_token_sha256", "continuation_token", "token_sha256"):
            del receipt[key]
    package_root = resolved_state / "packages" / study_date / package_id
    receipt["package_path"] = str(package_root)
    if independent:
        # The binding belongs to this attempt only; another unit cannot block it.
        unit_key = object_sha256([thread_ref_sha256, segment_key])
        binding_path = resolved_state / "unit-captures" / f"{unit_key}.json"
        binding = {"schema_version": "english_unit_capture_binding_v1",
                   "thread_ref_sha256": thread_ref_sha256, "segment_key": segment_key,
                   "request_sha256": request_sha256, "package_id": package_id,
                   "study_date": study_date}
        with _coordinated_lock(resolved_state / "locks" / "units" / f"{unit_key}.lock", deadline=deadline):
            if binding_path.exists():
                if load_json(binding_path) != binding:
                    raise IdempotencyConflict("this unit already binds different evidence; save a linked supplement")
            else:
                _write_immutable(binding_path, _json_bytes(binding))
            pointer = resolved_state / "archive-pointers" / study_date / f"{package_id}.json"
            if not package_root.exists() and pointer.exists():
                from .archive import resolve_archived_package_from_pointer
                package_root = resolve_archived_package_from_pointer(
                    resolved_state, resolved_state.parent, study_date=study_date,
                    package_id=package_id, expected_package_sha256=package_sha256,
                )
            replayed = package_root.exists()
            stored_receipt = _seal_package(
                package_root, receipt, manifest_bytes, conversation_bytes, source_bytes,
                attachment_payloads, deadline=deadline,
            )
            if fault_after_package:
                raise SegmentCapturePending("unit is sealed; retry the same unit to retrieve its receipt", package_id=package_id)
            return package_root, {**stored_receipt, "status": "idempotent_noop" if replayed else "created",
                                  "replayed": replayed, "segment_gate_status": "ready"}
    thread_lock = resolved_state / "locks" / "segment-frontier" / f"{thread_ref_sha256}.lock"
    with _coordinated_lock(thread_lock, deadline=deadline):
        events = _load_frontier_events(resolved_state, thread_ref_sha256)
        _require_projection_matches(resolved_state, thread_ref_sha256, events)
        _check_deadline(deadline, package_id)
        by_segment = {event["segment_key"]: event for event in events}
        existing_event = by_segment.get(segment_key)
        if existing_event is not None:
            if existing_event["request_sha256"] != request_sha256:
                raise IdempotencyConflict(
                    f"segment_key {segment_key!r} already binds "
                    f"{existing_event['request_sha256']}; proposed {request_sha256}"
                )
            existing_root = (
                resolved_state / "packages" / existing_event["study_date"]
                / existing_event["package_id"]
            )
            if not existing_root.is_dir():
                from .archive import resolve_archived_package_from_pointer
                existing_root = resolve_archived_package_from_pointer(
                    resolved_state, resolved_state.parent, study_date=existing_event["study_date"],
                    package_id=existing_event["package_id"], expected_package_sha256=existing_event["package_sha256"],
                    expected_manifest_sha256=existing_event["manifest_sha256"],
                )
            validate_conversation_package(existing_root)
            replay = read_package_receipt(existing_root)
            replay.update(
                {
                    "status": "idempotent_noop",
                    "replayed": True,
                    "segment_gate_status": "ready",
                    "continuation_token": existing_event["continuation_token"],
                    "token_sha256": existing_event["token_sha256"],
                    "package_path": str(existing_root),
                }
            )
            return existing_root, replay

        current_event = events[-1] if events else None
        if current_event and study_date < current_event["study_date"]:
            raise ValidationError("a dated supplement cannot regress an existing thread's study-date order")
        expected_previous = current_event["token_sha256"] if current_event else None
        if previous_token_sha256 != expected_previous:
            raise ValidationError(
                "previous continuation token does not match the current thread frontier"
            )
        if previous_token is not None:
            if previous_token.get("thread_ref_sha256") != thread_ref_sha256:
                raise ValidationError("previous continuation token belongs to another thread")
            if current_event is None or previous_token != current_event["continuation_token"]:
                raise ValidationError("previous continuation token bytes do not match the frontier")

        with _coordinated_lock(resolved_state / "locks" / "packages.lock", deadline=deadline):
            stored_receipt = _seal_package(
                package_root, receipt, manifest_bytes, conversation_bytes, source_bytes,
                attachment_payloads, deadline=deadline,
            )
        _check_deadline(deadline, package_id)
        if fault_after_package:
            raise SegmentCapturePending(
                "package sealed but segment frontier was not appended",
                package_id=package_id,
            )
        event = _frontier_event_from_receipt(stored_receipt)
        _append_frontier_event(resolved_state, event)
        if fault_after_frontier_append:
            raise SegmentCapturePending(
                "segment frontier appended but current projection was not updated",
                package_id=package_id,
            )
        _write_current_projection(resolved_state, thread_ref_sha256, event)
        ready = dict(stored_receipt)
        ready["segment_gate_status"] = "ready"
        ready["frontier_path"] = str(
            resolved_state / "segment-frontier" / study_date / f"{thread_ref_sha256}.jsonl"
        )
        ready["current_projection_path"] = str(_current_projection_path(resolved_state))
        return package_root, ready


def _seal_package(
    package_root: Path, receipt: dict[str, Any], manifest_bytes: bytes,
    conversation_bytes: bytes, source_bytes: bytes,
    attachment_payloads: list[tuple[Path, bytes]], *, deadline: float | None,
) -> dict[str, Any]:
    package_id = receipt["package_id"]
    if not package_root.exists():
        package_root.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".staging-{package_id}-", dir=package_root.parent))
        try:
            _write_immutable(staging / "conversation.json", conversation_bytes)
            _write_immutable(staging / "source.json", source_bytes)
            for relative, data in attachment_payloads:
                _check_deadline(deadline, package_id)
                _write_immutable(staging / relative, data)
            _write_immutable(staging / "manifest.json", manifest_bytes)
            _write_immutable(staging / "receipt.json", _json_bytes(receipt))
            validate_conversation_package(staging)
            _check_deadline(deadline, package_id)
            os.rename(staging, package_root)
            parent_fd = os.open(package_root.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
    stored = read_package_receipt(package_root)
    if stored["package_sha256"] != receipt["package_sha256"] or stored["request_sha256"] != receipt["request_sha256"]:
        raise IdempotencyConflict(f"package request binding collision for {package_id}")
    return stored


def validate_conversation_package(package_root: Path) -> dict[str, Any]:
    package_root = package_root.resolve(strict=True)
    if not package_root.is_dir():
        raise ValidationError(f"conversation package root is not a directory: {package_root}")
    manifest_path = package_root / "manifest.json"
    conversation_path = package_root / "conversation.json"
    source_path = package_root / "source.json"
    receipt_path = package_root / "receipt.json"
    for path in (manifest_path, conversation_path, source_path, receipt_path):
        if not path.is_file():
            raise ValidationError(f"conversation package file missing: {path.name}")
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise ValidationError("conversation package schema_version mismatch")
    if manifest.get("subject") != "english" or not _PACKAGE_ID_RE.fullmatch(
        str(manifest.get("package_id", ""))
    ):
        raise ValidationError("conversation package identity is invalid")
    if manifest.get("formal_write_count") != 0 or manifest.get("background_processing") != "none":
        raise ValidationError("conversation package must remain zero-write and foreground-only")
    files = manifest.get("files")
    if not isinstance(files, dict) or "conversation.json" not in files or "source.json" not in files:
        raise ValidationError("conversation package files map is incomplete")
    for relative, descriptor in files.items():
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValidationError("conversation package contains unsafe file locator")
        path = (package_root / relative).resolve(strict=True)
        try:
            path.relative_to(package_root)
        except ValueError as exc:
            raise ValidationError("conversation package file escapes package root") from exc
        if not path.is_file() or file_sha256(path) != descriptor.get("sha256"):
            raise ValidationError(f"conversation package file drift: {relative}")
        if path.stat().st_size != descriptor.get("bytes"):
            raise ValidationError(f"conversation package byte count drift: {relative}")
    conversation = load_json(conversation_path)
    messages = _normalize_messages(conversation)
    _validate_attachment_links(messages, manifest.get("attachments", []))
    source = load_json(source_path)
    if source.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise ValidationError("conversation package source schema mismatch")
    expected_package_sha = object_sha256(
        {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "package_id": manifest["package_id"],
            "subject": "english",
            "study_date": manifest["study_date"],
            "files": files,
        }
    )
    if expected_package_sha != manifest.get("package_canonical_sha256"):
        raise ValidationError("conversation package canonical hash mismatch")
    receipt = load_json(receipt_path)
    if (
        receipt.get("schema_version")
        not in {PACKAGE_RECEIPT_SCHEMA_VERSION, LEGACY_PACKAGE_RECEIPT_SCHEMA_VERSION, UNIT_RECEIPT_SCHEMA_VERSION}
        or receipt.get("package_id") != manifest["package_id"]
        or receipt.get("package_sha256") != expected_package_sha
        or receipt.get("manifest_sha256") != file_sha256(manifest_path)
    ):
        raise ValidationError("conversation package receipt binding mismatch")
    if manifest.get("capture_mode") == "independent_unit" or receipt.get("schema_version") == UNIT_RECEIPT_SCHEMA_VERSION:
        bindings = {"segment_key": manifest.get("segment_key"),
                    "request_sha256": manifest.get("request_sha256"),
                    "thread_ref_sha256": manifest.get("thread_ref_sha256"),
                    "captured_at": manifest.get("created_at"),
                    "study_date": manifest.get("study_date")}
        if (manifest.get("capture_mode") != "independent_unit"
                or receipt.get("schema_version") != UNIT_RECEIPT_SCHEMA_VERSION
                or receipt.get("capture_mode") != "independent_unit"
                or receipt.get("segment_gate_status") != "ready"
                or any(receipt.get(key) != value for key, value in bindings.items())
                or any(key in receipt or key in manifest for key in ("previous_token_sha256", "continuation_token", "token_sha256"))):
            raise ValidationError("independent unit receipt binding mismatch")
    if receipt.get("schema_version") == PACKAGE_RECEIPT_SCHEMA_VERSION:
        required_bindings = {
            "segment_key": manifest.get("segment_key"),
            "request_sha256": manifest.get("request_sha256"),
            "thread_ref_sha256": manifest.get("thread_ref_sha256"),
            "captured_at": manifest.get("created_at"),
            "previous_token_sha256": manifest.get("previous_token_sha256"),
        }
        if any(receipt.get(key) != value for key, value in required_bindings.items()):
            raise ValidationError("conversation package v2 segment binding mismatch")
        token = receipt.get("continuation_token")
        if (
            not isinstance(token, dict)
            or token.get("package_id") != manifest["package_id"]
            or token.get("package_sha256") != expected_package_sha
            or token.get("manifest_sha256") != file_sha256(manifest_path)
            or token.get("segment_key") != manifest.get("segment_key")
            or token.get("captured_at") != manifest.get("created_at")
            or token.get("thread_ref_sha256") != manifest.get("thread_ref_sha256")
            or token.get("previous_token_sha256") != manifest.get("previous_token_sha256")
            or object_sha256(token) != receipt.get("token_sha256")
        ):
            raise ValidationError("conversation package continuation token binding mismatch")
    return manifest


def read_package_receipt(package_root: Path) -> dict[str, Any]:
    """Validate a legacy chained receipt or an independent unit receipt."""
    manifest = validate_conversation_package(package_root)
    receipt = load_json(package_root.resolve(strict=True) / "receipt.json")
    if receipt.get("schema_version") == LEGACY_PACKAGE_RECEIPT_SCHEMA_VERSION:
        receipt["captured_at"] = str(manifest.get("created_at") or "")
        if not receipt["captured_at"]:
            raise ValidationError("legacy package receipt has no recoverable captured_at")
    receipt["package_path"] = str(package_root.resolve(strict=True))
    return receipt


def _recoverable_v2_receipts(
    state_dir: Path,
    thread_ref_sha256: str,
    *, deadline: float | None = None,
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for path in sorted((state_dir / "packages").glob("*/*/receipt.json")):
        _check_deadline(deadline)
        try:
            raw = load_json(path)
            if (
                raw.get("schema_version") != PACKAGE_RECEIPT_SCHEMA_VERSION
                or raw.get("thread_ref_sha256") != thread_ref_sha256
            ):
                continue
            receipts.append(read_package_receipt(path.parent))
        except (OSError, ValueError, json.JSONDecodeError, ValidationError):
            continue
    return receipts


def recover_segment_gate(state_dir: Path, *, thread_ref: str, deadline: float | None = None) -> dict[str, Any]:
    """Precisely replay one thread's package/frontier chain and rebuild its projection."""
    _check_deadline(deadline)
    resolved_state = _validate_state_root(state_dir)
    if not isinstance(thread_ref, str) or not thread_ref.strip():
        raise ValidationError("recover-segment-gate requires a nonempty thread_ref")
    thread_ref_sha256 = bytes_sha256(thread_ref.strip().encode("utf-8"))
    lock_path = resolved_state / "locks" / "segment-frontier" / f"{thread_ref_sha256}.lock"
    with _coordinated_lock(lock_path, deadline=deadline):
        events = _load_frontier_events(resolved_state, thread_ref_sha256)
        existing_keys = {event["segment_key"]: event for event in events}
        current_sha = events[-1]["token_sha256"] if events else None
        candidates = _recoverable_v2_receipts(resolved_state, thread_ref_sha256, deadline=deadline)
        appended: list[str] = []
        while True:
            _check_deadline(deadline)
            eligible = [
                receipt
                for receipt in candidates
                if receipt["segment_key"] not in existing_keys
                and receipt["previous_token_sha256"] == current_sha
            ]
            if not eligible:
                break
            if len(eligible) > 1:
                raise IdempotencyConflict(
                    "multiple sealed packages claim the same thread frontier; manual audit required"
                )
            receipt = eligible[0]
            event = _frontier_event_from_receipt(receipt)
            _append_frontier_event(resolved_state, event)
            events.append(event)
            existing_keys[event["segment_key"]] = event
            current_sha = event["token_sha256"]
            appended.append(event["segment_key"])
        unresolved = [
            receipt["package_id"]
            for receipt in candidates
            if receipt["segment_key"] not in existing_keys
        ]
        if unresolved:
            raise SegmentCapturePending(
                "sealed packages remain outside the recoverable continuation chain: "
                + ", ".join(unresolved)
            )
        if events:
            _check_deadline(deadline)
            _write_current_projection(
                resolved_state,
                thread_ref_sha256,
                events[-1],
                allow_reset=True,
            )
            _require_projection_matches(resolved_state, thread_ref_sha256, events)
        else:
            current = _load_current_projection(resolved_state)
            if thread_ref_sha256 in current["threads"]:
                del current["threads"][thread_ref_sha256]
                current["updated_at"] = utc_now()
                atomic_write_json(_current_projection_path(resolved_state), current)
        return {
            "schema_version": "english_segment_gate_recovery_receipt_v1",
            "status": "PASS",
            "thread_ref_sha256": thread_ref_sha256,
            "frontier_event_count": len(events),
            "appended_segment_keys": appended,
            "current_token_sha256": events[-1]["token_sha256"] if events else None,
            "continuation_token": events[-1]["continuation_token"] if events else None,
            "current_projection_path": str(_current_projection_path(resolved_state)),
            "formal_write_count": 0,
            "background_processing": "none",
        }


def discover_conversation_packages(
    state_dir: Path,
    study_date: str,
) -> list[Path]:
    root = state_dir.resolve() / "packages" / study_date
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if (
            path.is_dir()
            and _PACKAGE_ID_RE.fullmatch(path.name)
            and (path / "manifest.json").is_file()
        )
    )


def package_evidence_text(package_root: Path) -> str:
    conversation = load_json(package_root / "conversation.json")
    source = load_json(package_root / "source.json")
    return json.dumps(
        {"conversation": conversation, "source": source},
        ensure_ascii=False,
        sort_keys=True,
    )


def processed_package_sha256s(state_dir: Path) -> set[str]:
    processed: set[str] = set()
    for path in sorted((state_dir / "receipts" / "nightly").glob("**/*.json")):
        try:
            candidate = load_json(path)
            if candidate.get("mode") != "apply":
                continue
            batch_id = str(candidate.get("batch_id", ""))
            if not re.fullmatch(r"EN-BATCH-\d{8}-[0-9A-F]{12}", batch_id):
                continue
            manifest_path = state_dir / "nightly" / path.parent.name / (batch_id + ".manifest.json")
            manifest = load_json(manifest_path)
            overrides = frozen_package_overrides(state_dir, state_dir.parent, manifest)
            receipt, _ = validate_canonical_writer_closeout(
                state_dir, path, expected_manifest_path=manifest_path,
                package_root_overrides=overrides,
            )
        except (OSError, ValueError, json.JSONDecodeError, ValidationError):
            continue
        if receipt.get("mode") != "apply" or receipt.get("status") not in {
            "APPLIED",
            "PARTIAL",
            "NO_ACTION",
        }:
            continue
        package_ids = receipt.get("package_ids", [])
        package_sha256s = receipt.get("package_sha256s", [])
        dispositions = receipt.get("package_dispositions", {})
        action_results = receipt.get("action_results", [])
        if (
            not isinstance(package_ids, list)
            or not isinstance(package_sha256s, list)
            or len(package_ids) != len(package_sha256s)
            or not isinstance(dispositions, dict)
            or not isinstance(action_results, list)
        ):
            continue
        for package_id, package_sha256 in zip(package_ids, package_sha256s):
            if (
                not isinstance(package_id, str)
                or not isinstance(package_sha256, str)
                or dispositions.get(package_id) != "completed"
            ):
                continue
            rows = [
                row
                for row in action_results
                if isinstance(row, dict)
                and any(
                    isinstance(binding, dict)
                    and binding.get("package_id") == package_id
                    and binding.get("package_sha256") == package_sha256
                    and isinstance(binding.get("resolved_node_sha256"), str)
                    for binding in row.get("resolved_evidence", [])
                )
            ]
            if not rows:
                continue
            if any(row.get("result") in {"needs_user", "failed"} for row in rows):
                continue
            if any(row.get("result") in {"applied", "skipped"} for row in rows):
                processed.add(package_sha256)
    return processed


def package_records(
    state_dir: Path,
    study_date: str,
    *,
    include_processed: bool = False,
) -> list[dict[str, Any]]:
    processed = set() if include_processed else processed_package_sha256s(state_dir)
    records: list[dict[str, Any]] = []
    for root in discover_conversation_packages(state_dir, study_date):
        manifest = validate_conversation_package(root)
        package_sha = manifest["package_canonical_sha256"]
        if package_sha in processed:
            continue
        records.append(
            {
                "package_id": manifest["package_id"],
                "path": str(root),
                "package_sha256": package_sha,
                "manifest_sha256": file_sha256(root / "manifest.json"),
                "conversation_sha256": manifest["files"]["conversation.json"]["sha256"],
                "source_sha256": manifest["files"]["source.json"]["sha256"],
                "missing_fields": manifest.get("missing_fields", []),
            }
        )
    return sorted(records, key=lambda row: row["package_id"])


def complete_article_packages(
    state_dir: Path,
    *,
    source_id: str,
    idempotency_key: str,
    study_date: str | None = None,
    output_dir: Path | None = None,
    repo_root: Path | None = None,
    archive_contract: Any = None,
) -> dict[str, Any]:
    if not source_id.strip() or not idempotency_key.strip():
        raise ValidationError("complete-article requires source_id and idempotency_key")
    repo = (repo_root or state_dir.parent).resolve()
    dates = sorted(
        path.name for path in (state_dir / "packages").iterdir() if path.is_dir()
    ) if (state_dir / "packages").is_dir() else []
    if study_date:
        dates = [day for day in dates if day <= study_date]
    records: list[dict[str, Any]] = []
    for date in dates:
        for record in package_records(state_dir, date, include_processed=True):
            source = load_json(Path(record["path"]) / "source.json")
            if source.get("identity", {}).get("source_id") == source_id:
                records.append(record)
    from .archive import _parse_locator, resolve_archived_package_from_pointer, verify_volume_contract, VolumeContract
    volume = None
    by_id = {record["package_id"]: record for record in records}
    for pointer_path in sorted((state_dir / "archive-pointers").glob("*/*.json")):
        if study_date and pointer_path.parent.name > study_date:
            continue
        pointer = load_json(pointer_path)
        package_id = str(pointer.get("package_id", ""))
        if not _PACKAGE_ID_RE.fullmatch(package_id) or pointer_path.stem != package_id:
            raise ValidationError("article archive pointer identity is invalid")
        locator_path = repo / "wiki/raw_archives" / f"{package_id}.md"
        if not locator_path.is_file():
            raise ValidationError("article snapshot has an unverified archive locator; completeness is unknown")
        locator = _parse_locator(locator_path.read_text(encoding="utf-8"))
        if source_id not in locator.get("retrieval_keys", []):
            continue
        if volume is None:
            volume = verify_volume_contract(archive_contract or VolumeContract())
        archived = resolve_archived_package_from_pointer(
            state_dir, repo, study_date=pointer_path.parent.name, package_id=package_id,
            expected_package_sha256=pointer.get("package_sha256"),
        )
        archived.relative_to(volume["subject_root"])
        manifest = validate_conversation_package(archived)
        identity = load_json(archived / "source.json").get("identity", {})
        if identity.get("source_id") != source_id:
            raise ValidationError("article archive source differs from its locator")
        record = {"package_id": package_id, "path": str(archived),
                  "package_sha256": manifest["package_canonical_sha256"],
                  "manifest_sha256": file_sha256(archived / "manifest.json"),
                  "conversation_sha256": manifest["files"]["conversation.json"]["sha256"],
                  "source_sha256": manifest["files"]["source.json"]["sha256"],
                  "missing_fields": manifest.get("missing_fields", [])}
        if package_id in by_id and by_id[package_id]["package_sha256"] != record["package_sha256"]:
            raise IdempotencyConflict("local and archived article packages disagree")
        by_id.setdefault(package_id, record)
    records = list(by_id.values())
    if not records:
        raise ValidationError(f"cannot complete article without conversation packages: {source_id}")
    records.sort(key=lambda row: (Path(row["path"]).parent.name, row["package_id"]))
    completion_date = study_date or Path(records[-1]["path"]).parent.name
    identity = object_sha256(
        {
            "source_id": source_id,
            "idempotency_key": idempotency_key,
            "package_sha256s": [row["package_sha256"] for row in records],
        }
    )[:16].upper()
    completion_id = f"EN-COMPLETE-{completion_date.replace('-', '')}-{identity}"
    destination = output_dir or state_dir / "views" / completion_date
    export_json = destination / f"{completion_id}.json"
    export_markdown = destination / f"{completion_id}.md"
    snapshot = {
        "schema_version": "english_article_package_snapshot_v1",
        "completion_id": completion_id,
        "source_id": source_id,
        "study_date": completion_date,
        "package_documents": records,
        "package_count": len(records),
        "formal_write_count": 0,
        "background_processing": "none",
    }
    markdown = "\n".join(
        [
            "# English Article Conversation Snapshot",
            "",
            f"source_id: {source_id}",
            f"package_count: {len(records)}",
            "formal_write_count: 0",
            "background_processing: none",
            "",
            "## Packages",
            "",
            *[
                f"- {row['package_id']} | {row['package_sha256']} | missing_fields={','.join(row['missing_fields']) or 'none'}"
                for row in records
            ],
            "",
        ]
    )
    receipt_path = state_dir / "receipts" / "completion" / completion_date / f"{completion_id}.json"
    receipt = {
        "schema_version": "english_article_completion_receipt_v2",
        "receipt_id": completion_id,
        "completion_id": completion_id,
        "status": "created",
        "source_id": source_id,
        "study_date": completion_date,
        "package_ids": [row["package_id"] for row in records],
        "package_sha256s": [row["package_sha256"] for row in records],
        "export_json": str(export_json),
        "export_markdown": str(export_markdown),
        "formal_write_count": 0,
        "background_processing": "none",
    }
    if receipt_path.exists():
        existing = load_json(receipt_path)
        if existing.get("package_sha256s") != receipt["package_sha256s"]:
            raise IdempotencyConflict("article completion key already binds a different package set")
        # The raw package/receipt is immutable. Its locator-only snapshot is a
        # rebuildable view and must follow an exact move from local staging to T9.
        if not export_json.is_file() or load_json(export_json) != snapshot:
            atomic_write_json(export_json, snapshot)
        if not export_markdown.is_file() or export_markdown.read_text(encoding="utf-8") != markdown:
            atomic_write_text(export_markdown, markdown)
        replay = dict(existing)
        replay["status"] = "idempotent_noop"
        return replay
    atomic_write_json(export_json, snapshot)
    atomic_write_text(export_markdown, markdown)
    atomic_write_json(receipt_path, receipt)
    return receipt


def frozen_package_overrides(state_dir: Path, repo_root: Path, manifest: dict[str, Any], *, archive_contract: Any = None) -> dict[str, Path]:
    """Resolve missing frozen package roots from exact pointers, never a T9 scan."""
    from .archive import resolve_archived_package_from_pointer, verify_volume_contract, VolumeContract
    result: dict[str, Path] = {}
    volume = None
    for row in manifest.get("package_documents", []):
        if Path(row["path"]).is_dir():
            continue
        if volume is None:
            volume = verify_volume_contract(archive_contract or VolumeContract())
        package = resolve_archived_package_from_pointer(
            state_dir, repo_root, study_date=manifest["study_date"], package_id=row["package_id"],
            expected_package_sha256=row["package_sha256"], expected_manifest_sha256=row["manifest_sha256"],
        )
        package.relative_to(volume["subject_root"])
        result[row["package_id"]] = package
    return result


def validate_evidence_refs(
    refs: Any,
    package_documents: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(refs, list) or not refs:
        raise ValidationError("typed action requires at least one package evidence_ref")
    frozen = {row["package_id"]: row for row in package_documents}
    validated: list[dict[str, Any]] = []

    def resolve_pointer(document: Any, fragment: str, *, context: str) -> Any:
        if fragment == "#":
            return document
        if not fragment.startswith("#/"):
            raise ValidationError(f"{context} JSON pointer must start with '#/'")
        current = document
        for raw_token in fragment[2:].split("/"):
            token = raw_token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, list):
                if not token.isdigit():
                    raise ValidationError(f"{context} array pointer token is not an index")
                index_value = int(token)
                if index_value < 0 or index_value >= len(current):
                    raise ValidationError(f"{context} array pointer index is out of bounds")
                current = current[index_value]
            elif isinstance(current, dict):
                if token not in current:
                    raise ValidationError(f"{context} JSON pointer target does not exist")
                current = current[token]
            else:
                raise ValidationError(f"{context} JSON pointer traverses a scalar value")
        return current

    for index, ref in enumerate(refs, start=1):
        if not isinstance(ref, dict) or set(ref) != {
            "package_id",
            "package_sha256",
            "pointer",
            "kind",
        }:
            raise ValidationError(f"evidence_ref {index} fields do not match schema")
        record = frozen.get(ref["package_id"])
        if record is None or record["package_sha256"] != ref["package_sha256"]:
            raise ValidationError(f"evidence_ref {index} is outside the frozen package set")
        pointer = ref["pointer"]
        if not isinstance(pointer, str) or "#" not in pointer:
            raise ValidationError(f"evidence_ref {index} pointer is invalid")
        document_name, fragment = pointer.split("#", 1)
        if document_name not in {"conversation.json", "source.json", "manifest.json"}:
            raise ValidationError(f"evidence_ref {index} document is invalid")
        package_root = Path(record["path"])
        package_manifest = validate_conversation_package(package_root)
        document = (
            package_manifest
            if document_name == "manifest.json"
            else load_json(package_root / document_name)
        )
        node = resolve_pointer(document, f"#{fragment}", context=f"evidence_ref {index}")
        kind = ref["kind"]
        attachment_roles = {
            "question_image", "solution_image", "explanation_image",
            "user_work_image", "source_article_image", "other_attachment",
        }
        resolved_type: str
        resolved_role: str
        if document_name == "conversation.json":
            if not isinstance(node, dict) or node.get("role") not in {"user", "assistant"}:
                raise ValidationError(f"evidence_ref {index} message pointer must resolve to a complete message")
            role = node["role"]
            allowed_kind = "user_message" if role == "user" else "assistant_message"
            if kind == "independent_correct_use":
                metadata = node.get("metadata", {})
                tags = metadata.get("evidence_tags", []) if isinstance(metadata, dict) else []
                if role != "user" or "independent_correct_use" not in tags:
                    raise ValidationError(
                        f"evidence_ref {index} independent_correct_use lacks exact user metadata"
                    )
            elif kind != allowed_kind:
                raise ValidationError(f"evidence_ref {index} kind does not match message role")
            resolved_type = "conversation_message"
            resolved_role = role
        elif document_name == "source.json":
            if kind != "source_text":
                raise ValidationError(f"evidence_ref {index} kind must be source_text")
            if not isinstance(node, (str, int, float, bool)) or node in {"", None}:
                raise ValidationError(f"evidence_ref {index} source pointer must resolve to a scalar fact")
            resolved_type = "source_value"
            resolved_role = "source_text"
        else:
            if not pointer.startswith("manifest.json#/attachments/"):
                raise ValidationError(f"evidence_ref {index} manifest pointer must target one attachment")
            if not isinstance(node, dict) or node.get("role") not in attachment_roles:
                raise ValidationError(f"evidence_ref {index} attachment pointer is invalid")
            if kind != node["role"]:
                raise ValidationError(f"evidence_ref {index} kind does not match attachment role")
            attachment_path = package_root / str(node.get("path", ""))
            if not attachment_path.is_file() or file_sha256(attachment_path) != node.get("sha256"):
                raise ValidationError(f"evidence_ref {index} attachment bytes do not match manifest")
            resolved_type = "attachment"
            resolved_role = node["role"]
        if kind not in {
            "user_message", "assistant_message", "source_text",
            "independent_correct_use", *attachment_roles,
        }:
            raise ValidationError(f"evidence_ref {index} kind is invalid")
        validated.append(
            {
                "binding": {
                    **ref,
                    "resolved_type": resolved_type,
                    "resolved_role": resolved_role,
                    "resolved_node_sha256": object_sha256(node),
                },
                "value": node,
            }
        )
    return validated


def validate_receipt_resolved_evidence(
    receipt: dict[str, Any],
    manifest: dict[str, Any],
    *,
    package_root_overrides: dict[str, Path] | None = None,
) -> None:
    package_documents = manifest.get("package_documents", [])
    if not isinstance(package_documents, list):
        raise ValidationError("writer receipt manifest package_documents is invalid")
    effective_package_documents = [dict(row) for row in package_documents]
    for row in effective_package_documents:
        override = (package_root_overrides or {}).get(str(row.get("package_id")))
        if override is not None:
            row["path"] = str(override)
    for index, result in enumerate(receipt.get("action_results", []), start=1):
        if not isinstance(result, dict):
            raise ValidationError(f"writer receipt action result {index} is invalid")
        if result.get("result") not in {"applied", "skipped"}:
            continue
        recomputed = validate_evidence_refs(
            result.get("evidence_refs"), effective_package_documents
        )
        recomputed_bindings = [row["binding"] for row in recomputed]
        if result.get("resolved_evidence") != recomputed_bindings:
            raise ValidationError(
                f"writer receipt action result {index} resolved evidence drift"
            )


def validate_canonical_writer_closeout(
    state_dir: Path,
    receipt_path: Path,
    *,
    expected_manifest_path: Path | None = None,
    package_root_overrides: dict[str, Path] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state_root = state_dir.resolve()
    receipt_path = receipt_path.resolve(strict=True)
    receipt = load_json(receipt_path)
    if receipt.get("schema_version") != "english_apply_receipt_v1":
        raise ValidationError("writer closeout receipt schema mismatch")
    receipt_id = receipt.get("receipt_id")
    batch_id = receipt.get("batch_id")
    if not isinstance(receipt_id, str) or not isinstance(batch_id, str):
        raise ValidationError("writer closeout receipt identity is incomplete")
    manifest_matches = sorted(
        (state_root / "nightly").glob(f"*/{batch_id}.manifest.json")
    )
    if len(manifest_matches) != 1:
        raise ValidationError("writer closeout must resolve exactly one canonical manifest")
    manifest_path = manifest_matches[0].resolve(strict=True)
    if expected_manifest_path is not None and manifest_path != expected_manifest_path.resolve(strict=True):
        raise ValidationError("writer closeout manifest path differs from expected manifest")
    manifest = load_json(manifest_path)
    study_date = manifest.get("study_date")
    if manifest.get("batch_id") != batch_id or not isinstance(study_date, str):
        raise ValidationError("writer closeout manifest identity mismatch")
    canonical_manifest_path = (
        state_root / "nightly" / study_date / f"{batch_id}.manifest.json"
    ).resolve()
    if manifest_path != canonical_manifest_path:
        raise ValidationError("writer closeout manifest path is not canonical")
    canonical_receipt_path = (
        state_root / "receipts" / "nightly" / study_date / f"{receipt_id}.json"
    ).resolve()
    if receipt_path != canonical_receipt_path:
        raise ValidationError("writer closeout receipt path is not canonical")
    if receipt.get("manifest_sha256") != file_sha256(manifest_path):
        raise ValidationError("writer closeout receipt does not bind canonical manifest bytes")
    if manifest.get("schema_version") == "english_nightly_manifest_v3":
        preflight_path = Path(
            str(receipt.get("support_preflight_receipt_path") or "")
        )
        if (
            preflight_path.is_symlink()
            or not preflight_path.is_file()
            or file_sha256(preflight_path)
            != receipt.get("support_preflight_receipt_sha256")
        ):
            raise ValidationError("writer closeout support preflight receipt drift")
        preflight = load_json(preflight_path)
        if (
            preflight.get("schema_version")
            != "english_sentence_support_preflight_receipt_v1"
            or preflight.get("status") != "PASS"
            or preflight.get("receipt_id")
            != receipt.get("support_preflight_receipt_id")
            or preflight.get("batch_id") != batch_id
            or preflight.get("manifest_sha256") != receipt.get("manifest_sha256")
            or preflight.get("actions_sha256") != receipt.get("actions_sha256")
            or preflight.get("proposal_sha256")
            != receipt.get("support_proposal_sha256")
        ):
            raise ValidationError("writer closeout support proposal binding mismatch")
    canonical_journal_path = (
        state_root / "nightly" / study_date / f"{batch_id}.journal.jsonl"
    ).resolve()
    configured_journal = Path(str(receipt.get("journal_path", ""))).resolve()
    if configured_journal != canonical_journal_path or not canonical_journal_path.is_file():
        raise ValidationError("writer closeout journal path is not canonical")

    previous_sha256 = "0" * 64
    lifecycle: list[str] = []
    close_records: list[dict[str, Any]] = []
    for expected_sequence, line in enumerate(
        canonical_journal_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            raise ValidationError("writer closeout journal contains a blank record")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError("writer closeout journal contains invalid JSON") from exc
        if not isinstance(record, dict):
            raise ValidationError("writer closeout journal record must be an object")
        core = {key: value for key, value in record.items() if key != "record_sha256"}
        if record.get("sequence") != expected_sequence:
            raise ValidationError("writer closeout journal sequence is broken")
        if record.get("previous_sha256") != previous_sha256:
            raise ValidationError("writer closeout journal previous hash is broken")
        if object_sha256(core) != record.get("record_sha256"):
            raise ValidationError("writer closeout journal record hash is broken")
        previous_sha256 = record["record_sha256"]
        if record.get("receipt_id") == receipt_id:
            event_type = record.get("event_type")
            if isinstance(event_type, str):
                lifecycle.append(event_type)
            if event_type == "receipt_closed":
                close_records.append(record)
    required_lifecycle = [
        "started",
        "validated",
        "transaction_prepared",
        "committed_pending_receipt",
        "receipt_closed",
    ]
    lifecycle_positions: list[int] = []
    for event_type in required_lifecycle:
        try:
            lifecycle_positions.append(lifecycle.index(event_type))
        except ValueError as exc:
            raise ValidationError(f"writer closeout lifecycle missing {event_type}") from exc
    if lifecycle_positions != sorted(lifecycle_positions):
        raise ValidationError("writer closeout lifecycle order is invalid")
    if len(close_records) != 1:
        raise ValidationError("writer closeout must contain exactly one receipt_closed record")
    receipt_sha256 = file_sha256(receipt_path)
    close_data = close_records[0].get("data", {})
    if (
        close_data.get("receipt_path") != str(receipt_path)
        or close_data.get("receipt_sha256") != receipt_sha256
    ):
        raise ValidationError("writer closeout receipt_closed binding mismatch")

    validate_receipt_resolved_evidence(
        receipt,
        manifest,
        package_root_overrides=package_root_overrides,
    )
    return receipt, manifest
