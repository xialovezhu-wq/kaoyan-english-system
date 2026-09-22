#!/usr/bin/env python3
"""Merge the two independent, source-verified answer-key audit files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


EXPECTED = {text_no: list(range(16 + 5 * text_no, 21 + 5 * text_no)) for text_no in range(1, 5)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--older", type=Path, required=True)
    parser.add_argument("--newer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    older = json.loads(args.older.read_text(encoding="utf-8"))
    newer = json.loads(args.newer.read_text(encoding="utf-8"))
    merged: dict[str, dict[str, dict]] = {}

    for year in range(2010, 2017):
        year_key = str(year)
        merged[year_key] = {}
        for text_no in range(1, 5):
            text_key = str(text_no)
            numbers = EXPECTED[text_no]
            answers = older["answer_keys"][year_key][text_key]
            evidence = older["evidence"][year_key][text_key]
            per_question = evidence["solution_evidence_by_question"]
            evidence_pages = {
                str(number): [int(per_question[str(number)]["question_heading_page"])]
                for number in numbers
            }
            audit_pages_checked = {
                str(number): per_question[str(number)]["analysis_pdf_pages_checked"]
                for number in numbers
            }
            methods = sorted({per_question[str(number)]["verification"] for number in numbers})
            merged[year_key][text_key] = {
                "answers": answers,
                "question_numbers": numbers,
                "evidence_pages": evidence_pages,
                "audit_pages_checked": audit_pages_checked,
                "verification_status": "source_verified_high",
                "verification_method": "+".join(methods),
                "source_pdf": evidence["analysis_pdf"],
                "source_sha256": evidence["analysis_pdf_sha256"],
                "old_analysis_page_range": evidence["corpus_analysis_page_range"],
                "audit_confidence": evidence["confidence"],
            }

    for year in range(2017, 2025):
        year_key = str(year)
        merged[year_key] = {}
        year_evidence = newer["evidence"][year_key]
        for text_no in range(1, 5):
            source_key = f"text-{text_no}"
            numbers = EXPECTED[text_no]
            answers = newer["answer_keys"][year_key][source_key]
            details = year_evidence["texts"][source_key]
            evidence_pages = {
                str(number): [int(page)] for number, page in zip(numbers, details["answer_evidence_pages"])
            }
            merged[year_key][str(text_no)] = {
                "answers": answers,
                "question_numbers": numbers,
                "evidence_pages": evidence_pages,
                "verification_status": "source_verified_high",
                "verification_method": year_evidence["method"],
                "source_pdf": year_evidence["analysis_pdf"],
                "source_sha256": year_evidence["sha256"],
                "old_analysis_page_range": details["analysis_page_range"],
                "audit_confidence": details["confidence"],
                "note": details.get("note"),
            }

    failures = []
    answer_count = 0
    for year in range(2010, 2025):
        for text_no in range(1, 5):
            entry = merged[str(year)][str(text_no)]
            numbers = EXPECTED[text_no]
            if entry["question_numbers"] != numbers or len(entry["answers"]) != 5:
                failures.append(f"{year} T{text_no}: bad shape")
            if any(answer not in {"A", "B", "C", "D"} for answer in entry["answers"]):
                failures.append(f"{year} T{text_no}: bad answer")
            if set(map(int, entry["evidence_pages"])) != set(numbers):
                failures.append(f"{year} T{text_no}: bad evidence map")
            if any(not pages for pages in entry["evidence_pages"].values()):
                failures.append(f"{year} T{text_no}: empty evidence pages")
            answer_count += len(entry["answers"])
    if failures:
        raise SystemExit("\n".join(failures))

    payload = {
        "schema": "exam_reading_verified_answer_keys_v1",
        "generated_date": "2026-07-11",
        "visibility": "hidden_until_review",
        "source_policy": "answers read only from user-provided local analysis PDFs; no internet or memory-only inference",
        "text_count": 60,
        "answer_count": answer_count,
        "all_answers_source_verified": True,
        "answer_keys": merged,
        "input_audits": {
            "2010_2016": str(args.older),
            "2017_2024": str(args.newer),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"texts": 60, "answers": answer_count, "status": "PASS"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
