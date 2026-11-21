"""Deterministic, answer-safe reading units for foreground English teaching."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .sources import build_source_catalog, resolve_article_source
from .util import atomic_write_json, canonical_bytes, file_sha256, load_json, object_sha256, text_sha256


SCHEMA = "english_reading_preparation_v1"
DEFAULT_OUTPUT = Path("intake/reading-preparation")
_END = re.compile(r"[.!?][\"'’”)]*(?=\s+(?:[\"'‘“(]*[A-Z0-9]))")
_NON_ENDINGS = (
    "Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "Sr.", "Jr.", "St.", "vs.",
    "e.g.", "i.e.", "U.S.", "U.K.", "No.", "Fig.",
)


def _sentences(paragraph: str) -> list[str]:
    """Split only high-confidence boundaries and prove lossless reconstruction."""
    starts = [0]
    for match in _END.finditer(paragraph):
        end = match.end()
        prefix = paragraph[:end]
        open_quote = prefix.count("“") > prefix.count("”") or prefix.count('"') % 2 == 1
        if (prefix.endswith(_NON_ENDINGS) or re.search(r"\b[A-Z]\.$", prefix)
                or re.search(r"(?:\b[A-Z]\.){2,}$", prefix) or open_quote):
            continue
        starts.append(len(paragraph) - len(paragraph[end:].lstrip()))
    starts = sorted(set(starts))
    values = [paragraph[start:(starts[i + 1] if i + 1 < len(starts) else len(paragraph))].rstrip()
              for i, start in enumerate(starts)]
    if not values or " ".join(values) != paragraph:
        raise ValidationError("sentence segmentation is not lossless")
    return values


def _plain_article(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Read the extra source's body and answer-free questions, excluding study sections."""
    text = path.read_text(encoding="utf-8")
    before, marker, after = text.partition("\n## Questions\n")
    if not marker:
        raise ValidationError("plain source has no explicit Questions boundary")
    body_lines = before.splitlines()
    while body_lines and (not body_lines[0].strip() or body_lines[0].startswith("# ")
                          or re.match(r"^(date|source):", body_lines[0])):
        body_lines.pop(0)
    paragraphs = [part.replace("\n", " ").strip()
                  for part in re.split(r"\n\s*\n", "\n".join(body_lines)) if part.strip()]
    questions: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n(?=\d+\.)", after.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        head = re.fullmatch(r"(\d+)\.\s*(.+)", lines[0]) if lines else None
        if head is None or len(lines) != 5:
            raise ValidationError("plain source question format is ambiguous")
        options: dict[str, str] = {}
        for line in lines[1:]:
            option = re.fullmatch(r"\[([A-D])\]\s*(.+)", line)
            if option is None:
                raise ValidationError("plain source option format is ambiguous")
            options[option.group(1)] = option.group(2)
        if set(options) != {"A", "B", "C", "D"}:
            raise ValidationError("plain source options are incomplete")
        questions.append({"number": int(head.group(1)), "prompt": head.group(2), "options": options})
    return paragraphs, questions


