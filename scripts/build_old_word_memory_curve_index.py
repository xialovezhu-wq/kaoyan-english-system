#!/usr/bin/env python3
"""Build derived memory-curve indexes for old-word linkage.

This script reads formal English bank files and writes rebuildable wiki indexes.
It never modifies bank/master_bank.csv or bank/mastered_items.csv.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from english_pipeline.review_status import (
    effective_mastered_norms,
    effective_mastered_ids,
    effective_review_status,
    load_review_status_ledger,
)


MASTER_HEADER = [
    "id",
    "date",
    "type",
    "item",
    "source_article",
    "source_sentence",
    "meaning",
    "usage",
    "writing_value",
    "tags",
    "review_note",
    "appear_count",
    "last_seen",
]

ALLOWED_TYPES = {"单词", "词组", "熟词僻义", "句型", "长难句", "写作表达"}
OLD_WORD_TYPES = {"单词", "词组", "熟词僻义", "句型", "写作表达"}


def norm_item(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def parse_ymd(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def bucket_for(days: int | None) -> tuple[str, bool, str]:
    if days is None:
        return "invalid-date", False, "needs-date-fix"
    if days < 0:
        return "future-date", False, "needs-date-fix"
    if days == 0:
        return "recent-today", False, "recent-repeat"
    if days == 1:
        return "D1", True, "due"
    if 2 <= days <= 4:
        return "D3", True, "due"
    if days == 5:
        return "between-D3-D7", False, "between-window"
    if 6 <= days <= 8:
        return "D7", True, "due"
    if 9 <= days <= 12:
        return "between-D7-D15", False, "between-window"
    if 13 <= days <= 17:
        return "D15", True, "due"
    if 18 <= days <= 25:
        return "between-D15-D30", False, "between-window"
    if 26 <= days <= 34:
        return "D30", True, "due"
    if 35 <= days <= 51:
        return "between-D30-D60", False, "between-window"
    if 52 <= days <= 68:
        return "D60", True, "due"
    if 69 <= days <= 79:
        return "between-D60-D90", False, "between-window"
    return "D90+", True, "due"


def read_csv_dict(path: Path) -> tuple[list[str], list[dict[str, str]], list[tuple[int, int]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return [], [], []
    header = rows[0]
    bad_width = []
    data = []
    for line_no, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            bad_width.append((line_no, len(row)))
        fixed = row[: len(header)] + [""] * max(0, len(header) - len(row))
        data.append(dict(zip(header, fixed)))
    return header, data, bad_width


def merge_text(values: list[str], limit: int = 3) -> str:
    seen = []
    for value in values:
        value = (value or "").strip()
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return " / ".join(seen)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def build(root: Path, today: date) -> dict[str, object]:
    master_path = root / "bank" / "master_bank.csv"
    mastered_path = root / "bank" / "mastered_items.csv"
    review_status_path = root / "bank" / "review_exclusion_ledger.jsonl"
    out_dir = root / "wiki" / "old_words"

    header, master_rows, bad_width = read_csv_dict(master_path)
    if header != MASTER_HEADER:
        raise SystemExit(f"master_bank.csv header mismatch: {header!r}")

    mastered_header, mastered_rows, mastered_bad = read_csv_dict(mastered_path)
    mastered_norms = {norm_item(row.get("item", "")) for row in mastered_rows if norm_item(row.get("item", ""))}
    review_records = load_review_status_ledger(review_status_path)
    review_status = effective_review_status(review_records)
    mastered_norms = effective_mastered_norms(mastered_rows, review_records)
    mastered_ids = effective_mastered_ids(mastered_rows, review_records)
    review_excluded_norms = {
        item_norm
        for item_norm, row in review_status.items()
        if row.get("status") == "mastered_sentence_nonreport"
    }

    row_index = []
    by_norm: dict[str, list[dict[str, object]]] = defaultdict(list)

    for row in master_rows:
        item_norm = norm_item(row.get("item", ""))
        parsed_last = parse_ymd(row.get("last_seen", ""))
        parsed_date = parse_ymd(row.get("date", ""))
        basis = parsed_last or parsed_date
        basis_field = "last_seen" if parsed_last else "date" if parsed_date else "none"
        days = (today - basis).days if basis else None
        bucket, is_due, policy = bucket_for(days)
        independently_mastered = item_norm in mastered_norms or row.get("id", "").strip() in mastered_ids
        review_excluded = item_norm in review_excluded_norms
        mastered = independently_mastered or review_excluded
        type_valid = row.get("type", "") in ALLOWED_TYPES
        old_word_candidate = row.get("type", "") in OLD_WORD_TYPES
        active_status = "mastered_excluded" if mastered else "active"
        lint_flags = []
        if not type_valid:
            lint_flags.append("invalid_type")
        if basis is None:
            lint_flags.append("invalid_or_missing_date")
        if not row.get("source_article", "").strip():
            lint_flags.append("missing_source_article")
        if not row.get("meaning", "").strip():
            lint_flags.append("missing_meaning")
        indexed = {
            "generated_date": today.isoformat(),
            "id": row.get("id", ""),
            "item": row.get("item", ""),
            "item_norm": item_norm,
            "type": row.get("type", ""),
            "meaning": row.get("meaning", ""),
            "usage": row.get("usage", ""),
            "writing_value": row.get("writing_value", ""),
            "tags": row.get("tags", ""),
            "review_note": row.get("review_note", ""),
            "source_article": row.get("source_article", ""),
            "date": row.get("date", ""),
            "last_seen": row.get("last_seen", ""),
            "basis_date": basis.isoformat() if basis else "",
            "basis_field": basis_field,
            "days_since_seen": "" if days is None else days,
            "due_bucket": bucket,
            "is_due_today": "yes" if is_due else "no",
            "recency_policy": policy,
            "active_status": active_status,
            "exclusion_source": (
                "independent_correct_use"
                if independently_mastered
                else "sentence_nonreport_review_exclusion"
                if review_excluded
                else ""
            ),
            "old_word_candidate": "yes" if old_word_candidate else "no",
            "type_valid": "yes" if type_valid else "no",
            "lint_flags": "|".join(lint_flags),
        }
        row_index.append(indexed)
        by_norm[item_norm].append(indexed)

    active_items = []
    for item_norm, rows in by_norm.items():
        rows_sorted = sorted(
            rows,
            key=lambda r: (
                r["active_status"] != "active",
                -(int(r["days_since_seen"]) if str(r["days_since_seen"]).isdigit() else -1),
            ),
        )
        active_rows = [row for row in rows if row["active_status"] == "active"]
        if not active_rows:
            rows_for_item = rows
            active_status = "mastered_excluded"
        else:
            rows_for_item = active_rows
            active_status = "active"

        latest_basis = max(
            (parse_ymd(str(row.get("basis_date", ""))) for row in rows_for_item if row.get("basis_date")),
            default=None,
        )
        days_values = [
            int(row["days_since_seen"])
            for row in rows_for_item
            if str(row.get("days_since_seen", "")).isdigit()
        ]
        days = min(days_values) if days_values else None
        bucket, is_due, policy = bucket_for(days)
        representative = sorted(
            rows_for_item,
            key=lambda row: (
                row.get("type") not in OLD_WORD_TYPES,
                0 if row.get("is_due_today") == "yes" else 1,
                -(int(row["days_since_seen"]) if str(row["days_since_seen"]).isdigit() else -1),
            ),
        )[0]
        all_ids = [str(row.get("id", "")) for row in rows_for_item if row.get("id")]
        all_tags = sorted({tag for row in rows_for_item for tag in str(row.get("tags", "")).split("|") if tag})
        lint_flags = sorted({flag for row in rows_for_item for flag in str(row.get("lint_flags", "")).split("|") if flag})
        active_items.append(
            {
                "generated_date": today.isoformat(),
                "item": representative.get("item", ""),
                "item_norm": item_norm,
                "representative_id": representative.get("id", ""),
                "all_ids": "|".join(all_ids),
                "row_count": len(rows_for_item),
                "type": merge_text([str(row.get("type", "")) for row in rows_for_item]),
                "meaning": merge_text([str(row.get("meaning", "")) for row in rows_for_item]),
                "usage": merge_text([str(row.get("usage", "")) for row in rows_for_item], limit=2),
                "writing_value": merge_text([str(row.get("writing_value", "")) for row in rows_for_item]),
                "tags": "|".join(all_tags),
                "source_article": merge_text([str(row.get("source_article", "")) for row in rows_for_item], limit=2),
                "latest_basis_date": latest_basis.isoformat() if latest_basis else "",
                "days_since_seen": "" if days is None else days,
                "due_bucket": bucket,
                "is_due_today": "yes" if is_due else "no",
                "recency_policy": policy,
                "active_status": active_status,
                "exclusion_source": merge_text(
                    [str(row.get("exclusion_source", "")) for row in rows_for_item]
                ),
                "old_word_candidate": "yes" if any(row.get("old_word_candidate") == "yes" for row in rows_for_item) else "no",
                "lint_flags": "|".join(lint_flags),
            }
        )

    active_only = [row for row in active_items if row["active_status"] == "active"]
    active_due = [row for row in active_only if row["is_due_today"] == "yes"]
    oldest_sorted = sorted(
        active_only,
        key=lambda row: int(row["days_since_seen"]) if str(row.get("days_since_seen", "")).isdigit() else -1,
        reverse=True,
    )
    for rank, row in enumerate(oldest_sorted, start=1):
        row["oldest_fallback_rank"] = rank
    for row in active_items:
        row.setdefault("oldest_fallback_rank", "")

    row_index = sorted(
        row_index,
        key=lambda row: (
            row["active_status"] != "active",
            row["is_due_today"] != "yes",
            -(int(row["days_since_seen"]) if str(row["days_since_seen"]).isdigit() else -1),
            row["item_norm"],
        ),
    )
    active_items = sorted(
        active_items,
        key=lambda row: (
            row["active_status"] != "active",
            row["is_due_today"] != "yes",
            -(int(row["days_since_seen"]) if str(row["days_since_seen"]).isdigit() else -1),
            row["item_norm"],
        ),
    )

    row_fields = [
        "generated_date",
        "id",
        "item",
        "item_norm",
        "type",
        "meaning",
        "usage",
        "writing_value",
        "tags",
        "review_note",
        "source_article",
        "date",
        "last_seen",
        "basis_date",
        "basis_field",
        "days_since_seen",
        "due_bucket",
        "is_due_today",
        "recency_policy",
        "active_status",
        "exclusion_source",
        "old_word_candidate",
        "type_valid",
        "lint_flags",
    ]
    item_fields = [
        "generated_date",
        "item",
        "item_norm",
        "representative_id",
        "all_ids",
        "row_count",
        "type",
        "meaning",
        "usage",
        "writing_value",
        "tags",
        "source_article",
        "latest_basis_date",
        "days_since_seen",
        "due_bucket",
        "is_due_today",
        "recency_policy",
        "active_status",
        "exclusion_source",
        "old_word_candidate",
        "oldest_fallback_rank",
        "lint_flags",
    ]

    row_path = out_dir / "memory_curve_rows.csv"
    item_path = out_dir / "memory_curve_active_items.csv"
    summary_path = out_dir / "memory_curve_summary.md"
    write_csv(row_path, row_fields, row_index)
    write_csv(item_path, item_fields, active_items)

    bucket_counts = Counter(row["due_bucket"] for row in active_only)
    item_bucket_counts = Counter(row["due_bucket"] for row in active_items if row["active_status"] == "active")
    type_counts = Counter(row.get("type", "") for row in master_rows)
    lint_counts = Counter(
        flag
        for row in row_index
        for flag in str(row.get("lint_flags", "")).split("|")
        if flag
    )
    due_items = [row for row in active_items if row["active_status"] == "active" and row["is_due_today"] == "yes"]
    fallback_items = [row for row in oldest_sorted if row.get("is_due_today") == "no"][:20]

    summary = [
        "# 旧词记忆曲线预处理索引",
        "",
        f"generated_date: {today.isoformat()}",
        "source: `bank/master_bank.csv`; `bank/mastered_items.csv`",
        "status: derived-index; rebuildable; not formal bank data",
        "",
        "## 规则",
        "",
        "- 先排除 `bank/mastered_items.csv` 已掌握项。",
        "- 使用 `last_seen` 计算距离今天的天数；缺失时回退 `date`。",
        "- 命中窗口：D1, D3, D7, D15, D30, D60, D90+。",
        "- 最近今天/昨天刚见过的词不优先用于旧词联动。",
        "- 无自然命中时，使用 `oldest-fallback`：选择最久未复现且能自然造句的活跃旧词。",
        "- 本索引不更新 `appear_count`、`last_seen`、`master_bank.csv` 或复习文件。",
        "",
        "## 输出文件",
        "",
        f"- `{item_path.relative_to(root)}`：去重后的活跃旧词索引，供造句优先使用。",
        f"- `{row_path.relative_to(root)}`：master_bank 逐行预处理索引，供审计使用。",
        "",
        "## 总览",
        "",
        f"- master_bank 数据行：{len(master_rows)}",
        f"- mastered_items 数据行：{len(mastered_rows)}",
        f"- 活跃逐行记录：{sum(1 for row in row_index if row['active_status'] == 'active')}",
        f"- 去重后活跃 item：{len(active_only)}",
        f"- 今天命中记忆曲线窗口的活跃 item：{len(due_items)}",
        f"- CSV 字段错位行：{len(bad_width)}",
        f"- mastered_items 字段错位行：{len(mastered_bad)}",
        "",
        "## 类型分布",
        "",
        "| type | count |",
        "|---|---:|",
    ]
    for key, count in sorted(type_counts.items()):
        summary.append(f"| {key or '未记录'} | {count} |")

    summary.extend(["", "## 活跃 item 记忆曲线分布", "", "| bucket | count |", "|---|---:|"])
    bucket_order = [
        "recent-today",
        "D1",
        "D3",
        "between-D3-D7",
        "D7",
        "between-D7-D15",
        "D15",
        "between-D15-D30",
        "D30",
        "between-D30-D60",
        "D60",
        "between-D60-D90",
        "D90+",
        "invalid-date",
        "future-date",
    ]
    for key in bucket_order:
        count = item_bucket_counts.get(key, 0)
        if count:
            summary.append(f"| {key} | {count} |")

    summary.extend(["", "## 当前命中窗口样例", "", "| item | bucket | days | type | meaning |", "|---|---|---:|---|---|"])
    for row in due_items[:30]:
        summary.append(
            f"| {row['item']} | {row['due_bucket']} | {row['days_since_seen']} | {row['type']} | {str(row['meaning']).replace('|', '/')} |"
        )
    if not due_items:
        summary.append("| 无 | - | - | - | - |")

    summary.extend(["", "## oldest-fallback 样例", "", "| rank | item | days | type | meaning |", "|---:|---|---:|---|---|"])
    for row in fallback_items[:20]:
        summary.append(
            f"| {row.get('oldest_fallback_rank', '')} | {row['item']} | {row['days_since_seen']} | {row['type']} | {str(row['meaning']).replace('|', '/')} |"
        )
    if not fallback_items:
        summary.append("| - | 无 | - | - | - |")

    summary.extend(["", "## lint 提示", "", "| flag | count |", "|---|---:|"])
    if lint_counts:
        for key, count in sorted(lint_counts.items()):
            summary.append(f"| {key} | {count} |")
    else:
        summary.append("| 无 | 0 |")

    summary_path.write_text("\n".join(summary) + "\n", encoding="utf-8")

    return {
        "master_rows": len(master_rows),
        "mastered_rows": len(mastered_rows),
        "active_rows": sum(1 for row in row_index if row["active_status"] == "active"),
        "active_items": len(active_only),
        "due_items": len(due_items),
        "row_path": row_path,
        "item_path": item_path,
        "summary_path": summary_path,
        "bad_width": len(bad_width),
        "mastered_bad_width": len(mastered_bad),
        "lint_counts": dict(lint_counts),
        "bucket_counts": dict(bucket_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", help="kaoyan-english root")
    parser.add_argument("--today", default=date.today().isoformat(), help="YYYY-MM-DD")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    today = parse_ymd(args.today)
    if today is None:
        raise SystemExit("--today must be YYYY-MM-DD")
    result = build(root, today)
    print(f"generated_date={args.today}")
    print(f"master_rows={result['master_rows']}")
    print(f"active_items={result['active_items']}")
    print(f"due_items={result['due_items']}")
    print(f"item_index={Path(result['item_path']).relative_to(root)}")
    print(f"row_index={Path(result['row_path']).relative_to(root)}")
    print(f"summary={Path(result['summary_path']).relative_to(root)}")


if __name__ == "__main__":
    main()
