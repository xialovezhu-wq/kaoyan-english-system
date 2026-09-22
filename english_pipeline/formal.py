from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from .constants import FORMAL_FILES, MASTERED_HEADER, MASTER_HEADER, SP_FIELDS
from .errors import ValidationError
from .util import file_sha256


def resolve_formal_paths(repo_root: Path) -> dict[str, Path]:
    return {name: (repo_root / relative).resolve() for name, relative in FORMAL_FILES.items()}


def formal_hashes(repo_root: Path) -> dict[str, str]:
    paths = resolve_formal_paths(repo_root)
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise ValidationError(f"formal files missing: {missing}")
    return {name: file_sha256(path) for name, path in paths.items()}


def read_csv(path: Path, expected_header: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_header:
            raise ValidationError(
                f"CSV header mismatch for {path}: expected {expected_header}, got {reader.fieldnames}"
            )
        rows = list(reader)
    for index, row in enumerate(rows, start=2):
        if None in row:
            raise ValidationError(f"CSV width mismatch for {path}:{index}")
    return rows


def serialize_csv(rows: list[dict[str, Any]], header: list[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in header})
    return stream.getvalue().encode("utf-8")


def formal_snapshot(repo_root: Path) -> dict[str, Any]:
    paths = resolve_formal_paths(repo_root)
    master_rows = read_csv(paths["master_bank"], MASTER_HEADER)
    mastered_rows = read_csv(paths["mastered_items"], MASTERED_HEADER)
    sentence_patterns = paths["sentence_patterns"].read_text(encoding="utf-8")
    sp_validation = validate_sentence_patterns_text(sentence_patterns)
    return {
        "master_bank": {
            "path": str(paths["master_bank"]),
            "sha256": file_sha256(paths["master_bank"]),
            "column_count": len(MASTER_HEADER),
            "header": MASTER_HEADER,
            "row_count": len(master_rows),
        },
        "mastered_items": {
            "path": str(paths["mastered_items"]),
            "sha256": file_sha256(paths["mastered_items"]),
            "column_count": len(MASTERED_HEADER),
            "header": MASTERED_HEADER,
            "row_count": len(mastered_rows),
        },
        "sentence_patterns": {
            "path": str(paths["sentence_patterns"]),
            "sha256": file_sha256(paths["sentence_patterns"]),
            "field_count": len(SP_FIELDS),
            "fields": SP_FIELDS,
            "card_count": sp_validation["card_count"],
        },
    }


def validate_sentence_patterns_text(text: str) -> dict[str, Any]:
    headings = list(re.finditer(r"^## (SP-\d{3})｜(.+)$", text, flags=re.MULTILINE))
    ids = [match.group(1) for match in headings]
    if len(ids) != len(set(ids)):
        raise ValidationError("sentence_patterns contains duplicate SP ids")
    expected = SP_FIELDS[1:]
    for index, match in enumerate(headings):
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[start:end]
        observed = re.findall(r"^\*\*(.+?)\*\*：", block, flags=re.MULTILINE)
        if observed != expected:
            raise ValidationError(
                f"sentence_patterns {match.group(1)} field order mismatch: expected {expected}, got {observed}"
            )
    return {"card_count": len(headings), "ids": ids, "field_count": len(SP_FIELDS)}
