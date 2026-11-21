"""Validate reviewed display annotations against the exact source sentence."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .answer_safety import contains_answer_signal
from .errors import ValidationError
from .util import object_sha256

SCHEMA = "english_sentence_visual_v1"
OFFSET_UNIT = "unicode_codepoint"
WORD = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
STAGES = {"first_attempt", "hints", "correction", "later_independent_attempt"}
ROLES = {"主语", "谓语", "宾语", "表语", "从句", "修饰语", "状语", "并列项", "引语", "补语", "核心结构"}
RELATIONS = {"主谓", "动宾", "系表", "修饰", "条件", "时间", "方式", "并列", "目的", "指代", "补足", "对照", "说明"}
TEXT_FIELDS = {"translation": 400, "first_break": 100, "reading_cue": 80, "translation_focus": 40}


def tokens(text: str) -> list[dict[str, Any]]:
    return [
        {"id": i, "text": m.group(), "start": m.start(), "end": m.end()}
        for i, m in enumerate(WORD.finditer(text))
    ]


def normalize_word(word: str) -> str:
    return unicodedata.normalize("NFC", word.replace("’", "'")).casefold()


def _shape(row: Any, required: set[str], optional: set[str]) -> None:
    if not isinstance(row, dict) or not required <= row.keys() <= required | optional:
        raise ValidationError("reviewed sentence annotation fields are invalid")


def _span(text: str, row: dict[str, Any]) -> None:
    start, end = row["start"], row["end"]
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or text[start:end] != row["text"] or not row["text"].strip()):
        raise ValidationError("reviewed sentence offsets do not select the quoted source")


def _bounded_text(value: Any, size: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= size


def validate_review(text: str, value: dict[str, Any], ref_count: int) -> dict[str, Any]:
    """The reviewer supplies exact ranges and references. No parse or sense is inferred."""
    _shape(value, set(), {"spans", "arcs", "focus_span_ids", "evidence_ref_indexes",
                         "occurrences", "evidence_roles", *TEXT_FIELDS})
    for field in TEXT_FIELDS.keys() & value.keys():
        if not _bounded_text(value[field], TEXT_FIELDS[field]):
            raise ValidationError("reviewed sentence display text is invalid")
    if "translation_focus" in value and value.get("translation", "").count(value["translation_focus"]) != 1:
        raise ValidationError("Chinese emphasis must identify one exact span of the reviewed translation")
    spans, arcs = value.get("spans", []), value.get("arcs", [])
    focus, occurrences = value.get("focus_span_ids", []), value.get("occurrences", [])
    for items, maximum in ((spans, 8), (arcs, 2), (focus, 2), (occurrences, 12)):
        if not isinstance(items, list) or len(items) > maximum:
            raise ValidationError("reviewed sentence annotation exceeds its display bound")
    span_ids = set()
    for row in spans:
        _shape(row, {"id", "start", "end", "text", "role"}, set())
        _span(text, row)
        if (not isinstance(row["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", row["id"])
                or row["id"] in span_ids or row["role"] not in ROLES):
            raise ValidationError("reviewed sentence span identity or role is invalid")
        span_ids.add(row["id"])
    for row in arcs:
        _shape(row, {"head", "dependent", "relation"}, set())
        if (row["head"] not in span_ids or row["dependent"] not in span_ids
                or row["head"] == row["dependent"] or row["relation"] not in RELATIONS):
            raise ValidationError("reviewed sentence arc is invalid")
    if not set(focus) <= span_ids or bool(spans) != bool(arcs):
        raise ValidationError("reviewed sentence focus or relationship is missing")
    for row in occurrences:
        _shape(row, {"start", "end", "text"}, {"reviewed_sense_id", "sense_label"})
        _span(text, row)
        if ("reviewed_sense_id" in row) != ("sense_label" in row):
            raise ValidationError("reviewed occurrence needs both a sense ID and a label")
        if "reviewed_sense_id" in row and (
                not _bounded_text(row["reviewed_sense_id"], 96)
                or not _bounded_text(row["sense_label"], 24)):
            raise ValidationError("reviewed occurrence sense is invalid")
        if (not WORD.match(row["text"]) or not WORD.search(row["text"])
                or not any(t["start"] == row["start"] for t in tokens(text))
                or not any(t["end"] == row["end"] for t in tokens(text))):
            raise ValidationError("reviewed occurrence must select whole source words")
    refs = value.get("evidence_ref_indexes", [])
    roles = value.get("evidence_roles", {})
    _shape(roles, set(), STAGES)
    all_lists = [refs, *roles.values()]
    if any(not isinstance(items, list) or len(items) > 8
           or any(type(i) is not int or not 0 <= i < ref_count for i in items)
           or len(set(items)) != len(items) for items in all_lists):
        raise ValidationError("reviewed annotations require bounded direct evidence references")
    if (spans or occurrences or TEXT_FIELDS.keys() & value.keys()) and not refs:
        raise ValidationError("reviewed structure, sense, and translation need direct evidence")
    if any(len(roles.get(key, [])) > 1 for key in STAGES - {"hints"}):
        raise ValidationError("one exact first attempt, correction and follow-up are allowed")
    role_indexes = [i for group in roles.values() for i in group]
    if len(set(role_indexes)) != len(role_indexes) or contains_answer_signal(value):
        raise ValidationError("reviewed annotation has conflicting roles or protected content")
    return value


def bind_review(text: str, value: dict[str, Any], refs: list[dict[str, Any]], *,
                reviewed_cue: bool = False) -> dict[str, Any]:
    data = validate_review(text, value, len(refs))
    if not reviewed_cue and any(refs[i].get("kind") not in ("assistant_message", "source_text")
                                for i in data.get("evidence_ref_indexes", [])):
        raise ValidationError("reviewed display needs an exact explanation or source citation")
    roles = {}
    for stage, indices in data.get("evidence_roles", {}).items():
        allowed = {
            "first_attempt": {"user_message", "independent_correct_use"},
            "hints": {"assistant_message"},
            "correction": {"user_message", "assistant_message"},
            "later_independent_attempt": {"independent_correct_use"},
        }[stage]
        if any(refs[i].get("kind") not in allowed for i in indices):
            raise ValidationError("reviewed evidence role contradicts the cited message role")
        roles[stage] = [refs[i] for i in indices]
    return {key: item for key, item in data.items() if key not in {"evidence_ref_indexes", "evidence_roles"}} | {
        "evidence_refs": [refs[i] for i in data.get("evidence_ref_indexes", [])],
        "evidence_roles": roles,
    }


def review_from_legacy(record: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    if isinstance(annotation, dict) and "evidence_sha256" in annotation:
        _shape(annotation, {"evidence_sha256", "review"}, set())
        latest = record["latest_episode"]
        raw = annotation["review"]
        if not isinstance(raw, dict) or raw.get("first_break") or raw.get("evidence_roles"):
            raise ValidationError("a source reading cannot add personal observations")
        review = bind_review(record["source_identity"]["source_sentence"], raw, latest["evidence_refs"])
        cited = review["evidence_refs"]
        if not cited or any((ref.get("package_id"), ref.get("package_sha256")) !=
                            (latest["package_id"], latest["package_sha256"]) for ref in cited):
            raise ValidationError("reading review is outside the frozen evidence package")
        if object_sha256(cited) != annotation["evidence_sha256"]:
            raise ValidationError("reading review citations changed")
        return {**review, "reviewed_evidence_sha256": annotation["evidence_sha256"]}
    _shape(annotation, {"cue_sha256", "review"}, {"cue_field"})
    field = annotation.get("cue_field", "structure_cues")
    if field not in {"structure_cues", "meaning_constraints", "effective_explanations", "first_breaks"}:
        raise ValidationError("backfill cue field is invalid")
    cue = next((c for c in record["teaching_cues"][field]
                if object_sha256(c) == annotation["cue_sha256"]), None)
    if cue is None:
        raise ValidationError("backfill evidence no longer matches the reviewed support cue")
    latest = record["latest_episode"]
    if (cue["package_id"], cue["package_sha256"]) != (latest["package_id"], latest["package_sha256"]):
        raise ValidationError("backfill cue needs an exact citation to the current package")
    refs = latest["evidence_refs"]
    review = bind_review(record["source_identity"]["source_sentence"], annotation["review"], refs, reviewed_cue=True)
    if field == "first_breaks" and (review.get("arcs") or review.get("occurrences")):
        raise ValidationError("a historical mistake does not establish syntax or sense")
    if not set(ref["pointer"] for ref in review["evidence_refs"]) <= set(cue["evidence_pointers"]):
        raise ValidationError("backfill display citation is outside the reviewed cue evidence")
    return {**review, "reviewed_cue_sha256": annotation["cue_sha256"]}