def _practice_safe_markdown(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    text = path.read_text(encoding="utf-8")
    if "visibility: practice_safe" not in text:
        raise ValidationError("handoff canonical source is not practice-safe")
    article_match = re.search(r"^## Article\s*$\n(.*?)^## Questions\s*$\n", text, re.MULTILINE | re.DOTALL)
    if article_match is None:
        raise ValidationError("practice-safe Markdown lacks explicit article/question boundaries")
    article_part = article_match.group(1)
    paragraph_matches = list(re.finditer(r"^### P(\d+)\s*$\n+(.*?)(?=^### P\d+\s*$|\Z)",
                                                article_part, re.MULTILINE | re.DOTALL))
    paragraphs = [" ".join(match.group(2).strip().splitlines()).replace("*", "")
                  for match in paragraph_matches]
    question_part = text[article_match.end():]
    question_matches = list(re.finditer(r"^### (\d+)\.\s*$\n+(.*?)(?=^### \d+\.\s*$|\Z)",
                                               question_part, re.MULTILINE | re.DOTALL))
    questions = []
    for match in question_matches:
        block = match.group(2).strip()
        first_option = re.search(r"^\[A\]\s+", block, re.MULTILINE)
        if first_option is None:
            raise ValidationError("practice-safe Markdown question lacks options")
        prompt = " ".join(block[:first_option.start()].strip().splitlines())
        options = {letter: " ".join(value.strip().splitlines()) for letter, value in re.findall(
            r"^\[([A-D])\]\s+(.*?)(?=^\[[A-D]\]\s+|\Z)", block, re.MULTILINE | re.DOTALL)}
        if set(options) != {"A", "B", "C", "D"}:
            raise ValidationError("practice-safe Markdown options are incomplete")
        questions.append({"number": int(match.group(1)), "prompt": prompt, "options": options})
    if not paragraphs or not questions:
        raise ValidationError("practice-safe Markdown is incomplete")
    return paragraphs, questions


def _corpus_record(repo: Path, binding: dict[str, Any]) -> dict[str, Any] | None:
    candidates = sorted((repo / "raw/articles/exam-reading-corpus").glob("20??/text-*.json"))
    matches = []
    for path in candidates:
        record = load_json(path)
        if (record.get("source_id") == binding["source_id"]
                or record.get("article_path") == binding["source_article"]):
            matches.append(record)
    if len(matches) > 1:
        raise ValidationError("practice-safe corpus binding is ambiguous")
    return matches[0] if matches else None


def _source_payload(repo: Path, binding: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], dict[str, Any] | None]:
    canonical = repo / binding["canonical_path"]
    if binding["binding_kind"] == "handoff":
        paragraphs, questions = _practice_safe_markdown(canonical)
        return paragraphs, questions, _corpus_record(repo, binding)
    record = _corpus_record(repo, binding)
    if record is not None:
        if record.get("schema") != "exam_reading_practice_safe_v1" or record.get("visibility") != "practice_safe":
            raise ValidationError("ordinary preparation requires a practice-safe corpus record")
        return record["passage_paragraphs"], record["questions"], record
    if binding["binding_kind"] == "plain_source":
        paragraphs, questions = _plain_article(canonical)
        return paragraphs, questions, None
    raise ValidationError("reading preparation does not accept answer-bearing article Markdown")


def _article_record(repo: Path, binding: dict[str, Any]) -> dict[str, Any]:
    paragraphs, questions, corpus = _source_payload(repo, binding)
    sentence_no = 0
    prepared_paragraphs = []
    units = []
    for paragraph_no, paragraph in enumerate(paragraphs, 1):
        if not isinstance(paragraph, str) or not paragraph.strip():
            raise ValidationError("passage paragraph is empty")
        sentence_rows = []
        for sentence in _sentences(paragraph):
            sentence_no += 1
            row = {"unit_id": f"S{sentence_no:02d}", "sentence_id": f"S{sentence_no:02d}",
                   "paragraph_id": f"P{paragraph_no:02d}", "kind": "article",
                   "answer_exposure": "answer_free", "text": sentence,
                   "text_sha256": text_sha256(sentence)}
            sentence_rows.append(row)
            units.append(row)
        prepared_paragraphs.append({"paragraph_id": f"P{paragraph_no:02d}", "text": paragraph,
                                    "text_sha256": text_sha256(paragraph), "sentences": sentence_rows})
    prepared_questions = []
    for question in questions:
        number = int(question["number"])
        qid = f"Q{number}"
        q_units = [{"unit_id": f"{qid}-STEM", "question_id": qid, "kind": "question",
                    "answer_exposure": "answer_free", "text": question["prompt"],
                    "text_sha256": text_sha256(question["prompt"])}]
        for letter in "ABCD":
            text = question["options"][letter]
            q_units.append({"unit_id": f"{qid}-{letter}", "question_id": qid, "kind": "option",
                            "answer_exposure": "answer_free", "text": text,
                            "text_sha256": text_sha256(text)})
        prepared_questions.append({"question_id": qid, "number": number, "units": q_units})
        units.extend(q_units)
    core = {"schema_version": SCHEMA, "source_id": binding["source_id"],
            "source_hash": binding["source_hash"],
            "reference_id": corpus["reference_id"] if corpus is not None else binding["reference_id"],
            "source_article": binding["source_article"], "canonical_path": binding["canonical_path"],
            "practice_source_sha256": file_sha256(repo / binding["canonical_path"]),
            "visibility": "practice_safe", "answer_exposure": "answer_free",
            "paragraphs": prepared_paragraphs, "questions": prepared_questions, "units": units,
            "paragraph_count": len(prepared_paragraphs), "sentence_count": sentence_no,
            "question_count": len(prepared_questions), "unit_count": len(units),
            "formal_write_count": 0}
    return {**core, "record_sha256": object_sha256(core)}


