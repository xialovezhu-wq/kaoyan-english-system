"""Deterministic, answer-free source identities for the existing article library.

The library is not migrated. Existing handoff hashes remain authoritative; corpus
records and the one plain source article have explicit, content-stable fallbacks.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import SourceHashMismatch, ValidationError
from .util import canonical_bytes, load_json, normalize_sentence, object_sha256


CATALOG_SCHEMA = "english_source_catalog_v1"
_REFERENCE_RE = re.compile(r"EXAM-READING-(20\d{2})-T([1-4])")


def _inside(repo: Path, locator: str) -> Path:
    candidate = Path(locator)
    path = (candidate if candidate.is_absolute() else repo / candidate).resolve(strict=True)
    try:
        path.relative_to(repo)
    except ValueError as exc:
        raise ValidationError("English source locator escapes repository") from exc
    if not path.is_file():
        raise ValidationError("English source must be a file")
    return path


def _metadata(text: str, name: str) -> str | None:
    values = re.findall(
        rf"^\s*(?:-\s*)?{re.escape(name)}\s*[：:]\s*`?([^`\r\n]+?)`?\s*$",
        text, re.MULTILINE,
    )
    values = list(dict.fromkeys(value.strip() for value in values))
    if len(values) > 1:
        raise ValidationError(f"conflicting {name} source bindings")
    return values[0] if values else None


def _plain_source(text: str) -> dict[str, Any]:
    """The existing source-only article has no study or answer sections."""
    lines = text.splitlines()
    body = []
    started = False
    for line in lines:
        if not started and (not line.strip() or line.startswith("# ") or re.match(r"^(date|source):", line)):
            continue
        started = True
        if line.startswith("## ") and line.strip() != "## Questions":
            raise ValidationError("plain article contains an unclassified section; source binding required")
        body.append(line.rstrip())
    payload = "\n".join(body).strip()
    if not payload:
        raise ValidationError("plain article source is empty")
    return {"schema_version": "english_plain_practice_source_v1", "text": payload}


def resolve_article_source(repo_root: Path, source_article: str) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    article = _inside(repo, source_article)
    relative = article.relative_to(repo).as_posix()
    if not relative.startswith("articles/") or article.suffix != ".md":
        raise ValidationError("source_article must name an English article Markdown file")
    text = article.read_text(encoding="utf-8")
    handoff = _metadata(text, "pipeline_handoff")
    if handoff:
        # Keep the exact prior normalization and hash contract, including fixtures.
        from .cli import _validate_capture_source_object
        source_id = _metadata(text, "source_id")
        source_hash = str(_metadata(text, "source_hash") or "").removeprefix("sha256:")
        wrapper = {"article": {"source_id": source_id, "source_hash": source_hash,
                               "source_article": relative}}
        _validate_capture_source_object(repo, wrapper, allow_library_fallback=False)
        handoff_path = _inside(repo, handoff)
        from .cli import _resolve_canonical_payload
        document = load_json(handoff_path)
        canonical = _resolve_canonical_payload(repo, handoff_path, document["canonical_payload"]["path"])
        return {"schema_version": "english_source_binding_v1", "source_id": source_id,
                "source_hash": source_hash, "source_article": relative,
                "canonical_path": canonical.relative_to(repo).as_posix(), "binding_kind": "handoff",
                "reference_id": _metadata(text, "reference_id"), "subject": "english"}
    reference = _REFERENCE_RE.search(text[:6000])
    if reference is None:
        reference = re.search(r"(20\d{2})-english-i-text-([1-4])", article.name)
    if reference is None:
        # Early article filenames were descriptive. Their explicit year/Text lines
        # identify the existing source; no answer or model inference is involved.
        reference = re.search(r"(20\d{2})\s*(?:年|/|English).*?Text\s*([1-4])", text[:3000], re.IGNORECASE)
    if reference:
        year, text_no = reference.group(1), reference.group(2)
        canonical = repo / "raw/articles/exam-reading-corpus" / year / f"text-{text_no}.json"
        record = load_json(canonical)
        if record.get("schema") != "exam_reading_practice_safe_v1" or record.get("visibility") != "practice_safe":
            raise ValidationError("corpus source is not practice-safe")
        if record.get("article_path") != relative:
            raise ValidationError("corpus source does not bind this article")
        questions = record.get("questions", [])
        if not isinstance(questions, list) or any(
            not isinstance(q, dict) or set(q) != {"number", "prompt", "options"}
            or not isinstance(q["options"], dict) or set(q["options"]) != {"A", "B", "C", "D"}
            for q in questions
        ):
            raise ValidationError("corpus question fields are not answer-free")
        payload = {"schema_version": "english_corpus_practice_source_v1",
                   "reference_id": record["reference_id"], "source_id": record["source_id"],
                   "passage_paragraphs": record["passage_paragraphs"], "questions": questions}
        return {"schema_version": "english_source_binding_v1", "source_id": record["source_id"],
                "source_hash": object_sha256(payload), "source_article": relative,
                "canonical_path": canonical.relative_to(repo).as_posix(), "binding_kind": "corpus",
                "reference_id": record["reference_id"], "subject": "english"}
    if relative != "articles/2026-04-24-article.md":
        raise ValidationError("article has no verified corpus or handoff source")
    payload = _plain_source(text)
    return {"schema_version": "english_source_binding_v1", "source_id": "RAW-ARTICLE-20260424-001",
            "source_hash": object_sha256(payload), "source_article": relative,
            "canonical_path": relative, "binding_kind": "plain_source",
            "reference_id": None, "subject": "english"}


def build_source_catalog(repo_root: Path) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    entries = [resolve_article_source(repo, path.relative_to(repo).as_posix())
               for path in sorted((repo / "articles").glob("*.md"))]
    by_id: dict[str, list[str]] = {}
    for entry in entries:
        by_id.setdefault(entry["source_id"], []).append(entry["source_article"])
    if any(len(paths) != 1 for paths in by_id.values()):
        raise ValidationError("article source identities are ambiguous")
    return {"schema_version": CATALOG_SCHEMA, "subject": "english", "entries": entries,
            "entry_count": len(entries), "catalog_sha256": object_sha256(entries),
            "formal_write_count": 0}


def validate_binding(repo_root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    current = resolve_article_source(repo_root, str(binding.get("source_article") or ""))
    if current["source_id"] != binding.get("source_id") or current["source_hash"] != binding.get("source_hash"):
        raise SourceHashMismatch("English source changed after the recorded learning episode")
    return current
