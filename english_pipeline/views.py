from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from .constants import MASTERED_HEADER, MASTER_HEADER, STRONG_A_EVIDENCE
from .events import append_event, effective_sentence_events, load_events
from .formal import formal_hashes, read_csv, resolve_formal_paths
from .util import atomic_write_json, atomic_write_text, normalize_item, object_sha256, parse_iso_date, utc_now


QUICK_CAPTURE_PROJECTION_SCHEMA = "english_quick_capture_projection_v2"
QUICK_CAPTURE_BINDING_PREFIX = "<!-- study-intake-projection-binding-v1 "


def quick_capture_projection_binding(
    effective_events: list[dict[str, Any]],
    *,
    article_id: str | None,
    study_date: str | None,
) -> dict[str, Any]:
    ordered = sorted(effective_events, key=lambda event: str(event["event_id"]))
    rows = [f"{event['event_id']}:{object_sha256(event)}" for event in ordered]
    return {
        "schema_version": QUICK_CAPTURE_PROJECTION_SCHEMA,
        "data_role": "projection",
        "source_id": article_id or "all",
        "study_date": study_date,
        "effective_event_ids": [event["event_id"] for event in ordered],
        "effective_event_count": len(ordered),
        "effective_event_high_water_sha256": hashlib.sha256(
            "\n".join(rows).encode("utf-8")
        ).hexdigest(),
        "formal_write_count": 0,
    }


def _quick_capture_binding_line(binding: dict[str, Any]) -> str:
    return (
        QUICK_CAPTURE_BINDING_PREFIX
        + json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + " -->"
    )


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return cleaned or "article"


def render_quick_capture(
    events: list[dict[str, Any]],
    *,
    article_id: str | None = None,
    study_date: str | None = None,
) -> str:
    effective = effective_sentence_events(events, article_id=article_id, study_date=study_date)
    binding = quick_capture_projection_binding(
        effective,
        article_id=article_id,
        study_date=study_date,
    )
    title = article_id or "全部文章"
    lines = [
        _quick_capture_binding_line(binding),
        f"# 英语快速入库视图｜{title}",
        "",
        "> 本页由 append-only capture events 重建；它是可重建视图，不是正式学习库。",
        "",
        f"- 日期筛选：{study_date or '全部'}",
        f"- 有效句子事件：{len(effective)}",
        "- formal_write_count：0",
        "- formal_writeback：none",
        "",
    ]
    for event in effective:
        source = event["source"]
        learning = event["learning"]
        protected_explanation = source.get("source_kind") == "explanation"
        lines.extend(
            [
                f"## {source['sentence_id']}｜{event['event_id']}",
                "",
                f"- 来源文章：{event['article']['source_article']}",
                f"- 原句：{'[受保护解析正文不在可重建视图回显]' if protected_explanation else source['source_sentence']}",
                f"- 文章 source hash：`{event['article']['source_hash']}`",
                f"- 句子 sentence hash：`{source['sentence_hash']}`",
                f"- 第一遍翻译：{'[不回显]' if protected_explanation else learning.get('first_translation', '')}",
                f"- 用户证据：{'|'.join(learning.get('user_evidence', []))}",
                f"- 证据来源：{learning.get('evidence_origin', '')}",
                f"- 提示层级：L{learning.get('hint_level', 0)}",
                f"- 答案保护：{learning.get('answer_protection', '')}",
            ]
        )
        if learning.get("translation") and not protected_explanation:
            lines.append(f"- 本轮译文：{learning['translation']}")
        if learning.get("explanation") and not protected_explanation:
            lines.append(f"- 本轮讲解：{learning['explanation']}")
        if event.get("supersedes_event_id"):
            lines.append(f"- 更正替代：{event['supersedes_event_id']}")
        lines.extend(["", "### 本句候选", ""])
        candidates = event.get("candidates", [])
        if not candidates:
            lines.append("- 无")
        for candidate in ([] if protected_explanation else candidates):
            lines.append(
                f"- {candidate['item']}｜{candidate['candidate_type']}｜"
                f"{candidate.get('meaning', '')}｜{candidate['decision']}｜"
                f"{candidate.get('tier_hint', '待分层')}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_quick_capture_view(
    state_dir: Path,
    *,
    article_id: str | None = None,
    study_date: str | None = None,
    output: Path | None = None,
) -> tuple[Path, str]:
    events = load_events(state_dir)
    text = render_quick_capture(events, article_id=article_id, study_date=study_date)
    day = study_date or date.today().isoformat()
    path = output or state_dir / "views" / day / f"{safe_name(article_id or 'all')}-quick-capture.md"
    atomic_write_text(path, text)
    return path, text


