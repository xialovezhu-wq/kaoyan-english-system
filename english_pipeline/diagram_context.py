"""Prepare and reuse one answer-free context for an English sentence diagram."""
from __future__ import annotations

from pathlib import Path

from .errors import ValidationError
from .exam_lexicon import LEXICON_DB, contains_form, vocabulary_references
from .memory_query import _state_identity, query_memory_context
from .reading_preparation import DEFAULT_OUTPUT, query as read_unit
from .util import atomic_write_bytes, canonical_bytes, load_json, object_sha256, text_sha256


SCHEMA = "english_diagram_context_v1"
MAX_CONTEXT_BYTES = 1_000_000


def _file_identity(path):
    try:
        stat = path.stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    except FileNotFoundError:
        return None


def _memory_identity(state):
    return [list(stat) if stat is not None else None for stat in _state_identity(state)]


def _source_scope(repo, source_id, unit_id, *, source_hash=None, record_sha256=None):
    selected = read_unit(repo / DEFAULT_OUTPUT, repo_root=repo, source_id=source_id, unit_id=unit_id,
                         source_hash=source_hash, record_sha256=record_sha256)
    unit = selected["unit"]
    if unit.get("answer_exposure") != "answer_free":
        raise ValidationError("diagram context requires a practice-safe unit")
    scope = {"source_id": selected["source_id"], "source_hash": selected["source_hash"],
             "reference_id": selected["reference_id"], "unit_id": unit["unit_id"], "kind": unit["kind"],
             "question_id": unit.get("question_id"), "text_sha256": unit["text_sha256"]}
    if record_sha256 is not None:
        scope["record_sha256"] = selected["record_sha256"]
    return scope, unit["text"]


def prepare_diagram_context(repo, state, path, *, source_id=None, unit_id=None, text=None,
                            keys=(), ids=(), query=None, focus_key=None, vocabulary=(),
                            max_bytes=6144, lexicon_db=None, include_evidence=False,
                            source_hash=None, record_sha256=None):
    if path is not None and (not path.is_absolute() or not path.name.endswith(".context.json") or path.is_symlink()):
        raise ValidationError("diagram context output must be an absolute .context.json file")
    scope = {"source_id": None}
    if source_id or unit_id:
        if not (source_id and unit_id):
            raise ValidationError("a diagram source requires both source ID and unit ID")
        scope, original = _source_scope(repo, source_id, unit_id, source_hash=source_hash, record_sha256=record_sha256)
        if text is not None and text != original:
            raise ValidationError("diagram text differs from the indexed source unit")
        text = original
    elif not isinstance(text, str) or not text.strip():
        raise ValidationError("an unindexed diagram requires its current sentence text")
    if len(text.encode()) > 12_288:
        raise ValidationError("diagram source sentence exceeds the text limit")
    scope["text_sha256"] = text_sha256(text)
    if len(vocabulary) > 12 or any(not contains_form(text, item["source_form"]) for item in vocabulary):
        raise ValidationError("prepared vocabulary must identify original forms in the current sentence")

    lexicon_db = lexicon_db or repo / LEXICON_DB
    ordered = list(dict.fromkeys([*([focus_key] if focus_key else []), *keys]))
    if not scope.get("question_id") and len(ordered) < 8:
        for target in vocabulary:
            item = target["item"].strip().lower()
            point = ("phrase:" if " " in item else "word:") + item
            if point not in ordered:
                ordered.append(point)
            if len(ordered) == 8:
                break
    memory_state = _memory_identity(state) if ordered or ids or query else None
    lexicon_state = _file_identity(lexicon_db) if vocabulary else None
    history = None
    if memory_state is not None:
        container = (scope["reference_id"] or scope["source_id"]) if scope.get("kind") == "article" else None
        history = query_memory_context(repo, state, keys=ordered, ids=ids, query=query,
            source_id=scope["source_id"], unit_id=scope.get("unit_id"), question_id=scope.get("question_id"),
            max_bytes=max_bytes, diagram=True, article_container_id=container, include_evidence=include_evidence)
    references = vocabulary_references(vocabulary, db=lexicon_db)
    if ((memory_state is not None and _memory_identity(state) != memory_state)
            or (vocabulary and _file_identity(lexicon_db) != lexicon_state)):
        raise ValidationError("English diagram context changed while being prepared")
    payload = {"schema_version": SCHEMA, "scope": scope, "sentence": text, "memory": history,
               "vocabulary": [{"target": list(target), "reference": ref} for target, ref in references.items()],
               "memory_state": memory_state, "lexicon_state": lexicon_state}
    encoded = canonical_bytes(payload if path is None else {**payload, "context_sha256": object_sha256(payload)})
    if len(encoded) > MAX_CONTEXT_BYTES:
        raise ValidationError("diagram context exceeds the byte limit")
    if path is None:
        return payload
    atomic_write_bytes(path, encoded)
    return {key: payload[key] for key in ("schema_version", "scope", "sentence", "memory", "vocabulary")} | {
        "status": "prepared", "context_path": str(path), "focus_key": focus_key,
        "model_requests": 0, "formal_write_count": 0}


def load_diagram_context(repo, state, path, *, lexicon_db=None):
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_size > MAX_CONTEXT_BYTES):
        raise ValidationError("diagram context path or size is invalid")
    payload = load_json(path)
    digest = payload.pop("context_sha256", None)
    if payload.get("schema_version") != SCHEMA or object_sha256(payload) != digest:
        raise ValidationError("diagram context integrity could not be verified")
    if payload["memory"] is not None and payload["memory_state"] != _memory_identity(state):
        raise ValidationError("personal memory changed since this diagram context was prepared")
    lexicon_db = lexicon_db or repo / LEXICON_DB
    if payload["vocabulary"] and payload["lexicon_state"] != _file_identity(lexicon_db):
        raise ValidationError("exam vocabulary changed since this diagram context was prepared")
    scope = payload["scope"]
    if scope["source_id"]:
        current, _ = _source_scope(repo, scope["source_id"], scope["unit_id"],
                                   record_sha256=scope.get("record_sha256"))
        if current != scope:
            raise ValidationError("reading source changed since this diagram context was prepared")
    if text_sha256(payload["sentence"]) != scope["text_sha256"]:
        raise ValidationError("diagram context sentence differs from its source identity")
    return payload
