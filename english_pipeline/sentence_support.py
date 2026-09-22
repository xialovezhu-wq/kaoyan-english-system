from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from .errors import IdempotencyConflict, ValidationError
from .packages import (
    validate_canonical_writer_closeout,
    validate_conversation_package,
    validate_evidence_refs,
)
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_bytes,
    file_sha256,
    load_json,
    object_sha256,
    sentence_sha256,
    utc_now,
    exclusive_lock,
)


PROPOSAL_SCHEMA_VERSION = "english_sentence_support_proposal_v1"
RECORD_SCHEMA_VERSION = "english_sentence_support_record_v1"
INDEX_SCHEMA_VERSION = "english_sentence_support_index_v1"
RECEIPT_SCHEMA_VERSION = "english_sentence_support_refresh_receipt_v1"
QUERY_SCHEMA_VERSION = "english_sentence_support_query_result_v1"
PREFLIGHT_SCHEMA_VERSION = "english_sentence_support_preflight_receipt_v1"

MAX_PROPOSAL_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 12 * 1024
MAX_INDEX_BYTES = 512 * 1024
MAX_SENTENCE_POINTER_BYTES = 32 * 1024
MAX_SOURCE_POINTER_BYTES = 128 * 1024
DEFAULT_QUERY_BYTES = 16 * 1024
MAX_QUERY_BYTES = 256 * 1024
DEFAULT_QUERY_RECORDS = 64
MAX_QUERY_RECORDS = 128
MAX_HISTORY_EPISODES = 8
MAX_CUE_VALUES = 8
MAX_VOCABULARY_CANDIDATES = 16
MAX_QUERY_HISTORY_EPISODES = 2
MAX_QUERY_CUES_PER_FIELD = 2
MAX_QUERY_VOCABULARY_CANDIDATES = 8
MAX_GENERATION_CHAIN = 16
MAX_PACKAGE_BINDINGS = 32

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SOURCE_KINDS = {"article", "user_provided"}
_PROTECTED_SOURCE_KINDS = {"question", "option", "explanation"}
_HISTORY_STATES = {
    "independent",
    "guided",
    "unresolved",
    "transfer_verified",
}
_CUE_FIELDS = {
    "correct_observations",
    "first_breaks",
    "structure_cues",
    "meaning_constraints",
    "effective_explanations",
    "next_read_actions",
}
_VOCABULARY_KINDS = {
    "word",
    "phrase",
    "familiar_new_meaning",
    "function_expression",
}
_ANSWER_LEAK_RE = re.compile(
    r"(?:correct\s+answer|correct\s+option|standard\s+answer|answer\s+is)"
    r"\s*[:：]?\s*(?:option\s*)?[A-D]"
    r"|\b[A-D]\s+(?:is|was)\s+(?:correct|right|the\s+answer)\b"
    r"|(?:choose|select)\s+(?:option\s*)?[A-D]\b"
    r"|(?:I|we)\s+(?:chose|choose|selected)\s+(?:option\s*)?[A-D]\b"
    r"|(?:my|our)\s+answer\s*(?:is|was|[:：])\s*[A-D]\b"
    r"|(?:正确答案|正确选项|标准答案|应选|应该选|答案)"
    r"\s*[:：是为]?\s*(?:第?[一二三四1234]\s*项|[A-DＡ-Ｄ])"
    r"|第?[一二三四1234]\s*项\s*(?:正确|错误|不正确|是答案)"
    r"|(?:我|你)(?:选|选择|答)(?:了|的是)?\s*(?:第?[一二三四1234]\s*项|[A-DＡ-Ｄ])"
    r"|(?:我的|你的)答案\s*[:：是为]?\s*(?:第?[一二三四1234]\s*项|[A-DＡ-Ｄ])"
    r"|选项\s*[A-DＡ-Ｄ]\s*(?:正确|错误|不正确|应选)?",
    flags=re.IGNORECASE,
)


def current_index_path(state_dir: Path) -> Path:
    return state_dir.resolve() / "sentence-support" / "current" / "index.json"


def current_index_sha256(state_dir: Path) -> str | None:
    path = current_index_path(state_dir)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError("sentence-support current index must be a regular file")
    return file_sha256(path)


def support_receipt_path(state_dir: Path, study_date: str, batch_id: str) -> Path:
    return (
        state_dir.resolve()
        / "receipts"
        / "sentence-support"
        / study_date
        / f"{batch_id}.json"
    )


def support_preflight_receipt_path(
    state_dir: Path, study_date: str, batch_id: str
) -> Path:
    return (
        state_dir.resolve()
        / "receipts"
        / "sentence-support-preflight"
        / study_date
        / f"{batch_id}.json"
    )


def _lookup_token(*values: str) -> str:
    return hashlib.sha256("\u001f".join(values).encode("utf-8")).hexdigest()


def _sentence_pointer_path(
    generation_root: Path, source_id: str, sentence_id: str
) -> Path:
    token = _lookup_token(source_id, sentence_id)
    return generation_root / "sentences" / token[:2] / f"{token}.json"


def _source_pointer_path(generation_root: Path, source_id: str) -> Path:
    token = _lookup_token(source_id)
    return generation_root / "sources" / token[:2] / f"{token}.json"


def sentence_support_required(manifest: dict[str, Any]) -> bool:
    schema = manifest.get("schema_version")
    if schema == "english_nightly_manifest_v2":
        return False
    if schema != "english_nightly_manifest_v3":
        raise ValidationError("nightly manifest schema is unsupported")
    closures = manifest.get("required_postformal_closures")
    if (
        not isinstance(closures, list)
        or len(closures) != len(set(closures))
        or any(not isinstance(value, str) or not value for value in closures)
    ):
        raise ValidationError("nightly manifest postformal closure contract is invalid")
    if (
        manifest.get("postformal_contract_version") != "sentence-support-v1"
        or "sentence_support" not in closures
    ):
        raise ValidationError("nightly manifest does not require sentence-support closure")
    scope = manifest.get("sentence_support_history_scope")
    if (
        not isinstance(scope, dict)
        or set(scope) != {"mode", "selected_package_ids", "all_local_package_ids"}
        or scope.get("mode") not in {"full_local_date", "package_subset"}
        or not isinstance(scope.get("selected_package_ids"), list)
        or not isinstance(scope.get("all_local_package_ids"), list)
    ):
        raise ValidationError("nightly manifest sentence-support history scope is invalid")
    return True


def _regular_json(path: Path, *, label: str, max_bytes: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"{label} must be a regular file")
    size = path.stat().st_size
    if size <= 0 or size > max_bytes:
        raise ValidationError(f"{label} exceeds its bounded size")
    try:
        return load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{label} is not valid JSON") from exc


def _content_bytes(value: Any) -> bytes:
    return canonical_bytes(value) + b"\n"


def _write_content_object(directory: Path, value: dict[str, Any]) -> tuple[Path, str, int]:
    payload = _content_bytes(value)
    digest = hashlib.sha256(payload).hexdigest()
    path = directory / f"{digest}.json"
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise IdempotencyConflict("sentence-support content address collision")
    else:
        atomic_write_bytes(path, payload)
    return path, digest, len(payload)


def _read_content_object(
    state_root: Path,
    relative_path: str,
    digest: str,
    *,
    label: str,
    max_bytes: int,
) -> tuple[Path, dict[str, Any]]:
    if not _SHA256_RE.fullmatch(digest):
        raise ValidationError(f"{label} SHA-256 is invalid")
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"{label} path is unsafe")
    path = (state_root / relative).resolve(strict=True)
    try:
        path.relative_to(state_root)
    except ValueError as exc:
        raise ValidationError(f"{label} path escapes state root") from exc
    if path.name != f"{digest}.json" or file_sha256(path) != digest:
        raise ValidationError(f"{label} is not stored at its content address")
    return path, _regular_json(path, label=label, max_bytes=max_bytes)


