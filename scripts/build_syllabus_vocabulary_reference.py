#!/usr/bin/env python3
"""Build a headword-only reference layer from the syllabus vocabulary PDF.

The source is a 92-page scanned booklet.  Only the two bold English headword
columns on PDF pages 5-90 are OCRed.  Definitions, phonetics, printed
frequencies, translations, and learner-state fields are deliberately excluded.

Outputs are a derived reference layer, not ``bank/master_bank.csv`` and not a
claim that the historical booklet is the current examination syllabus.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF = Path(
    "/Users/your-user/Desktop/1. 1980-2025考研英一真题+解析/"
    "5.  大纲词汇背诵宝典 英语一.pdf"
)

SOURCE_ID = "SRC-SYLLABUS-VOCABULARY-BOOKLET-001"
BODY_START_PAGE = 5
BODY_END_PAGE = 90
EXPECTED_PDF_PAGES = 92
EXPECTED_TOTAL = 5746

RAW_ROOT = ROOT / "raw" / "reference_sources" / "syllabus_vocabulary"
OCR_ROOT = RAW_ROOT / "ocr"
ENTRIES_PATH = RAW_ROOT / "syllabus_vocabulary_entries.jsonl"
UNIQUE_JSONL_PATH = RAW_ROOT / "syllabus_vocabulary_unique.jsonl"
UNIQUE_TSV_PATH = RAW_ROOT / "syllabus_vocabulary_unique.tsv"
UNIQUE_TXT_PATH = RAW_ROOT / "syllabus_vocabulary_unique.txt"
PENDING_PATH = RAW_ROOT / "syllabus_vocabulary_pending_review.jsonl"
REJECTED_PATH = RAW_ROOT / "syllabus_vocabulary_rejected_ocr.jsonl"
MANUAL_REVIEW_PATH = RAW_ROOT / "syllabus_vocabulary_manual_review.jsonl"
STRATEGIC_REVIEW_PATH = RAW_ROOT / "syllabus_vocabulary_strategic_review.jsonl"
MANIFEST_PATH = RAW_ROOT / "manifest.json"
VALIDATION_JSON_PATH = RAW_ROOT / "validation.json"
CROP_SELECTION_PATH = RAW_ROOT / "right_crop_selection.json"
WIKI_PATH = ROOT / "wiki" / "vocabulary" / "大纲词汇参考库.md"
REPORT_PATH = ROOT / "wiki" / "validation" / "大纲词汇参考库验证报告.md"
BASELINE_MANUAL_VERIFIED_COUNT = 35

# Ratios are relative to a rendered page.  The crops include the entire bold
# headword lane and only a narrow sliver beyond it.  Parsing below reads the
# first bold-looking lexical run and never promotes the adjacent phonetic or
# definition text into structured fields.
CROP_RATIOS = {
    "left": (0.022, 0.018, 0.195, 0.982),
    # Some scans place the right headword lane a few pixels left of the centre
    # rule.  Starting at 0.482 preserves the initial letter on those pages.
    "right": (0.482, 0.018, 0.675, 0.982),
}
RIGHT_CROP_START_CANDIDATES = (0.488, 0.490, 0.492, 0.495, 0.498)

# A deliberately boundary-heavy, stratified visual sample (10 records per
# section).  These cases were checked against rendered PDF pages.  ``aliases``
# records the pre-review OCR form when a correction was needed; no dictionary
# lookup is used.
MANUAL_AUDIT_CASES = (
    # Part 1: p20 general layout plus the p71 mixed-page boundary.
    ("AUDIT-P1-01", "part_1_true_exam", 20, "left", "criticize", (), 0.02),
    ("AUDIT-P1-02", "part_1_true_exam", 20, "left", "cross", (), 0.10),
    ("AUDIT-P1-03", "part_1_true_exam", 20, "left", "crucial", (), 0.20),
    ("AUDIT-P1-04", "part_1_true_exam", 20, "left", "cry", (), 0.35),
    ("AUDIT-P1-05", "part_1_true_exam", 20, "left", "cucumber", (), 0.38),
    ("AUDIT-P1-06", "part_1_true_exam", 20, "left", "current", (), 0.68),
    ("AUDIT-P1-07", "part_1_true_exam", 20, "right", "daily", (), 0.02),
    ("AUDIT-P1-08", "part_1_true_exam", 20, "right", "dangerous", (), 0.20),
    ("AUDIT-P1-09", "part_1_true_exam", 20, "right", "decide", (), 0.95),
    ("AUDIT-P1-10", "part_1_true_exam", 71, "right", "zoo", ("z",), 0.176),
    # Part 2: p71 start boundary plus p88 end boundary.
    ("AUDIT-P2-01", "part_2_zero_frequency", 71, "left", "abdomen", (), 0.364),
    ("AUDIT-P2-02", "part_2_zero_frequency", 71, "left", "accessory", (), 0.457),
    ("AUDIT-P2-03", "part_2_zero_frequency", 71, "left", "acrobat", (), 0.533),
    ("AUDIT-P2-04", "part_2_zero_frequency", 71, "left", "adjacent", (), 0.570),
    ("AUDIT-P2-05", "part_2_zero_frequency", 71, "right", "ankle", (), 0.325),
    ("AUDIT-P2-06", "part_2_zero_frequency", 71, "right", "anyhow", (), 0.418),
    ("AUDIT-P2-07", "part_2_zero_frequency", 71, "right", "architect", (), 0.606),
    ("AUDIT-P2-08", "part_2_zero_frequency", 71, "right", "autumn", (), 0.871),
    ("AUDIT-P2-09", "part_2_zero_frequency", 88, "right", "zinc", ("zine",), 0.077),
    ("AUDIT-P2-10", "part_2_zero_frequency", 88, "right", "zip", ("i",), 0.096),
    # Part 3: first mixed page, both columns.
    ("AUDIT-P3-01", "part_3_beyond_syllabus", 88, "left", "abdicate", (), 0.279),
    ("AUDIT-P3-02", "part_3_beyond_syllabus", 88, "left", "adversarial", (), 0.317),
    ("AUDIT-P3-03", "part_3_beyond_syllabus", 88, "left", "algorithm", (), 0.375),
    ("AUDIT-P3-04", "part_3_beyond_syllabus", 88, "left", "anatomy", (), 0.433),
    ("AUDIT-P3-05", "part_3_beyond_syllabus", 88, "left", "anthropologist", (), 0.471),
    ("AUDIT-P3-06", "part_3_beyond_syllabus", 88, "right", "capuchin", (), 0.279),
    ("AUDIT-P3-07", "part_3_beyond_syllabus", 88, "right", "chauvinistic", (), 0.394),
    ("AUDIT-P3-08", "part_3_beyond_syllabus", 88, "right", "chromosome", (), 0.469),
    ("AUDIT-P3-09", "part_3_beyond_syllabus", 88, "right", "clientele", (), 0.601),
    ("AUDIT-P3-10", "part_3_beyond_syllabus", 88, "right", "cynic", ("eynic",), 0.792),
)

MANUAL_CORRECTION_CASES = (
    (
        "CORRECT-P074-R08-CRADLE",
        "part_2_zero_frequency",
        74,
        "right",
        "cradle",
        ("fi",),
        0.176,
    ),
    (
        "CORRECT-P082-R33-RECIPE",
        "part_2_zero_frequency",
        82,
        "right",
        "recipe",
        ("fi",),
        0.889,
    ),
    (
        "CORRECT-P062-R-SUDDEN",
        "part_1_true_exam",
        62,
        "right",
        "sudden",
        ("f",),
        0.264,
    ),
    (
        "CORRECT-P062-R-SUPERFICIAL",
        "part_1_true_exam",
        62,
        "right",
        "superficial",
        ("t",),
        0.684,
    ),
    (
        "CORRECT-P072-R-BRASS",
        "part_2_zero_frequency",
        72,
        "right",
        "brass",
        ("i",),
        0.553,
    ),
)

MANUAL_REJECT_CASES = (
    (
        "REJECT-P074-R10-IH",
        "part_2_zero_frequency",
        74,
        "right",
        "ih",
        0.213,
        "crane",
        "crazy",
    ),
    (
        "REJECT-P074-R22-WJ",
        "part_2_zero_frequency",
        74,
        "right",
        "wj",
        0.512,
        "curl",
        "currency",
    ),
    (
        "REJECT-P011-R-FOOTER",
        "part_1_true_exam",
        11,
        "right",
        "bsr nab",
        0.996,
        "boat",
        "body（下一页）",
    ),
    (
        "REJECT-P042-R-JI",
        "part_1_true_exam",
        42,
        "right",
        "ji",
        0.944,
        "mother",
        "motion",
    ),
    (
        "REJECT-P062-R-AF",
        "part_1_true_exam",
        62,
        "right",
        "af",
        0.870,
        "supplement",
        "supply",
    ),
)

# Independent visual coverage audit: 15 pages x 2 columns, 1,021 printed
# headwords.  This checks occurrence coverage by page/column after corrections;
# it is distinct from the 30-entry spelling sample above.
PAGE_COLUMN_VISUAL_COUNTS = {
    5: (20, 23),
    6: (31, 34),
    14: (29, 34),
    26: (35, 39),
    39: (28, 34),
    52: (33, 29),
    65: (37, 31),
    70: (34, 32),
    71: (34, 36),
    72: (38, 38),
    79: (39, 38),
    87: (35, 36),
    88: (35, 32),
    89: (41, 43),
    90: (37, 36),
}


@dataclass(frozen=True)
class SectionSpec:
    key: str
    label: str
    printed_target: int
    left_start: str
    right_start: str


SECTIONS = (
    SectionSpec("part_1_true_exam", "第一部分：真题词汇", 4249, "a/an", "abuse"),
    SectionSpec("part_2_zero_frequency", "第二部分：零频词汇", 1281, "abdomen", "ankle"),
    SectionSpec("part_3_beyond_syllabus", "第三部分：超纲词汇", 216, "abdicate", "capuchin"),
)
SECTION_BY_KEY = {spec.key: spec for spec in SECTIONS}

FORBIDDEN_STRUCTURED_FIELDS = {
    "meaning",
    "definition",
    "translation",
    "frequency",
    "true_exam_frequency",
    "part_of_speech",
    "mastery",
}


@dataclass
class OCRRow:
    pdf_page: int
    dpi: int
    column: str
    line_no: int
    y_ratio: float
    raw: str
    normalized: str
    confidence: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class Boundary:
    section_key: str
    pdf_page: int
    split_by_column: dict[str, float]
    status: str
    evidence: dict[str, object]


def run(cmd: Sequence[str]) -> str:
    proc = subprocess.run(
        list(cmd), check=True, capture_output=True, text=True, errors="replace"
    )
    return proc.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_page_count(path: Path) -> int:
    info = run(["pdfinfo", str(path)])
    match = re.search(r"(?m)^Pages:\s+(\d+)\s*$", info)
    if not match:
        raise RuntimeError(f"无法读取 PDF 页数：{path}")
    return int(match.group(1))


def ensure_dependencies() -> None:
    missing = [name for name in ("pdfinfo", "pdftoppm", "tesseract") if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"缺少 OCR 依赖：{', '.join(missing)}")
    langs = run(["tesseract", "--list-langs"])
    if not re.search(r"(?m)^eng\s*$", langs):
        raise RuntimeError("Tesseract 未安装 eng 语言包")


def normalize_headword(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = value.replace("–", "-").replace("—", "-")
    value = value.replace("‘", "'").replace("’", "'")
    value = re.sub(r"^[^A-Za-z]+", "", value)
    value = re.sub(r"[0-9¹²³⁴⁵⁶⁷⁸⁹⁰]+", "", value)
    # OCR regularly turns the superscript count into a trailing quote.  Keep
    # apostrophes only when they are genuinely between letters.
    value = re.sub(r"(?<![A-Za-z])['\"]+", "", value)
    value = re.sub(r"['\"]+(?![A-Za-z])", "", value)
    value = re.sub(r"[^A-Za-z'./ -]+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .-/")
    return value.lower()


def lexical_token(raw: str) -> str:
    if any(char in raw for char in "[]{}():;=+\\"):
        return ""
    return normalize_headword(raw)


def plausible_headword(value: str) -> bool:
    if not value or len(value) > 42:
        return False
    if len(value.split()) > 4:
        return False
    if not re.fullmatch(r"[a-z][a-z'./-]*(?: [a-z][a-z'./-]*)*", value):
        return False
    if value in {"sb", "sth", "doing sth", "sb sth", "prep", "pron", "conj", "art"}:
        return False
    return True


def render_page(pdf: Path, page: int, dpi: int, temp_dir: Path) -> Path:
    prefix = temp_dir / f"page-{page:03d}-{dpi}dpi"
    output = prefix.with_suffix(".png")
    if output.exists():
        return output
    subprocess.run(
        [
            "pdftoppm",
            "-f",
            str(page),
            "-l",
            str(page),
            "-r",
            str(dpi),
            "-gray",
            "-png",
            "-singlefile",
            str(pdf),
            str(prefix),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not output.exists():
        raise RuntimeError(f"PDF 渲染未生成预期文件：{output}")
    return output


def crop_page(
    image_path: Path,
    column: str,
    output: Path,
    dpi: int,
    *,
    ratios: tuple[float, float, float, float] | None = None,
) -> None:
    with Image.open(image_path) as image:
        width, height = image.size
        x0, y0, x1, y1 = ratios or CROP_RATIOS[column]
        box = (
            round(width * x0),
            round(height * y0),
            round(width * x1),
            round(height * y1),
        )
        crop = image.crop(box)
        crop.save(output, dpi=(dpi, dpi))


def ensure_ocr(
    pdf: Path,
    page: int,
    dpi: int,
    column: str,
    temp_dir: Path,
    *,
    refresh: bool,
    crop_ratios: tuple[float, float, float, float] | None = None,
) -> tuple[Path, Path]:
    out_dir = OCR_ROOT / f"{dpi}dpi"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"page-{page:03d}-{column}"
    txt = base.with_suffix(".txt")
    tsv = base.with_suffix(".tsv")
    if txt.exists() and tsv.exists() and not refresh:
        return txt, tsv

    rendered = render_page(pdf, page, dpi, temp_dir)
    crop_path = temp_dir / f"page-{page:03d}-{dpi}dpi-{column}.png"
    crop_page(rendered, column, crop_path, dpi, ratios=crop_ratios)
    subprocess.run(
        [
            "tesseract",
            str(crop_path),
            str(base),
            "-l",
            "eng",
            "--psm",
            "6",
            "-c",
            "preserve_interword_spaces=1",
            "txt",
            "tsv",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return txt, tsv


COMMON_SHORT_HEADWORDS = {
    "am",
    "an",
    "as",
    "at",
    "be",
    "by",
    "do",
    "go",
    "he",
    "if",
    "in",
    "is",
    "it",
    "me",
    "my",
    "no",
    "of",
    "on",
    "or",
    "ox",
    "so",
    "to",
    "tv",
    "up",
    "us",
    "we",
}


def crop_candidate_score(rows: Sequence[dict[str, object]], page: int) -> tuple[float, dict[str, int]]:
    normalized = [str(row["normalized"]) for row in rows]
    single = sum(len(value) == 1 for value in normalized)
    odd_short = sum(
        len(value) == 2 and value not in COMMON_SHORT_HEADWORDS for value in normalized
    )
    descents = 0
    for previous, current in zip(normalized, normalized[1:]):
        if current[:1] < previous[:1]:
            descents += 1
    allowed_resets = 1 if page in {71, 88} else 0
    excess_descents = max(0, descents - allowed_resets)
    exact = sum(row["verification_status"] == "ocr_consensus_dual_unreviewed" for row in rows)
    unmatched = sum(
        row["verification_status"]
        in {"300dpi_only_pending_review", "150dpi_only_pending_review"}
        for row in rows
    )
    mismatch = sum("mismatch" in str(row["verification_status"]) for row in rows)
    score = (
        len(rows)
        + exact * 2.0
        - single * 18.0
        - odd_short * 6.0
        - excess_descents * 12.0
        - unmatched * 3.0
        - mismatch * 1.5
    )
    return score, {
        "rows": len(rows),
        "exact_dual": exact,
        "single_character": single,
        "odd_short": odd_short,
        "alphabetic_descents": descents,
        "unmatched": unmatched,
        "mismatch": mismatch,
    }


def build_adaptive_right_ocr(
    pdf: Path, page: int, temp_dir: Path
) -> dict[str, object]:
    """Try narrow crop starts and publish only the strongest dual-OCR crop."""

    candidates: list[dict[str, object]] = []
    rendered = {
        dpi: render_page(pdf, page, dpi, temp_dir)
        for dpi in (300, 150)
    }
    for start in RIGHT_CROP_START_CANDIDATES:
        parsed: dict[int, list[OCRRow]] = {}
        raw_paths: dict[int, tuple[Path, Path]] = {}
        ratio = (start, CROP_RATIOS["right"][1], CROP_RATIOS["right"][2], CROP_RATIOS["right"][3])
        for dpi in (300, 150):
            token = f"{int(round(start * 1000)):03d}"
            crop_path = temp_dir / f"adaptive-p{page:03d}-{dpi}dpi-x{token}.png"
            crop_page(rendered[dpi], "right", crop_path, dpi, ratios=ratio)
            base = temp_dir / f"adaptive-p{page:03d}-{dpi}dpi-x{token}"
            subprocess.run(
                [
                    "tesseract",
                    str(crop_path),
                    str(base),
                    "-l",
                    "eng",
                    "--psm",
                    "6",
                    "-c",
                    "preserve_interword_spaces=1",
                    "txt",
                    "tsv",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            txt = base.with_suffix(".txt")
            tsv = base.with_suffix(".tsv")
            parsed[dpi], _rejected = parse_tsv(tsv, page, dpi, "right")
            raw_paths[dpi] = (txt, tsv)
        fused = fuse_rows(parsed[300], parsed[150])
        score, metrics = crop_candidate_score(fused, page)
        candidates.append(
            {
                "start_ratio": start,
                "score": round(score, 3),
                "metrics": metrics,
                "raw_paths": raw_paths,
            }
        )

    # On an equal score, prefer the narrower/later start: it contains less of
    # the central divider and adjacent definition lane.
    winner = max(candidates, key=lambda item: (float(item["score"]), float(item["start_ratio"])))
    out_dir = OCR_ROOT
    for dpi in (300, 150):
        dpi_dir = out_dir / f"{dpi}dpi"
        dpi_dir.mkdir(parents=True, exist_ok=True)
        base = dpi_dir / f"page-{page:03d}-right"
        source_txt, source_tsv = winner["raw_paths"][dpi]
        shutil.copyfile(source_txt, base.with_suffix(".txt"))
        shutil.copyfile(source_tsv, base.with_suffix(".tsv"))

    return {
        "pdf_page": page,
        "selected_start_ratio": winner["start_ratio"],
        "selected_score": winner["score"],
        "selected_metrics": winner["metrics"],
        "candidates": [
            {
                "start_ratio": item["start_ratio"],
                "score": item["score"],
                "metrics": item["metrics"],
            }
            for item in candidates
        ],
    }


def parse_tsv(path: Path, page: int, dpi: int, column: str) -> tuple[list[OCRRow], list[dict[str, object]]]:
    words_by_line: dict[tuple[int, int, int, int], list[dict[str, object]]] = defaultdict(list)
    page_width = 0
    page_height = 0
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            level = int(row.get("level") or 0)
            if level == 1:
                page_width = int(row.get("width") or 0)
                page_height = int(row.get("height") or 0)
            if level != 5 or not (row.get("text") or "").strip():
                continue
            key = tuple(int(row[name]) for name in ("page_num", "block_num", "par_num", "line_num"))
            words_by_line[key].append(
                {
                    "text": row["text"].strip(),
                    "left": int(row["left"]),
                    "top": int(row["top"]),
                    "width": int(row["width"]),
                    "height": int(row["height"]),
                    "conf": float(row["conf"]),
                }
            )
    if page_width <= 0 or page_height <= 0:
        raise ValueError(f"TSV 缺页尺寸：{path}")

    accepted: list[OCRRow] = []
    rejected: list[dict[str, object]] = []
    line_candidates: list[tuple[float, list[dict[str, object]]]] = []
    for tokens in words_by_line.values():
        tokens.sort(key=lambda item: int(item["left"]))
        top = min(int(item["top"]) for item in tokens)
        bottom = max(int(item["top"]) + int(item["height"]) for item in tokens)
        line_candidates.append(((top + bottom) / 2 / page_height, tokens))
    line_candidates.sort(key=lambda item: item[0])

    for line_no, (y_ratio, tokens) in enumerate(line_candidates, start=1):
        # Wrapped definition fragments start far to the right of the crop.  A
        # headword line must expose an English token inside the first third.
        start_index = None
        for idx, token in enumerate(tokens):
            if int(token["left"]) > page_width * 0.34:
                break
            if lexical_token(str(token["text"])):
                start_index = idx
                break
        if start_index is None:
            rejected.append(
                {
                    "pdf_page": page,
                    "dpi": dpi,
                    "column": column,
                    "line_no": line_no,
                    "y_ratio": round(y_ratio, 6),
                    "raw": " ".join(str(item["text"]) for item in tokens),
                    "reason": "no_leading_lexical_token",
                }
            )
            continue

        selected = [tokens[start_index]]
        previous = tokens[start_index]
        base_height = max(1, int(previous["height"]))
        for token in tokens[start_index + 1 :]:
            raw_token = str(token["text"])
            clean_token = lexical_token(raw_token)
            gap = int(token["left"]) - (int(previous["left"]) + int(previous["width"]))
            height_ratio = int(token["height"]) / base_height
            if not clean_token:
                break
            if gap > max(round(base_height * 1.25), round(14 * dpi / 150)):
                break
            if not 0.68 <= height_ratio <= 1.32:
                break
            if clean_token in {"n", "v", "a", "ad", "prep", "pron", "conj", "art", "num"}:
                break
            selected.append(token)
            previous = token
            if len(selected) >= 4:
                break

        raw = " ".join(str(item["text"]) for item in selected)
        normalized = normalize_headword(raw)
        confidence = sum(float(item["conf"]) for item in selected) / len(selected)
        warnings: list[str] = []
        if confidence < 30:
            warnings.append("low_ocr_confidence")
        if len(normalized) <= 1:
            warnings.append("single_character_candidate")
        if raw.upper() == raw and (len(normalized) > 6 or " " in normalized):
            warnings.append("all_caps_candidate")
        if not plausible_headword(normalized):
            rejected.append(
                {
                    "pdf_page": page,
                    "dpi": dpi,
                    "column": column,
                    "line_no": line_no,
                    "y_ratio": round(y_ratio, 6),
                    "raw": raw,
                    "normalized_candidate": normalized,
                    "confidence": round(confidence, 2),
                    "reason": "implausible_headword_shape",
                }
            )
            continue
        accepted.append(
            OCRRow(
                pdf_page=page,
                dpi=dpi,
                column=column,
                line_no=line_no,
                y_ratio=y_ratio,
                raw=raw,
                normalized=normalized,
                confidence=confidence,
                warnings=warnings,
            )
        )
    return accepted, rejected


def similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


def align_rows(primary: Sequence[OCRRow], secondary: Sequence[OCRRow]) -> list[tuple[OCRRow | None, OCRRow | None]]:
    """Order-preserving OCR alignment using y-position and text similarity."""

    n, m = len(primary), len(secondary)
    gap_cost = 0.75
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    move = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i * gap_cost
        move[i][0] = "primary_only"
    for j in range(1, m + 1):
        dp[0][j] = j * gap_cost
        move[0][j] = "secondary_only"

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            left = primary[i - 1]
            right = secondary[j - 1]
            y_delta = abs(left.y_ratio - right.y_ratio)
            if y_delta > 0.035:
                pair_cost = 4.0
            else:
                pair_cost = y_delta * 10 + (1 - similarity(left.normalized, right.normalized)) * 0.65
            options = (
                (dp[i - 1][j - 1] + pair_cost, "pair"),
                (dp[i - 1][j] + gap_cost, "primary_only"),
                (dp[i][j - 1] + gap_cost, "secondary_only"),
            )
            dp[i][j], move[i][j] = min(options, key=lambda item: item[0])

    aligned: list[tuple[OCRRow | None, OCRRow | None]] = []
    i, j = n, m
    while i or j:
        action = move[i][j]
        if action == "pair":
            aligned.append((primary[i - 1], secondary[j - 1]))
            i -= 1
            j -= 1
        elif action == "primary_only":
            aligned.append((primary[i - 1], None))
            i -= 1
        elif action == "secondary_only":
            aligned.append((None, secondary[j - 1]))
            j -= 1
        else:
            raise RuntimeError(f"OCR 对齐失败：i={i}, j={j}")
    aligned.reverse()
    return aligned


def fuse_rows(primary: Sequence[OCRRow], secondary: Sequence[OCRRow]) -> list[dict[str, object]]:
    fused: list[dict[str, object]] = []
    for row_300, row_150 in align_rows(primary, secondary):
        chosen = row_300 or row_150
        assert chosen is not None
        warnings = list(dict.fromkeys((row_300.warnings if row_300 else []) + (row_150.warnings if row_150 else [])))
        if row_300 and row_150:
            match_score = similarity(row_300.normalized, row_150.normalized)
            if row_300.normalized == row_150.normalized:
                status = "ocr_consensus_dual_unreviewed"
            elif match_score >= 0.82:
                status = "dual_ocr_close_mismatch_pending_review"
                warnings.append("dual_ocr_text_mismatch")
            else:
                status = "dual_ocr_mismatch_pending_review"
                warnings.append("dual_ocr_text_mismatch")
        elif row_300:
            match_score = None
            status = "300dpi_only_pending_review"
            warnings.append("missing_150dpi_match")
        else:
            match_score = None
            status = "150dpi_only_pending_review"
            warnings.append("missing_300dpi_match")

        material_warnings = [warning for warning in warnings if warning != "low_ocr_confidence"]
        if material_warnings and status == "ocr_consensus_dual_unreviewed":
            status = "ocr_consensus_with_warning_pending_review"
        fused.append(
            {
                "pdf_page": chosen.pdf_page,
                "column": chosen.column,
                "y_ratio": round(chosen.y_ratio, 6),
                "raw": chosen.raw,
                "normalized": chosen.normalized,
                "raw_ocr_300": row_300.raw if row_300 else None,
                "normalized_ocr_300": row_300.normalized if row_300 else None,
                "ocr_confidence_300": round(row_300.confidence, 2) if row_300 else None,
                "raw_ocr_150": row_150.raw if row_150 else None,
                "normalized_ocr_150": row_150.normalized if row_150 else None,
                "ocr_confidence_150": round(row_150.confidence, 2) if row_150 else None,
                "raw_ocr_450": None,
                "normalized_ocr_450": None,
                "ocr_confidence_450": None,
                "dual_ocr_similarity": round(match_score, 4) if match_score is not None else None,
                "verification_status": status,
                "warnings": warnings,
            }
        )
    return fused


def adjudicate_with_tertiary(
    fused: list[dict[str, object]], tertiary: Sequence[OCRRow]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Use 450 dpi only as a third vote for already detected dual-OCR rows."""

    proxies = [
        OCRRow(
            pdf_page=int(row["pdf_page"]),
            dpi=300,
            column=str(row["column"]),
            line_no=index,
            y_ratio=float(row["y_ratio"]),
            raw=str(row["raw"]),
            normalized=str(row["normalized"]),
            confidence=float(row.get("ocr_confidence_300") or row.get("ocr_confidence_150") or 0),
        )
        for index, row in enumerate(fused)
    ]
    ignored: list[dict[str, object]] = []
    for proxy, row_450 in align_rows(proxies, tertiary):
        if proxy is None and row_450 is not None:
            ignored.append(
                {
                    "pdf_page": row_450.pdf_page,
                    "dpi": 450,
                    "column": row_450.column,
                    "line_no": row_450.line_no,
                    "y_ratio": round(row_450.y_ratio, 6),
                    "raw": row_450.raw,
                    "normalized_candidate": row_450.normalized,
                    "reason": "450dpi_only_not_promoted_without_dual_detection",
                }
            )
            continue
        if proxy is None or row_450 is None:
            continue
        row = fused[proxy.line_no]
        row["raw_ocr_450"] = row_450.raw
        row["normalized_ocr_450"] = row_450.normalized
        row["ocr_confidence_450"] = round(row_450.confidence, 2)
        candidates = [
            ("300", row.get("normalized_ocr_300"), row.get("raw_ocr_300"), row.get("ocr_confidence_300")),
            ("150", row.get("normalized_ocr_150"), row.get("raw_ocr_150"), row.get("ocr_confidence_150")),
            ("450", row_450.normalized, row_450.raw, row_450.confidence),
        ]
        present = [candidate for candidate in candidates if candidate[1]]
        counts = Counter(str(candidate[1]) for candidate in present)
        majority_value, majority_count = counts.most_common(1)[0]
        if majority_count >= 2:
            selected = next(candidate for candidate in present if candidate[1] == majority_value)
            row["normalized"] = majority_value
            row["raw"] = selected[2]
            if len(present) == 3 and majority_count == 3:
                row["verification_status"] = "ocr_consensus_triple_unreviewed"
            else:
                row["verification_status"] = "ocr_majority_unreviewed"
                row["warnings"].append("ocr_disagreement_resolved_by_majority")
        else:
            # No spelling is promoted as verified.  If the current selection is
            # only a one-character crop artefact, retain the longest directly
            # observed OCR candidate for human review instead of inventing a
            # dictionary correction.
            selected = next(
                (candidate for candidate in present if candidate[1] == row["normalized"]),
                present[0],
            )
            if len(str(selected[1])) <= 1:
                selected = max(
                    present,
                    key=lambda candidate: (
                        len(str(candidate[1])),
                        float(candidate[3] or 0),
                    ),
                )
                row["normalized"] = selected[1]
                row["raw"] = selected[2]
                row["warnings"].append("selected_longer_observed_ocr_candidate")
            row["verification_status"] = "triple_ocr_no_majority_pending_review"
        row["warnings"] = list(dict.fromkeys(row["warnings"]))
    return fused, ignored


