#!/usr/bin/env python3
"""Build the protected 2010-2024 Reading Part A answer/explanation corpus.

The practice-safe article corpus remains answer-free. This builder renders the
user-provided analysis PDFs, runs macOS Vision OCR (Chinese + English), keeps the
PDF text layer as a second representation, and writes protected per-page and
per-Text raw artifacts for fast later review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = ROOT / "raw" / "articles" / "exam-reading-corpus"
OUTPUT_ROOT = ROOT / "raw" / "protected" / "exam-reading-analysis"
PAGE_ROOT = OUTPUT_ROOT / "source-pages"
TMP_ROOT = ROOT / "tmp" / "pdfs" / "exam-reading-analysis"
VISION_SOURCE = ROOT / "scripts" / "vision_ocr.swift"
VISION_BINARY = TMP_ROOT / "vision_ocr"
SCHEMA = "exam_reading_protected_analysis_v1"
PAGE_SCHEMA = "exam_reading_analysis_page_evidence_v1"
EXPECTED_QUESTIONS = {
    1: list(range(21, 26)),
    2: list(range(26, 31)),
    3: list(range(31, 36)),
    4: list(range(36, 41)),
}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=capture,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "no output").strip()
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def ensure_tools() -> None:
    missing = [name for name in ("pdftoppm", "pdftotext", "pdfinfo", "swiftc") if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"missing required tools: {', '.join(missing)}")
    if not VISION_SOURCE.is_file():
        raise RuntimeError(f"missing Vision OCR source: {VISION_SOURCE}")
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    if not VISION_BINARY.is_file() or VISION_BINARY.stat().st_mtime < VISION_SOURCE.stat().st_mtime:
        run(["swiftc", str(VISION_SOURCE), "-o", str(VISION_BINARY)])


def pdf_page_count(path: Path) -> int:
    output = run(["pdfinfo", str(path)]).stdout
    match = re.search(r"^Pages:\s+(\d+)", output, re.MULTILINE)
    if not match:
        raise ValueError(f"cannot determine PDF page count: {path}")
    return int(match.group(1))


def record_path(year: int, text_no: int) -> Path:
    return CORPUS_ROOT / str(year) / f"text-{text_no}.json"


def load_practice_record(year: int, text_no: int) -> dict[str, Any]:
    return json.loads(record_path(year, text_no).read_text(encoding="utf-8"))


def parse_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)-(\d+)", str(value).strip())
    if not match:
        raise ValueError(f"bad page range: {value!r}")
    return int(match.group(1)), int(match.group(2))


def page_json_path(year: int, page: int) -> Path:
    return PAGE_ROOT / str(year) / f"page-{page:03d}.json"


def page_md_path(year: int, page: int) -> Path:
    return PAGE_ROOT / str(year) / f"page-{page:03d}.md"


def text_json_path(year: int, text_no: int) -> Path:
    return OUTPUT_ROOT / str(year) / f"text-{text_no}.json"


def text_md_path(year: int, text_no: int) -> Path:
    return OUTPUT_ROOT / str(year) / f"text-{text_no}.md"


def render_and_ocr_page(
    *,
    year: int,
    page: int,
    source_pdf: Path,
    source_hash: str,
    dpi: int,
    refresh: bool,
    generated_date: str,
) -> dict[str, Any]:
    out_json = page_json_path(year, page)
    if out_json.is_file() and not refresh:
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        if payload.get("source_sha256") == source_hash and payload.get("ocr_dpi") == dpi:
            return payload

    page_tmp = TMP_ROOT / str(year)
    page_tmp.mkdir(parents=True, exist_ok=True)
    image_prefix = page_tmp / f"page-{page:03d}"
    image_path = image_prefix.with_suffix(".png")
    run(
        [
            "pdftoppm",
            "-f",
            str(page),
            "-l",
            str(page),
            "-r",
            str(dpi),
            "-png",
            "-singlefile",
            str(source_pdf),
            str(image_prefix),
        ]
    )
    vision_result = run([str(VISION_BINARY), str(image_path)])
    lines = json.loads(vision_result.stdout)
    vision_text = "\n".join(str(item.get("text", "")).strip() for item in lines if item.get("text", "").strip())
    pdf_text = run(
        ["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(source_pdf), "-"]
    ).stdout.strip()
    try:
        image_path.unlink()
    except FileNotFoundError:
        pass

    payload = {
        "schema": PAGE_SCHEMA,
        "generated_date": generated_date,
        "source_id": f"EXAM-ANALYSIS-SOURCE-{year}-P{page:03d}",
        "year": year,
        "pdf_page": page,
        "source_pdf": str(source_pdf),
        "source_sha256": source_hash,
        "visibility": "hidden_until_review",
        "extraction_method": "macos_vision_accurate_zh-Hans_en-US_plus_pdftotext_layout",
        "ocr_dpi": dpi,
        "vision_ocr_lines": lines,
        "vision_ocr_text": vision_text,
        "pdf_text_layer": pdf_text,
        "quality": {
            "vision_line_count": len(lines),
            "vision_character_count": len(vision_text),
            "pdf_text_character_count": len(pdf_text),
            "nonempty": bool(vision_text or pdf_text),
        },
    }
    write_json(out_json, payload)
    md = [
        "---",
        f"source_id: {payload['source_id']}",
        f"year: {year}",
        f"pdf_page: {page}",
        "visibility: hidden_until_review",
        "source_role: protected_derived_ocr_evidence",
        "---",
        "",
        f"# {year} 解析 PDF p{page}｜受保护逐页证据",
        "",
        "> [!warning] 答案解析受保护层",
        "> 用户只陈述选项时不读取本页；仅在明确要求核对答案、讲题或进入复盘后读取。Vision OCR 与 PDF 文字层均为来源转写，不冒充人工校订文本。",
        "",
        "## Vision OCR（中文 + 英文）",
        "",
        "```text",
        vision_text,
        "```",
        "",
        "## PDF 文字层（交叉核对）",
        "",
        "```text",
        pdf_text or "[无可用 PDF 文字层]",
        "```",
        "",
    ]
    page_md_path(year, page).parent.mkdir(parents=True, exist_ok=True)
    page_md_path(year, page).write_text("\n".join(md), encoding="utf-8")
    return payload


def normalize_english(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'")
    return " ".join(re.findall(r"[a-z0-9']+", text))


def find_question_anchor(
    prompt: str,
    question_number: int,
    lines: list[dict[str, Any]],
    *,
    min_index: int = 0,
    preferred_pages: list[int] | None = None,
) -> tuple[int, float]:
    target = normalize_english(prompt)
    target_tokens = set(target.split())
    preferred_page_set = set(preferred_pages or [])
    best_index = -1
    best_score = -1.0
    for index in range(min_index, len(lines)):
        for width in range(1, 6):
            window = " ".join(str(item["text"]) for item in lines[index : index + width])
            candidate = normalize_english(window)
            if not candidate:
                continue
            candidate_tokens = set(candidate.split())
            overlap = len(target_tokens & candidate_tokens) / max(1, len(target_tokens))
            sequence = SequenceMatcher(None, target, candidate[: max(len(target) * 2, 80)]).ratio()
            number_bonus = 0.12 if re.search(rf"(?:^|\s){question_number}(?:\.|\s)", window) else 0.0
            first_tokens = target.split()[:3]
            first_bonus = 0.08 if first_tokens and all(token in candidate_tokens for token in first_tokens) else 0.0
            # Answer-key summaries near the front of a Text can repeat an exact
            # question prompt before the publisher's full question analysis.
            # Prefer the independently audited evidence page, while keeping the
            # match fuzzy enough for OCR-damaged prompts.
            page_bonus = 0.16 if int(lines[index]["page"]) in preferred_page_set else 0.0
            score = overlap * 0.62 + sequence * 0.30 + number_bonus + first_bonus + page_bonus
            if score > best_score:
                best_score = score
                best_index = index
    if best_index < 0 or best_score < 0.34:
        # Last-resort anchor: a line that begins with the question number.
        for index, item in enumerate(lines[min_index:], min_index):
            if re.match(rf"^\s*{question_number}\s*[.．、]", str(item["text"])):
                return index, 0.25
        raise ValueError(f"cannot locate Q{question_number}; best_score={best_score:.3f}")
    return best_index, round(best_score, 4)


def flatten_page_lines(page_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for payload in sorted(page_payloads, key=lambda row: int(row["pdf_page"])):
        for line_index, item in enumerate(payload.get("vision_ocr_lines", []), 1):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            flattened.append(
                {
                    "page": int(payload["pdf_page"]),
                    "line_index": line_index,
                    "text": text,
                    "confidence": item.get("confidence"),
                }
            )
    return flattened


def is_next_part_heading(text: str) -> bool:
    """Return True for a short OCR heading that starts the following Part B.

    The phrase can occur inside ordinary publisher commentary, so this is only
    used for Text 4's final question and only when Part B begins the OCR line.
    """

    normalized = str(text).strip()
    return bool(
        re.match(
            r"^(?:[>〉7]\s*)?Part\s*B(?=$|[^A-Za-z0-9])",
            normalized,
            re.IGNORECASE,
        )
    )


def is_next_text_heading(text: str, text_no: int) -> bool:
    normalized = str(text).strip()
    return bool(
        re.match(
            rf"^(?:[>〉7]\s*)?Text\s*{text_no}(?=$|[^A-Za-z0-9])",
            normalized,
            re.IGNORECASE,
        )
    )


def normalize_key_entry(
    answer_keys: dict[str, Any], year: int, text_no: int, question_numbers: list[int]
) -> tuple[dict[int, str], dict[int, list[int]], str, str]:
    try:
        entry = answer_keys[str(year)][str(text_no)]
    except KeyError as exc:
        raise KeyError(f"missing verified answer key for {year} Text {text_no}") from exc
    status = "source_verified"
    method = "analysis_pdf_explicit_answer_or_visual_marking"
    evidence_pages: Any = None
    if isinstance(entry, list):
        answers = entry
    elif isinstance(entry, str):
        answers = list(re.sub(r"[^A-D]", "", entry.upper()))
    elif isinstance(entry, dict):
        answers = entry.get("answers", entry.get("answer_key", entry.get("letters")))
        evidence_pages = entry.get("evidence_pages", entry.get("pages"))
        status = entry.get("verification_status", entry.get("status", status))
        method = entry.get("verification_method", entry.get("method", method))
    else:
        raise TypeError(f"unsupported answer key entry for {year} Text {text_no}: {type(entry)}")

    if isinstance(answers, dict):
        answer_map = {int(key): str(value).upper() for key, value in answers.items()}
    else:
        answer_list = [str(value).upper() for value in answers]
        if len(answer_list) != 5:
            raise ValueError(f"{year} Text {text_no}: expected 5 answers, got {answer_list}")
        answer_map = dict(zip(question_numbers, answer_list))
    if set(answer_map) != set(question_numbers) or any(value not in {"A", "B", "C", "D"} for value in answer_map.values()):
        raise ValueError(f"{year} Text {text_no}: invalid answer map {answer_map}")

    page_map: dict[int, list[int]] = {number: [] for number in question_numbers}
    if isinstance(evidence_pages, dict):
        for key, value in evidence_pages.items():
            if value is None:
                page_map[int(key)] = []
            elif isinstance(value, list):
                page_map[int(key)] = [int(page) for page in value]
            else:
                page_map[int(key)] = [int(value)]
    elif isinstance(evidence_pages, list) and len(evidence_pages) == 5:
        page_map.update(
            dict(zip(question_numbers, [[int(value)] if value is not None else [] for value in evidence_pages]))
        )
    return answer_map, page_map, str(status), str(method)


def build_text_record(
    *,
    year: int,
    text_no: int,
    answer_keys: dict[str, Any],
    refresh_ocr: bool,
    dpi: int,
    generated_date: str,
) -> dict[str, Any]:
    practice = load_practice_record(year, text_no)
    source_pdf = Path(str(practice["analysis_pdf"]))
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    source_hash = sha256(source_pdf)
    key_entry = answer_keys[str(year)][str(text_no)]
    if isinstance(key_entry, dict):
        audited_hash = key_entry.get("source_sha256")
        audited_pdf = key_entry.get("source_pdf")
        if audited_hash and audited_hash != source_hash:
            raise ValueError(f"{year} Text {text_no}: answer audit source hash drift")
        if audited_pdf and Path(str(audited_pdf)) != source_pdf:
            raise ValueError(f"{year} Text {text_no}: answer audit source PDF mismatch")
    total_pages = pdf_page_count(source_pdf)
    anchor_start, anchor_end = parse_range(str(practice["analysis_page_range"]))
    question_numbers = EXPECTED_QUESTIONS[text_no]
    answer_map, evidence_page_map, verification_status, verification_method = normalize_key_entry(
        answer_keys, year, text_no, question_numbers
    )
    # Include one boundary page on each side. Several books begin the next Text
    # at the foot of the previous page, or continue the final explanation onto
    # the page immediately after the old anchor range.
    source_start = max(1, anchor_start - 1)
    source_end = min(total_pages, anchor_end + 1)
    pages = sorted(
        set(range(source_start, source_end + 1))
        | {
            page
            for evidence_pages in evidence_page_map.values()
            for page in evidence_pages
            if 1 <= page <= total_pages
        }
    )
    page_payloads = [
        render_and_ocr_page(
            year=year,
            page=page,
            source_pdf=source_pdf,
            source_hash=source_hash,
            dpi=dpi,
            refresh=refresh_ocr,
            generated_date=generated_date,
        )
        for page in pages
    ]
    lines = flatten_page_lines(page_payloads)
    anchors: dict[int, tuple[int, float]] = {}
    next_min_index = 0
    for question in practice["questions"]:
        number = int(question["number"])
        anchor = find_question_anchor(
            str(question["prompt"]),
            number,
            lines,
            min_index=next_min_index,
            preferred_pages=evidence_page_map.get(number),
        )
        anchors[number] = anchor
        next_min_index = anchor[0] + 1
    ordered = sorted((index, number, score) for number, (index, score) in anchors.items())
    if [number for _, number, _ in ordered] != question_numbers:
        raise ValueError(f"{year} Text {text_no}: OCR question anchors out of order: {ordered}")

    questions: list[dict[str, Any]] = []
    for position, (start_index, number, score) in enumerate(ordered):
        if position + 1 < len(ordered):
            end_index = ordered[position + 1][0]
        else:
            end_index = len(lines)
            next_text = text_no + 1
            if next_text <= 4:
                for candidate in range(start_index + 1, len(lines)):
                    if is_next_text_heading(str(lines[candidate]["text"]), next_text):
                        end_index = candidate
                        break
            else:
                for candidate in range(start_index + 1, len(lines)):
                    if is_next_part_heading(str(lines[candidate]["text"])):
                        end_index = candidate
                        break
        block = lines[start_index:end_index]
        block_text = "\n".join(str(item["text"]) for item in block).strip()
        block_pages = sorted({int(item["page"]) for item in block})
        if len(block_text) < 120:
            raise ValueError(f"{year} Text {text_no} Q{number}: analysis block too short ({len(block_text)})")
        answer_evidence_pages = evidence_page_map.get(number) or (block_pages[:1] if block_pages else [])
        evidence_page = answer_evidence_pages[-1] if answer_evidence_pages else None
        questions.append(
            {
                "question_number": number,
                "correct_answer": answer_map[number],
                "answer_evidence_page": evidence_page,
                "answer_evidence_pages": answer_evidence_pages,
                "analysis_source_pages": block_pages,
                "analysis_text": block_text,
                "anchor_score": score,
                "verification_status": verification_status,
                "verification_method": verification_method,
            }
        )

    full_sections = []
    source_page_paths = []
    for payload in page_payloads:
        page = int(payload["pdf_page"])
        full_sections.append(f"### PDF p{page}\n\n{payload['vision_ocr_text']}")
        source_page_paths.append(str(page_md_path(year, page).relative_to(ROOT)))
    full_text = "\n\n".join(full_sections).strip()
    record = {
        "schema": SCHEMA,
        "generated_date": generated_date,
        "analysis_id": f"EXAM-ANALYSIS-{year}-T{text_no}",
        "reference_id": practice["reference_id"],
        "year": year,
        "exam": "考研英语一",
        "section": "Section II Reading Comprehension Part A",
        "text_no": text_no,
        "question_numbers": question_numbers,
        "source_pdf": str(source_pdf),
        "source_sha256": source_hash,
        "old_analysis_anchor_range": practice["analysis_page_range"],
        "preprocessed_source_pages": pages,
        "source_page_paths": source_page_paths,
        "visibility": "hidden_until_review",
        "answer_key": {str(number): answer_map[number] for number in question_numbers},
        "questions": questions,
        "full_source_analysis_text": full_text,
        "extraction_status": "preprocessed_complete",
        "quality": {
            "answer_count": len(answer_map),
            "question_analysis_count": len(questions),
            "nonempty_question_analysis_count": sum(bool(item["analysis_text"].strip()) for item in questions),
            "source_page_count": len(pages),
            "full_source_analysis_characters": len(full_text),
            "minimum_anchor_score": min(score for _, _, score in ordered),
            "vision_ocr_dpi": dpi,
            "source_verified_answers": True,
        },
    }
    write_json(text_json_path(year, text_no), record)
    write_text_markdown(record)
    return record


def write_text_markdown(record: dict[str, Any]) -> None:
    year = int(record["year"])
    text_no = int(record["text_no"])
    lines = [
        "---",
        f"analysis_id: {record['analysis_id']}",
        f"reference_id: {record['reference_id']}",
        f"year: {year}",
        f"text_no: {text_no}",
        "visibility: hidden_until_review",
        "extraction_status: preprocessed_complete",
        "source_role: protected_derived_source",
        "---",
        "",
        f"# {year} English I Text {text_no}｜答案与解析预处理",
        "",
        "> [!danger] 自主练习阶段禁止展开",
        "> 本文件已经保存标准答案和出版方解析转写。用户只陈述选项时不读取；只有明确要求核对答案、讲题或进入复盘后，才读取对应题号的最小区块。",
        "",
        f"- 来源 PDF：`{record['source_pdf']}`",
        f"- 来源 SHA-256：`{record['source_sha256']}`",
        f"- 旧锚点：p{record['old_analysis_anchor_range']}",
        f"- 实际预处理页：p{record['preprocessed_source_pages'][0]}–{record['preprocessed_source_pages'][-1]}",
        f"- 逐页证据：{len(record['source_page_paths'])} 页",
        "",
        "<details>",
        "<summary>答案与解析（仅复盘时展开）</summary>",
        "",
        "## 标准答案",
        "",
        "| 题号 | 正确选项 | 来源页 | 核验状态 |",
        "|---:|:---:|---:|---|",
    ]
    for item in record["questions"]:
        lines.append(
            f"| {item['question_number']} | {item['correct_answer']} | p{item['answer_evidence_page']} | {item['verification_status']} |"
        )
    lines.extend(["", "## 逐题解析", ""])
    for item in record["questions"]:
        page_label = ", ".join(f"p{page}" for page in item["analysis_source_pages"])
        evidence_label = ", ".join(f"p{page}" for page in item["answer_evidence_pages"])
        lines.extend(
            [
                f"### Q{item['question_number']}",
                "",
                f"- 正确选项：{item['correct_answer']}",
                f"- 答案核验页：{evidence_label}",
                f"- 解析来源页：{page_label}",
                f"- 核验：{item['verification_status']} / {item['verification_method']}",
                "",
                "```text",
                item["analysis_text"],
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## 完整出版方解析 OCR",
            "",
            record["full_source_analysis_text"],
            "",
            "</details>",
            "",
            "## 读取边界",
            "",
            "- 本页不得用于自主练习、题干翻译或选项含义讨论。",
            "- 用户解锁后优先读取对应 Q 区块；若 OCR 文字存在歧义，再查看同页逐页证据的 PDF 文字层或外部视觉原页。",
            "- 本次预处理不写 `master_bank.csv`、句式卡、Tutor 或 review 文件。",
            "",
        ]
    )
    out = text_md_path(year, text_no)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


def build_index(generated_date: str) -> dict[str, Any]:
    records = []
    for year in range(2010, 2025):
        for text_no in range(1, 5):
            path = text_json_path(year, text_no)
            if path.is_file():
                records.append(json.loads(path.read_text(encoding="utf-8")))
    payload = {
        "schema": "exam_reading_protected_analysis_index_v1",
        "generated_date": generated_date,
        "visibility": "hidden_until_review",
        "record_count": len(records),
        "question_count": sum(len(record.get("questions", [])) for record in records),
        "records": [
            {
                "analysis_id": record["analysis_id"],
                "reference_id": record["reference_id"],
                "year": record["year"],
                "text_no": record["text_no"],
                "question_numbers": record["question_numbers"],
                "json_path": str(text_json_path(int(record["year"]), int(record["text_no"])).relative_to(ROOT)),
                "markdown_path": str(text_md_path(int(record["year"]), int(record["text_no"])).relative_to(ROOT)),
                "source_pages": record["preprocessed_source_pages"],
                "status": record["extraction_status"],
            }
            for record in records
        ],
    }
    write_json(OUTPUT_ROOT / "index.json", payload)
    md = [
        "---",
        "visibility: hidden_until_review",
        "source_role: protected_answer_analysis_index",
        "---",
        "",
        "# 2010–2024 阅读答案与解析受保护索引",
        "",
        "> [!danger] 自主练习阶段禁止读取",
        "> 本索引仅证明答案与解析已经预处理落盘。用户只陈述选项时不读取；只有明确要求核对答案、讲题或进入复盘后，才按题号打开对应文件。",
        "",
        f"- 逐篇记录：{payload['record_count']} / 60",
        f"- 逐题记录：{payload['question_count']} / 300",
        "",
        "| 年份 | Text | 题号 | 受保护文件 | 预处理页 | 状态 |",
        "|---:|---:|---|---|---|---|",
    ]
    for record in payload["records"]:
        nums = record["question_numbers"]
        page_values = record["source_pages"]
        rel = Path(str(record["markdown_path"]))
        link = rel.relative_to(OUTPUT_ROOT.relative_to(ROOT)).with_suffix("")
        md.append(
            f"| {record['year']} | Text {record['text_no']} | {nums[0]}–{nums[-1]} | "
            f"[[{link.as_posix()}|{record['analysis_id']}]] | p{page_values[0]}–{page_values[-1]} | {record['status']} |"
        )
    md.extend(
        [
            "",
            "## 快速读取规则",
            "",
            "1. 先以年份 + Text + 题号定位本索引。",
            "2. 只读取对应文件中的单个 Q 区块，不先展开整篇答案键。",
            "3. OCR 有歧义时读取逐页证据；仍不清楚才回到外部 PDF 视觉原页。",
            "",
        ]
    )
    (OUTPUT_ROOT / "index.md").write_text("\n".join(md), encoding="utf-8")
    return payload


def verify() -> dict[str, Any]:
    failures: list[str] = []
    records = []
    answer_ids = set()
    verified_key_path = OUTPUT_ROOT / "verified_answer_keys.json"
    if not verified_key_path.is_file():
        raise SystemExit(f"missing {verified_key_path.relative_to(ROOT)}")
    verified_payload = json.loads(verified_key_path.read_text(encoding="utf-8"))
    if verified_payload.get("visibility") != "hidden_until_review":
        failures.append("verified answer key visibility is not protected")
    verified_keys = verified_payload.get("answer_keys", verified_payload)
    manifest_path = ROOT / "raw" / "reference_sources" / "exam_pdf_manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"missing {manifest_path.relative_to(ROOT)}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    analysis_sources = {
        int(item["year"]): item
        for item in manifest.get("sources", [])
        if item.get("role") == "answer_analysis_hidden_source"
    }
    if set(analysis_sources) != set(range(2010, 2025)):
        failures.append(
            f"analysis source manifest years={sorted(analysis_sources)}"
        )
    for year in range(2010, 2025):
        for text_no in range(1, 5):
            path = text_json_path(year, text_no)
            if not path.is_file():
                failures.append(f"missing {path.relative_to(ROOT)}")
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            records.append(record)
            expected = EXPECTED_QUESTIONS[text_no]
            if record.get("visibility") != "hidden_until_review":
                failures.append(f"{year} T{text_no}: bad visibility")
            if record.get("extraction_status") != "preprocessed_complete":
                failures.append(f"{year} T{text_no}: bad extraction status")
            if record.get("question_numbers") != expected:
                failures.append(f"{year} T{text_no}: question numbers {record.get('question_numbers')}")
            answer_key = record.get("answer_key", {})
            if set(map(int, answer_key)) != set(expected) or any(value not in {"A", "B", "C", "D"} for value in answer_key.values()):
                failures.append(f"{year} T{text_no}: bad answer key")
            expected_answers, expected_pages, _, _ = normalize_key_entry(
                verified_keys, year, text_no, expected
            )
            if {int(key): value for key, value in answer_key.items()} != expected_answers:
                failures.append(f"{year} T{text_no}: answer key differs from verified audit")
            verified_entry = verified_keys[str(year)][str(text_no)]
            if record.get("source_sha256") != verified_entry.get("source_sha256"):
                failures.append(f"{year} T{text_no}: source hash differs from answer audit")
            manifest_source = analysis_sources.get(year)
            if manifest_source:
                if record.get("source_pdf") != manifest_source.get("external_path"):
                    failures.append(f"{year} T{text_no}: source PDF differs from manifest")
                if record.get("source_sha256") != manifest_source.get("sha256"):
                    failures.append(f"{year} T{text_no}: source hash differs from manifest")
            if not text_md_path(year, text_no).is_file():
                failures.append(f"missing {text_md_path(year, text_no).relative_to(ROOT)}")
            questions = record.get("questions", [])
            if len(questions) != 5:
                failures.append(f"{year} T{text_no}: question analysis count={len(questions)}")
            for item in questions:
                qid = (year, int(item.get("question_number", -1)))
                if qid in answer_ids:
                    failures.append(f"duplicate question {qid}")
                answer_ids.add(qid)
                if len(str(item.get("analysis_text", "")).strip()) < 120:
                    failures.append(f"{year} T{text_no} Q{item.get('question_number')}: empty/short analysis")
                if text_no == 4 and int(item.get("question_number", -1)) == 40:
                    if any(is_next_part_heading(line) for line in str(item.get("analysis_text", "")).splitlines()):
                        failures.append(f"{year} T{text_no} Q40: next Part B leaked into analysis block")
                if text_no < 4 and int(item.get("question_number", -1)) == max(expected):
                    if any(
                        is_next_text_heading(line, text_no + 1)
                        for line in str(item.get("analysis_text", "")).splitlines()
                    ):
                        failures.append(
                            f"{year} T{text_no} Q{max(expected)}: next Text leaked into analysis block"
                        )
                if item.get("correct_answer") != answer_key.get(str(item.get("question_number"))):
                    failures.append(f"{year} T{text_no} Q{item.get('question_number')}: answer mismatch within record")
                if not item.get("analysis_source_pages") or not item.get("answer_evidence_page"):
                    failures.append(f"{year} T{text_no} Q{item.get('question_number')}: missing source page")
                number = int(item.get("question_number", -1))
                if item.get("answer_evidence_pages") != expected_pages.get(number):
                    failures.append(f"{year} T{text_no} Q{number}: evidence pages differ from verified audit")
                preprocessed_pages = set(record.get("preprocessed_source_pages", []))
                if not set(item.get("analysis_source_pages", [])).issubset(preprocessed_pages):
                    failures.append(f"{year} T{text_no} Q{number}: analysis pages not preprocessed")
                if not set(item.get("answer_evidence_pages", [])).issubset(preprocessed_pages):
                    failures.append(f"{year} T{text_no} Q{number}: answer evidence pages not preprocessed")
                if float(item.get("anchor_score", 0)) < 0.34:
                    failures.append(f"{year} T{text_no} Q{number}: weak/missing question anchor")
                if not str(item.get("verification_status", "")).startswith("source_verified"):
                    failures.append(f"{year} T{text_no} Q{item.get('question_number')}: unverified answer")
            for rel in record.get("source_page_paths", []):
                if not (ROOT / rel).is_file():
                    failures.append(f"{year} T{text_no}: missing page evidence {rel}")
                else:
                    page_json = (ROOT / rel).with_suffix(".json")
                    if not page_json.is_file():
                        failures.append(f"{year} T{text_no}: missing page JSON {page_json.relative_to(ROOT)}")
                    else:
                        page_payload = json.loads(page_json.read_text(encoding="utf-8"))
                        if not page_payload.get("quality", {}).get("nonempty"):
                            failures.append(f"{year} T{text_no}: empty page OCR {page_json.relative_to(ROOT)}")
                        if page_payload.get("source_sha256") != record.get("source_sha256"):
                            failures.append(f"{year} T{text_no}: page evidence source hash mismatch {page_json.relative_to(ROOT)}")
            if len(str(record.get("full_source_analysis_text", ""))) < 500:
                failures.append(f"{year} T{text_no}: full analysis text too short")

    source_file_failures = []
    for year, item in sorted(analysis_sources.items()):
        path = Path(str(item.get("external_path", "")))
        if not path.is_file():
            source_file_failures.append(f"{year}:missing")
        elif sha256(path) != item.get("sha256"):
            source_file_failures.append(f"{year}:hash")
    if source_file_failures:
        failures.append(f"analysis source files drift={source_file_failures}")

    # Practice-safe layer must remain free of answer content.
    for path in sorted(CORPUS_ROOT.glob("20*/text-*.json")):
        practice = json.loads(path.read_text(encoding="utf-8"))
        forbidden = {"answer", "answers", "answer_key", "correct_answer", "standard_answer", "explanation", "analysis_text"}
        if forbidden & set(practice):
            failures.append(f"practice answer leak: {path.relative_to(ROOT)}")
        for question in practice.get("questions", []):
            if forbidden & set(question):
                failures.append(f"practice question answer leak: {path.relative_to(ROOT)} Q{question.get('number')}")

    index_path = OUTPUT_ROOT / "index.json"
    if not index_path.is_file():
        failures.append(f"missing {index_path.relative_to(ROOT)}")
    else:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if index.get("record_count") != 60 or index.get("question_count") != 300:
            failures.append(
                f"protected index counts records={index.get('record_count')} questions={index.get('question_count')}"
            )
    if not (OUTPUT_ROOT / "index.md").is_file():
        failures.append(f"missing {(OUTPUT_ROOT / 'index.md').relative_to(ROOT)}")

    if failures:
        raise SystemExit("\n".join(failures))
    referenced_page_paths = {
        rel
        for record in records
        for rel in record.get("source_page_paths", [])
    }
    return {
        "structural_gate": "PASS",
        "records": len(records),
        "questions": sum(len(record["questions"]) for record in records),
        "answers": sum(len(record["answer_key"]) for record in records),
        "question_analysis_nonempty": sum(
            bool(item["analysis_text"].strip()) for record in records for item in record["questions"]
        ),
        "source_page_evidence_files": len(list(PAGE_ROOT.glob("20*/page-*.json"))),
        "referenced_source_page_evidence_files": len(referenced_page_paths),
        "verified_answer_key_gate": "PASS",
        "analysis_source_manifest_gate": "15/15 PASS",
        "answer_leakage_gate": "PASS",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--end-year", type=int, default=2024)
    parser.add_argument("--text-no", type=int, choices=(1, 2, 3, 4))
    parser.add_argument("--answer-keys", type=Path)
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--refresh-ocr", action="store_true")
    parser.add_argument("--build-index", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    if args.verify_only:
        print(json.dumps(verify(), ensure_ascii=False, indent=2))
        return
    if args.build_index and args.answer_keys is None:
        print(json.dumps(build_index(args.date), ensure_ascii=False, indent=2))
        return
    if args.answer_keys is None:
        parser.error("--answer-keys is required when building Text records")
    if not (2010 <= args.start_year <= args.end_year <= 2024):
        parser.error("year range must be within 2010-2024")
    ensure_tools()
    key_payload = json.loads(args.answer_keys.read_text(encoding="utf-8"))
    keys = key_payload.get("answer_keys", key_payload)
    built = []
    for year in range(args.start_year, args.end_year + 1):
        text_numbers = [args.text_no] if args.text_no else list(range(1, 5))
        for text_no in text_numbers:
            record = build_text_record(
                year=year,
                text_no=text_no,
                answer_keys=keys,
                refresh_ocr=args.refresh_ocr,
                dpi=args.dpi,
                generated_date=args.date,
            )
            built.append(
                {
                    "analysis_id": record["analysis_id"],
                    "questions": len(record["questions"]),
                    "source_pages": len(record["preprocessed_source_pages"]),
                    "minimum_anchor_score": record["quality"]["minimum_anchor_score"],
                }
            )
            print(
                json.dumps(
                    {"status": "built", **built[-1]}, ensure_ascii=False
                ),
                flush=True,
            )
    if args.build_index:
        build_index(args.date)
    print(json.dumps({"built": len(built), "year_range": [args.start_year, args.end_year]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
