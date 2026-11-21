"""Bounded KWIC over the current, answer-free sentence-support projection."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .sentence_review import OFFSET_UNIT, normalize_word, tokens
from .util import canonical_bytes, object_sha256

SCHEMA = "english_concordance_v1"
NORMALIZATION_VERSION = "word-boundaries-casefold-v1"
MAX_CANDIDATES = 256


def create_index(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE sentences (
            sentence_key TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            sentence_id TEXT NOT NULL,
            data TEXT NOT NULL,
            UNIQUE (source_id, sentence_id)
        );
        CREATE TABLE concordance_occurrences (
            normalized_query TEXT NOT NULL,
            sentence_key TEXT NOT NULL REFERENCES sentences,
            match_start INTEGER NOT NULL,
            match_end INTEGER NOT NULL,
            PRIMARY KEY (normalized_query, sentence_key, match_start, match_end)
        ) WITHOUT ROWID;
        CREATE INDEX concordance_by_sentence ON concordance_occurrences
            (sentence_key, match_start, match_end, normalized_query);
    """)


def put_sentence(db: sqlite3.Connection, key: str, row: dict[str, Any], words) -> None:
    db.execute("INSERT INTO sentences VALUES (?, ?, ?, ?)",
               (key, row["source_id"], row["sentence_id"], canonical_bytes(row).decode()))
    db.executemany("INSERT INTO concordance_occurrences VALUES (?, ?, ?, ?)",
                   ((norm, key, start, end) for norm, start, end in words))


def indexed_words(text: str):
    return [(normalize_word(t["text"]), t["start"], t["end"]) for t in tokens(text)]


def prior_words(db: sqlite3.Connection, key: str):
    return db.execute("""SELECT normalized_query, match_start, match_end
                         FROM concordance_occurrences WHERE sentence_key=?
                         ORDER BY match_start, match_end, normalized_query""", (key,)).fetchall()


def _phrase(text: str) -> list[str]:
    if not isinstance(text, str) or not 0 < len(text.strip()) <= 120:
        raise ValidationError("a concordance query needs one bounded word or phrase")
    parts = text.split()
    if len(parts) > 8 or any(not tokens(part) or tokens(part)[0]["text"] != part for part in parts):
        raise ValidationError("a concordance phrase must contain one to eight whole words")
    return [normalize_word(part) for part in parts]


def candidate_rows(db: sqlite3.Connection, phrase: str, current: tuple[str, str] | None):
    terms = _phrase(phrase)
    if current is not None:
        rows = db.execute("""
            SELECT s.data, o.match_start FROM concordance_occurrences o
            JOIN sentences s ON s.sentence_key = o.sentence_key
            WHERE o.normalized_query = ? AND s.source_id=? AND s.sentence_id=?
            ORDER BY o.match_start
        """, (terms[0], *current))
        for encoded, start in rows:
            yield json.loads(encoded), start, terms
    cursor = db.execute("""
        SELECT s.data, o.match_start FROM concordance_occurrences o
        JOIN sentences s ON s.sentence_key = o.sentence_key
        WHERE o.normalized_query = ?
        ORDER BY o.sentence_key, o.match_start LIMIT ?
    """, (terms[0], MAX_CANDIDATES + 1))
    for index, (encoded, start) in enumerate(cursor):
        if index == MAX_CANDIDATES:
            yield None, None, terms
            break
        yield json.loads(encoded), start, terms


def occurrence(row: dict[str, Any], start: int, terms: list[str]) -> dict[str, Any] | None:
    text = row["original_sentence"]
    words = tokens(text)
    for i, word in enumerate(words):
        if word["start"] == start:
            break
    else:
        return None
    match = words[i:i + len(terms)]
    if len(match) != len(terms) or [normalize_word(w["text"]) for w in match] != terms:
        return None
    if any(not text[a["end"]:b["start"]].isspace() for a, b in zip(match, match[1:])):
        return None
    u = i + len(match)
    end = match[-1]["end"]
    left_start = words[i - 6]["start"] if i > 6 else 0
    right_end = words[u + 5]["end"] if len(words) > u + 6 else len(text)
    source = {key: row.get(key) for key in
              ("source_id", "source_hash", "sentence_id", "sentence_sha256", "support_record_sha256")}
    verified = next((item for item in row["reviewed_occurrences"]
                     if item["start"] == start and item["end"] == end), {})
    return {
        "schema_version": SCHEMA,
        "occurrence_id": "EN-OCC-" + object_sha256([
            row["source_id"], row["sentence_id"], row["sentence_sha256"], start, end
        ])[:24].upper(),
        **source, "offset_unit": OFFSET_UNIT, "match_start": start, "match_end": end,
        "surface": text[start:end], "normalized_query": " ".join(terms),
        "scope_bindings": row["scope_bindings"], "visibility": "answer_free",
        "source_locator": row["source_locator"], "evidence_refs": row["source_evidence_refs"],
        **{k: verified[k] for k in ("reviewed_sense_id", "sense_label", "sense_evidence_refs") if k in verified},
        "left": text[left_start:start], "right": text[end:right_end],
        "context_clipped": left_start > 0 or right_end < len(text), "original_sentence": text,
    }


