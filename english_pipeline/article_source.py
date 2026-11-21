"""Exact, practice-safe material for the local article projection."""
from __future__ import annotations

import re
from pathlib import Path

from . import reading_preparation as reading
from .answer_safety import contains_answer_signal
from .errors import ValidationError
from .expression_comparison import _excerpt
from .util import atomic_write_json, load_json, object_sha256, sentence_sha256, text_sha256

KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}")
UNIT = re.compile(r"(?:S[0-9]{2,}|Q[1-9][0-9]*)")


def checked_key(value):
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise ValidationError("invalid_entity_key")
    return value


def supplied_material(root, messages, session, reference):
    """A temporary identity follows its original message, never a fabricated catalog ID."""
    quote, _ = _excerpt(messages, reference, "user")
    if contains_answer_signal(quote):
        raise ValidationError("article_original_must_be_answer_free")
    key = "TEMP-" + object_sha256([session, reference["message_id"], text_sha256(quote)])[:32]
    path = root / "materials" / (key + ".json")
    doc = {"source_id": key, "source_hash": text_sha256(quote), "original": quote,
           "origin": {"session": session, "message_id": reference["message_id"]}}
    if path.exists() and load_json(path) != doc:
        raise ValidationError("temporary_material_changed")
    if not path.exists():
        atomic_write_json(path, doc)
    return key


def read_article(repo: Path, root: Path, key: str):
    checked_key(key)
    if key.startswith("TEMP-"):
        doc = load_json(root / "materials" / (key + ".json"))
        quote, origin = doc["original"], doc["origin"]
        if (doc["source_hash"] != text_sha256(quote) or doc["source_id"] != key or
                key != "TEMP-" + object_sha256([origin["session"], origin["message_id"], text_sha256(quote)])[:32]):
            raise ValidationError("temporary_material_changed")
        units = []
        for n, paragraph in enumerate(re.split(r"\n\s*\n", quote.strip()), 1):
            for sentence in reading._sentences(paragraph.strip()):
                units.append(dict(unit_id=f"S{len(units)+1:02d}", paragraph_id=f"P{n:02d}",
                                  kind="article", text=sentence, text_sha256=text_sha256(sentence)))
        rec = {**doc, "units": units, "questions": []}
        rec["record_sha256"] = object_sha256(rec)
    else:
        rec = reading.query(repo / reading.DEFAULT_OUTPUT, repo_root=repo, source_id=key)
    binding = {k: rec[k] for k in ("source_id", "source_hash", "record_sha256")}
    sections = {}
    for row in rec["units"]:
        if row["kind"] == "article":
            if row["text_sha256"] != text_sha256(row["text"]):
                raise ValidationError("sentence_source_changed")
            sections[row["unit_id"]] = {k: row[k] for k in ("paragraph_id", "text", "text_sha256")}
            sections[row["unit_id"]]["sentence_sha256"] = sentence_sha256(row["text"])
    for question in rec["questions"]:
        sections[question["question_id"]] = {"tabs": {
            u["unit_id"].split("-", 1)[1]: u["text"] for u in question["units"]}}
    if not sections or not all(UNIT.fullmatch(uid) for uid in sections):
        raise ValidationError("invalid_article_structure")
    return dict(binding=binding, source_version=object_sha256(binding), sections=sections)


def originals(article, allowed, active):
    if allowed == "article":
        result = [uid for uid in article["sections"] if uid.startswith("S")]
    else:
        if not isinstance(allowed, list):
            raise ValidationError("explicit_original_scope_required")
        result = list(allowed)
    if active not in article["sections"] or any(uid not in article["sections"] for uid in result):
        raise ValidationError("unknown_unit")
    return list(dict.fromkeys([*result, active]))


def offered_article(text, root, key):
    """Recognize only the exact article path or a chat link minted for this source."""
    if re.search(r"http://127\.0\.0\.1:\d+/english/a/" + re.escape(key) + r"(?![A-Za-z0-9_/-])", text):
        return True
    issued = root / "links" / (checked_key(key)+".json")
    if not issued.is_file():
        return False
    return any(re.search(re.escape(link)+r"(?![A-Za-z0-9_/-])",text)
               for link in load_json(issued)["chat_links"])
