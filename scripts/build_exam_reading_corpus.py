#!/usr/bin/env python3
"""Build the private, practice-safe English-I Part A reading corpus.

The official exam-paper PDFs are the authority for passages, questions, and
options. Answer/explanation content is built separately by
build_exam_reading_analysis.py into a protected hidden-review layer. This
script never reads or writes bank/master_bank.csv and never exposes answer keys
in generated article pages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
EXAM_ROOT = Path(
    "/Users/your-user/Desktop/1. 1980-2025考研英一真题+解析/"
    "03、2010-2024年考研英语真题+解析/2010-2024年考研英语一真题"
)
ANALYSIS_ROOT = Path(
    "/Users/your-user/Desktop/1. 1980-2025考研英一真题+解析/"
    "03、2010-2024年考研英语真题+解析/2010-2024年考研英语一解析"
)

TEXTS = (1, 2, 3, 4)
EXPECTED_QUESTIONS = {
    1: range(21, 26),
    2: range(26, 31),
    3: range(31, 36),
    4: range(36, 41),
}

QUESTION_SPILL_PAGES = {(2022, 4): 1}

OCR_TEXT_CORRECTIONS = {
    "ofrecordings": "of recordings",
    "andjournalism": "and journalism",
    "strongresistance": "strong resistance",
    "ofresponsibility": "of responsibility",
    "Califomia": "California",
    "in tum": "in turn",
    "isunknown": "is unknown",
    "newreality": "new reality",
    "ofastronomy": "of astronomy",
    "hertime": "her time",
    "feelgood": "feel-good",
    "2'/2": "2½",
    "frorn": "from",
    "Penguin Random , House": "Penguin Random House",
    "Paragraph i": "Paragraph 1",
    "Paragraph |": "Paragraph 1",
    "paragraph |": "paragraph 1",
    "20\" century": "20th century",
    "Jn re Bilski": "In re Bilski",
    "moreconcessions": "more concessions",
    "spurredby": "spurred by",
    "Itis suggested": "It is suggested",
    "Szent-Gy6érgyi": "Szent-Györgyi",
    "howto": "how to",
    "acknowledged —_ by": "acknowledged by",
    "Tt’s": "It’s",
    "oneof": "one of",
    "neededto": "needed to",
    "investing,’": "investing,”",
    "short-termism”’": "short-termism”",
    "bealigned": "be aligned",
    "rulehas": "rule has",
    "Géilardi": "Gilardi",
    "deploy AI’": "deploy AI”",
    "AlI-directed": "AI-directed",
    "Al-generated": "AI-generated",
    "Al companies": "AI companies",
    ". a It comes": ". It comes",
    "New Hampshire,.where": "New Hampshire, where",
    "percent, ‘housing advocates": "percent, housing advocates",
    "ways of ‘delivering": "ways of delivering",
    "who are Statistically literate": "who are statistically literate",
    "Taylor said. - However": "Taylor said. However",
    "end print,’": "end print,”",
    "reader error,’": "reader error,”",
    "distributed trust’.": "distributed trust”.",
    "French companies” interests": "French companies’ interests",
    "T love My Children": "I love My Children",
    "entrepreneurs.These": "entrepreneurs. These",
    "result.While": "result. While",
    "profits”? But": "profits.” But",
    "No.2 executives": "No. 2 executives",
    "SeditionActs": "Sedition Acts",
    "SocialMedia": "Social Media",
}

EXISTING_ARTICLES = {
    (2010, 4): "articles/2026-05-27-2010-english-i-text-4.md",
    (2011, 1): "articles/2026-06-21-alan-gilbert-philharmonic.md",
    (2011, 2): "articles/2026-06-22-2011-english-i-text-2.md",
    (2011, 3): "articles/2026-07-03-2011-english-i-text-3.md",
    (2011, 4): "articles/2026-07-10-2011-english-i-text-4.md",
}

EXISTING_SOURCE_IDS = {
    (2011, 3): "RAW-ARTICLE-20260703-001",
    (2011, 4): "RAW-ARTICLE-20260710-001",
}

# Ranges are page anchors in the explanation PDFs, not answer disclosure.
# The 2022 and 2024 scan-only editions were located with page-level OCR and
# visually checked at each Text boundary; OCR content itself is never copied
# into the practice-safe article pages.
ANALYSIS_RANGES: dict[int, dict[int, tuple[str, str]]] = {
    2010: {1: ("9-15", "text_anchor"), 2: ("19-25", "text_anchor"), 3: ("27-35", "text_anchor"), 4: ("37-44", "text_anchor")},
    2011: {1: ("8-15", "text_anchor"), 2: ("17-23", "text_anchor"), 3: ("26-33", "text_anchor"), 4: ("35-42", "text_anchor")},
    2012: {1: ("9-16", "approximate_text_anchor"), 2: ("17-25", "text_anchor"), 3: ("27-34", "text_anchor"), 4: ("36-44", "text_anchor")},
    2013: {1: ("8-16", "text_anchor"), 2: ("18-27", "approximate_text_anchor"), 3: ("29-38", "approximate_text_anchor"), 4: ("40-45", "text_anchor")},
    2014: {1: ("9-16", "text_anchor"), 2: ("19-25", "text_anchor"), 3: ("27-34", "text_anchor"), 4: ("36-44", "text_anchor")},
    2015: {1: ("12-20", "text_anchor"), 2: ("21-30", "text_anchor"), 3: ("32-39", "text_anchor"), 4: ("42-49", "text_anchor")},
    2016: {1: ("10-18", "text_anchor"), 2: ("20-29", "text_anchor"), 3: ("32-40", "text_anchor"), 4: ("44-51", "text_anchor")},
    2017: {1: ("11-18", "text_anchor"), 2: ("20-28", "text_anchor"), 3: ("30-39", "text_anchor"), 4: ("41-49", "text_anchor")},
    2018: {1: ("11-19", "text_anchor"), 2: ("21-29", "text_anchor"), 3: ("31-38", "text_anchor"), 4: ("41-48", "text_anchor")},
    2019: {1: ("11-17", "text_anchor"), 2: ("19-25", "text_anchor"), 3: ("27-36", "text_anchor"), 4: ("38-46", "text_anchor")},
    2020: {1: ("10-16", "text_anchor"), 2: ("19-25", "text_anchor"), 3: ("27-33", "text_anchor"), 4: ("35-42", "text_anchor")},
    2021: {1: ("10-16", "text_anchor"), 2: ("19-25", "text_anchor"), 3: ("27-34", "text_anchor"), 4: ("36-43", "text_anchor")},
    2022: {1: ("16-24", "scan_ocr_text_anchor"), 2: ("25-33", "scan_ocr_text_anchor"), 3: ("34-42", "scan_ocr_text_anchor"), 4: ("43-51", "scan_ocr_text_anchor")},
    2023: {1: ("10-12", "text_anchor"), 2: ("14-16", "text_anchor"), 3: ("18-20", "text_anchor"), 4: ("22-24", "text_anchor")},
    2024: {1: ("10-14", "scan_ocr_text_anchor"), 2: ("15-19", "scan_ocr_text_anchor"), 3: ("20-24", "scan_ocr_text_anchor"), 4: ("25-30", "scan_ocr_text_anchor")},
}


@dataclass
class Line:
    text: str
    page: int


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True, errors="replace")
    return proc.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_pages(path: Path) -> int:
    info = run(["pdfinfo", str(path)])
    match = re.search(r"(?m)^Pages:\s+(\d+)\s*$", info)
    if not match:
        raise RuntimeError(f"Cannot read page count: {path}")
    return int(match.group(1))


def truth_page_pair(year: int, text_no: int) -> tuple[int, int]:
    start = 3 if year == 2023 else 4
    passage = start + (text_no - 1) * 2
    return passage, passage + 1


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00ad", "-").replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"(?m)^(\s*)([234])\s+([0-9])(\s*[.。])", r"\1\2\3\4", value)
    value = re.sub(r"\[\s*[qQ]\s*\]", "[C]", value)
    value = re.sub(r"\{\s*([A-D])\s*\]", r"[\1]", value)
    value = re.sub(r"\[\s*([A-D])\s*\}", r"[\1]", value)
    value = re.sub(r"(?m)^\s*\|\s*([A-D])\s*[\}\]]", r"[\1]", value)
    value = re.sub(r"(?m)^(\s*[A-D])\s*[.．]\s*[-—]\s*", r"\1. ", value)
    for bad, good in OCR_TEXT_CORRECTIONS.items():
        value = value.replace(bad, good)
    return value


def embedded_page(path: Path, page: int) -> str:
    return normalize_text(
        run(["pdftotext", "-f", str(page), "-l", str(page), "-layout", "-enc", "UTF-8", str(path), "-"])
    )


def ocr_page(path: Path, year: int, page: int, *, refresh: bool = False) -> str:
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract is required for visual OCR")
    cache = ROOT / "raw" / "reference_sources" / "exam_page_ocr" / str(year) / f"page-{page:02d}.txt"
    if cache.exists() and not refresh:
        return normalize_text(cache.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="english_exam_ocr_") as tmp:
        prefix = Path(tmp) / "page"
        subprocess.run(
            ["pdftoppm", "-f", str(page), "-l", str(page), "-r", "300", "-png", "-singlefile", str(path), str(prefix)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result = normalize_text(
            run(["tesseract", str(prefix.with_suffix(".png")), "stdout", "-l", "eng", "--psm", "6"])
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(result, encoding="utf-8")
        return result


def extract_page(path: Path, year: int, page: int, *, refresh_ocr: bool = False) -> tuple[str, str]:
    return ocr_page(path, year, page, refresh=refresh_ocr), "visual_ocr_eng_300dpi"


def is_noise(line: str, year: int) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    patterns = [
        r"^Text\s*[1-4]$",
        r"^-\s*\d+\s*-$",
        r"^\d{4}\s*-\s*\d+$",
        r"^英语\s*\(?一\)?.*(共|页|试题)",
        r"^Section\s+II",
        r"^Part\s+[AB]$",
        r"^Directions\s*:?$",
        r"^Read the following four texts\b",
        r"^[A-D]\s+or\s+[A-D].*ANSWER SHEET",
        r"^[RH][A-Za-z]{1,4}\s*\(.*(?:#?\s*1[45]|4(?:14|15))\b.*$",
    ]
    return any(re.search(pattern, stripped, flags=re.I) for pattern in patterns)


def clean_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"\s+", " ", line)
    line = re.sub(r"\s+[\|_]\s*$", "", line)
    line = re.sub(r"\s+[\|_]\s+", " ", line)
    return line


def sanitize_fragment(value: str, *, prompt: bool = False) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    for bad, good in OCR_TEXT_CORRECTIONS.items():
        value = value.replace(bad, good)
    value = re.sub(r"([.!?])\s+[a-z]\s*$", r"\1", value)
    value = re.sub(r"\s+([,.;:?!])", r"\1", value)
    value = re.sub(r",\.\s*", ", ", value)
    value = re.sub(r"([“‘])\s+", r"\1", value)
    value = re.sub(r"\s+([”’])", r"\1", value)
    value = value.replace("—-", "—").replace("-—", "—")
    value = re.sub(r"([.!?])\s*[\|;:.]+\s*$", r"\1", value)
    value = re.sub(r"\s+(?:\\?\||—)\s*$", "", value)
    value = re.sub(r"([.!?])\s+\d+\s*$", r"\1", value)
    value = value.replace("told me;", "told me:")
    value = value.replace("voices.. After", "voices. After")
    value = value.replace("Penguin Random, House", "Penguin Random House")
    if prompt:
        # Scanned question blanks sometimes survive as combinations such as
        # ``—_i«`` or ``_i``.  They are layout marks, not question content.
        value = re.sub(r"\s+[—_-]+(?:[iIl1]+)?[«»]?[.,:;]?\s*$", "", value)
        value = re.sub(r"\s*\|\s*$", "", value)
        value = re.sub(r"\s*[.,:;]+$", "", value)
    else:
        value = re.sub(r"[,:;]+$", "", value)
    return value.strip()


def append_joined(current: str, new: str) -> str:
    if not current:
        return new
    if current.endswith("-") and new and new[0].islower():
        return current + new
    return current + " " + new


def parse_passage(raw: str, year: int) -> list[str]:
    paragraphs: list[str] = []
    current = ""
    for original in raw.splitlines():
        if is_noise(original, year):
            continue
        stripped = original.strip()
        if not stripped:
            if current and re.search(r"[.!?]\s*[\"”’)]?\s*$", current):
                paragraphs.append(current)
                current = ""
            continue
        # OCR sometimes leaves a page footer or scan label that contains no real English prose.
        ascii_letters = sum(ch.isascii() and ch.isalpha() for ch in stripped)
        if ascii_letters < 4:
            continue
        indent = len(original) - len(original.lstrip())
        cleaned = clean_line(original)
        cleaned = re.sub(r"^[.\s]*Text\s*[1-4]\s*[,.:]*\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"^,\s+(?=[A-Z])", "", cleaned)
        if re.match(r"^Text\s*[1-4]$", cleaned, flags=re.I):
            continue
        if not cleaned:
            continue
        if current and re.match(r"^[-_]\s+(?=[a-z])", cleaned):
            cleaned = re.sub(r"^[-_]\s+", "", cleaned)
        if current and indent >= 4 and re.match(r"^[A-Z\"'“‘]", cleaned):
            paragraphs.append(current)
            current = cleaned
        else:
            current = append_joined(current, cleaned)
    if current:
        paragraphs.append(current)
    paragraphs = [sanitize_fragment(p) for p in paragraphs if sum(ch.isalpha() for ch in p) >= 4]
    if len(paragraphs) == 1:
        # The embedded text layer occasionally loses indentation; sentence-safe chunks are
        # preferable to one giant paragraph, while preserving every token in order.
        sentences = split_sentences(paragraphs[0])
        if len(sentences) >= 8:
            target = max(4, min(8, round(len(sentences) / 3)))
            size = max(2, round(len(sentences) / target))
            paragraphs = [" ".join(sentences[i : i + size]) for i in range(0, len(sentences), size)]
    return paragraphs


def payload_token_count(raw: str, year: int) -> int:
    tokens = []
    for line in raw.splitlines():
        if is_noise(line, year):
            continue
        cleaned = clean_line(line)
        cleaned = re.sub(r"^[.\s]*Text\s*[1-4]\s*[,.:]*\s*", "", cleaned, flags=re.I)
        tokens.extend(re.findall(r"[A-Za-z]+", cleaned))
    return len(tokens)


QUESTION_RE = re.compile(r"(?m)^\s*([234])\s*([0-9])\s*[.,:，。：]\s*")
OPTION_MARKED_RE = re.compile(
    r"^\s*[-_\|]?\s*(?:[\[\{\(]\s*([A-Da-dqQ])\s*[\]\}\)]?\s*|"
    r"([A-Da-d])\s*[.．]\s*)(.*)$"
)
OPTION_PLAIN_2023_RE = re.compile(r"^\s*[-_\|]?\s*([A-Da-d])\s+(.*)$")


def split_question_preface(raw: str, expected_first: int, year: int) -> tuple[str, str, int]:
    normalized = normalize_text(raw)
    for match in QUESTION_RE.finditer(normalized):
        number = int(match.group(1) + match.group(2))
        if number == expected_first:
            prefix = normalized[: match.start()]
            prefix_paragraphs = parse_passage(prefix, year)
            prefix_words = sum(len(paragraph.split()) for paragraph in prefix_paragraphs)
            return prefix, normalized[match.start() :], prefix_words
    raise ValueError(f"{year}: cannot split question-page preface before Q{expected_first}")


def parse_questions(raw: str, expected: Iterable[int], year: int) -> list[dict[str, object]]:
    expected = list(expected)
    normalized = normalize_text(raw)
    normalized = re.split(r"(?im)^\s*Part\s+B\s*$", normalized, maxsplit=1)[0]
    matches = list(QUESTION_RE.finditer(normalized))
    starts: dict[int, tuple[int, int]] = {}
    for match in matches:
        number = int(match.group(1) + match.group(2))
        if number in expected and number not in starts:
            starts[number] = (match.start(), match.end())
    missing = [number for number in expected if number not in starts]
    if missing:
        raise ValueError(f"{year}: missing question starts {missing}")
    ordered = sorted((number, *starts[number]) for number in expected)
    questions: list[dict[str, object]] = []
    for idx, (number, _start, content_start) in enumerate(ordered):
        end = ordered[idx + 1][1] if idx + 1 < len(ordered) else len(normalized)
        block = normalized[content_start:end]
        prompt_parts: list[str] = []
        options: dict[str, str] = {}
        active_option: str | None = None
        for original in block.splitlines():
            if is_noise(original, year):
                continue
            line = clean_line(original)
            if not line:
                continue
            marker = OPTION_MARKED_RE.match(line)
            plain_marker = OPTION_PLAIN_2023_RE.match(line) if not marker and year == 2023 else None
            if marker or plain_marker:
                if marker:
                    letter = (marker.group(1) or marker.group(2)).upper()
                    content = marker.group(3)
                else:
                    letter = plain_marker.group(1).upper()
                    content = plain_marker.group(2)
                if letter == "Q":
                    letter = "C"
                active_option = letter
                options[letter] = content.strip()
            elif active_option:
                options[active_option] = append_joined(options[active_option], line)
            else:
                prompt_parts.append(line)
        if set(options) != {"A", "B", "C", "D"}:
            raise ValueError(f"{year} Q{number}: option set {sorted(options)}")
        prompt = sanitize_fragment(" ".join(prompt_parts), prompt=True)
        clean_options = {letter: sanitize_fragment(options[letter]) for letter in "ABCD"}
        if len(prompt.split()) < 2:
            raise ValueError(f"{year} Q{number}: empty or suspicious prompt {prompt!r}")
        if any(not value for value in clean_options.values()):
            raise ValueError(f"{year} Q{number}: empty option")
        questions.append(
            {
                "number": number,
                "prompt": prompt,
                "options": clean_options,
            }
        )
    return questions


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=(?:[\"'“‘(]*[A-Z0-9]))", text)
    return [part.strip() for part in parts if part.strip()]


def source_id(year: int, text_no: int, ingest_date: date) -> str:
    if (year, text_no) in EXISTING_SOURCE_IDS:
        return EXISTING_SOURCE_IDS[(year, text_no)]
    sequence = (year - 2010) * 4 + text_no
    return f"RAW-ARTICLE-{ingest_date.strftime('%Y%m%d')}-{sequence:03d}"


def article_path(year: int, text_no: int, ingest_date: date) -> str:
    return EXISTING_ARTICLES.get(
        (year, text_no),
        f"articles/{ingest_date.isoformat()}-{year}-english-i-text-{text_no}.md",
    )


def analysis_pdf(year: int) -> Path:
    if year == 2021:
        return ANALYSIS_ROOT / "2021黄皮书真题解析（英语一）.pdf"
    return ANALYSIS_ROOT / f"{year}年考研英语一真题解析.pdf"


def truth_pdf(year: int) -> Path:
    return EXAM_ROOT / f"{year}年考研英语一真题.pdf"


def raw_json_path(year: int, text_no: int) -> Path:
    return ROOT / "raw" / "articles" / "exam-reading-corpus" / str(year) / f"text-{text_no}.json"


def protected_analysis_path(year: int, text_no: int) -> str:
    return f"raw/protected/exam-reading-analysis/{year}/text-{text_no}.md"


def markdown_article(record: dict[str, object], ingest_date: date) -> str:
    year = int(record["year"])
    text_no = int(record["text_no"])
    paragraphs = record["passage_paragraphs"]
    questions = record["questions"]
    sentences = []
    for paragraph_no, paragraph in enumerate(paragraphs, start=1):
        for sentence in split_sentences(str(paragraph)):
            sentences.append((paragraph_no, sentence))

    out = [
        f"# {year} English I Text {text_no}",
        f"date: {ingest_date.isoformat()}",
        "source: 用户提供真题 PDF",
        "practice_status: corpus_practice_safe",
        f"reference_id: EXAM-READING-{year}-T{text_no}",
        "",
        "## 基本信息",
        "",
        f"- source_id：{record['source_id']}",
        f"- 来源年份 / 试卷 / 篇章编号：{year} / 考研英语一 / Text {text_no}",
        "- 原文来源：待确认",
        f"- 真题主源：`{record['truth_pdf']}`，PDF p{record['truth_pages'][0]}–{record['truth_pages'][1]}",
        f"- raw_path：`{record['raw_path']}`",
        f"- 外部视觉权威源：`{record['analysis_pdf']}`，旧定位 PDF p{record['analysis_page_range']}（{record['analysis_anchor_status']}）",
        f"- 受保护答案解析：`{record['protected_analysis_path']}`；状态 `{record['analysis_preprocess_status']}`",
        "- 解析读取：答案、定位、逐题解析和错项说明已经预处理，但本页不展示；仅在用户明确要求核对答案、讲题或进入复盘时读取对应题号区块",
        "- 用户已有作答、错因或标注：未记录",
        "- 文章主题：待用户完成自主练习后补充",
        "- 段落结构：待用户完成自主练习后补充",
        "- 核心观点：待用户完成自主练习后补充",
        "- 高频词汇：待逐句精读时按用户实际问题记录",
        "- 重点长难句：待逐句精读时确认",
        "- 后续精翻过的句子链接：待补充",
        "",
        "## 自主练习读取规则",
        "",
        "- 本页只保存原文、题目、全部选项、句子索引和空白学习区。",
        "- 用户只问词义、句法、指代、段落或选项中文时，不读取受保护答案解析。",
        "- 用户只陈述自己的选项时仍不读取受保护解析；只有明确要求核对、答案、解析、复盘或讲题时，才读取本地预处理文件中的对应 Q 区块。",
        "- 只有本地 OCR / 文字层存在歧义时，才回到外部 PDF 视觉原页核查。",
        "- 本页不嵌入标准答案、正确选项、出版方翻译或逐题讲解。",
        "",
        "## 原文全文",
        "",
    ]
    for number, paragraph in enumerate(paragraphs, start=1):
        out.extend([f"### P{number}", "", str(paragraph), ""])
    out.extend(["## 题目与选项", "", "> 本区只保存题干和全部选项，不进行作答判定。", ""])
    for question in questions:
        out.extend([f"### {question['number']}.", "", str(question["prompt"]), ""])
        for letter in "ABCD":
            out.extend([f"[{letter}] {question['options'][letter]}", ""])
    out.extend(
        [
            "## 用户作答记录",
            "",
            "| 题号 | 用户选择 | 用户把握度 | 备注 | 状态 |",
            "|---|---|---|---|---|",
        ]
    )
    for question in questions:
        out.append(f"| {question['number']} | 未提交 | 未记录 | 待用户作答 | pending |")
    out.extend(
        [
            "",
            "## 句子编号 / 逐句精读区",
            "",
            "> 后续逐句精读调用 `kaoyan-english-intensive-reading`。当前只建立原句索引。",
            "",
            "| sid | 段落 | 原句 | 状态 | 精读记录 |",
            "|---|---|---|---|---|",
        ]
    )
    for idx, (paragraph_no, sentence) in enumerate(sentences, start=1):
        safe_sentence = sentence.replace("|", "\\|")
        out.append(f"| S{idx:02d} | P{paragraph_no} | {safe_sentence} | pending | 待补充 |")
    out.extend(
        [
            "",
            "## 题目逻辑区",
            "",
            "| 题号 | 用户当前问题 | 用户定位 | 用户排除过程 | 状态 |",
            "|---|---|---|---|---|",
        ]
    )
    for question in questions:
        out.append(f"| {question['number']} | 未记录 | 未记录 | 未记录 | pending |")
    out.extend(
        [
            "",
            "## Question Error Records",
            "",
            "| 题号 | 我的误选 / 倾向 | 主错因 | 关键证据句 | 复盘提醒 | 状态 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for question in questions:
        out.append(f"| {question['number']} | 未明确 | 待用户作答后记录 | 待补充 | 待补充 | pending |")
    out.extend(
        [
            "",
            "## 词句候选区",
            "",
            "### 本文词汇 / 词组候选",
            "",
            "尚未开始逐句精读；当前候选为空。后续只记录用户明确不会、误译或影响理解的词句。",
            "",
            "### 本文句型 / 长难句候选",
            "",
            "尚未开始逐句精读；当前候选为空。后续按实际结构问题补充。",
            "",
            "## Intake Log",
            "",
            f"- {ingest_date.isoformat()}：由真题 PDF 创建 practice_safe 语境；保存原文、题目、全部选项和句子索引；答案与解析位于受保护层；正式 bank、句式卡与 review 未写入。",
            "",
        ]
    )
    return "\n".join(out)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_manifest(years: Iterable[int]) -> list[dict[str, object]]:
    files: list[tuple[Path, str, int | None]] = []
    for year in years:
        files.append((truth_pdf(year), "exam_paper_authority", year))
        files.append((analysis_pdf(year), "answer_analysis_hidden_source", year))
    files.append((EXAM_ROOT / "2024年考研英语一真题~.pdf", "alternate_transcription_diff_only", 2024))
    files.append((ANALYSIS_ROOT / "2021年考研英语一真题解析.pdf", "visual_backup_duplicate_analysis", 2021))
    manifest = []
    for path, role, year in files:
        if not path.exists():
            raise FileNotFoundError(path)
        manifest.append(
            {
                "source_id": f"SRC-EXAM-{len(manifest) + 1:03d}",
                "year": year,
                "role": role,
                "external_path": str(path),
                "filename": path.name,
                "bytes": path.stat().st_size,
                "pages": pdf_pages(path),
                "sha256": sha256(path),
                "rights": "private_study_reference_only",
            }
        )
    return manifest


def build(args: argparse.Namespace) -> dict[str, object]:
    ingest_date = date.fromisoformat(args.date)
    years = list(range(args.start_year, args.end_year + 1))
    corpus: list[dict[str, object]] = []
    for year in years:
        truth = truth_pdf(year)
        analysis = analysis_pdf(year)
        if not truth.exists() or not analysis.exists():
            raise FileNotFoundError(f"Missing source for {year}: {truth} / {analysis}")
        for text_no in TEXTS:
            passage_page, questions_page = truth_page_pair(year, text_no)
            passage_raw, passage_method = extract_page(
                truth, year, passage_page, refresh_ocr=args.refresh_ocr
            )
            question_page_numbers = list(
                range(questions_page, questions_page + QUESTION_SPILL_PAGES.get((year, text_no), 0) + 1)
            )
            question_extracts = [
                extract_page(truth, year, page, refresh_ocr=args.refresh_ocr)
                for page in question_page_numbers
            ]
            questions_raw_full = "\n".join(text for text, _method in question_extracts)
            question_method = "+".join(dict.fromkeys(method for _text, method in question_extracts))
            question_prefix, questions_raw, question_prefix_words = split_question_preface(
                questions_raw_full, min(EXPECTED_QUESTIONS[text_no]), year
            )
            paragraphs = parse_passage(passage_raw + "\n\n" + question_prefix, year)
            questions = parse_questions(questions_raw, EXPECTED_QUESTIONS[text_no], year)
            if len(paragraphs) < 2:
                raise ValueError(f"{year} Text {text_no}: only {len(paragraphs)} passage paragraphs")
            words = sum(len(p.split()) for p in paragraphs)
            if words < 250:
                raise ValueError(f"{year} Text {text_no}: suspiciously short passage ({words} words)")
            source_token_count = payload_token_count(passage_raw + "\n" + question_prefix, year)
            final_token_count = len(re.findall(r"[A-Za-z]+", " ".join(paragraphs)))
            token_coverage = final_token_count / source_token_count if source_token_count else 0.0
            if token_coverage < 0.97:
                raise ValueError(
                    f"{year} Text {text_no}: source-token coverage only {token_coverage:.3f} "
                    f"({final_token_count}/{source_token_count})"
                )
            page_range, anchor_status = ANALYSIS_RANGES[year][text_no]
            raw_rel = f"raw/articles/exam-reading-corpus/{year}/text-{text_no}.json"
            record: dict[str, object] = {
                "schema": "exam_reading_practice_safe_v1",
                "reference_id": f"EXAM-READING-{year}-T{text_no}",
                "source_id": source_id(year, text_no, ingest_date),
                "ingest_date": ingest_date.isoformat(),
                "year": year,
                "exam": "考研英语一",
                "section": "Section II Reading Comprehension Part A",
                "text_no": text_no,
                "visibility": "practice_safe",
                "truth_pdf": str(truth),
                "truth_pages": [passage_page, question_page_numbers[-1]],
                "truth_extraction": {"passage": passage_method, "questions": question_method},
                "analysis_pdf": str(analysis),
                "analysis_page_range": page_range,
                "analysis_anchor_status": anchor_status,
                "answer_visibility": "hidden_until_review",
                "protected_analysis_path": protected_analysis_path(year, text_no),
                "analysis_preprocess_status": (
                    "preprocessed_complete"
                    if (ROOT / protected_analysis_path(year, text_no)).is_file()
                    else "not_built"
                ),
                "raw_path": raw_rel,
                "article_path": article_path(year, text_no, ingest_date),
                "passage_paragraphs": paragraphs,
                "questions": questions,
                "quality": {
                    "passage_words": words,
                    "paragraphs": len(paragraphs),
                    "source_token_count": source_token_count,
                    "final_token_count": final_token_count,
                    "source_token_coverage": round(token_coverage, 6),
                    "question_page_continuation_words_merged": question_prefix_words,
                    "question_numbers": [q["number"] for q in questions],
                    "options_per_question": [len(q["options"]) for q in questions],
                    "structural_gate": "ok",
                    "visual_verification": "sampled_special_years" if year in {2019, 2021, 2024} else "pending_sample",
                },
            }
            write_json(raw_json_path(year, text_no), record)
            corpus.append(record)

            article_abs = ROOT / str(record["article_path"])
            if (year, text_no) not in EXISTING_ARTICLES:
                if article_abs.exists() and not args.overwrite_generated:
                    raise FileExistsError(f"Refusing to overwrite existing article: {article_abs}")
                article_abs.parent.mkdir(parents=True, exist_ok=True)
                article_abs.write_text(markdown_article(record, ingest_date), encoding="utf-8")

    manifest_path = ROOT / "raw" / "reference_sources" / "exam_pdf_manifest.json"
    write_json(
        manifest_path,
        {
            "schema": "private_pdf_source_manifest_v1",
            "generated_date": ingest_date.isoformat(),
            "source_count": 32,
            "sources": build_manifest(years),
        },
    )
    write_indexes(corpus, ingest_date)
    return {
        "years": len(years),
        "records": len(corpus),
        "new_articles": len([r for r in corpus if (r["year"], r["text_no"]) not in EXISTING_ARTICLES]),
        "existing_articles_preserved": len([r for r in corpus if (r["year"], r["text_no"]) in EXISTING_ARTICLES]),
        "manifest": str(manifest_path.relative_to(ROOT)),
    }


def write_indexes(corpus: list[dict[str, object]], ingest_date: date) -> None:
    raw_lines = [
        "# 2010–2024 考研英语一阅读原始抽取子索引",
        "",
        "本页登记 60 篇 Section II Part A 原文、题目和选项的结构化 raw JSON。答案与解析不进入 practice-safe 数据；其正文独立保存在 `raw/protected/exam-reading-analysis/`。",
        "",
        "| source_id | reference_id | 年份 | 篇目 | raw_path | 真题页 | 文章页 | 受保护解析 | 状态 |",
        "|---|---|---:|---:|---|---|---|---|---|",
    ]
    wiki_lines = [
        "# 2010–2024 考研英语一历年阅读总索引",
        "",
        f"生成日期：{ingest_date.isoformat()}",
        "",
        "范围：15 年 × 每年 4 篇 = 60 篇。所有新建文章页均为 `practice_safe`，只显示原文、题目和选项。",
        "",
        "> 当前学习进度：用户已做到 2011 年 Text 4；2012 年及以后均作为后续练习入口，不提前显示答案。",
        "",
    ]
    by_year: dict[int, list[dict[str, object]]] = {}
    for record in corpus:
        by_year.setdefault(int(record["year"]), []).append(record)
        raw_lines.append(
            f"| {record['source_id']} | {record['reference_id']} | {record['year']} | Text {record['text_no']} | "
            f"`{record['raw_path']}` | p{record['truth_pages'][0]}–{record['truth_pages'][1]} | "
            f"[[{str(record['article_path']).removesuffix('.md')}|打开文章]] | "
            f"`{record['protected_analysis_path']}` / {record['analysis_preprocess_status']} | practice_safe |"
        )
    for year in sorted(by_year):
        wiki_lines.extend(
            [
                f"## {year} 年",
                "",
                "| 篇目 | 自主练习页 | 原文 / 选项 | 解析源 | 学习状态 |",
                "|---|---|---|---|---|",
            ]
        )
        for record in sorted(by_year[year], key=lambda row: int(row["text_no"])):
            key = (int(record["year"]), int(record["text_no"]))
            if key == (2011, 4):
                learning = "当前进度 / 已有 practice-safe 页"
            elif key in EXISTING_ARTICLES:
                learning = "已有学习资产；本次未覆盖"
            elif year >= 2012:
                learning = "已整理 / 待学习"
            else:
                learning = "已整理 / 学习状态未记录"
            article_link = f"[[{str(record['article_path']).removesuffix('.md')}|{year} Text {record['text_no']}]]"
            wiki_lines.append(
                f"| Text {record['text_no']} | {article_link} | 已保存，5 题 × 4 选项 | "
                f"hidden_until_review；本地预处理 `{record['protected_analysis_path']}` | {learning} |"
            )
        wiki_lines.append("")
    wiki_lines.extend(
        [
            "## 读取边界",
            "",
            "- 自主练习、逐句翻译、词义和选项中文讨论只读文章页。",
            "- 用户只陈述选项时保持 practice-safe；明确要求核对答案、讲题或进入复盘时，才按年份、Text、题号读取 `raw/protected/exam-reading-analysis/index.md` 的最小本地区块。",
            "- 只有本地 OCR / 文字层存在歧义时，才回到外部解析 PDF 视觉页核查。",
            "- 2022、2024 的答案速查页不进入任何 practice-safe 页面。",
            "- 新词、词组和句型仍先进入文章候选区；本语料批量构建不写 `master_bank.csv`。",
            "",
        ]
    )
    raw_index = ROOT / "raw" / "articles" / "exam-reading-corpus" / "index.md"
    raw_index.parent.mkdir(parents=True, exist_ok=True)
    raw_index.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
    wiki_index = ROOT / "wiki" / "reading" / "历年真题阅读总索引.md"
    wiki_index.parent.mkdir(parents=True, exist_ok=True)
    wiki_index.write_text("\n".join(wiki_lines) + "\n", encoding="utf-8")


def verify(root: Path) -> dict[str, object]:
    files = sorted((root / "raw" / "articles" / "exam-reading-corpus").glob("20*/text-*.json"))
    failures: list[str] = []
    seen = set()
    forbidden_answer_keys = {
        "answer",
        "answers",
        "answer_key",
        "correct_answer",
        "standard_answer",
        "explanation",
        "analysis_text",
    }
    completed_visible_articles = {(2010, 4), (2011, 1), (2011, 2)}
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        key = (record.get("year"), record.get("text_no"))
        seen.add(key)
        if forbidden_answer_keys.intersection(record):
            failures.append(f"{key}: practice JSON top-level answer leakage")
        expected = list(EXPECTED_QUESTIONS[int(record["text_no"])])
        numbers = [q.get("number") for q in record.get("questions", [])]
        if numbers != expected:
            failures.append(f"{key}: question numbers {numbers}")
        for q in record.get("questions", []):
            if forbidden_answer_keys.intersection(q):
                failures.append(f"{key} Q{q.get('number')}: practice JSON answer leakage")
            if len(str(q.get("prompt", "")).split()) < 2:
                failures.append(f"{key} Q{q.get('number')}: empty prompt")
            if set(q.get("options", {})) != {"A", "B", "C", "D"}:
                failures.append(f"{key} Q{q.get('number')}: bad options")
            if any(not str(value).strip() for value in q.get("options", {}).values()):
                failures.append(f"{key} Q{q.get('number')}: empty option")
            joined = " ".join([str(q.get("prompt", "")), *map(str, q.get("options", {}).values())])
            if re.search(r"\b(?:RIG|RIE|iKRAM)\b|\(#?14\b", joined):
                failures.append(f"{key} Q{q.get('number')}: footer leakage")
            if re.search(r"(?:[_|«]|[—_-]+[iIl1]+[«»]?)\s*$", str(q.get("prompt", ""))):
                failures.append(f"{key} Q{q.get('number')}: OCR blank residue")
            if re.search(r"\b(?:Al-generated|Al companies|AlI-directed|oneof|neededto|rulehas|howto|spurredby)\b", joined):
                failures.append(f"{key} Q{q.get('number')}: known OCR token corruption")
            if joined.count("“") != joined.count("”"):
                failures.append(f"{key} Q{q.get('number')}: unbalanced double quotes")
        if record.get("answer_visibility") != "hidden_until_review":
            failures.append(f"{key}: answer visibility leak")
        protected_path = root / str(record.get("protected_analysis_path", ""))
        if record.get("analysis_preprocess_status") != "preprocessed_complete":
            failures.append(f"{key}: protected analysis not complete")
        if not protected_path.is_file():
            failures.append(f"{key}: missing protected analysis {protected_path}")
        if "待核验" in str(record.get("analysis_page_range")) or "pending" in str(record.get("analysis_anchor_status")):
            failures.append(f"{key}: unresolved analysis locator")
        coverage = float(record.get("quality", {}).get("source_token_coverage", 0))
        if coverage < 0.97:
            failures.append(f"{key}: source token coverage {coverage}")
        passage_text = " ".join(map(str, record.get("passage_paragraphs", [])))
        if re.search(r"\b[RH][A-Za-z]{1,4}\s*\(.*(?:#?\s*1[45]|4(?:14|15))\b", passage_text):
            failures.append(f"{key}: passage footer leakage")
        if re.search(r",\.|\.\s+-\s+[A-Z]|[a-z]\.[A-Z]", passage_text):
            failures.append(f"{key}: suspicious OCR punctuation")
        for paragraph_no, paragraph in enumerate(record.get("passage_paragraphs", []), start=1):
            if str(paragraph).count("“") != str(paragraph).count("”"):
                failures.append(f"{key} P{paragraph_no}: unbalanced double quotes")
        known_bad = (
            "20\" century",
            "Jn re Bilski",
            "moreconcessions",
            "acknowledged —_ by",
            "Tt’s",
            "investing,’",
            "bealigned",
            "Géilardi",
            "deploy AI’",
            "AlI-directed",
            "Al-generated",
            "Al companies",
            ". a It comes",
            "New Hampshire,.where",
            "percent, ‘housing advocates",
            "ways of ‘delivering",
            "who are Statistically literate",
            "Taylor said. - However",
            "end print,’",
            "reader error,’",
            "distributed trust’.",
            "French companies” interests",
            "T love My Children",
            "entrepreneurs.These",
            "result.While",
            "profits”? But",
            "No.2 executives",
            "SeditionActs",
            "SocialMedia",
        )
        if any(token in passage_text for token in known_bad):
            failures.append(f"{key}: known passage OCR corruption")
        article = root / str(record.get("article_path"))
        if not article.exists():
            failures.append(f"{key}: missing article {article}")
        else:
            article_text = article.read_text(encoding="utf-8")
            if key not in completed_visible_articles and re.search(
                r"(?:正确答案|标准答案|(?<!解析)答案)\s*[：:]\s*[A-D](?:\b|[.．、])",
                article_text,
            ):
                failures.append(f"{key}: visible answer leaked into unfinished article")
            if key not in EXISTING_ARTICLES:
                expected_article = markdown_article(record, date.fromisoformat(str(record["ingest_date"])))
                if article_text != expected_article:
                    failures.append(f"{key}: generated article differs from JSON")
    expected_keys = {(year, text_no) for year in range(2010, 2025) for text_no in TEXTS}
    if seen != expected_keys:
        failures.append(f"coverage mismatch: missing={sorted(expected_keys-seen)} extra={sorted(seen-expected_keys)}")
    protected_ocr = root / "raw" / "reference_sources" / "analysis_page_ocr"
    for year, expected_count in ((2022, 80), (2024, 48)):
        actual_count = len(list((protected_ocr / str(year)).glob("page-*.txt")))
        if actual_count != expected_count:
            failures.append(f"{year}: protected analysis OCR pages {actual_count}/{expected_count}")
    if failures:
        raise SystemExit("\n".join(failures))
    return {"records": len(files), "coverage": "2010-2024 x Text1-4", "structural_gate": "ok"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--end-year", type=int, default=2024)
    parser.add_argument("--overwrite-generated", action="store_true")
    parser.add_argument("--refresh-ocr", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    result = verify(ROOT) if args.verify_only else build(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