def expected_files(repo_root: Path) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    catalog = build_source_catalog(repo)
    files: dict[str, Any] = {}
    index_entries = []
    protected_entries = []
    total_sentences = total_questions = total_units = 0
    exam_questions = supplementary_questions = 0
    for binding in catalog["entries"]:
        record = _article_record(repo, binding)
        relative = f"articles/{binding['source_id']}.json"
        files[relative] = record
        index_entries.append({key: record[key] for key in (
            "source_id", "source_hash", "reference_id", "source_article", "canonical_path",
            "practice_source_sha256", "paragraph_count", "sentence_count", "question_count",
            "unit_count", "record_sha256")})
        total_sentences += record["sentence_count"]
        total_questions += record["question_count"]
        if record["reference_id"] is None:
            supplementary_questions += record["question_count"]
        else:
            exam_questions += record["question_count"]
        total_units += record["unit_count"]
        source = _corpus_record(repo, binding)
        if source is not None:
            protected_path = repo / source["protected_analysis_path"]
            protected_entries.append({"source_id": binding["source_id"], "reference_id": binding["reference_id"],
                                      "protected_path": source["protected_analysis_path"],
                                      "protected_sha256": file_sha256(protected_path),
                                      "visibility": "protected", "ordinary_query_eligible": False})
    index_core = {"schema_version": SCHEMA, "subject": "english", "visibility": "practice_safe",
                  "answer_exposure": "answer_free", "article_count": len(index_entries),
                  "exam_article_count": sum(e["reference_id"] is not None for e in index_entries),
                  "sentence_count": total_sentences, "question_count": total_questions,
                  "exam_question_count": exam_questions,
                  "supplementary_question_count": supplementary_questions,
                  "unit_count": total_units, "entries": index_entries, "formal_write_count": 0}
    files["index.json"] = {**index_core, "index_sha256": object_sha256(index_core)}
    protected_core = {"schema_version": SCHEMA, "visibility": "protected_reference_only",
                      "contains_answer_content": False, "entry_count": len(protected_entries),
                      "entries": protected_entries, "ordinary_query_eligible": False,
                      "formal_write_count": 0}
    files["protected-reference-index.json"] = {**protected_core,
                                                "index_sha256": object_sha256(protected_core)}
    return files


def build(repo_root: Path, output: Path) -> dict[str, Any]:
    files = expected_files(repo_root)
    output.mkdir(parents=True, exist_ok=True)
    expected_names = set(files)
    for old in output.rglob("*.json"):
        if old.relative_to(output).as_posix() not in expected_names:
            old.unlink()
    for relative, payload in files.items():
        atomic_write_json(output / relative, payload)
    return verify(repo_root, output)


def verify(repo_root: Path, output: Path) -> dict[str, Any]:
    expected = expected_files(repo_root)
    actual_names = {p.relative_to(output).as_posix() for p in output.rglob("*.json")} if output.is_dir() else set()
    if actual_names != set(expected):
        raise ValidationError("reading preparation file set is incomplete or contains stale JSON")
    for relative, payload in expected.items():
        if (output / relative).read_bytes() != json.dumps(payload, ensure_ascii=False, indent=2).encode() + b"\n":
            raise ValidationError(f"reading preparation changed or is stale: {relative}")
    index = expected["index.json"]
    return {"status": "verified", "article_count": index["article_count"],
            "exam_article_count": index["exam_article_count"], "question_count": index["question_count"],
            "exam_question_count": index["exam_question_count"],
            "supplementary_question_count": index["supplementary_question_count"],
            "sentence_count": index["sentence_count"], "unit_count": index["unit_count"],
            "protected_reference_count": expected["protected-reference-index.json"]["entry_count"],
            "formal_write_count": 0}


def _source_identity(source_id: str) -> None:
    if not isinstance(source_id, str) or not source_id or "/" in source_id or "\\" in source_id or "\x00" in source_id:
        raise ValidationError("invalid reading source ID")


class UnknownReadingSource(ValidationError):
    """The available preparation index has no such ID or alias."""


