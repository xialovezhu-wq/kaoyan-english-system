"""Publish and read offline English sentence visuals and concordance together."""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from . import concordance, sentence_support
from .answer_safety import contains_answer_signal
from .errors import PipelineError, SourceHashMismatch, ValidationError
from .memory_lifecycle import read_lifecycle
from .reading_preparation import DEFAULT_OUTPUT, query as read_unit
from .sentence_evidence import DIGEST, package_root, withdrawn_packages
from .sentence_review import OFFSET_UNIT, SCHEMA, review_from_legacy, tokens
from .sources import resolve_article_source
from .util import (
    atomic_write_bytes, atomic_write_json, exclusive_lock, file_sha256, load_json,
    object_sha256, sentence_sha256, text_sha256, utc_now,
)

MANIFEST = "english_sentence_teaching_generation_v1"
RENDERER_VERSION = "sentence-teaching-html-v2"
ERRORS = (OSError, ValueError, KeyError, TypeError, PipelineError, sqlite3.Error)
VERSION_INPUTS = ("sentence_visuals.py", "sentence_review.py", "concordance.py")
WRITER_ID = re.compile(r"EN-RECEIPT-[A-F0-9]{16}\Z")
BATCH_ID = re.compile(r"EN-BATCH-(\d{4})(\d{2})(\d{2})-[A-F0-9]{12}\Z")
STYLE = """
ROOT{color-scheme:light dark;font:16px/1.55 "Hiragino Sans GB","PingFang SC",system-ui,sans-serif;
     color:light-dark(#202b30,#e4eae9);background:light-dark(#fff,#172022);max-width:52rem;
     margin:auto;padding:.85rem 1rem;overflow-wrap:anywhere}
ROOT p,ROOT figure{margin:0}ROOT .original{font:1.2rem/1.6 Georgia,serif;white-space:pre-wrap}
ROOT .translation{margin-top:.35rem}ROOT .cue{margin:.45rem 0;color:light-dark(#38534e,#bfd2c8)}
ROOT strong,ROOT mark{font-weight:750;color:light-dark(#154b41,#ade8d1);background:none;
     text-decoration:underline;text-decoration-color:light-dark(#b0cfc3,#466c5f);text-underline-offset:.16em}
ROOT summary{cursor:pointer;color:inherit}ROOT summary:focus-visible{outline:2px solid currentColor;outline-offset:3px}
ROOT .relation{margin:.3rem 0}ROOT .rel{font-size:.84em;color:light-dark(#4e605b,#bacac3)}
ROOT .more{margin-top:.55rem}ROOT .more>summary,ROOT .source{font-size:.87rem;color:light-dark(#4e605b,#bacac3)}
ROOT .occurrence{margin:.45rem 0;padding:.2rem 0 .55rem;border-bottom:1px solid light-dark(#d4ded9,#475951)}
ROOT .original-line{font:1.1rem/1.6 Georgia,serif;white-space:pre-wrap}
ROOT .source{display:block;margin-top:.18em}ROOT .occurrence:not([open]) .extended{display:none}
ROOT .occurrence[open] .cut{display:none}
"""


def _fragment(ident: str, body: str, script: str = "") -> bytes:
    return (f'<style>{STYLE.replace("ROOT", "#" + ident)}</style>'
            f'<section id="{ident}" lang="zh-CN">{body}</section>{script}\n').encode()


def _standalone(title: str, fragment: bytes) -> bytes:
    header = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
              '<meta name="viewport" content="width=device-width, initial-scale=1">'
              f'<title>{html.escape(title)}</title><style>body{{margin:0}}</style><body>')
    return header.encode() + fragment + b"</body></html>\n"


def _highlight(text: str, spans: list[dict[str, Any]]) -> str:
    parts, last = [], 0
    for span in sorted(spans, key=lambda item: (item["start"], item["end"])):
        if span["start"] < last:
            continue
        parts.extend((html.escape(text[last:span["start"]]), "<strong>",
                      html.escape(text[span["start"]:span["end"]]), "</strong>"))
        last = span["end"]
    parts.append(html.escape(text[last:]))
    return "".join(parts)


