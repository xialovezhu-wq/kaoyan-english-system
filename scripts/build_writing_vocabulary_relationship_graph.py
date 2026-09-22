#!/usr/bin/env python3
"""Build the derived writing/syllabus/learner-vocabulary relationship graph.

This graph is a rebuildable reference layer.  It never writes the formal
learner bank, mastered ledger, or sentence-pattern cards.  Relationships are
limited to explicit provenance/taxonomy evidence and exact lexical matches;
the latter are deliberately marked as candidates and can never by themselves
authorize sentence generation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from english_pipeline.review_status import effective_mastered_norms, effective_mastered_ids, effective_review_status, load_review_status_ledger
from english_pipeline.util import normalize_item

WRITING_MANIFEST = ROOT / "raw/writing_reference/manifest.json"
WRITING_CANDIDATES = ROOT / "raw/writing_reference/extracted/english_candidates.jsonl"
TOPIC_CANDIDATES = ROOT / "raw/writing_reference/extracted/topic_candidates.jsonl"
APPROVED_PATTERNS = ROOT / "raw/writing_reference/reviewed/approved_patterns.jsonl"
APPROVED_VOCABULARY = ROOT / "raw/writing_reference/reviewed/approved_vocabulary.jsonl"
SYLLABUS_MANIFEST = ROOT / "raw/reference_sources/syllabus_vocabulary/manifest.json"
SYLLABUS_ENTRIES = ROOT / "raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_entries.jsonl"
SYLLABUS_UNIQUE = ROOT / "raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_unique.jsonl"
MASTER_BANK = ROOT / "bank/master_bank.csv"
MASTERED_ITEMS = ROOT / "bank/mastered_items.csv"
SENTENCE_PATTERNS = ROOT / "bank/sentence_patterns.md"
REVIEW_LEDGER = ROOT / "bank/review_exclusion_ledger.jsonl"

OUTPUT_DIR = ROOT / "raw/reference_relations/writing-vocabulary-foundation"
NODES_PATH = OUTPUT_DIR / "nodes.jsonl"
EDGES_PATH = OUTPUT_DIR / "edges.jsonl"
LOOKUP_PATH = OUTPUT_DIR / "lookup.json"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
WIKI_PATH = ROOT / "wiki/relationships/作文-大纲词-错词关系图谱.md"

GRAPH_INPUTS = (
    WRITING_MANIFEST,
    WRITING_CANDIDATES,
    TOPIC_CANDIDATES,
    APPROVED_PATTERNS,
    APPROVED_VOCABULARY,
    SYLLABUS_MANIFEST,
    SYLLABUS_ENTRIES,
    SYLLABUS_UNIQUE,
    MASTER_BANK,
    MASTERED_ITEMS,
    SENTENCE_PATTERNS,
)
GRAPH_OPTIONAL_INPUTS = (REVIEW_LEDGER,)
FORMAL_ZERO_WRITE_GUARD = (MASTER_BANK, MASTERED_ITEMS, SENTENCE_PATTERNS, REVIEW_LEDGER)

MASTER_FIELDS = [
    "id",
    "date",
    "type",
    "item",
    "source_article",
    "source_sentence",
    "meaning",
    "usage",
    "writing_value",
    "tags",
    "review_note",
    "appear_count",
    "last_seen",
]
MASTERED_FIELDS = [
    "item",
    "matched_id",
    "matched_type",
    "mastered_date",
    "evidence_sentence",
    "evidence_context",
    "proof_note",
]
ALLOWED_MASTER_TYPES = {"单词", "词组", "熟词僻义", "句型", "长难句", "写作表达"}
APPROVED_WRITING_STATUSES = {"approved", "corrected"}

# Function words produce high-volume, low-information token links.  Removing
# them is a lexical-indexing rule, not a semantic judgment.
LEXICAL_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "but",
    "by",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "hers",
    "him",
    "his",
    "i",
    "if",
    "in",
    "is",
    "it",
    "its",
    "me",
    "my",
    "not",
    "of",
    "on",
    "or",
    "our",
    "ours",
    "she",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "they",
    "this",
    "those",
    "to",
    "us",
    "was",
    "we",
    "were",
    "what",
    "when",
    "which",
    "who",
    "whom",
    "whose",
    "will",
    "with",
    "would",
    "you",
    "your",
    "yours",
}

LEXICAL_RELATIONS = {
    "WRITING_TOKEN_MATCHES_SYLLABUS",
    "LEARNER_TOKEN_MATCHES_WRITING",
    "LEARNER_TOKEN_MATCHES_SYLLABUS",
    "LEARNER_EXACT_MATCHES_WRITING",
}


class GraphError(RuntimeError):
    """Raised when a graph invariant is violated."""


def relpath(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def stable_hash(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length].upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("‐", "-")
        .replace("‑", "-")
        .replace("–", "-")
        .replace("—", "-")
    )
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def lexical_tokens(value: Any) -> list[str]:
    text = re.sub(r"\[[A-Z][A-Z0-9_]*\]", " ", str(value or ""))
    tokens = re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", normalize_text(text))
    return sorted(
        {
            token.strip("-'")
            for token in tokens
            if token.strip("-'")
            and len(token.strip("-'")) >= 3
            and token.strip("-'") not in LEXICAL_STOPWORDS
        }
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphError(f"cannot read JSON {relpath(path)}: {exc}") from exc
    if not isinstance(value, dict):
        raise GraphError(f"expected JSON object: {relpath(path)}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GraphError(f"cannot read JSONL {relpath(path)}: {exc}") from exc
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GraphError(f"invalid JSONL {relpath(path)}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise GraphError(f"expected object at {relpath(path)}:{line_no}")
        row["_line_no"] = line_no
        rows.append(row)
    return rows


def read_csv_rows(path: Path, expected_fields: list[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != expected_fields:
                raise GraphError(
                    f"CSV header mismatch in {relpath(path)}: "
                    f"expected {expected_fields}, got {reader.fieldnames}"
                )
            rows: list[dict[str, str]] = []
            for line_no, row in enumerate(reader, start=2):
                if None in row:
                    raise GraphError(f"extra CSV fields at {relpath(path)}:{line_no}")
                clean = {key: str(value or "") for key, value in row.items()}
                clean["_line_no"] = str(line_no)
                rows.append(clean)
            return rows
    except OSError as exc:
        raise GraphError(f"cannot read CSV {relpath(path)}: {exc}") from exc


def parse_sentence_patterns(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^## (SP-\d{3})[｜|](.+)$", text, flags=re.MULTILINE))
    cards: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end]

        def field(name: str) -> str:
            found = re.search(rf"^\*\*{re.escape(name)}\*\*：\s*(.+)$", body, flags=re.MULTILINE)
            return found.group(1).strip() if found else ""

        related_raw = field("相关词汇/搭配")
        related_items = []
        for item in re.split(r"[、；;]", related_raw):
            item = item.strip().strip("`。；;，,")
            if item:
                related_items.append(item)
        cards.append(
            {
                "sp_id": match.group(1),
                "title": match.group(2).strip(),
                "skeleton": field("骨架").strip("`"),
                "generation_template": field("生成模板").strip("`"),
                "scenes": [part.strip() for part in field("场景标签").split("|") if part.strip()],
                "reuse": field("可复用程度"),
                "related_items": related_items,
                "line_no": text[: match.start()].count("\n") + 1,
            }
        )
    return cards


def input_hashes() -> dict[str, str]:
    missing = [relpath(path) for path in GRAPH_INPUTS if not path.is_file()]
    if missing:
        raise GraphError(f"missing graph inputs: {missing}")
    return {relpath(path): sha256_file(path) for path in (*GRAPH_INPUTS, *GRAPH_OPTIONAL_INPUTS) if path.is_file()}


def learner_eligibility(row: dict[str, Any], mastered_ids: set[str], mastered_norms: set[str], review_excluded: set[str]) -> dict[str, Any]:
    """One current eligibility rule shared by graph nodes and their gate tests."""
    mastered = row["id"] in mastered_ids or normalize_item(row["item"]) in mastered_norms
    excluded = mastered or normalize_item(row["item"]) in review_excluded
    valid = (row.get("type") in ALLOWED_MASTER_TYPES and bool(row.get("item"))
             and bool(row.get("source_article")) and bool(row.get("source_sentence")))
    return {"valid": valid, "excluded": excluded,
            "status": "mastered_excluded" if mastered else "review_excluded" if excluded else "active_reference" if valid else "needs_review"}


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def jsonl_text(rows: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def add_node(store: dict[str, dict[str, Any]], node: dict[str, Any]) -> None:
    required = {"node_id", "node_type", "status", "automatic_use_allowed", "source_ref"}
    missing = sorted(required - node.keys())
    if missing:
        raise GraphError(f"node missing fields {missing}: {node.get('node_id')}")
    node_id = str(node["node_id"])
    node["source_ref"] = sorted(set(str(ref) for ref in node["source_ref"] if str(ref)))
    if not node["source_ref"]:
        raise GraphError(f"node has no source_ref: {node_id}")
    if node_id in store:
        raise GraphError(f"duplicate node_id: {node_id}")
    store[node_id] = node


def add_taxonomy_node(
    store: dict[str, dict[str, Any]],
    taxonomy_type: str,
    label: str,
    source_refs: Iterable[str],
    automatic_use_allowed: bool,
) -> str:
    normalized = normalize_text(label)
    node_id = f"REL-{taxonomy_type.upper()}-{stable_hash(normalized, 12)}"
    refs = sorted(set(source_refs))
    if node_id not in store:
        add_node(
            store,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": node_id,
                "node_type": taxonomy_type,
                "label": label,
                "normalized": normalized,
                "status": "derived_exact_label",
                "automatic_use_allowed": automatic_use_allowed,
                "automatic_use_scope": "routing_only" if automatic_use_allowed else "runtime_constraint_required",
                "source_ref": refs,
            },
        )
    else:
        store[node_id]["source_ref"] = sorted(set(store[node_id]["source_ref"]) | set(refs))
    return node_id


def add_edge(
    store: dict[tuple[str, str, str], dict[str, Any]],
    from_id: str,
    to_id: str,
    relation_type: str,
    *,
    status: str,
    automatic_use_allowed: bool,
    source_ref: Iterable[str],
    evidence_kind: str,
    **extra: Any,
) -> None:
    key = (from_id, relation_type, to_id)
    refs = sorted(set(str(ref) for ref in source_ref if str(ref)))
    if not refs:
        raise GraphError(f"edge has no source_ref: {key}")
    if relation_type in LEXICAL_RELATIONS and automatic_use_allowed:
        raise GraphError(f"lexical candidate edge cannot be auto-usable: {key}")
    if key not in store:
        edge = {
            "schema": "writing_vocab_relation_edge_v1",
            "edge_id": f"REL-EDGE-{stable_hash('|'.join(key), 20)}",
            "from_id": from_id,
            "to_id": to_id,
            "relation_type": relation_type,
            "status": status,
            "automatic_use_allowed": automatic_use_allowed,
            "source_ref": refs,
            "evidence_kind": evidence_kind,
        }
        edge.update(extra)
        store[key] = edge
        return
    edge = store[key]
    edge["source_ref"] = sorted(set(edge["source_ref"]) | set(refs))
    edge["automatic_use_allowed"] = bool(edge["automatic_use_allowed"] and automatic_use_allowed)
    if "matched_tokens" in extra:
        edge["matched_tokens"] = sorted(
            set(edge.get("matched_tokens", [])) | set(extra.get("matched_tokens", []))
        )


def build_graph() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    writing_manifest = read_json(WRITING_MANIFEST)
    syllabus_manifest = read_json(SYLLABUS_MANIFEST)
    writing_candidates = read_jsonl(WRITING_CANDIDATES)
    topic_candidates = read_jsonl(TOPIC_CANDIDATES)
    approved_patterns = read_jsonl(APPROVED_PATTERNS)
    approved_vocabulary = read_jsonl(APPROVED_VOCABULARY)
    syllabus_entries = read_jsonl(SYLLABUS_ENTRIES)
    syllabus_unique = read_jsonl(SYLLABUS_UNIQUE)
    master_rows = read_csv_rows(MASTER_BANK, MASTER_FIELDS)
    mastered_rows = read_csv_rows(MASTERED_ITEMS, MASTERED_FIELDS)
    sp_cards = parse_sentence_patterns(SENTENCE_PATTERNS)

    if len(writing_manifest.get("sources", [])) != 6:
        raise GraphError("writing manifest must contain exactly 6 registered sources")
    if len(writing_candidates) != 1513:
        raise GraphError(f"expected 1513 writing candidates, got {len(writing_candidates)}")
    if len(topic_candidates) != 453:
        raise GraphError(f"expected 453 topic candidates, got {len(topic_candidates)}")
    if len(approved_patterns) != 25:
        raise GraphError(f"expected 25 approved patterns, got {len(approved_patterns)}")
    if len(approved_vocabulary) != 46:
        raise GraphError(f"expected 46 approved vocabulary records, got {len(approved_vocabulary)}")
    if len(syllabus_entries) != 5746:
        raise GraphError(f"expected 5746 syllabus occurrences, got {len(syllabus_entries)}")
    if len(syllabus_unique) != 5742:
        raise GraphError(f"expected 5742 unique syllabus headwords, got {len(syllabus_unique)}")
    pattern_ref_count = sum(
        len(row.get("source_candidate_ids") or [row.get("source_candidate_id")])
        for row in approved_patterns
    )
    vocab_ref_count = sum(
        len(row.get("source_candidate_ids") or [row.get("source_candidate_id")])
        for row in approved_vocabulary
    )
    if pattern_ref_count != 33:
        raise GraphError(f"expected 33 approved-pattern source refs, got {pattern_ref_count}")
    if vocab_ref_count != 46:
        raise GraphError(f"expected 46 approved-vocabulary source refs, got {vocab_ref_count}")

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    # Seven registered PDF reference sources: six writing sources and one
    # syllabus booklet.  Source nodes themselves are provenance, not language
    # that may be injected into a generated example.
    writing_source_ids: set[str] = set()
    for source in writing_manifest["sources"]:
        source_id = source["source_id"]
        writing_source_ids.add(source_id)
        add_node(
            nodes,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": source_id,
                "node_type": "reference_source",
                "label": source["title"],
                "normalized": normalize_text(source["title"]),
                "status": "registered",
                "automatic_use_allowed": False,
                "source_role": source.get("source_role", ""),
                "source_sha256": source.get("sha256", ""),
                "page_count": source.get("pages"),
                "source_ref": [f"{relpath(WRITING_MANIFEST)}#sources/{source_id}"],
            },
        )
    syllabus_source_id = syllabus_manifest["source_id"]
    add_node(
        nodes,
        {
            "schema": "writing_vocab_relation_node_v1",
            "node_id": syllabus_source_id,
            "node_type": "reference_source",
            "label": "大纲词汇背诵宝典 英语一",
            "normalized": "大纲词汇背诵宝典 英语一",
            "status": "registered",
            "automatic_use_allowed": False,
            "source_role": syllabus_manifest.get("source_role", ""),
            "source_sha256": syllabus_manifest.get("source_sha256", ""),
            "page_count": syllabus_manifest.get("pdf_pages"),
            "source_ref": [f"{relpath(SYLLABUS_MANIFEST)}#source_id"],
        },
    )

    # Full writing candidate layers.  They are searchable and traceable, but
    # unreviewed/rejected language is never auto-usable.
    candidate_ids: set[str] = set()
    writing_lexical_text: dict[str, str] = {}
    for row in writing_candidates:
        node_id = row["candidate_id"]
        candidate_ids.add(node_id)
        status = "rejected_candidate" if row.get("review_status") == "rejected" else "unreviewed_candidate"
        source_ref = f"{relpath(WRITING_CANDIDATES)}#L{row['_line_no']}"
        add_node(
            nodes,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": node_id,
                "node_type": "writing_english_candidate",
                "label": row.get("verbatim", ""),
                "normalized": normalize_text(row.get("normalized", "")),
                "candidate_kind": row.get("candidate_kind", ""),
                "status": status,
                "automatic_use_allowed": False,
                "language_status": row.get("language_status", "unreviewed"),
                "source_id": row["source_id"],
                "source_page": row.get("source_page"),
                "source_line": row.get("source_line"),
                "source_ref": [source_ref],
            },
        )
        writing_lexical_text[node_id] = row.get("normalized", "")
        add_edge(
            edges,
            node_id,
            row["source_id"],
            "DERIVED_FROM_SOURCE",
            status="direct_source_location",
            automatic_use_allowed=False,
            source_ref=[source_ref],
            evidence_kind="recorded_source_id_page_line",
        )

    for row in topic_candidates:
        node_id = row["topic_candidate_id"]
        candidate_ids.add(node_id)
        status = "rejected_candidate" if row.get("review_status") == "rejected" else "unreviewed_candidate"
        source_ref = f"{relpath(TOPIC_CANDIDATES)}#L{row['_line_no']}"
        add_node(
            nodes,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": node_id,
                "node_type": "writing_topic_candidate",
                "label": row.get("english_candidate") or row.get("verbatim", ""),
                "normalized": normalize_text(row.get("english_candidate", "")),
                "bilingual_verbatim": row.get("verbatim", ""),
                "chinese_candidate": row.get("chinese_candidate", ""),
                "status": status,
                "automatic_use_allowed": False,
                "language_status": row.get("language_status", "unreviewed"),
                "source_id": row["source_id"],
                "source_page": row.get("source_page"),
                "source_line": row.get("source_line"),
                "source_ref": [source_ref],
            },
        )
        writing_lexical_text[node_id] = row.get("english_candidate", "")
        add_edge(
            edges,
            node_id,
            row["source_id"],
            "DERIVED_FROM_SOURCE",
            status="direct_source_location",
            automatic_use_allowed=False,
            source_ref=[source_ref],
            evidence_kind="recorded_source_id_page_line",
        )

    approved_writing_ids: list[str] = []
    for row in approved_patterns:
        node_id = row["approved_id"]
        approved_writing_ids.append(node_id)
        status = row.get("status", "")
        allowed = status in APPROVED_WRITING_STATUSES and row.get("language_status") == "reviewed"
        source_ref = f"{relpath(APPROVED_PATTERNS)}#L{row['_line_no']}"
        slots = sorted(set(re.findall(r"\[([A-Z][A-Z0-9_]*)\]", row.get("template", ""))))
        add_node(
            nodes,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": node_id,
                "node_type": "approved_writing_pattern",
                "label": row.get("template", ""),
                "normalized": normalize_text(row.get("template", "")),
                "verbatim": row.get("verbatim", ""),
                "category": row.get("category", ""),
                "slots": slots,
                "usage_constraints": row.get("usage_constraints", []),
                "status": status,
                "automatic_use_allowed": allowed,
                "automatic_use_scope": "eligible_with_usage_constraints_and_filled_slots",
                "source_id": row["source_id"],
                "source_page": row.get("source_page"),
                "source_candidate_ids": row.get("source_candidate_ids") or [row.get("source_candidate_id")],
                "source_ref": [source_ref],
            },
        )
        writing_lexical_text[node_id] = row.get("template", "")
        category_id = add_taxonomy_node(nodes, "writing_function", row["category"], [source_ref], True)
        add_edge(
            edges,
            node_id,
            category_id,
            "HAS_FUNCTION",
            status="reviewed_exact_metadata",
            automatic_use_allowed=allowed,
            source_ref=[source_ref],
            evidence_kind="approved_pattern_category_field",
            automatic_use_scope="routing_only_not_naturalness",
        )
        for slot in slots:
            slot_id = add_taxonomy_node(nodes, "writing_slot", slot, [source_ref], False)
            add_edge(
                edges,
                node_id,
                slot_id,
                "USES_SLOT",
                status="placeholder_extracted_slot_spec_missing",
                automatic_use_allowed=False,
                source_ref=[source_ref],
                evidence_kind="literal_bracket_placeholder",
                constraint_status="runtime_or_human_constraint_required",
            )
        for candidate_id in row.get("source_candidate_ids") or [row.get("source_candidate_id")]:
            if candidate_id not in candidate_ids:
                raise GraphError(f"approved pattern references missing candidate: {candidate_id}")
            candidate_rejected = nodes[candidate_id]["status"] == "rejected_candidate"
            add_edge(
                edges,
                node_id,
                candidate_id,
                "DERIVED_FROM_CANDIDATE",
                status="corrected_from_rejected_candidate" if candidate_rejected else "manually_reviewed_provenance",
                automatic_use_allowed=False,
                source_ref=[source_ref],
                evidence_kind="approved_record_source_candidate_id",
            )
        add_edge(
            edges,
            node_id,
            row["source_id"],
            "DERIVED_FROM_SOURCE",
            status="manually_reviewed_provenance",
            automatic_use_allowed=False,
            source_ref=[source_ref],
            evidence_kind="approved_record_source_id_page",
        )

    for row in approved_vocabulary:
        node_id = row["approved_id"]
        approved_writing_ids.append(node_id)
        status = row.get("status", "")
        allowed = status in APPROVED_WRITING_STATUSES and row.get("language_status") == "reviewed"
        source_ref = f"{relpath(APPROVED_VOCABULARY)}#L{row['_line_no']}"
        add_node(
            nodes,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": node_id,
                "node_type": "approved_writing_vocabulary",
                "label": row.get("template", ""),
                "normalized": normalize_text(row.get("normalized", "")),
                "verbatim": row.get("verbatim", ""),
                "theme": row.get("theme", ""),
                "form": row.get("form", ""),
                "usage_constraints": row.get("usage_constraints", []),
                "status": status,
                "automatic_use_allowed": allowed,
                "automatic_use_scope": "eligible_with_usage_constraints",
                "source_id": row["source_id"],
                "source_page": row.get("source_page"),
                "source_candidate_ids": row.get("source_candidate_ids") or [row.get("source_candidate_id")],
                "source_ref": [source_ref],
            },
        )
        writing_lexical_text[node_id] = row.get("normalized", "")
        theme_id = add_taxonomy_node(nodes, "theme", row["theme"], [source_ref], True)
        form_id = add_taxonomy_node(nodes, "writing_form", row["form"], [source_ref], True)
        add_edge(
            edges,
            node_id,
            theme_id,
            "HAS_THEME",
            status="reviewed_exact_metadata",
            automatic_use_allowed=allowed,
            source_ref=[source_ref],
            evidence_kind="approved_vocabulary_theme_field",
            automatic_use_scope="routing_only_not_naturalness",
        )
        add_edge(
            edges,
            node_id,
            form_id,
            "HAS_FORM",
            status="reviewed_exact_metadata",
            automatic_use_allowed=allowed,
            source_ref=[source_ref],
            evidence_kind="approved_vocabulary_form_field",
            automatic_use_scope="routing_only_not_naturalness",
        )
        for candidate_id in row.get("source_candidate_ids") or [row.get("source_candidate_id")]:
            if candidate_id not in candidate_ids:
                raise GraphError(f"approved vocabulary references missing candidate: {candidate_id}")
            candidate_rejected = nodes[candidate_id]["status"] == "rejected_candidate"
            add_edge(
                edges,
                node_id,
                candidate_id,
                "DERIVED_FROM_CANDIDATE",
                status="corrected_from_rejected_candidate" if candidate_rejected else "manually_reviewed_provenance",
                automatic_use_allowed=False,
                source_ref=[source_ref],
                evidence_kind="approved_record_source_candidate_id",
            )
        add_edge(
            edges,
            node_id,
            row["source_id"],
            "DERIVED_FROM_SOURCE",
            status="manually_reviewed_provenance",
            automatic_use_allowed=False,
            source_ref=[source_ref],
            evidence_kind="approved_record_source_id_page",
        )

    # One node per normalized syllabus headword.  Only manually verified
    # statuses can be auto-used; OCR consensus is still a locator, not proof.
    entry_by_id = {row["entry_id"]: row for row in syllabus_entries}
    syllabus_token_index: dict[str, str] = {}
    for row in syllabus_unique:
        node_id = row["canonical_entry_id"]
        status = row.get("verification_status", "")
        allowed = status.startswith("verified_")
        canonical = entry_by_id.get(node_id)
        if canonical is None:
            raise GraphError(f"syllabus unique row references missing entry: {node_id}")
        source_ref = f"{relpath(SYLLABUS_UNIQUE)}#L{row['_line_no']}"
        add_node(
            nodes,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": node_id,
                "node_type": "syllabus_headword",
                "label": row["normalized"],
                "normalized": normalize_text(row["normalized"]),
                "status": status,
                "automatic_use_allowed": allowed,
                "automatic_use_scope": "eligible_as_syllabus_foundation" if allowed else "locator_only",
                "canonical_entry_id": node_id,
                "entry_ids": row.get("entry_ids", []),
                "occurrence_count": row.get("occurrence_count", 0),
                "sections": row.get("sections", []),
                "pdf_page": canonical.get("pdf_page"),
                "column": canonical.get("column"),
                "source_id": syllabus_source_id,
                "source_ref": [source_ref, f"{relpath(SYLLABUS_ENTRIES)}#{node_id}"],
            },
        )
        normalized = normalize_text(row["normalized"])
        if re.fullmatch(r"[a-z]+(?:[-'][a-z]+)*", normalized):
            if normalized in syllabus_token_index:
                raise GraphError(f"duplicate unique syllabus token: {normalized}")
            syllabus_token_index[normalized] = node_id
        add_edge(
            edges,
            node_id,
            syllabus_source_id,
            "DERIVED_FROM_SOURCE",
            status="headword_occurrence_provenance",
            automatic_use_allowed=False,
            source_ref=[source_ref],
            evidence_kind="canonical_entry_and_occurrence_list",
        )

    # Formal learner bank.  A row tagged 错句来源/错题来源 records a candidate
    # evidence kind only; it does not assert that the user marked the item as
    # unknown.  Mastered evidence excludes matching learner nodes from use.
    review_records = load_review_status_ledger(REVIEW_LEDGER)
    mastered_ids = effective_mastered_ids(mastered_rows, review_records)
    mastered_normalized = effective_mastered_norms(mastered_rows, review_records)
    review_status = effective_review_status(review_records)
    review_excluded = {key for key, state in review_status.items() if state["status"] == "mastered_sentence_nonreport"}
    learner_ids: list[str] = []
    learner_normalized_index: dict[str, list[str]] = defaultdict(list)
    for row in master_rows:
        row_id = row["id"]
        node_id = f"LEARNER-{row_id}"
        normalized = normalize_text(row["item"])
        learner_ids.append(node_id)
        learner_normalized_index[normalized].append(node_id)
        eligibility = learner_eligibility(row, mastered_ids, mastered_normalized, review_excluded)
        valid, excluded, status = eligibility["valid"], eligibility["excluded"], eligibility["status"]
        source_ref = f"{relpath(MASTER_BANK)}#L{row['_line_no']}"
        evidence_kinds: list[str] = []
        tags = row.get("tags", "")
        if "错句来源" in tags:
            evidence_kinds.append("wrong_sentence_tag_candidate")
        if "错题来源" in tags:
            evidence_kinds.append("wrong_question_tag_candidate")
        add_node(
            nodes,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": node_id,
                "node_type": "learner_bank_item",
                "label": row.get("item", ""),
                "normalized": normalized,
                "item_type": row.get("type", ""),
                "meaning": row.get("meaning", ""),
                "usage": row.get("usage", ""),
                "writing_value": row.get("writing_value", ""),
                "tags": tags,
                "source_article": row.get("source_article", ""),
                "source_sentence": row.get("source_sentence", ""),
                "learner_evidence_kinds": evidence_kinds,
                "status": status,
                "automatic_use_allowed": valid and not excluded,
                "automatic_use_scope": "eligible_as_learner_foundation" if valid and not excluded else "excluded_or_review_required",
                "source_ref": [source_ref],
            },
        )
        for evidence_kind in evidence_kinds:
            evidence_node_id = f"REL-EVIDENCE-{stable_hash(node_id + '|' + evidence_kind, 16)}"
            add_node(
                nodes,
                {
                    "schema": "writing_vocab_relation_node_v1",
                    "node_id": evidence_node_id,
                    "node_type": "learner_evidence",
                    "label": evidence_kind,
                    "normalized": evidence_kind,
                    "learner_evidence_kind": evidence_kind,
                    "source_article": row.get("source_article", ""),
                    "source_sentence": row.get("source_sentence", ""),
                    "status": "recorded_tag_candidate",
                    "automatic_use_allowed": False,
                    "source_ref": [source_ref],
                },
            )
            add_edge(
                edges,
                node_id,
                evidence_node_id,
                "HAS_LEARNER_EVIDENCE",
                status="recorded_tag_candidate",
                automatic_use_allowed=False,
                source_ref=[source_ref],
                evidence_kind=evidence_kind,
                assertion_boundary="tag_is_recorded_but_user_unknown_is_not_asserted",
            )

    for row in mastered_rows:
        source_ref = f"{relpath(MASTERED_ITEMS)}#L{row['_line_no']}"
        evidence_node_id = f"REL-MASTERED-{stable_hash(source_ref + '|' + row.get('item', ''), 16)}"
        add_node(
            nodes,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": evidence_node_id,
                "node_type": "mastered_evidence",
                "label": row.get("item", ""),
                "normalized": normalize_text(row.get("item", "")),
                "matched_id": row.get("matched_id", ""),
                "mastered_date": row.get("mastered_date", ""),
                "status": "mastered_exclusion_evidence",
                "automatic_use_allowed": False,
                "source_ref": [source_ref],
            },
        )
        matched_nodes: list[str] = []
        if row.get("matched_id") and f"LEARNER-{row['matched_id']}" in nodes:
            matched_nodes.append(f"LEARNER-{row['matched_id']}")
        matched_nodes.extend(learner_normalized_index.get(normalize_text(row.get("item", "")), []))
        for learner_id in sorted(set(matched_nodes)):
            add_edge(
                edges,
                learner_id,
                evidence_node_id,
                "HAS_MASTERED_EVIDENCE",
                status="formal_mastered_exclusion" if nodes[learner_id]["status"] == "mastered_excluded" else "historical_mastery_proof",
                automatic_use_allowed=False,
                source_ref=[source_ref],
                evidence_kind="mastered_items_exact_id_or_normalized_match",
            )

    # SP cards are an optional structure layer.  Only cards explicitly tagged
    # 写作模板 and not marked low-reuse are eligible.  Generic lexical overlap
    # is not enough: links to learner items require an exact authored entry in
    # the card's 相关词汇/搭配 field.
    sp_ids: list[str] = []
    for card in sp_cards:
        node_id = card["sp_id"]
        sp_ids.append(node_id)
        allowed = "写作模板" in card["scenes"] and not card["reuse"].startswith("低")
        source_ref = f"{relpath(SENTENCE_PATTERNS)}#{node_id}"
        add_node(
            nodes,
            {
                "schema": "writing_vocab_relation_node_v1",
                "node_id": node_id,
                "node_type": "sentence_pattern_card",
                "label": card["title"],
                "normalized": normalize_text(card["title"]),
                "skeleton": card["skeleton"],
                "generation_template": card["generation_template"],
                "scenes": card["scenes"],
                "reuse": card["reuse"],
                "related_items": card["related_items"],
                "status": "formal_card",
                "automatic_use_allowed": allowed,
                "automatic_use_scope": "optional_structure_foundation" if allowed else "retrieval_only",
                "source_ref": [source_ref],
            },
        )
        for scene in card["scenes"]:
            scene_id = add_taxonomy_node(nodes, "sp_scene", scene, [source_ref], True)
            add_edge(
                edges,
                node_id,
                scene_id,
                "HAS_SCENE",
                status="formal_card_exact_metadata",
                automatic_use_allowed=True,
                source_ref=[source_ref],
                evidence_kind="sentence_pattern_scene_field",
                automatic_use_scope="routing_only_not_naturalness",
            )
        for related_item in card["related_items"]:
            normalized = normalize_text(related_item)
            for learner_id in learner_normalized_index.get(normalized, []):
                add_edge(
                    edges,
                    node_id,
                    learner_id,
                    "SP_EXPLICIT_RELATED_ITEM",
                    status="explicit_exact_related_item",
                    automatic_use_allowed=allowed and nodes[learner_id]["automatic_use_allowed"],
                    source_ref=[source_ref, *nodes[learner_id]["source_ref"]],
                    evidence_kind="exact_normalized_match_to_authored_related_item_field",
                    related_item=related_item,
                )

    # Exact token graph.  These links only prove spelling identity, never
    # meaning, theme, collocational naturalness, or permission to combine.
    for writing_id, lexical_text in writing_lexical_text.items():
        for token in lexical_tokens(lexical_text):
            syllabus_id = syllabus_token_index.get(token)
            if syllabus_id:
                add_edge(
                    edges,
                    writing_id,
                    syllabus_id,
                    "WRITING_TOKEN_MATCHES_SYLLABUS",
                    status="lexical_candidate",
                    automatic_use_allowed=False,
                    source_ref=[*nodes[writing_id]["source_ref"], *nodes[syllabus_id]["source_ref"]],
                    evidence_kind="exact_normalized_token_identity",
                    matched_tokens=[token],
                    assertion_boundary="not_semantic_not_collocational_not_naturalness",
                )

    reviewed_token_index: dict[str, list[str]] = defaultdict(list)
    reviewed_normalized_index: dict[str, list[str]] = defaultdict(list)
    for writing_id in approved_writing_ids:
        reviewed_normalized_index[nodes[writing_id]["normalized"]].append(writing_id)
        for token in lexical_tokens(writing_lexical_text[writing_id]):
            reviewed_token_index[token].append(writing_id)

    for learner_id in learner_ids:
        learner_node = nodes[learner_id]
        normalized = learner_node["normalized"]
        for writing_id in reviewed_normalized_index.get(normalized, []):
            add_edge(
                edges,
                learner_id,
                writing_id,
                "LEARNER_EXACT_MATCHES_WRITING",
                status="lexical_candidate",
                automatic_use_allowed=False,
                source_ref=[*learner_node["source_ref"], *nodes[writing_id]["source_ref"]],
                evidence_kind="exact_normalized_string_identity",
                matched_tokens=lexical_tokens(normalized),
                assertion_boundary="not_semantic_not_collocational_not_naturalness",
            )
        for token in lexical_tokens(learner_node["label"]):
            syllabus_id = syllabus_token_index.get(token)
            if syllabus_id:
                add_edge(
                    edges,
                    learner_id,
                    syllabus_id,
                    "LEARNER_TOKEN_MATCHES_SYLLABUS",
                    status="lexical_candidate",
                    automatic_use_allowed=False,
                    source_ref=[*learner_node["source_ref"], *nodes[syllabus_id]["source_ref"]],
                    evidence_kind="exact_normalized_token_identity",
                    matched_tokens=[token],
                    assertion_boundary="not_semantic_not_user_unknown_not_naturalness",
                )
            for writing_id in reviewed_token_index.get(token, []):
                add_edge(
                    edges,
                    learner_id,
                    writing_id,
                    "LEARNER_TOKEN_MATCHES_WRITING",
                    status="lexical_candidate",
                    automatic_use_allowed=False,
                    source_ref=[*learner_node["source_ref"], *nodes[writing_id]["source_ref"]],
                    evidence_kind="exact_normalized_token_identity",
                    matched_tokens=[token],
                    assertion_boundary="not_semantic_not_collocational_not_naturalness",
                )

    sorted_nodes = sorted(nodes.values(), key=lambda row: (row["node_type"], row["node_id"]))
    sorted_edges = sorted(
        edges.values(),
        key=lambda row: (row["relation_type"], row["from_id"], row["to_id"], row["edge_id"]),
    )

    lookup_maps: dict[str, dict[str, set[str]]] = {
        "normalized": defaultdict(set),
        "token": defaultdict(set),
        "theme": defaultdict(set),
        "source": defaultdict(set),
        "node_type": defaultdict(set),
        "status": defaultdict(set),
    }
    for node in sorted_nodes:
        node_id = node["node_id"]
        if node.get("normalized"):
            lookup_maps["normalized"][normalize_text(node["normalized"])].add(node_id)
        for token in lexical_tokens(node.get("label") or node.get("normalized", "")):
            lookup_maps["token"][token].add(node_id)
        if node.get("theme"):
            lookup_maps["theme"][normalize_text(node["theme"])].add(node_id)
        if node["node_type"] == "theme":
            lookup_maps["theme"][normalize_text(node["label"])].add(node_id)
        for source_ref in node["source_ref"]:
            lookup_maps["source"][source_ref].add(node_id)
        if node.get("source_id"):
            lookup_maps["source"][node["source_id"]].add(node_id)
        lookup_maps["node_type"][node["node_type"]].add(node_id)
        lookup_maps["status"][node["status"]].add(node_id)
    lookup = {
        "schema": "writing_vocab_relation_lookup_v1",
        "normalization": "NFKC + punctuation folding + whitespace collapse + casefold",
        "lexical_boundary": "exact tokens only; stopwords excluded; no stemming or semantic inference",
        "indexes": {
            index_name: {key: sorted(values) for key, values in sorted(mapping.items())}
            for index_name, mapping in lookup_maps.items()
        },
    }

    node_counts = Counter(row["node_type"] for row in sorted_nodes)
    node_status_counts = Counter(row["status"] for row in sorted_nodes)
    edge_counts = Counter(row["relation_type"] for row in sorted_edges)
    edge_status_counts = Counter(row["status"] for row in sorted_edges)
    learner_evidence_counts = Counter(
        row.get("learner_evidence_kind", "unclassified")
        for row in sorted_nodes
        if row["node_type"] == "learner_evidence"
    )
    summary = {
        "schema": "writing_vocab_relation_summary_v1",
        "node_count": len(sorted_nodes),
        "edge_count": len(sorted_edges),
        "node_counts_by_type": dict(sorted(node_counts.items())),
        "node_counts_by_status": dict(sorted(node_status_counts.items())),
        "edge_counts_by_type": dict(sorted(edge_counts.items())),
        "edge_counts_by_status": dict(sorted(edge_status_counts.items())),
        "automatic_use_allowed_nodes": sum(bool(row["automatic_use_allowed"]) for row in sorted_nodes),
        "automatic_use_allowed_edges": sum(bool(row["automatic_use_allowed"]) for row in sorted_edges),
        "reference_source_count": node_counts["reference_source"],
        "writing_source_count": len(writing_source_ids),
        "approved_pattern_count": node_counts["approved_writing_pattern"],
        "approved_vocabulary_count": node_counts["approved_writing_vocabulary"],
        "writing_candidate_count": node_counts["writing_english_candidate"],
        "topic_candidate_count": node_counts["writing_topic_candidate"],
        "syllabus_unique_count": node_counts["syllabus_headword"],
        "syllabus_verified_count": sum(
            1
            for row in sorted_nodes
            if row["node_type"] == "syllabus_headword" and row["status"].startswith("verified_")
        ),
        "learner_bank_count": node_counts["learner_bank_item"],
        "learner_error_tag_candidate_count": node_counts["learner_evidence"],
        "learner_evidence_counts_by_kind": dict(sorted(learner_evidence_counts.items())),
        "mastered_evidence_count": node_counts["mastered_evidence"],
        "sentence_pattern_count": node_counts["sentence_pattern_card"],
        "approved_pattern_source_ref_count": pattern_ref_count,
        "approved_vocabulary_source_ref_count": vocab_ref_count,
        "safety": {
            "unreviewed_candidates_automatic_use_allowed": False,
            "syllabus_auto_use_gate": "verification_status starts with verified_",
            "lexical_edges_automatic_use_allowed": False,
            "lexical_edges_assert_semantics": False,
            "theme_edges_assert_natural_cooccurrence": False,
            "wrong_sentence_tag_asserts_user_unknown": False,
            "slot_edges_require_runtime_or_human_constraints": True,
        },
    }
    validate_graph(sorted_nodes, sorted_edges, lookup, summary)
    return sorted_nodes, sorted_edges, lookup, summary


def validate_graph(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    lookup: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    failures: list[str] = []
    node_ids = [row.get("node_id") for row in nodes]
    node_id_set = set(node_ids)
    if len(node_ids) != len(node_id_set):
        failures.append("duplicate node_id")
    edge_ids = [row.get("edge_id") for row in edges]
    if len(edge_ids) != len(set(edge_ids)):
        failures.append("duplicate edge_id")
    signatures = [(row.get("from_id"), row.get("relation_type"), row.get("to_id")) for row in edges]
    if len(signatures) != len(set(signatures)):
        failures.append("duplicate edge signature")
    for node in nodes:
        if not isinstance(node.get("automatic_use_allowed"), bool):
            failures.append(f"node boolean gate missing: {node.get('node_id')}")
        if not node.get("status") or not node.get("source_ref"):
            failures.append(f"node status/source_ref missing: {node.get('node_id')}")
        if node.get("node_type") in {"writing_english_candidate", "writing_topic_candidate"} and node.get(
            "automatic_use_allowed"
        ):
            failures.append(f"unreviewed/rejected writing candidate auto-usable: {node.get('node_id')}")
        if node.get("node_type") == "syllabus_headword":
            expected = str(node.get("status", "")).startswith("verified_")
            if node.get("automatic_use_allowed") != expected:
                failures.append(f"syllabus verification gate mismatch: {node.get('node_id')}")
        if node.get("node_type") in {"approved_writing_pattern", "approved_writing_vocabulary"}:
            expected = node.get("status") in APPROVED_WRITING_STATUSES
            if node.get("automatic_use_allowed") != expected:
                failures.append(f"approved writing gate mismatch: {node.get('node_id')}")
        if node.get("node_type") == "learner_evidence" and node.get("automatic_use_allowed"):
            failures.append(f"learner evidence auto-usable: {node.get('node_id')}")
    for edge in edges:
        if edge.get("from_id") not in node_id_set or edge.get("to_id") not in node_id_set:
            failures.append(f"edge endpoint missing: {edge.get('edge_id')}")
        if not isinstance(edge.get("automatic_use_allowed"), bool):
            failures.append(f"edge boolean gate missing: {edge.get('edge_id')}")
        if not edge.get("status") or not edge.get("source_ref"):
            failures.append(f"edge status/source_ref missing: {edge.get('edge_id')}")
        if edge.get("relation_type") in LEXICAL_RELATIONS:
            if edge.get("automatic_use_allowed"):
                failures.append(f"lexical edge auto-usable: {edge.get('edge_id')}")
            if edge.get("status") != "lexical_candidate":
                failures.append(f"lexical edge has non-candidate status: {edge.get('edge_id')}")
        if edge.get("relation_type") == "USES_SLOT" and edge.get("automatic_use_allowed"):
            failures.append(f"slot edge auto-usable without slot spec: {edge.get('edge_id')}")
    if summary.get("node_count") != len(nodes):
        failures.append("summary node_count mismatch")
    if summary.get("edge_count") != len(edges):
        failures.append("summary edge_count mismatch")
    if summary.get("reference_source_count") != 7:
        failures.append("reference source count is not 7")
    if summary.get("approved_pattern_count") != 25:
        failures.append("approved pattern count is not 25")
    if summary.get("approved_vocabulary_count") != 46:
        failures.append("approved vocabulary count is not 46")
    if summary.get("writing_candidate_count") != 1513:
        failures.append("writing candidate count is not 1513")
    if summary.get("topic_candidate_count") != 453:
        failures.append("topic candidate count is not 453")
    if summary.get("syllabus_unique_count") != 5742:
        failures.append("syllabus unique count is not 5742")
    if summary.get("approved_pattern_source_ref_count") != 33:
        failures.append("approved pattern source ref count is not 33")
    if summary.get("approved_vocabulary_source_ref_count") != 46:
        failures.append("approved vocabulary source ref count is not 46")

    indexes = lookup.get("indexes", {})
    for required_index in ("normalized", "token", "theme", "source", "node_type", "status"):
        mapping = indexes.get(required_index)
        if not isinstance(mapping, dict):
            failures.append(f"lookup index missing: {required_index}")
            continue
        for key, ids in mapping.items():
            if ids != sorted(set(ids)):
                failures.append(f"lookup IDs not sorted/unique: {required_index}/{key}")
            unknown = set(ids) - node_id_set
            if unknown:
                failures.append(f"lookup references unknown nodes: {required_index}/{key}/{sorted(unknown)[:3]}")
    if failures:
        raise GraphError("graph validation failed:\n- " + "\n- ".join(failures[:100]))


def markdown_count_table(mapping: dict[str, int]) -> str:
    lines = ["| 类型 | 数量 |", "|---|---:|"]
    lines.extend(f"| `{key}` | {value} |" for key, value in mapping.items())
    return "\n".join(lines)


def build_wiki(summary: dict[str, Any], formal_hashes: dict[str, str]) -> str:
    node_table = markdown_count_table(summary["node_counts_by_type"])
    edge_table = markdown_count_table(summary["edge_counts_by_type"])
    formal_lines = "\n".join(f"- `{path}`：`{digest}`" for path, digest in sorted(formal_hashes.items()))
    return f"""# 作文—大纲词—用户词关系图谱

