from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .constants import MASTERED_HEADER, MASTER_HEADER, MASTER_TYPES, SP_FIELDS, WRITING_VALUES
from .errors import AuthorizationError, ValidationError
from .formal import read_csv, resolve_formal_paths, serialize_csv, validate_sentence_patterns_text
from .review_status import (
    append_review_status_record,
    load_review_status_ledger,
    serialize_review_status_ledger,
)
from .learning_state import (
    append_learning_event, learning_events_path, load_learning_events,
    serialize_learning_events, validate_learning_event,
)
from .packages import (
    validate_canonical_writer_closeout,
    validate_conversation_package,
    validate_evidence_refs,
    validate_receipt_resolved_evidence,
)
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    bytes_sha256,
    file_sha256,
    load_json,
    normalize_item,
    object_sha256,
    utc_now,
    exclusive_lock,
)


SAFE_ACTIONS = {
    "master_bank_insert", "master_bank_update", "mastered_insert",
    "sentence_pattern_append", "sentence_pattern_merge",
    "review_exclusion_append", "review_reactivation_append",
    "learning_event_append",
}
NOOP_ACTIONS = {"skip_duplicate", "needs_user"}
UPDATE_FIELDS = {"meaning", "source_sentence", "usage", "tags", "review_note", "appear_count", "last_seen"}


class SimulatedWriterCrash(BaseException):
    """Test-only process crash that intentionally bypasses in-process rollback."""


OPEN_TRANSACTION_STATUSES = {"prepared", "committing", "committed", "committed_pending_receipt"}


def _optional_file_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _optional_file_sha256(path: Path) -> str:
    return file_sha256(path) if path.exists() else bytes_sha256(b"")


def _open_transaction_paths(state_dir: Path) -> list[Path]:
    pending: list[Path] = []
    for path in sorted((state_dir / "nightly").glob("*/*/transactions/*/transaction.json")):
        try:
            status = load_json(path).get("status")
        except Exception:
            pending.append(path)
            continue
        if status in OPEN_TRANSACTION_STATUSES:
            pending.append(path)
    return pending


def _require_fields(value: dict[str, Any], required: set[str], context: str) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise ValidationError(f"{context} missing fields: {missing}")