def select_rows(rows: list[dict[str, Any]], current: tuple[str, str] | None,
                limit: int) -> list[dict[str, Any]]:
    # Stable source/position tie-breaks. Reviewed senses alone can establish
    # contrast. Surface similarity never creates or merges a sense.
    def identity(row):
        return row["source_id"], row["sentence_id"]

    remaining = sorted(rows, key=lambda row: (identity(row) != current, *identity(row), row["match_start"]))
    result, sources, senses = [], set(), set()
    while remaining and len(result) < limit:
        def rank(row):
            sense = row.get("reviewed_sense_id")
            return (identity(row) != current, not (sense and sense not in senses), row["source_id"] in sources)

        row = min(remaining, key=rank)
        unit = identity(row)
        remaining = [other for other in remaining if identity(other) != unit]
        result.append(row)
        sources.add(row["source_id"])
        if row.get("reviewed_sense_id"):
            senses.add(row["reviewed_sense_id"])
    return result


def query(repo: Path, state: Path, phrase: str, *, current: tuple[str, str] | None = None,
          limit: int = 2) -> dict[str, Any]:
    repo, state = repo.resolve(), state.resolve()
    from .sentence_visuals import (
        ERRORS, candidate_freshness, load_current, open_generation, read_guard, review_freshness, watched_inputs,
    )
    if not 2 <= limit <= 4:
        raise ValidationError("concordance limit must be two to four")
    _phrase(phrase)
    empty = {"schema_version": SCHEMA, "occurrences": [], "formal_write_count": 0, "model_requests": 0}
    try:
        start = read_guard(state)
        manifest = load_current(state)
        if manifest is None:
            return {**empty, "status": "pending", "freshness": "unresolved", "reason": "no_teaching_generation"}
        if manifest["support_index_sha256"] != start[1]:
            return {**empty, "status": "pending", "freshness": "stale", "reason": "support_generation_changed"}
        candidates, screened, checked = [], {}, {}
        capped = False
        with closing(open_generation(state, manifest)) as db:
            for row, match_start, terms in candidate_rows(db, phrase, current):
                if row is None:
                    capped = True
                    break
                match = occurrence(row, match_start, terms)
                if match is None:
                    continue
                unit = row["source_id"], row["sentence_id"]
                if unit not in screened:
                    screened[unit] = candidate_freshness(repo, state, row, checked=checked)
                if screened[unit]["freshness"] == "current":
                    if match.get("reviewed_sense_id") and review_freshness(state, row, checked)["freshness"] != "current":
                        for key in ("reviewed_sense_id", "sense_label", "sense_evidence_refs"):
                            match.pop(key, None)
                    candidates.append(match)
        if read_guard(state) != start or not watched_inputs(checked):
            return {**empty, "status": "pending", "freshness": "unresolved", "reason": "source_changed_during_read"}
        rows = select_rows(candidates, current, limit)
        stale = sum(proof["freshness"] == "stale" for proof in screened.values())
        unresolved = sum(proof["freshness"] == "unresolved" for proof in screened.values())
        freshness = "current" if rows or not (stale or unresolved) else "stale" if stale else "unresolved"
        return {**empty, "status": "ready" if len(rows) >= 2 else "insufficient",
                "freshness": freshness, "generation": manifest["generation"], "occurrences": rows,
                "screened_unavailable": stale + unresolved, "screened_stale": stale,
                "screened_unresolved": unresolved, "candidate_limit_reached": capped}
    except ERRORS:
        return {**empty, "status": "pending", "freshness": "unresolved", "reason": "generation_unverifiable"}