def _classify_candidate(candidate: dict[str, Any], evidence: set[str]) -> tuple[str, str]:
    explicit = candidate.get("tier_hint")
    if explicit in {"A", "B", "C"}:
        return str(explicit), "capture tier_hint"
    if evidence & STRONG_A_EVIDENCE:
        return "A", "explicit unknown, mistranslated, missed or familiar-new-meaning evidence"
    if candidate.get("candidate_type") in {"词组", "熟词僻义", "句型", "写作表达"}:
        return "B", "stable phrase, sense, structure or writing-transfer value"
    return "C", "recognition value without a current strong A/B signal"


def build_article_export(
    events: list[dict[str, Any]],
    *,
    completion_event: dict[str, Any],
    repo_root: Path,
    hashes_before: dict[str, str],
) -> dict[str, Any]:
    article_id = completion_event["article"]["article_id"]
    effective_ids = set(completion_event["completion"]["effective_capture_event_ids"])
    effective = [
        event
        for event in effective_sentence_events(events, article_id=article_id)
        if event["event_id"] in effective_ids and event.get("source", {}).get("source_kind") != "explanation"
    ]
    paths = resolve_formal_paths(repo_root)
    master_rows = read_csv(paths["master_bank"], MASTER_HEADER)
    mastered_rows = read_csv(paths["mastered_items"], MASTERED_HEADER)
    bank_by_norm: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in master_rows:
        bank_by_norm[normalize_item(row["item"])].append(row)
    mastered_norms = {normalize_item(row["item"]) for row in mastered_rows if normalize_item(row["item"])}
    mastered_ids = {row["matched_id"].strip() for row in mastered_rows if row["matched_id"].strip()}

    tiers: dict[str, list[dict[str, Any]]] = {"A": [], "B": [], "C": []}
    exclusions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in effective:
        evidence = set(event["learning"].get("user_evidence", []))
        for candidate in event.get("candidates", []):
            item = candidate["item"].strip()
            normalized = normalize_item(item)
            if not normalized:
                continue
            if candidate["decision"] in {"article_only", "not_recommended"}:
                exclusions.append(
                    {
                        "item": item,
                        "status": candidate["decision"],
                        "reason": candidate.get("reason", candidate["decision"]),
                        "source_event_id": event["event_id"],
                    }
                )
                continue
            if normalized in seen:
                exclusions.append(
                    {"item": item, "status": "duplicate_candidate", "reason": "normalized duplicate in completion snapshot", "source_event_id": event["event_id"]}
                )
                continue
            seen.add(normalized)
            bank_matches = bank_by_norm.get(normalized, [])
            if normalized in mastered_norms or any(row["id"] in mastered_ids for row in bank_matches):
                exclusions.append(
                    {"item": item, "status": "mastered_excluded", "reason": "matched mastered_items before bank classification", "source_event_id": event["event_id"]}
                )
                continue
            tier, reason = _classify_candidate(candidate, evidence)
            bank_status = "new_candidate"
            if len(bank_matches) == 1:
                bank_status = "existing_bank"
            elif len(bank_matches) > 1:
                bank_status = "duplicate_like"
            tiers[tier].append(
                {
                    "item": item,
                    "candidate_type": candidate["candidate_type"],
                    "meaning": candidate.get("meaning", ""),
                    "usage": candidate.get("usage", ""),
                    "review_note": candidate.get("review_note", ""),
                    "source_sentence": event["source"]["source_sentence"],
                    "source_translation": event["learning"].get("translation", ""),
                    "article_source_hash": event["article"]["source_hash"],
                    "sentence_hash": event["source"]["sentence_hash"],
                    "source_event_id": event["event_id"],
                    "bank_status": bank_status,
                    "bank_match_ids": [row["id"] for row in bank_matches],
                    "user_evidence": sorted(evidence),
                    "classification_reason": reason,
                }
            )
    hashes_after = formal_hashes(repo_root)
    return {
        "schema_version": "english_article_export_v1",
        "export_id": f"EXPORT-{completion_event['event_id'][4:]}",
        "completion_event_id": completion_event["event_id"],
        "article_id": article_id,
        "source_id": completion_event["article"]["source_id"],
        "study_date": completion_event["completion"]["study_date"],
        "generated_at": utc_now(),
        "formal_write_count": 0,
        "formal_writeback": "none",
        "tiers": tiers,
        "exclusions": exclusions,
        "formal_hashes_before": hashes_before,
        "formal_hashes_after": hashes_after,
    }