def _validate_sol_actions_v2(
    actions_doc: dict[str, Any],
    manifest: dict[str, Any],
    manifest_sha: str,
) -> None:
    if manifest.get("schema_version") not in {
        "english_nightly_manifest_v2",
        "english_nightly_manifest_v3",
    }:
        raise ValidationError("Sol actions v2 requires a supported package manifest")
    required_top = {
        "schema_version", "action_set_id", "batch_id", "batch_manifest_sha256",
        "created_at", "producer", "formal_write_count", "formal_writeback",
        "actions", "unresolved",
    }
    if set(actions_doc) != required_top:
        raise ValidationError("Sol actions v2 top-level fields do not match schema")
    if actions_doc.get("schema_version") != "english_sol_actions_v2":
        raise ValidationError("Sol actions v2 schema_version mismatch")
    if actions_doc.get("batch_id") != manifest.get("batch_id"):
        raise ValidationError("Sol actions batch_id does not match frozen package batch")
    if actions_doc.get("batch_manifest_sha256") != manifest_sha:
        raise ValidationError("Sol actions manifest hash mismatch")
    if actions_doc.get("formal_write_count") != 0 or actions_doc.get("formal_writeback") != "none":
        raise ValidationError("Sol proposal must declare zero formal writes")
    producer = actions_doc.get("producer")
    if not isinstance(producer, dict) or producer.get("role") != "sol_nightly_reviewer":
        raise ValidationError("Sol producer role must be sol_nightly_reviewer")
    package_documents = manifest.get("package_documents")
    if not isinstance(package_documents, list):
        raise ValidationError("nightly manifest package_documents must be an array")
    frozen_sha256s: list[str] = []
    for index, record in enumerate(package_documents, start=1):
        if not isinstance(record, dict):
            raise ValidationError(f"package document {index} must be an object")
        package_root = Path(str(record.get("path", "")))
        package = validate_conversation_package(package_root)
        if (
            package.get("package_id") != record.get("package_id")
            or package.get("package_canonical_sha256") != record.get("package_sha256")
            or file_sha256(package_root / "manifest.json") != record.get("manifest_sha256")
        ):
            raise ValidationError(f"frozen conversation package drift: {package_root}")
        frozen_sha256s.append(record["package_sha256"])
    if frozen_sha256s != manifest.get("package_sha256s"):
        raise ValidationError("nightly manifest package SHA order is inconsistent")
    if not isinstance(actions_doc.get("actions"), list) or not isinstance(actions_doc.get("unresolved"), list):
        raise ValidationError("Sol actions and unresolved must be arrays")
    action_ids: set[str] = set()
    for index, action in enumerate(actions_doc["actions"], start=1):
        context = f"action {index}"
        if not isinstance(action, dict):
            raise ValidationError(f"{context} must be an object")
        _require_fields(action, {"action_id", "action_type", "reason", "evidence_refs"}, context)
        if action["action_id"] in action_ids:
            raise ValidationError(f"duplicate action_id: {action['action_id']}")
        action_ids.add(action["action_id"])
        kind = action["action_type"]
        if kind not in SAFE_ACTIONS | {"skip_duplicate"}:
            raise ValidationError(f"unsupported action_type: {kind}")
        refs = validate_evidence_refs(action["evidence_refs"], package_documents)
        expected_fields = {
            "master_bank_insert": {"action_id", "action_type", "reason", "evidence_refs", "row"},
            "master_bank_update": {"action_id", "action_type", "reason", "evidence_refs", "record_id", "updates"},
            "mastered_insert": {"action_id", "action_type", "reason", "evidence_refs", "mastery_evidence_kind", "row"},
            "sentence_pattern_append": {"action_id", "action_type", "reason", "evidence_refs", "fields"},
            "sentence_pattern_merge": {"action_id", "action_type", "reason", "evidence_refs", "existing_sp_id", "additions"},
            "skip_duplicate": {"action_id", "action_type", "reason", "evidence_refs", "target", "item"},
            "review_exclusion_append": {"action_id", "action_type", "reason", "evidence_refs", "item", "bank_id", "sentence_evidence"},
            "review_reactivation_append": {"action_id", "action_type", "reason", "evidence_refs", "item", "bank_id", "sentence_evidence"},
            "learning_event_append": {"action_id", "action_type", "reason", "evidence_refs", "event"},
        }[kind]
        if set(action) != expected_fields:
            raise ValidationError(f"{context} fields do not match typed action schema")
        action["_resolved_evidence"] = refs
        def evidence_strings(value):
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                return [text for child in value.values() for text in evidence_strings(child)]
            if isinstance(value, list):
                return [text for child in value for text in evidence_strings(child)]
            return []
        # Compare source prose with its original text, not JSON escape syntax.
        # Real paragraphs contain newlines and quotations that json.dumps escapes.
        bound_text = "\n".join(text for ref in refs for text in evidence_strings(ref["value"])).casefold()
        if kind == "learning_event_append":
            if "learning_events" not in manifest.get("formal_snapshot", {}):
                raise ValidationError("formal learning events require a current learning-ledger snapshot")
            action["_learning_visibility"] = validate_learning_event(
                action["event"], refs, study_date=manifest["study_date"],
                package_documents=package_documents,
            )
            continue
        if kind == "skip_duplicate":
            if not str(action.get("item", "")).strip() or not str(action.get("target", "")).strip():
                raise ValidationError(f"{context} skip_duplicate needs target and item")
            continue
        if kind in {"review_exclusion_append", "review_reactivation_append"}:
            if not str(action.get("item", "")).strip() or not str(action.get("sentence_evidence", "")).strip():
                raise ValidationError(f"{context} review status action lacks raw evidence")
            continue
        if kind == "master_bank_insert":
            row = action.get("row")
            if not isinstance(row, dict) or set(row) != set(MASTER_HEADER):
                raise ValidationError(f"{context} master row must contain exactly 13 formal columns")
            if row["id"] not in {"", None}:
                raise ValidationError(f"{context} Sol must not allocate master_bank id")
            if row["type"] not in MASTER_TYPES or row["writing_value"] not in WRITING_VALUES:
                raise ValidationError(f"{context} master row enum is invalid")
            if not str(row["item"]).strip() or not str(row["source_sentence"]).strip():
                raise ValidationError(f"{context} master row lacks item/source_sentence")
            if str(row["source_sentence"]).casefold() not in bound_text:
                raise ValidationError(f"{context} source_sentence is absent from package evidence")
            if not isinstance(row["appear_count"], int) or row["appear_count"] < 1:
                raise ValidationError(f"{context} appear_count must be a positive integer")
        elif kind == "master_bank_update":
            if not str(action.get("record_id", "")).strip():
                raise ValidationError(f"{context} record_id is required")
            updates = action.get("updates")
            if not isinstance(updates, dict) or not updates or not set(updates).issubset(UPDATE_FIELDS):
                raise ValidationError(f"{context} updates contain unsupported fields")
            if "source_sentence" in updates and str(updates["source_sentence"]).casefold() not in bound_text:
                raise ValidationError(f"{context} corrected source sentence is absent from raw evidence")
            if "meaning" in updates and (not str(updates["meaning"]).strip() or str(updates["meaning"]).casefold() not in bound_text):
                raise ValidationError(f"{context} corrected meaning is absent from raw evidence")
        elif kind == "mastered_insert":
            row = action.get("row")
            if action.get("mastery_evidence_kind") != "independent_correct_use":
                raise ValidationError(f"{context} mastery gate requires independent_correct_use")
            if not any(
                ref["binding"]["kind"] == "independent_correct_use"
                for ref in refs
            ):
                raise ValidationError(f"{context} lacks an independent_correct_use evidence ref")
            if not isinstance(row, dict) or set(row) != set(MASTERED_HEADER):
                raise ValidationError(f"{context} mastered row must contain exactly 7 columns")
            for field in ("item", "evidence_sentence", "evidence_context", "proof_note"):
                if not str(row.get(field, "")).strip():
                    raise ValidationError(f"{context} mastered row missing {field}")
        elif kind == "sentence_pattern_append":
            fields = action.get("fields")
            if not isinstance(fields, dict) or set(fields) != set(SP_FIELDS):
                raise ValidationError(f"{context} SP proposal must contain exactly 15 fields")
            if re.search(r"\bSP-\d{3}\b", str(fields["title"])):
                raise ValidationError(f"{context} Sol must not allocate SP id")
            if not isinstance(fields["结构拆解"], list) or not isinstance(fields["来源与示例"], list):
                raise ValidationError(f"{context} SP list fields are invalid")
        elif kind == "sentence_pattern_merge":
            if not re.fullmatch(r"SP-\d{3}", str(action.get("existing_sp_id", ""))):
                raise ValidationError(f"{context} existing_sp_id is invalid")
            additions = action.get("additions")
            allowed = {"来源与示例", "常用变体", "相关词汇/搭配", "use_count_increment", "last_used"}
            if not isinstance(additions, dict) or not additions or not set(additions).issubset(allowed):
                raise ValidationError(f"{context} merge additions are invalid")
    for index, decision in enumerate(actions_doc["unresolved"], start=1):
        context = f"unresolved {index}"
        expected = {"action_id", "status", "reason", "evidence_refs", "target", "item"}
        if not isinstance(decision, dict) or set(decision) != expected:
            raise ValidationError(f"{context} fields do not match unresolved schema")
        if decision["action_id"] in action_ids:
            raise ValidationError(f"duplicate action_id: {decision['action_id']}")
        action_ids.add(decision["action_id"])
        if decision["status"] != "needs_user":
            raise ValidationError(f"unsupported unresolved status: {decision['status']}")
        decision["_resolved_evidence"] = validate_evidence_refs(
            decision["evidence_refs"], package_documents
        )
        if not str(decision["item"]).strip() or not str(decision["target"]).strip():
            raise ValidationError(f"{context} needs target and item")


def validate_sol_actions(
    actions_doc: dict[str, Any],
    manifest: dict[str, Any],
    manifest_sha: str,
) -> None:
    if actions_doc.get("schema_version") != "english_sol_actions_v2":
        raise ValidationError(
            "new formal writes require package-backed english_sol_actions_v2; historical evidence is read-only"
        )
    _validate_sol_actions_v2(actions_doc, manifest, manifest_sha)


def _next_master_id(rows: list[dict[str, Any]], row_date: str) -> str:
    prefix = row_date.replace("-", "")
    used = {
        int(match.group(1))
        for row in rows
        if (match := re.fullmatch(re.escape(prefix) + r"-(\d{3})", str(row.get("id", ""))))
    }
    sequence = max(used, default=0) + 1
    while sequence in used:
        sequence += 1
    return f"{prefix}-{sequence:03d}"


def _next_sp_id(text: str) -> str:
    used = [int(value) for value in re.findall(r"^## SP-(\d{3})｜", text, flags=re.MULTILINE)]
    return f"SP-{max(used, default=0) + 1:03d}"


def _render_sp_card(card_id: str, fields: dict[str, Any]) -> str:
    lines = [
        f"## {card_id}｜{fields['title']}",
        "",
        f"**骨架**：`{fields['骨架']}`",
        f"**难度等级**：{fields['难度等级']}",
        f"**基本句型**：{fields['基本句型']}",
        f"**从句类型**：{fields['从句类型']}",
        f"**场景标签**：{fields['场景标签']}",
        f"**可复用程度**：{fields['可复用程度']}",
        f"**中文解释**：{fields['中文解释']}",
        "**结构拆解**：",
        *[f"- {line}" for line in fields["结构拆解"]],
        f"**生成模板**：`{fields['生成模板']}`",
        f"**相关词汇/搭配**：{fields['相关词汇/搭配']}",
        f"**常用变体**：{fields['常用变体']}",
        "**来源与示例**：",
        *[f"{index}. {line}" for index, line in enumerate(fields["来源与示例"], start=1)],
        f"**use_count**：{fields['use_count']}",
        f"**last_used**：{fields['last_used']}",
        "",
    ]
    return "\n".join(lines)


