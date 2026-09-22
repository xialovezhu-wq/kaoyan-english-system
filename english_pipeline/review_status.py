from __future__ import annotations

from datetime import datetime, timezone

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ValidationError
from .util import normalize_item, object_sha256, parse_iso_date


REVIEW_STATUS_LEDGER_VERSION = "english_review_status_ledger_v1"
REVIEW_POLICY_VERSION = "english_sentence_nonreport_policy_v2"
WORD_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]+(?:['’\-][A-Za-z]+)*)(?![A-Za-z])")


def canonical_machine_decision(value: str) -> str:
    """Translate view labels at the boundary; storage only sees machine enums."""

    normalized = str(value).strip()
    mapping = {
        "长期库候选": "long_term_candidate",
        "不背单词候选": "bbdc_candidate",
        "仅文章": "article_only",
        "不建议": "not_recommended",
    }
    return mapping.get(normalized, normalized)


def token_spans(sentence: str) -> list[dict[str, Any]]:
    return [
        {
            "surface_form": match.group(1),
            "normalized": normalize_item(match.group(1)),
            "start": match.start(1),
            "end": match.end(1),
        }
        for match in WORD_RE.finditer(sentence)
    ]


def _controlled_forms(item: str, aliases: Mapping[str, Iterable[str]]) -> set[str]:
    canonical = normalize_item(item)
    forms = {canonical}
    for alias in aliases.get(canonical, []):
        normalized = normalize_item(str(alias))
        if normalized:
            forms.add(normalized)
    return forms


def exact_item_occurrences(
    sentence: str,
    item: str,
    *,
    aliases: Mapping[str, Iterable[str]] | None = None,
) -> list[dict[str, Any]]:
    """Return exact token/phrase spans; never accept substring containment."""

    aliases = aliases or {}
    forms = _controlled_forms(item, aliases)
    spans = token_spans(sentence)
    occurrences: list[dict[str, Any]] = []
    for length in sorted({len(form.split()) for form in forms}):
        for start_index in range(0, len(spans) - length + 1):
            window = spans[start_index : start_index + length]
            normalized = " ".join(row["normalized"] for row in window)
            if normalized not in forms:
                continue
            occurrences.append(
                {
                    "surface_form": sentence[window[0]["start"] : window[-1]["end"]],
                    "normalized": normalized,
                    "start": window[0]["start"],
                    "end": window[-1]["end"],
                }
            )
    return sorted(
        {object_sha256(row): row for row in occurrences}.values(),
        key=lambda row: (row["start"], row["end"], row["normalized"]),
    )


def _explicit_unknown_items(event: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for signal in event.get("observed_signals", []):
        if not isinstance(signal, Mapping):
            continue
        if signal.get("signal_type") != "vocabulary":
            continue
        if signal.get("knowledge_state") not in {"unknown", "mistranslated"}:
            continue
        value = normalize_item(str(signal.get("canonical_term", "")))
        if value:
            result.add(value)
    for candidate in event.get("candidates", []):
        if not isinstance(candidate, Mapping):
            continue
        if candidate.get("candidate_type") not in {"单词", "词组", "熟词僻义"}:
            continue
        value = normalize_item(str(candidate.get("item", "")))
        if value:
            result.add(value)
    return result


def _event_sentence(event: Mapping[str, Any]) -> str:
    source = event.get("source")
    return str(source.get("source_sentence", "")) if isinstance(source, Mapping) else ""


def effective_review_status(records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Project the append-only ledger without rewriting its history."""

    projected: dict[str, dict[str, Any]] = {}
    previous = "0" * 64
    for expected_sequence, raw in enumerate(records, start=1):
        record = dict(raw)
        required = {
            "schema_version",
            "sequence",
            "event_id",
            "event_type",
            "item",
            "bank_id",
            "study_date",
            "source_capture_event_ids",
            "sentence_evidence",
            "reason",
            "previous_sha256",
            "record_sha256",
        }
        if set(record) - required - {"observed_at"} or not required.issubset(record):
            raise ValidationError("review status ledger fields do not match schema")
        if record["schema_version"] != REVIEW_STATUS_LEDGER_VERSION:
            raise ValidationError("review status ledger schema mismatch")
        if record["sequence"] != expected_sequence or record["previous_sha256"] != previous:
            raise ValidationError("review status ledger chain break")
        core = {key: value for key, value in record.items() if key != "record_sha256"}
        if object_sha256(core) != record["record_sha256"]:
            raise ValidationError("review status ledger hash mismatch")
        if record["event_type"] not in {"exclude_from_review", "reactivate_for_review", "independent_mastery_confirmed"}:
            raise ValidationError("review status ledger event_type is invalid")
        normalized = normalize_item(str(record["item"]))
        if not normalized:
            raise ValidationError("review status ledger item is empty")
        if normalized in projected and str(record["study_date"]) < str(projected[normalized]["study_date"]):
            previous = record["record_sha256"]
            continue
        observed = record.get("observed_at")
        if observed is not None and parse_iso_date(str(observed)) != record["study_date"]:
            raise ValidationError("review status observation date differs from its learning day")
        parsed = datetime.fromisoformat(str(observed).replace("Z", "+00:00")) if observed else None
        if parsed is not None and parsed.tzinfo is None:
            raise ValidationError("precise review observation timestamps require a timezone")
        prior_observed = projected.get(normalized, {}).get("observed_at")
        if observed and prior_observed:
            prior = datetime.fromisoformat(str(prior_observed).replace("Z", "+00:00"))
            if parsed.tzinfo is None or prior.tzinfo is None:
                raise ValidationError("precise review observation timestamps require a timezone")
            if parsed.astimezone(timezone.utc) < prior.astimezone(timezone.utc):
                previous = record["record_sha256"]
                continue
        projected[normalized] = {
            "status": (
                "mastered_sentence_nonreport"
                if record["event_type"] == "exclude_from_review"
                else "independent_correct_use"
                if record["event_type"] == "independent_mastery_confirmed"
                else "unmastered_reactivated"
            ),
            "event_id": record["event_id"],
            "bank_id": record["bank_id"],
            "study_date": record["study_date"],
            "reason": record["reason"],
            **({"observed_at": observed} if observed is not None else {}),
        }
        previous = record["record_sha256"]
    return projected


def load_review_status_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid review status ledger JSON at line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValidationError(f"review status ledger line {line_number} is not an object")
        records.append(value)
    effective_review_status(records)
    return records


def build_review_status_proposals(
    events: Iterable[Mapping[str, Any]],
    bank_rows: Iterable[Mapping[str, Any]],
    *,
    study_date: str,
    current_status: Mapping[str, Mapping[str, Any]] | None = None,
    mastered_items: Iterable[Mapping[str, Any]] = (),
    aliases: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Aggregate the whole day before interpreting a later sentence non-report."""

    current_status = current_status or {}
    aliases = aliases or {}
    bank_rows = [dict(row) for row in bank_rows]
    mastered_items = [dict(row) for row in mastered_items]
    day_events = sorted(
        [
            dict(event)
            for event in events
            if event.get("event_type") in {"sentence_captured", "sentence_correction"}
            and parse_iso_date(str(event.get("occurred_at"))) == study_date
        ],
        key=lambda event: (str(event.get("occurred_at", "")), str(event.get("event_id", ""))),
    )
    explicit_by_event = {
        str(event.get("event_id")): _explicit_unknown_items(event)
        for event in day_events
    }
    day_explicit_unknown = set().union(*explicit_by_event.values()) if explicit_by_event else set()
    mastered_norms = {
        normalize_item(str(row.get("item", "")))
        for row in mastered_items
        if normalize_item(str(row.get("item", "")))
    }
    exclusions: list[dict[str, Any]] = []
    reactivations: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    bank_norm_counts: dict[str, int] = defaultdict(int)
    for row in bank_rows:
        normalized = normalize_item(str(row.get("item", "")))
        if normalized:
            bank_norm_counts[normalized] += 1

    for bank_row in bank_rows:
        item = str(bank_row.get("item", "")).strip()
        bank_id = str(bank_row.get("id", "")).strip()
        normalized = normalize_item(item)
        if not normalized:
            continue
        appearances: list[dict[str, Any]] = []
        for event in day_events:
            sentence = _event_sentence(event)
            matches = exact_item_occurrences(sentence, item, aliases=aliases)
            if matches:
                appearances.append(
                    {
                        "capture_event_id": event.get("event_id"),
                        "sentence_hash": event.get("source", {}).get("sentence_hash"),
                        "spans": matches,
                        "explicit_unknown": normalized in explicit_by_event[str(event.get("event_id"))],
                        "user_evidence_verbatim": event.get("learning", {}).get("user_evidence_verbatim"),
                    }
                )
        if not appearances:
            continue
        if bank_norm_counts[normalized] > 1:
            conflicts.append(
                {
                    "proposal_type": "needs_user_decision",
                    "item": item,
                    "normalized": normalized,
                    "bank_id": bank_id,
                    "reason": "homograph_or_duplicate_bank_identity",
                    "source_capture_event_ids": [
                        str(row["capture_event_id"]) for row in appearances
                    ],
                    "sentence_evidence": appearances,
                }
            )
            continue

        prior = current_status.get(normalized, {})
        previously_excluded = prior.get("status") == "mastered_sentence_nonreport"
        independently_mastered = normalized in mastered_norms
        if normalized in day_explicit_unknown:
            if previously_excluded or independently_mastered:
                reactivations.append(
                    {
                        "proposal_type": "reactivation_proposal",
                        "item": item,
                        "normalized": normalized,
                        "bank_id": bank_id,
                        "reason": "explicit_unknown_or_mistranslated_after_mastery",
                        "source_capture_event_ids": [
                            row["capture_event_id"]
                            for row in appearances
                            if row["explicit_unknown"]
                        ],
                        "sentence_evidence": [row for row in appearances if row["explicit_unknown"]],
                        "preserve_mastered_items_history": independently_mastered,
                    }
                )
            continue

        nonreport = [row for row in appearances if not row["explicit_unknown"]]
        if not nonreport or previously_excluded:
            continue
        source_ids = [str(row["capture_event_id"]) for row in nonreport]
        exclusions.append(
            {
                "proposal_type": "review_exclusion_proposal",
                "item": item,
                "normalized": normalized,
                "bank_id": bank_id,
                "reason": "appeared_in_sentence_but_not_reported_unknown",
                "source_capture_event_ids": source_ids,
                "sentence_evidence": nonreport,
                "day_conflict_check": {
                    "study_date": study_date,
                    "explicit_unknown_seen_anywhere_in_day": False,
                    "event_count_scanned": len(day_events),
                    "policy_version": REVIEW_POLICY_VERSION,
                },
            }
        )

    return {
        "schema_version": "english_review_status_proposals_v2",
        "study_date": study_date,
        "policy_version": REVIEW_POLICY_VERSION,
        "capture_event_ids": [str(event.get("event_id")) for event in day_events],
        "daily_explicit_unknown_terms": sorted(day_explicit_unknown),
        "review_exclusion_proposals": sorted(exclusions, key=lambda row: (row["normalized"], row["bank_id"])),
        "reactivation_proposals": sorted(reactivations, key=lambda row: (row["normalized"], row["bank_id"])),
        "needs_user_decision": conflicts,
    }


def append_review_status_record(
    records: list[dict[str, Any]],
    *,
    event_type: str,
    item: str,
    bank_id: str,
    study_date: str,
    source_capture_event_ids: list[str],
    sentence_evidence: list[dict[str, Any]],
    reason: str,
    observed_at: str | None = None,
) -> dict[str, Any]:
    effective_review_status(records)
    if event_type not in {"exclude_from_review", "reactivate_for_review", "independent_mastery_confirmed"}:
        raise ValidationError("review status event_type is invalid")
    previous = records[-1]["record_sha256"] if records else "0" * 64
    core = {
        "schema_version": REVIEW_STATUS_LEDGER_VERSION,
        "sequence": len(records) + 1,
        "event_id": "EN-REVIEW-" + object_sha256(
            {
                "event_type": event_type,
                "item": normalize_item(item),
                "bank_id": bank_id,
                "study_date": study_date,
                "source_capture_event_ids": source_capture_event_ids,
                "previous": previous,
            }
        )[:20].upper(),
        "event_type": event_type,
        "item": item,
        "bank_id": bank_id,
        "study_date": study_date,
        "source_capture_event_ids": list(source_capture_event_ids),
        "sentence_evidence": sentence_evidence,
        "reason": reason,
        "previous_sha256": previous,
        **({"observed_at": observed_at} if observed_at is not None else {}),
    }
    record = {**core, "record_sha256": object_sha256(core)}
    return record


def eligible_review_bank_rows(
    bank_rows: Iterable[Mapping[str, Any]],
    ledger_records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    projected = effective_review_status(ledger_records)
    return [
        dict(row)
        for row in bank_rows
        if projected.get(normalize_item(str(row.get("item", ""))), {}).get("status")
        not in {"mastered_sentence_nonreport", "independent_correct_use"}
    ]


def effective_mastered_norms(
    mastered_rows: Iterable[Mapping[str, Any]], ledger_records: Iterable[Mapping[str, Any]]
) -> set[str]:
    """Keep old proof, but let the append-only current review state supersede it."""
    rows = list(mastered_rows)
    mastered = {normalize_item(str(row.get("item", ""))) for row in rows
                if normalize_item(str(row.get("item", "")))}
    proof_dates: dict[str, str] = {}
    for row in rows:
        key = normalize_item(str(row.get("item", "")))
        proof_dates[key] = max(proof_dates.get(key, ""), str(row.get("mastered_date", "")))
    for item, status in effective_review_status(ledger_records).items():
        if status["status"] == "unmastered_reactivated":
            if str(status["study_date"]) >= proof_dates.get(item, ""):
                mastered.discard(item)
        elif status["status"] == "independent_correct_use":
            mastered.add(item)
    return mastered


def effective_mastered_ids(
    mastered_rows: Iterable[Mapping[str, Any]], ledger_records: Iterable[Mapping[str, Any]]
) -> set[str]:
    """Preserve exact-ID proof matching while honoring later reactivation."""
    rows = list(mastered_rows)
    identities = {str(row.get("matched_id", "")).strip() for row in rows if str(row.get("matched_id", "")).strip()}
    dates: dict[str, str] = {}
    for row in rows:
        key = str(row.get("matched_id", "")).strip()
        dates[key] = max(dates.get(key, ""), str(row.get("mastered_date", "")))
    for status in effective_review_status(ledger_records).values():
        key = str(status.get("bank_id", "")).strip()
        if not key:
            continue
        if status["status"] == "unmastered_reactivated" and str(status["study_date"]) >= dates.get(key, ""):
            identities.discard(key)
        elif status["status"] == "independent_correct_use":
            identities.add(key)
    return identities


def serialize_review_status_ledger(records: Iterable[Mapping[str, Any]]) -> bytes:
    rows = [dict(record) for record in records]
    effective_review_status(rows)
    if not rows:
        return b""
    return (
        "\n".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for row in rows
        )
        + "\n"
    ).encode("utf-8")
