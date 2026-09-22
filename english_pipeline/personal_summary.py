"""Point-hash routing to precomputed personal notes; no runtime history scans."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from .errors import IdempotencyConflict, ValidationError
from .util import atomic_write_json, canonical_bytes, exclusive_lock, file_sha256, load_json, object_sha256

SCHEMA = "english_point_personal_summary_v1"
INDEX = "english_point_personal_summary_index_v1"
PREFIXES = {"word", "phrase", "syntax", "option_type", "error_type"}


def normalize_key(value: str) -> str:
    value = " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())
    if ":" not in value or value.split(":", 1)[0] not in PREFIXES or not value.split(":", 1)[1]:
        raise ValidationError("point key must be word:, phrase:, syntax:, option_type: or error_type:")
    return value


def key_hash(value: str) -> str:
    return object_sha256(normalize_key(value))


def publish_personal_summaries(repo: Path, state: Path, document: dict[str, Any], *,
                                formal_receipt: dict[str, Any] | None = None,
                                bootstrap_existing_formal: bool = False) -> dict[str, Any]:
    """Publish after verified formal intake, or explicitly bootstrap formal history.

    Local curation supplies semantics. Raw Capture never calls this function.
    Merge episodes by identity, retaining earlier contributions from other parts.
    """
    if not bootstrap_existing_formal and (not formal_receipt or formal_receipt.get("status") != "PASS"):
        raise ValidationError("personal summaries update only after verified formal intake")
    if document.get("schema_version") != SCHEMA or not document.get("records"):
        raise ValidationError("point summary input must contain explicit records")
    root = state / "personal-summary"
    root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(root / ".publish.lock"):
        index_path = root / "current.json"
        index = load_json(index_path) if index_path.exists() else {"schema_version": INDEX, "routes": {}}
        if index.get("schema_version") != INDEX:
            raise ValidationError("personal summary index version mismatch")
        routes = dict(index["routes"])
        updated = []
        for supplied in document["records"]:
            record = dict(supplied)
            canonical = normalize_key(record["key"])
            record["key"] = canonical
            if record.get("visibility") not in {"answer_free", "protected"}:
                raise ValidationError("summary requires explicit answer visibility")
            if not record.get("teaching_hint") or not record.get("episodes"):
                raise ValidationError("summary needs a teaching hint and actual observations")
            pointer = routes.get(key_hash(canonical))
            old = _read_record(root, pointer) if pointer else None
            if old and old["key"] != canonical:
                raise ValidationError("canonical key collides with an existing alias")
            episodes = {e["episode_id"]: e for e in old["episodes"]} if old else {}
            for episode in record["episodes"]:
                for field in ("episode_id", "first_attempt", "correction", "later_observation", "evidence"):
                    if not episode.get(field):
                        raise ValidationError(f"summary episode requires {field}; explicitly mark unobserved results")
                for ref in episode["evidence"]:
                    target = (repo / ref["path"]).resolve(strict=True)
                    relative = target.relative_to(repo.resolve())
                    if bootstrap_existing_formal and relative.parts[0] not in {"articles", "bank"}:
                        raise ValidationError("bootstrap reads existing formal article/bank records only")
                    if file_sha256(target) != ref["sha256"]:
                        raise ValidationError("summary evidence changed")
                eid = episode["episode_id"]
                if eid in episodes and episodes[eid] != episode:
                    raise IdempotencyConflict("existing observation cannot be silently rewritten")
                episodes[eid] = episode
            record["episodes"] = list(episodes.values())
            aliases = set(old.get("aliases", []) if old else []) | set(record.get("aliases", []))
            record["aliases"] = sorted({normalize_key(a) for a in aliases} - {canonical})
            if old and old.get("visibility") == "protected":
                record["visibility"] = "protected"
            record["schema_version"] = SCHEMA
            record["publication_basis"] = ("existing_formal_bootstrap" if bootstrap_existing_formal
                                           else formal_receipt["receipt_id"])
            data = canonical_bytes(record)
            if len(data) > 12 * 1024:
                raise ValidationError("point summary exceeds 12 KiB; compact its teaching summary during formal curation")
            digest = object_sha256(record)
            target = root / "objects" / f"{digest}.json"
            target.parent.mkdir(exist_ok=True)
            if target.exists() and target.read_bytes() != data:
                raise ValidationError("summary object hash collision or corruption")
            if not target.exists():
                target.write_bytes(data)
            for alias in [canonical, *record["aliases"]]:
                hashed = key_hash(alias)
                previous = routes.get(hashed)
                if previous and previous["key"] != canonical:
                    raise ValidationError(f"ambiguous point alias: {alias}")
                routes[hashed] = {"key": canonical, "sha256": digest}
            updated.append(canonical)
        atomic_write_json(index_path, {"schema_version": INDEX, "routes": routes})
    return {"status": "published", "updated_keys": updated, "route_count": len(routes),
            "index_path": str(index_path), "formal_write_count": 0}


def _read_record(root: Path, pointer: dict[str, Any]) -> dict[str, Any]:
    digest = pointer["sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValidationError("invalid summary object identity")
    path = root / "objects" / f"{digest}.json"
    if path.stat().st_size > 12 * 1024:
        raise ValidationError("point summary exceeds read budget")
    record = load_json(path)
    if object_sha256(record) != digest or record["key"] != pointer["key"] or record.get("schema_version") != SCHEMA:
        raise ValidationError("summary object binding changed")
    return record


def _fit_query_budget(result: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Keep complete teaching hints, then the newest whole observations that fit.

    This changes the response only; immutable published records retain every
    observation. A large point must not hide other independently useful hits.
    """
    if len(canonical_bytes(result)) <= max_bytes:
        return result
    compact = {**result, "status": "partial", "matched": [], "misses": [],
               "unavailable": [], "compacted": True,
               "diagnostics_omitted": (len(result["matched"]) + len(result["misses"])
                                       + len(result["unavailable"]))}
    pending = []
    budget_unavailable = []
    for matched in result["matched"]:
        count = len(matched["episodes"])
        point = {**matched, "episodes": [], "episodes_total": count,
                 "episodes_returned": 0, "episodes_omitted": count}
        compact["matched"].append(point)
        compact["diagnostics_omitted"] -= 1
        if len(canonical_bytes(compact)) <= max_bytes:
            # Dates order observations across independently published parts;
            # insertion order breaks same-date ties and handles legacy dates.
            episodes = sorted(enumerate(matched["episodes"]),
                              key=lambda item: (str(item[1].get("study_date", "")), item[0]),
                              reverse=True)
            pending.append((point, [episode for _, episode in episodes]))
        else:
            compact["matched"].pop()
            compact["diagnostics_omitted"] += 1
            budget_unavailable.append({"key": matched["key"], "reason": "response_budget"})

    # Whole diagnostics count against the same UTF-8 budget. Even unusually
    # long input keys cannot make the fallback response exceed that budget.
    for field, items in (("unavailable", result["unavailable"] + budget_unavailable),
                         ("misses", result["misses"])):
        for item in items:
            compact[field].append(item)
            compact["diagnostics_omitted"] -= 1
            if len(canonical_bytes(compact)) > max_bytes:
                compact[field].pop()
                compact["diagnostics_omitted"] += 1

    # Give each retained point a chance to include its latest observation before
    # adding older ones. Never return an older episode after its newer one failed
    # to fit: that could hide a later correction or an unresolved recurrence.
    while pending:
        next_pending = []
        for point, episodes in pending:
            if not episodes:
                continue
            point["episodes"].append(episodes[0])
            point["episodes_returned"] += 1
            point["episodes_omitted"] -= 1
            if len(canonical_bytes(compact)) <= max_bytes:
                next_pending.append((point, episodes[1:]))
            else:
                point["episodes"].pop()
                point["episodes_returned"] -= 1
                point["episodes_omitted"] += 1
        pending = next_pending
    return compact


