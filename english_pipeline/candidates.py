from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ValidationError
from .events import event_by_id, load_events
from .util import atomic_write_text, load_json, object_sha256, parse_iso_date, sentence_sha256


GROUNDING_STATUSES = {
    "passed",
    "needs_context",
    "needs_user_evidence",
    "needs_reference_graph",
    "reference_gap",
    "not_requested",
}


def _required(obj: dict[str, Any], names: set[str], context: str) -> None:
    missing = sorted(names - obj.keys())
    if missing:
        raise ValidationError(f"{context} missing fields: {missing}")


def _validate_luna_candidate_v2(
    candidate: dict[str, Any],
    *,
    state_dir: Path | None,
) -> dict[str, Any]:
    required = {
        "schema_version", "candidate_id", "study_date", "source_id", "article_id",
        "article_source_hash", "created_at", "producer", "runtime_identity",
        "capture_event_ids", "capture_event_sha256", "evidence_manifest",
        "sentence_records", "observed_signals", "signal_outcomes", "capture_coverage",
        "review_exclusion_proposals", "reactivation_proposals", "needs_user_decision",
        "formal_write_count", "formal_writeback", "validation", "items",
    }
    if set(candidate) != required:
        raise ValidationError(
            f"Luna candidate v2 fields do not match schema: {sorted(set(candidate) ^ required)}"
        )
    if candidate["article_id"] != candidate["source_id"]:
        raise ValidationError("Luna candidate article_id alias must equal canonical source_id")
    if candidate["formal_write_count"] != 0 or candidate["formal_writeback"] != "none":
        raise ValidationError("Luna candidate must declare zero formal writes")
    capture_ids = candidate["capture_event_ids"]
    capture_hashes = candidate["capture_event_sha256"]
    if not isinstance(capture_ids, list) or len(capture_ids) != len(set(capture_ids)):
        raise ValidationError("capture_event_ids must be a unique array")
    if not isinstance(capture_hashes, dict) or set(capture_hashes) != set(capture_ids):
        raise ValidationError("capture_event_sha256 keys must exactly match capture_event_ids")

    known_events: dict[str, dict[str, Any]] = {}
    if state_dir is not None:
        known_events = event_by_id(load_events(state_dir))
        for capture_id in capture_ids:
            event = known_events.get(capture_id)
            if event is None or object_sha256(event) != capture_hashes[capture_id]:
                raise ValidationError(f"candidate capture missing or drifted: {capture_id}")
            if parse_iso_date(str(event["occurred_at"])) != candidate["study_date"]:
                raise ValidationError(f"candidate capture crosses study_date boundary: {capture_id}")

    records = candidate["sentence_records"]
    if not isinstance(records, list):
        raise ValidationError("sentence_records must be an array")
    record_ids: set[str] = set()
    record_events: set[str] = set()
    for index, record in enumerate(records, start=1):
        required_record = {
            "sentence_record_id", "source_event_id", "sequence", "source_sentence",
            "sentence_hash", "user_first_translation", "corrected_meaning", "explanation",
            "user_evidence_verbatim",
        }
        if not isinstance(record, dict) or set(record) != required_record:
            raise ValidationError(f"sentence record {index} fields do not match schema")
        if sentence_sha256(str(record["source_sentence"])) != record["sentence_hash"]:
            raise ValidationError(f"sentence record {index} hash mismatch")
        if record["source_event_id"] not in capture_ids:
            raise ValidationError(f"sentence record {index} source is not frozen")
        if record["sentence_record_id"] in record_ids or record["source_event_id"] in record_events:
            raise ValidationError("sentence records must be unique per frozen capture")
        record_ids.add(record["sentence_record_id"])
        record_events.add(record["source_event_id"])
        if known_events:
            event = known_events[record["source_event_id"]]
            if event.get("source", {}).get("sentence_hash") != record["sentence_hash"]:
                raise ValidationError(f"sentence record {index} differs from frozen capture")
            learning = event.get("learning", {})
            exact_bindings = {
                "user_first_translation": learning.get("first_translation"),
                "user_evidence_verbatim": learning.get("user_evidence_verbatim"),
            }
            if any(record[key] != value for key, value in exact_bindings.items()):
                raise ValidationError(f"sentence record {index} user evidence differs from frozen capture")
    if record_events != set(capture_ids):
        raise ValidationError("every frozen capture must have exactly one sentence record")

    signals = candidate["observed_signals"]
    if not isinstance(signals, list):
        raise ValidationError("observed_signals must be an array")
    signal_ids = [str(signal.get("signal_id")) for signal in signals if isinstance(signal, dict)]
    if len(signal_ids) != len(signals) or len(signal_ids) != len(set(signal_ids)):
        raise ValidationError("observed_signals must have unique signal ids")
    outcomes = candidate["signal_outcomes"]
    if not isinstance(outcomes, list) or {row.get("signal_id") for row in outcomes if isinstance(row, dict)} != set(signal_ids):
        raise ValidationError("each observed signal must have exactly one outcome")
    if len(outcomes) != len(signal_ids):
        raise ValidationError("signal outcomes contain duplicates")
    allowed_terminal = {"candidate", "explicitly_excluded", "unmatched", "structure_resolved", "novel_structure_proposal"}
    if any(row.get("terminal_status") not in allowed_terminal for row in outcomes):
        raise ValidationError("signal outcome terminal status is invalid")
    coverage = candidate["capture_coverage"]
    if not isinstance(coverage, dict) or coverage.get("covered_signal_count") != len(signals):
        raise ValidationError("candidate capture coverage is incomplete")

    referenced_signals: list[str] = []
    seen_items: set[str] = set()
    for index, item in enumerate(candidate["items"], start=1):
        required_item = {
            "item_id", "sequence", "item", "candidate_type", "candidate_status", "tier",
            "source_event_id", "sentence_record_id", "source_signal_ids", "evidence_states",
            "evidence_origin", "user_evidence", "bank_status", "bank_match_ids",
            "mastered_status", "grounding", "card",
        }
        if not isinstance(item, dict) or set(item) - (required_item | {"mastery_proposal"}):
            raise ValidationError(f"candidate v2 item {index} has unsupported fields")
        _required(item, required_item, f"candidate v2 item {index}")
        if item["item_id"] in seen_items:
            raise ValidationError("candidate v2 item ids must be unique")
        seen_items.add(item["item_id"])
        if item["source_event_id"] not in capture_ids or item["sentence_record_id"] not in record_ids:
            raise ValidationError(f"candidate v2 item {index} source binding is invalid")
        refs = item["source_signal_ids"]
        if not isinstance(refs, list) or not refs or not set(refs).issubset(set(signal_ids)):
            raise ValidationError(f"candidate v2 item {index} signal binding is invalid")
        referenced_signals.extend(refs)
        card = item["card"]
        if not isinstance(card, dict) or "source_sentence" in card or "source_translation" in card:
            raise ValidationError("candidate v2 word cards must reference sentence_records, not repeat the sentence")
        _required(card, {"meaning", "usage", "review_note"}, f"candidate v2 item {index} card")
    candidate_outcomes = {
        row["signal_id"] for row in outcomes if row.get("terminal_status") == "candidate"
    }
    if candidate_outcomes != set(referenced_signals):
        raise ValidationError("candidate signal outcomes disagree with item source_signal_ids")

    for field in ("review_exclusion_proposals", "reactivation_proposals", "needs_user_decision"):
        if not isinstance(candidate[field], list):
            raise ValidationError(f"{field} must be an array")
    if not isinstance(candidate["evidence_manifest"], dict):
        raise ValidationError("candidate v2 evidence_manifest must be an object")
    validation = candidate["validation"]
    if not isinstance(validation, dict) or validation.get("status") not in {"PASS", "BLOCKED"}:
        raise ValidationError("candidate v2 validation is invalid")
    identity = candidate["runtime_identity"]
    if not isinstance(identity, dict) or identity.get("status") not in {
        "confirmed", "requested_unverified", "mismatch", "unavailable"
    }:
        raise ValidationError("candidate v2 runtime_identity is invalid")
    if validation["status"] == "PASS" and identity["status"] not in {"confirmed", "requested_unverified"}:
        raise ValidationError("PASS candidate v2 has invalid runtime identity")
    return {
        "schema_version": candidate["schema_version"],
        "candidate_id": candidate["candidate_id"],
        "status": "PASS",
        "item_count": len(candidate["items"]),
        "capture_event_count": len(capture_ids),
        "formal_write_count": 0,
        "formal_writeback": "none",
    }


