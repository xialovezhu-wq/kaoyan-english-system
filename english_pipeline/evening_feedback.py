"""Prepare and publish a day of local English feedback from committed learning units."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from . import reading_preparation
from .answer_safety import contains_answer_signal
from .errors import ValidationError
from .exam_lexicon import LEXICON_DB, WORD, connect, key, paper_counts
from .memory_lifecycle import read_lifecycle
from .packages import frozen_package_overrides, validate_canonical_writer_closeout
from .publication import local_quality_pending
from .sentence_evidence import package_root, withdrawn_packages
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    exclusive_lock,
    file_sha256,
    learner_message_spans,
    load_json,
    object_sha256,
    text_sha256,
)

SCHEMA = "english_evening_feedback_v1"
MAIL = re.compile(
    r"(?:第\s*\d+\s*题|(?:正确|标准)答案|正确选项|选项\s*[A-D]|\bQ[1-9]\d\b|^\s*\[[A-D]\])", re.M | re.I
)
FUNCTION_WORDS = frozenset(
    "the a an be being been am is are was were have has had do does did of to at for on in from by with as and or if but so than that which what who whose how when this these those it its we you he she they i us our their them there here his her will would should may might can could shall must not very more most much many some such any other all both each one two also out about".split()
)


def valid_date(day):
    if date.fromisoformat(day).isoformat() != day:
        raise ValidationError("expected an ISO study date")
    return day


def public_text(value, *, maximum=2500):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError("feedback requires bounded, nonempty text")
    if contains_answer_signal(value) or MAIL.search(value):
        raise ValidationError("protected question content in public feedback")
    return value.strip()


def collect_day(repo, state, day):
    """Resolve every successful batch of the original day. Dry runs never contribute."""
    valid_date(day)
    packages, receipts, pending = {}, [], {}
    for path in sorted((state / "receipts/nightly" / day).glob("*.json")):
        r = load_json(path)
        if r.get("mode") != "apply" or r.get("status") not in {"APPLIED", "PARTIAL", "NO_ACTION"}:
            continue
        batch = str(r.get("batch_id", ""))
        if not re.fullmatch(r"EN-BATCH-\d{8}-[0-9A-F]{12}", batch):
            raise ValidationError("invalid writer batch")
        mp = state / "nightly" / day / (batch + ".manifest.json")
        overrides = frozen_package_overrides(state, repo, load_json(mp))
        r, m = validate_canonical_writer_closeout(
            state, path, expected_manifest_path=mp, package_root_overrides=overrides
        )
        if m["study_date"] != day:
            raise ValidationError("writer receipt study date mismatch")
        quality = local_quality_pending(state, path)
        receipts.append(r["receipt_id"])
        for pid, digest in zip(r["package_ids"], r["package_sha256s"], strict=True):
            disposition = r.get("package_dispositions", {}).get(pid, "incomplete")
            if quality or disposition != "completed":
                pending[pid] = quality or disposition
                continue
            row = m["source_snapshot"][pid]
            unit = row.get("unit_id") or row.get("sentence_id") or row.get("reference_id") or pid
            record = dict(
                package_id=pid,
                sha256=digest,
                source_id=row.get("source_id") or row.get("reference_id") or pid,
                source_hash=row.get("source_hash"),
                unit_id=unit,
                sentence_id=row.get("sentence_id"),
                paragraph_id=row.get("paragraph_id"),
                question_id=row.get("question_id"),
                source_kind=row.get("source_kind"),
                answer_exposure=row.get("answer_exposure"),
            )
            if pid in packages and packages[pid] != record:
                raise ValidationError("conflicting frozen package identity")
            packages[pid] = record
    for manifest in sorted((state / "packages" / day).glob("*/manifest.json")):
        pending.setdefault(manifest.parent.name, "awaiting_formal_intake")
    lifecycle = read_lifecycle(state)
    withdrawn = withdrawn_packages(lifecycle)
    for pid in set(packages) & withdrawn:
        packages[pid]["withdrawn_episodes"] = sorted(
            eid for eid in lifecycle["invalidated_episode_ids"] if eid.split(":",1)[0] == pid
        )
    if lifecycle.get("global_pending"):
        raise ValidationError("teaching memory is under revision")
    eligible = list(packages.values())
    payload = dict(
        study_date=day,
        receipts=sorted(set(receipts)),
        packages=sorted(eligible, key=lambda x: x["package_id"]),
        pending={k: v for k, v in sorted(pending.items()) if k not in packages},
    )
    payload["scope_sha256"] = object_sha256(payload)
    return payload


def sources_for_day(repo, state, scope):
    groups = {}
    records = {}
    for p in scope["packages"]:
        sid = p["source_id"]
        if sid not in records:
            prepared = state / "reading-preparation" / "articles" / (sid + ".json")
            if prepared.parent != state / "reading-preparation/articles":
                raise ValidationError("invalid source identity")
            records[sid] = (
                reading_preparation.query(state / "reading-preparation", source_id=sid, repo_root=repo)
                if prepared.exists() or re.fullmatch(r"EXAM-READING-\d{4}-T\d+", sid)
                else None
            )
        r = records[sid]
        if r:
            if p["source_hash"] and r["source_hash"] != p["source_hash"]:
                raise ValidationError("reading source has changed")
            name = r.get("reference_id") or sid
            identity = (r["source_id"], r["source_hash"], "article")
            whole = p["unit_id"] == r.get("reference_id")
            units = {
                u["unit_id"]: u["text"]
                for u in r["units"]
                if u.get("kind") == "article"
                and u.get("answer_exposure") == "answer_free"
                and not p.get("question_id")
                and (
                    whole
                    or p["unit_id"] in (u["unit_id"], u.get("paragraph_id"))
                    or p["sentence_id"] == u["unit_id"]
                )
            }
        else:
            name, units = sid, {}
            identity = (sid, p["source_hash"], p["unit_id"])
        gid = object_sha256(identity)[:24]
        group = groups.setdefault(
            gid, dict(id=gid, source_id=sid, name=name, units={}, packages=[], prepared_article=bool(r))
        )
        group["units"].update(units)
        group["packages"].append(p)
    return groups


def counts_for(conn, item):
    rows = paper_counts(conn, key(item))
    result = {}
    for series in ("I", "II"):
        modern = [r for r in rows if r["series"] == series and 2010 <= r["year"] <= 2026 and r["n"]]
        result[series] = {"papers": len({r["year"] for r in modern}), "hits": sum(r["n"] for r in modern)}
    return result


def writing_expressions(repo, *, items, theme=None, function=None, limit=8):
    """Rank relevant candidates from the user's reviewed writing materials and exact requested terms."""
    if not 1 <= limit <= 30 or len(items) > 30 or not (items or theme or function):
        raise ValidationError(
            "provide a writing theme, paragraph function or up to 30 candidate expressions"
        )
    root = repo / "raw/writing_reference/reviewed"

    def approved(name):
        with (root / name).open(encoding="utf-8") as handle:
            return [
                r
                for line in handle
                if line.strip() and (r := json.loads(line)).get("status") in {"approved", "corrected"}
            ]

    def origin(row):
        return {k: row[k] for k in ("approved_id", "source_id", "source_page", "usage_constraints")}

    requested = {key(item): [] for item in items}
    for row in approved("approved_vocabulary.jsonl"):
        item = key(row["normalized"])
        if item in requested or (theme and theme in row["theme"]):
            requested.setdefault(item, []).append(origin(row))
    patterns = [
        dict(template=row["template"], category=row["category"], **origin(row))
        for row in approved("approved_patterns.jsonl")
        if function and function in row["category"]
    ]
    with closing(connect(repo / LEXICON_DB)) as conn:
        words = [
            dict(item=k, counts=counts_for(conn, k), sources=sources) for k, sources in requested.items()
        ]
    words.sort(key=lambda v: (-v["counts"]["I"]["papers"], -v["counts"]["I"]["hits"], v["item"]))
    return dict(
        scope="2010–2026 英语一、二正式卷，各 17 套；词条全部语境",
        expressions=words[:limit],
        patterns=patterns[:limit],
        formal_write_count=0,
    )


