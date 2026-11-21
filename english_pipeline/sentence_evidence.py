"""Read only the original messages explicitly bound to one English attempt."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .answer_safety import contains_answer_signal
from .errors import ValidationError
from .packages import validate_conversation_package
from .util import canonical_bytes, file_sha256, load_json, object_sha256, sentence_sha256

PACKAGE_ID = re.compile(r"EN-PKG-(\d{4})(\d{2})(\d{2})-[A-F0-9]{16}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
MESSAGE = re.compile(r"conversation\.json#/messages/(\d+)\Z")
FIELDS = ("original_sentence", "first_attempt", "hints", "correction", "later_independent_attempt")
FIRST = re.compile(r"(?:^|\n)\s*(?:我的翻译|我翻译|首译|初译|我的译文)\s*[:：]", re.M)
ERRORS = (OSError, ValueError, KeyError, TypeError, ValidationError)


def package_root(repo: Path, state: Path, pid: str, digest: str) -> Path:
    match = PACKAGE_ID.fullmatch(pid)
    if match is None or not DIGEST.fullmatch(digest):
        raise ValidationError("an exact English package identity and hash are required")
    day = "-".join(match.groups())
    path = state / "packages" / day / pid
    if not path.is_dir():
        from .archive import resolve_archived_package_from_pointer
        path = resolve_archived_package_from_pointer(state, repo, study_date=day, package_id=pid,
                                                     expected_package_sha256=digest)
    manifest = validate_conversation_package(path)
    if manifest["package_id"] != pid or manifest["package_canonical_sha256"] != digest:
        raise ValidationError("English attempt package no longer matches its evidence")
    return path


def withdrawn_packages(lifecycle: dict[str, Any]) -> set[str]:
    return {pid for eid in lifecycle.get("invalidated_episode_ids", [])
            if isinstance(eid, str) and PACKAGE_ID.fullmatch(pid := eid.split(":", 1)[0])}


def _message_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize only exact, frozen message references from either established reader."""
    result = {}
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        pointers = [ref["pointer"]] if isinstance(ref.get("pointer"), str) else []
        if ref.get("member") == "conversation.json" and not pointers:
            locators = ref.get("locators", [])
            if isinstance(ref.get("locator"), str):
                locators = [*locators, *ref["locator"].split(";")]
            pointers = ["conversation.json#/" + location.lstrip("#/")
                        for location in locators if isinstance(location, str)]
        pid, digest = ref.get("package_id"), ref.get("package_sha256")
        for pointer in pointers:
            if MESSAGE.fullmatch(pointer) and isinstance(pid, str) and isinstance(digest, str):
                key = (pid, digest, pointer)
                # Prefer the support binding with an exact resolved-node hash.
                if key not in result or ref.get("resolved_node_sha256"):
                    result[key] = {**ref, "pointer": pointer}
    return list(result.values())