def validate_luna_candidate(
    candidate: dict[str, Any],
    *,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    if candidate.get("schema_version") == "english_luna_candidate_v2":
        return _validate_luna_candidate_v2(candidate, state_dir=state_dir)
    top_allowed = {
        "schema_version", "candidate_id", "study_date", "source_id", "article_id", "article_source_hash",
        "created_at", "producer", "runtime_identity", "capture_event_ids", "capture_event_sha256",
        "formal_write_count", "formal_writeback", "validation", "items",
    }
    if set(candidate) - top_allowed:
        raise ValidationError(f"Luna candidate has unsupported fields: {sorted(set(candidate) - top_allowed)}")
    _required(
        candidate,
        {
            "schema_version",
            "candidate_id",
            "study_date",
            "article_id",
            "source_id",
            "article_source_hash",
            "created_at",
            "producer",
            "runtime_identity",
            "capture_event_ids",
            "capture_event_sha256",
            "formal_write_count",
            "formal_writeback",
            "validation",
            "items",
        },
        "Luna candidate",
    )
    if candidate["schema_version"] != "english_luna_candidate_v1":
        raise ValidationError("Luna candidate schema_version mismatch")
    if candidate["formal_write_count"] != 0 or candidate["formal_writeback"] != "none":
        raise ValidationError("Luna candidate must declare zero formal writes")
    if not isinstance(candidate["article_source_hash"], str) or len(candidate["article_source_hash"]) != 64:
        raise ValidationError("Luna candidate article_source_hash is invalid")
    producer = candidate["producer"]
    if not isinstance(producer, dict) or set(producer) != {"role", "model", "prompt_version"} or producer.get("role") != "luna_candidate_consumer":
        raise ValidationError("Luna candidate producer role must be luna_candidate_consumer")
    identity = candidate["runtime_identity"]
    if not isinstance(identity, dict):
        raise ValidationError("runtime_identity must be an object")
    _required(identity, {"requested", "observed", "provenance", "status"}, "runtime_identity")
    if identity["status"] not in {"confirmed", "requested_unverified", "mismatch", "unavailable"}:
        raise ValidationError("runtime_identity status is invalid")
    for side in ("requested", "observed"):
        if not isinstance(identity[side], dict):
            raise ValidationError(f"runtime_identity.{side} must be an object")
        _required(identity[side], {"model", "reasoning_effort"}, f"runtime_identity.{side}")
    provenance = identity["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"source", "reference", "observed_at"}:
        raise ValidationError("runtime_identity.provenance fields do not match schema")
    if identity["status"] == "confirmed":
        if identity["requested"] != identity["observed"]:
            raise ValidationError("confirmed runtime identity requires equal requested and observed identity")
        if provenance["source"] not in {"runtime_attestation", "process_metadata"} or not provenance["reference"]:
            raise ValidationError("confirmed runtime identity requires independent provenance")
    elif identity["status"] == "requested_unverified":
        if provenance["source"] != "request_only":
            raise ValidationError("requested_unverified runtime identity requires request_only provenance")
    elif identity["status"] == "mismatch":
        if identity["requested"] == identity["observed"]:
            raise ValidationError("mismatch runtime identity requires different requested and observed identity")
    elif identity["status"] == "unavailable" and provenance["source"] != "none":
        raise ValidationError("unavailable runtime identity requires none provenance")
    validation = candidate["validation"]
    if not isinstance(validation, dict):
        raise ValidationError("validation must be an object")
    _required(validation, {"status", "blockers"}, "candidate validation")
    if validation["status"] not in {"PASS", "BLOCKED"} or not isinstance(validation["blockers"], list):
        raise ValidationError("candidate validation status/blockers are invalid")
    if validation["status"] == "PASS" and validation["blockers"]:
        raise ValidationError("PASS candidate cannot carry blockers")
    if validation["status"] == "PASS" and identity["status"] not in {"confirmed", "requested_unverified"}:
        raise ValidationError("PASS candidate requires confirmed or requested_unverified runtime identity")
    if identity["status"] in {"mismatch", "unavailable"}:
        if validation["status"] != "BLOCKED" or not validation["blockers"]:
            raise ValidationError("mismatch or unavailable runtime identity must be BLOCKED with a blocker")

    capture_ids = candidate["capture_event_ids"]
    capture_hashes = candidate["capture_event_sha256"]
    if not isinstance(capture_ids, list) or len(capture_ids) != len(set(capture_ids)):
        raise ValidationError("capture_event_ids must be a unique array")
    if not isinstance(capture_hashes, dict) or set(capture_hashes) != set(capture_ids):
        raise ValidationError("capture_event_sha256 keys must exactly match capture_event_ids")
    if not isinstance(candidate["items"], list):
        raise ValidationError("candidate items must be an array")

    known_events: dict[str, dict[str, Any]] = {}
    if state_dir is not None:
        known_events = event_by_id(load_events(state_dir))
        for capture_id in capture_ids:
            event = known_events.get(capture_id)
            if event is None:
                raise ValidationError(f"candidate references missing capture event: {capture_id}")
            if object_sha256(event) != capture_hashes[capture_id]:
                raise ValidationError(f"candidate capture hash drift: {capture_id}")
            if parse_iso_date(str(event["occurred_at"])) != candidate["study_date"]:
                raise ValidationError(f"candidate capture crosses study_date boundary: {capture_id}")

    seen_item_ids: set[str] = set()
    for index, item in enumerate(candidate["items"], start=1):
        context = f"candidate item {index}"
        if not isinstance(item, dict):
            raise ValidationError(f"{context} must be an object")
        item_allowed = {
            "item_id", "sequence", "item", "candidate_type", "candidate_status", "tier", "source_event_id",
            "source_article", "source_kind", "source_sentence", "article_source_hash", "sentence_hash",
            "evidence_states", "evidence_origin", "user_evidence", "bank_status", "bank_match_ids",
            "mastered_status", "mastery_proposal", "grounding", "card",
        }
        if set(item) - item_allowed:
            raise ValidationError(f"{context} has unsupported fields: {sorted(set(item) - item_allowed)}")
        _required(
            item,
            {
                "item_id",
                "sequence",
                "item",
                "candidate_type",
                "candidate_status",
                "tier",
                "source_event_id",
                "source_article",
                "source_sentence",
                "source_kind",
                "article_source_hash",
                "sentence_hash",
                "evidence_states",
                "evidence_origin",
                "user_evidence",
                "bank_status",
                "mastered_status",
                "grounding",
                "card",
            },
            context,
        )
        if item["item_id"] in seen_item_ids:
            raise ValidationError(f"duplicate candidate item_id: {item['item_id']}")
        seen_item_ids.add(item["item_id"])
        if item["source_kind"] == "explanation":
            raise ValidationError(f"{context} protected explanation cannot enter Luna candidate output")
        if item["evidence_origin"] not in {"live_user", "synthetic_fixture"}:
            raise ValidationError(f"{context} evidence_origin is invalid")
        if sentence_sha256(str(item["source_sentence"])) != item["sentence_hash"]:
            raise ValidationError(f"{context} source hash mismatch")
        if item["source_event_id"] not in capture_ids:
            raise ValidationError(f"{context} source_event_id is not frozen in candidate")
        if item["article_source_hash"] != candidate["article_source_hash"]:
            raise ValidationError(f"{context} article_source_hash differs from candidate package")
        if known_events:
            event = known_events[item["source_event_id"]]
            if event.get("source", {}).get("sentence_hash") != item["sentence_hash"]:
                raise ValidationError(f"{context} source differs from capture event")
            if event.get("article", {}).get("source_hash") != item["article_source_hash"]:
                raise ValidationError(f"{context} article source hash differs from capture event")
            if event.get("article", {}).get("article_id") != candidate["article_id"]:
                raise ValidationError(f"{context} article_id differs from capture event")
        mastered_status = item["mastered_status"]
        if item["candidate_status"] not in {"familiarity_candidate", "mastery_candidate"}:
            raise ValidationError(f"{context} candidate_status is invalid")
        if item["candidate_status"] == "mastery_candidate" and "independent_correct_use" not in item["evidence_states"]:
            raise ValidationError(f"{context} mastery_candidate requires independent_correct_use")
        if item["candidate_status"] == "mastery_candidate" and item["evidence_origin"] != "live_user":
            raise ValidationError(f"{context} synthetic_fixture cannot support mastery_candidate")
        if item["candidate_status"] == "mastery_candidate" and mastered_status != "mastery_proposed":
            raise ValidationError(f"{context} mastery_candidate requires mastered_status=mastery_proposed")
        proposal = item.get("mastery_proposal")
        if mastered_status == "mastery_proposed":
            if not isinstance(proposal, dict):
                raise ValidationError(f"{context} mastery_proposed requires mastery_proposal")
            if proposal.get("evidence_kind") != "independent_correct_use":
                raise ValidationError(f"{context} mastery gate requires independent_correct_use")
            for field in ("evidence_sentence", "evidence_context", "proof_note"):
                if not str(proposal.get(field, "")).strip():
                    raise ValidationError(f"{context} mastery proposal missing {field}")
            if set(proposal) != {"evidence_kind", "evidence_sentence", "evidence_context", "proof_note"}:
                raise ValidationError(f"{context} mastery proposal fields do not match schema")
            if "independent_correct_use" not in item["evidence_states"]:
                raise ValidationError(f"{context} mastery proposal lacks user active-use evidence")
        elif proposal is not None:
            raise ValidationError(f"{context} mastery_proposal is only allowed for mastery_proposed")

        grounding = item["grounding"]
        if not isinstance(grounding, dict):
            raise ValidationError(f"{context} grounding must be object")
        _required(grounding, {"status", "user_evidence_ref", "writing_pattern", "writing_vocabulary", "syllabus_occurrence", "sentence_pattern", "old_word_sources", "naturalness_check"}, f"{context} grounding")
        grounding_allowed = {"status", "user_evidence_ref", "writing_pattern", "writing_vocabulary", "syllabus_occurrence", "sentence_pattern", "old_word_sources", "naturalness_check"}
        if set(grounding) != grounding_allowed:
            raise ValidationError(f"{context} grounding fields do not match schema")
        if grounding["status"] not in GROUNDING_STATUSES:
            raise ValidationError(f"{context} grounding status is invalid")
        card = item["card"]
        if not isinstance(card, dict):
            raise ValidationError(f"{context} card must be object")
        card_allowed = {"meaning", "source_translation", "usage", "review_note", "adopted_pattern", "old_word_example", "example_translation", "structure_breakdown", "review_old_words"}
        if set(card) - card_allowed:
            raise ValidationError(f"{context} card has unsupported fields")
        _required(card, {"meaning", "source_translation", "usage", "review_note"}, f"{context} card")
        if card.get("old_word_example"):
            if grounding["status"] != "passed":
                raise ValidationError(f"{context} generated example requires passed four-layer grounding")
            if grounding["writing_pattern"].get("status") not in {"approved", "corrected"}:
                raise ValidationError(f"{context} writing pattern is not approved")
            if grounding["writing_vocabulary"].get("status") not in {"approved", "corrected"}:
                raise ValidationError(f"{context} writing vocabulary is not approved")
            if not str(grounding["syllabus_occurrence"].get("status", "")).startswith("verified"):
                raise ValidationError(f"{context} syllabus occurrence is not verified")

    return {
        "schema_version": candidate["schema_version"],
        "candidate_id": candidate["candidate_id"],
        "status": "PASS",
        "item_count": len(candidate["items"]),
        "capture_event_count": len(capture_ids),
        "formal_write_count": 0,
        "formal_writeback": "none",
    }


def validate_luna_candidate_file(path: Path, *, state_dir: Path | None = None) -> dict[str, Any]:
    candidate = load_json(path)
    result = validate_luna_candidate(candidate, state_dir=state_dir)
    result["path"] = str(path.resolve())
    result["sha256"] = object_sha256(candidate)
    return result


def render_luna_candidate(candidate: dict[str, Any]) -> str:
    validate_luna_candidate(candidate)
    if candidate.get("schema_version") == "english_luna_candidate_v2":
        return _render_luna_candidate_v2(candidate)
    lines = [
        f"# Luna 正式入库候选｜{candidate['candidate_id']}",
        "",
        f"- 文章：{candidate['article_id']}",
        f"- 日期：{candidate['study_date']}",
        f"- runtime identity：{candidate['runtime_identity']['status']}",
        f"- validation：{candidate['validation']['status']}",
        "- formal_write_count：0",
        "- formal_writeback：none",
        "",
    ]
    if not candidate["items"]:
        lines.extend(["本批次没有候选项。", ""])
        return "\n".join(lines)
    for item in sorted(candidate["items"], key=lambda value: (value["sequence"], value["item_id"])):
        card = item["card"]
        lines.extend(
            [
                "---",
                "",
                f"{item['sequence']} | {item['item']}",
                "含义",
                card["meaning"],
                "真题原句",
                item["source_sentence"],
                "原句中文",
                card["source_translation"],
                "用法",
                card["usage"],
                "一句话笔记",
                card["review_note"],
            ]
        )
        if card.get("adopted_pattern"):
            lines.extend(["采用句型", card["adopted_pattern"]])
        if card.get("old_word_example"):
            lines.extend(["系统生成旧词联动例句", card["old_word_example"]])
        if card.get("example_translation"):
            lines.extend(["例句中文", card["example_translation"]])
        if card.get("structure_breakdown"):
            lines.extend(["结构拆解", card["structure_breakdown"]])
        if card.get("review_old_words"):
            lines.extend(["复习旧词", "；".join(card["review_old_words"])])
        if item["grounding"].get("old_word_sources"):
            sources = [
                f"{row.get('id', '')}｜{row.get('item', '')}｜{row.get('status', '')}"
                for row in item["grounding"]["old_word_sources"]
            ]
            lines.extend(["复习词来源", "；".join(sources)])
        lines.extend(
            [
                "候选状态",
                f"{item['tier']}｜{item['candidate_status']}｜{item['bank_status']}｜{item['mastered_status']}",
                "四层地基",
                (
                    f"总状态={item['grounding']['status']}；"
                    f"作文句型={item['grounding']['writing_pattern'].get('status', '')}；"
                    f"作文词组={item['grounding']['writing_vocabulary'].get('status', '')}；"
                    f"大纲词={item['grounding']['syllabus_occurrence'].get('status', '')}；"
                    f"句式卡={item['grounding']['sentence_pattern'].get('status', '')}；"
                    f"自然度={item['grounding']['naturalness_check']}"
                ),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_luna_candidate_v2(candidate: dict[str, Any]) -> str:
    records = {
        row["sentence_record_id"]: row
        for row in candidate["sentence_records"]
    }
    lines = [
        f"# Luna 正式入库候选｜{candidate['candidate_id']}",
        "",
        f"- 文章：{candidate['source_id']}",
        f"- 日期：{candidate['study_date']}",
        f"- runtime identity：{candidate['runtime_identity']['status']}",
        "- formal_write_count：0",
        "",
        "## 句子记录",
        "",
    ]
    for record in sorted(candidate["sentence_records"], key=lambda row: row["sequence"]):
        lines.extend(
            [
                f"### {record['sentence_record_id']}",
                "",
                record["source_sentence"],
                "",
                f"用户首译：{record['user_first_translation'] or ''}",
                f"校正含义：{record['corrected_meaning']}",
                f"解释：{record['explanation']}",
                "",
            ]
        )
    lines.extend(["## 原子候选", ""])
    for item in sorted(candidate["items"], key=lambda row: (row["sequence"], row["item_id"])):
        record = records[item["sentence_record_id"]]
        lines.extend(
            [
                f"### {item['sequence']}｜{item['item']}",
                "",
                f"句子引用：{record['sentence_record_id']}｜{record['sentence_hash']}",
                f"含义：{item['card']['meaning']}",
                f"用法：{item['card']['usage']}",
                f"复习提示：{item['card']['review_note']}",
                "",
            ]
        )
    lines.extend(["## 复习状态提议", ""])
    for proposal in candidate["review_exclusion_proposals"]:
        lines.append(f"- 排除候选：{proposal['item']}｜{proposal['reason']}")
    for proposal in candidate["reactivation_proposals"]:
        lines.append(f"- 恢复未掌握候选：{proposal['item']}｜{proposal['reason']}")
    if not candidate["review_exclusion_proposals"] and not candidate["reactivation_proposals"]:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


def render_luna_candidate_file(path: Path, output: Path | None = None) -> tuple[Path | None, str]:
    candidate = load_json(path)
    text = render_luna_candidate(candidate)
    if output is not None:
        atomic_write_text(output, text)
    return output, text
    if candidate["article_id"] != candidate["source_id"]:
        raise ValidationError("Luna candidate article_id alias must equal canonical source_id")