def render_article_export(export: dict[str, Any]) -> str:
    lines = [
        f"# 不背单词候选｜{export['article_id']}",
        "",
        f"- 日期：{export['study_date']}",
        f"- A/B/C：{len(export['tiers']['A'])}/{len(export['tiers']['B'])}/{len(export['tiers']['C'])}",
        "- output-only：是",
        "- formal_write_count：0",
        "",
    ]
    labels = {"A": "A 类", "B": "B 类", "C": "C 类"}
    for tier in ("A", "B", "C"):
        lines.extend([f"## {labels[tier]}", ""])
        if not export["tiers"][tier]:
            lines.extend(["无", ""])
            continue
        for card in export["tiers"][tier]:
            lines.extend(
                [
                    f"### {card['item']}：{card['meaning']}",
                    "",
                    f"原句：{card['source_sentence']}",
                    f"原句中文：{card.get('source_translation', '')}",
                    f"用法：{card.get('usage', '')}",
                    f"一句话笔记：{card.get('review_note', '')}",
                    f"长期库状态：{card['bank_status']}",
                    f"分层依据：{card['classification_reason']}",
                    "",
                ]
            )
    if export["exclusions"]:
        lines.extend(["## 已掌握、重复或仅文章保留", ""])
        for row in export["exclusions"]:
            lines.append(f"- {row['item']}：{row['status']}；{row['reason']}")
        lines.append("")
    lines.extend(["## 正式数据保护", "", "master_bank、mastered_items、sentence_patterns 未修改。", ""])
    return "\n".join(lines)


def complete_article(
    state_dir: Path,
    repo_root: Path,
    *,
    article_id: str,
    idempotency_key: str,
    study_date: str | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    events_before = load_events(state_dir)
    effective = effective_sentence_events(events_before, article_id=article_id)
    if effective:
        article = effective[-1]["article"]
    else:
        raise ValueError(f"cannot complete article without at least one capture event: {article_id}")
    study_date = study_date or date.today().isoformat()
    hashes_before = formal_hashes(repo_root)
    event_hashes = {event["event_id"]: object_sha256(event) for event in effective}
    request = {
        "event_type": "article_completed",
        "idempotency_key": idempotency_key,
        "article": article,
        "completion": {
            "study_date": study_date,
            "effective_capture_event_ids": [event["event_id"] for event in effective],
            "capture_event_sha256": event_hashes,
        },
    }
    capture_receipt = append_event(state_dir, request)
    events_after = load_events(state_dir)
    completion_event = next(event for event in events_after if event["event_id"] == capture_receipt["capture_id"])
    export = build_article_export(events_after, completion_event=completion_event, repo_root=repo_root, hashes_before=hashes_before)
    package_id = export["export_id"]
    directory = output_dir or state_dir / "views" / study_date
    json_path = directory / f"{package_id}.abc.json"
    markdown_path = directory / f"{package_id}.abc.md"
    atomic_write_json(json_path, export)
    atomic_write_text(markdown_path, render_article_export(export))
    return {
        "schema_version": "english_article_completion_receipt_v1",
        "receipt_id": f"COMPLETE-{completion_event['event_id'][4:]}",
        "completion_event_id": completion_event["event_id"],
        "capture_status": capture_receipt["status"],
        "article_id": article_id,
        "source_id": completion_event["article"]["source_id"],
        "study_date": study_date,
        "effective_capture_event_ids": completion_event["completion"]["effective_capture_event_ids"],
        "export_json": str(json_path),
        "export_markdown": str(markdown_path),
        "tier_counts": {tier: len(export["tiers"][tier]) for tier in ("A", "B", "C")},
        "formal_write_count": 0,
        "formal_writeback": "none",
        "formal_sources_unchanged": export["formal_hashes_before"] == export["formal_hashes_after"],
    }