def filter_unresolved_single_characters(
    rows: list[dict[str, object]], rejected: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Quarantine unresolved one-character OCR artefacts.

    The booklet prints ``a/an`` rather than a standalone ``a`` and its scanned
    column rules repeatedly OCR as I/j/t-like glyphs.  No one-character row is
    published unless another OCR pass expands it to an observed headword.
    """

    kept: list[dict[str, object]] = []
    for row in rows:
        if len(str(row["normalized"])) > 1:
            kept.append(row)
            continue
        rejected.append(
            {
                "pdf_page": row["pdf_page"],
                "dpi": "fused+450",
                "column": row["column"],
                "line_no": None,
                "y_ratio": row["y_ratio"],
                "raw": row["raw"],
                "normalized_candidate": row["normalized"],
                "reason": "unresolved_single_character_layout_artifact",
                "raw_ocr_300": row.get("raw_ocr_300"),
                "raw_ocr_150": row.get("raw_ocr_150"),
                "raw_ocr_450": row.get("raw_ocr_450"),
            }
        )
    return kept


def apply_manual_audit(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Apply the small visual audit layer without rewriting raw OCR evidence."""

    audit_rows: list[dict[str, object]] = []
    review_cases = [
        (*case, True, "stratified_sample") for case in MANUAL_AUDIT_CASES
    ] + [
        (*case, False, "targeted_correction") for case in MANUAL_CORRECTION_CASES
    ]
    for (
        audit_id,
        section,
        page,
        column,
        expected,
        aliases,
        y_hint,
        sample_member,
        review_kind,
    ) in review_cases:
        accepted_values = {expected, *aliases}
        candidates = [
            row
            for row in rows
            if int(row["pdf_page"]) == page
            and row["column"] == column
            and row["normalized"] in accepted_values
        ]
        if not candidates:
            raise RuntimeError(
                f"人工抽样项无法定位：{audit_id} / p{page} / {column} / {expected} / {aliases}"
            )
        selected = min(candidates, key=lambda row: abs(float(row["y_ratio"]) - y_hint))
        observed_before = str(selected["normalized"])
        outcome_before = "match" if observed_before == expected else "mismatch"
        selected["normalized"] = expected
        selected["verification_status"] = "verified_manual_visual"
        selected["manual_review"] = {
            "audit_id": audit_id,
            "review_method": "visual_review_rendered_pdf",
            "pdf_page": page,
            "column": column,
            "expected": expected,
            "observed_before": observed_before,
            "correction_applied": observed_before != expected,
                "review_result": "match_after_review",
                "review_kind": review_kind,
            }
        selected["manual_audit_id"] = audit_id
        if observed_before != expected:
            selected["warnings"].append("manual_correction_from_rendered_pdf")
            selected["manual_corrected_from"] = observed_before
        selected["warnings"] = list(dict.fromkeys(selected["warnings"]))
        audit_rows.append(
            {
                "audit_id": audit_id,
                "section": section,
                "pdf_page": page,
                "column": column,
                "expected": expected,
                "observed_before": observed_before,
                "pre_correction_result": outcome_before,
                "correction_applied": observed_before != expected,
                "post_correction_result": "match",
                "evidence": "visual_review_rendered_pdf",
                "sample_member": sample_member,
                "review_kind": review_kind,
                "action": "keep_or_correct",
            }
        )
    return audit_rows


def apply_manual_rejects(
    rows: list[dict[str, object]], rejected: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Remove two visually confirmed non-headword OCR fragments."""

    remove_ids: set[int] = set()
    review_rows: list[dict[str, object]] = []
    for (
        review_id,
        section,
        page,
        column,
        observed,
        y_hint,
        before,
        after,
    ) in MANUAL_REJECT_CASES:
        candidates = [
            row
            for row in rows
            if int(row["pdf_page"]) == page
            and row["column"] == column
            and row["normalized"] == observed
        ]
        if not candidates:
            raise RuntimeError(
                f"人工 reject 项无法定位：{review_id} / p{page} / {column} / {observed}"
            )
        selected = min(candidates, key=lambda row: abs(float(row["y_ratio"]) - y_hint))
        remove_ids.add(id(selected))
        rejected.append(
            {
                "pdf_page": page,
                "dpi": "manual_visual",
                "column": column,
                "line_no": None,
                "y_ratio": selected["y_ratio"],
                "raw": selected["raw"],
                "normalized_candidate": selected["normalized"],
                "raw_ocr_300": selected.get("raw_ocr_300"),
                "raw_ocr_150": selected.get("raw_ocr_150"),
                "raw_ocr_450": selected.get("raw_ocr_450"),
                "reason": "manual_visual_confirmed_non_headword_between_neighbors",
                "neighbor_before": before,
                "neighbor_after": after,
                "review_id": review_id,
            }
        )
        review_rows.append(
            {
                "audit_id": review_id,
                "section": section,
                "pdf_page": page,
                "column": column,
                "expected": None,
                "observed_before": observed,
                "pre_correction_result": "false_positive",
                "correction_applied": False,
                "post_correction_result": "rejected_non_headword",
                "evidence": "visual_review_rendered_pdf",
                "sample_member": False,
                "review_kind": "targeted_reject",
                "action": "reject",
                "neighbor_before": before,
                "neighbor_after": after,
                "pre_filter_locator": review_id.removeprefix("REJECT-"),
            }
        )
    return [row for row in rows if id(row) not in remove_ids], review_rows


def find_anchor(rows: Iterable[dict[str, object]], value: str) -> list[dict[str, object]]:
    exact = [row for row in rows if row["normalized"] == value]
    if exact:
        return exact
    return [row for row in rows if similarity(str(row["normalized"]), value) >= 0.88]


def filter_layout_noise(
    rows: list[dict[str, object]], rejected: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Remove only layout regions proven not to contain headwords.

    PDF p5 has a large cover/title block above the first headwords and a two-line
    explanatory footnote below them.  The two transition pages also place a
    Chinese section title between an alphabetic y/z tail and the next section's
    a-column.  OCR fragments from those regions are not vocabulary entries.
    """

    remove_ids: set[int] = set()

    # First body page: trim each column to its audited first anchor, then cut a
    # clearly separated footer fragment after the final headword group.
    first_page_rows = [row for row in rows if int(row["pdf_page"]) == BODY_START_PAGE]
    for column, expected_start in (("left", SECTIONS[0].left_start), ("right", SECTIONS[0].right_start)):
        ordered = sorted(
            (row for row in first_page_rows if row["column"] == column),
            key=lambda row: float(row["y_ratio"]),
        )
        anchors = find_anchor(ordered, expected_start)
        if not anchors:
            raise RuntimeError(f"p{BODY_START_PAGE} 无法定位首词：{column}/{expected_start}")
        anchor_index = ordered.index(anchors[0])
        for row in ordered[:anchor_index]:
            remove_ids.add(id(row))
        body = ordered[anchor_index:]
        for idx in range(1, len(body)):
            previous_y = float(body[idx - 1]["y_ratio"])
            current_y = float(body[idx]["y_ratio"])
            if previous_y >= 0.80 and current_y - previous_y >= 0.045:
                for row in body[idx:]:
                    remove_ids.add(id(row))
                break

    # Both later sections begin after the previous section's y/z tail.  Keep
    # only y/z entries before the exact a-column anchors on those same pages;
    # anything else in the interstitial title band is layout OCR noise.
    for spec in SECTIONS[1:]:
        left_anchors = [
            row
            for row in rows
            if row["column"] == "left" and row["normalized"] == spec.left_start
        ]
        if not left_anchors:
            continue
        page = int(left_anchors[0]["pdf_page"])
        for column, expected_start in (("left", spec.left_start), ("right", spec.right_start)):
            ordered = sorted(
                (row for row in rows if int(row["pdf_page"]) == page and row["column"] == column),
                key=lambda row: float(row["y_ratio"]),
            )
            anchors = find_anchor(ordered, expected_start)
            if not anchors:
                continue
            anchor_index = ordered.index(anchors[0])
            for row in ordered[:anchor_index]:
                normalized = str(row["normalized"])
                if not normalized.startswith(("y", "z")):
                    remove_ids.add(id(row))

    kept: list[dict[str, object]] = []
    for row in rows:
        if id(row) not in remove_ids:
            kept.append(row)
            continue
        rejected.append(
            {
                "pdf_page": row["pdf_page"],
                "dpi": "fused",
                "column": row["column"],
                "line_no": None,
                "y_ratio": row["y_ratio"],
                "raw": row["raw"],
                "normalized_candidate": row["normalized"],
                "reason": "audited_non_headword_layout_region",
            }
        )
    return kept


def column_split(
    rows: Sequence[dict[str, object]], expected_start: str
) -> tuple[float, str, dict[str, object]]:
    ordered = sorted(rows, key=lambda row: float(row["y_ratio"]))
    anchors = find_anchor(ordered, expected_start)
    if anchors:
        anchor = anchors[0]
        index = ordered.index(anchor)
        if index == 0:
            split = 0.0
            gap = float(anchor["y_ratio"])
        else:
            previous = ordered[index - 1]
            split = (float(previous["y_ratio"]) + float(anchor["y_ratio"])) / 2
            gap = float(anchor["y_ratio"]) - float(previous["y_ratio"])
        exact = anchor["normalized"] == expected_start
        status = "exact_anchor" if exact and (index == 0 or gap >= 0.045) else "anchor_gap_pending_review"
        return split, status, {
            "expected_start": expected_start,
            "observed_start": anchor["normalized"],
            "previous": ordered[index - 1]["normalized"] if index else None,
            "gap_ratio": round(gap, 6),
            "split_ratio": round(split, 6),
        }

    gaps: list[tuple[float, int]] = []
    for idx in range(1, len(ordered)):
        gaps.append((float(ordered[idx]["y_ratio"]) - float(ordered[idx - 1]["y_ratio"]), idx))
    if not gaps:
        return 0.0, "missing_anchor_pending_review", {
            "expected_start": expected_start,
            "observed_start": None,
            "gap_ratio": None,
            "split_ratio": 0.0,
        }
    gap, index = max(gaps)
    split = (float(ordered[index - 1]["y_ratio"]) + float(ordered[index]["y_ratio"])) / 2
    return split, "largest_gap_fallback_pending_review", {
        "expected_start": expected_start,
        "observed_start": ordered[index]["normalized"],
        "previous": ordered[index - 1]["normalized"],
        "gap_ratio": round(gap, 6),
        "split_ratio": round(split, 6),
    }


def detect_boundaries(rows: Sequence[dict[str, object]]) -> list[Boundary]:
    boundaries: list[Boundary] = []
    for spec in SECTIONS[1:]:
        left_candidates = [
            row for row in rows if row["column"] == "left" and row["normalized"] == spec.left_start
        ]
        if not left_candidates:
            left_candidates = [
                row
                for row in rows
                if row["column"] == "left"
                and similarity(str(row["normalized"]), spec.left_start) >= 0.88
            ]
        if not left_candidates:
            raise RuntimeError(f"无法定位分部起始词：{spec.label} / {spec.left_start}")
        page = int(left_candidates[0]["pdf_page"])
        page_rows = [row for row in rows if int(row["pdf_page"]) == page]
        split_by_column: dict[str, float] = {}
        statuses: list[str] = []
        evidence: dict[str, object] = {"page": page, "columns": {}}
        for column, expected in (("left", spec.left_start), ("right", spec.right_start)):
            column_rows = [row for row in page_rows if row["column"] == column]
            split, status, column_evidence = column_split(column_rows, expected)
            split_by_column[column] = split
            statuses.append(status)
            evidence["columns"][column] = column_evidence
        overall = "automatic_exact_anchors" if statuses == ["exact_anchor", "exact_anchor"] else "boundary_pending_review"
        boundaries.append(
            Boundary(
                section_key=spec.key,
                pdf_page=page,
                split_by_column=split_by_column,
                status=overall,
                evidence=evidence,
            )
        )
    if not boundaries[0].pdf_page < boundaries[1].pdf_page:
        raise RuntimeError(f"分部边界顺序异常：{boundaries}")
    return boundaries


def assign_section(row: dict[str, object], boundaries: Sequence[Boundary]) -> tuple[str, str | None]:
    page = int(row["pdf_page"])
    column = str(row["column"])
    y = float(row["y_ratio"])
    part2, part3 = boundaries
    boundary_warning = None
    if page < part2.pdf_page:
        key = SECTIONS[0].key
    elif page == part2.pdf_page:
        key = SECTIONS[0].key if y < part2.split_by_column[column] else SECTIONS[1].key
        if part2.status != "automatic_exact_anchors":
            boundary_warning = "part_1_to_part_2_boundary_pending_review"
    elif page < part3.pdf_page:
        key = SECTIONS[1].key
    elif page == part3.pdf_page:
        key = SECTIONS[1].key if y < part3.split_by_column[column] else SECTIONS[2].key
        if part3.status != "automatic_exact_anchors":
            boundary_warning = "part_2_to_part_3_boundary_pending_review"
    else:
        key = SECTIONS[2].key
    return key, boundary_warning


def alphabetic_warnings(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    anomalies: list[dict[str, object]] = []
    by_section: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in entries:
        by_section[str(entry["section"])].append(entry)
    for spec in SECTIONS:
        previous: dict[str, object] | None = None
        for entry in by_section[spec.key]:
            current_key = str(entry["normalized"]).replace("-", " ").replace("/", " ")
            if previous is not None:
                previous_key = str(previous["normalized"]).replace("-", " ").replace("/", " ")
                if current_key < previous_key and current_key[:1] != previous_key[:1]:
                    entry["warnings"].append("alphabetic_order_anomaly")
                    if str(entry["verification_status"]).startswith(("ocr_consensus", "ocr_majority")):
                        entry["verification_status"] = "ocr_consensus_with_order_warning_pending_review"
                    anomalies.append(
                        {
                            "entry_id": entry["entry_id"],
                            "previous_entry_id": previous["entry_id"],
                            "previous": previous["normalized"],
                            "current": entry["normalized"],
                            "pdf_page": entry["pdf_page"],
                            "column": entry["column"],
                        }
                    )
            previous = entry
    return anomalies


def jsonl_write(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_strategic_review() -> list[dict[str, object]]:
    """Load the curated writing-foundation visual-review overlay.

    The overlay is deliberately separate from OCR output.  It may grow as
    additional writing-grounding terms are checked against rendered PDF pages,
    but every row must keep an exact, stable occurrence locator.
    """

    if not STRATEGIC_REVIEW_PATH.exists():
        raise RuntimeError(
            f"缺少大纲战略视觉核验 overlay：{STRATEGIC_REVIEW_PATH.relative_to(ROOT)}"
        )
    rows: list[dict[str, object]] = []
    with STRATEGIC_REVIEW_PATH.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"{STRATEGIC_REVIEW_PATH.relative_to(ROOT)}:{line_no}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise RuntimeError(
                    f"{STRATEGIC_REVIEW_PATH.relative_to(ROOT)}:{line_no}: JSONL 行必须是 object"
                )
            rows.append(value)
    if not rows:
        raise RuntimeError("大纲战略视觉核验 overlay 不得为空")
    return rows


def validate_strategic_review(
    entries: Sequence[dict[str, object]],
    overlay_rows: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Validate overlay locators and the baseline-plus-overlay verified set."""

    required_fields = {
        "entry_id",
        "expected_normalized",
        "pdf_page",
        "column",
        "verification_status",
        "evidence",
        "review_scope",
        "reviewed_on",
        "match_type",
    }
    entry_by_id = {str(entry["entry_id"]): entry for entry in entries}
    if len(entry_by_id) != len(entries):
        raise RuntimeError("应用战略核验 overlay 前检测到重复 entry_id")

    overlay_ids: set[str] = set()
    for index, review in enumerate(overlay_rows, start=1):
        missing = required_fields - set(review)
        if missing:
            raise RuntimeError(f"战略核验 overlay 第 {index} 行缺字段：{sorted(missing)}")
        entry_id = str(review["entry_id"])
        if entry_id in overlay_ids:
            raise RuntimeError(f"战略核验 overlay 重复 entry_id：{entry_id}")
        overlay_ids.add(entry_id)
        if review["verification_status"] != "verified_manual_visual":
            raise RuntimeError(f"战略核验 overlay 状态非法：{entry_id}")
        if review["evidence"] != "visual_review_rendered_pdf":
            raise RuntimeError(f"战略核验 overlay 证据类型非法：{entry_id}")
        if review["review_scope"] != "writing_foundation":
            raise RuntimeError(f"战略核验 overlay 范围非法：{entry_id}")
        reviewed_on = str(review["reviewed_on"])
        try:
            date.fromisoformat(reviewed_on)
        except ValueError as exc:
            raise RuntimeError(
                f"战略核验 overlay 复核日期非法：{entry_id} / {reviewed_on}"
            ) from exc
        if not str(review["match_type"]).strip():
            raise RuntimeError(f"战略核验 overlay 缺 match_type：{entry_id}")
        if review["column"] not in {"left", "right"}:
            raise RuntimeError(f"战略核验 overlay 栏位非法：{entry_id}")
        if review["match_type"] != "exact_headword" and not str(review.get("notes", "")).strip():
            raise RuntimeError(f"非直接匹配必须记录 notes：{entry_id}")

        entry = entry_by_id.get(entry_id)
        if entry is None:
            raise RuntimeError(f"战略核验 overlay 无法定位 entry_id：{entry_id}")
        expected_locator = (
            int(review["pdf_page"]),
            str(review["column"]),
            str(review["expected_normalized"]),
        )
        actual_locator = (
            int(entry["pdf_page"]),
            str(entry["column"]),
            str(entry["normalized"]),
        )
        if actual_locator != expected_locator:
            raise RuntimeError(
                f"战略核验 overlay 定位不一致：{entry_id} / "
                f"expected={expected_locator} / actual={actual_locator}"
            )
        if entry.get("verification_status") != "verified_manual_visual":
            raise RuntimeError(f"战略核验 overlay 未应用到词条：{entry_id}")
        strategic_metadata = entry.get("strategic_review")
        if not isinstance(strategic_metadata, dict):
            raise RuntimeError(f"战略核验 overlay 缺词条元数据：{entry_id}")
        for key in (
            "evidence",
            "review_scope",
            "reviewed_on",
            "match_type",
            "writing_form",
            "notes",
        ):
            if key in review and strategic_metadata.get(key) != review.get(key):
                raise RuntimeError(f"战略核验 overlay 元数据不一致：{entry_id} / {key}")

    baseline_ids = {
        str(entry["entry_id"])
        for entry in entries
        if entry.get("manual_review")
        and entry.get("verification_status") == "verified_manual_visual"
    }
    if len(baseline_ids) != BASELINE_MANUAL_VERIFIED_COUNT:
        raise RuntimeError(
            f"基线人工视觉核验数量应为 {BASELINE_MANUAL_VERIFIED_COUNT}，"
            f"实际为 {len(baseline_ids)}"
        )
    overlap = baseline_ids & overlay_ids
    if overlap:
        raise RuntimeError(f"战略核验 overlay 与基线重复：{sorted(overlap)}")
    actual_verified_ids = {
        str(entry["entry_id"])
        for entry in entries
        if entry.get("verification_status") == "verified_manual_visual"
    }
    expected_verified_ids = baseline_ids | overlay_ids
    if actual_verified_ids != expected_verified_ids:
        raise RuntimeError(
            "verified_manual_visual 必须等于基线 35 条与战略 overlay 的并集："
            f"expected={len(expected_verified_ids)} / actual={len(actual_verified_ids)}"
        )
    return {
        "path": str(STRATEGIC_REVIEW_PATH.relative_to(ROOT)),
        "review_scope": "writing_foundation",
        "overlay_count": len(overlay_ids),
        "baseline_verified_count": len(baseline_ids),
        "total_verified_expected": len(expected_verified_ids),
        "overlap_with_baseline": 0,
    }


def apply_strategic_review(
    entries: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Promote exact overlay occurrences after stable entry IDs are assigned."""

    overlay_rows = load_strategic_review()
    entry_by_id = {str(entry["entry_id"]): entry for entry in entries}
    for review in overlay_rows:
        entry_id = str(review.get("entry_id", ""))
        entry = entry_by_id.get(entry_id)
        if entry is None:
            # The complete validator below provides the detailed locator error.
            continue
        entry["verification_status"] = "verified_manual_visual"
        entry["strategic_review"] = {
            key: review[key]
            for key in (
                "evidence",
                "review_scope",
                "reviewed_on",
                "match_type",
                "writing_form",
                "notes",
            )
            if key in review
        }
    return overlay_rows, validate_strategic_review(entries, overlay_rows)


def status_rollup(statuses: Iterable[str]) -> str:
    values = set(statuses)
    if values == {"verified_manual_visual"}:
        return "verified_manual_visual"
    if values and all(
        value.startswith(("ocr_consensus", "ocr_majority")) and "pending_review" not in value
        for value in values
    ):
        return "ocr_consensus_unreviewed"
    if "verified_manual_visual" in values or any(
        value.startswith(("ocr_consensus", "ocr_majority")) for value in values
    ):
        return "mixed_with_pending_review"
    return "pending_review_only"


def build_unique(entries: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in entries:
        grouped[str(entry["normalized"])].append(entry)
    unique_rows: list[dict[str, object]] = []
    for normalized in sorted(grouped):
        occurrences = grouped[normalized]
        canonical = next(
            (row for row in occurrences if row["verification_status"] == "verified_manual_visual"),
            occurrences[0],
        )
        unique_rows.append(
            {
                "normalized": normalized,
                "canonical_entry_id": canonical["entry_id"],
                "occurrence_count": len(occurrences),
                "entry_ids": [row["entry_id"] for row in occurrences],
                "sections": list(dict.fromkeys(str(row["section"]) for row in occurrences)),
                "verification_status": status_rollup(str(row["verification_status"]) for row in occurrences),
            }
        )
    return unique_rows


def write_unique(unique_rows: Sequence[dict[str, object]]) -> None:
    jsonl_write(UNIQUE_JSONL_PATH, unique_rows)
    with UNIQUE_TSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(
            ["normalized", "canonical_entry_id", "occurrence_count", "sections", "verification_status"]
        )
        for row in unique_rows:
            writer.writerow(
                [
                    row["normalized"],
                    row["canonical_entry_id"],
                    row["occurrence_count"],
                    "|".join(row["sections"]),
                    row["verification_status"],
                ]
            )
    UNIQUE_TXT_PATH.write_text(
        "\n".join(str(row["normalized"]) for row in unique_rows) + "\n", encoding="utf-8"
    )


def markdown_table_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_wiki(
    source_pdf: Path,
    generated_date: str,
    entries: Sequence[dict[str, object]],
    unique_rows: Sequence[dict[str, object]],
    section_counts: dict[str, int],
    status_counts: Counter[str],
    gate: str,
    manual_audit_rows: Sequence[dict[str, object]],
    strategic_review_rows: Sequence[dict[str, object]],
) -> None:
    sample_rows = [row for row in manual_audit_rows if row.get("sample_member")]
    pre_matches = sum(row["pre_correction_result"] == "match" for row in sample_rows)
    visual_expected = sum(sum(counts) for counts in PAGE_COLUMN_VISUAL_COUNTS.values())
    visual_actual = sum(
        int(row["pdf_page"]) in PAGE_COLUMN_VISUAL_COUNTS for row in entries
    )
    lines = [
        "# 大纲词汇参考库",
        "",
        "status: derived_ocr_reference",
        f"generated_date: {generated_date}",
        f"source_id: {SOURCE_ID}",
        "",
        "本页是历史版《大纲词汇背诵宝典 英语一》的只读词头参考入口。它只登记两栏英文词头，不保存释义、音标、书内词频或学习者掌握状态，也不等同于 `bank/master_bank.csv`。",
        "",
        "## 来源与边界",
        "",
        f"- 外部只读 PDF：`{source_pdf}`",
        f"- PDF：{EXPECTED_PDF_PAGES} 页；正文 OCR 范围：p{BODY_START_PAGE}-p{BODY_END_PAGE}。",
        "- 方法：Poppler 渲染 + Tesseract `eng`；300 dpi 与 150 dpi 两次独立词头栏 OCR 后按纵坐标对齐。",
        "- 来源角色：`derived source`；PDF 是历史私人学习资料，不据此宣称为当前年份官方大纲。",
        "- 版权边界：只抽取词头；释义、例句、音标和词频说明均未结构化复制。",
        "",
        "## 当前门禁",
        "",
        f"- 印刷目标：{EXPECTED_TOTAL} 条（4249 / 1281 / 216）。",
        f"- OCR 实际词头记录：{len(entries)} 条；去重后：{len(unique_rows)} 个 normalized item。",
        f"- 数量门禁：`{gate}`。数量不等时保留真实 OCR 结果并进入复核，脚本不会补造记录。",
        f"- 人工目视核验：{status_counts.get('verified_manual_visual', 0)} 条；其余 OCR 共识仍标 `unreviewed`。",
        f"- 其中作文地基战略视觉核验 overlay：{len(strategic_review_rows)} 条；它们与原 35 条基线核验无重叠。",
        f"- 可检索候选层：{len(entries)} 条 occurrence；现场目视后可直接引用层：{status_counts.get('verified_manual_visual', 0)} 条；其余 {len(entries) - status_counts.get('verified_manual_visual', 0)} 条命中时执行 on-demand visual。",
        f"- 人工分层边界抽样：校正前 {pre_matches}/{len(sample_rows)}（{pre_matches / len(sample_rows):.1%}），校正层后 {len(sample_rows)}/{len(sample_rows)}；另有 {len(MANUAL_CORRECTION_CASES)} 项定向校正、{len(MANUAL_REJECT_CASES)} 项定向 reject。该抽样刻意覆盖难页，不外推为全库准确率。",
        f"- 独立页栏覆盖抽样：15 页 × 2 栏，印刷 {visual_expected} 条，产物 {visual_actual} 条；逐栏计数见验证报告。",
        f"- 待复核：{sum(count for status, count in status_counts.items() if 'pending_review' in status)} 条。",
        "- 完整差异、边界证据和疑似错误见 [[wiki/validation/大纲词汇参考库验证报告|验证报告]]。",
        "",
        "| 分部 | 印刷目标 | OCR 实际 | 差额（目标-实际） |",
        "|---|---:|---:|---:|",
    ]
    for spec in SECTIONS:
        actual = section_counts.get(spec.key, 0)
        lines.append(f"| {spec.label} | {spec.printed_target} | {actual} | {spec.printed_target - actual:+d} |")
    lines.extend(
        [
            "",
            "## 机器可读入口",
            "",
            "- 全量出现记录：`raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_entries.jsonl`",
            "- 去重 JSONL：`raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_unique.jsonl`",
            "- 去重 TSV：`raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_unique.tsv`",
            "- 纯词表：`raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_unique.txt`",
            "- 待复核队列：`raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_pending_review.jsonl`",
            "- 人工分层抽样与校正：`raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_manual_review.jsonl`",
            "- 作文地基战略视觉核验：`raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_strategic_review.jsonl`",
            "- OCR 原始文本 / TSV：`raw/reference_sources/syllabus_vocabulary/ocr/{300dpi,150dpi,450dpi}/`（450 dpi 仅用于双跑不一致栏位）。",
            "- 来源与构建参数：`raw/reference_sources/syllabus_vocabulary/manifest.json`",
            "",
            "## 后续造句引用规则",
            "",
            "1. 先按 normalized 精确查询去重 JSONL / TSV；不要只凭记忆判断某词在大纲参考层。",
            "2. 只有 `verification_status=verified_manual_visual` 可直接作为 `SYL-VOCAB-...` 引用；`ocr_consensus_*_unreviewed` 只是多次 OCR 同形，使用前仍须回看 PDF 页。",
            "3. 引用格式：`SYL-VOCAB-Pxxx-L/Rxx｜normalized`。目标生词本身命中时可以承担大纲词汇依据。",
            "4. 本参考层只证明“该历史资料中出现了这个词头”，不证明用户不会、不更新 `appear_count` / `last_seen`，也不写 `master_bank.csv`。",
            "5. 例句还必须同时满足 [[schema/reference_grounded_examples|用户措辞 / 明确错词证据 + approved / corrected 作文句型 + approved / corrected 作文词组 + verified 大纲 occurrence 四层地基]]；自然度优先，禁止机械堆词。",
            "",
            "## 精确查询示例",
            "",
            "```bash",
            "rg -n '\"normalized\": \"target-word\"' raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_unique.jsonl",
            "rg -n '\"normalized\": \"target-word\"' raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_entries.jsonl",
            "```",
            "",
        ]
    )
    WIKI_PATH.parent.mkdir(parents=True, exist_ok=True)
    WIKI_PATH.write_text("\n".join(lines), encoding="utf-8")


def write_report(
    source_pdf: Path,
    generated_date: str,
    source_hash: str,
    entries: Sequence[dict[str, object]],
    unique_rows: Sequence[dict[str, object]],
    section_counts: dict[str, int],
    status_counts: Counter[str],
    boundaries: Sequence[Boundary],
    rejected: Sequence[dict[str, object]],
    anomalies: Sequence[dict[str, object]],
    missing_pages: Sequence[int],
    gate: str,
    manual_audit_rows: Sequence[dict[str, object]],
    strategic_review_rows: Sequence[dict[str, object]],
) -> None:
    pending = [entry for entry in entries if "pending_review" in str(entry["verification_status"])]
    sample_rows = [row for row in manual_audit_rows if row.get("sample_member")]
    manual_pre_matches = sum(
        row["pre_correction_result"] == "match" for row in sample_rows
    )
    visual_expected = sum(sum(counts) for counts in PAGE_COLUMN_VISUAL_COUNTS.values())
    visual_actual = sum(
        int(row["pdf_page"]) in PAGE_COLUMN_VISUAL_COUNTS for row in entries
    )
    lines = [
        "# 大纲词汇参考库验证报告",
        "",
        f"生成日期：{generated_date}",
        "",
        "## 结论",
        "",
        f"- 数量门禁：`{gate}`。",
        f"- 书内印刷目标：{EXPECTED_TOTAL} 条；OCR 实际：{len(entries)} 条；总差额（目标-实际）：{EXPECTED_TOTAL - len(entries):+d}。",
        f"- 去重后 normalized item：{len(unique_rows)}。去重数不与印刷条目数直接比较。",
        f"- 人工目视核验：{status_counts.get('verified_manual_visual', 0)}；显式 pending：{len(pending)}；被拒 OCR 行：{len(rejected)}。",
        f"- 作文地基战略视觉核验 overlay：{len(strategic_review_rows)} 条；与基线 {BASELINE_MANUAL_VERIFIED_COUNT} 条无重叠。",
        f"- 层级：extracted candidates={len(entries)}；verified_manual_visual={status_counts.get('verified_manual_visual', 0)}；on_demand_visual_required={len(entries) - status_counts.get('verified_manual_visual', 0)}。",
        f"- 未抽到词头的正文页：{', '.join(f'p{p}' for p in missing_pages) if missing_pages else '无'}。",
        "- 未为满足 5746 目标而补齐、复制或猜造任何词头。",
        f"- 人工分层边界抽样：校正前 {manual_pre_matches}/{len(sample_rows)}（{manual_pre_matches / len(sample_rows):.1%}）；校正层后 {len(sample_rows)}/{len(sample_rows)}。另有 {len(MANUAL_CORRECTION_CASES)} 项定向校正、{len(MANUAL_REJECT_CASES)} 项定向 reject。样本刻意覆盖 p71 / p88 边界，不可外推为全库随机准确率。",
        f"- 独立页栏覆盖抽样：15 页 × 2 栏，印刷 {visual_expected} 条，产物 {visual_actual} 条。",
        "",
        "## 来源证据",
        "",
        f"- PDF：`{source_pdf}`",
        f"- SHA-256：`{source_hash}`",
        f"- 页数：{EXPECTED_PDF_PAGES}；OCR 范围：p{BODY_START_PAGE}-p{BODY_END_PAGE}。",
        f"- 战略视觉核验 overlay：`{STRATEGIC_REVIEW_PATH.relative_to(ROOT)}`（{len(strategic_review_rows)} 条）。",
        "- 数据字段仅含词头定位、双 OCR 候选、分部和验证状态；没有释义、频次、词性、中文或掌握度字段。",
        "",
        "## 分部数量门禁",
        "",
        "| 分部 | 印刷目标 | OCR 实际 | 差额（目标-实际） | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    for spec in SECTIONS:
        actual = section_counts.get(spec.key, 0)
        diff = spec.printed_target - actual
        state = "PASS" if diff == 0 else "NEEDS_REVIEW"
        lines.append(f"| {spec.label} | {spec.printed_target} | {actual} | {diff:+d} | {state} |")

    lines.extend(
        [
            "",
            "## 人工目视抽样与受控校正",
            "",
            "| audit_id | 类型 | PDF 页 / 栏 | 校正前 | 期望 / 动作 | 结果 | entry_id / 定位 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for audit in manual_audit_rows:
        expected_or_action = (
            audit.get("expected")
            if audit.get("action") != "reject"
            else f"reject（{audit.get('neighbor_before')} → {audit.get('neighbor_after')} 间无词头）"
        )
        locator = audit.get("entry_id") or audit.get("pre_filter_locator")
        lines.append(
            f"| {audit['audit_id']} | {audit.get('review_kind')} | p{audit['pdf_page']} / {audit['column']} | "
            f"{markdown_table_escape(audit.get('observed_before'))} | {markdown_table_escape(expected_or_action)} | "
            f"{audit.get('post_correction_result')} | {locator} |"
        )

    lines.extend(
        [
            "",
            "### 独立页栏覆盖抽样（15 页 × 2 栏）",
            "",
            "| PDF 页 | 左栏印刷 / 产物 | 右栏印刷 / 产物 | 状态 |",
            "|---:|---:|---:|---|",
        ]
    )
    for page, (expected_left, expected_right) in PAGE_COLUMN_VISUAL_COUNTS.items():
        actual_left = sum(
            int(row["pdf_page"]) == page and row["column"] == "left" for row in entries
        )
        actual_right = sum(
            int(row["pdf_page"]) == page and row["column"] == "right" for row in entries
        )
        state = (
            "PASS"
            if (actual_left, actual_right) == (expected_left, expected_right)
            else "NEEDS_REVIEW"
        )
        lines.append(
            f"| {page} | {expected_left} / {actual_left} | {expected_right} / {actual_right} | {state} |"
        )

    lines.extend(["", "## 同页分部边界", ""])
    for boundary in boundaries:
        spec = SECTION_BY_KEY[boundary.section_key]
        lines.append(f"### {spec.label} 起点（PDF p{boundary.pdf_page}）")
        lines.append("")
        lines.append(f"- 边界状态：`{boundary.status}`")
        columns = boundary.evidence["columns"]
        for column in ("left", "right"):
            evidence = columns[column]
            lines.append(
                f"- {column}：期望 `{evidence['expected_start']}`，观察 `{evidence['observed_start']}`，"
                f"上一词 `{evidence['previous']}`，gap={evidence['gap_ratio']}，split={evidence['split_ratio']}。"
            )
        lines.append("")

    lines.extend(["## 验证状态分布", "", "| verification_status | 数量 |", "|---|---:|"])
    for status, count in status_counts.most_common():
        lines.append(f"| `{status}` | {count} |")

    lines.extend(
        [
            "",
            "## 疑似错误与缺口解释",
            "",
            "- `dual_ocr_*mismatch*`：300 dpi 与 150 dpi 在同一纵坐标读到不同词头；保留两边原值，未自动猜改。",
            "- `ocr_consensus_triple_unreviewed` / `ocr_majority_unreviewed`：只在双跑不一致栏位加做 450 dpi；它们只表示 OCR 同形或多数票，不等于人工核真。",
            "- `triple_ocr_no_majority_pending_review`：三跑没有形成同形多数票；只保留直接观察到的候选，不标 verified。",
            "- `verified_manual_visual`：已对照渲染 PDF 的人工抽样 / 校正项；只有此状态可直接用于后续来源 ID。",
            "- `*_only_pending_review`：另一分辨率未能对齐同一行，可能是漏识别或行拆分。",
            "- `alphabetic_order_anomaly`：跨首字母顺序出现回退，通常值得回看 PDF；它只是一条启发式告警。",
            "- `rejected_ocr`：词头栏里的续行、页码、字母图标或不合词头形状的 OCR 噪声；不计入实际词头数。",
            "- 数量差额只表示“OCR 与印刷声明尚未闭合”，不能直接断言每个差额都是漏词，也不能据此复制邻词补数。",
            "",
            f"### 待复核记录（展示前 {min(200, len(pending))} / {len(pending)}；全量见 JSONL）",
            "",
            "| entry_id | PDF 页 | 栏 | raw 300 | raw 150 | raw 450 | normalized | 状态 / 告警 |",
            "|---|---:|---|---|---|---|---|---|",
        ]
    )
    for entry in pending[:200]:
        state = f"{entry['verification_status']} / {','.join(entry['warnings']) or '-'}"
        lines.append(
            "| {entry_id} | {pdf_page} | {column} | {raw_ocr_300} | {raw_ocr_150} | {raw_ocr_450} | {normalized} | {state} |".format(
                entry_id=entry["entry_id"],
                pdf_page=entry["pdf_page"],
                column=entry["column"],
                raw_ocr_300=markdown_table_escape(entry["raw_ocr_300"]),
                raw_ocr_150=markdown_table_escape(entry["raw_ocr_150"]),
                raw_ocr_450=markdown_table_escape(entry["raw_ocr_450"]),
                normalized=markdown_table_escape(entry["normalized"]),
                state=markdown_table_escape(state),
            )
        )

    lines.extend(
        [
            "",
            f"### 字母顺序告警（展示前 {min(100, len(anomalies))} / {len(anomalies)}）",
            "",
            "| PDF 页 | 栏 | 前一词 | 当前词 | entry_id |",
            "|---:|---|---|---|---|",
        ]
    )
    for item in anomalies[:100]:
        lines.append(
            f"| {item['pdf_page']} | {item['column']} | {markdown_table_escape(item['previous'])} | "
            f"{markdown_table_escape(item['current'])} | {item['entry_id']} |"
        )

    lines.extend(
        [
            "",
            f"### 被拒 OCR 行（展示前 {min(100, len(rejected))} / {len(rejected)}）",
            "",
            "| PDF 页 | DPI | 栏 | raw | 原因 |",
            "|---:|---:|---|---|---|",
        ]
    )
    for item in rejected[:100]:
        lines.append(
            f"| {item['pdf_page']} | {item['dpi']} | {item['column']} | "
            f"{markdown_table_escape(item.get('raw', ''))} | {item['reason']} |"
        )
    lines.extend(
        [
            "",
            "## 复核优先级",
            "",
            "1. 先核对两处分部同页边界及两栏起始锚点。",
            "2. 再按待复核 JSONL 处理 dual OCR 不一致与单分辨率记录。",
            "3. 最后以每部分印刷目标检查仍有的数量差额；只依据原 PDF 补正，不按数量反推词头。",
            "4. 所有校正应重新运行本脚本或维护独立人工校正层；不要把 OCR 候选写进 `master_bank.csv`。",
            "",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def build(args: argparse.Namespace) -> dict[str, object]:
    ensure_dependencies()
    source_pdf = args.pdf.expanduser().resolve()
    if not source_pdf.exists():
        raise FileNotFoundError(source_pdf)
    pages = pdf_page_count(source_pdf)
    if pages != EXPECTED_PDF_PAGES:
        raise RuntimeError(f"PDF 页数与已审计版本不一致：expected={EXPECTED_PDF_PAGES}, actual={pages}")
    source_hash = sha256(source_pdf)
    generated_date = args.date

    old_manifest = None
    if MANIFEST_PATH.exists():
        try:
            old_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old_manifest = None
    source_changed = bool(old_manifest and old_manifest.get("source_sha256") != source_hash)
    refresh = args.refresh_ocr or source_changed

    crop_selection_by_page: dict[int, dict[str, object]] = {}
    if CROP_SELECTION_PATH.exists() and not refresh:
        try:
            saved_selection = json.loads(CROP_SELECTION_PATH.read_text(encoding="utf-8"))
            if saved_selection.get("source_sha256") == source_hash:
                crop_selection_by_page = {
                    int(item["pdf_page"]): item for item in saved_selection.get("pages", [])
                }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            crop_selection_by_page = {}

    all_rows: list[dict[str, object]] = []
    all_rejected: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="syllabus_vocab_ocr_") as tmp:
        temp_dir = Path(tmp)
        for page in range(args.start_page, args.end_page + 1):
            if page not in crop_selection_by_page:
                crop_selection_by_page[page] = build_adaptive_right_ocr(
                    source_pdf, page, temp_dir
                )
            selected_start = float(crop_selection_by_page[page]["selected_start_ratio"])
            selected_right_ratio = (
                selected_start,
                CROP_RATIOS["right"][1],
                CROP_RATIOS["right"][2],
                CROP_RATIOS["right"][3],
            )
            for column in ("left", "right"):
                rows_by_dpi: dict[int, list[OCRRow]] = {}
                for dpi in (args.primary_dpi, args.secondary_dpi):
                    _txt, tsv = ensure_ocr(
                        source_pdf,
                        page,
                        dpi,
                        column,
                        temp_dir,
                        # Adaptive selection already publishes the winning
                        # right-column OCR cache.  The left lane follows the
                        # ordinary refresh policy.
                        refresh=refresh if column == "left" else False,
                        crop_ratios=selected_right_ratio if column == "right" else None,
                    )
                    rows, rejected = parse_tsv(tsv, page, dpi, column)
                    rows_by_dpi[dpi] = rows
                    all_rejected.extend(rejected)
                fused = fuse_rows(
                    rows_by_dpi[args.primary_dpi], rows_by_dpi[args.secondary_dpi]
                )
                needs_tertiary = any(
                    "pending_review" in str(row["verification_status"])
                    or len(str(row["normalized"])) <= 1
                    for row in fused
                )
                if needs_tertiary:
                    _txt_450, tsv_450 = ensure_ocr(
                        source_pdf,
                        page,
                        450,
                        column,
                        temp_dir,
                        refresh=refresh,
                        crop_ratios=selected_right_ratio if column == "right" else None,
                    )
                    rows_450, rejected_450 = parse_tsv(tsv_450, page, 450, column)
                    all_rejected.extend(rejected_450)
                    fused, ignored_450 = adjudicate_with_tertiary(fused, rows_450)
                    all_rejected.extend(ignored_450)
                all_rows.extend(fused)
            if (page - args.start_page + 1) % 5 == 0 or page == args.end_page:
                print(
                    f"OCR progress: p{page}/{args.end_page}, fused_rows={len(all_rows)}",
                    flush=True,
                )

    write_json(
        CROP_SELECTION_PATH,
        {
            "schema": "syllabus_vocabulary_right_crop_selection_v1",
            "generated_date": generated_date,
            "source_sha256": source_hash,
            "candidate_start_ratios": list(RIGHT_CROP_START_CANDIDATES),
            "score_note": "dual exact agreement rewarded; single/odd-short/unmatched/mismatch/alphabetic descents penalized",
            "pages": [crop_selection_by_page[page] for page in sorted(crop_selection_by_page)],
        },
    )

    manual_audit_rows = apply_manual_audit(all_rows)
    all_rows, manual_reject_rows = apply_manual_rejects(all_rows, all_rejected)
    manual_audit_rows.extend(manual_reject_rows)
    all_rows = filter_unresolved_single_characters(all_rows, all_rejected)
    all_rows = filter_layout_noise(all_rows, all_rejected)
    boundaries = detect_boundaries(all_rows)
    for row in all_rows:
        section_key, boundary_warning = assign_section(row, boundaries)
        row["source_id"] = SOURCE_ID
        row["printed_page"] = int(row["pdf_page"]) - 4
        row["section"] = section_key
        row["section_label"] = SECTION_BY_KEY[section_key].label
        if boundary_warning:
            row["warnings"].append(boundary_warning)
            if row["verification_status"] != "verified_manual_visual":
                row["verification_status"] = "boundary_assignment_pending_review"

    # Reading order is page -> section segment -> left column -> right column.
    section_order = {spec.key: index for index, spec in enumerate(SECTIONS, start=1)}
    all_rows.sort(
        key=lambda row: (
            section_order[str(row["section"])],
            int(row["pdf_page"]),
            0 if row["column"] == "left" else 1,
            float(row["y_ratio"]),
        )
    )
    section_sequences: Counter[str] = Counter()
    page_column_sequences: Counter[tuple[int, str]] = Counter()
    for row in all_rows:
        section_key = str(row["section"])
        section_sequences[section_key] += 1
        page_column_key = (int(row["pdf_page"]), str(row["column"]))
        page_column_sequences[page_column_key] += 1
        col_letter = "L" if row["column"] == "left" else "R"
        row["entry_id"] = (
            f"SYL-VOCAB-P{int(row['pdf_page']):03d}-{col_letter}{page_column_sequences[page_column_key]:02d}"
        )
        row["section_sequence"] = section_sequences[section_key]

    # Apply the curated writing-foundation visual-review overlay only after
    # stable occurrence IDs exist and before unique/status rollups are built.
    strategic_review_rows, strategic_review_stats = apply_strategic_review(all_rows)

    audited_entries = {
        str(row["manual_audit_id"]): row for row in all_rows if row.get("manual_audit_id")
    }
    for audit in manual_audit_rows:
        if audit.get("action") == "reject":
            audit["entry_id"] = None
            audit["observed_after"] = None
            audit["verification_status"] = "rejected_manual_visual"
            continue
        entry = audited_entries.get(str(audit["audit_id"]))
        if entry is None:
            raise RuntimeError(f"人工抽样项在过滤后丢失：{audit['audit_id']}")
        audit["entry_id"] = entry["entry_id"]
        audit["observed_after"] = entry["normalized"]
        audit["verification_status"] = entry["verification_status"]
    jsonl_write(MANUAL_REVIEW_PATH, manual_audit_rows)
    sample_audit_rows = [audit for audit in manual_audit_rows if audit.get("sample_member")]
    manual_pre_matches = sum(
        audit["pre_correction_result"] == "match" for audit in sample_audit_rows
    )
    manual_post_matches = sum(
        audit["post_correction_result"] == "match" for audit in sample_audit_rows
    )

    anomalies = alphabetic_warnings(all_rows)
    unique_rows = build_unique(all_rows)
    section_counts = {spec.key: section_sequences[spec.key] for spec in SECTIONS}
    status_counts: Counter[str] = Counter(str(row["verification_status"]) for row in all_rows)
    page_column_audit: list[dict[str, object]] = []
    for page, (expected_left, expected_right) in PAGE_COLUMN_VISUAL_COUNTS.items():
        for column, expected in (("left", expected_left), ("right", expected_right)):
            actual = sum(
                int(row["pdf_page"]) == page and row["column"] == column for row in all_rows
            )
            page_column_audit.append(
                {
                    "pdf_page": page,
                    "column": column,
                    "expected_printed_headwords": expected,
                    "actual_occurrences": actual,
                    "match": actual == expected,
                }
            )
    pages_with_entries = {int(row["pdf_page"]) for row in all_rows}
    missing_pages = [
        page for page in range(args.start_page, args.end_page + 1) if page not in pages_with_entries
    ]
    exact_sections = all(section_counts[spec.key] == spec.printed_target for spec in SECTIONS)
    gate = "PASS" if len(all_rows) == EXPECTED_TOTAL and exact_sections and not missing_pages else "NEEDS_REVIEW"

    jsonl_write(ENTRIES_PATH, all_rows)
    write_unique(unique_rows)
    pending = [row for row in all_rows if "pending_review" in str(row["verification_status"])]
    jsonl_write(PENDING_PATH, pending)
    jsonl_write(REJECTED_PATH, all_rejected)

    validation = {
        "schema": "syllabus_vocabulary_validation_v1",
        "generated_date": generated_date,
        "source_id": SOURCE_ID,
        "source_sha256": source_hash,
        "printed_target_total": EXPECTED_TOTAL,
        "actual_occurrences": len(all_rows),
        "actual_unique_normalized": len(unique_rows),
        "gap_target_minus_actual": EXPECTED_TOTAL - len(all_rows),
        "section_counts": [
            {
                "section": spec.key,
                "label": spec.label,
                "printed_target": spec.printed_target,
                "actual": section_counts[spec.key],
                "gap_target_minus_actual": spec.printed_target - section_counts[spec.key],
            }
            for spec in SECTIONS
        ],
        "verification_status_counts": dict(status_counts),
        "pending_review_count": len(pending),
        "manual_review": {
            "sample_design": "stratified_boundary_heavy_10_per_section",
            "sample_size": len(sample_audit_rows),
            "pre_correction_matches": manual_pre_matches,
            "pre_correction_accuracy": round(manual_pre_matches / len(sample_audit_rows), 4),
            "post_correction_matches": manual_post_matches,
            "post_correction_accuracy_within_sample": round(
                manual_post_matches / len(sample_audit_rows), 4
            ),
            "targeted_visual_corrections_outside_sample": len(MANUAL_CORRECTION_CASES),
            "targeted_visual_rejects": len(MANUAL_REJECT_CASES),
            "page_column_coverage_audit": {
                "sample_page_columns": len(page_column_audit),
                "expected_printed_headwords": sum(
                    int(row["expected_printed_headwords"]) for row in page_column_audit
                ),
                "actual_occurrences": sum(
                    int(row["actual_occurrences"]) for row in page_column_audit
                ),
                "matching_page_columns": sum(bool(row["match"]) for row in page_column_audit),
                "rows": page_column_audit,
            },
            "scope_warning": "boundary-heavy audit; do not extrapolate as whole-corpus accuracy",
            "path": str(MANUAL_REVIEW_PATH.relative_to(ROOT)),
        },
        "strategic_visual_review": strategic_review_stats,
        "rejected_ocr_line_count": len(all_rejected),
        "alphabetic_anomaly_count": len(anomalies),
        "missing_body_pages": missing_pages,
        "boundary_detection": [
            {
                "section": boundary.section_key,
                "pdf_page": boundary.pdf_page,
                "split_by_column": boundary.split_by_column,
                "status": boundary.status,
                "evidence": boundary.evidence,
            }
            for boundary in boundaries
        ],
        "adaptive_right_crop_selection": str(CROP_SELECTION_PATH.relative_to(ROOT)),
        "count_gate": gate,
        "citation_gate": "on_demand_visual_required_except_verified_manual_visual",
        "padding_or_fabrication": False,
    }
    write_json(VALIDATION_JSON_PATH, validation)
    write_json(
        MANIFEST_PATH,
        {
            "schema": "syllabus_vocabulary_headword_reference_v1",
            "generated_date": generated_date,
            "source_id": SOURCE_ID,
            "source_role": "derived_ocr_reference_from_private_study_pdf",
            "source_pdf": str(source_pdf),
            "source_sha256": source_hash,
            "pdf_pages": pages,
            "body_pdf_pages": [args.start_page, args.end_page],
            "ocr": {
                "renderer": "pdftoppm grayscale",
                "engine": "tesseract eng psm=6",
                "primary_dpi": args.primary_dpi,
                "secondary_dpi": args.secondary_dpi,
                "tertiary_dpi": 450,
                "tertiary_policy": "only columns containing dual-OCR pending rows; majority vote only",
                "left_crop_ratios": CROP_RATIOS["left"],
                "right_crop_end_ratios": CROP_RATIOS["right"][1:],
                "right_crop_start_strategy": "per-page dual-OCR candidate scoring",
                "right_crop_start_candidates": list(RIGHT_CROP_START_CANDIDATES),
                "right_crop_selection": str(CROP_SELECTION_PATH.relative_to(ROOT)),
                "structured_scope": "headwords_only",
                "excluded": [
                    "definitions",
                    "phonetics",
                    "translations",
                    "printed_frequency_superscripts",
                    "learner_mastery",
                ],
            },
            "printed_targets": {
                "total": EXPECTED_TOTAL,
                **{spec.key: spec.printed_target for spec in SECTIONS},
            },
            "strategic_visual_review": strategic_review_stats,
            "outputs": {
                "entries_jsonl": str(ENTRIES_PATH.relative_to(ROOT)),
                "unique_jsonl": str(UNIQUE_JSONL_PATH.relative_to(ROOT)),
                "unique_tsv": str(UNIQUE_TSV_PATH.relative_to(ROOT)),
                "unique_txt": str(UNIQUE_TXT_PATH.relative_to(ROOT)),
                "pending_review_jsonl": str(PENDING_PATH.relative_to(ROOT)),
                "rejected_ocr_jsonl": str(REJECTED_PATH.relative_to(ROOT)),
                "manual_review_jsonl": str(MANUAL_REVIEW_PATH.relative_to(ROOT)),
                "strategic_review_jsonl": str(STRATEGIC_REVIEW_PATH.relative_to(ROOT)),
                "validation_json": str(VALIDATION_JSON_PATH.relative_to(ROOT)),
                "right_crop_selection_json": str(CROP_SELECTION_PATH.relative_to(ROOT)),
                "wiki": str(WIKI_PATH.relative_to(ROOT)),
                "report": str(REPORT_PATH.relative_to(ROOT)),
            },
        },
    )
    write_wiki(
        source_pdf,
        generated_date,
        all_rows,
        unique_rows,
        section_counts,
        status_counts,
        gate,
        manual_audit_rows,
        strategic_review_rows,
    )
    write_report(
        source_pdf,
        generated_date,
        source_hash,
        all_rows,
        unique_rows,
        section_counts,
        status_counts,
        boundaries,
        all_rejected,
        anomalies,
        missing_pages,
        gate,
        manual_audit_rows,
        strategic_review_rows,
    )
    result = {
        "source": str(source_pdf),
        "entries": len(all_rows),
        "unique": len(unique_rows),
        "section_counts": section_counts,
        "printed_target": EXPECTED_TOTAL,
        "gap_target_minus_actual": EXPECTED_TOTAL - len(all_rows),
        "pending_review": len(pending),
        "verified_manual_visual": status_counts.get("verified_manual_visual", 0),
        "strategic_visual_review": len(strategic_review_rows),
        "on_demand_visual_required": len(all_rows)
        - status_counts.get("verified_manual_visual", 0),
        "rejected_ocr_lines": len(all_rejected),
        "count_gate": gate,
        "wiki": str(WIKI_PATH.relative_to(ROOT)),
        "report": str(REPORT_PATH.relative_to(ROOT)),
    }
    if args.strict_count and gate != "PASS":
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def load_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc
    return rows


def verify_outputs(*, strict_count: bool) -> dict[str, object]:
    required_paths = (
        ENTRIES_PATH,
        UNIQUE_JSONL_PATH,
        UNIQUE_TSV_PATH,
        UNIQUE_TXT_PATH,
        PENDING_PATH,
        REJECTED_PATH,
        MANUAL_REVIEW_PATH,
        STRATEGIC_REVIEW_PATH,
        MANIFEST_PATH,
        VALIDATION_JSON_PATH,
        CROP_SELECTION_PATH,
        WIKI_PATH,
        REPORT_PATH,
    )
    missing = [str(path.relative_to(ROOT)) for path in required_paths if not path.exists()]
    if missing:
        raise SystemExit(f"缺少生成产物：{missing}")
    entries = load_jsonl(ENTRIES_PATH)
    unique_rows = load_jsonl(UNIQUE_JSONL_PATH)
    manual_review = load_jsonl(MANUAL_REVIEW_PATH)
    strategic_review = load_strategic_review()
    rejected_rows = load_jsonl(REJECTED_PATH)
    required_fields = {
        "entry_id",
        "source_id",
        "pdf_page",
        "column",
        "raw",
        "normalized",
        "section",
        "verification_status",
    }
    failures: list[str] = []
    ids: set[str] = set()
    for index, row in enumerate(entries, start=1):
        missing_fields = required_fields - set(row)
        if missing_fields:
            failures.append(f"row {index}: missing {sorted(missing_fields)}")
        forbidden = FORBIDDEN_STRUCTURED_FIELDS & set(row)
        if forbidden:
            failures.append(f"row {index}: forbidden fields {sorted(forbidden)}")
        entry_id = str(row.get("entry_id"))
        if entry_id in ids:
            failures.append(f"duplicate entry_id: {entry_id}")
        ids.add(entry_id)
        page = int(row.get("pdf_page", 0))
        if not BODY_START_PAGE <= page <= BODY_END_PAGE:
            failures.append(f"row {index}: page out of range {page}")
        if row.get("column") not in {"left", "right"}:
            failures.append(f"row {index}: invalid column {row.get('column')}")
        if not plausible_headword(str(row.get("normalized", ""))):
            failures.append(f"row {index}: invalid normalized {row.get('normalized')!r}")
        status = str(row.get("verification_status", ""))
        if status.startswith("verified_") and status != "verified_manual_visual":
            failures.append(f"row {index}: OCR-only status must not claim verified: {status}")

    expected_unique = len({str(row["normalized"]) for row in entries})
    if expected_unique != len(unique_rows):
        failures.append(f"unique mismatch: expected={expected_unique} actual={len(unique_rows)}")
    validation = json.loads(VALIDATION_JSON_PATH.read_text(encoding="utf-8"))
    if validation.get("actual_occurrences") != len(entries):
        failures.append("validation actual_occurrences mismatch")
    if validation.get("actual_unique_normalized") != len(unique_rows):
        failures.append("validation actual_unique_normalized mismatch")
    if validation.get("padding_or_fabrication") is not False:
        failures.append("padding_or_fabrication must be false")
    try:
        strategic_stats = validate_strategic_review(entries, strategic_review)
    except RuntimeError as exc:
        failures.append(str(exc))
        strategic_stats = None
    validation_strategic = validation.get("strategic_visual_review")
    if not isinstance(validation_strategic, dict):
        failures.append("validation strategic_visual_review missing")
    elif strategic_stats is not None and validation_strategic != strategic_stats:
        failures.append("validation strategic_visual_review mismatch")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_strategic = manifest.get("strategic_visual_review")
    if not isinstance(manifest_strategic, dict):
        failures.append("manifest strategic_visual_review missing")
    elif strategic_stats is not None and manifest_strategic != strategic_stats:
        failures.append("manifest strategic_visual_review mismatch")
    if manifest.get("outputs", {}).get("strategic_review_jsonl") != str(
        STRATEGIC_REVIEW_PATH.relative_to(ROOT)
    ):
        failures.append("manifest strategic_review_jsonl path mismatch")

    section_counts = Counter(str(row["section"]) for row in entries)
    for spec in SECTIONS:
        if section_counts[spec.key] != spec.printed_target:
            failures.append(
                f"section count mismatch {spec.key}: expected={spec.printed_target} "
                f"actual={section_counts[spec.key]}"
            )

    def matching(page: int, column: str, normalized: str) -> list[dict[str, object]]:
        return [
            row
            for row in entries
            if int(row["pdf_page"]) == page
            and row["column"] == column
            and row["normalized"] == normalized
        ]

    verified_anchors = (
        (71, "right", "zoo"),
        (88, "right", "zinc"),
        (88, "right", "zip"),
        (88, "right", "cynic"),
        (74, "right", "cradle"),
        (82, "right", "recipe"),
        (62, "right", "sudden"),
        (62, "right", "superficial"),
        (72, "right", "brass"),
    )
    for page, column, normalized in verified_anchors:
        hits = matching(page, column, normalized)
        if len(hits) != 1:
            failures.append(f"manual anchor mismatch p{page}/{column}/{normalized}: {len(hits)}")
        elif hits[0]["verification_status"] != "verified_manual_visual":
            failures.append(
                f"manual anchor not verified p{page}/{column}/{normalized}: "
                f"{hits[0]['verification_status']}"
            )

    forbidden_anchors = (
        (74, "right", "ih"),
        (74, "right", "wj"),
        (11, "right", "bsr nab"),
        (42, "right", "ji"),
        (62, "right", "af"),
    )
    for page, column, forbidden_value in forbidden_anchors:
        if matching(page, column, forbidden_value):
            failures.append(
                f"manual false positive still published: p{page}/{column}/{forbidden_value}"
            )
    rejected_ids = {row.get("review_id") for row in rejected_rows}
    for expected_reject in (
        "REJECT-P074-R10-IH",
        "REJECT-P074-R22-WJ",
        "REJECT-P011-R-FOOTER",
        "REJECT-P042-R-JI",
        "REJECT-P062-R-AF",
    ):
        if expected_reject not in rejected_ids:
            failures.append(f"missing manual reject evidence: {expected_reject}")

    sample_rows = [row for row in manual_review if row.get("sample_member")]
    if len(sample_rows) != 30:
        failures.append(f"manual sample size mismatch: {len(sample_rows)}")
    if sum(row.get("pre_correction_result") == "match" for row in sample_rows) != 26:
        failures.append("manual pre-correction sample must be 26/30")
    if any(row.get("post_correction_result") != "match" for row in sample_rows):
        failures.append("manual post-correction sample contains non-match")
    baseline_verified = sum(
        row.get("verification_status") == "verified_manual_visual" for row in manual_review
    )
    if baseline_verified != BASELINE_MANUAL_VERIFIED_COUNT:
        failures.append(
            "baseline verified_manual_visual count must remain "
            f"{BASELINE_MANUAL_VERIFIED_COUNT}, actual={baseline_verified}"
        )
    expected_verified = BASELINE_MANUAL_VERIFIED_COUNT + len(strategic_review)
    actual_verified = sum(
        row.get("verification_status") == "verified_manual_visual" for row in entries
    )
    if actual_verified != expected_verified:
        failures.append(
            "verified_manual_visual count must equal baseline plus strategic overlay: "
            f"expected={expected_verified} actual={actual_verified}"
        )

    for page, (expected_left, expected_right) in PAGE_COLUMN_VISUAL_COUNTS.items():
        actual_left = sum(
            int(row["pdf_page"]) == page and row["column"] == "left" for row in entries
        )
        actual_right = sum(
            int(row["pdf_page"]) == page and row["column"] == "right" for row in entries
        )
        if (actual_left, actual_right) != (expected_left, expected_right):
            failures.append(
                f"page-column audit mismatch p{page}: expected={(expected_left, expected_right)} "
                f"actual={(actual_left, actual_right)}"
            )

    duplicate_expectations = {
        "diplomatic": {"part_2_zero_frequency", "part_3_beyond_syllabus"},
        "spectrum": {"part_1_true_exam", "part_3_beyond_syllabus"},
    }
    for normalized, expected_sections in duplicate_expectations.items():
        actual_sections = {
            str(row["section"]) for row in entries if row["normalized"] == normalized
        }
        if not expected_sections <= actual_sections:
            failures.append(
                f"cross-section duplicate lost {normalized}: expected={expected_sections} "
                f"actual={actual_sections}"
            )
    if failures:
        raise SystemExit("\n".join(failures[:100]))

    result = {
        "structural_gate": "PASS",
        "entries": len(entries),
        "unique": len(unique_rows),
        "count_gate": validation.get("count_gate"),
        "printed_target": EXPECTED_TOTAL,
        "gap_target_minus_actual": EXPECTED_TOTAL - len(entries),
        "no_forbidden_structured_fields": True,
        "no_padding_or_fabrication": True,
        "section_counts": dict(section_counts),
        "verified_manual_visual": sum(
            row.get("verification_status") == "verified_manual_visual" for row in entries
        ),
        "baseline_verified_manual_visual": BASELINE_MANUAL_VERIFIED_COUNT,
        "strategic_visual_review": len(strategic_review),
        "manual_sample_pre_correction": "26/30",
        "manual_sample_post_correction": "30/30",
        "citation_gate": validation.get("citation_gate"),
    }
    if strict_count and validation.get("count_gate") != "PASS":
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a headword-only OCR reference from the syllabus vocabulary booklet."
    )
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--start-page", type=int, default=BODY_START_PAGE)
    parser.add_argument("--end-page", type=int, default=BODY_END_PAGE)
    parser.add_argument("--primary-dpi", type=int, default=300)
    parser.add_argument("--secondary-dpi", type=int, default=150)
    parser.add_argument("--refresh-ocr", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--strict-count",
        action="store_true",
        help="Exit non-zero unless actual OCR counts equal all printed targets; never pads records.",
    )
    args = parser.parse_args()
    if args.start_page != BODY_START_PAGE or args.end_page != BODY_END_PAGE:
        raise SystemExit(
            f"当前审计只允许完整正文范围 p{BODY_START_PAGE}-p{BODY_END_PAGE}；"
            "如需抽样，请使用已有 OCR 缓存而不要发布局部参考库。"
        )
    if args.primary_dpi == args.secondary_dpi:
        raise SystemExit("primary-dpi 与 secondary-dpi 必须不同")
    if (args.primary_dpi, args.secondary_dpi) != (300, 150):
        raise SystemExit("当前自适应双 OCR 已审计组合固定为 primary=300 / secondary=150")
    result = verify_outputs(strict_count=args.strict_count) if args.verify_only else build(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