def attempt_bundle(repo: Path, state: Path, identity: dict[str, Any], refs: list[dict[str, Any]], *,
                   episode_id: str | None = None, roles: dict[str, Any] | None = None,
                   authored_first: str | None = None, max_bytes: int = 4096) -> dict[str, Any]:
    exact_refs = _message_refs(refs)
    packages = {(r["package_id"], r["package_sha256"]) for r in exact_refs}
    coverage = dict.fromkeys(FIELDS, "unknown")
    bundle = {**dict.fromkeys(FIELDS), "evidence_refs": [],
              "coverage": coverage, "attempt_id": None}
    if episode_id:
        bundle["episode_id"] = episode_id
    if len(packages) != 1 or not identity.get("sentence_id") or identity.get("question_id"):
        return bundle
    pid, digest = packages.pop()
    from .memory_lifecycle import read_lifecycle
    from .memory_query import _state_identity

    initial = _state_identity(state)
    lifecycle = read_lifecycle(state)
    if pid in withdrawn_packages(lifecycle) or episode_id in lifecycle.get("invalidated_episode_ids", []):
        return {**bundle, "reason": "withdrawn_evidence"}
    if lifecycle.get("global_pending"):
        return {**bundle, "reason": "memory_update_pending"}
    try:
        path = package_root(repo, state, pid, digest)
        source = load_json(path / "source.json")["identity"]
        original = source["source_sentence"]
        if (source.get("answer_exposure") != "answer_free" or source.get("question_id")
                or source.get("source_kind") not in {"article", "user_provided"}
                or any(source.get(k) != identity.get(k) for k in ("source_id", "sentence_id", "source_hash"))
                or not isinstance(original, str) or sentence_sha256(original) != identity.get("sentence_sha256")
                or contains_answer_signal(original)):
            return {**bundle, "reason": "package_not_exact_answer_free_sentence"}
        doc = path / "conversation.json"
        doc_sha = file_sha256(doc)
        messages = load_json(doc)["messages"]
        selected = {}
        for ref in exact_refs:
            ordinal = int(MESSAGE.fullmatch(ref["pointer"])[1])
            if not 0 <= ordinal < len(messages):
                continue
            node = messages[ordinal]
            node_sha = object_sha256(node)
            expected_sha = ref.get("resolved_node_sha256")
            if (expected_sha and expected_sha != node_sha) or (
                    not expected_sha and ref.get("sha256") != doc_sha):
                continue
            content = node.get("content")
            if not isinstance(content, str) or not content.strip() or contains_answer_signal(content):
                continue
            role = node.get("role")
            if role not in {"user", "assistant"} or ref.get("resolved_role", role) != role:
                continue
            kind = ref.get("kind") or ("user_message" if role == "user" else "assistant_message")
            if kind not in {"user_message", "assistant_message", "independent_correct_use"}:
                continue
            tags = (node.get("metadata") or {}).get("evidence_tags", [])
            selected[ref["pointer"]] = (ordinal, {
                "sequence": node.get("sequence", ordinal + 1), "package_id": pid, "package_sha256": digest, "pointer": ref["pointer"],
                "resolved_node_sha256": node_sha, "kind": kind,
            }, role, content, tags)
        if file_sha256(doc) != doc_sha or _state_identity(state) != initial:
            return {**bundle, "reason": "evidence_changed_during_read"}
    except ERRORS:
        return {**bundle, "reason": "original_capture_unavailable"}
    if not selected:
        return {**bundle, "reason": "no_readable_exact_message"}
    bundle["original_sentence"] = original
    coverage["original_sentence"] = "verified"
    source_ref = {"package_id": pid, "package_sha256": digest, "pointer": "source.json#/identity/source_sentence",
                  "resolved_node_sha256": object_sha256(original), "kind": "source_text"}
    ordered = sorted(selected.values(), key=lambda row: row[0])
    first_candidates = [row for row in ordered if row[2] == "user" and (
        "first_attempt" in row[4] or FIRST.search(row[3])
        or (authored_first and authored_first == row[3]))]
    used = {source_ref["pointer"]: source_ref}
    phase_rows = {}
    for field in FIELDS[1:]:
        supplied = (roles or {}).get(field, [])
        rows = [selected[b["pointer"]] for b in supplied
                if b.get("pointer") in selected and (b.get("package_id"), b.get("package_sha256")) == (pid, digest)]
        if not supplied:
            tagged = [row for row in ordered if field in row[4] or (field == "hints" and "hint" in row[4])]
            rows = first_candidates if field == "first_attempt" and not tagged else tagged
        phase_rows[field] = rows if field == "hints" or len(rows) == 1 else []
    anchor = phase_rows["first_attempt"]
    initial_ordinal = anchor[0][0] if anchor and anchor[0][2] == "user" else None
    correction = phase_rows["correction"]
    correction_ordinal = correction[0][0] if correction and initial_ordinal is not None and correction[0][0] > initial_ordinal else None
    for field, rows in phase_rows.items():
        if field == "first_attempt":
            eligible = rows if initial_ordinal is not None else []
        elif field == "hints":
            eligible = [row for row in rows if row[2] == "assistant" and
                        (correction_ordinal is None or row[0] < correction_ordinal)]
        elif field == "correction":
            eligible = rows if correction_ordinal is not None else []
        else:
            eligible = [row for row in rows if row[2] == "user" and "independent_correct_use" in row[4]
                        and (correction_ordinal is not None and row[0] > correction_ordinal)]
        if not eligible:
            continue
        fields = [{"text": row[3], "pointer": row[1]["pointer"], "sequence": row[1]["sequence"]} for row in eligible]
        bundle[field] = fields if field == "hints" else fields[0]
        coverage[field] = "verified"
        for row in eligible:
            used[row[1]["pointer"]] = row[1]
    bundle["evidence_refs"] = list(used.values())
    bundle["attempt_id"] = "EN-ATTEMPT-" + object_sha256([
        pid, digest, identity["sentence_id"], initial_ordinal if initial_ordinal is not None
        else [row[1]["pointer"] for row in ordered],
    ])[:24].upper()
    # Report an exact second-read route rather than truncating hints or a quote.
    if len(canonical_bytes(bundle)) > max_bytes:
        bundle = {**bundle, **dict.fromkeys(FIELDS), "evidence_refs": [],
                  "coverage": {k: "available_on_exact_read" if v == "verified" else v for k, v in coverage.items()},
                  "readback": {"source_id": identity["source_id"], "sentence_id": identity["sentence_id"],
                               "source_hash": identity["source_hash"], "sentence_sha256": identity["sentence_sha256"],
                               "package_id": pid, "episode_id": episode_id}}
    return bundle


