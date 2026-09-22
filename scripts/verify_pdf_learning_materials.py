#!/usr/bin/env python3
"""End-to-end, read-only verification for the PDF-backed English reference corpus."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []
PASSES: list[str] = []


def check(condition: bool, label: str, evidence: str) -> None:
    if condition:
        PASSES.append(f"PASS｜{label}｜{evidence}")
    else:
        FAILURES.append(f"FAIL｜{label}｜{evidence}")


def load_json(path: str | Path) -> dict:
    with (ROOT / path).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: str | Path) -> list[dict]:
    records: list[dict] = []
    with (ROOT / path).open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                FAILURES.append(f"FAIL｜JSONL 可解析｜{path}:{line_no}: {exc}")
    return records


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_external_sources() -> None:
    exam = load_json("raw/reference_sources/exam_pdf_manifest.json")
    writing = load_json("raw/writing_reference/manifest.json")
    syllabus = load_json("raw/reference_sources/syllabus_vocabulary/manifest.json")

    source_specs: list[tuple[str, Path, str]] = []
    source_specs.extend(
        (item["source_id"], Path(item["external_path"]), item["sha256"])
        for item in exam["sources"]
    )
    source_specs.extend(
        (item["source_id"], Path(item["external_source_path"]), item["sha256"])
        for item in writing["sources"]
    )
    source_specs.append(
        (
            syllabus["source_id"],
            Path(syllabus["source_pdf"]),
            syllabus["source_sha256"],
        )
    )

    check(exam.get("source_count") == 32, "真题 / 解析来源数量", "32/32")
    check(len(writing["sources"]) == 6, "作文来源数量", "6/6")
    check(len(source_specs) == 39, "用户提供 PDF 总数", f"{len(source_specs)}/39")
    check(
        len({str(path.resolve()) for _, path, _ in source_specs}) == 39,
        "PDF 外部路径唯一",
        "39 个独立路径",
    )

    missing: list[str] = []
    mismatched: list[str] = []
    for source_id, path, expected_hash in source_specs:
        if not path.is_file():
            missing.append(source_id)
            continue
        if file_sha256(path) != expected_hash:
            mismatched.append(source_id)
    check(not missing, "39 份源 PDF 可读", f"missing={missing or 'none'}")
    check(not mismatched, "39 份源 PDF SHA-256", f"mismatch={mismatched or 'none'}")


def verify_reading_corpus() -> None:
    paths = sorted((ROOT / "raw/articles/exam-reading-corpus").glob("20??/text-*.json"))
    records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    coverage = Counter((record.get("year"), record.get("text_no")) for record in records)

    expected = {(year, text_no) for year in range(2010, 2025) for text_no in range(1, 5)}
    check(len(records) == 60 and set(coverage) == expected, "历年阅读覆盖", "15 年 × 4 篇 = 60")

    malformed: list[str] = []
    unsafe: list[str] = []
    article_paths: list[str] = []
    forbidden_keys = {
        "answer",
        "answers",
        "answer_key",
        "correct_answer",
        "standard_answer",
        "explanation",
        "analysis_text",
    }
    visible_answer_leaks: list[str] = []
    completed_visible_articles = {(2010, 4), (2011, 1), (2011, 2)}
    for record in records:
        ref = record.get("reference_id", "unknown")
        questions = record.get("questions", [])
        if len(questions) != 5 or any(set(q.get("options", {})) != {"A", "B", "C", "D"} for q in questions):
            malformed.append(ref)
        if record.get("visibility") != "practice_safe" or record.get("answer_visibility") != "hidden_until_review":
            unsafe.append(ref)
        if forbidden_keys.intersection(record):
            unsafe.append(ref)
        if any(forbidden_keys.intersection(question) for question in questions):
            unsafe.append(ref)
        protected_path = record.get("protected_analysis_path", "")
        if (
            record.get("analysis_preprocess_status") != "preprocessed_complete"
            or not protected_path
            or not (ROOT / str(protected_path)).is_file()
        ):
            malformed.append(f"{ref}:protected-analysis")
        article_paths.append(record.get("article_path", ""))
        key = (int(record.get("year", 0)), int(record.get("text_no", 0)))
        article = ROOT / str(record.get("article_path", ""))
        if article.is_file() and key not in completed_visible_articles:
            article_text = article.read_text(encoding="utf-8")
            if re.search(r"(?:正确答案|标准答案|(?<!解析)答案)\s*[：:]\s*[A-D](?:\b|[.．、])", article_text):
                visible_answer_leaks.append(ref)

    check(not malformed, "每篇题目 / 选项结构", f"malformed={malformed or 'none'}")
    check(not unsafe, "自主练习答案保护", f"unsafe={sorted(set(unsafe)) or 'none'}")
    check(
        not visible_answer_leaks,
        "未完成文章页答案隔离",
        f"leaks={visible_answer_leaks or 'none'}",
    )
    check(
        len(set(article_paths)) == 60 and all((ROOT / path).is_file() for path in article_paths),
        "60 篇文章入口",
        "article_path 唯一且存在",
    )


def verify_writing_reference() -> None:
    receipt = load_json("raw/writing_reference/build_receipt.json")
    patterns = load_jsonl("raw/writing_reference/reviewed/approved_patterns.jsonl")
    vocabulary = load_jsonl("raw/writing_reference/reviewed/approved_vocabulary.jsonl")
    receipt_failures = [item for item in receipt.get("checks", []) if item.get("status") != "PASS"]

    check(not receipt_failures, "作文构建回执", f"checks={len(receipt.get('checks', []))}, all PASS")
    check(len(patterns) == 25, "作文审核句型", f"{len(patterns)}/25")
    check(len(vocabulary) == 46, "作文审核词汇 / 搭配", f"{len(vocabulary)}/46")
    usable = patterns + vocabulary
    ids = [item.get("approved_id") for item in usable]
    check(len(ids) == len(set(ids)), "作文白名单 ID 唯一", f"ids={len(ids)}")
    check(
        all(item.get("status") in {"approved", "corrected"} for item in usable),
        "作文白名单状态",
        "仅 approved / corrected",
    )


def verify_syllabus_reference() -> None:
    manifest = load_json("raw/reference_sources/syllabus_vocabulary/manifest.json")
    validation = load_json("raw/reference_sources/syllabus_vocabulary/validation.json")
    entries = load_jsonl("raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_entries.jsonl")
    manual = load_jsonl("raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_manual_review.jsonl")
    strategic = load_jsonl(
        "raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_strategic_review.jsonl"
    )
    pending = load_jsonl("raw/reference_sources/syllabus_vocabulary/syllabus_vocabulary_pending_review.jsonl")

    targets = manifest["printed_targets"]
    section_actual = Counter(item.get("section") for item in entries)
    expected_sections = {
        "part_1_true_exam": targets["part_1_true_exam"],
        "part_2_zero_frequency": targets["part_2_zero_frequency"],
        "part_3_beyond_syllabus": targets["part_3_beyond_syllabus"],
    }
    check(len(entries) == targets["total"] == 5746, "大纲词汇出现记录", f"{len(entries)}/5746")
    check(dict(section_actual) == expected_sections, "大纲词汇分区计数", str(dict(section_actual)))

    entry_ids = [item.get("entry_id") for item in entries]
    check(len(entry_ids) == len(set(entry_ids)), "大纲词汇出现 ID 唯一", f"ids={len(entry_ids)}")
    check(validation.get("padding_or_fabrication") is False, "大纲词汇无填充伪造", "padding_or_fabrication=false")

    manual_verified_ids = {
        item.get("entry_id")
        for item in manual
        if item.get("verification_status", "").startswith("verified_") and item.get("entry_id")
    }
    strategic_verified_ids = {
        item.get("entry_id")
        for item in strategic
        if item.get("verification_status", "").startswith("verified_") and item.get("entry_id")
    }
    entry_verified_ids = {
        item.get("entry_id")
        for item in entries
        if item.get("verification_status", "").startswith("verified_")
    }
    check(
        entry_verified_ids == manual_verified_ids | strategic_verified_ids,
        "大纲词汇人工核验闭环",
        (
            f"verified_entries={len(entry_verified_ids)}, baseline={len(manual_verified_ids)}, "
            f"writing_foundation_overlay={len(strategic_verified_ids)}"
        ),
    )
    check(
        len(manual_verified_ids) == 35
        and len(strategic_verified_ids) == 59
        and not (manual_verified_ids & strategic_verified_ids),
        "作文地基战略视觉核验",
        "35 条基线 + 59 条独立 overlay = 94 条可直接引用 occurrence",
    )
    entry_by_id = {item.get("entry_id"): item for item in entries}
    strategic_locator_errors = [
        item.get("entry_id")
        for item in strategic
        if item.get("entry_id") not in entry_by_id
        or entry_by_id[item.get("entry_id")].get("normalized") != item.get("expected_normalized")
        or entry_by_id[item.get("entry_id")].get("pdf_page") != item.get("pdf_page")
        or entry_by_id[item.get("entry_id")].get("column") != item.get("column")
    ]
    check(
        not strategic_locator_errors,
        "作文地基 overlay 定位",
        f"entry/page/column/normalized errors={strategic_locator_errors or 'none'}",
    )
    check(
        all("unreviewed" in item.get("verification_status", "") for item in entries if item.get("verification_status", "").startswith("ocr_consensus")),
        "OCR 共识不冒充人工核验",
        "所有 ocr_consensus 均为 unreviewed",
    )
    check(
        len(pending) == validation.get("pending_review_count"),
        "大纲词汇待复核队列",
        f"{len(pending)} 条，可追溯",
    )
    manual_stats = validation.get("manual_review", {})
    check(
        manual_stats.get("sample_size") == 30
        and manual_stats.get("post_correction_matches") == 30
        and manual_stats.get("post_correction_accuracy_within_sample") == 1.0,
        "大纲词汇边界重抽样",
        "30/30 after correction; scope not extrapolated",
    )


def verify_formal_boundaries_and_links() -> None:
    with (ROOT / "bank/master_bank.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    widths = Counter(len(row) for row in rows)
    check(widths == {13: len(rows)}, "master_bank 结构未破坏", f"rows={len(rows)}, widths={dict(widths)}")

    required_docs = [
        "wiki/reference/英语PDF学习资料总索引.md",
        "wiki/reading/历年真题阅读总索引.md",
        "wiki/vocabulary/大纲词汇参考库.md",
        "wiki/writing/作文资料总索引.md",
        "wiki/writing/作文造句可用白名单.md",
        "schema/reference_grounded_examples.md",
        "schema/protected_exam_analysis.md",
        "raw/protected/exam-reading-analysis/index.md",
        "wiki/relationships/作文-大纲词-错词关系图谱.md",
        "wiki/validation/2026-07-11-英语PDF学习资料验收.md",
        "wiki/validation/2026-07-11-历年阅读答案解析预处理验收.md",
        "wiki/validation/2026-07-11-双源造句冒烟测试.md",
        "wiki/validation/2026-07-11-不背单词四层地基验收.md",
    ]
    existing_count = sum((ROOT / path).is_file() for path in required_docs)
    check(
        existing_count == len(required_docs),
        "总入口与验收页面",
        f"{existing_count}/{len(required_docs)} 存在",
    )

    broken: list[str] = []
    link_pattern = re.compile(r"\[[^]]*\]\(([^)]+)\)")
    for relative in required_docs:
        page = ROOT / relative
        if not page.is_file():
            continue
        for target in link_pattern.findall(page.read_text(encoding="utf-8")):
            target = unquote(target.split("#", 1)[0]).strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            resolved = (page.parent / target).resolve()
            if not resolved.exists():
                broken.append(f"{relative} -> {target}")
    check(not broken, "核心 Markdown 相对链接", f"broken={broken or 'none'}")


def run_component_verifiers() -> None:
    scripts = [
        "scripts/build_exam_reading_corpus.py",
        "scripts/build_exam_reading_analysis.py",
        "scripts/build_syllabus_vocabulary_reference.py",
        "scripts/build_writing_reference_corpus.py",
        "scripts/build_writing_vocabulary_relationship_graph.py",
        "scripts/select_bbdc_foundation.py",
    ]
    for script in scripts:
        result = subprocess.run(
            [sys.executable, str(ROOT / script), "--verify-only"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        tail = (result.stdout + result.stderr).strip().splitlines()[-1:] or ["no output"]
        check(result.returncode == 0, f"组件验证 {Path(script).name}", tail[0])


def main() -> int:
    verify_external_sources()
    verify_reading_corpus()
    verify_writing_reference()
    verify_syllabus_reference()
    verify_formal_boundaries_and_links()
    run_component_verifiers()

    for line in PASSES:
        print(line)
    for line in FAILURES:
        print(line, file=sys.stderr)
    print(f"SUMMARY｜pass={len(PASSES)}｜fail={len(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
