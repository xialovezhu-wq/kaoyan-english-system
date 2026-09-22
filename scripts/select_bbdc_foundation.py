#!/usr/bin/env python3
"""Select a traceable foundation packet for a future BBDC example.

The selector is deliberately read-only.  It does not generate the final
sentence and it never writes to the formal vocabulary bank, review files, or
the sentence-pattern card library.  Its job is to collect *eligible* building
blocks and leave semantic fit and naturalness to the language-model review
step.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from english_pipeline.review_status import (
    effective_mastered_norms,
    effective_mastered_ids,
    effective_review_status,
    load_review_status_ledger,
)


MASTER_HEADER = [
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

MASTERED_HEADER = [
    "item",
    "matched_id",
    "matched_type",
    "mastered_date",
    "evidence_sentence",
    "evidence_context",
    "proof_note",
]

ALLOWED_USER_EVIDENCE = {"", "unknown", "mistranslated", "missed", "candidate"}
ALLOWED_MODES = {"argumentative", "picture", "chart", "letter", "notice"}
ALLOWED_POS = {"", "noun", "verb", "adjective", "adverb", "phrase", "clause"}

# This is a syntactic shortlist, not a claim that every listed pattern is
# semantically suitable.  The final model must still pass usage_constraints.
PATTERN_RUNTIME = {
    "WRITING-APPROVED-PATTERN-001": {"modes": {"picture"}, "roles": {"clause"}},
    "WRITING-APPROVED-PATTERN-002": {"modes": {"picture"}, "roles": {"clause"}},
    "WRITING-APPROVED-PATTERN-003": {"modes": {"chart"}, "roles": {"clause"}},
    "WRITING-APPROVED-PATTERN-004": {"modes": {"chart"}, "roles": {"noun", "phrase"}},
    "WRITING-APPROVED-PATTERN-005": {"modes": {"chart"}, "roles": {"noun", "phrase"}},
    "WRITING-APPROVED-PATTERN-006": {"modes": {"chart"}, "roles": {"noun", "phrase", "adverb"}},
    "WRITING-APPROVED-PATTERN-007": {"modes": {"chart"}, "roles": {"noun", "phrase"}},
    "WRITING-APPROVED-PATTERN-008": {"modes": {"argumentative", "chart"}, "roles": {"noun", "phrase", "adjective"}},
    "WRITING-APPROVED-PATTERN-009": {"modes": {"chart"}, "roles": {"clause"}},
    "WRITING-APPROVED-PATTERN-010": {"modes": {"argumentative", "picture"}, "roles": {"noun", "phrase"}},
    "WRITING-APPROVED-PATTERN-011": {"modes": {"argumentative", "picture", "chart"}, "roles": {"noun", "phrase"}},
    "WRITING-APPROVED-PATTERN-012": {"modes": {"argumentative", "picture", "chart"}, "roles": {"noun", "verb", "adjective", "adverb", "phrase", "clause"}},
    "WRITING-APPROVED-PATTERN-013": {"modes": {"argumentative", "picture", "chart"}, "roles": {"noun", "verb", "adjective", "adverb", "phrase", "clause"}},
    "WRITING-APPROVED-PATTERN-014": {"modes": {"argumentative", "picture", "chart"}, "roles": {"noun", "verb", "phrase"}},
    "WRITING-APPROVED-PATTERN-015": {"modes": {"argumentative", "picture", "chart"}, "roles": {"noun", "phrase"}},
    "WRITING-APPROVED-PATTERN-016": {"modes": {"argumentative", "picture", "chart"}, "roles": {"noun", "verb", "phrase"}},
    "WRITING-APPROVED-PATTERN-017": {"modes": {"argumentative", "picture", "chart"}, "roles": {"noun", "adjective", "phrase"}},
    "WRITING-APPROVED-PATTERN-018": {"modes": {"letter", "notice"}, "roles": {"noun", "phrase"}},
    "WRITING-APPROVED-PATTERN-019": {"modes": {"letter", "notice"}, "roles": {"clause"}},
    "WRITING-APPROVED-PATTERN-020": {"modes": {"letter", "notice"}, "roles": {"verb", "phrase"}},
    "WRITING-APPROVED-PATTERN-021": {"modes": {"letter", "notice"}, "roles": {"noun", "verb", "phrase"}},
    "WRITING-APPROVED-PATTERN-022": {"modes": {"letter", "notice"}, "roles": {"noun", "phrase"}},
    "WRITING-APPROVED-PATTERN-023": {"modes": {"letter", "notice"}, "roles": {"noun", "phrase"}},
    "WRITING-APPROVED-PATTERN-024": {"modes": {"letter"}, "roles": {"noun", "phrase", "verb"}},
    "WRITING-APPROVED-PATTERN-025": {"modes": {"letter", "notice"}, "roles": {"noun", "phrase"}},
}

THEME_ALIASES = {
    "文化": "文化",
    "科技": "科技创新",
    "科技创新": "科技创新",
    "环境": "环境",
    "健康": "健康",
    "教育": "教育",
    "个人": "个人成长",
    "个人成长": "个人成长",
    "社会": "社会协作",
    "社会协作": "社会协作",
    "通用": "通用搭配",
    "通用搭配": "通用搭配",
}

WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "been", "being", "by", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "that", "the", "their", "this",
    "to", "with", "we", "our", "you", "your", "others", "much", "done",
}


def norm(value: str) -> str:
    value = (value or "").strip().lower().replace("’", "'")
    return " ".join(value.split())


def parse_date(value: str) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def due_bucket(days: int | None) -> tuple[str, bool, str]:
    if days is None:
        return "invalid-date", False, "needs-date-fix"
    if days < 0:
        return "future-date", False, "needs-date-fix"
    if days == 0:
        return "recent-today", False, "recent-repeat"
    if days == 1:
        return "D1", True, "due"
    if 2 <= days <= 4:
        return "D3", True, "due"
    if days == 5:
        return "between-D3-D7", False, "between-window"
    if 6 <= days <= 8:
        return "D7", True, "due"
    if 9 <= days <= 12:
        return "between-D7-D15", False, "between-window"
    if 13 <= days <= 17:
        return "D15", True, "due"
    if 18 <= days <= 25:
        return "between-D15-D30", False, "between-window"
    if 26 <= days <= 34:
        return "D30", True, "due"
    if 35 <= days <= 51:
        return "between-D30-D60", False, "between-window"
    if 52 <= days <= 68:
        return "D60", True, "due"
    if 69 <= days <= 79:
        return "between-D60-D90", False, "between-window"
    return "D90+", True, "due"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL {path}:{line_no}: {exc}") from exc
    return rows


def load_csv(path: Path, expected: list[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected:
            raise SystemExit(f"header mismatch for {path}: {reader.fieldnames!r}")
        rows = list(reader)
    return rows


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def word_tokens(value: str) -> list[str]:
    return [token.lower() for token in WORD_RE.findall(value or "")]


def cache_status(path: Path, today: date) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "generated_date": "", "selection_source": "recomputed_from_formal_bank"}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        first = next(reader, None)
    generated = (first or {}).get("generated_date", "")
    return {
        "status": "fresh" if generated == today.isoformat() else "stale",
        "generated_date": generated,
        "selection_source": "recomputed_from_formal_bank",
    }


def learner_evidence(row: dict[str, str]) -> tuple[str, int]:
    joined = "|".join(
        [row.get("tags", ""), row.get("review_note", ""), row.get("source_article", "")]
    )
    if "错句来源" in joined:
        return "wrong_sentence_tag_candidate", 0
    if "错题来源" in joined:
        return "wrong_question_tag_candidate", 1
    if row.get("type") == "熟词僻义" or "熟词僻义" in joined or "易混表达" in joined:
        return "familiar_new_meaning_or_confusion", 2
    if any(label in joined for label in ("题目定位", "题干选项", "选项", "定位句")):
        return "question_location_evidence", 3
    return "due_old_word", 4


def select_old_words(
    master_rows: list[dict[str, str]],
    mastered_norms: set[str],
    today: date,
    theme: str,
    target_norm: str,
    limit: int,
) -> list[dict[str, Any]]:
    by_item: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in master_rows:
        item_norm = norm(row.get("item", ""))
        if not item_norm or item_norm == target_norm or item_norm in mastered_norms:
            continue
        by_item[item_norm].append(row)

    candidates: list[dict[str, Any]] = []
    for item_norm, rows in by_item.items():
        representative = sorted(rows, key=lambda r: (r.get("type") not in {"单词", "词组", "熟词僻义"}, r.get("id", "")))[0]
        latest = max(
            (d for r in rows if (d := parse_date(r.get("last_seen", "")) or parse_date(r.get("date", "")))),
            default=None,
        )
        days = (today - latest).days if latest else None
        bucket, is_due, recency = due_bucket(days)
        evidence_options = [learner_evidence(row) for row in rows]
        evidence, evidence_rank = min(evidence_options, key=lambda item: item[1])
        joined_tags = "|".join(row.get("tags", "") for row in rows)
        theme_match = bool(theme and (theme in joined_tags or THEME_ALIASES.get(theme, theme) in joined_tags))
        recent_penalty = 1 if days in {0, 1} else 0
        # Strong learner evidence, then theme, then due windows, then oldest fallback.
        sort_key = (
            evidence_rank,
            0 if theme_match else 1,
            0 if is_due else 1,
            recent_penalty,
            -(days if days is not None else -1),
            item_norm,
        )
        candidates.append(
            {
                "item": representative.get("item", ""),
                "item_norm": item_norm,
                "representative_id": representative.get("id", ""),
                "all_ids": [row.get("id", "") for row in rows if row.get("id")],
                "type": representative.get("type", ""),
                "meaning": representative.get("meaning", ""),
                "usage": representative.get("usage", ""),
                "tags": sorted({tag for row in rows for tag in row.get("tags", "").split("|") if tag}),
                "learner_evidence_kind": evidence,
                "learner_evidence_caution": (
                    "derived candidate only; does not by itself prove the learner currently does not know this item"
                ),
                "theme_match": theme_match,
                "latest_basis_date": latest.isoformat() if latest else "",
                "days_since_seen": days,
                "due_bucket": bucket,
                "is_due_today": is_due,
                "recency_policy": recency,
                "selection_requires_naturalness_review": True,
                "_sort": sort_key,
            }
        )

    selected = sorted(candidates, key=lambda row: row["_sort"])[:limit]
    for row in selected:
        row.pop("_sort", None)
    return selected


def select_patterns(patterns: list[dict[str, Any]], mode: str, pos: str, limit: int) -> list[dict[str, Any]]:
    candidates = []
    for row in patterns:
        if row.get("status") not in {"approved", "corrected"}:
            continue
        runtime = PATTERN_RUNTIME.get(str(row.get("approved_id")), {"modes": set(), "roles": set()})
        if mode not in runtime["modes"]:
            continue
        role_match = not pos or pos in runtime["roles"] or "clause" in runtime["roles"]
        if not role_match:
            continue
        candidates.append(
            {
                "ref": row.get("approved_id"),
                "category": row.get("category"),
                "template": row.get("template"),
                "status": row.get("status"),
                "source_ref": f"{row.get('source_id')}#p{row.get('source_page')}",
                "compatible_modes": sorted(runtime["modes"]),
                "syntactic_roles": sorted(runtime["roles"]),
                "usage_constraints": row.get("usage_constraints", []),
                "selection_status": "syntactically_eligible_needs_semantic_and_naturalness_review",
            }
        )
    return candidates[:limit]


def select_writing_vocab(
    rows: list[dict[str, Any]], target_norm: str, theme: str, limit: int
) -> list[dict[str, Any]]:
    normalized_theme = THEME_ALIASES.get(theme, theme)
    candidates = []
    for row in rows:
        if row.get("status") not in {"approved", "corrected"}:
            continue
        item_norm = norm(str(row.get("normalized", "")))
        tokens = set(word_tokens(item_norm))
        exact = item_norm == target_norm
        token_hit = bool(target_norm and target_norm in tokens)
        theme_match = bool(normalized_theme and row.get("theme") == normalized_theme)
        general = row.get("theme") == "通用搭配"
        if not (exact or token_hit or theme_match or general):
            continue
        rank = (0 if exact else 1 if token_hit else 2 if theme_match else 3, str(row.get("approved_id")))
        candidates.append(
            {
                "ref": row.get("approved_id"),
                "item": row.get("normalized"),
                "theme": row.get("theme"),
                "form": row.get("form"),
                "status": row.get("status"),
                "source_ref": f"{row.get('source_id')}#p{row.get('source_page')}",
                "match_basis": "exact_target" if exact else "contains_target_token" if token_hit else "exact_theme" if theme_match else "general_collocation",
                "usage_constraints": row.get("usage_constraints", []),
                "selection_status": "eligible_needs_naturalness_review",
                "_sort": rank,
            }
        )
    selected = sorted(candidates, key=lambda row: row["_sort"])[:limit]
    for row in selected:
        row.pop("_sort", None)
    return selected


def syllabus_indexes(
    unique_rows: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id = {str(row.get("entry_id")): row for row in entries}
    by_norm = {norm(str(row.get("normalized", ""))): row for row in unique_rows}
    verified_by_norm: dict[str, dict[str, Any]] = {}
    for item_norm, row in by_norm.items():
        verified_occurrences = [
            by_id[entry_id]
            for entry_id in row.get("entry_ids", [])
            if entry_id in by_id and str(by_id[entry_id].get("verification_status", "")).startswith("verified_")
        ]
        if verified_occurrences:
            verified_by_norm[item_norm] = verified_occurrences[0]
    return by_norm, by_id, verified_by_norm


def syll_ref(row: dict[str, Any], basis: str) -> dict[str, Any]:
    return {
        "ref": row.get("entry_id"),
        "item": row.get("normalized"),
        "verification_status": row.get("verification_status"),
        "pdf_page": row.get("pdf_page"),
        "column": row.get("column"),
        "source_id": row.get("source_id"),
        "match_basis": basis,
    }


def select_syllabus(
    target_norm: str,
    writing_vocab: list[dict[str, Any]],
    old_words: list[dict[str, Any]],
    by_norm: dict[str, dict[str, Any]],
    verified_by_norm: dict[str, dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    double_hits: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item_norm: str, basis: str) -> None:
        if not item_norm or item_norm in seen:
            return
        if item_norm in verified_by_norm:
            selected.append(syll_ref(verified_by_norm[item_norm], basis))
            seen.add(item_norm)
        elif item_norm in by_norm:
            row = by_norm[item_norm]
            gaps.append(
                {
                    "item": item_norm,
                    "canonical_entry_id": row.get("canonical_entry_id"),
                    "verification_status": row.get("verification_status"),
                    "required_action": "on_demand_visual_review_before_use",
                    "match_basis": basis,
                }
            )

    add(target_norm, "target_exact")
    for vocab in writing_vocab:
        hits = []
        for token in word_tokens(str(vocab.get("item", ""))):
            if token in STOPWORDS:
                continue
            before = len(selected)
            add(token, f"writing_vocab_token:{vocab.get('ref')}")
            if len(selected) > before:
                hits.append(token)
        if hits:
            double_hits.append(
                {"writing_vocab_ref": vocab.get("ref"), "writing_item": vocab.get("item"), "verified_syllabus_tokens": hits}
            )
    for old in old_words:
        add(str(old.get("item_norm", "")), f"old_word_exact:{old.get('representative_id')}")

    return selected[:limit], gaps[:limit], double_hits


def parse_sp_cards(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    starts = list(re.finditer(r"^## (SP-\d{3})｜(.+)$", text, flags=re.MULTILINE))
    cards = []
    for index, match in enumerate(starts):
        block = text[match.start() : starts[index + 1].start() if index + 1 < len(starts) else len(text)]
        fields: dict[str, str] = {}
        for label in ("骨架", "场景标签", "可复用程度", "生成模板", "相关词汇/搭配", "use_count", "last_used"):
            field_match = re.search(rf"^\*\*{re.escape(label)}\*\*：(.+)$", block, flags=re.MULTILINE)
            fields[label] = field_match.group(1).strip().strip("`") if field_match else ""
        cards.append(
            {
                "ref": match.group(1),
                "title": match.group(2).strip(),
                "skeleton": fields["骨架"],
                "scenes": fields["场景标签"].split("|") if fields["场景标签"] else [],
                "reusability": fields["可复用程度"],
                "generation_template": fields["生成模板"],
                "related_items": [item.strip() for item in re.split(r"[、,]", fields["相关词汇/搭配"]) if item.strip()],
                "use_count": int(fields["use_count"]) if fields["use_count"].isdigit() else None,
                "last_used": fields["last_used"],
            }
        )
    return cards


def select_sp(cards: list[dict[str, Any]], target_norm: str, old_words: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    old_norms = {str(row.get("item_norm", "")) for row in old_words}
    candidates = []
    for card in cards:
        related = {norm(item) for item in card.get("related_items", [])}
        exact_target = target_norm in related
        old_hits = sorted(old_norms & related)
        writing_scene = "写作模板" in card.get("scenes", [])
        if not (exact_target or old_hits or writing_scene):
            continue
        candidates.append(
            {
                **card,
                "match_basis": "target_explicit_related_item" if exact_target else "old_word_explicit_related_item" if old_hits else "writing_scene_only",
                "matched_old_words": old_hits,
                "selection_status": "optional_candidate_needs_slot_mapping_and_naturalness_review",
                "_sort": (
                    0 if exact_target else 1 if old_hits else 2,
                    card.get("use_count") if card.get("use_count") is not None else 10**9,
                    card.get("last_used") or "0000-00-00",
                    card.get("ref"),
                ),
            }
        )
    selected = sorted(candidates, key=lambda row: row["_sort"])[:limit]
    for row in selected:
        row.pop("_sort", None)
    return selected


def load_graph_runtime(
    root: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    """Load and hash-check the derived graph used by the selector.

    A present but stale graph is not accepted.  The selector still reports the
    stale state, but it will not claim that a four-layer packet is ready.
    """

    graph_dir = root / "raw" / "reference_relations" / "writing-vocabulary-foundation"
    required = [graph_dir / name for name in ("nodes.jsonl", "edges.jsonl", "lookup.json", "summary.json", "manifest.json")]
    if not all(path.exists() for path in required):
        return (
            {
                "status": "missing",
                "path": str(graph_dir.relative_to(root)),
                "required_action": "run scripts/build_writing_vocabulary_relationship_graph.py",
            },
            {},
            {},
        )
    try:
        summary = json.loads((graph_dir / "summary.json").read_text(encoding="utf-8"))
        manifest = json.loads((graph_dir / "manifest.json").read_text(encoding="utf-8"))
        lookup = json.loads((graph_dir / "lookup.json").read_text(encoding="utf-8"))
        nodes = load_jsonl(graph_dir / "nodes.jsonl")
    except (OSError, json.JSONDecodeError, SystemExit) as exc:
        return (
            {
                "status": "invalid",
                "path": str(graph_dir.relative_to(root)),
                "error": str(exc),
                "required_action": "rebuild and verify the relationship graph",
            },
            {},
            {},
        )

    stale_inputs = []
    for record in manifest.get("inputs", []):
        path_text = str(record.get("path", ""))
        path = root / path_text
        if not path.is_file() or sha256(path) != record.get("sha256"):
            stale_inputs.append(path_text)
    for path_text, expected in manifest.get("optional_inputs", {}).items():
        if (root / path_text).is_file() != bool(expected.get("exists")):
            stale_inputs.append(path_text)
    review_path = "bank/review_exclusion_ledger.jsonl"
    if (root / review_path).is_file() and review_path not in {row.get("path") for row in manifest.get("inputs", [])}:
        stale_inputs.append(review_path)
    bad_outputs = []
    for path_text, record in manifest.get("outputs", {}).items():
        path = root / path_text
        if not path.is_file() or sha256(path) != record.get("sha256"):
            bad_outputs.append(path_text)
    node_by_id = {str(row.get("node_id")): row for row in nodes}
    structural_errors = []
    if len(node_by_id) != len(nodes):
        structural_errors.append("duplicate_node_id")
    if summary.get("node_count") != len(nodes):
        structural_errors.append("summary_node_count_mismatch")
    if lookup.get("schema") != "writing_vocab_relation_lookup_v1":
        structural_errors.append("lookup_schema_mismatch")
    if stale_inputs or bad_outputs or structural_errors:
        return (
            {
                "status": "stale_or_invalid",
                "path": str(graph_dir.relative_to(root)),
                "stale_inputs": stale_inputs,
                "bad_outputs": bad_outputs,
                "structural_errors": structural_errors,
                "required_action": "run graph builder and --verify-only before selection",
            },
            {},
            {},
        )
    public = {
        "status": "loaded",
        "path": str(graph_dir.relative_to(root)),
        "schema": summary.get("schema"),
        "node_count": summary.get("node_count"),
        "edge_count": summary.get("edge_count"),
        "manifest_schema": manifest.get("schema"),
        "manifest_hash_gate": "PASS",
        "syllabus_verified_count": summary.get("syllabus_verified_count"),
    }
    return public, node_by_id, lookup


def graph_gate_single_ref_candidates(
    candidates: list[dict[str, Any]],
    node_by_id: dict[str, dict[str, Any]],
    expected_type: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        node_id = str(candidate.get("ref", ""))
        node = node_by_id.get(node_id)
        if (
            node is None
            or node.get("node_type") != expected_type
            or not node.get("automatic_use_allowed")
        ):
            rejected.append(
                {
                    "ref": node_id,
                    "expected_type": expected_type,
                    "reason": "missing_wrong_type_or_graph_gate_denied",
                }
            )
            continue
        enriched = dict(candidate)
        enriched["graph_node_id"] = node_id
        enriched["graph_status"] = node.get("status")
        enriched["graph_automatic_use_allowed"] = True
        enriched["graph_source_ref"] = node.get("source_ref", [])
        accepted.append(enriched)
    return accepted, rejected


def graph_gate_old_words(
    candidates: list[dict[str, Any]], node_by_id: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        graph_ids = [
            f"LEARNER-{row_id}"
            for row_id in candidate.get("all_ids", [])
            if f"LEARNER-{row_id}" in node_by_id
            and node_by_id[f"LEARNER-{row_id}"].get("node_type") == "learner_bank_item"
            and node_by_id[f"LEARNER-{row_id}"].get("automatic_use_allowed")
        ]
        if not graph_ids:
            rejected.append(
                {
                    "item": candidate.get("item"),
                    "all_ids": candidate.get("all_ids", []),
                    "reason": "no_active_learner_graph_node",
                }
            )
            continue
        enriched = dict(candidate)
        enriched["graph_node_ids"] = graph_ids
        enriched["graph_automatic_use_allowed"] = True
        enriched["graph_source_refs"] = sorted(
            {
                ref
                for node_id in graph_ids
                for ref in node_by_id[node_id].get("source_ref", [])
            }
        )
        accepted.append(enriched)
    return accepted, rejected


def build_packet(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    today = parse_date(args.today)
    if today is None:
        raise SystemExit("--today must be YYYY-MM-DD")
    if args.user_evidence not in ALLOWED_USER_EVIDENCE:
        raise SystemExit(f"--user-evidence must be one of {sorted(ALLOWED_USER_EVIDENCE - {''})}")
    if args.mode not in ALLOWED_MODES:
        raise SystemExit(f"--mode must be one of {sorted(ALLOWED_MODES)}")
    if args.pos not in ALLOWED_POS:
        raise SystemExit(f"--pos must be one of {sorted(ALLOWED_POS - {''})}")

    paths = {
        "master": root / "bank" / "master_bank.csv",
        "mastered": root / "bank" / "mastered_items.csv",
        "review_status": root / "bank" / "review_exclusion_ledger.jsonl",
        "patterns": root / "raw" / "writing_reference" / "reviewed" / "approved_patterns.jsonl",
        "writing_vocab": root / "raw" / "writing_reference" / "reviewed" / "approved_vocabulary.jsonl",
        "syllabus_unique": root / "raw" / "reference_sources" / "syllabus_vocabulary" / "syllabus_vocabulary_unique.jsonl",
        "syllabus_entries": root / "raw" / "reference_sources" / "syllabus_vocabulary" / "syllabus_vocabulary_entries.jsonl",
        "sp": root / "bank" / "sentence_patterns.md",
        "curve": root / "wiki" / "old_words" / "memory_curve_active_items.csv",
    }
    missing = [
        str(path)
        for name, path in paths.items()
        if name != "review_status" and not path.exists()
    ]
    if missing:
        raise SystemExit(f"required inputs missing: {missing}")

    protected = [paths["master"], paths["mastered"], paths["sp"]]
    if paths["review_status"].exists():
        protected.append(paths["review_status"])
    before_hashes = {str(path.relative_to(root)): sha256(path) for path in protected}
    graph_public, graph_nodes, graph_lookup = load_graph_runtime(root)

    master_rows = load_csv(paths["master"], MASTER_HEADER)
    mastered_rows = load_csv(paths["mastered"], MASTERED_HEADER)
    mastered_norms = {norm(row.get("item", "")) for row in mastered_rows if norm(row.get("item", ""))}
    review_records = load_review_status_ledger(paths["review_status"])
    review_status = effective_review_status(review_records)
    mastered_norms = effective_mastered_norms(mastered_rows, review_records)
    mastered_ids = effective_mastered_ids(mastered_rows, review_records)
    mastered_norms.update(norm(row["item"]) for row in master_rows if row["id"] in mastered_ids)
    review_excluded_norms = {
        item_norm
        for item_norm, row in review_status.items()
        if row.get("status") == "mastered_sentence_nonreport"
    }
    selector_excluded_norms = mastered_norms | review_excluded_norms
    target_norm = norm(args.item)
    target_bank_rows = [row for row in master_rows if norm(row.get("item", "")) == target_norm]
    target_independently_mastered = target_norm in mastered_norms or any(row["id"] in mastered_ids for row in target_bank_rows)
    target_review_excluded = target_norm in review_excluded_norms
    target_mastered = target_independently_mastered or target_review_excluded

    patterns = load_jsonl(paths["patterns"])
    writing_vocab_rows = load_jsonl(paths["writing_vocab"])
    syllabus_unique = load_jsonl(paths["syllabus_unique"])
    syllabus_entries = load_jsonl(paths["syllabus_entries"])
    by_syllabus_norm, _, verified_syllabus = syllabus_indexes(syllabus_unique, syllabus_entries)

    graph_gate_failures: list[dict[str, Any]] = []
    old_words = select_old_words(
        master_rows, selector_excluded_norms, today, args.theme,
        target_norm, args.max_old_words,
    )
    pattern_candidates = select_patterns(patterns, args.mode, args.pos, args.max_patterns)
    writing_vocab = select_writing_vocab(writing_vocab_rows, target_norm, args.theme, args.max_writing_vocab)
    if graph_public.get("status") == "loaded":
        old_words, rejected = graph_gate_old_words(old_words, graph_nodes)
        graph_gate_failures.extend(rejected)
        pattern_candidates, rejected = graph_gate_single_ref_candidates(
            pattern_candidates, graph_nodes, "approved_writing_pattern"
        )
        graph_gate_failures.extend(rejected)
        writing_vocab, rejected = graph_gate_single_ref_candidates(
            writing_vocab, graph_nodes, "approved_writing_vocabulary"
        )
        graph_gate_failures.extend(rejected)
    else:
        graph_public["diagnostic_pre_graph_candidate_counts"] = {
            "old_words": len(old_words),
            "writing_patterns": len(pattern_candidates),
            "writing_vocabulary": len(writing_vocab),
        }
        # Never expose un-gated records as usable foundations.
        old_words = []
        pattern_candidates = []
        writing_vocab = []
    syllabus_refs, syllabus_gaps, double_hits = select_syllabus(
        target_norm, writing_vocab, old_words, by_syllabus_norm, verified_syllabus, args.max_syllabus
    )
    sp_candidates = select_sp(parse_sp_cards(paths["sp"]), target_norm, old_words, args.max_sp)
    if graph_public.get("status") == "loaded":
        syllabus_refs, rejected = graph_gate_single_ref_candidates(
            syllabus_refs, graph_nodes, "syllabus_headword"
        )
        graph_gate_failures.extend(rejected)
        sp_candidates, rejected = graph_gate_single_ref_candidates(
            sp_candidates, graph_nodes, "sentence_pattern_card"
        )
        graph_gate_failures.extend(rejected)
    else:
        graph_public["diagnostic_pre_graph_candidate_counts"].update(
            {
                "syllabus_vocabulary": len(syllabus_refs),
                "sentence_patterns": len(sp_candidates),
            }
        )
        syllabus_refs = []
        double_hits = []
        sp_candidates = []

    graph_indexes = graph_lookup.get("indexes", {}) if graph_lookup else {}
    graph_queries = {
        "target_normalized": target_norm,
        "target_node_ids": graph_indexes.get("normalized", {}).get(target_norm, []),
        "user_wording_normalized": norm(args.user_wording),
        "user_wording_node_ids": graph_indexes.get("normalized", {}).get(
            norm(args.user_wording), []
        )
        if args.user_wording
        else [],
        "theme": THEME_ALIASES.get(args.theme, args.theme),
        "theme_node_ids": graph_indexes.get("theme", {}).get(
            norm(THEME_ALIASES.get(args.theme, args.theme)), []
        )
        if args.theme
        else [],
    }

    if target_norm in verified_syllabus:
        syllabus_path = "SYL-0_target_verified"
    elif double_hits:
        syllabus_path = "SYL-1_writing_vocab_and_syllabus_double_hit"
    elif syllabus_refs:
        syllabus_path = "SYL-2_independent_verified_companion"
    elif any(row.get("item") == target_norm for row in syllabus_gaps):
        syllabus_path = "SYL-3_on_demand_visual_required"
    else:
        syllabus_path = "SYL-4_no_verified_natural_candidate"

    if old_words:
        first_old = old_words[0]
        if first_old.get("learner_evidence_kind") in {
            "wrong_sentence_tag_candidate",
            "wrong_question_tag_candidate",
            "familiar_new_meaning_or_confusion",
            "question_location_evidence",
        }:
            old_word_path = (
                "OLD-0_learner_evidence_due"
                if first_old.get("is_due_today")
                else "OLD-1_learner_evidence_not_due"
            )
        elif first_old.get("is_due_today"):
            old_word_path = "OLD-3_due_active_item"
        else:
            old_word_path = "OLD-4_oldest_or_ranked_fallback"
    else:
        old_word_path = "OLD-5_no_eligible_active_item"
    sp_path = "SP-0_optional_candidate_available" if sp_candidates else "SP-1_no_compatible_card"

    has_context = bool(args.source_sentence.strip())
    has_user_foundation = bool(args.user_wording.strip()) or args.user_evidence in {
        "unknown",
        "mistranslated",
        "missed",
    }
    graph_loaded = graph_public.get("status") == "loaded"
    graph_path = "GRAPH-0_loaded" if graph_loaded else "GRAPH-1_rebuild_required"
    bbdc_status = (
        "mastered_excluded"
        if target_mastered
        else "needs_context"
        if not has_context
        else "needs_user_evidence"
        if not has_user_foundation
        else "needs_reference_graph"
        if not graph_loaded
        else "ready"
    )
    foundation_ready = (
        bool(has_user_foundation and pattern_candidates and writing_vocab and syllabus_refs)
        and not target_mastered
        and graph_loaded
    )
    reference_gap = []
    if not pattern_candidates:
        reference_gap.append("no_syntactically_eligible_approved_writing_pattern")
    if not writing_vocab:
        reference_gap.append("no_approved_writing_vocabulary_candidate")
    if not syllabus_refs:
        reference_gap.append("no_verified_syllabus_vocabulary_candidate")
    if not has_context:
        reference_gap.append("missing_real_source_sentence")
    if not has_user_foundation:
        reference_gap.append("missing_user_wording_or_explicit_wrong_word_evidence")
    if target_mastered:
        reference_gap.append("target_already_mastered")
    if not graph_loaded:
        reference_gap.append("relationship_graph_missing_stale_or_invalid")

    current_user_foundation = []
    if has_user_foundation:
        current_user_foundation.append(
            {
                "wording": args.user_wording.strip() or args.item,
                "evidence_kind": args.user_evidence or "user_wording",
                "source": "current_user_input",
                "priority": "highest",
                "caution": "preserve the learner's intended meaning; correct errors instead of copying them blindly",
            }
        )

    after_hashes = {str(path.relative_to(root)): sha256(path) for path in protected}
    packet = {
        "schema": "bbdc_foundation_packet_v1",
        "generated_at": today.isoformat(),
        "read_only": True,
        "target": {
            "item": args.item,
            "item_norm": target_norm,
            "meaning": args.meaning,
            "pos": args.pos,
            "theme": THEME_ALIASES.get(args.theme, args.theme),
            "mode": args.mode,
            "source_article": args.source_article,
            "source_sentence": args.source_sentence,
            "user_wording": args.user_wording,
            "user_evidence": args.user_evidence,
        },
        "eligibility": {
            "bbdc_status": bbdc_status,
            "bank_match": "existing_bank" if target_bank_rows else "new_candidate",
            "bank_ids": [row.get("id") for row in target_bank_rows],
            "mastered_check": "excluded" if target_mastered else "clear",
            "mastered_evidence_source": (
                "independent_correct_use"
                if target_independently_mastered
                else "sentence_nonreport_review_exclusion"
                if target_review_excluded
                else "none"
            ),
            "foundation_ready_for_generation_review": foundation_ready and has_context,
        },
        "relationship_graph": {**graph_public, "queries": graph_queries},
        "memory_curve": cache_status(paths["curve"], today),
        "foundation": {
            "current_user_foundation": current_user_foundation,
            "user_old_word_candidates": old_words,
            "writing_pattern_candidates": pattern_candidates,
            "writing_vocab_candidates": writing_vocab,
            "syllabus_vocab_candidates": syllabus_refs,
            "double_hits": double_hits,
            "optional_sp_candidates": sp_candidates,
        },
        "generation": {
            "example": "",
            "translation": "",
            "structure_breakdown": "",
            "word_count": None,
            "status": "not_generated_by_selector",
        },
        "validation": {
            "all_refs_exist": graph_loaded,
            "all_selected_refs_exist": graph_loaded,
            "statuses_allowed": all(row.get("status") in {"approved", "corrected"} for row in patterns)
            and all(row.get("status") in {"approved", "corrected"} for row in writing_vocab_rows),
            "target_present": None,
            "writing_pattern_constraints_pass": None,
            "writing_vocab_present": None,
            "syllabus_vocab_present": None,
            "sp_nodes_pass": None,
            "naturalness": "pending_model_review",
            "degradation_path": [graph_path, syllabus_path, old_word_path, sp_path],
            "graph_gate_rejections": graph_gate_failures,
            "unreviewed_syllabus_matches": syllabus_gaps,
            "reference_gap": reference_gap,
            "required_final_checks": [
                "12_to_28_words",
                "target_uses_requested_meaning_and_part_of_speech",
                "approved_writing_pattern_is_structurally_realized",
                "approved_writing_vocabulary_appears_naturally",
                "verified_syllabus_vocabulary_appears_naturally",
                "selected_user_wording_or_old_word_is_not_forced",
                "all_usage_constraints_pass",
                "generated_example_is_never_written_as_source_sentence",
            ],
        },
        "formal_input_hashes_before": before_hashes,
        "formal_input_hashes_after": after_hashes,
        "formal_sources_unchanged": before_hashes == after_hashes,
        "formal_writeback": "none",
    }
    return packet


def verify(root: Path) -> dict[str, Any]:
    failures: list[str] = []
    pattern_rows = load_jsonl(root / "raw" / "writing_reference" / "reviewed" / "approved_patterns.jsonl")
    vocab_rows = load_jsonl(root / "raw" / "writing_reference" / "reviewed" / "approved_vocabulary.jsonl")
    entries = load_jsonl(root / "raw" / "reference_sources" / "syllabus_vocabulary" / "syllabus_vocabulary_entries.jsonl")
    master = load_csv(root / "bank" / "master_bank.csv", MASTER_HEADER)
    load_csv(root / "bank" / "mastered_items.csv", MASTERED_HEADER)
    cards = parse_sp_cards(root / "bank" / "sentence_patterns.md")

    if len(PATTERN_RUNTIME) != len(pattern_rows):
        failures.append(f"pattern runtime coverage {len(PATTERN_RUNTIME)} != approved patterns {len(pattern_rows)}")
    pattern_ids = {str(row.get("approved_id")) for row in pattern_rows}
    if set(PATTERN_RUNTIME) != pattern_ids:
        failures.append("pattern runtime ids do not exactly match approved pattern ids")
    if any(row.get("status") not in {"approved", "corrected"} for row in pattern_rows):
        failures.append("non-approved pattern in approved file")
    if any(row.get("status") not in {"approved", "corrected"} for row in vocab_rows):
        failures.append("non-approved vocabulary in approved file")
    if any(str(row.get("verification_status", "")).startswith("verified_") and not row.get("entry_id") for row in entries):
        failures.append("verified syllabus row without entry_id")
    if len(cards) != len({card["ref"] for card in cards}):
        failures.append("duplicate SP ids")

    def make_smoke_args(**overrides: Any) -> argparse.Namespace:
        values: dict[str, Any] = {
            "root": str(root),
            "item": "culture",
            "meaning": "文化",
            "pos": "noun",
            "theme": "文化",
            "source_article": "selector-smoke-test",
            "source_sentence": "The original source context contains culture.",
            "user_wording": "culture in public life",
            "user_evidence": "unknown",
            "mode": "argumentative",
            "today": date.today().isoformat(),
            "max_old_words": 6,
            "max_patterns": 4,
            "max_writing_vocab": 6,
            "max_syllabus": 6,
            "max_sp": 3,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    primary_args = make_smoke_args()
    packet = build_packet(primary_args)
    if packet["formal_writeback"] != "none" or not packet["formal_sources_unchanged"]:
        failures.append("selector violated read-only contract")
    if packet["eligibility"]["bbdc_status"] == "mastered_excluded":
        # A future learner action may legitimately master the smoke target.
        pass
    elif packet["eligibility"]["bbdc_status"] != "ready":
        failures.append("context-rich smoke packet is not ready")
    if not packet["foundation"]["writing_pattern_candidates"]:
        failures.append("smoke packet has no writing pattern candidates")
    if not packet["foundation"]["writing_vocab_candidates"]:
        failures.append("smoke packet has no writing vocabulary candidates")
    if not packet["foundation"]["syllabus_vocab_candidates"]:
        failures.append("smoke packet has no verified syllabus candidates")
    if any(
        not str(row.get("verification_status", "")).startswith("verified_")
        for row in packet["foundation"]["syllabus_vocab_candidates"]
    ):
        failures.append("unverified syllabus row entered automatic candidate set")
    if packet["relationship_graph"].get("status") != "loaded":
        failures.append("derived relationship graph is missing or not loadable")

    missing_context = build_packet(make_smoke_args(source_sentence=""))
    if missing_context["eligibility"]["bbdc_status"] not in {
        "needs_context",
        "mastered_excluded",
    }:
        failures.append("missing source sentence did not trigger needs_context")

    missing_user_foundation = build_packet(
        make_smoke_args(user_wording="", user_evidence="candidate")
    )
    if missing_user_foundation["eligibility"]["bbdc_status"] not in {
        "needs_user_evidence",
        "mastered_excluded",
    }:
        failures.append("missing user wording/evidence did not trigger needs_user_evidence")
    if missing_user_foundation["eligibility"]["foundation_ready_for_generation_review"]:
        failures.append("packet without user wording/evidence was marked foundation-ready")

    unreviewed_target = build_packet(
        make_smoke_args(
            item="discern",
            meaning="辨别；识别",
            pos="verb",
            theme="教育",
            user_wording="discern reliable evidence",
            user_evidence="unknown",
        )
    )
    gap_items = {
        str(row.get("item"))
        for row in unreviewed_target["validation"].get(
            "unreviewed_syllabus_matches", []
        )
    }
    if "discern" not in gap_items:
        failures.append("unreviewed target did not trigger on-demand visual gap")
    if not unreviewed_target["foundation"]["current_user_foundation"]:
        failures.append("current user wording/evidence was not preserved")

    letter_packet = build_packet(
        make_smoke_args(item="inform", meaning="通知", pos="verb", theme="", mode="letter")
    )
    letter_refs = {
        str(row.get("ref"))
        for row in letter_packet["foundation"]["writing_pattern_candidates"]
    }
    if letter_refs and any(int(ref.rsplit("-", 1)[-1]) < 18 for ref in letter_refs):
        failures.append("letter mode admitted picture/chart/argumentative-only pattern")

    source_hashes = packet["formal_input_hashes_before"]
    for candidate_packet in (
        missing_context,
        missing_user_foundation,
        unreviewed_target,
        letter_packet,
    ):
        if candidate_packet["formal_input_hashes_after"] != source_hashes:
            failures.append("multi-scenario verification observed formal input mutation")
            break

    return {
        "schema": "bbdc_foundation_selector_validation_v1",
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "approved_patterns": len(pattern_rows),
        "approved_vocabulary": len(vocab_rows),
        "verified_syllabus_occurrences": sum(
            str(row.get("verification_status", "")).startswith("verified_") for row in entries
        ),
        "master_rows": len(master),
        "sp_cards": len(cards),
        "smoke_targets": ["culture", "discern", "inform"],
        "scenario_count": 5,
        "formal_sources_unchanged": packet["formal_sources_unchanged"],
        "relationship_graph": packet["relationship_graph"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only BBDC foundation packet.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--item", default="")
    parser.add_argument("--meaning", default="")
    parser.add_argument("--pos", default="", choices=sorted(ALLOWED_POS))
    parser.add_argument("--theme", default="")
    parser.add_argument("--source-article", default="")
    parser.add_argument("--source-sentence", default="")
    parser.add_argument("--user-wording", default="")
    parser.add_argument("--user-evidence", default="", choices=sorted(ALLOWED_USER_EVIDENCE))
    parser.add_argument("--mode", default="argumentative", choices=sorted(ALLOWED_MODES))
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--max-old-words", type=int, default=8)
    parser.add_argument("--max-patterns", type=int, default=6)
    parser.add_argument("--max-writing-vocab", type=int, default=8)
    parser.add_argument("--max-syllabus", type=int, default=8)
    parser.add_argument("--max-sp", type=int, default=4)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    result = verify(root) if args.verify_only else build_packet(args)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2, sort_keys=True))
    if result.get("status") == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