def writing_prompt(repo, year, part, paper):
    """Open a single requested task. Illustrated Part B uses the original question image."""
    course = repo / "learning/writing-course-2027"
    if part == "B":
        matches = [
            row
            for row in json.loads((course / "questions.json").read_text(encoding="utf-8"))
            if row.get("year") == year and row.get("exam") == paper and row.get("part") == "B"
        ]
        if len(matches) != 1:
            raise ValidationError("no unique original essay image at that year and paper")
        image = (course / matches[0]["image"]).resolve(strict=True)
        if not image.is_relative_to((course / "questions").resolve()):
            raise ValidationError("essay question image must belong to the course")
        return dict(
            question_id=matches[0]["id"],
            mode="original_image",
            image=str(image),
            source_page=matches[0]["source_pdf_page"],
            formal_write_count=0,
        )
    with closing(connect(repo / LEXICON_DB)) as conn:
        rows = conn.execute(
            "SELECT id,text FROM units WHERE paper_id=? AND kind='writing_prompt' AND id LIKE '%-part-a-%' ORDER BY id",
            (f"{year}-{paper}",),
        ).fetchall()
    if len(rows) != 1 or not rows[0]["text"].strip():
        raise ValidationError(
            "no unique printed Part A prompt at that year and paper; use the named original page"
        )
    return dict(
        question_id=f"E{paper}-{year}-A",
        mode="printed_directions",
        locator=rows[0]["id"],
        directions=rows[0]["text"],
        formal_write_count=0,
    )


