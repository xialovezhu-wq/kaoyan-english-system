"""Read-only exam-frequency and dictionary support for sentence details.

This is objective reference data, independent of learner memory, grading and
formal vocabulary intake.  No lookup downloads data or repairs an index.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from .runtime_paths import REPO_ROOT


LEXICON_DB = Path("raw/reference_sources/exam_lexicon/current/lexicon.sqlite3")
DEFAULT_DB = REPO_ROOT / LEXICON_DB
SCHEMA = "english-exam-lexicon-v1"
LETTERS = "A-Za-zÀ-ÖØ-öø-ÿĀ-ž"
WORD = re.compile(r"(?<![" + LETTERS + r"0-9])(?:[A-Z][a-z]?\.)+[A-Z][a-z]?(?:\.|(?![" + LETTERS + r"]))|(?<![" + LETTERS + r"0-9])[" + LETTERS + r"]+(?:['’-][" + LETTERS + r"]+)*")
ACRONYMS = set("US USA UK UN EU AI DNA RNA GDP IT CEO BBC NHS FBI NASA WHO OECD IMF WTO IBM MIT MBA MPA GM GPS PC TV ID IQ SAT ACT FDA EPA FCC SEC PR HR R\u0026D".split())


def key(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("a vocabulary item must be 1–200 characters")
    value = unicodedata.normalize("NFKC", value).replace("’", "'").replace("‘", "'")
    value = value.replace("‐", "-").replace("‑", "-")
    value = " ".join(value.split())
    # Pronouns and all-caps acronyms have different meanings and counts.
    return " ".join(part if part in ACRONYMS or part.replace(".", "") in ACRONYMS else part.casefold() for part in value.split())


def contains_form(text: str, form: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(form) + r"(?!\w)", text, re.IGNORECASE))


def connect(db: Path):
    db = Path(db).resolve()
    if not db.is_file():
        raise FileNotFoundError("exam vocabulary index is unavailable")
    conn = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    version = conn.execute("SELECT value FROM meta WHERE name='schema'").fetchone()
    if not version or json.loads(version[0]) != SCHEMA:
        conn.close()
        raise ValueError("unsupported exam vocabulary index")
    return conn


def _counts(rows) -> dict:
    result = {"I": 0, "II": 0, "I_legacy": 0, "I_modern": 0, "I_with_legacy_reference": 0}
    for row in rows:
        if row["series"] == "I":
            result["I_legacy" if row["year"] < 2010 else "I_modern"] += row["n"]
            result["I_with_legacy_reference"] += row["n"]
            if row["year"] >= 2010:
                result["I"] += row["n"]
        else:
            result["II"] += row["n"]
    return result


def _word_counts(conn, item: str, mode: str, scope: str) -> dict:
    return _counts(conn.execute(
        "SELECT series,year,n FROM counts WHERE item=? AND mode=? AND scope=?", (item, mode, scope)))


def paper_counts(conn, item: str, mode: str = "lemma", scope: str = "main") -> list[dict]:
    """Use the lexicon's original word/phrase boundaries, retaining paper identity."""
    if " " not in item:
        return [dict(row) for row in conn.execute(
            "SELECT series,year,n FROM counts WHERE item=? AND mode=? AND scope=?", (item, mode, scope))]
    # FTS locates candidates; the final boundary-aware sequence counts repeats.
    # It does not join stems to options, paragraphs, blanks or punctuation.
    tokens = [key(t) for t in WORD.findall(item)]
    if mode == "lemma":
        tokens = [t.replace(".", "") if t.replace(".", "") in ACRONYMS else t for t in tokens]
    if len(tokens) < 2 or len(tokens) > 12:
        return []
    phrase = " ".join(tokens)
    if (conn.execute("SELECT 1 FROM sqlite_master WHERE name='phrase_cache'").fetchone()
            and conn.execute("SELECT 1 FROM phrase_cache WHERE item=?", (phrase,)).fetchone()):
        return [dict(row) for row in conn.execute(
            "SELECT series,year,n FROM counts WHERE item=? AND mode=? AND scope=?", (phrase, mode, scope))]
    search = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    rows = conn.execute("""SELECT u.surface_text,u.lemma_text,p.series,p.year
        FROM unit_fts f JOIN units u ON u.rowid=f.rowid JOIN papers p ON p.id=u.paper_id
        WHERE unit_fts MATCH ? AND u.scope=?""", (search, scope))
    found = []
    for row in rows:
        literal = row["surface_text"].split()
        lemmas = row["lemma_text"].split()
        count = 0
        for i in range(len(literal) - len(tokens) + 1):
            if all(literal[i + j] == token or (mode == "lemma" and lemmas[i + j] == token) for j, token in enumerate(tokens)):
                count += 1
        if count:
            found.append({"series": row["series"], "year": row["year"], "n": count})
    return found


