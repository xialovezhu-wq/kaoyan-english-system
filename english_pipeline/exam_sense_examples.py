"""Source-checked bilingual examples for the live reading dictionary."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from .answer_safety import contains_answer_signal
from .errors import ValidationError
from .exam_lexicon import LEXICON_DB, WORD, connect, key
from .reading_preparation import DEFAULT_OUTPUT, _sentences, query


CATALOG = Path("raw/reference_sources/exam_lexicon/reviewed-exam-usages.json")
FIELDS = ("unit_id", "surface", "meaning", "example", "translation")


def location(row):
    year, series = row["year"], row["series"]
    exam = "旧统考" if year < 2010 else ("英语一" if series == "I" else "英语二")
    unit = row["id"].split(":", 1)[-1]
    match = re.fullmatch(r"p-section2-part-a-(\d+)-(\d+)", unit)
    if match:
        part = f"阅读 Text {match[1]} · 第 {match[2]} 段"
    elif unit.startswith("p-section2-part-b-"):
        part = "阅读 Part B"
    elif unit.startswith("p-section2-part-c-") or (series == "II" and unit.startswith("p-section3-")):
        part = "翻译"
    else:
        part = "原文"
    return f"{year} {exam} · {part}"


def matching_sentences(row, item, *, mode="lemma"):
    """Use the exact occurrence's lemma; never borrow a homograph from another sentence."""
    terms = key(item).split()
    lemmas = [w for w in row["lemma_text"].split() if w != "|"]
    surface = [w for w in row["surface_text"].split() if w != "|"]
    offset = 0
    for sentence in _sentences(row["text"]):
        tokens = list(WORD.finditer(sentence))
        if [key(t.group()) for t in tokens] != surface[offset:offset+len(tokens)]:
            raise ValidationError("exam_sentence_token_alignment_changed")
        seen = set()
        for i in range(len(tokens)-len(terms)+1):
            start = offset + i
            words = surface[start:start+len(terms)]
            if mode == "surface":
                matched = words == terms
            else:
                matched = all(lemmas[start+j] == t or (len(terms)>1 and words[j] == t) for j,t in enumerate(terms))
            if not matched:
                continue
            if any(sentence[tokens[j].end():tokens[j+1].start()].strip() for j in range(i,i+len(terms)-1)):
                continue
            form = " ".join(words)
            if form not in seen:
                seen.add(form)
                yield sentence, form
        offset += len(tokens)


def reading_order(reference):
    match = re.fullmatch(r"(\d{4})-(I|II)-T([1-4])", reference or "")
    if not match:
        raise ValidationError("exact_reading_boundary_required")
    return int(match[1]), match[2], int(match[3])


def earlier(row, boundary):
    year, series, text_number = boundary
    if row["year"] != year:
        return row["year"] < year
    passage = re.fullmatch(r"[^:]+:p-section2-part-a-([1-4])-\d+",row["id"])
    return bool(row["series"] == series and passage and int(passage[1]) < text_number)


def candidates(conn, item, limit=30, exclude_paper=None, *, before=None, mode="lemma"):
    terms = [key(t) for t in WORD.findall(item)]
    if not terms or not 1 <= limit <= 80:
        raise ValidationError("invalid_exam_example_query")
    search = " AND ".join('"'+t+'"' for t in terms)
    rows = conn.execute("""SELECT u.*,p.year,p.series FROM unit_fts f JOIN units u ON u.rowid=f.rowid
        JOIN papers p ON p.id=u.paper_id WHERE unit_fts MATCH ? AND u.kind='passage' AND u.scope='main'
        AND (? IS NULL OR u.paper_id<>?) ORDER BY p.year DESC,u.id""", (search,exclude_paper,exclude_paper))
    found = []
    for row in rows:
        if before and not earlier(row,before):
            continue
        for example, form in matching_sentences(row, item, mode=mode):
            found.append(dict(unit_id=row["id"],surface=form,example=example,locator=location(row)))
            if len(found) == limit:
                return found
    return found


def verified(conn, item, record, paper, *, before=None):
    if not isinstance(record,dict) or not all(isinstance(record.get(k),str) and record[k].strip() for k in FIELDS):
        raise ValidationError("incomplete_exam_sense_example")
    if contains_answer_signal({k:record[k] for k in FIELDS}):
        raise ValidationError("protected_exam_example")
    row = conn.execute("""SELECT u.*,p.year,p.series FROM units u JOIN papers p ON p.id=u.paper_id
        WHERE u.id=? AND u.kind='passage' AND u.scope='main'""", (record["unit_id"],)).fetchone()
    if row is None:
        raise ValidationError("exam_example_source_mismatch")
    if before and not earlier(row,before):
        return None
    if (record["example"],key(record["surface"])) not in matching_sentences(row,item):
        raise ValidationError("exam_example_source_mismatch")
    # Other passages from the paper being studied can give away the active questions.
    if not before and row["paper_id"] == paper:
        return None
    return {**{k:record[k] for k in FIELDS}, "locator":location(row)}