def query(output: Path, *, source_id: str, unit_id: str | None = None, paragraph_id: str | None = None,
          repo_root: Path | None = None, source_hash: str | None = None,
          record_sha256: str | None = None) -> dict[str, Any]:
    _source_identity(source_id)
    path = output / "articles" / f"{source_id}.json"
    binding = None
    prepared_index = False
    canonical_id = source_id
    index_path = output / "index.json"
    if repo_root is not None and (index_path.is_file() or not path.exists()):
        if index_path.is_file():
            index = load_json(index_path)
            index_body = {key: value for key, value in index.items() if key != "index_sha256"}
            if (index.get("schema_version") != SCHEMA or index.get("visibility") != "practice_safe"
                    or index.get("answer_exposure") != "answer_free"
                    or object_sha256(index_body) != index.get("index_sha256")):
                raise ValidationError("reading preparation alias index is stale or invalid")
            entries = index["entries"]
            prepared_index = True
        else:
            entries = build_source_catalog(repo_root)["entries"]
        matches = [entry for entry in entries if source_id in (entry["source_id"], entry.get("reference_id"))]
        if not matches and not path.exists():
            raise UnknownReadingSource("reading preparation has no matching source or alias")
        if len(matches) != 1:
            raise ValidationError("reading preparation source is unindexed or ambiguous")
        binding = matches[0]
        canonical_id = binding["source_id"]
        _source_identity(canonical_id)
        path = output / "articles" / f"{canonical_id}.json"
    record = load_json(path)
    if record.get("source_id") != canonical_id:
        raise ValidationError("reading preparation source identity differs from its filename")
    identity_fields = ("source_id", "source_hash", "reference_id", "source_article", "canonical_path")
    if binding is not None:
        binding_fields = identity_fields + (("record_sha256", "practice_source_sha256") if prepared_index else ())
        if any(record.get(key) != binding.get(key) for key in binding_fields):
            raise ValidationError("reading preparation source alias differs from the verified catalog")
    if record.get("visibility") != "practice_safe" or record.get("answer_exposure") != "answer_free":
        raise ValidationError("reading preparation record is not answer-safe")
    core = {key: value for key, value in record.items() if key != "record_sha256"}
    if object_sha256(core) != record.get("record_sha256"):
        raise ValidationError("reading preparation record hash is invalid")
    if ((source_hash is not None and (not isinstance(source_hash, str)
                                     or source_hash.removeprefix("sha256:") != record["source_hash"]))
            or (record_sha256 is not None and record_sha256 != record["record_sha256"])):
        raise ValidationError("reading source version changed; query the current revision separately")
    if repo_root is not None:
        source = repo_root.resolve(strict=True) / record["canonical_path"]
        if file_sha256(source) != record.get("practice_source_sha256"):
            raise ValidationError("reading preparation source changed; rebuild before teaching")
        if prepared_index:
            live = resolve_article_source(repo_root, record["source_article"])
            if any(live.get(key) != binding.get(key) for key in identity_fields if key != "reference_id"):
                raise ValidationError("reading preparation alias differs from its current source")
            if live["reference_id"] != record["reference_id"]:
                # One legacy handoff declares the exam alias only in the answer-free corpus.
                match = re.fullmatch(r"EXAM-READING-(20\d{2})-T([1-4])", record["reference_id"] or "")
                if live["reference_id"] or live["binding_kind"] != "handoff" or not match:
                    raise ValidationError("reading preparation exam reference changed")
                corpus = load_json(repo_root / "raw/articles/exam-reading-corpus" / match[1] / f"text-{match[2]}.json")
                if (corpus.get("visibility") != "practice_safe" or corpus.get("schema") != "exam_reading_practice_safe_v1"
                        or corpus.get("article_path") != record["source_article"]
                        or any(corpus.get(key) != record[key] for key in ("source_id", "reference_id"))):
                    raise ValidationError("reading preparation exam reference has no matching corpus source")
    scope = {"schema_version": SCHEMA, "source_id": record["source_id"], "source_hash": record["source_hash"],
             "reference_id": record["reference_id"], "source_article": record["source_article"],
             "record_sha256": record["record_sha256"], "canonical_path": record["canonical_path"],
             "formal_write_count": 0}
    if paragraph_id is not None:
        paragraphs = record["paragraphs"]
        positions = [i for i, paragraph in enumerate(paragraphs) if paragraph["paragraph_id"] == paragraph_id]
        if unit_id is not None or len(positions) != 1:
            raise ValidationError("select one exact paragraph of this reading source")
        i = positions[0]
        pin = {k: scope[k] for k in ("source_id", "source_hash", "record_sha256")}
        more = i + 1 < len(paragraphs)
        return {**scope, "paragraph": paragraphs[i], "paragraph_count": len(paragraphs),
                "eof": not more, "full_read": pin,
                "next_read": {**pin, "paragraph_id": paragraphs[i+1]["paragraph_id"]} if more else None}
    if unit_id is None:
        return record
    matches = [unit for unit in record["units"] if unit["unit_id"] == unit_id]
    if len(matches) != 1:
        raise ValidationError("reading preparation unit identity is missing or ambiguous")
    return {**scope, "unit": matches[0]}