def _phrase_counts(conn, item: str, mode: str, scope: str) -> dict:
    return _counts(paper_counts(conn, item, mode, scope))


def _surface_lemmas(conn, surface: str, scope: str) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT lemma FROM occurrences WHERE surface=? AND scope=? "
        "AND lemma_status!='ambiguous_literal' ORDER BY lemma", (surface, scope))]


def _lookup(conn, item: str, *, scope="main", mode="lemma", exam_examples=0) -> dict:
    if mode not in {"lemma", "surface"} or scope not in {"main", "directions"}:
        raise ValueError("unsupported frequency mode or scope")
    requested = key(item)
    normalized = requested
    form_candidates = []
    if " " not in requested and mode == "lemma":
        form_candidates = _surface_lemmas(conn, requested, scope)
        # An inflected lookup must not quietly return zero after its occurrences
        # were correctly grouped under a base form. Ambiguous roots stay explicit.
        if len(form_candidates) == 1:
            normalized = form_candidates[0]
    row = conn.execute("SELECT data FROM entries WHERE item=?", (normalized,)).fetchone()
    entry = json.loads(row[0]) if row else {"item": normalized, "dictionary_status": "not_found", "translations": [], "senses": []}
    is_phrase = " " in normalized
    literal = _phrase_counts(conn, requested, "surface", scope) if is_phrase else _word_counts(conn, requested, "surface", scope)
    effective_mode = mode
    if (mode == "lemma" and not is_phrase and any(literal.values())
            and (not form_candidates or (normalized == requested and requested not in form_candidates))):
        effective_mode = "surface"
    count = _phrase_counts(conn, normalized, effective_mode, scope) if is_phrase else _word_counts(conn, normalized, effective_mode, scope)
    forms = [] if is_phrase else [dict(r) for r in conn.execute(
        """SELECT o.surface,COUNT(*) AS n,SUM(p.series='I') AS I,SUM(p.series='II') AS II
        FROM occurrences o JOIN units u ON u.id=o.unit_id JOIN papers p ON p.id=u.paper_id
        WHERE o.lemma=? AND o.scope=? AND o.lemma_status!='ambiguous_literal' AND p.year>=2010 GROUP BY o.surface ORDER BY n DESC,o.surface""", (normalized, scope))]
    ambiguous = [] if is_phrase else [dict(r) for r in conn.execute(
        "SELECT surface,note,n FROM ambiguities WHERE candidate=? ORDER BY surface", (normalized,))]
    manifest = json.loads(conn.execute("SELECT value FROM meta WHERE name='manifest'").fetchone()[0])
    result = {**entry, "query": item, "item": normalized, "status": "ready", "count_mode": effective_mode,
              "scope": scope, "counts": count, "exact_counts": literal, "forms": forms,
              "requested_item": requested, "resolved_from_surface": normalized != requested, "form_candidates": form_candidates,
              "match_type": "contiguous_phrase" if is_phrase else "word",
              "ambiguous_forms": ambiguous, "coverage": manifest["coverage"],
              "corpus_version": manifest["corpus_version"], "counting_note": "词或词组的出现次数；不是本义项单独的考查次数。"}
    if exam_examples:
        # Only explicit callers may retrieve unrelated original passage text.
        result["exam_examples"] = []
        if not is_phrase:
            rows = conn.execute("""SELECT DISTINCT u.id,u.text,p.year,p.series FROM occurrences o
                JOIN units u ON u.id=o.unit_id JOIN papers p ON p.id=u.paper_id
                WHERE o.lemma=? AND o.scope='main' AND u.kind='passage'
                ORDER BY p.year DESC,u.id LIMIT ?""", (normalized, min(exam_examples, 5)))
            result["exam_examples"] = [dict(r) for r in rows]
    return result


