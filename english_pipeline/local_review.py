"""Exact, user-evidenced native local review of otherwise web-held captures.

This never changes the immutable capture route or manufactures a web return.
Authorization admits a package set; a separate local review binds final actions.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import IdempotencyConflict, ValidationError
from .packages import validate_conversation_package
from .util import atomic_write_json, bytes_sha256, file_sha256, load_json, object_sha256

_ID = re.compile(r"EN-LOCAL-REVIEW-[A-F0-9]{24}")
_PACKAGE = re.compile(r"EN-PKG-([0-9]{8})-[A-F0-9]{16}")
_LOCAL_REQUEST = "不用走网页端了，直接在本地入库"


def _user_evidence(path: Path, line: int, session_id: str) -> dict[str, Any]:
    if line < 2:
        raise ValidationError("local review requires the exact user message line")
    with path.open("rb") as stream:
        header = json.loads(next(stream))
        if (header.get("type") != "session_meta"
                or header["payload"].get("id") != session_id):
            raise ValidationError("local review rollout session binding differs")
        raw = next((raw for number, raw in enumerate(stream, 2) if number == line), None)
    if raw is None:
        raise ValidationError("local review user message line is missing")
    record = json.loads(raw)
    message = record.get("payload", {})
    if (record.get("type") != "response_item" or message.get("type") != "message"
            or message.get("role") != "user" or not message.get("id")):
        raise ValidationError("local review authorization must be an original user message")
    text = "\n".join(item.get("text", "") for item in message.get("content", [])
                     if item.get("type") in {"input_text", "text"})
    if _LOCAL_REQUEST not in text or "正式入库" not in text:
        raise ValidationError("user message does not explicitly authorize native local formal intake")
    return {"rollout_path": str(path.resolve()), "line": line, "session_id": session_id,
            "message_id": message["id"], "record_sha256": bytes_sha256(raw), "text": text}


def _immutable(path: Path, document: dict[str, Any]) -> None:
    if path.exists():
        if load_json(path) != document:
            raise IdempotencyConflict("native local review record already binds different content")
    else:
        atomic_write_json(path, document)


def create_local_authorization(
    state: Path, *, package_ids: set[str], rollout_path: Path,
    user_message_line: int, session_id: str,
) -> dict[str, Any]:
    """Persist the user's route override for one explicit, nonempty package set."""
    if not package_ids:
        raise ValidationError("local review requires a nonempty exact package set")
    evidence = _user_evidence(rollout_path, user_message_line, session_id)
    packages = []
    for package_id in sorted(package_ids):
        match = _PACKAGE.fullmatch(package_id)
        if not match:
            raise ValidationError("invalid local review package ID")
        token = match[1]
        date = f"{token[:4]}-{token[4:6]}-{token[6:]}"
        root = state.resolve() / "packages" / date / package_id
        package = validate_conversation_package(root)
        packages.append({"package_id": package_id, "study_date": date,
                         "package_sha256": package["package_canonical_sha256"],
                         "manifest_sha256": file_sha256(root / "manifest.json")})
    core = {"schema_version": "english_native_local_authorization_v1",
            "review_kind": "native_local_review", "user_evidence": evidence,
            "packages": packages}
    review_id = "EN-LOCAL-REVIEW-" + object_sha256(core)[:24].upper()
    document = {**core, "local_review_id": review_id}
    _immutable(state / "local-review" / review_id / "authorization.json", document)
    return document


def local_authorization(state: Path, review_id: str) -> dict[str, Any]:
    if not _ID.fullmatch(review_id):
        raise ValidationError("invalid native local review ID")
    row = load_json(state / "local-review" / review_id / "authorization.json")
    core = {key: value for key, value in row.items() if key != "local_review_id"}
    if (row.get("local_review_id") != review_id
            or "EN-LOCAL-REVIEW-" + object_sha256(core)[:24].upper() != review_id
            or row.get("review_kind") != "native_local_review"
            or row.get("schema_version") != "english_native_local_authorization_v1"):
        raise ValidationError("native local review authorization binding differs")
    evidence = row["user_evidence"]
    current = _user_evidence(Path(evidence["rollout_path"]), evidence["line"], evidence["session_id"])
    if current != evidence:
        raise ValidationError("native local review user evidence changed")
    return row