def _merge_sp_card(text: str, existing_sp_id: str, additions: dict[str, Any]) -> tuple[str, bool]:
    headings = list(re.finditer(r"^## (SP-\d{3})｜.+$", text, flags=re.MULTILINE))
    matches = [(index, match) for index, match in enumerate(headings) if match.group(1) == existing_sp_id]
    if len(matches) != 1:
        raise ValidationError(f"sentence_pattern_merge target not unique: {existing_sp_id}")
    index, heading = matches[0]
    start = heading.start()
    end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
    block = text[start:end]
    original = block

    def append_inline(field: str, separator: str) -> None:
        nonlocal block
        additions_list = [str(value).strip() for value in additions.get(field, []) if str(value).strip()]
        if not additions_list:
            return
        pattern = re.compile(rf"^(\*\*{re.escape(field)}\*\*：)(.*)$", flags=re.MULTILINE)
        match = pattern.search(block)
        if not match:
            raise ValidationError(f"{existing_sp_id} missing merge field {field}")
        existing = [value.strip() for value in re.split(r"[、；|]", match.group(2)) if value.strip()]
        for value in additions_list:
            if value not in existing:
                existing.append(value)
        block = block[: match.start()] + match.group(1) + separator.join(existing) + block[match.end() :]

    append_inline("相关词汇/搭配", "、")
    append_inline("常用变体", "；")

    sources_to_add = [str(value).strip() for value in additions.get("来源与示例", []) if str(value).strip()]
    if sources_to_add:
        source_match = re.search(
            r"^\*\*来源与示例\*\*：\n(?P<body>.*?)(?=^\*\*use_count\*\*：)",
            block,
            flags=re.MULTILINE | re.DOTALL,
        )
        if not source_match:
            raise ValidationError(f"{existing_sp_id} missing 来源与示例 block")
        existing_sources = [match.group(1).strip() for match in re.finditer(r"^\d+\.\s*(.+)$", source_match.group("body"), re.MULTILINE)]
        for value in sources_to_add:
            if value not in existing_sources:
                existing_sources.append(value)
        rendered = "".join(f"{number}. {value}\n" for number, value in enumerate(existing_sources, start=1))
        block = block[: source_match.start("body")] + rendered + block[source_match.end("body") :]

    increment = int(additions.get("use_count_increment", 0))
    if increment:
        count_match = re.search(r"^(\*\*use_count\*\*：)(\d+)$", block, flags=re.MULTILINE)
        if not count_match:
            raise ValidationError(f"{existing_sp_id} missing use_count")
        new_count = int(count_match.group(2)) + increment
        block = block[: count_match.start()] + count_match.group(1) + str(new_count) + block[count_match.end() :]
    if additions.get("last_used"):
        last_match = re.search(r"^(\*\*last_used\*\*：)(.+)$", block, flags=re.MULTILINE)
        if not last_match:
            raise ValidationError(f"{existing_sp_id} missing last_used")
        desired = str(additions["last_used"])
        block = block[: last_match.start()] + last_match.group(1) + desired + block[last_match.end() :]
    return text[:start] + block + text[end:], block != original