def render_sentence(data: dict[str, Any]) -> bytes:
    esc = html.escape
    spans = {span["id"]: span for span in data["spans"]}
    focus = [spans[key] for key in data["focus_span_ids"]]
    if not focus:
        first = data["arcs"][0]
        focus = [spans[first["head"]], spans[first["dependent"]]]
    original = _highlight(data["original_sentence"], focus)
    relations = []
    for arc in data["arcs"]:
        head, dependent = spans[arc["head"]], spans[arc["dependent"]]
        modifier = arc["relation"] in {"修饰", "时间", "条件", "目的", "方式", "指代"}
        origin, target = (dependent, head) if modifier else (head, dependent)
        left, right = sorted((origin, target), key=lambda span: span["start"])
        if arc["relation"] in {"并列", "对照"}:
            arrow = "↔"
        else:
            arrow = "→" if left is origin else "←"
        relations.append(f'<p class="relation"><span lang="en">{esc(left["text"])}</span> '
                         f'<span class="rel">{arrow} {esc(arc["relation"])}</span> '
                         f'<span lang="en">{esc(right["text"])}</span></p>')
    translated = data.get("translation", "")
    highlight = data.get("translation_focus")
    if highlight:
        start = translated.index(highlight)
        translated_html = _highlight(translated, [{"start": start, "end": start + len(highlight)}])
    else:
        translated_html = esc(translated)
    body = f'<p class="original" lang="en">{original}</p>'
    if translated_html:
        body += f'<p class="translation">{translated_html}</p>'
    cue = data.get("reading_cue")
    if cue and cue != translated:
        body += f'<p class="cue">{esc(cue)}</p>'
    # A legacy review without a translation still gives its first verified relation immediately.
    if not translated and not cue and relations:
        body += relations.pop(0)
    if data.get("first_break") and data["first_break"] not in (cue, translated):
        relations.append('<p class="cue">上次阅读：' + esc(data["first_break"]) + '</p>')
    if relations:
        body += '<details class="more"><summary>句子结构</summary>' + "".join(relations) + '</details>'
    return _fragment("en-sentence-" + data["support_record_sha256"][:16], body)


def _locator_label(locator: dict[str, Any]) -> str:
    ref = locator.get("reference_id") or ""
    exam = re.fullmatch(r"EXAM-READING-(\d{4})-T(\d+)", ref)
    if exam:
        ref = f"{exam[1]} 年 · Text {exam[2]}"
    elif ref == locator.get("source_id") or ref.startswith(("RAW-", "EN-PKG-")):
        ref = ""
    return " · ".join(v for v in (ref, locator.get("sentence_id")) if v)


def render_concordance(result: dict[str, Any]) -> bytes:
    esc = html.escape
    rows = []
    for item in result["occurrences"]:
        text = item["original_sentence"]
        start, end = item["match_start"], item["match_end"]
        prefix = text[:start - len(item["left"])]
        suffix = text[end + len(item["right"]):]
        def extension(part):
            if not part:
                return ""
            return '<span class="cut">…</span><span class="extended">'+esc(part)+'</span>'
        excerpt = (extension(prefix) + esc(item["left"]) + '<mark>' + esc(item["surface"]) +
                   '</mark>' + esc(item["right"]) + extension(suffix))
        line = f'<span class="original-line" lang="en">{excerpt}</span>'
        source = _locator_label(item["source_locator"])
        label = " · ".join(v for v in (item.get("sense_label"), source) if v)
        if label:
            line += f'<small class="source">{esc(label)}</small>'
        if prefix or suffix:
            rows.append(f'<details class="occurrence"><summary>{line}</summary></details>')
        else:
            rows.append(f'<div class="occurrence">{line}</div>')
    if result["status"] == "insufficient":
        rows.append('<p class="source">当前可核对的原句不足两句。</p>')
    ident = "en-kwic-" + object_sha256([r["occurrence_id"] for r in result["occurrences"]])[:16]
    return _fragment(ident, "".join(rows))


def root(state: Path) -> Path:
    return state.resolve() / "sentence-teaching"


def current_path(state: Path) -> Path:
    return root(state) / "current.json"


def _stat(path: Path) -> list[int]:
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def _schema_basis() -> dict[str, Any]:
    return {"renderer_version": RENDERER_VERSION, "normalization_version": concordance.NORMALIZATION_VERSION,
            "code": {name: file_sha256(Path(__file__).with_name(name)) for name in VERSION_INPUTS}}


def load_current(state: Path) -> dict[str, Any] | None:
    pointer = current_path(state)
    if not pointer.is_file():
        return None
    doc = load_json(pointer)
    if (doc.get("schema_version") != MANIFEST or not DIGEST.fullmatch(doc.get("generation", ""))
            or object_sha256({k: v for k, v in doc.items() if k != "manifest_sha256"}) != doc.get("manifest_sha256")):
        raise ValidationError("sentence-teaching current manifest is invalid")
    return doc


def _generation_path(state: Path, manifest: dict[str, Any]) -> Path:
    return root(state) / "generations" / (manifest["generation"] + ".sqlite3")


def open_generation(state: Path, manifest: dict[str, Any]) -> sqlite3.Connection:
    db_path = _generation_path(state, manifest)
    if db_path.is_symlink() or _stat(db_path) != manifest["database_identity"] or _schema_basis() != manifest["generator"]:
        raise ValidationError("sentence-teaching generation or renderer changed")
    return sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)


def read_guard(state: Path) -> tuple[Any, ...]:
    from .memory_query import _state_identity

    pointer = current_path(state)
    return (_stat(pointer) if pointer.is_file() else None,
            sentence_support.current_index_sha256(state), _state_identity(state))