> 派生层，按脚本重建。它用于检索和生成前的证据路由，不替代 `master_bank.csv`、`mastered_items.csv` 或 `sentence_patterns.md`。

## 当前规模

- 节点：{summary['node_count']}
- 边：{summary['edge_count']}
- 注册 PDF 来源：{summary['reference_source_count']}（作文 6 + 大纲词汇 1）
- 人工审核作文句型：{summary['approved_pattern_count']}（来源候选引用 {summary['approved_pattern_source_ref_count']}）
- 人工审核作文词组 / 主题词：{summary['approved_vocabulary_count']}（来源候选引用 {summary['approved_vocabulary_source_ref_count']}）
- 未审核英文候选：{summary['writing_candidate_count']}
- 未审核主题词候选：{summary['topic_candidate_count']}
- 大纲唯一词头：{summary['syllabus_unique_count']}，其中人工视觉核验可调用：{summary['syllabus_verified_count']}
- 用户长期库词项：{summary['learner_bank_count']}
- 用户证据标签候选：错句来源 {summary['learner_evidence_counts_by_kind'].get('wrong_sentence_tag_candidate', 0)}，错题来源 {summary['learner_evidence_counts_by_kind'].get('wrong_question_tag_candidate', 0)}（均不等于“用户明确不会”）
- 句式卡：{summary['sentence_pattern_count']}