def _simulate(
    actions: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    *,
    master_rows: list[dict[str, Any]],
    mastered_rows: list[dict[str, Any]],
    sp_text: str,
    review_status_records: list[dict[str, Any]],
    learning_event_records: list[dict[str, Any]],
    study_date: str,
    dry_run: bool,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], str, list[dict[str, Any]],
    list[dict[str, Any]], int, int, list[dict[str, Any]],
]:
    master = [dict(row) for row in master_rows]
    mastered = [dict(row) for row in mastered_rows]
    patterns = sp_text
    review_records = [dict(row) for row in review_status_records]
    learning_records = [dict(row) for row in learning_event_records]
    results: list[dict[str, Any]] = []
    safe_count = 0
    unresolved_count = 0
    for action in actions:
        kind = action["action_type"]
        result = {
            "action_id": action["action_id"],
            "action_type": kind,
            "target": action.get("target", kind),
        }
        if "evidence_refs" in action:
            result["evidence_refs"] = action["evidence_refs"]
        if "_resolved_evidence" in action:
            result["resolved_evidence"] = [
                row["binding"] for row in action["_resolved_evidence"]
            ]
        if kind == "skip_duplicate":
            result.update(result="skipped", message=action["reason"])
            results.append(result)
            continue
        if kind == "master_bank_insert":
            row = dict(action["row"])
            normalized = normalize_item(row["item"])
            existing_rows = [existing for existing in master if normalize_item(existing["item"]) == normalized]
            if existing_rows:
                proposed = {key: str(value) for key, value in row.items() if key != "id"}
                exact = any(all(str(existing.get(key, "")) == value for key, value in proposed.items()) for existing in existing_rows)
                if exact:
                    result.update(result="skipped", message="idempotent existing master_bank row")
                    results.append(result)
                    continue
                raise ValidationError(f"master_bank_insert conflicts with existing item: {row['item']}")
            row["id"] = _next_master_id(master, row["date"])
            master.append(row)
            result["assigned_id"] = row["id"]
        elif kind == "master_bank_update":
            matches = [row for row in master if row["id"] == action["record_id"]]
            if len(matches) != 1:
                raise ValidationError(f"master_bank_update record_id not unique: {action['record_id']}")
            updates = {key: str(value) for key, value in action["updates"].items()}
            changes = {key: value for key, value in updates.items() if str(matches[0].get(key, "")) != value}
            if not changes:
                result.update(result="skipped", assigned_id=action["record_id"], message="idempotent no-change update")
                results.append(result)
                continue
            matches[0].update(changes)
            result["assigned_id"] = action["record_id"]
        elif kind == "mastered_insert":
            row = dict(action["row"])
            normalized = normalize_item(row["item"])
            existing_rows = [existing for existing in mastered if normalize_item(existing["item"]) == normalized]
            if existing_rows:
                if any(all(str(existing.get(key, "")) == str(value) for key, value in row.items()) for existing in existing_rows):
                    result.update(result="skipped", message="idempotent existing mastered row")
                    results.append(result)
                    continue
                if str(row["mastered_date"]) < max(str(existing["mastered_date"]) for existing in existing_rows):
                    raise ValidationError("new mastery proof cannot regress the recorded chronology")
            mastered.append(row)
            review_records.append(append_review_status_record(
                review_records, event_type="independent_mastery_confirmed", item=row["item"],
                bank_id=row["matched_id"], study_date=study_date,
                source_capture_event_ids=[ref["package_id"] for ref in action["evidence_refs"]],
                sentence_evidence=[{"evidence_sentence": row["evidence_sentence"], "proof_note": row["proof_note"]}],
                reason=action["reason"],
            ))
        elif kind == "sentence_pattern_append":
            title_norm = normalize_item(action["fields"]["title"])
            if any(title_norm == normalize_item(title) for title in re.findall(r"^## SP-\d{3}｜(.+)$", patterns, re.MULTILINE)):
                raise ValidationError("sentence_pattern_append duplicate title must use sentence_pattern_merge")
            card_id = _next_sp_id(patterns)
            if not patterns.endswith("\n"):
                patterns += "\n"
            patterns += "\n" + _render_sp_card(card_id, action["fields"])
            result["assigned_id"] = card_id
        elif kind == "sentence_pattern_merge":
            patterns, changed = _merge_sp_card(patterns, action["existing_sp_id"], action["additions"])
            if not changed:
                result.update(result="skipped", assigned_id=action["existing_sp_id"], message="idempotent SP merge")
                results.append(result)
                continue
            result["assigned_id"] = action["existing_sp_id"]
        elif kind in {"review_exclusion_append", "review_reactivation_append"}:
            source_ids = action.get("source_capture_event_ids") or [
                ref["package_id"] for ref in action.get("evidence_refs", [])
            ]
            event_type = (
                "exclude_from_review"
                if kind == "review_exclusion_append"
                else "reactivate_for_review"
            )
            if any(
                row.get("event_type") == event_type
                and normalize_item(str(row.get("item", "")))
                == normalize_item(action["item"])
                and row.get("bank_id") == action["bank_id"]
                and row.get("source_capture_event_ids") == source_ids
                for row in review_records
            ):
                result.update(result="skipped", message="idempotent review status event")
                results.append(result)
                continue
            record = append_review_status_record(
                review_records,
                event_type=event_type,
                item=action["item"],
                bank_id=action["bank_id"],
                study_date=study_date,
                source_capture_event_ids=source_ids,
                sentence_evidence=action["sentence_evidence"],
                reason=action["reason"],
            )
            review_records.append(record)
            result["assigned_id"] = record["event_id"]
        elif kind == "learning_event_append":
            event = action["event"]
            bank_ids = {row["id"] for row in master}
            sp_ids = set(re.findall(r"^## (SP-\d+)｜", patterns, re.MULTILINE))
            if not set(event["bank_ids"]).issubset(bank_ids) or not set(event["sentence_pattern_ids"]).issubset(sp_ids):
                raise ValidationError("learning event targets missing formal bank/SP identities")
            known_concepts = {"bank:" + key for key in bank_ids} | {"sp:" + key for key in sp_ids}
            if not set(event["concept_ids"]).issubset(known_concepts):
                raise ValidationError("learning event concept identity is not a current bank/SP concept")
            record, created = append_learning_event(
                learning_records, event, [row["binding"] for row in action["_resolved_evidence"]],
                visibility=action["_learning_visibility"],
            )
            result["assigned_id"] = record["event_id"]
            result["learning_event_sha256"] = record["record_sha256"]
            result["learning_event_visibility"] = action["_learning_visibility"]
            result["learning_event_targets"] = {
                "vocabulary": sorted(set(event["bank_ids"]) | {key[5:] for key in event["concept_ids"] if key.startswith("bank:")}),
                "sentence_patterns": sorted(set(event["sentence_pattern_ids"]) | {key[3:] for key in event["concept_ids"] if key.startswith("sp:")}),
            }
            if not created:
                result.update(result="skipped", message="idempotent formal learning event")
                results.append(result)
                continue
            # Only actual learning evidence changes eligibility. A's proposal
            # acceptance/rejection is retained without inventing learner mastery.
            if event["kind"] in {"answer", "review"}:
                status_event = (
                    "reactivate_for_review" if event["outcome"] == "incorrect" or event["reasoning_status"] == "invalid"
                    else "independent_mastery_confirmed"
                    if event["outcome"] == "correct" and event["hint_dependence"] == "none" and event["reasoning_status"] == "valid"
                    else None
                )
                if status_event:
                    affected = set(event["bank_ids"]) | {key[5:] for key in event["concept_ids"] if key.startswith("bank:")}
                    for bank_row in master:
                        if bank_row["id"] in affected:
                            review_records.append(append_review_status_record(
                                review_records, event_type=status_event, item=bank_row["item"], bank_id=bank_row["id"],
                                study_date=study_date, source_capture_event_ids=[ref["package_id"] for ref in action["evidence_refs"]],
                                sentence_evidence=[{"learning_event_id": record["event_id"], "user_response": event["user_response"]}],
                                reason=action["reason"],
                                observed_at=event["observed_at"],
                            ))
        safe_count += 1
        result.update(result="would_apply" if dry_run else "applied", message=action["reason"])
        results.append(result)
    for decision in unresolved:
        unresolved_count += 1
        unresolved_result = {
                "action_id": decision["action_id"],
                "action_type": "needs_user",
                "target": decision["target"],
                "result": "needs_user",
                "message": decision["reason"],
            }
        if "evidence_refs" in decision:
            unresolved_result["evidence_refs"] = decision["evidence_refs"]
        if "_resolved_evidence" in decision:
            unresolved_result["resolved_evidence"] = [
                row["binding"] for row in decision["_resolved_evidence"]
            ]
        results.append(unresolved_result)
    return (
        master, mastered, patterns, review_records, results,
        safe_count, unresolved_count,
        learning_records,
    )


def _validated_journal_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    expected_previous = "0" * 64
    for expected_sequence, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            raise ValidationError(f"journal blank record at {path}:{expected_sequence}")
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValidationError(f"journal record is not an object at {path}:{expected_sequence}")
        core = {key: value for key, value in record.items() if key != "record_sha256"}
        if record.get("sequence") != expected_sequence:
            raise ValidationError(f"journal sequence break at {path}:{expected_sequence}")
        if record.get("previous_sha256") != expected_previous:
            raise ValidationError(f"journal previous hash break at {path}:{expected_sequence}")
        if object_sha256(core) != record.get("record_sha256"):
            raise ValidationError(f"journal record hash mismatch at {path}:{expected_sequence}")
        expected_previous = record["record_sha256"]
        records.append(record)
    return records