def grammar_for(repo, conn, sentences):
    """Only the audited surface forms in the checked Reading A corpus get paper totals."""
    path = repo / "assets/english/review-grammar.json"
    if not path.is_file():
        return {}
    catalog = load_json(path)
    fingerprint = catalog.pop("sha256")
    version = json.loads(conn.execute("SELECT value FROM meta WHERE name='manifest'").fetchone()[0])[
        "corpus_version"
    ]
    if object_sha256(catalog) != fingerprint or catalog["corpus_version"] != version:
        return {}
    normalize = lambda s: " ".join(WORD.findall(s.casefold().replace("’", "'")))
    indexed = {normalize(s): u for u, s in sentences.items()}
    matches = {}
    for key_name, pattern in catalog["patterns"].items():
        hits = pattern["hits"]
        unit_ids = sorted(
            {indexed[normalize(row["sentence"])] for row in hits if normalize(row["sentence"]) in indexed}
        )
        if not unit_ids:
            continue
        counts = {}
        for series in ("I", "II"):
            papers = [
                row["locator"].split(":")[0]
                for row in hits
                if row["locator"].split(":")[0].endswith("-" + series)
            ]
            counts[series] = dict(papers=len(set(papers)), hits=len(papers))
        matches[key_name] = dict(
            title=pattern["title"], scope=catalog["scope"], units=unit_ids, counts=counts
        )
    return matches


