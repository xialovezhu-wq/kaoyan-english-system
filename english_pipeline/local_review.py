"""Exact, user-evidenced native local review of otherwise web-held captures.

This never changes the immutable capture route or manufactures a web return.
Authorization admits a package set; a separate local review binds final actions.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .cold_storage import cold_artifacts, cold_directory, guard_write
from .errors import IdempotencyConflict, ValidationError
from .packages import validate_conversation_package
from .util import atomic_write_json, bytes_sha256, canonical_bytes, file_sha256, load_json, object_sha256

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


def local_worklist(state: Path, rows: list[dict[str, Any]], cutoff: str, *, persist: bool) -> dict[str, Any]:
    """Freeze the local author's exact pending scope. Formal apply still uses its separate writer authorization."""
    packages = []
    for row in rows:
        if row["status"] not in {"waiting_web_review", "pending"}:
            continue
        binding = {k: row[k] for k in ("package_id", "study_date", "package_sha256", "manifest_sha256")}
        hold = state / "web-review" / "holds" / (row["package_id"] + ".json")
        binding["previous_hold_sha256"] = file_sha256(hold) if hold.is_file() else None
        packages.append(binding)
    if not packages:
        raise ValidationError("a local authoring worklist requires pending packages")
    core = {"schema_version": "english_native_local_worklist_v1",
            "review_kind": "native_local_review", "cutoff_date": cutoff, "packages": packages,
            "purpose": "local_authoring_scope_only", "formal_write_count": 0}
    review_id = "EN-LOCAL-REVIEW-" + object_sha256(core)[:24].upper()
    document = {**core, "local_review_id": review_id}
    if persist:
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
            or row.get("schema_version") not in {"english_native_local_authorization_v1", "english_native_local_worklist_v1"}):
        raise ValidationError("native local review authorization binding differs")
    if row["schema_version"] == "english_native_local_worklist_v1":
        return row
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
        if "previous_hold_sha256" in allowed:
            hold = state / "web-review" / "holds" / (row["package_id"] + ".json")
            current_hold = file_sha256(hold) if hold.is_file() else None
            if current_hold != allowed["previous_hold_sha256"]:
                raise ValidationError("local worklist package hold changed")
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