def _bind_legacy_episode_refs(episode: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize old file+message locators only when they identify this same package."""
    refs = episode.get("evidence", [])
    latest = record["latest_episode"]
    pid, digest = latest["package_id"], latest["package_sha256"]
    episode_pid = re.split(r"[:/]", episode["episode_id"], maxsplit=1)[0]
    result = []
    for ref in refs:
        location = ref.get("path")
        if not isinstance(location, str) or Path(location).name != "conversation.json" or ref.get("package_id"):
            result.append(ref)
            continue
        path = Path(location)
        parent = path.parent.name
        if parent and parent != pid:
            # Multiple/ambiguous package locators cannot define one attempt.
            return []
        if not (parent == pid or episode_pid == pid) or not DIGEST.fullmatch(str(ref.get("sha256", ""))):
            return []
        result.append({**ref, "member": "conversation.json", "package_id": pid, "package_sha256": digest})
    return result


def bundle_for_episode(repo: Path, state: Path, source_id: str, unit_id: str,
                       episode: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Resolve the selected memory episode, never a nearby support observation."""
    from .reading_preparation import DEFAULT_OUTPUT, query
    from .sources import resolve_article_source

    try:
        selected = query(repo / DEFAULT_OUTPUT, repo_root=repo, source_id=source_id, unit_id=unit_id)
        unit = selected["unit"]
        article = resolve_article_source(repo, selected["source_article"])
        if (unit["kind"] != "article" or unit["answer_exposure"] != "answer_free" or unit.get("question_id")
                or (article["source_id"], article["source_hash"]) != (selected["source_id"], selected["source_hash"])):
            raise ValidationError("memory episode is outside a current exact practice sentence")
        identity = {"source_id": selected["source_id"], "source_hash": selected["source_hash"],
                    "sentence_id": unit_id, "sentence_sha256": sentence_sha256(unit["text"])}
        roles = None
        refs = episode.get("evidence", [])
        try:
            from .sentence_visuals import _read_entry, _review_for_record
            entry, record = _read_entry(state, identity["source_id"], unit_id)
            if any(record["source_identity"].get(key) != value for key, value in identity.items()):
                raise ValidationError("active sentence support differs from this episode")
            refs = _bind_legacy_episode_refs(episode, record)
            review = _review_for_record(state, entry, record)
            roles = (review or {}).get("evidence_roles")
        except ERRORS:
            pass  # The exact memory locators remain independently verifiable.
        return attempt_bundle(repo, state, identity, refs, episode_id=episode["episode_id"],
                              authored_first=episode.get("first_attempt"), roles=roles, max_bytes=max_bytes)
    except ERRORS:
        return {**dict.fromkeys(FIELDS), "evidence_refs": [], "coverage": dict.fromkeys(FIELDS, "unknown"),
                "episode_id": episode["episode_id"], "reason": "current_exact_sentence_unavailable"}