### 节点类型

{node_table}

### 关系类型

{edge_table}

## 安全边界

1. `approved|corrected` 作文条目才可进入自动候选；完整候选层只检索，不自动用。
2. 大纲词只有 `verification_status` 以 `verified_` 开头才可进入自动候选；OCR 共识仍只是定位线索。
3. 所有 `*_TOKEN_MATCHES_*` / `LEARNER_EXACT_MATCHES_WRITING` 边均为 `lexical_candidate` 且 `automatic_use_allowed=false`。它们只证明字面词元相同，不证明语义、自然搭配或主题共现。
4. 相同主题只用于路由，不代表两项可以自然同句。图谱不生成主题笛卡尔积。
5. `错句来源` 只记为 `wrong_sentence_tag_candidate`，不能据此断言“用户不会这个词”。
6. 方括号槽位虽可从模板中提取，但当前缺少独立 `slot_specs`；`USES_SLOT` 一律要求运行时或人工约束，不可自动填充。
7. `SP-*` 只有在“相关词汇/搭配”字段与用户词项完全一致时建立强关系；普通词面重叠不建 SP 强边。

## 查询入口

- `lookup.json` 的 `normalized`：规范化完整字符串查询。
- `lookup.json` 的 `token`：精确英文词元查询；不做词形还原和语义扩展。
- `lookup.json` 的 `theme`：按作文白名单中人工审核主题查询。
- `lookup.json` 的 `source`：按来源 ID 或精确 `source_ref` 查询。
- `nodes.jsonl` / `edges.jsonl`：查看每项状态、自动调用门禁和证据来源。