@cold_artifacts("local-english-authoring")
def author_local_day(state: Path, manifest_path: Path, reviewer: str, *, repo: Path | None = None) -> dict[str, Any]:
    """Expose the frozen local sources and assemble only real, completed per-package decisions."""
    from .sentence_support import current_index_sha256

    manifest_path = manifest_path.resolve(strict=True)
    manifest = load_json(manifest_path)
    review_id = manifest.get("local_review_id")
    batch = manifest["batch_id"]
    day = manifest["study_date"]
    if (not review_id or not re.fullmatch(r"EN-BATCH-\d{8}-[A-F0-9]{12}", batch)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
            or manifest_path != (state / "nightly" / day / f"{batch}.manifest.json").resolve()
            or manifest.get("schema_version") != "english_nightly_manifest_v3"):
        raise ValidationError("local authoring requires its canonical, dated v3 freeze")
    documents = manifest["package_documents"]
    validate_local_packages(state, review_id, documents, study_date=day)
    manifest_sha = file_sha256(manifest_path)
    producer = {"role": "sol_nightly_reviewer", "model": reviewer, "prompt_version": "native-local-authoring-v1"}
    if not reviewer.strip():
        raise ValidationError("a local author identity is required")

    work = cold_directory(state / "local-authoring" / batch, "local-authoring", batch)
    source_path = work / "sources.json"
    sources = {"batch_id": batch, "batch_manifest_sha256": manifest_sha, "local_review_id": review_id,
               "manifest_path": str(manifest_path), "packages": documents,
               "expected_index_sha256": current_index_sha256(state)}
    if source_path.exists():
        fixed = load_json(source_path)
        if any(fixed.get(k) != v for k, v in sources.items() if k != "expected_index_sha256"):
            raise ValidationError("fixed local authoring sources changed")
        sources = fixed
    else:
        guard_write(source_path, canonical_bytes(sources))
        _immutable(source_path, sources)
    dest = work / "decisions"
    dest.mkdir(exist_ok=True)
    from .memory_use import inspect as inspect_memory_use
    pending, decisions = [], []
    memory_use = {}
    for row in documents:
        pid = row["package_id"]
        memory_use[pid] = inspect_memory_use(state, pid, row["package_sha256"])
        path = dest / (pid + ".json")
        if not path.is_file():
            pending.append(pid)
            continue
        if path.is_symlink():
            raise ValidationError("local author decision must be a regular file")
        item = load_json(path)
        from .writing_author import is_writing_package,review_rows
        expected={"package_id","package_sha256","batch_manifest_sha256","actions","unresolved","support_record"}
        if is_writing_package(Path(row["path"])):
            expected.add("writing_review")
            if "writing_review" in item:
                review_rows(state,Path(row["path"]),item)
        if (set(item) != expected
                or item["package_id"] != pid or item["package_sha256"] != row["package_sha256"]
                or item["batch_manifest_sha256"] != manifest_sha
                or not isinstance(item["actions"], list) or not isinstance(item["unresolved"], list)
                or not (item["actions"] or item["unresolved"])):
            raise ValidationError("local author decision lacks exact evidence or disposition: " + pid)
        for action in item["actions"] + item["unresolved"]:
            if not any(ref.get("package_id") == pid and ref.get("package_sha256") == row["package_sha256"]
                       for ref in action.get("evidence_refs", [])):
                raise ValidationError("local author action lacks this package's evidence: " + pid)
        support = item["support_record"]
        if support is not None and (not isinstance(support, dict) or support.get("package_id") != pid):
            raise ValidationError("local sentence-support record belongs to another package: " + pid)
        decisions.append(item)
    result = {"local_review_id": review_id, "batch_id": batch, "sources_path": str(source_path),
              "decisions_dir": str(dest), "pending_package_ids": pending, "completed_drafts": len(decisions),
              "memory_use": memory_use, "formal_write_count": 0}
    if pending:
        return {**result, "status": "LOCAL_AUTHORING_IN_PROGRESS"}
    revision = object_sha256([producer, sources, decisions])
    output = work / "compiled" / revision
    output.mkdir(parents=True, exist_ok=True)
    action_id = "EN-LOCAL-ACTIONS-" + revision[:24].upper()
    fixed = {"batch_id": batch, "batch_manifest_sha256": manifest_sha, "action_set_id": action_id,
             "producer": producer, "created_at": manifest["frozen_at"], "formal_write_count": 0}
    actions = {"schema_version": "english_sol_actions_v2", **fixed, "formal_writeback": "none",
               "actions": [a for item in decisions for a in item["actions"]],
               "unresolved": [u for item in decisions for u in item["unresolved"]]}
    support_doc = {"schema_version": "english_sentence_support_proposal_v1", **fixed,
                   "proposal_id": "SUPPORT-" + action_id, "expected_index_sha256": sources["expected_index_sha256"],
                   "records": [item["support_record"] for item in decisions if item["support_record"] is not None],
                   "background_processing": "none"}
    for suffix, obj in [("actions", actions), ("support", support_doc)]:
        path = output / f"{batch}.{suffix}.json"
        guard_write(path, canonical_bytes(obj))
        _immutable(path, obj)
    actions_path = output / f"{batch}.actions.json"
    from .writing_author import store_coverage
    store_coverage(state,manifest_path,actions_path,decisions)
    bind_reviewed_actions(state, local_review_id=review_id, manifest_path=manifest_path,
                          actions_path=actions_path, reviewer=reviewer)
    from .formal_model_review import formal_model_review_enabled, prepare_formal_model_review
    if formal_model_review_enabled(state):
        from .writer import validate_sol_actions
        reviewed = load_json(actions_path)
        validate_sol_actions(reviewed, manifest, manifest_sha)
        result["formal_model_review"] = prepare_formal_model_review(
            state, repo or state.parent, manifest, reviewed, manifest_sha, file_sha256(actions_path))
    return {**result, "status": "LOCAL_ACTIONS_READY_FOR_NATIVE_PREFLIGHT", "actions_path": str(actions_path),
            "support_path": str(output / f"{batch}.support.json"), "compiled_dir": str(output)}