def rank_candidates(conn, sentences):
    # Recover the contextual lemma in known paper paragraphs where spelling alone is ambiguous.
    phrases = {r[0] for r in conn.execute("SELECT item FROM phrase_cache")}
    candidates = {}
    word_lemmas = {}
    longest_phrase = max((len(s.split()) for s in phrases), default=0)
    for unit, sentence in sentences.items():
        tokens = [key(t) for t in WORD.findall(sentence)]
        for form in set(tokens):
            if len(form) < 3 or form in FUNCTION_WORDS:
                continue
            if form not in word_lemmas:
                lemmas = [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT lemma FROM occurrences WHERE surface=? AND lemma_status!='ambiguous_literal'",
                        (form,),
                    )
                ]
                word_lemmas[form] = lemmas[0] if len(lemmas) == 1 else form
            item = word_lemmas[form]
            if item not in FUNCTION_WORDS:
                candidates.setdefault(item, (unit, form, "word"))
        for start in range(len(tokens)):
            for length in range(2, min(longest_phrase, len(tokens) - start) + 1):
                phrase = " ".join(tokens[start : start + length])
                if phrase in phrases and phrase.casefold() in sentence.casefold():
                    candidates.setdefault(phrase, (unit, phrase, "phrase"))
    ranked = []
    for item, (unit, form, kind) in candidates.items():
        counts = counts_for(conn, item)
        if counts["I"]["hits"] or counts["II"]["hits"]:
            ranked.append(dict(item=item, form=form, unit=unit, kind=kind, counts=counts))
    ranked.sort(key=lambda x: (-x["counts"]["I"]["papers"], -x["counts"]["I"]["hits"], x["item"]))
    return {kind: [r for r in ranked if r["kind"] == kind][:14] for kind in ("word", "phrase")}


def article_groups(repo, state, day, *, candidates=False):
    scope = collect_day(repo, state, day)
    groups = sources_for_day(repo, state, scope)
    if candidates and groups:
        with closing(connect(repo / LEXICON_DB)) as conn:
            for group in groups.values():
                if group["units"]:
                    group["candidates"] = rank_candidates(conn, group["units"])
                    group["grammar"] = grammar_for(repo, conn, group["units"])
    return scope, groups


def _quote(messages, ref, sentence, *, writing=False, bound_sentence=False):
    pos = ref.get("message")
    if type(pos) is not int or not 0 <= pos < len(messages):
        raise ValidationError("feedback quote has no exact message")
    msg = messages[pos]
    quote = public_text(ref.get("quote"), maximum=8000 if writing else 650)
    body = msg.get("content", "")
    if msg.get("role") != "user" or not isinstance(body, str):
        raise ValidationError("feedback observation must cite the learner")
    if not writing and not bound_sentence and sentence and sentence.casefold() not in body.casefold():
        prompt = ref.get("prompt_message")
        if (
            type(prompt) is not int
            or not 0 <= prompt < pos
            or sentence.casefold() not in str(messages[prompt].get("content", "")).casefold()
        ):
            raise ValidationError("bind the exact source prompt or a frozen single-sentence unit")
        if any(m.get("role") == "user" for m in messages[prompt + 1 : pos]):
            raise ValidationError("ambiguous answer after an intervening user turn")
    if not any(quote in part for part in learner_message_spans(msg, sentence or "")):
        raise ValidationError("the cited words are not a learner-authored span")
    return quote