def watched_inputs(checked: dict) -> bool:
    try:
        return all(_stat(path) == snapshot for path, snapshot in checked.get("watched", {}).items())
    except OSError:
        return False


def _file_dependency(repo: Path, path: Path, checked: dict) -> dict[str, Any]:
    watch = checked.setdefault("watched", {})
    snapshot = _stat(path)
    key = ("digest", path, tuple(snapshot))
    if key not in checked:
        checked[key] = file_sha256(path)
    if _stat(path) != snapshot:
        raise ValidationError("file changed during sentence source validation")
    watch[path] = snapshot
    return {"path": path.relative_to(repo).as_posix(), "sha256": checked[key]}


def _source(repo: Path, state: Path, record: dict[str, Any], checked: dict) -> tuple[dict, list[dict], list[dict]]:
    identity, episode = record["source_identity"], record["latest_episode"]
    sid, unit, sentence = (identity[k] for k in ("source_id", "sentence_id", "source_sentence"))
    if (record.get("answer_safe") is not True or identity.get("answer_exposure") != "answer_free"
            or identity.get("question_id") or not sentence or contains_answer_signal(sentence)
            or sentence_sha256(sentence) != identity["sentence_sha256"]):
        raise ValidationError("source is not an answer-free sentence")
    source_ref: dict[str, Any]
    if identity["source_kind"] == "article":
        key = ("prepared", sid)
        if key not in checked:
            prepared = read_unit(repo / DEFAULT_OUTPUT, source_id=sid)
            canonical = (repo / prepared["canonical_path"]).resolve(strict=True)
            if not canonical.is_relative_to(repo):
                raise ValidationError("canonical practice source escapes the English repository")
            raw = _file_dependency(repo, canonical, checked)
            if raw["sha256"] != prepared["practice_source_sha256"]:
                raise SourceHashMismatch("passage file differs from the indexed source")
            wrapper = (repo / prepared["source_article"]).resolve(strict=True)
            # The wrapper can contain study notes. Only the source binding is
            # hashed as a dependency. Guard against concurrent edits to the file.
            wrapper_stat = _stat(wrapper)
            article = resolve_article_source(repo, prepared["source_article"])
            checked["watched"][wrapper] = wrapper_stat
            if any(prepared[k] != article[k] for k in ("source_id", "source_hash", "source_article", "canonical_path")):
                raise SourceHashMismatch("prepared passage is outside the current source")
            checked[key] = (article, raw, {r["unit_id"]: r for r in prepared["units"]})
        article, raw, units = checked[key]
        selected = units.get(unit, {})
        if (article["source_hash"] != identity["source_hash"] or selected.get("kind") != "article"
                or selected.get("answer_exposure") != "answer_free" or selected.get("question_id")
                or selected.get("text") != sentence):
            raise SourceHashMismatch("sentence support differs from the current exact practice unit")
        locator = {"source_id": sid, "reference_id": article.get("reference_id"),
                   "sentence_id": unit, "paragraph_id": selected["paragraph_id"],
                   "path": raw["path"], "unit_locator": unit}
        source_ref = {"kind": "practice_sentence", **raw, "locator": unit,
                      "resolved_node_sha256": text_sha256(sentence)}
        dependencies = [{"kind": "canonical_source", **raw},
                        {"kind": "article_binding", "path": article["source_article"],
                         "sha256": object_sha256(article)}]
    elif identity["source_kind"] == "user_provided":
        path = package_root(repo, state, episode["package_id"], episode["package_sha256"])
        source_path = path / "source.json"
        source = load_json(source_path)["identity"]
        if (source.get("source_sentence") != sentence or source.get("question_id")
                or any(source.get(k) != identity.get(k) for k in
                       ("source_id", "sentence_id", "source_hash", "answer_exposure", "source_kind"))):
            raise SourceHashMismatch("capture no longer binds the reviewed sentence")
        digest = file_sha256(source_path)
        checked.setdefault("watched", {})[source_path] = _stat(source_path)
        locator = {"source_id": sid, "sentence_id": unit, "reference_id": sid,
                   "package_id": episode["package_id"], "unit_locator": "source.json#/identity/source_sentence"}
        source_ref = {"kind": "source_text", "package_id": episode["package_id"],
                      "package_sha256": episode["package_sha256"], "sha256": digest,
                      "pointer": locator["unit_locator"]}
        dependencies = [source_ref]
    else:
        raise ValidationError("unsupported teaching source category")
    return locator, dependencies, [source_ref]