## 重建与校验

```bash
python3 scripts/build_writing_vocabulary_relationship_graph.py
python3 scripts/build_writing_vocabulary_relationship_graph.py --verify-only
```

## 正式输入零写入守卫

本次 manifest 保存下列正式输入哈希；`--verify-only` 会回查，构建过程也会比较写前 / 写后哈希：

{formal_lines}

机器文件：

- [节点](../../raw/reference_relations/writing-vocabulary-foundation/nodes.jsonl)
- [关系边](../../raw/reference_relations/writing-vocabulary-foundation/edges.jsonl)
- [检索索引](../../raw/reference_relations/writing-vocabulary-foundation/lookup.json)
- [统计摘要](../../raw/reference_relations/writing-vocabulary-foundation/summary.json)
- [构建清单](../../raw/reference_relations/writing-vocabulary-foundation/manifest.json)
"""


def build() -> dict[str, Any]:
    before = input_hashes()
    formal_before = {relpath(path): before[relpath(path)] for path in FORMAL_ZERO_WRITE_GUARD if relpath(path) in before}
    nodes, edges, lookup, summary = build_graph()

    atomic_write_text(NODES_PATH, jsonl_text(nodes))
    atomic_write_text(EDGES_PATH, jsonl_text(edges))
    atomic_write_text(LOOKUP_PATH, json.dumps(lookup, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    atomic_write_text(SUMMARY_PATH, json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    atomic_write_text(WIKI_PATH, build_wiki(summary, formal_before))

    after = input_hashes()
    formal_after = {relpath(path): after[relpath(path)] for path in FORMAL_ZERO_WRITE_GUARD if relpath(path) in after}
    if formal_before != formal_after:
        raise GraphError("formal input changed during graph build")
    if before != after:
        changed = [path for path in set(before) | set(after) if before.get(path) != after.get(path)]
        raise GraphError(f"graph input changed during build: {changed}")

    source_dates = [
        str(read_json(WRITING_MANIFEST).get("generated_date", "")),
        str(read_json(SYLLABUS_MANIFEST).get("generated_date", "")),
    ]
    manifest = {
        "schema": "writing_vocab_relation_manifest_v1",
        "generated_date": max(source_dates),
        "builder": relpath(Path(__file__).resolve()),
        "policy": "derived_evidence_graph_no_semantic_inference",
        "inputs": [
            {
                "path": path,
                "sha256": digest,
                "role": "formal_zero_write_guard" if path in formal_before else "reference_input",
            }
            for path, digest in sorted(before.items())
        ],
        "formal_zero_write_guard": formal_before,
        "optional_inputs": {relpath(path): {"exists": relpath(path) in before, "sha256": before.get(relpath(path))} for path in GRAPH_OPTIONAL_INPUTS},
        "outputs": {
            relpath(path): {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in (NODES_PATH, EDGES_PATH, LOOKUP_PATH, SUMMARY_PATH, WIKI_PATH)
        },
        "counts": summary,
        "verification": {
            "status": "PASS",
            "endpoint_integrity": "PASS",
            "id_uniqueness": "PASS",
            "automatic_use_gates": "PASS",
            "lexical_edge_nonautomatic_gate": "PASS",
            "formal_input_hash_unchanged": "PASS",
        },
    }
    atomic_write_text(MANIFEST_PATH, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return manifest


def verify_only() -> dict[str, Any]:
    required = (NODES_PATH, EDGES_PATH, LOOKUP_PATH, SUMMARY_PATH, MANIFEST_PATH, WIKI_PATH)
    missing = [relpath(path) for path in required if not path.is_file()]
    if missing:
        raise GraphError(f"missing graph outputs: {missing}")
    nodes = read_jsonl(NODES_PATH)
    edges = read_jsonl(EDGES_PATH)
    for row in nodes:
        row.pop("_line_no", None)
    for row in edges:
        row.pop("_line_no", None)
    lookup = read_json(LOOKUP_PATH)
    summary = read_json(SUMMARY_PATH)
    manifest = read_json(MANIFEST_PATH)
    validate_graph(nodes, edges, lookup, summary)

    current_inputs = input_hashes()
    recorded_inputs = {row["path"]: row["sha256"] for row in manifest.get("inputs", [])}
    if current_inputs != recorded_inputs:
        changed = sorted(set(current_inputs) | set(recorded_inputs))
        changed = [path for path in changed if current_inputs.get(path) != recorded_inputs.get(path)]
        raise GraphError(f"input hashes differ from manifest: {changed}")
    formal_current = {relpath(path): current_inputs[relpath(path)] for path in FORMAL_ZERO_WRITE_GUARD if relpath(path) in current_inputs}
    for path_text, expected in manifest.get("optional_inputs", {}).items():
        if (ROOT / path_text).is_file() != bool(expected.get("exists")):
            raise GraphError(f"optional formal input changed: {path_text}")
    if formal_current != manifest.get("formal_zero_write_guard"):
        raise GraphError("formal zero-write guard hash mismatch")
    for path_text, record in manifest.get("outputs", {}).items():
        path = ROOT / path_text
        if not path.is_file():
            raise GraphError(f"manifest output missing: {path_text}")
        if sha256_file(path) != record.get("sha256"):
            raise GraphError(f"output hash mismatch: {path_text}")
        if path.stat().st_size != record.get("bytes"):
            raise GraphError(f"output size mismatch: {path_text}")
    if manifest.get("counts") != summary:
        raise GraphError("manifest counts differ from summary")
    return {
        "status": "PASS",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "syllabus_verified_count": summary["syllabus_verified_count"],
        "formal_zero_write_guard": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="validate outputs and input hashes without writing")
    args = parser.parse_args()
    try:
        result = verify_only() if args.verify_only else build()
    except GraphError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.verify_only:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        counts = result["counts"]
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "node_count": counts["node_count"],
                    "edge_count": counts["edge_count"],
                    "syllabus_verified_count": counts["syllabus_verified_count"],
                    "manifest": relpath(MANIFEST_PATH),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