def _check_semantics(repo, state, group, entry, conn, roots):
    if not isinstance(entry, dict):
        raise ValidationError("each feedback group must be an object")
    parts = group["packages"]
    by_pid = {p["package_id"]: p for p in parts}

    def original(ref):
        p = by_pid.get(ref.get("package_id"))
        if not p:
            raise ValidationError("source from a different learning unit")
        if p.get("withdrawn_episodes"):
            raise ValidationError("personal evidence for this package needs renewed scope review")
        pid = p["package_id"]
        if pid not in roots:
            root = package_root(repo, state, pid, p["sha256"])
            roots[pid] = (
                load_json(root / "conversation.json")["messages"],
                load_json(root / "source.json")["identity"],
            )
        return roots[pid]

    title = public_text(entry.get("title"), maximum=90)
    mode = entry.get("mode")
    if mode not in {"reading", "writing", "language", "protected"}:
        raise ValidationError("unknown daily learning unit type")
    result = dict(id=group["id"], title=title, mode=mode, cards=[], terms=[], patterns=[], writing=[])
    if mode == "protected":
        if group["units"]:
            raise ValidationError("public studied article language must be covered")
        if any(entry.get(k) for k in ("cards", "terms", "patterns", "writing")):
            raise ValidationError("protected unit can only display its coverage")
        result["title"] = "已学习的受保护题目"
        return result
    source_units = group["units"].copy()
    if group["prepared_article"] and mode == "writing":
        raise ValidationError("reading evidence cannot become an essay draft")
    if group["prepared_article"] and not source_units:
        raise ValidationError("question-only groups use protected coverage")
    if not source_units and mode != "writing":
        for p in parts:
            if (
                p["source_kind"] not in {"article", "user_provided"}
                or p["answer_exposure"] != "answer_free"
            ):
                continue
            _, src = original(p)
            if not src.get("question_id"):
                source_units[p["unit_id"]] = public_text(src["source_sentence"], maximum=18000)
    cards = entry.get("cards", [])
    if mode == "writing":
        if cards or not entry.get("writing"):
            raise ValidationError("writing feedback requires a real draft")
        for item in entry["writing"]:
            msgs, src = original(item)
            # A protected writing critique can yield a short author-reviewed draft span only.
            identity = " ".join(
                str(src.get(k, "")) for k in ("unit_id", "source_id", "reference_id", "title")
            )
            writing = src.get("writing_session", {})
            task = writing.get("task", {})
            native_writing = (writing.get("schema") == "english-writing-session/2"
                              and not src.get("question_id") and bool(task.get("task_id"))
                              and src.get("unit_id") == task["task_id"]
                              and src.get("source_hash") == object_sha256(task)
                              and src.get("source_id") == "WRUNIT-" + src["source_hash"][:32])
            if not native_writing and not re.search(r"(?:essay|writing|作文|E(?:I|II)-\d{4}-[AB])", identity, re.I) and (
                src.get("question_id")
                or src.get("source_kind") not in {"user_provided", "article"}
                or src.get("answer_exposure") != "answer_free"
            ):
                raise ValidationError("reading-question evidence cannot become a writing draft")
            if not re.search(r"[A-Za-z]{2,}", item.get("quote", "")):
                raise ValidationError("English writing feedback requires the original English draft")
            draft = _quote(msgs, item, src.get("source_sentence") or "", writing=True)
            stage = item.get("stage")
            if stage not in {"原稿", "提示后改稿", "改稿", "自述独立完成", "帮助程度未记录"}:
                raise ValidationError("writing stage is required")
            if stage == "自述独立完成":
                if not item.get("independence_quote"):
                    raise ValidationError(
                        "independence self-report needs the learner's original statement"
                    )
                _quote(
                    msgs,
                    {**item, "quote": item["independence_quote"]},
                    src.get("source_sentence") or "",
                    writing=True,
                )
            result["writing"].append(
                dict(
                    stage=stage,
                    quote=draft,
                    feedback=public_text(item.get("feedback")),
                    source=f"{item['package_id']} · 消息 {item['message']}",
                )
            )
        if entry.get("terms") or entry.get("patterns"):
            source_units.update({f"D{i + 1}": v["quote"] for i, v in enumerate(result["writing"])})
    if mode != "writing" and not cards:
        raise ValidationError("public feedback requires sentence cards")
    for card in cards:
        unit = card.get("unit")
        if unit not in source_units:
            raise ValidationError("card must bind a known public source sentence")
        sentence = public_text(source_units[unit])
        nodes = []
        for node in card.get("nodes", []):
            span = public_text(node.get("en"))
            if span not in sentence:
                raise ValidationError("structure span missing from source")
            nodes.append(
                dict(
                    en=span,
                    zh=public_text(node.get("zh")),
                    role=public_text(node.get("role")),
                    relation=public_text(node.get("relation")),
                )
            )
        row = dict(
            unit=unit,
            en=sentence,
            zh=public_text(card.get("zh")),
            action=public_text(card["action"]) if card.get("action") else "",
            nodes=nodes,
        )
        if "translation_focus" in card:
            phrase = public_text(card["translation_focus"], maximum=40)
            if row["zh"].count(phrase) != 1:
                raise ValidationError("translation focus must occur exactly once in the complete translation")
            row["translation_focus"] = phrase
        if "focus" in card:
            focus = card["focus"]
            if not isinstance(focus, list) or not focus:
                raise ValidationError("focus must list the decisive spans from this sentence")
            spans = [public_text(span) for span in focus]
            if any(sentence.count(span) != 1 for span in spans):
                raise ValidationError("each focus span must occur exactly once in the original sentence")
            offsets = sorted((sentence.index(span), sentence.index(span) + len(span)) for span in spans)
            if any(current[0] < previous[1] for previous, current in zip(offsets, offsets[1:])):
                raise ValidationError("focus spans cannot overlap")
            row["focus"] = spans
        if card.get("observation"):
            ref = card["observation"]
            msgs, origin = original(ref)
            later = None
            if ref.get("followup"):
                followup = ref["followup"]
                if (
                    followup.get("package_id") != ref.get("package_id")
                    or type(followup.get("message")) is not int
                    or followup["message"] <= ref["message"]
                ):
                    raise ValidationError("follow-up requires a later learner message in this attempt")
                status = followup.get("stage")
                if status not in {"提示后复述", "同次自改", "自述理解"}:
                    raise ValidationError(
                        "unsupported follow-up evidence; use native history for independent transfer"
                    )
                later = f"{status} · " + _quote(msgs, followup, sentence, writing=True)
            exact = origin.get("source_sentence", "").strip() == sentence and not origin.get(
                "question_id"
            )
            row["observation"] = dict(
                quote=_quote(msgs, ref, sentence, bound_sentence=exact),
                feedback=public_text(ref.get("feedback")),
                source=f"{ref['package_id']} · 消息 {ref['message']}",
                later=later,
            )
        result["cards"].append(row)
    for term in entry.get("terms", []):
        unit = term.get("unit")
        form = public_text(term.get("form"), maximum=100)
        item = key(term.get("item", ""))
        if unit not in source_units or not re.search(
            r"(?<!\w)" + re.escape(form) + r"(?!\w)", source_units[unit], re.I
        ):
            raise ValidationError("expression missing from its sentence")
        checked_reference(conn, item, form)
        row = dict(item=item, form=form, unit=unit, en=source_units[unit], counts=counts_for(conn, item))
        row.update(
            {k: public_text(term.get(k)) for k in ("pos", "meaning", "collocation", "translation")}
        )
        if term.get("writing"):
            row.update(
                writing=public_text(term["writing"]),
                example=public_text(term.get("example")),
                example_zh=public_text(term.get("example_zh")),
            )
        if term.get("exam_example"):
            row["exam_example"] = check_example(conn, term["exam_example"], item=item)
        if term.get("other_uses"):
            row["other_uses"] = [
                {k: public_text(sense.get(k)) for k in ("meaning", "example", "translation")}
                for sense in term["other_uses"]
            ]
        result["terms"].append(row)
    result["terms"].sort(
        key=lambda v: (-v["counts"]["I"]["papers"], -v["counts"]["I"]["hits"], v["item"])
    )
    reviewed_grammar = grammar_for(repo, conn, source_units)
    for pattern in entry.get("patterns", []):
        unit = pattern.get("unit")
        if unit not in source_units:
            raise ValidationError("grammar example has no source sentence")
        examples = [check_example(conn, e) for e in pattern.get("examples", [])]
        locs = [(e["locator"], e["quote"]) for e in examples]
        if len(locs) != len(set(locs)) or not examples:
            raise ValidationError("unique cross-paper grammar evidence required")
        row = dict(
            unit=unit,
            title=public_text(pattern.get("title")),
            relation=public_text(pattern.get("relation")),
            en=source_units[unit],
            examples=examples,
            papers=len({e["paper"] for e in examples}),
        )
        if pattern.get("grammar_id"):
            match = reviewed_grammar.get(pattern["grammar_id"])
            if not match or unit not in match["units"]:
                raise ValidationError("the audited structure does not match this source sentence")
            row.update(counts=match["counts"], scope=match["scope"])
        result["patterns"].append(row)
    result["patterns"].sort(
        key=lambda v: (
            -v.get("counts", {}).get("I", {}).get("papers", 0),
            -v.get("counts", {}).get("I", {}).get("hits", 0),
            -v["papers"],
        )
    )
    return result