def query_review(output: Path, *, repo_root: Path, source_id: str, question_id: str,
                 unlocked_question: str, evidence_units: tuple[str, ...] = (),
                 source_hash: str | None = None, record_sha256: str | None = None) -> dict[str, Any]:
    """Return the indexed publisher analysis for one explicitly unlocked question."""
    if not re.fullmatch(r"Q[1-9][0-9]*", question_id) or unlocked_question != f"{source_id}/{question_id}":
        raise ValidationError("review requires the matching source and explicitly unlocked question")
    article = query(output, source_id=source_id, repo_root=repo_root,
                    source_hash=source_hash, record_sha256=record_sha256)
    reference = article["reference_id"]
    if not reference or not any(q["question_id"] == question_id for q in article["questions"]):
        raise ValidationError("no indexed exam question at this source and question ID")
    evidence = {}
    if evidence_units:
        sentences = {unit["unit_id"]: unit for unit in article["units"] if unit["kind"] == "article"}
        if any(unit_id not in sentences for unit_id in evidence_units):
            raise ValidationError("evidence must identify source sentences from this article")
        evidence = {unit_id: sentences[unit_id] for unit_id in evidence_units}

    protected = repo_root / "raw/protected/exam-reading-analysis"
    index = load_json(protected / "index.json")
    entries = [entry for entry in index["records"] if entry["reference_id"] == reference]
    if len(entries) != 1 or entries[0]["status"] != "preprocessed_complete":
        raise ValidationError("verified publisher analysis is unavailable for this source")
    entry = entries[0]
    path = (repo_root / entry["json_path"]).resolve()
    if not path.is_relative_to(protected.resolve()):
        raise ValidationError("invalid protected analysis path")
    record = load_json(path)
    if (record.get("reference_id") != reference or record.get("analysis_id") != entry["analysis_id"]
            or record.get("visibility") != "hidden_until_review"
            or record.get("extraction_status") != "preprocessed_complete"):
        raise ValidationError("publisher analysis does not match the indexed exam source")
    sources = load_json(repo_root / "raw/reference_sources/exam_pdf_manifest.json")["sources"]
    publisher = [source for source in sources if source["year"] == record["year"]
                 and source["role"] == "answer_analysis_hidden_source"]
    if len(publisher) != 1 or publisher[0]["sha256"] != record.get("source_sha256"):
        raise ValidationError("publisher analysis differs from the registered PDF")
    number = int(question_id[1:])
    matches = [item for item in record["questions"] if item["question_number"] == number]
    if len(matches) != 1:
        raise ValidationError("publisher analysis lacks an exact question block")
    question = matches[0]
    answer = question.get("correct_answer")
    if (not str(question.get("verification_status", "")).startswith("source_verified")
            or answer not in {"A", "B", "C", "D"} or record["answer_key"].get(str(number)) != answer
            or not str(question.get("analysis_text", "")).strip()
            or not question.get("analysis_source_pages")):
        raise ValidationError("publisher answer or analysis lacks source verification")
    result = {
        "source_id": article["source_id"], "reference_id": reference, "question_id": question_id,
        "source_hash": article["source_hash"], "record_sha256": article["record_sha256"],
        "analysis_id": record["analysis_id"], "analysis_record_sha256": object_sha256(record),
        "correct_answer": answer, "analysis_text": question["analysis_text"],
        "analysis_path": entry["json_path"], "analysis_source_pages": question["analysis_source_pages"],
        "answer_evidence_page": question["answer_evidence_page"], "source_sha256": record["source_sha256"],
        "verification_status": question["verification_status"], "formal_write_count": 0,
    }
    if evidence:
        result["source_sentences"] = evidence
    return result