def validate_local_packages(
    state: Path, review_id: str, documents: list[dict[str, Any]], *, study_date: str,
) -> None:
    authorization = local_authorization(state, review_id)
    expected = {p["package_id"]: p for p in authorization["packages"]
                if p["study_date"] == study_date}
    if not expected or len(documents) != len(expected) or {p["package_id"] for p in documents} != set(expected):
        raise ValidationError("native local review requires its exact daily package set")
    for row in documents:
        package = validate_conversation_package(Path(row["path"]))
        allowed = expected[row["package_id"]]
        if (package["package_id"] != row["package_id"]
                or package["package_canonical_sha256"] != allowed["package_sha256"]
                or row["package_sha256"] != allowed["package_sha256"]
                or file_sha256(Path(row["path"]) / "manifest.json") != allowed["manifest_sha256"]):
            raise ValidationError("native local review exact package hash differs")


def local_pending_record(state: Path, row: dict[str, Any], review_id: str | None) -> dict[str, Any]:
    if not review_id:
        return row
    approved = {p["package_id"]: p for p in local_authorization(state, review_id)["packages"]}
    allowed = approved.get(row["package_id"])
    if not allowed:
        return row
    if row.get("package_sha256") != allowed["package_sha256"]:
        raise ValidationError("native local review exact package hash differs")
    return {**row, "local_review_id": review_id,
            **({"status": "pending", "reason": "explicit_native_local_review"}
               if row["status"] == "waiting_web_review" else {})}


def _review_binding(state: Path, review_id: str, manifest_path: Path, actions_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if manifest.get("local_review_id") != review_id or manifest.get("web_review_id"):
        raise ValidationError("manifest is not bound to this native local review")
    validate_local_packages(state, review_id, manifest["package_documents"], study_date=manifest["study_date"])
    from .writer import validate_sol_actions
    validate_sol_actions(load_json(actions_path), manifest, file_sha256(manifest_path))
    return {"schema_version": "english_native_local_actions_review_v1",
            "review_kind": "native_local_review", "local_review_id": review_id,
            "batch_id": manifest["batch_id"], "study_date": manifest["study_date"],
            "manifest_sha256": file_sha256(manifest_path), "manifest": manifest,
            "actions_sha256": file_sha256(actions_path)}


def bind_reviewed_actions(
    state: Path, *, local_review_id: str, manifest_path: Path, actions_path: Path,
    reviewer: str,
) -> dict[str, Any]:
    """Called by the local reviewer only after reviewing the complete action set."""
    if not reviewer.strip():
        raise ValidationError("native local actions require a named reviewer")
    binding = _review_binding(state, local_review_id, manifest_path, actions_path)
    review_sha = object_sha256(binding)
    row = {**binding, "reviewer": reviewer, "review_sha256": review_sha}
    _immutable(state / "local-review" / local_review_id / "actions" / f"{review_sha}.json", row)
    return row


def validate_local_writer_admission(state: Path, manifest: dict[str, Any], actions_path: Path) -> None:
    review_id = manifest["local_review_id"]
    validate_local_packages(state, review_id, manifest["package_documents"], study_date=manifest["study_date"])
    # The writer independently validates actions against the actual manifest file;
    # this receipt additionally proves a native reviewer bound these exact bytes.
    actions = load_json(actions_path)
    binding = {"schema_version": "english_native_local_actions_review_v1",
               "review_kind": "native_local_review", "local_review_id": review_id,
               "batch_id": manifest["batch_id"], "study_date": manifest["study_date"],
               "manifest_sha256": actions.get("batch_manifest_sha256"), "manifest": manifest,
               "actions_sha256": file_sha256(actions_path)}
    review_sha = object_sha256(binding)
    path = state / "local-review" / review_id / "actions" / f"{review_sha}.json"
    if not path.is_file():
        raise ValidationError("native local formal actions have not been reviewed or have changed")
    row = load_json(path)
    if ({k: v for k, v in row.items() if k not in {"reviewer", "review_sha256"}} != binding
            or row.get("review_sha256") != review_sha or not row.get("reviewer")):
        raise ValidationError("native local action review binding differs")