def _required_exact(value: dict[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise ValidationError(f"{label} fields do not match the sentence-support contract")


def _source_identity_from_package(package_root: Path) -> dict[str, Any]:
    package = validate_conversation_package(package_root)
    source = _regular_json(
        package_root / "source.json",
        label="conversation package source",
        max_bytes=512 * 1024,
    )
    identity = source.get("identity")
    if not isinstance(identity, dict):
        raise ValidationError("conversation package source identity is invalid")
    source_id = str(identity.get("source_id") or "").strip()
    source_hash = str(identity.get("source_hash") or "").strip()
    if not source_id or not _SHA256_RE.fullmatch(source_hash):
        raise ValidationError("sentence-support requires source_id and source_hash")
    sentence_id = identity.get("sentence_id")
    paragraph_id = identity.get("paragraph_id")
    question_id = identity.get("question_id")
    knowledge_point_id = identity.get("knowledge_point_id")
    source_sentence = identity.get("source_sentence")
    raw_source_kind = identity.get("source_kind")
    source_kind = (
        str(raw_source_kind)
        if raw_source_kind in _SAFE_SOURCE_KINDS | _PROTECTED_SOURCE_KINDS
        else "unknown"
    )
    answer_exposure = (
        str(identity.get("answer_exposure"))
        if identity.get("answer_exposure") in {"answer_free", "protected"}
        else "unknown"
    )
    segment_candidates = [
        ("sentence", sentence_id),
        ("paragraph", paragraph_id),
        ("question", question_id),
        ("knowledge_point", knowledge_point_id),
    ]
    segment = next(
        ((kind, str(raw).strip()) for kind, raw in segment_candidates if isinstance(raw, str) and raw.strip()),
        None,
    )
    if segment is None:
        raise ValidationError("sentence-support package lacks an exact segment identity")
    if segment[0] == "sentence" and (
        not isinstance(source_sentence, str) or not source_sentence.strip()
    ):
        raise ValidationError("sentence-support sentence package lacks source_sentence")
    return {
        "source_id": source_id,
        "source_hash": source_hash,
        "segment_type": segment[0],
        "segment_id": segment[1],
        "sentence_id": str(sentence_id).strip() if isinstance(sentence_id, str) and sentence_id.strip() else None,
        "paragraph_id": str(paragraph_id).strip() if isinstance(paragraph_id, str) and paragraph_id.strip() else None,
        "question_id": str(question_id).strip() if isinstance(question_id, str) and question_id.strip() else None,
        "knowledge_point_id": (
            str(knowledge_point_id).strip()
            if isinstance(knowledge_point_id, str) and knowledge_point_id.strip()
            else None
        ),
        "source_kind": source_kind,
        "answer_exposure": answer_exposure,
        "source_sentence": source_sentence.strip() if isinstance(source_sentence, str) and source_sentence.strip() else None,
        "sentence_sha256": sentence_sha256(source_sentence) if isinstance(source_sentence, str) and source_sentence.strip() else None,
        "package_id": package["package_id"],
        "package_sha256": package["package_canonical_sha256"],
    }


def _validate_source_identity(proposed: Any, actual: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(proposed, dict):
        raise ValidationError("sentence-support source_identity must be an object")
    fields = {
        "source_id", "source_hash", "segment_type", "segment_id", "sentence_id",
        "paragraph_id", "question_id", "knowledge_point_id", "source_kind",
        "answer_exposure", "source_sentence", "sentence_sha256",
    }
    _required_exact(proposed, fields, "sentence-support source_identity")
    expected = {key: actual[key] for key in fields}
    if proposed != expected:
        raise ValidationError("sentence-support source identity differs from package evidence")
    return expected


def _support_key(source: dict[str, Any], *, package_id: str | None = None) -> str:
    base = "\u001f".join(
        (source["source_id"], source["segment_type"], source["segment_id"])
    )
    if (source.get("source_kind") in _SAFE_SOURCE_KINDS and source["segment_type"] == "sentence"
            and source.get("question_id") is None and source.get("answer_exposure") == "answer_free"):
        return base
    if not package_id:
        raise ValidationError("protected support requires its exact package identity")
    # Protected sidecars are never ordinary sentence pointers. Giving each its
    # own key prevents an A/answer supplement from replacing a safe source record
    # or another protected package's required closeout binding.
    return base + "\u001fprotected\u001f" + package_id


def _validate_evidence_indexes(
    indexes: Any,
    bindings: list[dict[str, Any]],
    *,
    label: str,
) -> list[int]:
    if not isinstance(indexes, list) or len(indexes) > len(bindings):
        raise ValidationError(f"{label} evidence_ref_indexes are invalid")
    result: list[int] = []
    for value in indexes:
        if not isinstance(value, int) or value < 0 or value >= len(bindings):
            raise ValidationError(f"{label} evidence_ref index is out of bounds")
        if value not in result:
            result.append(value)
    return result


def _validate_paragraph_context(
    value: Any, resolved_evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    bindings = [row["binding"] for row in resolved_evidence]
    if not isinstance(value, dict):
        raise ValidationError("paragraph_context must be an object")
    _required_exact(value, {"text", "locator", "evidence_ref_indexes"}, "paragraph_context")
    text = value.get("text")
    locator = value.get("locator")
    if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 4096):
        raise ValidationError("paragraph_context text is invalid")
    if locator is not None and (not isinstance(locator, str) or not locator.strip() or len(locator) > 1024):
        raise ValidationError("paragraph_context locator is invalid")
    indexes = _validate_evidence_indexes(
        value.get("evidence_ref_indexes"), bindings, label="paragraph_context"
    )
    if (text is not None or locator is not None) and not indexes:
        raise ValidationError("paragraph_context must bind exact package evidence")
    if locator is not None and locator not in {
        bindings[index].get("pointer") for index in indexes
    }:
        raise ValidationError("paragraph_context locator is not one of its evidence pointers")
    if text is not None:
        evidence_strings: list[str] = []
        for index in indexes:
            node = resolved_evidence[index]["value"]
            if isinstance(node, str):
                evidence_strings.append(node)
            elif isinstance(node, dict) and isinstance(node.get("content"), str):
                evidence_strings.append(node["content"])
        if not any(text.strip() in evidence for evidence in evidence_strings):
            raise ValidationError(
                "paragraph_context text must be a continuous substring of bound evidence"
            )
    return {
        "text": text.strip() if isinstance(text, str) else None,
        "locator": locator.strip() if isinstance(locator, str) else None,
        "evidence_refs": [bindings[index] for index in indexes],
    }


def _validate_history_episodes(
    value: Any,
    bindings: list[dict[str, Any]],
    *,
    study_date: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_HISTORY_EPISODES:
        raise ValidationError("history_episodes must be a bounded array")
    result: list[dict[str, Any]] = []
    for ordinal, episode in enumerate(value, start=1):
        if not isinstance(episode, dict):
            raise ValidationError(f"history episode {ordinal} must be an object")
        _required_exact(
            episode,
            {"state", "summary", "observed_date", "evidence_ref_indexes"},
            f"history episode {ordinal}",
        )
        state = episode.get("state")
        summary = episode.get("summary")
        observed_date = episode.get("observed_date")
        if state not in _HISTORY_STATES:
            raise ValidationError(f"history episode {ordinal} state is invalid")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 1024:
            raise ValidationError(f"history episode {ordinal} summary is invalid")
        if observed_date != study_date:
            raise ValidationError("new history episode observed_date must equal package study_date")
        indexes = _validate_evidence_indexes(
            episode.get("evidence_ref_indexes"),
            bindings,
            label=f"history episode {ordinal}",
        )
        if not indexes:
            raise ValidationError(f"history episode {ordinal} lacks package evidence")
        selected = [bindings[index] for index in indexes]
        if state in {"independent", "transfer_verified"} and not any(
            ref.get("kind") == "independent_correct_use" for ref in selected
        ):
            raise ValidationError(
                f"history episode {ordinal} cannot claim independent evidence"
            )
        result.append(
            {
                "state": state,
                "summary": summary.strip(),
                "observed_date": observed_date,
                "evidence_refs": selected,
            }
        )
    return result


def _validate_teaching_cues(
    value: Any, bindings: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise ValidationError("teaching_cues must be an object")
    _required_exact(value, _CUE_FIELDS, "teaching_cues")
    result: dict[str, list[dict[str, Any]]] = {}
    for key in sorted(_CUE_FIELDS):
        raw_values = value[key]
        if not isinstance(raw_values, list) or len(raw_values) > MAX_CUE_VALUES:
            raise ValidationError(f"teaching_cues.{key} must be a bounded array")
        validated: list[dict[str, Any]] = []
        for ordinal, cue in enumerate(raw_values, start=1):
            if not isinstance(cue, dict):
                raise ValidationError(f"teaching_cues.{key}[{ordinal}] must be an object")
            _required_exact(
                cue,
                {"text", "evidence_state", "evidence_ref_indexes"},
                f"teaching_cues.{key}[{ordinal}]",
            )
            text = cue.get("text")
            state = cue.get("evidence_state")
            if not isinstance(text, str) or not text.strip() or len(text) > 1024:
                raise ValidationError(f"teaching_cues.{key}[{ordinal}] text is invalid")
            if state not in _HISTORY_STATES:
                raise ValidationError(f"teaching_cues.{key}[{ordinal}] state is invalid")
            indexes = _validate_evidence_indexes(
                cue.get("evidence_ref_indexes"),
                bindings,
                label=f"teaching_cues.{key}[{ordinal}]",
            )
            if not indexes:
                raise ValidationError(f"teaching_cues.{key}[{ordinal}] lacks evidence")
            evidence_refs = [bindings[index] for index in indexes]
            if state in {"independent", "transfer_verified"} and not any(
                ref.get("kind") == "independent_correct_use"
                for ref in evidence_refs
            ):
                raise ValidationError(
                    f"teaching_cues.{key}[{ordinal}] cannot claim independent evidence"
                )
            validated.append(
                {
                    "text": text.strip(),
                    "evidence_state": state,
                    "evidence_refs": evidence_refs,
                }
            )
        result[key] = validated
    return result


def _validate_vocabulary_candidates(
    value: Any,
    bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_VOCABULARY_CANDIDATES:
        raise ValidationError("vocabulary_candidates must be a bounded array")
    result: list[dict[str, Any]] = []
    for ordinal, candidate in enumerate(value, start=1):
        if not isinstance(candidate, dict):
            raise ValidationError(f"vocabulary candidate {ordinal} must be an object")
        _required_exact(
            candidate,
            {"item", "kind", "evidence_ref_indexes"},
            f"vocabulary candidate {ordinal}",
        )
        item = candidate.get("item")
        kind = candidate.get("kind")
        if not isinstance(item, str) or not item.strip() or len(item) > 256:
            raise ValidationError(f"vocabulary candidate {ordinal} item is invalid")
        if kind not in _VOCABULARY_KINDS:
            raise ValidationError(f"vocabulary candidate {ordinal} kind is invalid")
        indexes = _validate_evidence_indexes(
            candidate.get("evidence_ref_indexes"),
            bindings,
            label=f"vocabulary candidate {ordinal}",
        )
        if not indexes:
            raise ValidationError(f"vocabulary candidate {ordinal} lacks package evidence")
        result.append(
            {
                "item": item.strip(),
                "kind": kind,
                "evidence_refs": [bindings[index] for index in indexes],
            }
        )
    return result


def _writer_package_closure(
    writer_receipt: dict[str, Any], package_id: str
) -> dict[str, Any]:
    rows = [
        row
        for row in writer_receipt.get("action_results", [])
        if any(
            isinstance(ref, dict) and ref.get("package_id") == package_id
            for ref in row.get("resolved_evidence", [])
        )
    ]
    completed = [row for row in rows if row.get("result") in {"applied", "skipped"}]
    outcomes: set[str] = set()
    formal_refs = {
        "vocabulary": [],
        "sentence_patterns": [],
        "mastery": [],
        "review": [],
    }
    action_closures: list[dict[str, Any]] = []
    for row in completed:
        action_type = str(row.get("action_type") or "")
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
        assigned_id = row.get("assigned_id")
        if assigned_id:
            target = (
                "vocabulary" if action_type.startswith("master_bank_")
                else "sentence_patterns" if action_type.startswith("sentence_pattern_")
                else "mastery" if action_type == "mastered_insert"
                else "review"
            )
            if str(assigned_id) not in formal_refs[target]:
                formal_refs[target].append(str(assigned_id))
        if row.get("learning_event_visibility", "answer_free") == "answer_free":
            for target in ("vocabulary", "sentence_patterns"):
                for formal_id in row.get("learning_event_targets", {}).get(target, []):
                    if str(formal_id) not in formal_refs[target]:
                        formal_refs[target].append(str(formal_id))
        closure = {
            "action_id": row.get("action_id"),
            "action_type": action_type,
            "target": row.get("target"),
            "result": row.get("result"),
        }
        if assigned_id:
            closure["assigned_id"] = str(assigned_id)
        action_closures.append(closure)
    return {
        "terminal_outcomes": sorted(outcomes),
        "formal_refs": {key: sorted(values) for key, values in formal_refs.items()},
        "action_closures": action_closures,
    }


def _merge_unique(previous: Iterable[Any], current: Iterable[Any], maximum: int) -> list[Any]:
    result: list[Any] = []
    fingerprints: set[str] = set()
    for value in [*previous, *current]:
        fingerprint = object_sha256(value)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        result.append(value)
    return result[-maximum:]


def _validate_proposal(
    proposal_path: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    writer_receipt: dict[str, Any] | None = None,
    *,
    expected_action_set_id: str | None = None,
    expected_completed_ids: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if proposal_path.is_symlink() or not proposal_path.is_file():
        raise ValidationError("sentence-support proposal must be a regular file")
    if proposal_path.stat().st_size > MAX_PROPOSAL_BYTES:
        raise ValidationError("sentence-support proposal exceeds its bounded size")
    proposal = _regular_json(
        proposal_path,
        label="sentence-support proposal",
        max_bytes=MAX_PROPOSAL_BYTES,
    )
    _required_exact(
        proposal,
        {
            "schema_version", "proposal_id", "batch_id", "batch_manifest_sha256",
            "action_set_id", "expected_index_sha256", "created_at", "producer",
            "records", "formal_write_count", "background_processing",
        },
        "sentence-support proposal",
    )
    if (
        proposal.get("schema_version") != PROPOSAL_SCHEMA_VERSION
        or not isinstance(proposal.get("proposal_id"), str)
        or not proposal["proposal_id"].strip()
        or proposal.get("batch_id") != manifest.get("batch_id")
        or proposal.get("batch_manifest_sha256") != file_sha256(manifest_path)
        or proposal.get("action_set_id")
        != (
            writer_receipt.get("action_set_id")
            if writer_receipt is not None
            else expected_action_set_id
        )
        or proposal.get("formal_write_count") != 0
        or proposal.get("background_processing") != "none"
    ):
        raise ValidationError("sentence-support proposal batch binding mismatch")
    expected_index = proposal.get("expected_index_sha256")
    if expected_index is not None and not _SHA256_RE.fullmatch(str(expected_index)):
        raise ValidationError("sentence-support expected_index_sha256 is invalid")
    producer = proposal.get("producer")
    if (
        not isinstance(producer, dict)
        or set(producer) != {"role", "model", "prompt_version"}
        or producer.get("role") != "sol_nightly_reviewer"
    ):
        raise ValidationError("sentence-support proposal producer is invalid")
    package_documents = manifest.get("package_documents", [])
    by_id = {row["package_id"]: row for row in package_documents}
    superseded_sources = set()
    if manifest.get("web_review_id"):
        from .web_review_gate import approval
        reviewed = approval(manifest_path.parent.parent.parent, manifest["web_review_id"])
        for correction in reviewed.get("corrections", []):
            corrected = by_id.get(correction["package_id"])
            original = by_id.get(correction["original_package_id"])
            if corrected and original and corrected["package_sha256"] == correction["package_sha256"]:
                superseded_sources.add(correction["original_package_id"])
    if writer_receipt is not None:
        dispositions = writer_receipt.get("package_dispositions", {})
        completed_ids = {
            package_id
            for package_id in manifest.get("package_ids", [])
            if dispositions.get(package_id) == "completed"
        }
    else:
        if expected_action_set_id is None or expected_completed_ids is None:
            raise ValidationError("sentence-support preflight lacks expected writer subset")
        completed_ids = set(expected_completed_ids)
    if not isinstance(proposal.get("records"), list):
        raise ValidationError("sentence-support proposal records must be an array")
    proposed_ids = [
        row.get("package_id") for row in proposal["records"] if isinstance(row, dict)
    ]
    deferred_artifacts = set()
    if writer_receipt is not None:
        for raw in proposal["records"]:
            pid = raw.get("package_id") if isinstance(raw, dict) else None
            if pid in completed_ids or pid not in by_id:
                continue
            identity = load_json(Path(by_id[pid]["path"]) / "source.json").get("identity", {})
            if (writer_receipt.get("package_dispositions", {}).get(pid) == "incomplete"
                    and identity.get("artifact_capture") is True
                    and raw.get("record_kind") == "protected_sidecar"
                    and raw.get("paragraph_context") == {"text": None, "locator": None, "evidence_ref_indexes": []}
                    and raw.get("history_episodes") == [] and raw.get("vocabulary_candidates") == []
                    and not any(raw.get("teaching_cues", {}).values())):
                # A preflight can include a retained administrative artifact that
                # the actual writer did not complete. Keep its immutable proposal,
                # but publish no support or completion claim for that artifact.
                deferred_artifacts.add(pid)
    if len(proposed_ids) != len(set(proposed_ids)) or set(proposed_ids) - deferred_artifacts != completed_ids:
        raise ValidationError(
            "sentence-support proposal must cover exactly the writer-completed package subset"
        )
    validated: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(proposal["records"], start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"sentence-support record proposal {ordinal} is invalid")
        _required_exact(
            raw,
            {
                "package_id", "package_sha256", "record_kind", "source_identity",
                "evidence_refs", "paragraph_context", "history_episodes",
                "teaching_cues", "vocabulary_candidates",
            },
            f"sentence-support record proposal {ordinal}",
        )
        package_id = raw["package_id"]
        if package_id in deferred_artifacts:
            continue
        package_record = by_id.get(package_id)
        if package_record is None or raw.get("package_sha256") != package_record.get("package_sha256"):
            raise ValidationError("sentence-support record is outside the frozen package set")
        package_root = Path(package_record["path"])
        actual_source = _source_identity_from_package(package_root)
        source = _validate_source_identity(raw.get("source_identity"), actual_source)
        if package_id in superseded_sources:
            # Preserve the raw identity for audit, but never install its mistaken
            # unit as an ordinary teaching pointer. The corrected native capture
            # owns the actual sentence and learner observations.
            source["answer_exposure"] = "protected"
        record_kind = raw.get("record_kind")
        expected_kind = (
            "sentence"
            if source["source_kind"] in _SAFE_SOURCE_KINDS
            and source["segment_type"] == "sentence"
            and source.get("question_id") is None
            and source.get("answer_exposure") == "answer_free"
            else "protected_sidecar"
        )
        if record_kind != expected_kind:
            raise ValidationError("sentence-support record_kind is inconsistent with source identity")
        resolved = validate_evidence_refs(raw.get("evidence_refs"), package_documents)
        bindings = [row["binding"] for row in resolved]
        if any(ref.get("package_id") != package_id for ref in bindings):
            raise ValidationError("one sentence-support record may bind only its own package")
        if record_kind == "sentence":
            allowed_evidence_kinds = {
                "user_message",
                "assistant_message",
                "source_text",
                "independent_correct_use",
            }
            if any(ref.get("kind") not in allowed_evidence_kinds for ref in bindings):
                raise ValidationError(
                    "answer-safe sentence support cannot reference protected attachments"
                )
            evidence_surface = json.dumps(
                [row["value"] for row in resolved],
                ensure_ascii=False,
                sort_keys=True,
            )
            if _ANSWER_LEAK_RE.search(evidence_surface):
                raise ValidationError(
                    "answer-safe sentence evidence contains an exam answer signal"
                )
        paragraph_context = _validate_paragraph_context(
            raw.get("paragraph_context"), resolved
        )
        history = _validate_history_episodes(
            raw.get("history_episodes"),
            bindings,
            study_date=manifest["study_date"],
        )
        cues = _validate_teaching_cues(raw.get("teaching_cues"), bindings)
        vocabulary = _validate_vocabulary_candidates(raw.get("vocabulary_candidates"), bindings)
        if record_kind == "protected_sidecar" and (
            paragraph_context["text"] is not None
            or paragraph_context["locator"] is not None
            or history
            or any(cues.values())
            or vocabulary
        ):
            raise ValidationError("protected sidecar cannot publish teaching or answer-bearing content")
        answer_surface = json.dumps(
            {
                "paragraph_context": paragraph_context,
                "history_episodes": history,
                "teaching_cues": cues,
                "vocabulary_candidates": vocabulary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if _ANSWER_LEAK_RE.search(answer_surface):
            raise ValidationError("sentence-support proposal contains an explicit exam answer leak")
        validated.append(
            {
                "package_id": package_id,
                "package_sha256": raw["package_sha256"],
                "record_kind": record_kind,
                "source_identity": source,
                "evidence_refs": bindings,
                "paragraph_context": paragraph_context,
                "history_episodes": history,
                "teaching_cues": cues,
                "vocabulary_candidates": vocabulary,
            }
        )
    order = {package_id: index for index, package_id in enumerate(manifest["package_ids"])}
    validated.sort(key=lambda row: order[row["package_id"]])
    return proposal, validated


def _empty_index() -> dict[str, Any]:
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "status": "READY",
        "entry_count": 0,
        "source_count": 0,
        "generation_id": None,
        "generation_path": None,
        "generation_tree_sha256": None,
        "generation_chain": [],
        "last_refresh": None,
        "generated_at": None,
    }


def _load_current_index(state_root: Path) -> tuple[dict[str, Any], str | None]:
    path = current_index_path(state_root)
    if not path.exists():
        return _empty_index(), None
    value = _regular_json(path, label="sentence-support current index", max_bytes=MAX_INDEX_BYTES)
    if (
        value.get("schema_version") != INDEX_SCHEMA_VERSION
        or value.get("status") != "READY"
        or not isinstance(value.get("entry_count"), int)
        or value.get("entry_count") < 0
        or not isinstance(value.get("source_count"), int)
        or value.get("source_count") < 0
    ):
        raise ValidationError("sentence-support current index is invalid")
    return value, file_sha256(path)


def _resolve_generation_root(
    state_root: Path, index: dict[str, Any]
) -> Path | None:
    value = index.get("generation_path")
    if value is None:
        if index.get("entry_count") == 0 and index.get("source_count") == 0:
            return None
        raise ValidationError("sentence-support index lacks generation path")
    relative = Path(str(value))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("sentence-support generation path is unsafe")
    generation_root = (state_root / relative).resolve(strict=True)
    expected_root = (state_root / "sentence-support" / "generations").resolve()
    try:
        generation_root.relative_to(expected_root)
    except ValueError as exc:
        raise ValidationError("sentence-support generation escapes generation root") from exc
    if not generation_root.is_dir() or generation_root.name != index.get("generation_id"):
        raise ValidationError("sentence-support generation identity mismatch")
    return generation_root


def _resolve_generation_chain(
    state_root: Path, index: dict[str, Any]
) -> list[Path]:
    raw_chain = index.get("generation_chain")
    if raw_chain is None:
        root = _resolve_generation_root(state_root, index)
        return [root] if root is not None else []
    if not isinstance(raw_chain, list) or len(raw_chain) > MAX_GENERATION_CHAIN:
        raise ValidationError("sentence-support generation chain is invalid")
    roots: list[Path] = []
    expected_root = (state_root / "sentence-support" / "generations").resolve()
    for item in raw_chain:
        if not isinstance(item, dict) or set(item) != {
            "generation_id", "generation_path", "generation_tree_sha256"
        }:
            raise ValidationError("sentence-support generation chain item is invalid")
        relative = Path(str(item["generation_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("sentence-support generation chain path is unsafe")
        root = (state_root / relative).resolve(strict=True)
        try:
            root.relative_to(expected_root)
        except ValueError as exc:
            raise ValidationError(
                "sentence-support generation chain escapes root"
            ) from exc
        if root.name != item["generation_id"]:
            raise ValidationError("sentence-support generation chain identity mismatch")
        roots.append(root)
    if roots:
        latest = raw_chain[0]
        if (
            index.get("generation_id") != latest["generation_id"]
            or index.get("generation_path") != latest["generation_path"]
            or index.get("generation_tree_sha256")
            != latest["generation_tree_sha256"]
        ):
            raise ValidationError("sentence-support latest generation chain binding mismatch")
    return roots


def _generation_tree_hash(generation_root: Path) -> str:
    return object_sha256(
        {
            path.relative_to(generation_root).as_posix(): file_sha256(path)
            for path in sorted(generation_root.rglob("*.json"))
            if path.is_file() and not path.is_symlink()
        }
    )


def _load_sentence_pointer(
    state_root: Path, source_id: str, sentence_id: str
) -> dict[str, Any] | None:
    path = _sentence_pointer_path(state_root, source_id, sentence_id)
    if not path.exists():
        return None
    pointer = _regular_json(
        path,
        label="sentence-support exact pointer",
        max_bytes=MAX_SENTENCE_POINTER_BYTES,
    )
    token = _lookup_token(source_id, sentence_id)
    if (
        pointer.get("schema_version")
        != "english_sentence_support_sentence_pointer_v1"
        or pointer.get("lookup_sha256") != token
        or pointer.get("source_id") != source_id
        or pointer.get("sentence_id") != sentence_id
        or not isinstance(pointer.get("entry"), dict)
        or pointer.get("pointer_payload_sha256")
        != object_sha256(
            {
                key: value
                for key, value in pointer.items()
                if key != "pointer_payload_sha256"
            }
        )
    ):
        raise ValidationError("sentence-support exact pointer is invalid")
    return pointer


def _load_source_pointer(state_root: Path, source_id: str) -> dict[str, Any] | None:
    path = _source_pointer_path(state_root, source_id)
    if not path.exists():
        return None
    pointer = _regular_json(
        path,
        label="sentence-support source pointer",
        max_bytes=MAX_SOURCE_POINTER_BYTES,
    )
    token = _lookup_token(source_id)
    entries = pointer.get("entries")
    if (
        pointer.get("schema_version")
        != "english_sentence_support_source_pointer_v1"
        or pointer.get("lookup_sha256") != token
        or pointer.get("source_id") != source_id
        or not isinstance(entries, list)
        or pointer.get("entry_count") != len(entries)
        or len(entries) > MAX_QUERY_RECORDS
        or pointer.get("pointer_payload_sha256")
        != object_sha256(
            {
                key: value
                for key, value in pointer.items()
                if key != "pointer_payload_sha256"
            }
        )
    ):
        raise ValidationError("sentence-support source pointer is invalid")
    keys = [entry.get("support_key") for entry in entries if isinstance(entry, dict)]
    if len(keys) != len(entries) or len(keys) != len(set(keys)):
        raise ValidationError("sentence-support source pointer entries are invalid")
    return pointer


def _load_sentence_pointer_from_chain(
    generation_roots: list[Path], source_id: str, sentence_id: str
) -> dict[str, Any] | None:
    for root in generation_roots:
        pointer = _load_sentence_pointer(root, source_id, sentence_id)
        if pointer is not None:
            return pointer
    return None


def _load_source_pointer_from_chain(
    generation_roots: list[Path], source_id: str
) -> dict[str, Any] | None:
    for root in generation_roots:
        pointer = _load_source_pointer(root, source_id)
        if pointer is not None:
            return pointer
    return None


def _record_from_entry(state_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    _, record = _read_content_object(
        state_root,
        str(entry.get("record_path", "")),
        str(entry.get("record_sha256", "")),
        label="sentence-support record",
        max_bytes=MAX_RECORD_BYTES,
    )
    if (
        record.get("schema_version") != RECORD_SCHEMA_VERSION
        or record.get("support_key") != entry.get("support_key")
        or record.get("source_identity") != entry.get("source_identity")
    ):
        raise ValidationError("sentence-support record differs from its index entry")
    return record


def _new_record(
    state_root: Path,
    proposal_row: dict[str, Any],
    manifest: dict[str, Any],
    writer_receipt: dict[str, Any],
    previous_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    previous_record: dict[str, Any] | None = None
    previous_binding: dict[str, Any] | None = None
    source = proposal_row["source_identity"]
    published_source = dict(source)
    if proposal_row["record_kind"] == "protected_sidecar":
        published_source["source_sentence"] = None
        published_source["sentence_sha256"] = None
    same_source = False
    if previous_entry is not None:
        previous_record = _record_from_entry(state_root, previous_entry)
        previous_binding = {
            "record_path": previous_entry["record_path"],
            "record_sha256": previous_entry["record_sha256"],
        }
        old_source = previous_record.get("source_identity", {})
        same_source = (
            old_source.get("source_hash") == source["source_hash"]
            and old_source.get("sentence_sha256") == source["sentence_sha256"]
        )
        if (
            same_source
            and str(previous_record.get("history_complete_through") or "")
            > str(manifest.get("study_date") or "")
        ):
            raise IdempotencyConflict(
                "sentence-support refresh would regress history chronology"
            )
    prior_history = previous_record.get("history_episodes", []) if same_source and previous_record else []
    history = _merge_unique(
        prior_history,
        [
            {
                **episode,
                "package_id": proposal_row["package_id"],
                "package_sha256": proposal_row["package_sha256"],
            }
            for episode in proposal_row["history_episodes"]
        ],
        MAX_HISTORY_EPISODES,
    )
    prior_cues = previous_record.get("teaching_cues", {}) if same_source and previous_record else {}
    cues = {
        key: _merge_unique(
            prior_cues.get(key, []),
            [
                {
                    "text": cue["text"],
                    "observed_date": manifest["study_date"],
                    "package_id": proposal_row["package_id"],
                    "package_sha256": proposal_row["package_sha256"],
                    "evidence_state": cue["evidence_state"],
                    "evidence_pointers": sorted(
                        {
                            ref["pointer"]
                            for ref in cue["evidence_refs"]
                        }
                    ),
                    "evidence_ref_sha256": object_sha256(
                        cue["evidence_refs"]
                    ),
                }
                for cue in proposal_row["teaching_cues"][key]
            ],
            MAX_CUE_VALUES,
        )
        for key in sorted(_CUE_FIELDS)
    }
    prior_vocab = previous_record.get("vocabulary_candidates", []) if same_source and previous_record else []
    vocabulary = _merge_unique(
        prior_vocab,
        proposal_row["vocabulary_candidates"],
        MAX_VOCABULARY_CANDIDATES,
    )
    prior_packages = (
        previous_record.get("package_bindings", [])
        if same_source and previous_record
        else []
    )
    package_bindings = _merge_unique(
        prior_packages,
        [
            {
                "package_id": proposal_row["package_id"],
                "package_sha256": proposal_row["package_sha256"],
                "evidence_ref_sha256": object_sha256(
                    proposal_row["evidence_refs"]
                ),
            }
        ],
        MAX_PACKAGE_BINDINGS,
    )
    writer_closure = _writer_package_closure(writer_receipt, proposal_row["package_id"])
    prior_formal = previous_record.get("formal_refs", {}) if same_source and previous_record else {}
    formal_refs = {
        key: sorted(
            set(prior_formal.get(key, [])) | set(writer_closure["formal_refs"][key])
        )
        for key in writer_closure["formal_refs"]
    }
    latest_context = proposal_row["paragraph_context"]
    if (
        latest_context.get("text") is None
        and latest_context.get("locator") is None
        and same_source
        and previous_record is not None
    ):
        latest_context = previous_record.get("paragraph_context", latest_context)
    excluded_ids = [
        package_id
        for package_id in manifest.get("package_ids", [])
        if writer_receipt.get("package_dispositions", {}).get(package_id)
        != "completed"
    ]
    scope = manifest.get("sentence_support_history_scope", {})
    coverage_status = (
        "needs_user_excluded"
        if excluded_ids
        else "complete_local_date"
        if scope.get("mode") == "full_local_date"
        else "package_subset"
    )
    prior_complete_through = (
        previous_record.get("history_complete_through")
        if same_source and previous_record is not None
        else None
    )
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "status": "ready" if proposal_row["record_kind"] == "sentence" else "protected_sidecar",
        "answer_safe": proposal_row["record_kind"] == "sentence",
        "record_kind": proposal_row["record_kind"],
        "support_key": _support_key(source, package_id=proposal_row["package_id"]),
        "source_identity": published_source,
        "paragraph_context": latest_context,
        "history_episodes": history,
        "history_episode_count": len(history),
        "history_complete_through": (
            manifest["study_date"]
            if coverage_status == "complete_local_date"
            else prior_complete_through
        ),
        "latest_refresh_study_date": manifest["study_date"],
        "history_coverage_status": coverage_status,
        "history_excluded_package_ids": excluded_ids,
        "teaching_cues": cues,
        "vocabulary_candidates": vocabulary,
        "package_bindings": package_bindings,
        "formal_refs": formal_refs,
        "latest_episode": {
            "package_id": proposal_row["package_id"],
            "package_sha256": proposal_row["package_sha256"],
            "study_date": manifest["study_date"],
            "evidence_refs": proposal_row["evidence_refs"],
            "writer_receipt_id": writer_receipt["receipt_id"],
            "writer_receipt_sha256": None,
            **writer_closure,
        },
        "previous_record": previous_binding,
        "source_replaced": previous_record is not None and not same_source,
        "formal_write_count": 0,
        "background_processing": "none",
    }


def _receipt_core(receipt: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in receipt.items() if key != "receipt_id"}


def _validate_closed_current_generation(
    state_root: Path,
    index: dict[str, Any],
    index_sha256: str | None,
) -> Path | None:
    if index_sha256 is None:
        return None
    last_refresh = index.get("last_refresh")
    if not isinstance(last_refresh, dict):
        raise ValidationError("sentence-support current generation lacks refresh binding")
    receipt_path = support_receipt_path(
        state_root,
        str(last_refresh.get("study_date") or ""),
        str(last_refresh.get("batch_id") or ""),
    )
    if not receipt_path.is_file():
        raise ValidationError("sentence-support current generation is not receipt-closed")
    receipt = _regular_json(
        receipt_path,
        label="sentence-support current generation receipt",
        max_bytes=512 * 1024,
    )
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("status") != "PASS"
        or receipt.get("batch_id") != last_refresh.get("batch_id")
        or receipt.get("index_snapshot_sha256") != index_sha256
        or receipt.get("receipt_id")
        != "EN-SUPPORT-" + object_sha256(_receipt_core(receipt))[:16].upper()
    ):
        raise ValidationError("sentence-support current generation receipt drift")
    return _resolve_generation_root(state_root, index)


def preflight_sentence_support(
    state_dir: Path,
    *,
    manifest_path: Path,
    proposal_path: Path,
    dry_run_receipt_path: Path,
    expected_completed_ids: set[str],
) -> tuple[Path, dict[str, Any]]:
    state_root = state_dir.resolve()
    manifest_path = manifest_path.resolve(strict=True)
    proposal_path = proposal_path.resolve(strict=True)
    dry_run_receipt_path = dry_run_receipt_path.resolve(strict=True)
    manifest = _regular_json(
        manifest_path,
        label="nightly manifest",
        max_bytes=4 * 1024 * 1024,
    )
    if not sentence_support_required(manifest):
        raise ValidationError("new sentence-support preflight requires manifest v3")
    dry_receipt = _regular_json(
        dry_run_receipt_path,
        label="formal dry-run receipt",
        max_bytes=4 * 1024 * 1024,
    )
    if (
        dry_receipt.get("mode") != "dry_run"
        or dry_receipt.get("status")
        not in {"DRY_RUN_VALID", "DRY_RUN_PARTIAL", "DRY_RUN_NO_ACTION"}
        or dry_receipt.get("manifest_sha256") != file_sha256(manifest_path)
    ):
        raise ValidationError("sentence-support preflight requires matching formal dry-run")
    action_set_id = str(dry_receipt.get("action_set_id") or "")
    proposal, rows = _validate_proposal(
        proposal_path,
        manifest_path,
        manifest,
        None,
        expected_action_set_id=action_set_id,
        expected_completed_ids=expected_completed_ids,
    )
    current_index, current_sha = _load_current_index(state_root)
    if proposal.get("expected_index_sha256") != current_sha:
        raise IdempotencyConflict(
            "sentence-support preflight current index CAS mismatch"
        )
    current_generation = _validate_closed_current_generation(
        state_root, current_index, current_sha
    )
    current_generations = _resolve_generation_chain(state_root, current_index)
    predicted_writer = json.loads(json.dumps(dry_receipt, ensure_ascii=False))
    predicted_writer["receipt_id"] = str(dry_receipt.get("receipt_id") or "")
    predicted_writer["action_results"] = [
        {
            **result,
            "result": (
                "applied" if result.get("result") == "would_apply" else result.get("result")
            ),
        }
        for result in dry_receipt.get("action_results", [])
    ]
    predicted_writer["package_dispositions"] = {
        package_id: (
            "completed"
            if package_id in expected_completed_ids
            else "needs_user"
            if any(
                result.get("result") == "needs_user"
                and any(
                    isinstance(ref, dict) and ref.get("package_id") == package_id
                    for ref in result.get("evidence_refs", [])
                )
                for result in dry_receipt.get("action_results", [])
            )
            else "incomplete"
        )
        for package_id in manifest.get("package_ids", [])
    }
    pending_entries: dict[str, dict[str, Any]] = {}
    projected_records: list[dict[str, Any]] = []
    for row in rows:
        source = row["source_identity"]
        key = _support_key(source, package_id=row["package_id"])
        previous_entry = pending_entries.get(key)
        if previous_entry is None and current_generation is not None and row[
            "record_kind"
        ] == "sentence":
            pointer = _load_sentence_pointer_from_chain(
                current_generations,
                source["source_id"],
                source["sentence_id"],
            )
            _load_source_pointer_from_chain(
                current_generations, source["source_id"]
            )
            previous_entry = pointer.get("entry") if pointer is not None else None
        predicted_record = _new_record(
            state_root,
            row,
            manifest,
            predicted_writer,
            previous_entry,
        )
        predicted_record["latest_episode"]["writer_receipt_sha256"] = "0" * 64
        record_path, record_sha, record_bytes = _write_content_object(
            state_root / "sentence-support" / "preflight-records",
            predicted_record,
        )
        if record_bytes > MAX_RECORD_BYTES:
            raise ValidationError(
                "sentence-support projected record exceeds 12 KiB before formal apply"
            )
        projected_query = {
            "schema_version": QUERY_SCHEMA_VERSION,
            "status": "ready",
            "records": [{"record": _project_query_record(predicted_record)}],
            "record_count": 1,
            "formal_write_count": 0,
        }
        if len(_content_bytes(projected_query)) > DEFAULT_QUERY_BYTES:
            raise ValidationError(
                "sentence-support projected exact query exceeds 16 KiB before formal apply"
            )
        pending_entries[key] = {
            "support_key": key,
            "record_kind": row["record_kind"],
            "status": predicted_record["status"],
            "source_identity": predicted_record["source_identity"],
            "record_path": record_path.relative_to(state_root).as_posix(),
            "record_sha256": record_sha,
            "record_bytes": record_bytes,
            "history_complete_through": predicted_record[
                "history_complete_through"
            ],
            "latest_refresh_study_date": predicted_record[
                "latest_refresh_study_date"
            ],
            "history_coverage_status": predicted_record[
                "history_coverage_status"
            ],
        }
        projected_records.append(
            {
                "package_id": row["package_id"],
                "support_key": key,
                "projected_record_sha256": record_sha,
                "projected_record_bytes": record_bytes,
            }
        )
    proposal_object_path, proposal_sha, _ = _write_content_object(
        state_root / "sentence-support" / "proposals", proposal
    )
    receipt_path = support_preflight_receipt_path(
        state_root, manifest["study_date"], manifest["batch_id"]
    )
    receipt_core = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "status": "PASS",
        "batch_id": manifest["batch_id"],
        "study_date": manifest["study_date"],
        "manifest_path": str(manifest_path),
        "manifest_sha256": file_sha256(manifest_path),
        "dry_run_receipt_path": str(dry_run_receipt_path),
        "dry_run_receipt_sha256": file_sha256(dry_run_receipt_path),
        "actions_sha256": dry_receipt["actions_sha256"],
        "proposal_input_sha256": file_sha256(proposal_path),
        "proposal_path": proposal_object_path.relative_to(state_root).as_posix(),
        "proposal_sha256": proposal_sha,
        "action_set_id": action_set_id,
        "expected_completed_package_ids": sorted(expected_completed_ids),
        "expected_index_sha256": current_sha,
        "record_count": len(rows),
        "projected_records": projected_records,
        "formal_write_count": 0,
        "background_processing": "none",
        "preflight_at": utc_now(),
    }
    receipt = {
        **receipt_core,
        "receipt_id": "EN-SUPPORT-PREFLIGHT-"
        + object_sha256(
            {key: value for key, value in receipt_core.items() if key != "preflight_at"}
        )[:16].upper(),
    }
    if receipt_path.exists():
        existing = load_json(receipt_path)
        comparable_existing = {
            key: value for key, value in existing.items() if key != "preflight_at"
        }
        comparable_new = {
            key: value for key, value in receipt.items() if key != "preflight_at"
        }
        if comparable_existing != comparable_new:
            raise IdempotencyConflict(
                "sentence-support preflight receipt already binds different evidence"
            )
        return receipt_path, existing
    atomic_write_json(receipt_path, receipt)
    reopened = load_json(receipt_path)
    if reopened != receipt:
        raise ValidationError("sentence-support preflight receipt failed durable reopen")
    return receipt_path, reopened


def validate_sentence_support_preflight(
    state_dir: Path,
    *,
    manifest_path: Path,
    actions_path: Path,
    receipt_path: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    state_root = state_dir.resolve()
    manifest_path = manifest_path.resolve(strict=True)
    actions_path = actions_path.resolve(strict=True)
    manifest = load_json(manifest_path)
    actions = load_json(actions_path)
    path = receipt_path or support_preflight_receipt_path(
        state_root, manifest["study_date"], manifest["batch_id"]
    )
    if path.is_symlink() or not path.is_file():
        raise ValidationError("sentence-support preflight receipt is missing")
    path = path.resolve(strict=True)
    expected_path = support_preflight_receipt_path(
        state_root, manifest["study_date"], manifest["batch_id"]
    ).resolve()
    receipt = _regular_json(
        path,
        label="sentence-support preflight receipt",
        max_bytes=512 * 1024,
    )
    identity_core = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_id", "preflight_at"}
    }
    if (
        path != expected_path
        or receipt.get("schema_version") != PREFLIGHT_SCHEMA_VERSION
        or receipt.get("status") != "PASS"
        or receipt.get("batch_id") != manifest.get("batch_id")
        or receipt.get("study_date") != manifest.get("study_date")
        or receipt.get("manifest_path") != str(manifest_path)
        or receipt.get("manifest_sha256") != file_sha256(manifest_path)
        or receipt.get("action_set_id") != actions.get("action_set_id")
        or receipt.get("actions_sha256") != file_sha256(actions_path)
        or receipt.get("receipt_id")
        != "EN-SUPPORT-PREFLIGHT-" + object_sha256(identity_core)[:16].upper()
        or receipt.get("formal_write_count") != 0
        or receipt.get("background_processing") != "none"
    ):
        raise ValidationError("sentence-support preflight receipt binding mismatch")
    dry_path = Path(str(receipt.get("dry_run_receipt_path") or ""))
    if (
        dry_path.is_symlink()
        or not dry_path.is_file()
        or file_sha256(dry_path) != receipt.get("dry_run_receipt_sha256")
    ):
        raise ValidationError("sentence-support preflight dry-run binding mismatch")
    _, current_sha = _load_current_index(state_root)
    if current_sha != receipt.get("expected_index_sha256"):
        raise IdempotencyConflict(
            "sentence-support preflight current generation changed before apply"
        )
    proposal_path, proposal = _read_content_object(
        state_root,
        str(receipt.get("proposal_path") or ""),
        str(receipt.get("proposal_sha256") or ""),
        label="sentence-support preflight proposal",
        max_bytes=MAX_PROPOSAL_BYTES,
    )
    if (
        proposal.get("schema_version") != PROPOSAL_SCHEMA_VERSION
        or proposal.get("batch_id") != manifest.get("batch_id")
        or proposal.get("action_set_id") != actions.get("action_set_id")
    ):
        raise ValidationError("sentence-support preflight proposal binding mismatch")
    projected = receipt.get("projected_records")
    if not isinstance(projected, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("projected_record_bytes"), int)
        or row["projected_record_bytes"] > MAX_RECORD_BYTES
        for row in projected
    ):
        raise ValidationError("sentence-support preflight record-size gate is invalid")
    return receipt, proposal_path


def refresh_sentence_support(
    state_dir: Path,
    repo_root: Path,
    *,
    manifest_path: Path,
    writer_receipt_path: Path,
    proposal_path: Path,
) -> tuple[Path, dict[str, Any]]:
    del repo_root  # The refresh consumes only frozen package and writer evidence.
    state_root = state_dir.resolve()
    manifest_path = manifest_path.resolve(strict=True)
    writer_receipt_path = writer_receipt_path.resolve(strict=True)
    proposal_path = proposal_path.resolve(strict=True)
    writer_receipt, manifest = validate_canonical_writer_closeout(
        state_root,
        writer_receipt_path,
        expected_manifest_path=manifest_path,
    )
    if writer_receipt.get("mode") != "apply" or writer_receipt.get("status") not in {
        "APPLIED", "PARTIAL", "NO_ACTION",
    }:
        raise ValidationError("sentence-support refresh requires a committed writer closeout")
    preflight_path = Path(
        str(writer_receipt.get("support_preflight_receipt_path") or "")
    )
    if (
        preflight_path.is_symlink()
        or not preflight_path.is_file()
        or file_sha256(preflight_path)
        != writer_receipt.get("support_preflight_receipt_sha256")
    ):
        raise ValidationError("sentence-support refresh preflight receipt drift")
    preflight = load_json(preflight_path)
    if (
        preflight.get("schema_version") != PREFLIGHT_SCHEMA_VERSION
        or preflight.get("status") != "PASS"
        or preflight.get("receipt_id")
        != writer_receipt.get("support_preflight_receipt_id")
        or preflight.get("proposal_sha256")
        != writer_receipt.get("support_proposal_sha256")
        or preflight.get("batch_id") != manifest.get("batch_id")
        or preflight.get("manifest_sha256") != file_sha256(manifest_path)
        or preflight.get("action_set_id") != writer_receipt.get("action_set_id")
    ):
        raise ValidationError("sentence-support refresh preflight binding mismatch")
    supplied_proposal_sha = file_sha256(proposal_path)
    if supplied_proposal_sha not in {
        preflight.get("proposal_input_sha256"),
        preflight.get("proposal_sha256"),
    }:
        raise IdempotencyConflict(
            "sentence-support refresh proposal differs from preflight-bound bytes"
        )
    persisted_proposal_path, _ = _read_content_object(
        state_root,
        str(preflight.get("proposal_path") or ""),
        str(preflight.get("proposal_sha256") or ""),
        label="sentence-support preflight-bound proposal",
        max_bytes=MAX_PROPOSAL_BYTES,
    )
    proposal_path = persisted_proposal_path
    receipt_path = support_receipt_path(
        state_root, manifest["study_date"], manifest["batch_id"]
    )
    lock_path = state_root / "sentence-support" / ".refresh.lock"
    with exclusive_lock(lock_path):
        if receipt_path.exists():
            receipt, _ = validate_sentence_support_refresh(
                state_root,
                manifest_path=manifest_path,
                writer_receipt_path=writer_receipt_path,
                receipt_path=receipt_path,
            )
            if (
                receipt.get("proposal_input_sha256")
                != preflight.get("proposal_input_sha256")
                or receipt.get("proposal_sha256")
                != preflight.get("proposal_sha256")
            ):
                raise IdempotencyConflict(
                    "sentence-support batch already binds a different proposal"
                )
            return receipt_path, receipt
        proposal, proposal_rows = _validate_proposal(
            proposal_path, manifest_path, manifest, writer_receipt
        )
        proposal_input_sha = preflight["proposal_input_sha256"]
        proposal_object_path, proposal_sha, _ = _write_content_object(
            state_root / "sentence-support" / "proposals",
            proposal,
        )
        current, current_sha = _load_current_index(state_root)
        last_refresh = current.get("last_refresh")
        if isinstance(last_refresh, dict) and last_refresh.get("batch_id") == manifest["batch_id"]:
            if (
                last_refresh.get("writer_receipt_sha256") != file_sha256(writer_receipt_path)
                or last_refresh.get("proposal_sha256") != proposal_sha
                or last_refresh.get("proposal_input_sha256") != proposal_input_sha
                or last_refresh.get("previous_index_sha256")
                != proposal.get("expected_index_sha256")
            ):
                raise IdempotencyConflict(
                    "sentence-support current index binds different evidence for this batch"
                )
            index_snapshot_path, snapshot = _read_content_object(
                state_root,
                (
                    state_root
                    / "sentence-support"
                    / "indexes"
                    / f"{current_sha}.json"
                ).relative_to(state_root).as_posix(),
                str(current_sha or ""),
                label="sentence-support index snapshot",
                max_bytes=MAX_INDEX_BYTES,
            )
            closures = last_refresh.get("package_closures")
            if not isinstance(closures, list):
                raise ValidationError("sentence-support interrupted refresh lacks closures")
            published_index_sha = file_sha256(index_snapshot_path)
            generation_root = _resolve_generation_root(state_root, snapshot)
            if generation_root is None:
                raise ValidationError("sentence-support refreshed index lacks generation")
            if _generation_tree_hash(generation_root) != snapshot.get(
                "generation_tree_sha256"
            ):
                raise ValidationError("sentence-support generation tree drift")
        else:
            if proposal.get("expected_index_sha256") != current_sha:
                raise IdempotencyConflict(
                    "sentence-support current index CAS does not match proposal"
                )
            closures: list[dict[str, Any]] = []
            entry_count = int(current.get("entry_count", 0))
            source_count = int(current.get("source_count", 0))
            final_entries_by_key: dict[str, dict[str, Any]] = {}
            current_generations = _resolve_generation_chain(state_root, current)
            generation_id = object_sha256(
                {
                    "batch_id": manifest["batch_id"],
                    "writer_receipt_sha256": file_sha256(writer_receipt_path),
                    "proposal_sha256": proposal_sha,
                    "previous_index_sha256": current_sha,
                }
            )[:32]
            generations_root = state_root / "sentence-support" / "generations"
            final_generation = generations_root / generation_id
            stage_generation = generations_root / f".{generation_id}.stage"
            if stage_generation.exists():
                shutil.rmtree(stage_generation)
            compact_generation_chain = (
                len(current_generations) >= MAX_GENERATION_CHAIN
            )
            prior_chain_entries = list(current.get("generation_chain") or [])
            if not prior_chain_entries and current_generations:
                prior_chain_entries = [
                    {
                        "generation_id": current["generation_id"],
                        "generation_path": current["generation_path"],
                        "generation_tree_sha256": current[
                            "generation_tree_sha256"
                        ],
                    }
                ]
            stage_generation.mkdir(parents=True, exist_ok=False)
            if compact_generation_chain:
                for parent_generation in reversed(current_generations):
                    for source_path in sorted(parent_generation.rglob("*.json")):
                        relative = source_path.relative_to(parent_generation)
                        destination = stage_generation / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_path, destination)
            lookup_generations = [stage_generation] + (
                [] if compact_generation_chain else current_generations
            )
            writer_receipt_sha = file_sha256(writer_receipt_path)
            for proposal_row in proposal_rows:
                key = _support_key(proposal_row["source_identity"], package_id=proposal_row["package_id"])
                source = proposal_row["source_identity"]
                previous_pointer = None
                if proposal_row["record_kind"] == "sentence":
                    previous_pointer = _load_sentence_pointer_from_chain(
                        lookup_generations,
                        source["source_id"],
                        source["sentence_id"],
                    )
                previous_entry = (
                    previous_pointer.get("entry")
                    if isinstance(previous_pointer, dict)
                    else None
                )
                record = _new_record(
                    state_root,
                    proposal_row,
                    manifest,
                    writer_receipt,
                    previous_entry,
                )
                record["latest_episode"]["writer_receipt_sha256"] = writer_receipt_sha
                record_path, record_sha, record_bytes = _write_content_object(
                    state_root / "sentence-support" / "objects",
                    record,
                )
                if record_bytes > MAX_RECORD_BYTES:
                    raise ValidationError("sentence-support record exceeds hot-path budget")
                relative_record_path = record_path.relative_to(state_root).as_posix()
                entry = {
                    "support_key": key,
                    "record_kind": proposal_row["record_kind"],
                    "status": record["status"],
                    "source_identity": record["source_identity"],
                    "record_path": relative_record_path,
                    "record_sha256": record_sha,
                    "record_bytes": record_bytes,
                    "history_complete_through": record["history_complete_through"],
                    "latest_refresh_study_date": record["latest_refresh_study_date"],
                    "history_coverage_status": record["history_coverage_status"],
                }
                final_entries_by_key[key] = entry
                resolved_formal_ids = sorted(
                    {
                        formal_id
                        for values in record.get("formal_refs", {}).values()
                        for formal_id in values
                    }
                )
                old_record_sha = (
                    record.get("previous_record", {}).get("record_sha256")
                    if isinstance(record.get("previous_record"), dict)
                    else None
                )
                closures.append(
                    {
                        "package_id": proposal_row["package_id"],
                        "package_sha256": proposal_row["package_sha256"],
                        "support_key": key,
                        "record_kind": proposal_row["record_kind"],
                        "record_path": relative_record_path,
                        "record_sha256": record_sha,
                        "old_record_sha256": old_record_sha,
                        "new_record_sha256": record_sha,
                        "record_bytes": record_bytes,
                        "resolved_formal_ids": resolved_formal_ids,
                        "formal_locator_policy": "resolve_current_by_id",
                        "dependency_locator_sha256": object_sha256(
                            {
                                "policy": "resolve_current_by_id",
                                "formal_ids": resolved_formal_ids,
                            }
                        ),
                    }
                )
                if proposal_row["record_kind"] != "sentence":
                    continue
                if previous_pointer is None:
                    entry_count += 1
                sentence_pointer_core = {
                    "schema_version": "english_sentence_support_sentence_pointer_v1",
                    "lookup_sha256": _lookup_token(
                        source["source_id"], source["sentence_id"]
                    ),
                    "source_id": source["source_id"],
                    "sentence_id": source["sentence_id"],
                    "entry": entry,
                    "updated_batch_id": manifest["batch_id"],
                }
                sentence_pointer = {
                    **sentence_pointer_core,
                    "pointer_payload_sha256": object_sha256(
                        sentence_pointer_core
                    ),
                }
                sentence_pointer_path = _sentence_pointer_path(
                    stage_generation, source["source_id"], source["sentence_id"]
                )
                sentence_payload = _content_bytes(sentence_pointer)
                if len(sentence_payload) > MAX_SENTENCE_POINTER_BYTES:
                    raise ValidationError(
                        "sentence-support exact pointer exceeds its bounded size"
                    )
                atomic_write_bytes(sentence_pointer_path, sentence_payload)
                source_pointer = _load_source_pointer_from_chain(
                    lookup_generations, source["source_id"]
                )
                if source_pointer is None:
                    source_count += 1
                    source_entries: dict[str, dict[str, Any]] = {}
                else:
                    source_entries = {
                        existing["support_key"]: dict(existing)
                        for existing in source_pointer["entries"]
                    }
                source_entries[key] = entry
                if len(source_entries) > MAX_QUERY_RECORDS:
                    raise ValidationError(
                        "sentence-support source lookup exceeds record bound"
                    )
                updated_source_pointer_core = {
                    "schema_version": "english_sentence_support_source_pointer_v1",
                    "lookup_sha256": _lookup_token(source["source_id"]),
                    "source_id": source["source_id"],
                    "entry_count": len(source_entries),
                    "entries": sorted(
                        source_entries.values(),
                        key=lambda row: (
                            str(row["source_identity"].get("sentence_id") or ""),
                            row["support_key"],
                        ),
                    ),
                    "updated_batch_id": manifest["batch_id"],
                }
                updated_source_pointer = {
                    **updated_source_pointer_core,
                    "pointer_payload_sha256": object_sha256(
                        updated_source_pointer_core
                    ),
                }
                source_payload = _content_bytes(updated_source_pointer)
                if len(source_payload) > MAX_SOURCE_POINTER_BYTES:
                    raise ValidationError(
                        "sentence-support source pointer exceeds its bounded size"
                    )
                atomic_write_bytes(
                    _source_pointer_path(stage_generation, source["source_id"]),
                    source_payload,
                )
            for closure in closures:
                published_entry = final_entries_by_key[closure["support_key"]]
                closure["published_record_path"] = published_entry["record_path"]
                closure["published_record_sha256"] = published_entry["record_sha256"]
            generation_tree_sha = _generation_tree_hash(stage_generation)
            if final_generation.exists():
                if _generation_tree_hash(final_generation) != generation_tree_sha:
                    raise IdempotencyConflict(
                        "sentence-support generation id binds different lookup bytes"
                    )
                shutil.rmtree(stage_generation)
            else:
                final_generation.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage_generation, final_generation)
                directory_fd = os.open(final_generation.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            generated_at = utc_now()
            snapshot = {
                "schema_version": INDEX_SCHEMA_VERSION,
                "status": "READY",
                "entry_count": entry_count,
                "source_count": source_count,
                "generation_id": generation_id,
                "generation_path": final_generation.relative_to(state_root).as_posix(),
                "generation_tree_sha256": generation_tree_sha,
                "generation_chain": [
                    {
                        "generation_id": generation_id,
                        "generation_path": final_generation.relative_to(
                            state_root
                        ).as_posix(),
                        "generation_tree_sha256": generation_tree_sha,
                    },
                    *(
                        []
                        if compact_generation_chain
                        else prior_chain_entries
                    ),
                ],
                "last_refresh": {
                    "batch_id": manifest["batch_id"],
                    "study_date": manifest["study_date"],
                    "writer_receipt_sha256": file_sha256(writer_receipt_path),
                    "proposal_sha256": proposal_sha,
                    "proposal_input_sha256": proposal_input_sha,
                    "previous_index_sha256": current_sha,
                    "package_closures": closures,
                },
                "generated_at": generated_at,
            }
            index_snapshot_path, published_index_sha, index_bytes = _write_content_object(
                state_root / "sentence-support" / "indexes",
                snapshot,
            )
            if index_bytes > MAX_INDEX_BYTES:
                raise ValidationError("sentence-support index exceeds its bounded size")
            atomic_write_bytes(current_index_path(state_root), index_snapshot_path.read_bytes())
            if file_sha256(current_index_path(state_root)) != published_index_sha:
                raise ValidationError("sentence-support current index failed atomic reopen")
        excluded_package_ids = [
            package_id
            for package_id in manifest["package_ids"]
            if writer_receipt.get("package_dispositions", {}).get(package_id)
            != "completed"
        ]
        coverage_status = (
            "needs_user_excluded"
            if excluded_package_ids
            else "complete_local_date"
            if manifest.get("sentence_support_history_scope", {}).get("mode")
            == "full_local_date"
            else "package_subset"
        )
        receipt_core = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "PASS",
            "batch_id": manifest["batch_id"],
            "study_date": manifest["study_date"],
            "manifest_path": str(manifest_path),
            "manifest_sha256": file_sha256(manifest_path),
            "writer_receipt_id": writer_receipt["receipt_id"],
            "writer_receipt_path": str(writer_receipt_path),
            "writer_receipt_sha256": file_sha256(writer_receipt_path),
            "proposal_id": proposal["proposal_id"],
            "proposal_input_sha256": proposal_input_sha,
            "proposal_path": proposal_object_path.relative_to(state_root).as_posix(),
            "proposal_sha256": proposal_sha,
            "support_preflight_receipt_id": preflight["receipt_id"],
            "support_preflight_receipt_path": str(preflight_path.resolve(strict=True)),
            "support_preflight_receipt_sha256": file_sha256(preflight_path),
            "previous_index_sha256": proposal.get("expected_index_sha256"),
            "index_snapshot_path": index_snapshot_path.relative_to(state_root).as_posix(),
            "index_snapshot_sha256": published_index_sha,
            "package_closures": closures,
            "completed_package_ids": [row["package_id"] for row in closures],
            "excluded_package_ids": excluded_package_ids,
            "history_complete_through": (
                manifest["study_date"]
                if coverage_status == "complete_local_date"
                else None
            ),
            "latest_refresh_study_date": manifest["study_date"],
            "history_coverage_status": coverage_status,
            "formal_write_count": 0,
            "background_processing": "none",
            "completed_at": utc_now(),
        }
        receipt = {
            **receipt_core,
            "receipt_id": "EN-SUPPORT-" + object_sha256(receipt_core)[:16].upper(),
        }
        atomic_write_bytes(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        reopened, _ = validate_sentence_support_refresh(
            state_root,
            manifest_path=manifest_path,
            writer_receipt_path=writer_receipt_path,
            receipt_path=receipt_path,
        )
        return receipt_path, reopened


def validate_sentence_support_refresh(
    state_dir: Path,
    *,
    manifest_path: Path,
    writer_receipt_path: Path,
    receipt_path: Path | None = None,
    package_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state_root = state_dir.resolve()
    manifest_path = manifest_path.resolve(strict=True)
    writer_receipt_path = writer_receipt_path.resolve(strict=True)
    manifest = _regular_json(manifest_path, label="nightly manifest", max_bytes=4 * 1024 * 1024)
    writer = _regular_json(writer_receipt_path, label="writer receipt", max_bytes=4 * 1024 * 1024)
    path = receipt_path or support_receipt_path(
        state_root, str(manifest.get("study_date", "")), str(manifest.get("batch_id", ""))
    )
    if path.is_symlink() or not path.is_file():
        raise ValidationError("sentence-support refresh receipt is missing")
    path = path.resolve(strict=True)
    expected_path = support_receipt_path(
        state_root, str(manifest.get("study_date", "")), str(manifest.get("batch_id", ""))
    ).resolve()
    receipt = _regular_json(path, label="sentence-support refresh receipt", max_bytes=512 * 1024)
    if (
        path != expected_path
        or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("status") != "PASS"
        or receipt.get("batch_id") != manifest.get("batch_id")
        or receipt.get("study_date") != manifest.get("study_date")
        or receipt.get("manifest_path") != str(manifest_path)
        or receipt.get("manifest_sha256") != file_sha256(manifest_path)
        or receipt.get("writer_receipt_id") != writer.get("receipt_id")
        or receipt.get("writer_receipt_path") != str(writer_receipt_path)
        or receipt.get("writer_receipt_sha256") != file_sha256(writer_receipt_path)
        or receipt.get("support_preflight_receipt_id")
        != writer.get("support_preflight_receipt_id")
        or receipt.get("support_preflight_receipt_path")
        != writer.get("support_preflight_receipt_path")
        or receipt.get("support_preflight_receipt_sha256")
        != writer.get("support_preflight_receipt_sha256")
        or receipt.get("proposal_sha256")
        != writer.get("support_proposal_sha256")
        or receipt.get("receipt_id")
        != "EN-SUPPORT-" + object_sha256(_receipt_core(receipt))[:16].upper()
        or receipt.get("formal_write_count") != 0
        or receipt.get("background_processing") != "none"
    ):
        raise ValidationError("sentence-support refresh receipt binding mismatch")
    bound_preflight_path = Path(
        str(receipt.get("support_preflight_receipt_path") or "")
    )
    if (
        bound_preflight_path.is_symlink()
        or not bound_preflight_path.is_file()
        or file_sha256(bound_preflight_path)
        != receipt.get("support_preflight_receipt_sha256")
    ):
        raise ValidationError("sentence-support refresh preflight receipt drift")
    _, proposal = _read_content_object(
        state_root,
        str(receipt.get("proposal_path", "")),
        str(receipt.get("proposal_sha256", "")),
        label="sentence-support persisted proposal",
        max_bytes=MAX_PROPOSAL_BYTES,
    )
    if (
        proposal.get("schema_version") != PROPOSAL_SCHEMA_VERSION
        or proposal.get("proposal_id") != receipt.get("proposal_id")
        or proposal.get("batch_id") != receipt.get("batch_id")
        or proposal.get("batch_manifest_sha256") != receipt.get("manifest_sha256")
    ):
        raise ValidationError("sentence-support persisted proposal binding mismatch")
    _, snapshot = _read_content_object(
        state_root,
        str(receipt.get("index_snapshot_path", "")),
        str(receipt.get("index_snapshot_sha256", "")),
        label="sentence-support index snapshot",
        max_bytes=MAX_INDEX_BYTES,
    )
    if snapshot.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise ValidationError("sentence-support index snapshot schema mismatch")
    closures = receipt.get("package_closures")
    if not isinstance(closures, list):
        raise ValidationError("sentence-support refresh receipt lacks package closures")
    completed_ids = [
        package_id_value
        for package_id_value in manifest.get("package_ids", [])
        if writer.get("package_dispositions", {}).get(package_id_value) == "completed"
    ]
    if (
        receipt.get("completed_package_ids") != [row.get("package_id") for row in closures]
        or receipt.get("completed_package_ids") != completed_ids
        or set(receipt.get("excluded_package_ids", []))
        != set(manifest.get("package_ids", [])) - set(completed_ids)
    ):
        raise ValidationError("sentence-support completed subset differs from writer disposition")
    generation_root = _resolve_generation_root(state_root, snapshot)
    if generation_root is None or _generation_tree_hash(
        generation_root
    ) != snapshot.get("generation_tree_sha256"):
        raise ValidationError("sentence-support receipt generation tree mismatch")
    selected: dict[str, Any] | None = None
    for closure in closures:
        if not isinstance(closure, dict) or set(closure) != {
            "package_id", "package_sha256", "support_key", "record_kind",
            "record_path", "record_sha256", "old_record_sha256",
            "new_record_sha256", "record_bytes", "resolved_formal_ids",
            "formal_locator_policy", "dependency_locator_sha256",
            "published_record_path", "published_record_sha256",
        }:
            raise ValidationError("sentence-support package closure is invalid")
        _, record = _read_content_object(
            state_root,
            closure["record_path"],
            closure["record_sha256"],
            label="sentence-support closed record",
            max_bytes=MAX_RECORD_BYTES,
        )
        if (
            record.get("schema_version") != RECORD_SCHEMA_VERSION
            or record.get("support_key") != closure["support_key"]
            or record.get("record_kind") != closure["record_kind"]
            or record.get("latest_episode", {}).get("package_id") != closure["package_id"]
            or record.get("latest_episode", {}).get("package_sha256") != closure["package_sha256"]
            or record.get("latest_episode", {}).get("writer_receipt_id") != writer.get("receipt_id")
            or record.get("latest_episode", {}).get("writer_receipt_sha256") != file_sha256(writer_receipt_path)
            or closure.get("new_record_sha256") != closure.get("record_sha256")
            or closure.get("old_record_sha256")
            != (
                record.get("previous_record", {}).get("record_sha256")
                if isinstance(record.get("previous_record"), dict)
                else None
            )
            or closure.get("formal_locator_policy") != "resolve_current_by_id"
            or closure.get("resolved_formal_ids")
            != sorted(
                {
                    formal_id
                    for values in record.get("formal_refs", {}).values()
                    for formal_id in values
                }
            )
            or closure.get("dependency_locator_sha256")
            != object_sha256(
                {
                    "policy": "resolve_current_by_id",
                    "formal_ids": closure.get("resolved_formal_ids"),
                }
            )
        ):
            raise ValidationError("sentence-support closed record binding mismatch")
        _, published_record = _read_content_object(
            state_root,
            closure["published_record_path"],
            closure["published_record_sha256"],
            label="sentence-support published record",
            max_bytes=MAX_RECORD_BYTES,
        )
        if not any(
            binding.get("package_id") == closure["package_id"]
            and binding.get("package_sha256") == closure["package_sha256"]
            for binding in published_record.get("package_bindings", [])
            if isinstance(binding, dict)
        ):
            raise ValidationError(
                "sentence-support published record lost a batch package binding"
            )
        if closure["record_kind"] == "sentence":
            source = published_record.get("source_identity", {})
            if not isinstance(source, dict):
                raise ValidationError("sentence-support published source identity is invalid")
            exact_pointer = _load_sentence_pointer(
                generation_root,
                str(source.get("source_id") or ""),
                str(source.get("sentence_id") or ""),
            )
            source_pointer = _load_source_pointer(
                generation_root, str(source.get("source_id") or "")
            )
            if (
                exact_pointer is None
                or exact_pointer["entry"].get("record_sha256")
                != closure["published_record_sha256"]
                or source_pointer is None
                or not any(
                    entry.get("support_key") == closure["support_key"]
                    and entry.get("record_sha256")
                    == closure["published_record_sha256"]
                    for entry in source_pointer["entries"]
                    if isinstance(entry, dict)
                )
            ):
                raise ValidationError(
                    "sentence-support generation lookup did not publish closed record"
                )
        if closure["record_bytes"] != (state_root / closure["record_path"]).stat().st_size:
            raise ValidationError("sentence-support closed record byte count mismatch")
        if closure["package_id"] == package_id:
            selected = {
                "support_refresh_receipt_id": receipt["receipt_id"],
                "support_refresh_receipt_path": str(path),
                "support_refresh_receipt_sha256": file_sha256(path),
                "index_snapshot_path": receipt["index_snapshot_path"],
                "index_snapshot_sha256": receipt["index_snapshot_sha256"],
                **closure,
            }
    if package_id is not None and selected is None:
        raise ValidationError("package is absent from sentence-support refresh closure")
    return receipt, selected


def sentence_support_closure_map(
    receipt: dict[str, Any], receipt_path: Path
) -> dict[str, dict[str, Any]]:
    resolved_path = receipt_path.resolve(strict=True)
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("status") != "PASS"
    ):
        raise ValidationError("sentence-support closure map requires validated PASS receipt")
    result: dict[str, dict[str, Any]] = {}
    for closure in receipt.get("package_closures", []):
        package_id = closure.get("package_id") if isinstance(closure, dict) else None
        if not isinstance(package_id, str) or package_id in result:
            raise ValidationError("sentence-support closure map package ids are invalid")
        result[package_id] = {
            "support_refresh_receipt_id": receipt["receipt_id"],
            "support_refresh_receipt_path": str(resolved_path),
            "support_refresh_receipt_sha256": file_sha256(resolved_path),
            "index_snapshot_path": receipt["index_snapshot_path"],
            "index_snapshot_sha256": receipt["index_snapshot_sha256"],
            **closure,
        }
    return result


def _query_failure(reason: str, **identity: Any) -> dict[str, Any]:
    return {
        "schema_version": QUERY_SCHEMA_VERSION,
        "status": reason if reason in {"missing", "stale", "hash_mismatch", "oversize", "tampered"} else "unavailable",
        "reason": reason,
        **identity,
        "records": [],
        "record_count": 0,
        "formal_write_count": 0,
    }


def _query_history_slice(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(episodes) <= MAX_QUERY_HISTORY_EPISODES:
        return episodes
    selected: list[dict[str, Any]] = []
    for states in (
        {"independent", "transfer_verified"},
        {"guided", "unresolved"},
    ):
        match = next(
            (episode for episode in reversed(episodes) if episode.get("state") in states),
            None,
        )
        if match is not None and match not in selected:
            selected.append(match)
    for episode in reversed(episodes):
        if len(selected) >= MAX_QUERY_HISTORY_EPISODES:
            break
        if episode not in selected:
            selected.append(episode)
    return sorted(
        selected,
        key=lambda row: (
            str(row.get("observed_date") or ""),
            str(row.get("package_id") or ""),
        ),
    )


def _project_query_record(record: dict[str, Any]) -> dict[str, Any]:
    history = _query_history_slice(list(record.get("history_episodes", [])))
    selected_packages = {
        episode.get("package_id") for episode in history if episode.get("package_id")
    }
    latest_package = record.get("latest_episode", {}).get("package_id")
    if latest_package:
        selected_packages.add(latest_package)
    cues: dict[str, list[dict[str, Any]]] = {}
    for key in sorted(_CUE_FIELDS):
        values = [
            value
            for value in record.get("teaching_cues", {}).get(key, [])
            if isinstance(value, dict)
            and value.get("package_id") in selected_packages
        ]
        cues[key] = values[-MAX_QUERY_CUES_PER_FIELD:]
    latest = record.get("latest_episode", {})
    latest_projection = {
        key: latest.get(key)
        for key in (
            "package_id",
            "package_sha256",
            "study_date",
            "evidence_refs",
            "writer_receipt_id",
            "writer_receipt_sha256",
            "terminal_outcomes",
            "formal_refs",
            "action_closures",
        )
        if key in latest
    }
    return {
        "schema_version": record["schema_version"],
        "status": record["status"],
        "answer_safe": record["answer_safe"],
        "record_kind": record["record_kind"],
        "support_key": record["support_key"],
        "source_identity": record["source_identity"],
        "paragraph_context": record["paragraph_context"],
        "history_episodes": history,
        "history_complete_through": record.get("history_complete_through"),
        "latest_refresh_study_date": record.get("latest_refresh_study_date"),
        "history_coverage_status": record.get("history_coverage_status"),
        "history_excluded_package_ids": record.get(
            "history_excluded_package_ids", []
        ),
        "teaching_cues": cues,
        "vocabulary_candidates": record.get("vocabulary_candidates", [])[
            -MAX_QUERY_VOCABULARY_CANDIDATES:
        ],
        "formal_refs": record.get("formal_refs", {}),
        "latest_episode": latest_projection,
        "formal_write_count": 0,
        "background_processing": "none",
    }


def query_sentence_support(
    state_dir: Path,
    *,
    source_id: str,
    sentence_id: str | None = None,
    source_hash: str | None = None,
    sentence_sha256_value: str | None = None,
    max_records: int = DEFAULT_QUERY_RECORDS,
    max_bytes: int = DEFAULT_QUERY_BYTES,
) -> dict[str, Any]:
    identity = {
        "source_id": source_id,
        "sentence_id": sentence_id,
        "source_hash": source_hash,
        "sentence_sha256": sentence_sha256_value,
    }
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValidationError("query-sentence-support requires source_id")
    if max_records < 1 or max_records > MAX_QUERY_RECORDS:
        raise ValidationError("query-sentence-support max_records is out of bounds")
    if max_bytes < 512 or max_bytes > MAX_QUERY_BYTES:
        raise ValidationError("query-sentence-support max_bytes is out of bounds")
    if source_hash is not None and not _SHA256_RE.fullmatch(source_hash):
        raise ValidationError("query-sentence-support source_hash is invalid")
    if sentence_sha256_value is not None and not _SHA256_RE.fullmatch(sentence_sha256_value):
        raise ValidationError("query-sentence-support sentence_sha256 is invalid")
    state_root = state_dir.resolve()
    source_id = source_id.strip()
    try:
        index, index_sha = _load_current_index(state_root)
        if index_sha is None:
            return _query_failure("missing", **identity)
        last_refresh = index.get("last_refresh")
        if not isinstance(last_refresh, dict):
            return _query_failure("tampered", **identity)
        query_receipt_path = support_receipt_path(
            state_root,
            str(last_refresh.get("study_date") or ""),
            str(last_refresh.get("batch_id") or ""),
        )
        if not query_receipt_path.is_file():
            return _query_failure("generation_not_closed", **identity)
        query_receipt = _regular_json(
            query_receipt_path,
            label="sentence-support query generation receipt",
            max_bytes=512 * 1024,
        )
        if (
            query_receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
            or query_receipt.get("status") != "PASS"
            or query_receipt.get("batch_id") != last_refresh.get("batch_id")
            or query_receipt.get("index_snapshot_sha256") != index_sha
            or query_receipt.get("receipt_id")
            != "EN-SUPPORT-"
            + object_sha256(_receipt_core(query_receipt))[:16].upper()
        ):
            return _query_failure("tampered", **identity)
        generation_root = _resolve_generation_root(state_root, index)
        if generation_root is None:
            return _query_failure("missing", **identity)
        generation_roots = _resolve_generation_chain(state_root, index)
        if sentence_id is not None:
            pointer = None
            pointer_path = None
            for candidate_root in generation_roots:
                pointer = _load_sentence_pointer(
                    candidate_root, source_id, sentence_id
                )
                if pointer is not None:
                    pointer_path = _sentence_pointer_path(
                        candidate_root, source_id, sentence_id
                    )
                    break
            if pointer is None:
                source_pointer = _load_source_pointer_from_chain(
                    generation_roots, source_id
                )
                if source_pointer is not None and any(
                    isinstance(entry, dict)
                    and entry.get("source_identity", {}).get("sentence_id")
                    == sentence_id
                    for entry in source_pointer.get("entries", [])
                    if isinstance(entry, dict)
                    and isinstance(entry.get("source_identity"), dict)
                ):
                    return _query_failure("tampered", **identity)
                return _query_failure("missing", **identity)
            entries = [pointer["entry"]]
            assert pointer_path is not None
        else:
            pointer = None
            pointer_path = None
            for candidate_root in generation_roots:
                pointer = _load_source_pointer(candidate_root, source_id)
                if pointer is not None:
                    pointer_path = _source_pointer_path(candidate_root, source_id)
                    break
            if pointer is None:
                return _query_failure("missing", **identity)
            entries = list(pointer["entries"])
            assert pointer_path is not None
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return _query_failure("tampered", **identity)
    if len(entries) > max_records:
        return _query_failure("oversize", **identity)
    if any(
        not isinstance(row, dict)
        or row.get("record_kind") != "sentence"
        or row.get("status") != "ready"
        or not isinstance(row.get("source_identity"), dict)
        or row.get("source_identity", {}).get("source_id") != source_id
        or (
            sentence_id is not None
            and row.get("source_identity", {}).get("sentence_id") != sentence_id
        )
        for row in entries
    ):
        return _query_failure("tampered", **identity)
    mismatched = [
        row
        for row in entries
        if (
            source_hash is not None
            and row.get("source_identity", {}).get("source_hash") != source_hash
        )
        or (
            sentence_sha256_value is not None
            and row.get("source_identity", {}).get("sentence_sha256")
            != sentence_sha256_value
        )
    ]
    if mismatched:
        reason = "hash_mismatch" if sentence_id is not None else "stale"
        return _query_failure(reason, **identity)
    records: list[dict[str, Any]] = []
    try:
        for entry in entries:
            record = _record_from_entry(state_dir.resolve(), entry)
            if record.get("answer_safe") is not True or record.get("status") != "ready":
                return _query_failure("tampered", **identity)
            projected = _project_query_record(record)
            records.append(
                {
                    "record_sha256": entry["record_sha256"],
                    "record_path": entry["record_path"],
                    "record": projected,
                }
            )
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return _query_failure("tampered", **identity)
    result = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "status": "ready",
        "reason": None,
        **identity,
        "lookup_pointer_sha256": file_sha256(pointer_path),
        "records": records,
        "record_count": len(records),
        "history_complete_through": (
            max(
                value
                for value in (
                    row["record"].get("history_complete_through")
                    for row in records
                )
                if isinstance(value, str) and value
            )
            if records
            and all(
                row["record"].get("history_coverage_status")
                == "complete_local_date"
                and isinstance(
                    row["record"].get("history_complete_through"), str
                )
                and row["record"].get("history_complete_through")
                for row in records
            )
            else None
        ),
        "history_coverage_status": (
            "complete_local_date"
            if records
            and all(
                row["record"].get("history_coverage_status")
                == "complete_local_date"
                for row in records
            )
            else "partial_source_slice"
        ),
        "latest_refresh_study_date": max(
            str(row["record"].get("latest_refresh_study_date") or "")
            for row in records
        ),
        "formal_write_count": 0,
    }
    if len(_content_bytes(result)) > max_bytes:
        return _query_failure("oversize", **identity)
    return result