def checked_reference(conn, item, form):
    normalize = lambda s: re.sub(r"[^a-z0-9' -]", "", key(s).lower())
    if normalize(item) == normalize(form):
        return
    a, b = normalize(item).split(), normalize(form).split()
    if len(a) != len(b) or any(
        x != y
        and not conn.execute(
            "SELECT 1 FROM occurrences WHERE surface=? AND lemma=? AND lemma_status!='ambiguous_literal' LIMIT 1",
            (y, x),
        ).fetchone()
        for x, y in zip(a, b)
    ):
        raise ValidationError("unverified form/lemma pair")


def check_example(conn, e, item=None):
    loc = e["locator"]
    quote = public_text(e.get("quote"))
    row = conn.execute(
        "SELECT u.text,u.paper_id,p.year FROM units u JOIN papers p ON p.id=u.paper_id WHERE u.id=? AND u.kind='passage' AND u.scope='main'",
        (loc,),
    ).fetchone()
    if not row or not 2010 <= row["year"] <= 2026 or quote not in row["text"]:
        raise ValidationError("exam example must match the named main-corpus passage")
    if item:
        form = public_text(e.get("form", item), maximum=100)
        if not re.search(r"(?<!\w)" + re.escape(form) + r"(?!\w)", quote, re.I):
            raise ValidationError("exam example lacks the expression")
        checked_reference(conn, item, form)
    return dict(
        locator=loc, paper=row["paper_id"], quote=quote, translation=public_text(e.get("translation"))
    )