def query_personal_summary(state: Path, *, keys: list[str], question_review: bool = False,
                           max_bytes: int = 8192) -> dict[str, Any]:
    keys = list(dict.fromkeys(normalize_key(key) for key in keys))
    if not 1 <= len(keys) <= 8 or not 512 <= max_bytes <= 16384:
        raise ValidationError("query supports 1..8 point keys and a 512..16384 byte response")
    root = state / "personal-summary"
    path = root / "current.json"
    result = {"status": "ready", "matched": [], "misses": [], "unavailable": [],
              "formal_write_count": 0, "history_scanned": False}
    if not path.exists():
        return _fit_query_budget({**result, "misses": keys}, max_bytes)
    index = load_json(path)
    if index.get("schema_version") != INDEX:
        raise ValidationError("personal summary index version mismatch")
    seen = set()
    for key in keys:
        pointer = index["routes"].get(key_hash(key))
        if pointer is None:
            result["misses"].append(key)
            continue
        if pointer["key"] in seen:
            continue
        seen.add(pointer["key"])
        try:
            record = _read_record(root, pointer)
            if record["visibility"] != "answer_free" and not question_review:
                result["unavailable"].append({"key": key, "reason": "protected"})
                continue
            # Evidence is verified at formal publication, never reread in tutoring.
            result["matched"].append({"key": record["key"], "summary_sha256": pointer["sha256"],
                                      "teaching_hint": record["teaching_hint"],
                                      "episodes": [{k: v for k, v in e.items() if k != "evidence"}
                                                   for e in record["episodes"]]})
        except (OSError, ValueError, KeyError, TypeError, ValidationError):
            result["unavailable"].append({"key": key, "reason": "invalid_or_missing_summary"})
    return _fit_query_budget(result, max_bytes)