def lookup_many(items, *, db=DEFAULT_DB, scope="main", mode="lemma", exam_examples=0) -> list[dict]:
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 12:
        raise ValueError("query one to twelve specific words or phrases")
    with closing(connect(Path(db))) as conn:
        return [_lookup(conn, value, scope=scope, mode=mode, exam_examples=exam_examples) for value in items]


def status(*, db=DEFAULT_DB) -> dict:
    with closing(connect(Path(db))) as conn:
        manifest = json.loads(conn.execute("SELECT value FROM meta WHERE name='manifest'").fetchone()[0])
        return {"status": "ready", "schema": SCHEMA, **manifest}


def vocabulary_key(note: dict) -> tuple[str, str, str]:
    if not isinstance(note, dict):
        raise ValueError("vocabulary target must be an object")
    mode = note.get("frequency_mode", "lemma")
    if mode not in {"lemma", "surface"}:
        raise ValueError("unsupported vocabulary frequency mode")
    return key(note.get("item")), key(note.get("source_form")), mode


class VocabularyIdentityError(ValueError):
    pass


def _vocabulary_reference(conn, item: str, form: str, mode: str) -> dict:
    words, spelling = item.split(), form.split()
    reason = None
    if len(words) != len(spelling):
        reason = "unverified_phrase_identity"
    else:
        for base, surface in zip(words, spelling):
            if base == surface:
                continue
            lemmas = _surface_lemmas(conn, surface, "main")
            if base in lemmas:
                continue
            if lemmas:
                raise VocabularyIdentityError(f"indexed source form {surface!r} does not match vocabulary item {base!r}")
            reason = "unverified_inflection"
    if reason:
        return {"status": "unavailable", "reason": reason, "counts": None, "translations": [], "senses": []}
    reference = _lookup(conn, item, mode=mode)
    reference["source_form_counts"] = (_phrase_counts(conn, form, "surface", "main")
                                       if " " in form else _word_counts(conn, form, "surface", "main"))
    return reference


def vocabulary_references(notes: list, *, db=DEFAULT_DB) -> dict[tuple[str, str, str], dict]:
    targets = list(dict.fromkeys(vocabulary_key(note) for note in notes))
    if not targets:
        return {}
    try:
        with closing(connect(Path(db))) as conn:
            return {target: _vocabulary_reference(conn, *target) for target in targets}
    except VocabularyIdentityError:
        raise
    except (OSError, sqlite3.DatabaseError, ValueError):
        return {target: {"status": "unavailable", "reason": "lexicon_unavailable",
                         "counts": None, "translations": [], "senses": []} for target in targets}


