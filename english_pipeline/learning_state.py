"""Formal learning events and fresh, bounded English tutoring reads.

This module performs no semantic inference. The existing typed writer owns event
decisions; consumers reopen current formal bytes on every request. Sentence support
is optional evidence, never a second copy of current mastery or word meanings.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .constants import MASTER_HEADER, MASTERED_HEADER
from .errors import IdempotencyConflict, ValidationError
from .formal import read_csv
from .review_status import effective_review_status, effective_mastered_norms, effective_mastered_ids, load_review_status_ledger
from .util import canonical_bytes, file_sha256, normalize_item, object_sha256, parse_iso_date, utc_now, atomic_write_json


EVENT_SCHEMA = "english_formal_learning_event_v1"
EVENT_FIELDS = {
    "event_key", "kind", "origin", "task_id", "source_commit", "source_id", "unit_id",
    "bank_ids", "sentence_pattern_ids", "concept_ids", "observed_at", "outcome",
    "hint_dependence", "reasoning_status", "user_response", "reasoning", "correction",
}
KINDS = {"answer", "correction", "review", "adjudication"}
OUTCOMES = {"correct", "incorrect", "unresolved", "accepted", "modified", "rejected"}
ORIGINS = {"daily", "project-A", "project-B", "project-C", "project-D", "project-E"}
_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def learning_events_path(repo_root: Path) -> Path:
    return repo_root / "bank" / "learning_events.jsonl"


def load_learning_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise ValidationError("formal learning ledger must be a regular file")
    rows = []
    previous = "0" * 64
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or set(row) != {
            "schema_version", "subject", "sequence", "event_id", "event", "evidence_refs",
            "visibility", "previous_sha256", "record_sha256",
        }:
            raise ValidationError("formal learning event fields are invalid")
        core = {key: value for key, value in row.items() if key != "record_sha256"}
        if (row["schema_version"] != EVENT_SCHEMA or row["subject"] != "english"
                or row["sequence"] != len(rows) + 1 or row["previous_sha256"] != previous
                or row["visibility"] not in {"answer_free", "protected"}
                or object_sha256(core) != row["record_sha256"]):
            raise ValidationError("formal learning event chain is invalid")
        event = row["event"]
        _validate_event_fields(event)
        if row["event_id"] != "EN-LEARN-" + object_sha256({"event_key": event["event_key"]})[:24].upper():
            raise ValidationError("formal learning event identity does not match its stable key")
        if event["event_key"] in keys:
            raise ValidationError("duplicate formal learning event key")
        keys.add(event["event_key"])
        previous = row["record_sha256"]
        rows.append(row)
    return rows


def _validate_event_fields(event: Any) -> None:
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise ValidationError("learning_event_append requires the exact formal event fields")
    for key in ("event_key", "source_id", "unit_id", "observed_at"):
        if not isinstance(event[key], str) or not event[key].strip():
            raise ValidationError(f"formal learning event requires {key}")
    if event["kind"] not in KINDS or event["origin"] not in ORIGINS or event["outcome"] not in OUTCOMES:
        raise ValidationError("formal learning event classification is invalid")
    if event["hint_dependence"] not in {"none", "hint", "explanation", "not_observed"}:
        raise ValidationError("formal learning event requires observed hint dependence")
    if event["reasoning_status"] not in {"valid", "invalid", "not_observed"}:
        raise ValidationError("formal learning event reasoning status is invalid")
    for key in ("user_response", "reasoning", "correction"):
        if not isinstance(event[key], str):
            raise ValidationError(f"formal learning event {key} must preserve text")
    for key in ("bank_ids", "sentence_pattern_ids", "concept_ids"):
        values = event[key]
        if not isinstance(values, list) or len(values) > 16 or any(not isinstance(x, str) or not x.strip() for x in values) or len(values) != len(set(values)):
            raise ValidationError(f"formal learning event {key} must be a bounded unique list")
    parse_iso_date(event["observed_at"])
    if datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00")).tzinfo is None:
        raise ValidationError("formal learning observations require an explicit timezone")
    if event["origin"] != "daily":
        if not isinstance(event["task_id"], str) or not event["task_id"].strip() or not _COMMIT.fullmatch(str(event["source_commit"] or "")):
            raise ValidationError("web learning backflow requires task_id and the actually read source_commit")
    elif event["task_id"] is not None or event["source_commit"] is not None:
        raise ValidationError("daily learning does not fabricate web task provenance")
    if event["kind"] == "adjudication":
        if event["origin"] not in {"project-A", "project-E"} or event["outcome"] not in {"accepted", "modified", "rejected"}:
            raise ValidationError("A adjudication must preserve accepted/modified/rejected, not mastery")
    elif event["outcome"] not in {"correct", "incorrect", "unresolved"}:
        raise ValidationError("a real learning event cannot use proposal adjudication as an answer")


def validate_learning_event(event: Any, resolved: list[dict[str, Any]], *, study_date: str,
                            package_documents: list[dict[str, Any]]) -> str:
    _validate_event_fields(event)
    if parse_iso_date(event["observed_at"]) != study_date and not (event["kind"] == "adjudication" and event["origin"] == "project-E"):
        raise ValidationError("formal learning event must retain the original package study date")
    user_text = [str(row["value"].get("content", "")) for row in resolved
                 if row["binding"].get("resolved_role") == "user" and isinstance(row["value"], dict)]
    def text_values(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [text for child in value.values() for text in text_values(child)]
        if isinstance(value, list):
            return [text for child in value for text in text_values(child)]
        return []
    evidence_text = [text for row in resolved for text in text_values(row["value"])]
    if event["kind"] != "adjudication":
        if not event["user_response"].strip() or not any(event["user_response"] in text for text in user_text):
            raise ValidationError("real learning backflow requires the verbatim user response in package evidence")
    for key in ("reasoning", "correction"):
        if event[key] and not any(event[key] in text for text in evidence_text):
            raise ValidationError(f"formal {key} is absent from the bound raw evidence")
    if event["reasoning_status"] != "not_observed" and not event["reasoning"].strip():
        raise ValidationError("observed reasoning status requires the actual reasoning text")
    if event["kind"] in {"answer", "review"} and event["reasoning_status"] != "not_observed" and not any(event["reasoning"] in text for text in user_text):
        raise ValidationError("learner reasoning must come from the actual user message")
    if event["kind"] in {"answer", "review"} and event["outcome"] == "correct" and event["hint_dependence"] == "none" and event["reasoning_status"] == "valid":
        if not any(row["binding"]["kind"] == "independent_correct_use" for row in resolved):
            raise ValidationError("independent success requires exact independent_correct_use user evidence")
    frozen = {row["package_id"]: row for row in package_documents}
    source_matches = False
    answer_safe = True
    web_matches = event["origin"] == "daily"
    actual_adjudication_time_matches = event["kind"] != "adjudication" or event["origin"] != "project-E"
    for row in resolved:
        package = frozen[row["binding"]["package_id"]]
        identity = load_json_document(Path(package["path"]) / "source.json").get("identity", {})
        if identity.get("answer_exposure") != "answer_free" or identity.get("source_kind") in {"question", "option", "explanation"}:
            answer_safe = False
        units = {identity.get(key) for key in ("sentence_id", "question_id", "paragraph_id", "knowledge_point_id")}
        if identity.get("source_id") == event["source_id"]:
            if event["unit_id"] in units:
                source_matches = True
            provenance = identity.get("web_result", {})
            if isinstance(provenance, dict) and provenance == {
                "origin": event["origin"], "task_id": event["task_id"], "source_commit": event["source_commit"]
            }:
                web_matches = True
                package_manifest = load_json_document(Path(package["path"]) / "manifest.json")
                if identity.get("artifact_capture") is True and package_manifest.get("created_at") == event["observed_at"]:
                    actual_adjudication_time_matches = True
    if not source_matches:
        raise ValidationError("formal learning event source/unit is not bound by its package")
    if not web_matches:
        raise ValidationError("web result version/task provenance is not bound by the source package")
    if not actual_adjudication_time_matches:
        raise ValidationError("English web adjudication must preserve the actual local processing timestamp")
    return "answer_free" if answer_safe else "protected"


def append_learning_event(records: list[dict[str, Any]], event: dict[str, Any],
                          bindings: list[dict[str, Any]], *, visibility: str) -> tuple[dict[str, Any], bool]:
    previous = next((row for row in records if row["event"]["event_key"] == event["event_key"]), None)
    if previous is not None:
        if previous["event"] != event or previous["visibility"] != visibility:
            raise IdempotencyConflict("formal learning event key already binds a different result")
        return previous, False
    core = {"schema_version": EVENT_SCHEMA, "subject": "english", "sequence": len(records) + 1,
            "event_id": "EN-LEARN-" + object_sha256({"event_key": event["event_key"]})[:24].upper(),
            "event": event, "evidence_refs": bindings, "visibility": visibility,
            "previous_sha256": records[-1]["record_sha256"] if records else "0" * 64}
    row = {**core, "record_sha256": object_sha256(core)}
    records.append(row)
    return row, True


def serialize_learning_events(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(canonical_bytes(row) + b"\n" for row in records)


def load_json_document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValidationError("English JSON source must be an object")
    return value


def formal_versions(repo_root: Path) -> dict[str, str | None]:
    return {relative: file_sha256(repo_root / relative) if (repo_root / relative).is_file() else None
            for relative in ("bank/master_bank.csv", "bank/mastered_items.csv", "bank/sentence_patterns.md",
                             "bank/review_exclusion_ledger.jsonl", "bank/learning_events.jsonl", "bank/concept_aliases.json")}


def invalidate_after_formal(repo_root: Path, state_dir: Path, *, event_id: str) -> dict[str, Any]:
    versions = formal_versions(repo_root)
    value = {"schema_version": "english_learning_context_version_v1", "subject": "english",
             "event_id": event_id, "formal_version": object_sha256(versions), "source_hashes": versions,
             "reader_policy": "reopen_current_formal_sources_each_request"}
    target = state_dir / "learning-context" / "current.json"
    if not target.is_file() or load_json_document(target) != value:
        atomic_write_json(target, value)
    manifest_path = repo_root / "raw/reference_relations/writing-vocabulary-foundation/manifest.json"
    stale = []
    graph_invalid = False
    if manifest_path.is_file():
        try:
            graph = load_json_document(manifest_path)
            for row in graph.get("inputs", []):
                if row.get("path") in versions and row.get("sha256") != versions[row["path"]]:
                    stale.append(row["path"])
            for path_text, expected in graph.get("optional_inputs", {}).items():
                if (repo_root / path_text).is_file() != bool(expected.get("exists")):
                    stale.append(path_text)
            review_path = "bank/review_exclusion_ledger.jsonl"
            if versions[review_path] is not None and review_path not in {row.get("path") for row in graph.get("inputs", [])}:
                stale.append(review_path)
        except (OSError, ValueError, ValidationError, TypeError, AttributeError):
            graph_invalid = True
    marker = {"schema_version": "english_derived_dependency_status_v1", "formal_version": value["formal_version"],
              "graph_status": "invalid" if graph_invalid else "stale" if stale else "current" if manifest_path.is_file() else "missing",
              "changed_graph_inputs": sorted(set(stale)), "formal_write_count": 0}
    marker_path = state_dir / "learning-context" / "derived-status.json"
    if not marker_path.is_file() or load_json_document(marker_path) != marker:
        atomic_write_json(marker_path, marker)
    return value


def _sentence_patterns(repo: Path) -> dict[str, dict[str, str]]:
    text = (repo / "bank/sentence_patterns.md").read_text(encoding="utf-8")
    matches = list(re.finditer(r"^## (SP-\d{3,})｜(.+)$", text, re.MULTILINE))
    return {m.group(1): {"id": m.group(1), "title": m.group(2),
                         "text": text[m.start():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()}
            for i, m in enumerate(matches)}


def _article_episode(repo: Path, source_article: str, sentence_id: str | None) -> dict[str, Any] | None:
    """Return an existing exact article record; never turn it into a new event."""
    if not sentence_id or not re.fullmatch(r"S\d+", sentence_id):
        return None
    path = (repo / source_article).resolve(strict=True)
    path.relative_to(repo)
    text = path.read_text(encoding="utf-8")
    headings = list(re.finditer(r"^###\s+([^\n]+)$", text, re.MULTILINE))
    selected = []
    number = int(sentence_id[1:])
    for i, heading in enumerate(headings):
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*[｜|]\s*S(\d+)(?:\s*[-–]\s*S?(\d+))?", heading.group(1).strip())
        if match is None or not int(match.group(2)) <= number <= int(match.group(3) or match.group(2)):
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        selected.append({"observed_date": match.group(1), "text": text[heading.start():end].strip(),
                         "start_line": text.count("\n", 0, heading.start()) + 1})
    if not selected:
        return None
    return {"evidence_type": "existing_article_record", "source_path": source_article,
            "source_file_sha256": file_sha256(path), "sentence_id": sentence_id, "records": selected,
            "not_a_synthesized_conversation": True}


def _verify_event_evidence(repo: Path, state_dir: Path, rows: list[dict[str, Any]]) -> None:
    """Verify only used local evidence; archived history needs its exact locator.

    Archive bytes are not read on the teaching hot path. Missing local bytes without
    a verified locator cannot be replaced by an old support hash or a summary.
    """
    checked: set[tuple[str, str]] = set()
    for row in rows:
        for ref in row["evidence_refs"]:
            package_id = str(ref.get("package_id", ""))
            match = re.fullmatch(r"EN-PKG-(\d{4})(\d{2})(\d{2})-[0-9A-F]{16}", package_id)
            if match is None:
                raise ValidationError("learning evidence package identity is invalid")
            key = (package_id, str(ref.get("package_sha256", "")))
            if key in checked:
                continue
            day = "-".join(match.groups())
            root = state_dir / "packages" / day / package_id
            if root.is_dir():
                manifest = load_json_document(root / "manifest.json")
                core = {field: manifest[field] for field in ("schema_version", "package_id", "subject", "study_date", "files")}
                if manifest.get("subject") != "english" or object_sha256(core) != key[1]:
                    raise ValidationError("learning evidence package manifest changed")
                for relative in ("conversation.json", "source.json"):
                    if file_sha256(root / relative) != manifest["files"][relative]["sha256"]:
                        raise ValidationError("learning evidence original text changed")
                for attachment in manifest.get("attachments", []):
                    if attachment.get("role") == "user_work_image":
                        path = (root / attachment["path"]).resolve(strict=True)
                        path.relative_to(root.resolve())
                        if file_sha256(path) != attachment["sha256"]:
                            raise ValidationError("learning evidence user image changed")
            else:
                from .archive import _parse_locator
                pointer = load_json_document(state_dir / "archive-pointers" / day / f"{package_id}.json")
                locator_path = repo / "wiki/raw_archives" / f"{package_id}.md"
                locator = _parse_locator(locator_path.read_text(encoding="utf-8"))
                if (pointer.get("package_sha256") != key[1] or pointer.get("package_id") != package_id
                        or Path(str(pointer.get("archive_locator_path", ""))).resolve() != locator_path.resolve()
                        or locator.get("type") != "raw_archive_locator" or locator.get("subject") != "english"
                        or locator.get("archive_status") != "verified" or locator.get("raw_archive_package_sha256") != key[1]
                        or package_id not in locator.get("retrieval_keys", [])):
                    raise ValidationError("learning evidence archive locator changed")
            checked.add(key)


def query_learning_context(repo_root: Path, state_dir: Path, *, source_id: str | None = None,
                           sentence_id: str | None = None, source_article: str | None = None,
                           source_hash: str | None = None, sentence_sha256_value: str | None = None,
                           bank_ids: list[str] | None = None, sp_ids: list[str] | None = None,
                           concept_ids: list[str] | None = None, aliases: list[str] | None = None,
                           max_bytes: int = 16 * 1024, allow_protected: bool = False) -> dict[str, Any]:
    from .sentence_support import query_sentence_support, _ANSWER_LEAK_RE
    from .sources import resolve_article_source
    repo = repo_root.resolve(strict=True)
    if max_bytes < 512 or max_bytes > 256 * 1024:
        raise ValidationError("learning context byte budget is out of bounds")
    requested_bank, requested_sp = set(bank_ids or []), set(sp_ids or [])
    requested_concepts = set(concept_ids or [])
    if len(requested_bank | requested_sp | requested_concepts | set(aliases or [])) > 16:
        raise ValidationError("learning context identity set is too broad")
    before = formal_versions(repo)
    bank_rows = read_csv(repo / "bank/master_bank.csv", MASTER_HEADER)
    mastered = read_csv(repo / "bank/mastered_items.csv", MASTERED_HEADER)
    reviews = load_review_status_ledger(repo / "bank/review_exclusion_ledger.jsonl")
    events = load_learning_events(learning_events_path(repo))
    patterns = _sentence_patterns(repo)
    by_bank = {row["id"]: row for row in bank_rows}
    if len(by_bank) != len(bank_rows):
        raise ValidationError("formal bank IDs are ambiguous")
    known: dict[str, set[str]] = {}
    for row in bank_rows:
        known.setdefault(normalize_item(row["item"]), set()).add("bank:" + row["id"])
    for key, row in patterns.items():
        known.setdefault(normalize_item(row["title"]), set()).add("sp:" + key)
    alias_path = repo / "bank/concept_aliases.json"
    if alias_path.is_file():
        document = load_json_document(alias_path)
        if document.get("subject") != "english" or document.get("schema_version") != "english_concept_aliases_v1":
            raise ValidationError("concept aliases must belong to English")
        for alias, targets in document.get("aliases", {}).items():
            if not isinstance(targets, list) or any(not isinstance(x, str) for x in targets):
                raise ValidationError("concept alias targets are invalid")
            known.setdefault(normalize_item(alias), set()).update(targets)
    ambiguous = {}
    missing_aliases = []
    for alias in aliases or []:
        values = known.get(normalize_item(alias), set())
        if len(values) > 1:
            ambiguous[alias] = sorted(values)
        elif values:
            requested_concepts.update(values)
        else:
            missing_aliases.append(alias)
    if ambiguous:
        return {"status": "unavailable", "reason": "ambiguous_alias", "subject": "english", "aliases": ambiguous,
                "formal_write_count": 0}
    for key in requested_concepts:
        if key.startswith("bank:"):
            requested_bank.add(key[5:])
        elif key.startswith("sp:"):
            requested_sp.add(key[3:])
        elif not any(key in values for values in known.values()):
            return {"status": "unavailable", "reason": "unknown_concept", "subject": "english", "concept_id": key,
                    "formal_write_count": 0}
    support: dict[str, Any] = {"status": "unavailable", "reason": "not_requested", "records": []}
    source_status = "current_input_only"
    binding = None
    article_episode = None
    if source_article:
        binding = resolve_article_source(repo, source_article)
        if source_id and source_id != binding["source_id"]:
            raise ValidationError("learning context source identity mismatch")
        source_id = binding["source_id"]
        if source_hash and source_hash != binding["source_hash"]:
            # A supplied identity is a constraint, not a hint to silently refresh.
            # Stop before loading article/support history or implicit formal refs.
            return {"status": "unavailable", "reason": "source_changed", "subject": "english",
                    "source_status": "source_changed", "fallback": "current_input_only",
                    "formal_write_count": 0, "background_processing": "none"}
        source_status = "verified_current_source"
        source_hash = binding["source_hash"]
        article_episode = _article_episode(repo, binding["source_article"], sentence_id)
        if article_episode and not allow_protected and _ANSWER_LEAK_RE.search(json.dumps(article_episode, ensure_ascii=False)):
            article_episode = None
    if source_id and sentence_id:
        support = query_sentence_support(state_dir, source_id=source_id, sentence_id=sentence_id,
                                         source_hash=source_hash, sentence_sha256_value=sentence_sha256_value,
                                         max_records=1, max_bytes=max_bytes)
        if source_article is None and not (source_hash and sentence_sha256_value):
            support = {"status": "unavailable", "reason": "current_source_hashes_required", "records": []}
        if support.get("status") == "ready":
            for row in support["records"]:
                refs = row["record"].get("formal_refs", {})
                requested_bank.update(refs.get("vocabulary", []))
                requested_sp.update(refs.get("sentence_patterns", []))
    for row in events:
        event = row["event"]
        if (allow_protected or row["visibility"] == "answer_free") and source_id and sentence_id and event["source_id"] == source_id and event["unit_id"] == sentence_id:
            requested_bank.update(event["bank_ids"])
            requested_sp.update(event["sentence_pattern_ids"])
            requested_concepts.update(event["concept_ids"])
    requested_bank.update(key[5:] for key in requested_concepts if key.startswith("bank:"))
    requested_sp.update(key[3:] for key in requested_concepts if key.startswith("sp:"))
    if len(requested_bank) + len(requested_sp) > 16:
        return {"status": "unavailable", "reason": "oversize", "subject": "english", "formal_write_count": 0}
    missing_ids = sorted((requested_bank - by_bank.keys()) | (requested_sp - patterns.keys()))
    if missing_ids:
        return {"status": "unavailable", "reason": "formal_source_deleted", "subject": "english",
                "missing_ids": missing_ids, "formal_write_count": 0}
    requested_concepts.update("bank:" + key for key in requested_bank)
    requested_concepts.update("sp:" + key for key in requested_sp)
    related_all = [row for row in events if (
        set(row["event"]["bank_ids"]) & requested_bank
        or set(row["event"]["sentence_pattern_ids"]) & requested_sp
        or set(row["event"]["concept_ids"]) & requested_concepts
        or (source_id and row["event"]["source_id"] == source_id and row["event"]["unit_id"] == sentence_id)
    )]
    related = sorted([row for row in related_all if allow_protected or row["visibility"] == "answer_free"],
                     key=lambda row: (datetime.fromisoformat(row["event"]["observed_at"].replace("Z", "+00:00")).astimezone(timezone.utc), row["sequence"]))
    excluded = effective_mastered_norms(mastered, reviews)
    mastered_ids = effective_mastered_ids(mastered, reviews)
    current_review = effective_review_status(reviews)
    formal_records = []
    used_state_events = []
    for key in sorted(requested_bank):
        row = by_bank[key]
        if not allow_protected and _ANSWER_LEAK_RE.search(json.dumps(row, ensure_ascii=False)):
            return {"status": "unavailable", "reason": "formal_record_protected", "subject": "english",
                    "formal_write_count": 0}
        matching = [event for event in related if event["event"]["kind"] in {"answer", "review"}
                    and (key in event["event"]["bank_ids"] or "bank:" + key in event["event"]["concept_ids"])]
        state = "mastered" if normalize_item(row["item"]) in excluded or key in mastered_ids else "active"
        review = current_review.get(normalize_item(row["item"]))
        proof_date = max((str(proof.get("mastered_date", "")) for proof in mastered
                          if proof.get("matched_id") == key or normalize_item(str(proof.get("item", ""))) == normalize_item(row["item"])), default="")
        if review and str(review["study_date"]) >= proof_date:
            state = {"unmastered_reactivated": "active_recurrence", "independent_correct_use": "independent_correct_observed",
                     "mastered_sentence_nonreport": "review_excluded"}.get(review["status"], state)
        latest_state = None
        if matching:
            used_state_events.append(matching[-1])
            latest = matching[-1]["event"]
            if latest["kind"] != "adjudication":
                if latest["outcome"] == "incorrect" or latest["reasoning_status"] == "invalid":
                    latest_state = "active_recurrence"
                elif latest["outcome"] == "correct" and latest["hint_dependence"] == "none" and latest["reasoning_status"] == "valid":
                    latest_state = "independent_correct_observed"
                else:
                    latest_state = "guided_or_unresolved"
        safe_review = {field: review[field] for field in ("status", "event_id", "bank_id", "study_date")} if review else None
        formal_records.append({"concept_id": "bank:" + key, "record": row, "effective_state": state,
                               "latest_learning_state": latest_state,
                               "review_status": safe_review,
                               "source_path": "bank/master_bank.csv"})
    if not allow_protected and any(_ANSWER_LEAK_RE.search(patterns[key]["text"]) for key in requested_sp):
        return {"status": "unavailable", "reason": "formal_record_protected", "subject": "english", "formal_write_count": 0}
    # Keep the first episode and the latest seven, with explicit coverage; no
    # latest-only claim and no silent replacement of raw conversation evidence.
    selected = related if len(related) <= 8 else [related[0], *related[-7:]]
    known_event_ids = {row["event_id"] for row in events}
    if any(isinstance(evidence, dict) and evidence.get("learning_event_id") not in known_event_ids
           for row in reviews if row.get("bank_id") in requested_bank
           for evidence in row.get("sentence_evidence", []) if isinstance(evidence, dict) and evidence.get("learning_event_id")):
        return {"status": "unavailable", "reason": "learning_evidence_missing", "subject": "english", "formal_write_count": 0}
    try:
        _verify_event_evidence(repo, state_dir, selected + used_state_events)
    except (OSError, ValueError, ValidationError, KeyError, TypeError):
        return {"status": "unavailable", "reason": "learning_evidence_missing_or_changed", "subject": "english", "formal_write_count": 0}
    after = formal_versions(repo)
    if binding is not None:
        try:
            if resolve_article_source(repo, binding["source_article"]) != binding:
                return {"status": "unavailable", "reason": "source_changed_during_read", "subject": "english", "formal_write_count": 0}
            if article_episode and file_sha256(repo / binding["source_article"]) != article_episode["source_file_sha256"]:
                return {"status": "unavailable", "reason": "source_changed_during_read", "subject": "english", "formal_write_count": 0}
        except (OSError, ValueError, ValidationError):
            return {"status": "unavailable", "reason": "source_changed_during_read", "subject": "english", "formal_write_count": 0}
    if before != after:
        return {"status": "unavailable", "reason": "formal_changed_during_read", "subject": "english", "formal_write_count": 0}
    result = {"schema_version": "english_learning_context_v1", "status": "ready", "subject": "english",
              "formal_version": object_sha256(after), "source_hashes": after,
              "context_version": object_sha256({"formal": after, "source": binding, "article_episode": article_episode}),
              "source_status": source_status, "source_id": source_id, "sentence_id": sentence_id,
              "sentence_support": support, "formal_records": formal_records,
              "recorded_article_episode": article_episode,
              "sentence_patterns": [patterns[key] for key in sorted(requested_sp)],
              "learning_history": selected, "related_event_count": len(related),
              "protected_event_count": len(related_all) - len(related),
              "history_scope": "matching_recorded_formal_events; article records are separate evidence, not reconstructed conversations",
              "history_coverage": "all_matching_recorded_events" if len(selected) == len(related) else "bounded_first_and_recent",
              "missing_aliases": missing_aliases, "formal_write_count": 0, "background_processing": "none"}
    if len(canonical_bytes(result)) > max_bytes:
        return {"status": "unavailable", "reason": "oversize", "subject": "english", "formal_write_count": 0}
    return result