def example_diagram(row, layout, prepare_example):
    """Compile optional word glosses against exact spans within the verified example."""
    if not isinstance(layout, dict) or set(layout) != {"rows", "edges"} or contains_answer_signal(layout):
        raise ValidationError("exam_example_structure_required")
    rows = layout["rows"]
    if not isinstance(rows, list) or any(not isinstance(group, list) for group in rows):
        raise ValidationError("invalid_exam_example_structure")
    annotated = set()
    for group in rows:
        for node in group:
            if (not isinstance(node, list) or len(node) not in (4, 5)
                    or not all(isinstance(value, str) for value in node[:4])):
                raise ValidationError("invalid_exam_example_structure")
            if len(node) == 5:
                if not isinstance(node[4], dict) or not node[4] or set(node[4]) - {"parts", "breakdown"}:
                    raise ValidationError("invalid_exam_example_structure")
                if "parts" in node[4]:
                    annotated.add(node[0])
    diagram = prepare_example(dict(sentence=row["example"], translation=row["translation"], **layout))
    aligned = diagram.get("aligned_text", {}).get("sentence", [])
    if ({n["id"] for n in diagram["nodes"]} != {nid for nid, _ in aligned if nid is not None}
            or any(nid is None and re.search(r"\w", chunk) for nid, chunk in aligned)):
        raise ValidationError("incomplete_exam_example_structure")
    for node in diagram["nodes"]:
        if node["id"] not in annotated:
            continue
        text = node["text"]
        chunks, cursor = [], 0
        for index, part in enumerate(node["parts"]):
            pattern = re.compile(rf"(?<![\w’'-]){re.escape(part['text'])}(?![\w’'-])")
            match = pattern.search(text, cursor)
            if match is None:
                raise ValidationError("exam_example_word_outside_its_source_span")
            start, end = match.span()
            if cursor < start:
                chunks.append([None, text[cursor:start]])
            chunks.append([None if part.get("translation_protected") else index, text[start:end]])
            cursor = end
        if cursor < len(text):
            chunks.append([None, text[cursor:]])
        node["part_alignment"] = chunks
    return diagram


def attach(repo, model, source_id, unit, supplied=None, *, prepare_example):
    entries = [w for n in model["nodes"] for field in ("vocabulary", "comparison_vocabulary")
               for w in n.get(field, [])]
    if not entries:
        return
    supplied = {} if supplied is None else supplied
    if not isinstance(supplied,dict) or set(supplied) - {key(e["item"]) for e in entries}:
        raise ValidationError("exam_examples_outside_current_vocabulary")
    current = {} if source_id.startswith("TEMP-") else query(repo/DEFAULT_OUTPUT,repo_root=repo,source_id=source_id,unit_id=unit)
    ref = current.get("reference_id") or ""
    match = re.fullmatch(r"EXAM-READING-(\d{4})-T(\d+)",ref)
    before = (int(match[1]), "I", int(match[2])) if match else None
    label = f"{match[1]} 英语一 · 阅读 Text {match[2]} · {unit}" if match else "本句"
    if current.get("unit",{}).get("text",model["sentence"]) != model["sentence"]:
        label += " · 原句片段"
    path = repo / CATALOG
    catalog = json.loads(path.read_text()) if path.is_file() else {}
    for entry in entries:
        entry["current_locator"] = label
        entry["meanings"] = []
        reference = entry.get("reference",{})
        for field in ("senses","translations","exam_examples"):
            reference.pop(field,None)
        if not before:
            entry["exam_usages"] = []
    if not before:
        return
    try:
        conn = connect(repo / LEXICON_DB)
    except (OSError,ValueError,sqlite3.DatabaseError):
        if supplied or any(e.get("exam_usages") for e in entries):
            raise ValidationError("exam_example_index_unavailable")
        for e in entries:
            e["exam_usages"] = []
        return
    with closing(conn):
        for e in entries:
            item = key(e["item"])
            records = {}
            extra = supplied.get(item,[])
            if not isinstance(extra,list) or len(extra)>20:
                raise ValidationError("bounded_exam_usages_required")
            for r in [*e.get("exam_usages",[]),*catalog.get(item,[]),*extra]:
                row = verified(conn,item,r,None,before=before)
                if row:
                    # Only author-reviewed structure enters an example. Older display rows can
                    # recover their compact source layout; catalog corrections take precedence.
                    layout = r.get("structure")
                    if layout is None and r not in extra:
                        continue
                    row.update(structure=layout, diagram=example_diagram(row, layout, prepare_example))
                    records[(row["unit_id"],row["example"],row["surface"])] = row
            e["exam_usages"] = list(records.values())