def reference_basis(repo):
    grammar = repo / "assets/english/review-grammar.json"
    with closing(connect(repo / LEXICON_DB)) as conn:
        manifest = conn.execute("SELECT value FROM meta WHERE name='manifest'").fetchone()[0]
    return object_sha256([manifest, file_sha256(grammar) if grammar.is_file() else None,
                          file_sha256(Path(__file__)), file_sha256(Path(__file__).with_name("evening_render.py"))])


def _publish(repo, state, day, document, scope, groups):
    from .evening_render import render

    destination = repo / "review/personalized" / day
    if (
        not groups
        or document.get("schema_version") != SCHEMA
        or document.get("study_date") != day
        or document.get("scope_sha256") != scope["scope_sha256"]
    ):
        raise ValidationError("refresh the exact daily study scope before publishing feedback")
    entries = document.get("groups", [])
    if len(entries) != len(groups) or {v["id"] for v in entries} != set(groups):
        raise ValidationError("feedback must cover each completed learning group exactly once")
    if document.get("reviewed_visibility") != "answer_free":
        raise ValidationError("an answer-free semantic review is required")
    roots = {}
    with closing(connect(repo / LEXICON_DB)) as conn:
        content = [_check_semantics(repo, state, groups[v["id"]], v, conn, roots) for v in entries]
    day_state = state / "evening-feedback" / day
    path = destination / "index.html"
    stage = day_state / "author.json"
    # A validated draft survives a renderer/interrupted-write failure. No writer is replayed.
    atomic_write_json(stage, document)
    page = render(day, content, {**scope, "history_available": (repo / "review/personalized/history/index.html").is_file()})
    if collect_day(repo, state, day)["scope_sha256"] != scope["scope_sha256"]:
        raise ValidationError("day scope changed during rendering")
    atomic_write_bytes(path, page.encode())
    result = dict(
        status="READY",
        study_date=day,
        scope_sha256=scope["scope_sha256"],
        writer_receipts=scope["receipts"],
        groups=len(content),
        completed_packages=len(scope["packages"]),
        pending=scope["pending"],
        page=str(path),
        page_sha256=text_sha256(page),
        author_sha256=object_sha256(document),
        reference_basis=reference_basis(repo),
        brief=[
            {
                "title": v["title"],
                "units": len(v["cards"]),
                "first_action": next((c["action"] for c in v["cards"] if c["action"]), None),
                "expressions": [t["item"] for t in v["terms"][:3]],
                "writing_transfer": next(({"item":t["item"], "use":t["writing"]} for t in v["terms"] if t.get("writing")), None),
            }
            for v in content
        ],
        formal_write_count=0,
    )
    atomic_write_json(day_state / "current.json", result)
    return result