def _support_binding(state: Path, record: dict[str, Any], checked: dict) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    latest = record["latest_episode"]
    date, rid = latest["study_date"], latest["writer_receipt_id"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not WRITER_ID.fullmatch(rid):
        raise ValidationError("support record has no exact writer identity")
    path = state / "receipts" / "nightly" / date / (rid + ".json")
    dependency = _file_dependency(state, path, checked)
    key = ("writer", dependency["sha256"])
    if key not in checked:
        checked[key] = load_json(path)
    writer = checked[key]
    sealed_packages = dict(zip(writer["package_ids"], writer["package_sha256s"], strict=True))
    if (sealed_packages.get(latest["package_id"]) != latest["package_sha256"]
            or dependency["sha256"] != latest["writer_receipt_sha256"] or writer.get("receipt_id") != rid
            or writer.get("mode") != "apply" or writer.get("status") not in {"APPLIED", "PARTIAL", "NO_ACTION"}
            or writer.get("package_dispositions", {}).get(latest["package_id"]) != "completed"):
        raise ValidationError("sentence support is outside its verified writer closure")
    return latest, [{"kind": "writer_receipt", **dependency}]


def _record_basis(repo, state, entry, record, review, checked, lifecycle):
    identity = record["source_identity"]
    pkg, writer_dependencies = _support_binding(state, record, checked)
    locator, dependencies, refs = _source(repo, state, record, checked)
    review_refs = (review or {}).get("evidence_refs", [])
    package_ids = {pkg["package_id"], *(ref["package_id"] for ref in review_refs)}
    if package_ids & withdrawn_packages(lifecycle):
        raise ValidationError("sentence evidence is withdrawn")
    review_dependency = (_file_dependency(state, _legacy_review_path(state, entry["record_sha256"]), checked)
                         if review and (review.get("reviewed_cue_sha256") or review.get("reviewed_evidence_sha256")) else None)
    return {
        "source_id": identity["source_id"], "source_hash": identity["source_hash"],
        "sentence_id": identity["sentence_id"], "sentence_sha256": identity["sentence_sha256"],
        "original_sentence": identity["source_sentence"], "support_record_sha256": entry["record_sha256"],
        "support_record_path": entry["record_path"], "visibility": "answer_free",
        "scope_bindings": [{"source_id": identity["source_id"], "sentence_id": identity["sentence_id"],
                            "source_hash": identity["source_hash"], "question_id": None}],
        "source_locator": locator, "source_evidence_refs": refs, "package_ids": sorted(package_ids),
        "dependencies": dependencies + writer_dependencies,
        "review_sha256": object_sha256(review) if review else None, "review_dependency": review_dependency,
    }


def _read_entry(state: Path, sid: str, unit: str) -> tuple[dict, dict]:
    index, digest = sentence_support._load_current_index(state)
    sentence_support._validate_closed_current_generation(state, index, digest)
    generations = sentence_support._resolve_generation_chain(state, index)
    pointer = sentence_support._load_sentence_pointer_from_chain(generations, sid, unit)
    if pointer is None:
        raise FileNotFoundError("no exact sentence support")
    entry = pointer["entry"]
    return entry, sentence_support._record_from_entry(state, entry)


def candidate_freshness(repo: Path, state: Path, row: dict[str, Any], *, checked: dict) -> dict[str, str]:
    try:
        lifecycle = checked.setdefault("lifecycle", read_lifecycle(state))
        if lifecycle.get("global_pending"):
            return {"freshness": "unresolved", "reason": "memory_update_pending"}
        if set(row["package_ids"]) & withdrawn_packages(lifecycle):
            return {"freshness": "stale", "reason": "withdrawn_evidence"}
        entry, record = _read_entry(state, row["source_id"], row["sentence_id"])
        if entry["record_sha256"] != row["support_record_sha256"]:
            return {"freshness": "stale", "reason": "sentence_support_changed"}
        _pkg, writer_deps = _support_binding(state, record, checked)
        locator, deps, _refs = _source(repo, state, record, checked)
        if locator != row["source_locator"] or deps + writer_deps != row["dependencies"]:
            return {"freshness": "stale", "reason": "file_dependency_changed"}
        return {"freshness": "current", "reason": "dependencies_verified"}
    except SourceHashMismatch:
        return {"freshness": "stale", "reason": "current_source_changed"}
    except ERRORS:
        return {"freshness": "unresolved", "reason": "source_dependency_unverifiable"}


def _active_entries(state: Path, index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    generators = sentence_support._resolve_generation_chain(state, index)
    tree_hashes = [row["generation_tree_sha256"] for row in index.get("generation_chain", [])]
    if generators and not tree_hashes:
        tree_hashes = [index["generation_tree_sha256"]]
    for gen, expected in zip(generators, tree_hashes, strict=True):
        if sentence_support._generation_tree_hash(gen) != expected:
            raise ValidationError("sentence-support generation tree has changed")
        for path in sorted((gen / "sentences").glob("*/*.json")):
            key = path.stem
            if key not in entries:
                pointer = load_json(path)
                exact = sentence_support._load_sentence_pointer(gen, pointer["source_id"], pointer["sentence_id"])
                if exact != pointer or key != pointer["lookup_sha256"]:
                    raise ValidationError("sentence-support inventory pointer is inconsistent")
                entries[key] = pointer["entry"]
    if len(entries) != index.get("entry_count"):
        raise ValidationError("sentence-support inventory count does not match its receipt")
    return entries


def _legacy_review_path(state: Path, record_digest: str) -> Path:
    if not DIGEST.fullmatch(record_digest):
        raise ValidationError("invalid sentence review support digest")
    directory = root(state) / "reviewed-backfill"
    revised = directory / "concise-reading" / (record_digest + ".json")
    return revised if revised.is_file() else directory / (record_digest + ".json")


def _review_for_record(state, entry, record, *, checked=None):
    review = record.get("display_review")
    if review is not None:
        return review
    path = _legacy_review_path(state, entry["record_sha256"])
    if not path.is_file():
        return None
    if path.is_symlink():
        raise ValidationError("sentence review backfill must be a regular evidence file")
    if checked is not None:
        _file_dependency(state, path, checked)
    return review_from_legacy(record, load_json(path))


def review_freshness(state: Path, row: dict[str, Any], checked: dict) -> dict[str, str]:
    dependency = row.get("review_dependency")
    if dependency is not None:
        path = _legacy_review_path(state, row["support_record_sha256"])
        try:
            current = _file_dependency(state, path, checked)
            if current != dependency or path.is_symlink():
                return {"freshness": "stale", "reason": "review_dependency_changed"}
        except ERRORS:
            return {"freshness": "unresolved", "reason": "review_dependency_unverifiable"}
    return {"freshness": "current", "reason": "dependencies_verified"}


def _asset_path(state: Path, digest: str) -> Path:
    if not DIGEST.fullmatch(digest):
        raise ValidationError("invalid sentence visual object identity")
    return root(state) / "objects" / (digest + ".html")


def _ready_asset(state, row):
    if row.get("visual_status") != "ready":
        return False
    try:
        path = _asset_path(state, row["html_sha256"])
        return not path.is_symlink() and file_sha256(path) == row["html_sha256"]
    except OSError:
        return False


def _publish_visual(state, basis, review):
    fields = {key: review[key] for key in
              ("spans", "arcs", "focus_span_ids", "translation", "translation_focus", "reading_cue",
               "first_break") if key in review}
    fields.setdefault("focus_span_ids", [])
    payload = {"schema_version": SCHEMA, **basis, "renderer_version": RENDERER_VERSION,
               "offset_unit": OFFSET_UNIT, "tokens": tokens(basis["original_sentence"]), **fields,
               "evidence_refs": review["evidence_refs"]}
    if contains_answer_signal(payload):
        raise ValidationError("reviewed display contains an answer signal")
    data = render_sentence(payload)
    digest = text_sha256(data.decode("utf-8"))
    path = _asset_path(state, digest)
    if not path.is_file() or file_sha256(path) != digest:
        atomic_write_bytes(path, data)
    if file_sha256(path) != digest:
        raise ValidationError("sentence visual file verification failed")
    return payload, digest


def rebuild(repo: Path, state: Path) -> dict[str, Any]:
    """Validate the current support snapshot and atomically publish both derived views."""
    repo, state = repo.resolve(), state.resolve()
    with exclusive_lock(state / "locks" / "sentence-teaching.lock"):
        initial = read_guard(state)
        index, index_sha = sentence_support._load_current_index(state)
        if index_sha is None:
            return {"status": "not_required", "reason": "no_sentence_support", "formal_write_count": 0}
        sentence_support._validate_closed_current_generation(state, index, index_sha)
        entries = _active_entries(state, index)
        generator, previous = _schema_basis(), None
        counts, issues, rows, checked = Counter(), [], {}, {}
        lifecycle = read_lifecycle(state)
        try:
            previous = load_current(state)
            if previous:
                with closing(open_generation(state, previous)) as db:
                    prior = {key: json.loads(value) for key, value in db.execute("SELECT sentence_key, data FROM sentences")}
                    old_words = {key: concordance.prior_words(db, key) for key in prior}
            else:
                prior, old_words = {}, {}
        except ERRORS:
            prior, old_words = {}, {}
        for key, entry in entries.items():
            identity = entry["source_identity"]
            sid, unit = identity["source_id"], identity["sentence_id"]
            short = {"source_id": sid, "sentence_id": unit, "support_record_sha256": entry["record_sha256"]}
            try:
                record = sentence_support._record_from_entry(state, entry)
                review = _review_for_record(state, entry, record, checked=checked)
                basis = _record_basis(repo, state, entry, record, review, checked, lifecycle)
            except ERRORS as exc:
                counts["source_unavailable"] += 1
                issues.append({**short, "freshness": "stale" if isinstance(exc, SourceHashMismatch) else "unresolved",
                               "reason": str(exc)})
                continue
            counts["source_current"] += 1
            old = prior.get(key, {})
            unchanged = old.get("basis") == object_sha256(basis)
            reviewed_occurrences = [{**item, "sense_evidence_refs": review["evidence_refs"]}
                                    for item in (review or {}).get("occurrences", [])]
            row = {**basis, "basis": object_sha256(basis), "reviewed_occurrences": reviewed_occurrences,
                   "visual_status": "missing_review"}
            if review and review.get("arcs"):
                if unchanged and _ready_asset(state, old):
                    row.update(visual_status="ready", visual=old["visual"], html_sha256=old["html_sha256"])
                    counts["visual_reused"] += 1
                else:
                    try:
                        visual, html_sha = _publish_visual(state, basis, review)
                        row.update(visual_status="ready", visual=visual, html_sha256=html_sha)
                        counts["visual_rebuilt" if key in prior else "visual_created"] += 1
                    except ERRORS as exc:
                        row["visual_status"] = "failed"
                        counts["visual_failed"] += 1
                        issues.append({**short, "freshness": "unresolved", "reason": "visual_render:" + type(exc).__name__})
            elif review and review.get("translation"):
                row["visual_status"] = "reviewed_text"
                row["reading_text"] = {k: review[k] for k in ("translation", "translation_focus", "reading_cue") if k in review}
                counts["text_sufficient"] += 1
            else:
                counts["visual_missing_review"] += 1
                issues.append({**short, "freshness": "unresolved", "reason": "no_reviewed_structure"})
            if unchanged:
                words = old_words[key]
                counts["concordance_reused"] += 1
            else:
                words = concordance.indexed_words(row["original_sentence"])
                counts["concordance_rebuilt" if key in prior else "concordance_created"] += 1
            counts["concordance_indexed_words"] += len(words)
            rows[key] = row, words
        if read_guard(state) != initial or index_sha != initial[1] or not watched_inputs(checked):
            raise ValidationError("sentence teaching inputs changed during build; keep the current generation")
        components = {
            "sentence_visual": {"status": "pending" if counts["visual_failed"] else "ready",
                                "ready": sum(row["visual_status"] == "ready" for row, _ in rows.values()),
                                "reviewed_text": counts["text_sufficient"],
                                "missing_review": counts["visual_missing_review"],
                                "failed": counts["visual_failed"]},
            "concordance": {"status": "ready", "sentences": len(rows), "indexed_words": counts["concordance_indexed_words"]},
        }
        support = index["last_refresh"]
        support_receipt = sentence_support.support_receipt_path(state, support["study_date"], support["batch_id"])
        support_binding = {key: support[key] for key in ("study_date", "batch_id", "writer_receipt_sha256")}
        support_binding["receipt_sha256"] = file_sha256(support_receipt)
        support_binding["writer_receipt_id"] = load_json(support_receipt)["writer_receipt_id"]
        generation = object_sha256([generator, index_sha, support_binding,
                                   {key: object_sha256(row) for key, (row, _) in rows.items()}, issues])
        target = {"generation": generation}
        db_path = _generation_path(state, target)
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=db_path.parent, suffix=".sqlite3")
        os.close(fd)
        temp_path = Path(temp)
        try:
            with closing(sqlite3.connect(temp_path)) as db:
                concordance.create_index(db)
                with db:
                    for key, (row, words) in sorted(rows.items()):
                        concordance.put_sentence(db, key, row, words)
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValidationError("sentence teaching index did not verify")
            db_digest = file_sha256(temp_path)
            if read_guard(state) != initial or not watched_inputs(checked) or generator != _schema_basis():
                raise ValidationError("sentence teaching dependencies changed before activation")
            # Preserve immutable old databases for rollback. An idempotent build
            # never changes the identity recorded by an existing pointer.
            if not db_path.exists():
                os.replace(temp_path, db_path)
            elif file_sha256(db_path) != db_digest:
                raise ValidationError("immutable sentence teaching generation differs from rebuild")
        finally:
            temp_path.unlink(missing_ok=True)
        manifest = {"schema_version": MANIFEST, **target, "generator": generator,
                    "database_identity": _stat(db_path), "database_sha256": db_digest,
                    "support_generation": index["generation_id"], "support_index_sha256": index_sha,
                    "support_receipt": support_binding, "components": components,
                    "issues_sha256": object_sha256(issues), "issue_count": len(issues),
                    "eligible_support_count": len(entries), "source_unavailable_count": counts["source_unavailable"],
                    "previous_generation": (previous.get("previous_generation") if previous.get("generation") == generation
                                            else previous.get("generation")) if previous else None,
                    "formal_write_count": 0, "model_requests": 0}
        manifest["manifest_sha256"] = object_sha256(manifest)
        if read_guard(state) != initial or not watched_inputs(checked):
            raise ValidationError("sentence teaching inputs changed during publication")
        issues_path = db_path.with_suffix(".issues.json")
        if not issues_path.is_file():
            atomic_write_json(issues_path, {"issues": issues})
        if object_sha256(load_json(issues_path).get("issues")) != manifest["issues_sha256"]:
            raise ValidationError("sentence teaching issue inventory did not verify")
        immutable_manifest = db_path.with_suffix(".manifest.json")
        if not immutable_manifest.exists():
            atomic_write_json(immutable_manifest, manifest)
        if previous != manifest:
            atomic_write_json(current_path(state), manifest)
        if load_current(state) != manifest:
            raise ValidationError("sentence teaching manifest did not read back")
        return {**manifest, "status": "pending" if counts["visual_failed"] else "ready",
                "counts": dict(counts), "issues": issues, "checked_at": utc_now()}


def _stage_receipts(state: Path, rid: str, result: dict[str, Any]) -> dict[str, Any]:
    for name in ("sentence_visual", "concordance"):
        component = result.get("components", {}).get(name, {"status": result["status"]})
        path = root(state) / "receipts" / rid / (name + ".json")
        status = {**component, "writer_receipt_id": rid, "generation": result.get("generation"),
                  "support_receipt": result.get("support_receipt"), "reason": result.get("reason"),
                  "issue_count": result.get("issue_count"), "issues_sha256": result.get("issues_sha256"),
                  "basis": result.get("recovery_basis"), "formal_write_count": 0}
        if not path.is_file() or load_json(path) != status:
            atomic_write_json(path, status)
    return result


def writer_receipt_day(receipt: dict[str, Any]) -> str:
    rid, batch = receipt.get("receipt_id"), receipt.get("batch_id")
    match = BATCH_ID.fullmatch(batch) if isinstance(batch, str) else None
    if not isinstance(rid, str) or not WRITER_ID.fullmatch(rid) or match is None:
        raise ValidationError("invalid canonical writer or batch identity")
    return "-".join(match.groups())


def refresh_after_support(repo: Path, state: Path, writer_receipt: dict[str, Any], *,
                          force_rebuild: bool = False) -> dict[str, Any]:
    """Resume a verified writer's local display stage without calling apply."""
    try:
        day = writer_receipt_day(writer_receipt)
    except ValidationError:
        return {"status": "not_required", "formal_write_count": 0}
    state = state.resolve()
    rid, batch = writer_receipt["receipt_id"], writer_receipt["batch_id"]
    receipt = sentence_support.support_receipt_path(state, day, batch)
    if not receipt.is_file():
        status = "waiting_for_support" if writer_receipt.get("support_preflight_receipt_id") else "not_required"
        return {"status": status, "formal_write_count": 0}
    basis = None
    try:
        support = load_json(receipt)
        writer_path = state / "receipts" / "nightly" / day / (rid + ".json")
        if (support["writer_receipt_id"] != rid or support["writer_receipt_sha256"] != file_sha256(writer_path)
                or writer_receipt != load_json(writer_path) or support["status"] != "PASS"
                or support["receipt_id"] != "EN-SUPPORT-" + object_sha256(sentence_support._receipt_core(support))[:16].upper()):
            raise ValidationError("English display refresh does not bind the exact writer and support receipts")
        basis = {"batch_id": batch, "writer_receipt_sha256": support["writer_receipt_sha256"],
                 "support_receipt_sha256": file_sha256(receipt)}
        result = None
        if not force_rebuild:
            try:
                current = load_current(state)
                if (current and current["support_index_sha256"] == sentence_support.current_index_sha256(state)
                        and current["generator"] == _schema_basis()
                        and all(row["status"] == "ready" for row in current["components"].values())):
                    with closing(open_generation(state, current)):
                        result = {**current, "status": "ready"}
            except ERRORS:
                pass
        if result is None:
            result = rebuild(repo, state)
    except ERRORS as exc:
        result = {"status": "pending", "reason": type(exc).__name__, "formal_write_count": 0}
    try:
        return _stage_receipts(state, rid, {**result, "recovery_basis": basis})
    except ERRORS as exc:
        return {**result, "status": "pending", "reason": "derived_receipt:" + type(exc).__name__}


def stage_status(state: Path, rid: str) -> dict[str, Any]:
    if not WRITER_ID.fullmatch(rid):
        return {}
    out = {}
    for component in ("sentence_visual", "concordance"):
        path = root(state) / "receipts" / rid / (component + ".json")
        if path.is_file():
            try:
                row = load_json(path)
                if row.get("writer_receipt_id") != rid:
                    raise ValueError("derived receipt mismatch")
                out[component] = row
            except ERRORS:
                out[component] = {"status": "pending", "reason": "unverifiable_stage_receipt"}
    return out


def query_sentence(repo: Path, state: Path, sid: str, unit: str, *,
                   source_hash: str | None = None, sentence_hash: str | None = None,
                   include_evidence: bool = False, max_bytes: int = 4096) -> dict[str, Any]:
    repo, state = repo.resolve(), state.resolve()
    base = {"source_id": sid, "sentence_id": unit, "formal_write_count": 0, "model_requests": 0}
    try:
        guard = read_guard(state)
        entry, record = _read_entry(state, sid, unit)
        identity = record["source_identity"]
        if (source_hash and identity["source_hash"] != source_hash
                or sentence_hash and identity["sentence_sha256"] != sentence_hash):
            return {**base, "status": "pending", "freshness": "stale", "reason": "requested_source_mismatch"}
        checked = {}
        if read_lifecycle(state).get("global_pending"):
            return {**base, "status": "pending", "freshness": "unresolved", "reason": "memory_update_pending"}
        latest, _deps = _support_binding(state, record, checked)
        if latest["package_id"] in withdrawn_packages(read_lifecycle(state)):
            return {**base, "status": "pending", "freshness": "stale", "reason": "withdrawn_evidence"}
        locator, _deps, source_refs = _source(repo, state, record, checked)
        base |= {k: identity[k] for k in ("source_hash", "sentence_sha256")}
        base |= {"support_record_sha256": entry["record_sha256"], "source_locator": locator,
                 "original_sentence": identity["source_sentence"]}
        result = {**base, "status": "text_fallback", "freshness": "unresolved", "reason": "no_reviewed_structure"}
        try:
            manifest = load_current(state)
            if manifest is not None:
                with closing(open_generation(state, manifest)) as db:
                    found = db.execute("SELECT data FROM sentences WHERE source_id=? AND sentence_id=?", (sid, unit)).fetchone()
                if found:
                    row = json.loads(found[0])
                    proof = candidate_freshness(repo, state, row, checked=checked)
                    if proof["freshness"] == "current":
                        proof = review_freshness(state, row, checked)
                    result.update(proof, generation=manifest["generation"])
                    if proof["freshness"] == "current":
                        if _ready_asset(state, row):
                            result.update(status="ready", reason="reviewed_structure", visual=row["visual"],
                                          html_sha256=row["html_sha256"])
                        elif row["visual_status"] == "reviewed_text":
                            result.update(reason="reviewed_text_sufficient", **row["reading_text"])
                        else:
                            result.update(reason="no_reviewed_structure" if row["visual_status"] == "missing_review"
                                          else "visual_asset_unavailable")
        except ERRORS:
            result.update(reason="generation_unverifiable", freshness="unresolved")
        if include_evidence:
            from .sentence_evidence import attempt_bundle
            review = _review_for_record(state, entry, record, checked=checked) or {}
            bundle = attempt_bundle(repo, state, identity, latest["evidence_refs"],
                                    roles=review.get("evidence_roles"), max_bytes=max_bytes)
            if not bundle["original_sentence"] and bundle["coverage"]["original_sentence"] == "unknown":
                from .util import canonical_bytes
                with_source = {**bundle, "original_sentence": base["original_sentence"],
                               "coverage": {**bundle["coverage"], "original_sentence": "verified"},
                               "evidence_refs": [*bundle["evidence_refs"], *source_refs]}
                if len(canonical_bytes(with_source)) <= max_bytes:
                    bundle = with_source
            result["evidence_bundle"] = bundle
        if read_guard(state) != guard or not watched_inputs(checked):
            return {"source_id": sid, "sentence_id": unit, "status": "pending", "freshness": "unresolved",
                    "reason": "inputs_changed_during_read", "formal_write_count": 0, "model_requests": 0}
        return result
    except FileNotFoundError:
        return {**base, "status": "missing", "freshness": "unresolved", "reason": "exact_source_or_support_missing"}
    except SourceHashMismatch:
        return {**base, "status": "pending", "freshness": "stale", "reason": "current_source_changed"}
    except ERRORS:
        return {**base, "status": "pending", "freshness": "unresolved", "reason": "source_dependency_unverifiable"}


def export_result(state: Path, result: dict[str, Any], output: Path, inline_root: Path | None, *,
                  lesson_page: bool = False) -> dict[str, Any]:
    from .lesson_display import check_lesson_output, display_targets
    check_lesson_output(output)
    if output.suffix != ".html" or not output.is_absolute() or output.is_symlink():
        raise ValidationError("sentence teaching output must be an absolute regular HTML path")
    if inline_root is not None and (not inline_root.is_absolute() or not output.resolve().is_relative_to(inline_root.resolve())):
        raise ValidationError("sentence teaching output escapes its visualization directory")
    is_concordance = result.get("schema_version") == concordance.SCHEMA
    title = "原句对照" if is_concordance else "句子结构"
    if is_concordance:
        if result["status"] not in {"ready", "insufficient"}:
            return result
        data = render_concordance(result)
    else:
        if result["status"] != "ready":
            return result
        data = _asset_path(state, result["html_sha256"]).read_bytes()
        if text_sha256(data.decode("utf-8")) != result["html_sha256"]:
            raise ValidationError("sentence visual changed before export")
    atomic_write_bytes(output, data if inline_root is not None else _standalone(title, data))
    response = {**result, "output_path": str(output)}
    if inline_root is not None:
        response.update(display_targets(output, title=title, fragment=data,
                                        preview_root=inline_root if lesson_page else None))
    return response