def enrich_vocabulary(notes: list, *, node_text: str, sentence: str | None = None, translation: str | None = None, db=DEFAULT_DB,
                      references: dict[tuple[str, str, str], dict] | None = None) -> list[dict]:
    """Attach computed counts and source dictionaries to teacher-authored meaning.

    The teacher supplies the current context and real translation evidence. The
    deterministic renderer never infers a learner error or a sense from counts.
    """
    allowed = {"item", "source_form", "context_meaning", "sense_cue", "user_translation", "issue", "evidence_kind", "meanings", "frequency_mode"}
    if not isinstance(notes, list) or len(notes) > 8:
        raise ValueError("vocabulary must be a list of up to eight actual target items")
    checked, seen = [], set()
    for note in notes:
        if isinstance(note, dict) and "reviewed_sense" in note:
            from .reviewed_senses import expand_sense
            note = expand_sense(note)
        if not isinstance(note, dict) or set(note) - allowed:
            raise ValueError("vocabulary accepts teaching content, not manually supplied counts or dictionary data")
        kind = note.get("evidence_kind", "current_translation")
        if kind not in {"current_translation", "demo", "language_reference"}:
            raise ValueError("unsupported vocabulary evidence kind")
        required = ["item", "source_form", "context_meaning"]
        if kind == "language_reference":
            if "issue" in note or "user_translation" in note:
                raise ValueError("a language reference must not assert a learner error")
        else:
            required.append("user_translation")
            if note.get("issue") not in {"sense", "phrase", "form", "untranslated", "uncertain"}:
                raise ValueError("unsupported vocabulary diagnosis")
        if kind == "language_reference" or "sense_cue" in note:
            required.append("sense_cue")
        for field in required:
            if not isinstance(note.get(field), str) or not note[field].strip():
                raise ValueError(f"vocabulary.{field} must contain the actual context/evidence")
        normalized = key(note["item"])
        if normalized in seen:
            raise ValueError("duplicate vocabulary target in one fragment")
        seen.add(normalized)
        if not contains_form(node_text, note["source_form"]):
            raise ValueError("vocabulary source_form must occur in its own sentence fragment")
        if note.get("frequency_mode", "lemma") not in {"lemma", "surface"}:
            raise ValueError("unsupported vocabulary frequency mode")
        meanings = note.get("meanings", [])
        if not isinstance(meanings, list) or (not meanings and not note.get("sense_cue")):
            raise ValueError("supply a sentence-specific clue or meanings with complete bilingual examples")
        expanded = []
        for meaning in meanings:
            if not isinstance(meaning, dict) or set(meaning) - {"pos", "meaning", "example", "translation", "source", "cue"}:
                raise ValueError("unsupported meaning/example fields")
            if meaning.get("source") == "current_sentence":
                meaning = {"example": sentence, "translation": translation, **meaning}
            if "cue" in meaning and (not isinstance(meaning["cue"], str) or not meaning["cue"].strip()):
                raise ValueError("a sense cue must be nonempty")
            for field in ("pos", "meaning", "example", "translation"):
                if not isinstance(meaning.get(field), str) or not meaning[field].strip():
                    raise ValueError(f"vocabulary.meanings.{field} must be nonempty")
            if meaning.get("source", "teaching_example") not in {"teaching_example", "current_sentence"} and not str(meaning["source"]).startswith("oewn-"):
                raise ValueError("examples must be teaching examples or exact sourced dictionary examples")
            expanded.append(meaning)
        checked.append({**note, "evidence_kind": kind, "meanings": expanded})
    if not checked:
        return []
    if references is None:
        references = vocabulary_references(checked, db=db)
    result = []
    for note in checked:
        target = vocabulary_key(note)
        if target not in references:
            raise ValueError("vocabulary selection is outside the prepared diagram context")
        reference = references[target]
        dictionary_examples = {sense["id"]: sense.get("examples", []) for sense in reference.get("senses", [])}
        for meaning in note["meanings"]:
            source = meaning.get("source", "teaching_example")
            if source == "current_sentence" and (sentence is None or meaning["example"] != sentence):
                raise ValueError("current-sentence example must quote the authorized sentence exactly")
            if source not in {"teaching_example", "current_sentence"} and meaning["example"] not in dictionary_examples.get(source, []):
                raise ValueError("dictionary example does not match its referenced sense")
        result.append({**note, "reference": reference})
    return result