def publish_day(repo, state, day, document):
    valid_date(day)
    with exclusive_lock(state / "locks" / f"evening-{day}.lock"):
        scope, groups = article_groups(repo, state, day)
        return _publish(repo, state, day, document, scope, groups)


def resume_day(repo, state, day):
    """Return a verified page, rebuild just that page, or request the missing author coverage."""
    valid_date(day)
    with exclusive_lock(state / "locks" / f"evening-{day}.lock"):
        scope, groups = article_groups(repo, state, day)
        base = dict(study_date=day, scope_sha256=scope["scope_sha256"], formal_write_count=0)
        if not groups:
            return {**base, "status": "NO_COMPLETED_UNITS", "pending": scope["pending"]}
        day_state = state / "evening-feedback" / day
        author_path, receipt_path = day_state / "author.json", day_state / "current.json"
        draft = load_json(author_path) if author_path.is_file() else {}
        if draft.get("scope_sha256") != scope["scope_sha256"]:
            return {
                **base,
                "status": "AUTHORING_REQUIRED",
                "next_action": "prepare-evening-feedback",
                "groups": list(groups),
                "prior_draft": str(author_path) if draft else None,
            }
        receipt = load_json(receipt_path) if receipt_path.is_file() else {}
        page = repo / "review/personalized" / day / "index.html"
        if (
            receipt.get("status") == "READY"
            and receipt.get("scope_sha256") == scope["scope_sha256"]
            and receipt.get("author_sha256") == object_sha256(draft)
            and receipt.get("reference_basis") == reference_basis(repo)
            and receipt.get("page") == str(page)
            and page.is_file()
            and file_sha256(page) == receipt.get("page_sha256")
        ):
            return receipt
        return _publish(repo, state, day, draft, scope, groups)


def after_formal(repo, state, receipt, local):
    """A teaching delivery failure cannot alter a committed receipt or a learning event."""
    day = (receipt.get("authorization") or {}).get("study_date")
    if not day:
        match = re.fullmatch(
            r"EN-BATCH-(\d{4})(\d{2})(\d{2})-[A-F0-9]{12}", str(receipt.get("batch_id", ""))
        )
        if not match:
            return {"status": "NO_STUDY_DATE", "formal_write_count": 0}
        day = "-".join(match.groups())
    if local.get("status") != "VERIFIED_CURRENT":
        return {"status": "WAITING_FOR_NATIVE_READBACK", "study_date": day, "formal_write_count": 0}
    try:
        return resume_day(repo, state, day)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ValidationError) as exc:
        return {
            "status": "FEEDBACK_PENDING",
            "study_date": day,
            "reason": str(exc),
            "next_action": "resume-evening-feedback",
            "formal_write_count": 0,
        }