def _append_journal(path: Path, receipt_id: str, event_type: str, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_existed = path.exists()
    previous_hash = "0" * 64
    sequence = 1
    if path.exists():
        records = _validated_journal_records(path)
        if records:
            previous_hash = records[-1]["record_sha256"]
            sequence = len(records) + 1
    core = {
        "sequence": sequence,
        "receipt_id": receipt_id,
        "event_type": event_type,
        "at": utc_now(),
        "previous_sha256": previous_hash,
        "data": data,
    }
    record = {**core, "record_sha256": object_sha256(core)}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if not file_existed:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def apply_nightly(
    state_dir: Path,
    repo_root: Path,
    *,
    manifest_path: Path,
    actions_path: Path,
    apply: bool = False,
    authorization: str | None = None,
    authorized_at: str | None = None,
    support_preflight_path: Path | None = None,
    fault_after_replacements: int | None = None,
    fault_after_commit: bool = False,
    fault_after_receipt: bool = False,
    publication_runner: Any = None,
) -> tuple[Path, dict[str, Any]]:
    state_dir = state_dir.resolve()
    repo_root = repo_root.resolve()
    manifest_path = manifest_path.resolve()
    actions_path = actions_path.resolve()
    manifest = load_json(manifest_path)
    if apply and manifest.get("schema_version") == "english_nightly_manifest_v2":
        raise ValidationError(
            "english_nightly_manifest_v2 is legacy read-only; new formal apply requires v3"
        )
    if manifest.get("schema_version") == "english_nightly_manifest_v3":
        from .sentence_support import sentence_support_required

        sentence_support_required(manifest)
    actions_doc = load_json(actions_path)
    manifest_sha = file_sha256(manifest_path)
    actions_sha = file_sha256(actions_path)
    from .web_review_gate import validate_writer_admission
    validate_writer_admission(state_dir, manifest, actions_path)
    validate_sol_actions(actions_doc, manifest, manifest_sha)
    if apply and authorization != manifest["batch_id"]:
        raise AuthorizationError("apply requires --authorization equal to the exact frozen batch_id")
    support_preflight_binding: dict[str, Any] | None = None
    if apply and manifest.get("schema_version") == "english_nightly_manifest_v3":
        from .sentence_support import (
            support_preflight_receipt_path,
            validate_sentence_support_preflight,
        )

        resolved_preflight = support_preflight_path or support_preflight_receipt_path(
            state_dir, manifest["study_date"], manifest["batch_id"]
        )
        preflight_receipt, _ = validate_sentence_support_preflight(
            state_dir,
            manifest_path=manifest_path,
            actions_path=actions_path,
            receipt_path=resolved_preflight,
        )
        resolved_preflight = resolved_preflight.resolve(strict=True)
        support_preflight_binding = {
            "support_preflight_receipt_id": preflight_receipt["receipt_id"],
            "support_preflight_receipt_path": str(resolved_preflight),
            "support_preflight_receipt_sha256": file_sha256(resolved_preflight),
            "support_proposal_sha256": preflight_receipt["proposal_sha256"],
        }
    started_at = utc_now()
    mode = "apply" if apply else "dry_run"
    receipt_id = "EN-RECEIPT-" + object_sha256(
        {"batch_id": manifest["batch_id"], "action_set_id": actions_doc["action_set_id"], "mode": mode, "time_ns": time.time_ns()}
    )[:16].upper()
    study_date = manifest["study_date"]
    journal_path = state_dir / "nightly" / study_date / f"{manifest['batch_id']}.journal.jsonl"
    receipt_path = state_dir / "receipts" / "nightly" / study_date / f"{receipt_id}.json"
    lock_path = state_dir / "locks" / "formal.lock"
    formal_paths = resolve_formal_paths(repo_root)
    formal_paths["review_exclusion_ledger"] = (
        repo_root / "bank" / "review_exclusion_ledger.jsonl"
    ).resolve()
    if "learning_events" in manifest.get("formal_snapshot", {}):
        formal_paths["learning_events"] = learning_events_path(repo_root).resolve()

    with exclusive_lock(lock_path):
        pending_transactions = _open_transaction_paths(state_dir)
        if pending_transactions:
            raise ValidationError(f"recovery_required: open formal transactions {pending_transactions}")
        prehashes = {
            name: _optional_file_sha256(path)
            for name, path in formal_paths.items()
        }
        expected = {name: manifest["formal_snapshot"][name]["sha256"] for name in formal_paths}
        _append_journal(journal_path, receipt_id, "started", {"mode": mode, "prehashes": prehashes})
        if prehashes != expected:
            receipt = {
                "schema_version": "english_apply_receipt_v1",
                "receipt_id": receipt_id,
                "batch_id": manifest["batch_id"],
                "action_set_id": actions_doc["action_set_id"],
                "mode": mode,
                "status": "CAS_CONFLICT",
                "started_at": started_at,
                "completed_at": utc_now(),
                "authorization": None,
                "manifest_sha256": manifest_sha,
                "actions_sha256": actions_sha,
                "lock_path": str(lock_path),
                "journal_path": str(journal_path),
                "formal_prehashes": prehashes,
                "proposed_posthashes": {},
                "formal_posthashes": prehashes,
                "formal_files_changed": False,
                "formal_write_count": 0,
                "action_results": [],
                "validation": {"status": "FAIL", "reason": "formal prehash drift", "expected": expected},
                **(support_preflight_binding or {}),
            }
            _append_journal(journal_path, receipt_id, "cas_conflict", receipt["validation"])
            atomic_write_json(receipt_path, receipt)
            return receipt_path, receipt

        master_rows = read_csv(formal_paths["master_bank"], MASTER_HEADER)
        mastered_rows = read_csv(formal_paths["mastered_items"], MASTERED_HEADER)
        sp_text = formal_paths["sentence_patterns"].read_text(encoding="utf-8")
        review_status_records = load_review_status_ledger(
            formal_paths["review_exclusion_ledger"]
        )
        learning_event_records = load_learning_events(learning_events_path(repo_root))
        (
            master, mastered, patterns, review_status_records,
            results, safe_count, unresolved_count,
            learning_event_records,
        ) = _simulate(
            actions_doc["actions"],
            actions_doc["unresolved"],
            master_rows=master_rows,
            mastered_rows=mastered_rows,
            sp_text=sp_text,
            review_status_records=review_status_records,
            learning_event_records=learning_event_records,
            study_date=study_date,
            dry_run=not apply,
        )
        changed_action_types = {
            result["action_type"]
            for result in results
            if result["result"] in {"would_apply", "applied"}
        }
        proposed_bytes = {
            "master_bank": (
                serialize_csv(master, MASTER_HEADER)
                if changed_action_types & {"master_bank_insert", "master_bank_update"}
                else _optional_file_bytes(formal_paths["master_bank"])
            ),
            "mastered_items": (
                serialize_csv(mastered, MASTERED_HEADER)
                if "mastered_insert" in changed_action_types
                else _optional_file_bytes(formal_paths["mastered_items"])
            ),
            "sentence_patterns": (
                patterns.encode("utf-8")
                if changed_action_types & {"sentence_pattern_append", "sentence_pattern_merge"}
                else _optional_file_bytes(formal_paths["sentence_patterns"])
            ),
            "review_exclusion_ledger": (
                serialize_review_status_ledger(review_status_records)
                if changed_action_types
                & {"review_exclusion_append", "review_reactivation_append", "mastered_insert", "learning_event_append"}
                else _optional_file_bytes(
                    formal_paths["review_exclusion_ledger"]
                )
            ),
        }
        if "learning_events" in formal_paths:
            proposed_bytes["learning_events"] = (
                serialize_learning_events(learning_event_records)
                if "learning_event_append" in changed_action_types
                else _optional_file_bytes(formal_paths["learning_events"])
            )
        validate_sentence_patterns_text(patterns)
        proposed_hashes = {name: bytes_sha256(data) for name, data in proposed_bytes.items()}
        _append_journal(
            journal_path,
            receipt_id,
            "validated",
            {"safe_action_count": safe_count, "unresolved_count": unresolved_count, "proposed_hashes": proposed_hashes},
        )
        authorization_record = None
        if apply:
            authorization_record = {
                "command": "apply-nightly",
                "batch_id": manifest["batch_id"],
                "study_date": study_date,
                "authorized_at": authorized_at or utc_now(),
                "scope": manifest["authorization"]["scope"],
            }
            before_commit = {
                name: _optional_file_sha256(path)
                for name, path in formal_paths.items()
            }
            if before_commit != prehashes:
                raise ValidationError("formal files drifted after lock acquisition")
            transaction_dir = state_dir / "nightly" / study_date / manifest["batch_id"] / "transactions" / receipt_id
            backup_dir = transaction_dir / "preimages"
            staged_dir = transaction_dir / "staged"
            for name, path in formal_paths.items():
                atomic_write_bytes(
                    backup_dir / path.name, _optional_file_bytes(path)
                )
                atomic_write_bytes(staged_dir / path.name, proposed_bytes[name])
            transaction_path = transaction_dir / "transaction.json"
            transaction = {
                "schema_version": "english_formal_transaction_v1",
                "receipt_id": receipt_id,
                "batch_id": manifest["batch_id"],
                "study_date": study_date,
                "status": "prepared",
                "journal_path": str(journal_path),
                "formal_paths": {name: str(path) for name, path in formal_paths.items()},
                "preimages": {name: str(backup_dir / path.name) for name, path in formal_paths.items()},
                "staged": {name: str(staged_dir / path.name) for name, path in formal_paths.items()},
                "prehashes": prehashes,
                "proposed_hashes": proposed_hashes,
                "safe_action_count": safe_count,
                "unresolved_count": unresolved_count,
                "receipt_path": str(receipt_path),
                **(support_preflight_binding or {}),
                "replaced": [],
            }
            atomic_write_json(transaction_path, transaction)
            _append_journal(journal_path, receipt_id, "transaction_prepared", {"path": str(transaction_path)})
            replaced: list[str] = []
            try:
                for name, path in formal_paths.items():
                    if proposed_hashes[name] != prehashes[name]:
                        atomic_write_bytes(path, proposed_bytes[name])
                        replaced.append(name)
                        transaction["status"] = "committing"
                        transaction["replaced"] = list(replaced)
                        atomic_write_json(transaction_path, transaction)
                        if fault_after_replacements is not None and len(replaced) >= fault_after_replacements:
                            raise SimulatedWriterCrash(str(transaction_path))
                posthashes = {
                    name: _optional_file_sha256(path)
                    for name, path in formal_paths.items()
                }
                if posthashes != proposed_hashes:
                    raise ValidationError("post-write hashes differ from proposed hashes")
            except Exception:
                for name, path in formal_paths.items():
                    backup = backup_dir / path.name
                    if backup.exists():
                        atomic_write_bytes(path, backup.read_bytes())
                _append_journal(journal_path, receipt_id, "rolled_back", {"replaced": replaced})
                transaction["status"] = "rolled_back"
                atomic_write_json(transaction_path, transaction)
                raise
            status = "PARTIAL" if unresolved_count else ("APPLIED" if safe_count else "NO_ACTION")
            write_count = safe_count
            changed = posthashes != prehashes
            _append_journal(journal_path, receipt_id, "committed_pending_receipt", {"status": status, "posthashes": posthashes})
            transaction["status"] = "committed_pending_receipt"
            transaction["posthashes"] = posthashes
            atomic_write_json(transaction_path, transaction)
            if fault_after_commit:
                raise SimulatedWriterCrash(str(transaction_path))
        else:
            posthashes = {
                name: _optional_file_sha256(path)
                for name, path in formal_paths.items()
            }
            status = "DRY_RUN_PARTIAL" if unresolved_count else ("DRY_RUN_VALID" if safe_count else "DRY_RUN_NO_ACTION")
            write_count = 0
            changed = False
            _append_journal(journal_path, receipt_id, "dry_run_complete", {"status": status})

        receipt = {
            "schema_version": "english_apply_receipt_v1",
            "receipt_id": receipt_id,
            "batch_id": manifest["batch_id"],
            "action_set_id": actions_doc["action_set_id"],
            "mode": mode,
            "status": status,
            "started_at": started_at,
            "completed_at": utc_now(),
            "authorization": authorization_record,
            "manifest_sha256": manifest_sha,
            "actions_sha256": actions_sha,
            "package_ids": manifest.get("package_ids", []),
            "package_sha256s": manifest.get("package_sha256s", []),
            "lock_path": str(lock_path),
            "journal_path": str(journal_path),
            "formal_prehashes": prehashes,
            "proposed_posthashes": proposed_hashes,
            "formal_posthashes": posthashes,
            "formal_files_changed": changed,
            "formal_write_count": write_count,
            "action_results": results,
            "package_dispositions": {
                package_id: (
                    "needs_user"
                    if any(
                        result.get("result") == "needs_user"
                        and any(
                            ref.get("package_id") == package_id
                            for ref in result.get("evidence_refs", [])
                            if isinstance(ref, dict)
                        )
                        for result in results
                    )
                    else (
                        "completed"
                        if any(
                            result.get("result") in {"applied", "skipped"}
                            and any(
                                ref.get("package_id") == package_id
                                for ref in result.get("evidence_refs", [])
                                if isinstance(ref, dict)
                            )
                            for result in results
                        )
                        else "incomplete"
                    )
                )
                for package_id in manifest.get("package_ids", [])
            },
            "validation": {
                "status": "PASS" if not unresolved_count else "PARTIAL",
                "safe_action_count": safe_count,
                "unresolved_count": unresolved_count,
                "formal_schema": {"master_bank_columns": 13, "mastered_items_columns": 7, "sentence_pattern_fields": 15},
            },
            "archive_status": "pending" if apply else "not_started",
            "workflow_status": (
                "FORMAL_COMMITTED_SUPPORT_PENDING" if apply else status
            ),
            **(support_preflight_binding or {}),
        }
        atomic_write_json(receipt_path, receipt)
        if apply and fault_after_receipt:
            raise SimulatedWriterCrash(str(receipt_path))
        if apply:
            transaction["status"] = "closed"
            transaction["receipt_sha256"] = file_sha256(receipt_path)
            atomic_write_json(transaction_path, transaction)
            _append_journal(journal_path, receipt_id, "receipt_closed", {"receipt_path": str(receipt_path), "receipt_sha256": transaction["receipt_sha256"]})
    if apply:
        from .publication import complete_formal_closeout
        complete_formal_closeout(repo_root, state_dir, receipt, runner=publication_runner)
    return receipt_path, receipt


def _validate_recovery_commit_binding(
    state_dir: Path,
    transaction_path: Path,
    transaction: dict[str, Any],
    formal_paths: dict[str, Path],
    current_hashes: dict[str, str],
) -> tuple[Path, Path, dict[str, Any], Path]:
    required = {
        "receipt_id", "batch_id", "study_date", "journal_path", "formal_paths",
        "preimages", "staged", "prehashes", "proposed_hashes", "posthashes",
        "receipt_path",
    }
    if not required.issubset(transaction):
        raise ValidationError("recovery committed transaction binding is incomplete")
    receipt_id = str(transaction["receipt_id"])
    batch_id = str(transaction["batch_id"])
    study_date = str(transaction["study_date"])
    canonical_transaction_path = (
        state_dir
        / "nightly"
        / study_date
        / batch_id
        / "transactions"
        / receipt_id
        / "transaction.json"
    ).resolve()
    if transaction_path.resolve() != canonical_transaction_path:
        raise ValidationError("recovery transaction path is not canonical")
    canonical_journal_path = (
        state_dir / "nightly" / study_date / f"{batch_id}.journal.jsonl"
    ).resolve()
    if Path(str(transaction["journal_path"])).resolve() != canonical_journal_path:
        raise ValidationError("recovery transaction journal path is not canonical")
    canonical_receipt_path = (
        state_dir / "receipts" / "nightly" / study_date / f"{receipt_id}.json"
    ).resolve()
    if Path(str(transaction["receipt_path"])).resolve() != canonical_receipt_path:
        raise ValidationError("recovery transaction receipt path is not canonical")
    if not canonical_receipt_path.is_file():
        raise ValidationError("recovery committed receipt is missing")
    receipt = load_json(canonical_receipt_path)
    if (
        receipt.get("schema_version") != "english_apply_receipt_v1"
        or receipt.get("receipt_id") != receipt_id
        or receipt.get("batch_id") != batch_id
        or receipt.get("mode") != "apply"
        or receipt.get("status") not in {"APPLIED", "PARTIAL", "NO_ACTION"}
    ):
        raise ValidationError("recovery committed receipt identity or status mismatch")
    manifest_path = (
        state_dir / "nightly" / study_date / f"{batch_id}.manifest.json"
    ).resolve()
    if not manifest_path.is_file() or receipt.get("manifest_sha256") != file_sha256(manifest_path):
        raise ValidationError("recovery committed receipt manifest binding mismatch")
    manifest = load_json(manifest_path)
    if manifest.get("batch_id") != batch_id or manifest.get("study_date") != study_date:
        raise ValidationError("recovery committed manifest identity mismatch")
    if manifest.get("schema_version") == "english_nightly_manifest_v3":
        binding_fields = {
            "support_preflight_receipt_id",
            "support_preflight_receipt_path",
            "support_preflight_receipt_sha256",
            "support_proposal_sha256",
        }
        if any(
            transaction.get(field) != receipt.get(field)
            for field in binding_fields
        ):
            raise ValidationError(
                "recovery committed support preflight binding mismatch"
            )
        preflight_path = Path(
            str(receipt.get("support_preflight_receipt_path") or "")
        )
        if (
            preflight_path.is_symlink()
            or not preflight_path.is_file()
            or file_sha256(preflight_path)
            != receipt.get("support_preflight_receipt_sha256")
        ):
            raise ValidationError(
                "recovery committed support preflight receipt drift"
            )
        preflight = load_json(preflight_path)
        if (
            preflight.get("receipt_id")
            != receipt.get("support_preflight_receipt_id")
            or preflight.get("proposal_sha256")
            != receipt.get("support_proposal_sha256")
            or preflight.get("actions_sha256") != receipt.get("actions_sha256")
        ):
            raise ValidationError(
                "recovery committed support proposal binding mismatch"
            )
    expected_formal_paths = {name: str(value) for name, value in formal_paths.items()}
    if transaction.get("formal_paths") != expected_formal_paths:
        raise ValidationError("recovery committed formal path binding mismatch")
    prehashes = transaction["prehashes"]
    proposed = transaction["proposed_hashes"]
    if current_hashes != proposed or transaction.get("posthashes") != proposed:
        raise ValidationError("recovery committed formal posthash binding mismatch")
    if (
        receipt.get("formal_prehashes") != prehashes
        or receipt.get("proposed_posthashes") != proposed
        or receipt.get("formal_posthashes") != proposed
    ):
        raise ValidationError("recovery committed receipt formal hash binding mismatch")
    for name, digest in prehashes.items():
        preimage = Path(str(transaction["preimages"].get(name, "")))
        staged = Path(str(transaction["staged"].get(name, "")))
        if not preimage.is_file() or file_sha256(preimage) != digest:
            raise ValidationError(f"recovery committed preimage binding mismatch: {name}")
        if not staged.is_file() or file_sha256(staged) != proposed[name]:
            raise ValidationError(f"recovery committed staged binding mismatch: {name}")
    validate_receipt_resolved_evidence(receipt, manifest)
    records = _validated_journal_records(canonical_journal_path)
    receipt_events = [
        record.get("event_type")
        for record in records
        if record.get("receipt_id") == receipt_id
    ]
    for event_type in (
        "started", "validated", "transaction_prepared", "committed_pending_receipt"
    ):
        if event_type not in receipt_events:
            raise ValidationError(f"recovery committed journal missing {event_type}")
    close_records = [
        record
        for record in records
        if record.get("receipt_id") == receipt_id
        and record.get("event_type") == "receipt_closed"
    ]
    if len(close_records) > 1:
        raise ValidationError("recovery committed journal has duplicate receipt_closed")
    if close_records:
        data = close_records[0].get("data", {})
        if (
            data.get("receipt_path") != str(canonical_receipt_path)
            or data.get("receipt_sha256") != file_sha256(canonical_receipt_path)
        ):
            raise ValidationError("recovery committed receipt_closed binding mismatch")
    return canonical_receipt_path, canonical_journal_path, receipt, manifest_path


def recover_nightly(
    state_dir: Path,
    repo_root: Path,
    *,
    transaction_path: Path | None = None,
    publication_runner: Any = None,
    archive_contract: Any = None,
    retry_publication: bool = False,
) -> dict[str, Any]:
    state_dir = state_dir.resolve()
    repo_root = repo_root.resolve()
    candidates = (
        [transaction_path.resolve()]
        if transaction_path is not None
        else sorted((state_dir / "nightly").glob("*/*/transactions/*/transaction.json"))
    )
    lock_path = state_dir / "locks" / "formal.lock"
    base_formal_paths = resolve_formal_paths(repo_root)
    base_formal_paths["review_exclusion_ledger"] = (
        repo_root / "bank" / "review_exclusion_ledger.jsonl"
    ).resolve()
    recovered: list[dict[str, Any]] = []
    closed_receipts: list[tuple[dict[str, Any], Path]] = []
    with exclusive_lock(lock_path):
        for path in candidates:
            transaction = load_json(path)
            formal_paths = dict(base_formal_paths)
            if "learning_events" in transaction.get("formal_paths", {}):
                formal_paths["learning_events"] = learning_events_path(repo_root).resolve()
            if transaction.get("status") not in OPEN_TRANSACTION_STATUSES:
                if transaction.get("status") == "closed":
                    try:
                        from .packages import frozen_package_overrides
                        if (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(transaction.get("study_date", "")))
                                or not re.fullmatch(r"EN-BATCH-\d{8}-[0-9A-F]{12}", str(transaction.get("batch_id", "")))
                                or not re.fullmatch(r"EN-RECEIPT-[0-9A-F]{16}", str(transaction.get("receipt_id", "")))
                                or transaction.get("formal_paths") != {name: str(value) for name, value in formal_paths.items()}):
                            raise ValidationError("closed transaction identity/path binding is invalid")
                        canonical_receipt = state_dir / "receipts/nightly" / transaction["study_date"] / f"{transaction['receipt_id']}.json"
                        manifest_path = state_dir / "nightly" / transaction["study_date"] / f"{transaction['batch_id']}.manifest.json"
                        if transaction.get("receipt_sha256") != file_sha256(canonical_receipt):
                            raise ValidationError("closed transaction receipt hash drift")
                        manifest = load_json(manifest_path)
                        overrides = frozen_package_overrides(state_dir, repo_root, manifest, archive_contract=archive_contract)
                        verified, _ = validate_canonical_writer_closeout(state_dir, canonical_receipt,
                            expected_manifest_path=manifest_path, package_root_overrides=overrides)
                        closed_receipts.append((verified, canonical_receipt))
                    except (OSError, ValueError, ValidationError) as exc:
                        recovered.append({"transaction": str(path), "status": "postformal_pending", "reason": type(exc).__name__})
                        continue
                recovered.append({"transaction": str(path), "status": "skipped", "reason": transaction.get("status")})
                continue
            if transaction.get("formal_paths") != {name: str(value) for name, value in formal_paths.items()}:
                recovered.append({"transaction": str(path), "status": "failed", "reason": "formal path mismatch"})
                continue
            current = {
                name: _optional_file_sha256(value)
                for name, value in formal_paths.items()
            }
            prehashes = transaction["prehashes"]
            proposed = transaction["proposed_hashes"]
            unknown = [name for name, digest in current.items() if digest not in {prehashes[name], proposed[name]}]
            journal_path = Path(transaction["journal_path"])
            if unknown:
                recovered.append({"transaction": str(path), "status": "failed", "reason": f"unknown external drift: {unknown}"})
                _append_journal(journal_path, transaction["receipt_id"], "recovery_blocked", {"unknown": unknown})
                continue
            receipt_path = Path(transaction.get("receipt_path", ""))
            if receipt_path.is_file() and transaction.get("status") in {"committed", "committed_pending_receipt"}:
                canonical_receipt, canonical_journal, _, manifest_path = (
                    _validate_recovery_commit_binding(
                        state_dir,
                        path,
                        transaction,
                        formal_paths,
                        current,
                    )
                )
                close_count = sum(
                    1
                    for record in _validated_journal_records(canonical_journal)
                    if record.get("receipt_id") == transaction["receipt_id"]
                    and record.get("event_type") == "receipt_closed"
                )
                if close_count == 0:
                    _append_journal(
                        canonical_journal,
                        transaction["receipt_id"],
                        "receipt_closed",
                        {
                            "receipt_path": str(canonical_receipt),
                            "receipt_sha256": file_sha256(canonical_receipt),
                        },
                    )
                validate_canonical_writer_closeout(
                    state_dir,
                    canonical_receipt,
                    expected_manifest_path=manifest_path,
                )
                transaction["status"] = "closed"
                transaction["receipt_sha256"] = file_sha256(canonical_receipt)
                atomic_write_json(path, transaction)
                recovered.append({"transaction": str(path), "status": "RECOVERED_COMMIT_WITH_RECEIPT"})
                closed_receipts.append((load_json(canonical_receipt), canonical_receipt))
                continue
            for name, formal_path in formal_paths.items():
                preimage = Path(transaction["preimages"][name])
                if not preimage.is_file() or file_sha256(preimage) != prehashes[name]:
                    raise ValidationError(f"recovery preimage missing or corrupt: {preimage}")
                atomic_write_bytes(formal_path, preimage.read_bytes())
            restored = {
                name: _optional_file_sha256(value)
                for name, value in formal_paths.items()
            }
            if restored != prehashes:
                raise ValidationError("recovery rollback hashes do not match prehashes")
            transaction["status"] = "recovered_rollback"
            transaction["posthashes"] = restored
            atomic_write_json(path, transaction)
            _append_journal(journal_path, transaction["receipt_id"], "recovered_rollback", {"posthashes": restored})
            recovered.append({"transaction": str(path), "status": "RECOVERED_ROLLBACK"})
    from .publication import complete_formal_closeout
    from .archive import retry_archived_publication
    publication_results = []
    for receipt, receipt_path in closed_receipts:
        final = retry_archived_publication(state_dir, repo_root, receipt, writer_receipt_path=receipt_path,
            publication_runner=publication_runner, retry_publication=retry_publication)
        publication_results.append(final["publication"] if final is not None else
            complete_formal_closeout(repo_root, state_dir, receipt, runner=publication_runner,
                                    retry_publication=retry_publication))
    publication_pending = any(row.get("status") not in {"PUBLISHED", "NOT_PRODUCTION_REPO"}
                              for row in publication_results)
    recovery_receipt = {
        "schema_version": "english_recovery_receipt_v1",
        "status": "PASS" if not publication_pending and not any(row["status"] in {"failed", "postformal_pending"} for row in recovered) else "PARTIAL",
        "recovered": recovered,
        "formal_write_count": 0,
        "publication": publication_results,
    }
    receipt_id = "EN-RECOVERY-" + object_sha256({"time_ns": time.time_ns(), "recovered": recovered})[:16].upper()
    receipt_day = next((load_json(path).get("study_date") for path in candidates if path.is_file()), "unknown-date")
    receipt_path = state_dir / "receipts" / "recovery" / str(receipt_day) / f"{receipt_id}.json"
    recovery_receipt["receipt_id"] = receipt_id
    recovery_receipt["receipt_path"] = str(receipt_path)
    atomic_write_json(receipt_path, recovery_receipt)
    return recovery_receipt
