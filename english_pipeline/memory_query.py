"""Bounded, read-only context assembly over the English memory projection."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from .answer_safety import contains_answer_signal
from .errors import ValidationError
from .memory_store import (is_answer_safe, open_memory_projection,
                           read_lifecycle, record_text, search_tokens)
from .personal_summary import (INDEX, OBSERVATION_ORDER_AMBIGUOUS, _read_record, ambiguous_observation_order,
                               key_hash, normalize_key, select_observations)
from .util import LOCAL_ZONE, canonical_bytes, file_sha256, load_json, object_sha256

QUERY_SCHEMA = "english_memory_context_v1"
_PREFERENCE_ID = "teaching-preferences"
_PUBLICATION_ID = re.compile(r"EN-MEMORY-[A-F0-9]{24}")
_QUESTION_IN_UNIT = re.compile(r"(?<![A-Z0-9])Q\d+(?![A-Z0-9])", re.I)


def _question_scope_matches(question_id: str, questions: list[str], units: list[str]) -> bool:
    # Older full-review packages use their first question as the file-level
    # label for a range of questions. That label establishes article scope only.
    return set(questions) == {question_id} and all(
        len(set(_QUESTION_IN_UNIT.findall(unit.upper()))) <= 1 for unit in units)


def _time(value: str, *, end_of_day: bool = False) -> datetime:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = datetime.combine(datetime.fromisoformat(value).date(), time.max if end_of_day else time.min)
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_ZONE)
    return parsed.astimezone(timezone.utc)


def _scoped(episode: dict[str, Any], source_id: str | None, unit_id: str | None,
            question_id: str | None = None) -> bool:
    if not (source_id or unit_id or question_id):
        return True
    bindings = episode.get("bindings", [])
    if bindings:
        # One observation can cite multiple contexts. Its prose cannot be
        # mechanically split: every context must be within the requested scope.
        def matches(binding):
            return ((not source_id or source_id in binding.get("source_ids", []))
                    and (not unit_id or unit_id in binding.get("unit_ids", []) or unit_id in binding.get("question_ids", []))
                    and (not question_id or _question_scope_matches(
                        question_id, binding.get("question_ids", []), binding.get("unit_ids", []))))
        return all(matches(binding) for binding in bindings)
    if source_id and (unit_id or question_id):
        return False  # A union of IDs is not proof of a source/unit pair.
    if source_id and source_id not in episode["source_ids"]:
        return False
    if unit_id and unit_id not in episode["unit_ids"] and unit_id not in episode["question_ids"]:
        return False
    if question_id and not _question_scope_matches(question_id, episode["question_ids"], episode["unit_ids"]):
        return False
    if source_id and len(episode["source_ids"]) > 1:
        return False
    if unit_id and len(set(episode["unit_ids"]) | set(episode["question_ids"])) > 1:
        return False
    return True


def _episode_scope(row: sqlite3.Row) -> dict[str, Any]:
    return {"source_ids": json.loads(row["source_ids_json"]), "unit_ids": json.loads(row["unit_ids_json"]),
            "question_ids": json.loads(row["question_ids_json"]), "bindings": json.loads(row["scope_bindings_json"])}


def _article_only(episode: dict[str, Any], source_id: str, container_id: str) -> bool:
    bindings = episode["bindings"]
    return bool(not episode["question_ids"] and set(episode["unit_ids"]) <= {container_id, source_id}
                and bindings and all(
        source_id in binding["source_ids"] and not binding["question_ids"]
        and set(binding["unit_ids"]) <= {container_id, source_id} for binding in bindings))


def _lexical_article(episode: dict[str, Any], source_id: str, container_id: str) -> tuple[str, str] | None:
    """Match one unambiguous article across all verified bindings, never a question or an unscoped note."""
    bindings = episode["bindings"]
    if not bindings or episode["question_ids"]:
        return None
    families = (r"EXAM-READING-\d{4}-T\d+", r"RAW-ARTICLE-[\w-]+")
    for pattern in families:
        current = next((s for s in (source_id, container_id) if re.fullmatch(pattern, s)), None)
        if not current:
            continue
        articles = set()
        for binding in bindings:
            sources = set(binding["source_ids"])
            ids = {s for s in sources if re.fullmatch(pattern, s)}
            if len(ids) != 1:
                break
            if (binding["question_ids"] or not binding["unit_ids"] or
                    any(not (re.fullmatch(r"S\d+", unit) or
                             (unit in sources and any(re.fullmatch(p, unit) for p in families)))
                        for unit in binding["unit_ids"])):
                return None
            articles.update(ids)
        else:
            if len(articles) != 1:
                return None
            prior = articles.pop()
            return ("same_article" if prior == current else "other_article", prior)
    return None


def _diagram_point(point: dict[str, Any], scope: str) -> dict[str, Any]:
    result = {key: point[key] for key in ("key", "summary_sha256", "visibility", "retrieval_method",
                                         "latest_observation") if key in point}
    result.update(scope_match=scope, audit={"status": point["audit"]["status"]})
    for field in ("claims", "contrasts", "teaching_hint", "teaching_hint_status"):
        if point.get(field):
            result[field] = point[field]
    result["sources"] = [{key: value for key, value in ref.items() if key != "source_key"}
                         for ref in point["sources"]]
    return result


def _episode_audits(db: sqlite3.Connection, key: str, identity: str) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT s.audit_json FROM sources s JOIN dependencies d ON d.source_key=s.source_key "
        "WHERE d.point_key=? AND d.episode_id=? ORDER BY s.source_key", (key, identity))]


def _episode_public(episode: dict[str, Any], cutoff: datetime | None = None) -> dict[str, Any]:
    allowed = ("episode_id", "study_date", "observed_at", "last_observed_at", "first_attempt", "correction",
               "later_observation", "hint_dependence", "reasoning_status", "observation_status", "next_action",
               "first_attempt_hint_dependence", "followup_status", "source_episode_id", "source_study_dates", "formal_ids")
    result = {name: episode[name] for name in allowed if name in episode}
    if "writing_stage" in episode:
        from .reading_writing_tasks import safe_fields
        result["writing_task"]=safe_fields(episode)
        if not episode.get("writing_task"):
            for field in ("first_attempt","later_observation"):
                if isinstance(result.get(field),str):
                    result[field]=result[field].replace("本次换题无提示写对，已观察到独立运用。","开放迁移的作答条件待核。")
        result.update({k:episode[k] for k in ("attempt_id","writing_stage","linked_keys") if k in episode})
    if cutoff is not None:
        limitations = []
        omitted = []
        follow_at = episode.get("last_observed_at") or episode.get("later_observed_at")
        try:
            follow_visible = bool(follow_at) and _time(follow_at, end_of_day=True) <= cutoff
        except (ValueError, TypeError):
            follow_visible = False
        if not follow_visible:
            for field in ("later_observation", "last_observed_at", "hint_dependence", "reasoning_status", "followup_status"):
                if field in result:
                    omitted.append(field)
                result.pop(field, None)
            limitations.append("later_observation_time_unknown" if not follow_at else "later_observation_after_cutoff")
        corrected_at = episode.get("correction_observed_at") or episode.get("corrected_at")
        try:
            correction_visible = bool(corrected_at) and _time(corrected_at, end_of_day=True) <= cutoff
        except (ValueError, TypeError):
            correction_visible = False
        if not correction_visible:
            if "correction" in result:
                omitted.append("correction")
            result.pop("correction", None)
            limitations.append("correction_time_unknown" if not corrected_at else "correction_after_cutoff")
        if "next_action" in result:
            omitted.append("next_action")
        result.pop("next_action", None)
        result["temporal_limitations"] = limitations
        result["temporal_fields_omitted"] = omitted
    return result


def _public_ref(audit: dict[str, Any], repo: Path, *, check_access: bool = True) -> dict[str, Any]:
    allowed = ("path", "sha256", "locator", "locators", "pointer", "pointers", "snapshot_pointer",
               "member", "package_id", "package_sha256")
    locator = audit.get("resolved_path") or audit.get("reference", {}).get("path")
    access = "not_reopened"
    if isinstance(locator, str) and locator and (check_access or not locator.startswith("/Volumes/")):
        path = Path(locator) if Path(locator).is_absolute() else repo / locator
        if path.is_file():
            access = "available_not_reopened"
        else:
            access = "external_source_unavailable" if str(path.resolve()).startswith("/Volumes/") else "source_unavailable_now"
    return {"source_key": audit["source_key"], "status_at_build": audit["status"],
            "verification": "verified_at_build" if audit["status"] == "verified" else "audit_at_build",
            "current_access": access,
            "locator_status": audit.get("locator_status", "unknown"),
            **{name: audit["reference"][name] for name in allowed if name in audit.get("reference", {})}}


def _load_claims(state: Path) -> dict[str, Any]:
    try:
        claims = load_json(state / "memory/claims.json").get("claims", {})
    except (OSError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _claims_by_point(claims: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for claim_id, claim in claims.items():
        if isinstance(claim, dict) and isinstance(claim.get("point_key"), str):
            groups.setdefault(claim["point_key"], {})[claim_id] = claim
    return groups


def _claims(claims: dict[str, Any], key: str, episode_ids: set[str], lifecycle: dict[str, Any],
            as_of: datetime | None, safe: bool) -> list[dict[str, Any]]:
    target_time = as_of or datetime.now(timezone.utc)
    invalid = set(lifecycle.get("invalidated_claim_ids", []))
    result = []
    for cid, claim in claims.items():
        if not isinstance(claim, dict) or claim.get("point_key") != key or cid in invalid:
            continue
        if claim.get("status") not in {"candidate", "confirmed", "active"}:
            continue
        evidence = claim.get("evidence_episode_ids", [])
        if not evidence or not set(evidence) <= episode_ids:
            continue
        if as_of is not None and not claim.get("valid_from"):
            continue
        try:
            if claim.get("valid_from") and _time(claim["valid_from"]) > target_time:
                continue
            if claim.get("valid_to") and _time(claim["valid_to"]) <= target_time:
                continue
        except (ValueError, TypeError):
            continue
        if safe and (claim.get("visibility") == "protected" or contains_answer_signal(claim.get("content"))):
            continue
        result.append({name: claim[name] for name in
                       ("claim_id", "content", "basis", "status", "evidence_episode_ids", "valid_from", "valid_to") if name in claim})
    return result


def _preference(state: Path, query: str | None) -> dict[str, Any] | None:
    path = state / "personal-summary/teaching-preferences.md"
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8")
    sections = re.split(r"(?m)(?=^## )", content)
    if query:
        tokens = search_tokens(query)
        scored = [(sum(token in section.casefold() for token in tokens), i, section)
                  for i, section in enumerate(sections)]
        selected = [section for score, _, section in sorted(scored, key=lambda x: (-x[0], x[1])) if score]
        if selected:
            sections = selected
    return {"key": "preference:teaching-preferences", "role": "explicit_preference",
            "source": {"path": str(path), "sha256": file_sha256(path)}, "content": "".join(sections),
            "sections_total": len(sections), "_sections": sections,
            "not_mastery_evidence": True}


def _public_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Share document identity while retaining every exact reference/locator."""
    matched = []
    for original in result["matched"]:
        point = {key: value for key, value in original.items() if not key.startswith("_")}
        episode_ids = {episode["episode_id"] for episode in point.get("episodes", [])}
        if "latest_observation" in point:
            episode_ids.add(point["latest_observation"]["episode_id"])
        omitted = 0
        for field, dependency in (("claims", "evidence_episode_ids"), ("contrasts", "source_episode_ids")):
            if field in point:
                delivered = [item for item in point[field] if set(item.get(dependency, [])) <= episode_ids]
                omitted += len(point[field]) - len(delivered)
                point[field] = delivered
        if omitted:
            point["dependent_items_omitted"] = omitted
        sources = point.get("sources")
        if sources:
            documents = {}
            common = ("path", "sha256", "status_at_build", "verification", "current_access")
            for source in sources:
                identity = tuple(source.get(key) for key in common)
                documents.setdefault(identity, []).append(source)
            if len(documents) < len(sources):
                grouped = []
                for group in documents.values():
                    if len(group) == 1:
                        grouped.append(group[0])
                    else:
                        shared = {key: group[0][key] for key in common if key in group[0]}
                        shared["references"] = [{key: value for key, value in source.items() if key not in common}
                                                for source in group]
                        grouped.append(shared)
                point["sources"] = grouped
                point["source_format"] = "shared_documents_exact_references"
        matched.append(point)
    return {**result, "matched": matched}


def _fit(result: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Fit complete observations and preference sections, never sliced claims."""
    temporal_omissions = Counter()
    unknown_time = False
    ambiguous_count = (sum(item.get("reason") == OBSERVATION_ORDER_AMBIGUOUS for item in result["unavailable"])
                       + sum("observation_order" in point for point in result["matched"]))
    ambiguity = {"observation_order_ambiguous_count": ambiguous_count} if ambiguous_count else {}
    if ambiguity:
        result = {**result, "coverage": {**result["coverage"], **ambiguity}}
    for point in result["matched"]:
        for episode in point.get("episodes", []):
            temporal_omissions.update(episode.get("temporal_fields_omitted", []))
            unknown_time |= any("unknown" in reason for reason in episode.get("temporal_limitations", []))
    if temporal_omissions:
        result = {**result, "coverage": {**result["coverage"], "temporal_fields_omitted": dict(temporal_omissions),
                                          "temporal_coverage_limited": unknown_time}}
        if unknown_time and result["status"] == "ready":
            result["status"] = "partial"
    public = _public_payload(result)
    if len(canonical_bytes(public)) <= max_bytes:
        return public
    output = {**result, "status": "partial", "matched": [], "misses": [], "unavailable": [],
              "pending": [], "conflicts": [], "coverage": {**result["coverage"], "budget_limited": True,
                "points_omitted": 0, "episodes_omitted": 0, "diagnostics_omitted": 0}}
    for point in result["matched"]:
        clean = {k: v for k, v in point.items() if not k.startswith("_")}
        if point.get("role") == "explicit_preference":
            clean["content"] = ""
            clean["sections_returned"] = 0
            clean["sections_omitted"] = point["sections_total"]
            output["matched"].append(clean)
            for section in point.get("_sections", []):
                old_content = clean["content"]
                clean["content"] += section
                clean["sections_returned"] += 1
                clean["sections_omitted"] -= 1
                if len(canonical_bytes(_public_payload(output))) > max_bytes:
                    clean["content"] = old_content
                    clean["sections_returned"] -= 1
                    clean["sections_omitted"] += 1
                    break
            if not clean["sections_returned"]:
                output["matched"].pop()
                output["coverage"]["points_omitted"] += 1
        elif "episodes" in point:
            episodes = clean.pop("episodes")
            def report_remaining(start: int) -> None:
                clean["remaining_episode_ids"] = [episode["episode_id"] for episode in episodes[start:start + 8]]
                clean["remaining_episode_ids_omitted"] = max(0, len(episodes) - start - 8)

            clean.update({"episodes": [], "episodes_returned": 0, "episodes_omitted": len(episodes)})
            report_remaining(0)
            source_pool = {source["source_key"]: source for source in clean.pop("sources", [])}
            clean["sources"] = []
            included_sources = set()
            output["matched"].append(clean)
            for i, episode in enumerate(episodes):
                extra_sources = [source_pool[key] for key in episode.get("source_keys", [])
                                 if key not in included_sources and key in source_pool]
                clean["episodes"].append(episode)
                clean["sources"].extend(extra_sources)
                report_remaining(i + 1)
                clean["episodes_returned"] += 1
                clean["episodes_omitted"] -= 1
                if len(canonical_bytes(_public_payload(output))) > max_bytes:
                    clean["episodes"].pop()
                    report_remaining(i)
                    if extra_sources:
                        del clean["sources"][-len(extra_sources):]
                    clean["episodes_returned"] -= 1
                    clean["episodes_omitted"] += 1
                    break
                included_sources.update(source["source_key"] for source in extra_sources)
            if not clean["episodes"]:
                output["matched"].pop()
                output["coverage"]["points_omitted"] += 1
            output["coverage"]["episodes_omitted"] += clean["episodes_omitted"]
        else:
            output["matched"].append(clean)
            if len(canonical_bytes(_public_payload(output))) > max_bytes:
                output["matched"].pop()
                output["coverage"]["points_omitted"] += 1
                if clean.get("scope_match"):
                    output["coverage"].setdefault("omitted_point_keys", []).append(clean["key"])
    for field in ("misses", "unavailable", "pending", "conflicts"):
        for item in result[field]:
            output[field].append(item)
            if len(canonical_bytes(_public_payload(output))) > max_bytes:
                output[field].pop()
                output["coverage"]["diagnostics_omitted"] += 1
    output["coverage"]["points_returned"] = len(output["matched"])
    # Very small budgets still get a truthful, bounded envelope.
    if len(canonical_bytes(_public_payload(output))) > max_bytes:
        output = {"schema_version": QUERY_SCHEMA, "status": "partial", "matched": [],
                  "coverage": {"budget_limited": True, "points_omitted": len(result["matched"]),
                               "diagnostics_omitted": sum(len(result[k]) for k in ("misses", "unavailable", "pending", "conflicts")),
                               **ambiguity},
                  "formal_write_count": 0, "history_scanned": False}
    return _public_payload(output)


def _result(view: str, as_of: str | None) -> dict[str, Any]:
    return {"schema_version": QUERY_SCHEMA, "status": "ready", "view": view,
            "matched": [], "misses": [], "unavailable": [], "pending": [], "conflicts": [],
            "generation": None, "summary_index_sha256": None,
            "coverage": {"as_of": as_of, "time_semantics": "occurred_cutoff_with_currently_verified_evidence",
                         "projection_available": False, "points_returned": 0,
                         "not_a_mastery_assessment": True},
            "formal_write_count": 0, "learning_event_write_count": 0, "history_scanned": False}


def _state_identity(state: Path) -> tuple[Any, ...]:
    result = []
    for path in (state / "memory/lifecycle.json", state / "memory/claims.json", state / "memory/current.json",
                 state / "personal-summary/current.json"):
        try:
            stat = path.stat()
            result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except OSError:
            result.append(None)
    return tuple(result)


def _published_generation(state: Path, publication_id: str, cutoff: datetime) -> dict[str, Any]:
    path = state / "memory/publications" / f"{publication_id}.json"
    if path.resolve() != path:
        raise ValidationError("memory replay requires a local publication receipt")
    receipt = load_json(path)
    if (receipt.get("publication_id") != publication_id or receipt.get("status") != "ready"
            or receipt.get("pending_component") is not None or not receipt.get("ready_at")):
        raise ValidationError("memory publication lacks a proven ready timestamp")
    generation = receipt.get("memory_generation")
    if not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{64}", generation):
        raise ValidationError("memory publication lacks a valid generation")
    if _time(receipt["ready_at"], end_of_day=True) > cutoff:
        raise ValidationError("memory publication became ready after the cutoff")
    return receipt


def query_memory_context(repo: Path, state: Path, *, keys: list[str] | None = None,
                         ids: list[str] | None = None, query: str | None = None,
                         source_id: str | None = None, unit_id: str | None = None,
                         view: str = "teaching", as_of: str | None = None,
                         max_bytes: int = 6144, question_id: str | None = None,
                         unlocked_question_ids: list[str] | None = None,
                         candidate_keys: list[str] | None = None,
                         known_publication: str | None = None, diagram: bool = False,
                         article_container_id: str | None = None,
                         include_evidence: bool = False) -> dict[str, Any]:
    """Return evidence-bounded context. Missing indexes never trigger builds."""
    repo, state = repo.resolve(), state.resolve()
    keys = list(dict.fromkeys(normalize_key(key) for key in (keys or [])))
    ids = list(dict.fromkeys(ids or []))
    if view not in {"teaching", "history", "audit"} or not 512 <= max_bytes <= 16384:
        raise ValidationError("memory query supports teaching/history/audit and 512..16384 bytes")
    if diagram and (view != "teaching" or as_of or known_publication or unlocked_question_ids):
        raise ValidationError("diagram context supports answer-free current teaching only")
    if article_container_id and (not diagram or not source_id or not unit_id or question_id):
        raise ValidationError("article context requires a scoped answer-free sentence diagram")
    if len(keys) > 8 or len(ids) > 8 or any(not isinstance(v, str) or not v or len(v) > 256 for v in ids):
        raise ValidationError("memory query supports at most eight keys and eight bounded IDs")
    if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 1000):
        raise ValidationError("memory natural query must contain 1..1000 characters")
    if not keys and not ids and not query:
        raise ValidationError("memory query requires keys, IDs or natural language")
    for value in (source_id, unit_id, question_id):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 512):
            raise ValidationError("invalid memory query scope")
    if unlocked_question_ids is not None and (not isinstance(unlocked_question_ids, list)
            or len(unlocked_question_ids) > 32 or any(not isinstance(x, str) or not x or len(x) > 256 for x in unlocked_question_ids)):
        raise ValidationError("unlocked question IDs must be an explicit bounded list")
    if include_evidence and (view != "teaching" or as_of or question_id or unlocked_question_ids
                             or not source_id or not re.fullmatch(r"S\d+", unit_id or "")):
        raise ValidationError("memory evidence requires one current answer-free source sentence")
    bundle_episodes = {}
    cutoff = _time(as_of, end_of_day=True) if as_of else None
    if known_publication is not None and (
            not isinstance(known_publication, str) or not _PUBLICATION_ID.fullmatch(known_publication) or cutoff is None
            or view == "teaching" or query is not None):
        raise ValidationError("known publication requires an exact key/ID, a cutoff, and history or audit view")
    natural_query = query if not (keys or ids) else None
    initial_state_identity = _state_identity(state)
    lifecycle = read_lifecycle(state)
    result = _result(view, as_of)
    short_question_without_source = bool(question_id and not source_id
                                         and re.fullmatch(r"(?:Q)?\d+(?:[-_:].*)?", question_id, re.I))
    receipt = None
    if known_publication:
        result["coverage"].update(time_semantics="occurred_cutoff_with_published_generation",
                                  known_publication=known_publication, live_safety_veto_applied=True,
                                  historical_claims_and_preferences_available=False)
        try:
            receipt = _published_generation(state, known_publication, cutoff)
        except (OSError, ValueError, TypeError, AttributeError, ValidationError):
            result.update(status="unavailable", unavailable=[{"reason": "historical_publication_unavailable"}])
            return _fit(result, max_bytes)
    def changed(payload: dict[str, Any]) -> dict[str, Any]:
        matched = [point for point in payload["matched"] if point.get("role") == "explicit_preference"]
        return {**payload, "matched": matched, "status": "partial" if matched else "pending",
                "pending": [*payload["pending"], {"reason": "memory_state_changed_during_query"}],
                "coverage": {**payload["coverage"], "points_returned": len(matched)}}

    def finish(payload: dict[str, Any], *, check_state: bool = False) -> dict[str, Any]:
        stale = check_state and _state_identity(state) != initial_state_identity
        if stale:
            payload = changed(payload)
        # Denied identities can themselves reveal correctness (for example a
        # protected "correct-choice" point). Echo only caller-known identities.
        for field in ("unavailable", "pending", "conflicts"):
            for item in payload.get(field, []):
                if item.get("key") and item["key"] not in keys:
                    item.pop("key", None)
                    if ids:
                        item["requested_ids"] = ids
                    elif keys:
                        item["requested_keys"] = keys
        fit = _fit(payload, max_bytes)
        if include_evidence and _state_identity(state) == initial_state_identity:
            from .sentence_evidence import bundle_for_episode
            target = next((point for point in fit["matched"] if point.get("key") in bundle_episodes), None)
            if target is not None:
                remaining = max_bytes - len(canonical_bytes(fit)) - 24
                if remaining >= 512:
                    ep = bundle_episodes[target["key"]]
                    bundle = bundle_for_episode(repo, state, source_id, unit_id, ep, min(4096, remaining))
                    if len(canonical_bytes(bundle)) <= remaining and _state_identity(state) == initial_state_identity:
                        fit["evidence_bundle"] = bundle
                if "evidence_bundle" not in fit:
                    fit["coverage"]["evidence_bundle"] = "use_exact_sentence_or_episode_read"
                    if len(canonical_bytes(fit)) > max_bytes:
                        fit["coverage"].pop("evidence_bundle")
        if include_evidence and check_state and not stale and _state_identity(state) != initial_state_identity:
            return _fit(changed(payload), max_bytes)
        return fit
    pref_requested = _PREFERENCE_ID in ids or "preference:" + _PREFERENCE_ID in ids
    if query and not keys and not ids and any(term in query for term in ("偏好", "答案公开", "图解", "选项按钮")):
        pref_requested = True
    if pref_requested:
        if as_of:
            result["unavailable"].append({"id": _PREFERENCE_ID, "reason": "preference_history_unavailable"})
        elif source_id or unit_id or question_id:
            result["unavailable"].append({"id": _PREFERENCE_ID, "reason": "preference_scope_unavailable"})
        else:
            pref = _preference(state, query)
            if pref:
                result["matched"].append(pref)
            else:
                result["misses"].append(_PREFERENCE_ID)
    ids = [value for value in ids if value not in {_PREFERENCE_ID, "preference:" + _PREFERENCE_ID}]
    if pref_requested and not keys and not ids:
        result["coverage"]["points_returned"] = len(result["matched"])
        if not result["matched"]:
            result["status"] = "unavailable" if result["unavailable"] else "not_found"
        return finish(result)
    try:
        summary_index = load_json(state / "personal-summary/current.json")
        if summary_index.get("schema_version") != INDEX:
            raise ValueError("invalid current summary index")
    except (OSError, ValueError):
        result["unavailable"].append({"reason": "personal_summary_index_unavailable"})
        result["status"] = "unavailable"
        return finish(result)
    try:
        db, manifest = open_memory_projection(state, known_generation=receipt["memory_generation"] if receipt else None)
    except (OSError, ValueError, sqlite3.Error, ValidationError) as exc:
        if receipt:
            result.update(status="unavailable", unavailable=[{"reason": "historical_generation_unavailable"}])
            return finish(result)
        result["coverage"]["degraded"] = "memory_projection_unavailable"
        result["coverage"]["projection_error"] = type(exc).__name__
        if (state / "memory/current.json").exists() or (state / "memory/generations").is_dir():
            # A plain summary cannot carry a previously published source or visibility veto.
            result["status"] = "partial" if result["matched"] else "unavailable"
            result["unavailable"].append({"reason": "published_source_audit_unavailable"})
            result["coverage"]["points_returned"] = len(result["matched"])
            return finish(result)
        if diagram:
            result.update(status="unavailable", unavailable=[{"reason": "diagram_projection_unavailable"}])
            return finish(result)
        return finish(_fallback_exact(state, summary_index, keys, ids, result, lifecycle, view, cutoff,
                               source_id, unit_id, question_id, unlocked_question_ids, max_bytes), check_state=True)
    live_db = None
    try:
        if receipt:
            try:
                consistent_time = _time(manifest["created_at"], end_of_day=True) <= _time(receipt["ready_at"], end_of_day=True)
            except (KeyError, TypeError, ValueError, AttributeError):
                consistent_time = False
            if receipt.get("summary_index_sha256") != manifest["summary_index_sha256"] or not consistent_time:
                result.update(status="unavailable", unavailable=[{"reason": "historical_generation_not_bound_to_publication"}])
                return finish(result)
            try:
                live_db, _ = open_memory_projection(state)
            except (OSError, ValueError, sqlite3.Error, ValidationError):
                result.update(status="unavailable", unavailable=[{"reason": "published_source_audit_unavailable"}])
                return finish(result)
            result["coverage"]["publication_ready_at"] = receipt["ready_at"]
        result["generation"] = manifest["generation"]
        result["summary_index_sha256"] = manifest["summary_index_sha256"]
        result["coverage"]["projection_available"] = True
        candidates: list[tuple[str, bool, str]] = []
        requested_episodes: dict[str, set[str]] = {}
        for key in keys:
            row = db.execute("SELECT point_key FROM aliases WHERE alias=?", (key,)).fetchone()
            if row:
                candidates.append((row[0], True, "exact_key"))
            else:
                result["misses"].append(key)
        for identity in ids:
            rows = db.execute("SELECT DISTINCT point_key,kind FROM identities WHERE identity=? ORDER BY point_key LIMIT 9", (identity,)).fetchall()
            if len(rows) > 8:
                result["unavailable"].append({"id": identity, "reason": "ambiguous_identity"})
            elif rows:
                candidates.extend((row[0], True, "exact_id") for row in rows)
                for row in rows:
                    if row[1] == "episode":
                        requested_episodes.setdefault(row[0], set()).add(identity)
            else:
                result["misses"].append(identity)
        # Known keys that miss never silently turn into guesses. Natural queries
        # are a distinct, explicit lookup mode.
        native = _natural_candidates(db, natural_query, source_id, unit_id, cutoff) if natural_query else []
        if natural_query and view == "history" and not cutoff:
            from .writing_history import first_draft_candidates
            for point,ident in first_draft_candidates(state,manifest["generation"],natural_query):
                candidates.append((point,False,"writing_task_locator"))
                requested_episodes.setdefault(point,set()).add(ident)
        candidates.extend(item for item in native if item[2] == "exact_label_candidate")
        for key in ((candidate_keys or []) if natural_query else [])[:64]:
            try:
                candidates.append((normalize_key(key), False, "external_candidate"))
            except (ValueError, ValidationError, TypeError):
                continue
        candidates.extend(item for item in native if item[2] != "exact_label_candidate")
        seen, eligible = set(), 0
        claims = None
        for key, exact, method in candidates:
            if key in seen:
                continue
            seen.add(key)
            row = db.execute("SELECT point_key,summary_sha256,answer_safe,audit_json,question_ids_json,"
                             "record_json IS NOT NULL AS has_record FROM points WHERE point_key=?", (key,)).fetchone()
            if row is None:
                continue
            audit = json.loads(row["audit_json"])
            if not row["has_record"]:
                if exact:
                    result["unavailable"].append({"key": key, "reason": "invalid_or_missing_summary"})
                continue
            current = ({"sha256": row["summary_sha256"]} if receipt
                       else summary_index["routes"].get(key_hash(key)))
            if not current or current.get("sha256") != row["summary_sha256"]:
                result["pending"].append({"key": key, "reason": "summary_generation_pending"})
                continue
            if audit.get("status") == "conflict":
                if exact:
                    result["conflicts"].append({"key": key, "reason": "summary_structure_or_source_conflict", "issues": audit["issues"]})
                continue
            teaching_only = view == "teaching" and cutoff is None
            header_digest = current.get("teaching_sha256") if teaching_only else None
            if header_digest:
                head = db.execute("SELECT payload_json FROM teaching_headers WHERE point_key=?", (key,)).fetchone()
                encoded = head[0] if head else None
                expected = header_digest
            else:
                encoded = db.execute("SELECT record_json FROM points WHERE point_key=?", (key,)).fetchone()[0]
                expected = row["summary_sha256"]
            record = json.loads(encoded) if encoded else {}
            if object_sha256(record) != expected or record.get("key") != key:
                result["unavailable"].append({"key": key, "reason": "projection_record_corrupt"})
                continue
            question_bound = bool(json.loads(row["question_ids_json"]))
            protected = not bool(row["answer_safe"]) or not is_answer_safe(record, question_bound=question_bound)
            authorized = exact and bool(question_id) and question_id in (unlocked_question_ids or [])
            if short_question_without_source:
                if exact:
                    reason = ("protected_question_requires_source_scope" if protected and authorized
                              else "question_requires_source_scope")
                    result["unavailable"].append({"key": key, "reason": reason})
                continue
            if protected and not authorized:
                if exact:
                    result["unavailable"].append({"key": key, "reason": "protected"})
                continue
            if lifecycle.get("global_pending") or key in lifecycle.get("pending_keys", {}):
                result["pending"].append({"key": key, "reason": "memory_update_pending"})
                continue
            if key in lifecycle.get("invalidated_summary_keys", []) and lifecycle.get("accepted_summary_hashes", {}).get(key) != row["summary_sha256"]:
                result["pending"].append({"key": key, "reason": "invalidated_summary_requires_refresh"})
                continue
            if live_db is not None:
                live_point = live_db.execute("SELECT summary_sha256,answer_safe,audit_json FROM points WHERE point_key=?",
                                             (key,)).fetchone()
                live_route = summary_index["routes"].get(key_hash(key), {})
                if (live_point is None or live_point["summary_sha256"] != live_route.get("sha256")
                        or json.loads(live_point["audit_json"])["status"] in {"conflict", "invalid_summary"}
                        or (row["answer_safe"] and not live_point["answer_safe"])):
                    result["unavailable"].append({"key": key, "reason": "historical_evidence_no_longer_verified"})
                    continue
            episodes, article_observations, other_article_observations = [], [], []
            # Select identity and scope before reading a teaching episode body.
            all_rows = db.execute("SELECT episode_id,occurred_at,source_ids_json,unit_ids_json,"
                                  "question_ids_json,scope_bindings_json FROM episodes WHERE point_key=?",
                                  (key,)).fetchall()
            invalid_ids = set(lifecycle.get("invalidated_episode_ids", []))
            invalid_present = any(item["episode_id"] in invalid_ids for item in all_rows)
            for item in all_rows:
                if key in requested_episodes and item["episode_id"] not in requested_episodes[key]:
                    continue
                if item["episode_id"] in invalid_ids:
                    continue
                scoped = _episode_scope(item)
                if diagram and not question_id and scoped["question_ids"]:
                    continue
                if cutoff:
                    if not item["occurred_at"]:
                        continue
                    if _time(item["occurred_at"], end_of_day=True) > cutoff:
                        continue
                episode = {"episode_id": item["episode_id"], "occurred_at": item["occurred_at"], **scoped}
                if _scoped(scoped, source_id, unit_id, question_id):
                    episodes.append(episode)
                elif (article_container_id and exact and not protected and audit.get("status") == "verified" and
                      _article_only(scoped, source_id, article_container_id)):
                    article_observations.append(episode)
                elif (diagram and article_container_id and not question_id and method == "exact_key"
                      and key.startswith(("word:", "phrase:")) and not protected
                      and audit.get("status") == "verified"):
                    match = _lexical_article(scoped, source_id, article_container_id)
                    if match:
                        episode["article_label"] = match[1]
                        (article_observations if match[0] == "same_article" else other_article_observations).append(episode)
            same_article = not episodes and bool(article_observations)
            if same_article:
                episodes = article_observations
            other_article = not episodes and bool(other_article_observations)
            if other_article:
                episodes = other_article_observations
            if not episodes:
                if exact:
                    reason = "no_evidence_as_of" if cutoff else "no_evidence_in_scope" if source_id or unit_id or question_id else "no_active_evidence"
                    result["unavailable"].append({"key": key, "reason": reason})
                continue
            eligible += 1
            accepted = lifecycle.get("accepted_summary_hashes", {}).get(key) == row["summary_sha256"]
            if invalid_present and not accepted:
                result["pending"].append({"key": key, "reason": "invalidated_evidence_requires_summary_refresh"})
                continue
            filtered = len(episodes) != len(all_rows)
            group, episodes = select_observations(episodes, record if not cutoff else None)
            ambiguous = len(group) > 1
            if teaching_only and ambiguous:
                if exact:
                    result["unavailable"].append({"key": key, **ambiguous_observation_order(group)})
                continue
            selected_episodes = group if teaching_only else episodes
            history_allowed = True
            for episode in selected_episodes:
                identity = episode["episode_id"]
                audits = _episode_audits(db, key, identity)
                if live_db is not None:
                    live_episode = live_db.execute(
                        "SELECT source_ids_json,unit_ids_json,question_ids_json,scope_bindings_json "
                        "FROM episodes WHERE point_key=? AND episode_id=?", (key, identity)).fetchone()
                    if (live_episode is None or not _scoped(_episode_scope(live_episode), source_id, unit_id, question_id)
                            or audits != _episode_audits(live_db, key, identity)):
                        history_allowed = False
                        break
                raw = json.loads(db.execute("SELECT episode_json FROM episodes WHERE point_key=? AND episode_id=?",
                                            (key, identity)).fetchone()[0])
                episode["raw"] = raw
                sources = [json.loads(audit) for audit in audits]
                direct_keys = {object_sha256(ref) for ref in raw.get("evidence", []) if isinstance(ref, dict)}
                if view != "audit":
                    sources = [a for a in sources if a["source_key"] in direct_keys] or sources
                episode["sources"] = [_public_ref(source, repo, check_access=not teaching_only) for source in sources]
            if not history_allowed:
                result["unavailable"].append({"key": key, "reason": "historical_evidence_no_longer_verified"})
                continue
            if not protected and any(contains_answer_signal(e["raw"]) for e in selected_episodes):
                result["unavailable"].append({"key": key, "reason": "answer_visibility_conflict"})
                continue
            episode_ids = {e["episode_id"] for e in episodes}
            # Reuse the first eligible point's parse within this state-checked request.
            if receipt is None and claims is None:
                claims = _claims_by_point(_load_claims(state))
            point = {"key": key, "summary_sha256": row["summary_sha256"], "retrieval_method": method,
                     "visibility": "protected" if protected else "answer_free", "audit": audit,
                     "claims": [] if claims is None else _claims(claims.get(key, {}), key, episode_ids, lifecycle, cutoff, not protected),
                     "episodes_total": len(all_rows), "episodes_in_scope": len(episodes)}
            active_count = sum(item["episode_id"] not in invalid_ids for item in all_rows)
            scope_filtered = filtered and not (accepted and len(episodes) == active_count)
            summary_scope_incomplete = bool(cutoff or source_id or unit_id or question_id or scope_filtered)
            point["summary_scope_incomplete"] = summary_scope_incomplete
            if ambiguous:
                point["observation_order"] = ambiguous_observation_order(group)
            elif not summary_scope_incomplete and not cutoff:
                point["teaching_hint"] = record["teaching_hint"]
                point["teaching_hint_status"] = "legacy_unclassified"
                if record.get("contrasts"):
                    point["contrasts"] = [c for c in record["contrasts"] if set(c.get("source_episode_ids", [])) <= episode_ids]
            elif summary_scope_incomplete:
                point["teaching_hint_omitted"] = "current_hint_is_not_historical_evidence" if cutoff else "summary_scope_incomplete"
            if view == "teaching" and not cutoff:
                latest = episodes[0]
                point["latest_observation"] = _episode_public(latest["raw"])
                point["sources"] = latest["sources"]
                if include_evidence and not protected and not same_article and not other_article:
                    bundle_episodes[key] = latest["raw"]
                if diagram and latest.get("article_label"):
                    point["latest_observation"]["source_article"] = latest["article_label"]
                if diagram and not protected and not question_id and audit.get("status") == "verified":
                    # A scoped diagram may reuse only contrasts directly grounded in this selected episode.
                    point["contrasts"] = [c for c in record.get("contrasts", [])
                                          if set(c.get("source_episode_ids", [])) == {latest["episode_id"]}
                                          and not contains_answer_signal(c)]
            else:
                sources = {source["source_key"]: source for e in episodes for source in e["sources"]}
                point["sources"] = list(sources.values())
                point["episodes"] = [{**_episode_public(e["raw"], cutoff), "source_keys": [s["source_key"] for s in e["sources"]],
                                       # File and package aliases remain in the
                                       # exact source references. Repeating them
                                       # per observation crowds out real history.
                                       "source_ids": [sid for sid in e["source_ids"] if "/" not in sid and not sid.startswith("EN-PKG-")],
                                       "unit_ids": e["unit_ids"]} for e in episodes]
            scope = "other_article" if other_article else "same_article" if same_article else "exact_unit" if unit_id else "recorded_history"
            result["matched"].append(_diagram_point(point, scope) if diagram else point)
            if len(result["matched"]) >= 8:
                break
        result["coverage"].update({"candidate_count": len(seen), "points_in_scope": eligible,
                                    "points_returned": len(result["matched"]),
                                    "source_integrity_checked_at_build": manifest.get("source_audit_performed", False),
                                    "original_sources_reopened": False,
                                    "source_graph_expansion": "transitive" if view == "audit" else "direct_references; audit_view_expands_ancestors"})
        if result["matched"]:
            result["status"] = "partial" if any(result[k] for k in ("misses", "unavailable", "pending", "conflicts")) else "ready"
        elif result["pending"]:
            result["status"] = "stale" if all(item["reason"] == "summary_generation_pending" for item in result["pending"]) else "pending"
        elif result["conflicts"]:
            result["status"] = "conflict"
        elif any(item.get("reason", "").startswith("no_") for item in result["unavailable"]):
            result["status"] = "no_evidence"
        elif result["unavailable"]:
            result["status"] = "unavailable"
        else:
            result["status"] = "not_found"
        return finish(result, check_state=True)
    finally:
        if live_db is not None:
            live_db.close()
        db.close()


def _natural_candidates(db: sqlite3.Connection, query: str, source_id: str | None, unit_id: str | None,
                        cutoff: datetime | None = None) -> list[tuple[str, bool, str]]:
    tokens = search_tokens(query)[:80]
    if not tokens:
        return []
    match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    where, params = ["p.answer_safe=1"], []
    episode_filters = ["e.point_key=p.point_key"]
    if source_id or unit_id:
        def scope_match(bindings, sources, units, questions):
            return int(_scoped({"bindings": json.loads(bindings), "source_ids": json.loads(sources),
                                "unit_ids": json.loads(units), "question_ids": json.loads(questions)}, source_id, unit_id))
        db.create_function("memory_scope_matches", 4, scope_match, deterministic=True)
        episode_filters.append("memory_scope_matches(e.scope_bindings_json,e.source_ids_json,e.unit_ids_json,e.question_ids_json)=1")
    if cutoff:
        episode_filters.append("(CASE WHEN length(e.occurred_at)=10 THEN julianday(e.occurred_at||'T23:59:59.999999+08:00') ELSE julianday(e.occurred_at) END)<=julianday(?)")
        params.append(cutoff.isoformat())
    if len(episode_filters) > 1:
        where.append("EXISTS(SELECT 1 FROM episodes e WHERE " + " AND ".join(episode_filters) + ")")
    sql = ("SELECT safe_search.point_key,bm25(safe_search) FROM safe_search JOIN points p ON p.point_key=safe_search.point_key "
           "WHERE safe_search MATCH ? AND " + " AND ".join(where) + " ORDER BY bm25(safe_search),safe_search.point_key LIMIT 48")
    rows = db.execute(sql, [match, *params]).fetchall()
    # Overlapping CJK bigrams/trigrams are retrieval aids, not independent
    # query requirements. Prefer coverage of distinct user-supplied terms so
    # repeating one rare phrase cannot outrank a point matching all concepts.
    primary_terms = list(dict.fromkeys(part.casefold() for part in
        re.findall(r"[a-zA-Z0-9]+(?:[-'][a-zA-Z0-9]+)*|[\u3400-\u9fff]+", query)
        if 2 <= len(part) <= 24))
    if len(primary_terms) > 1:
        scores = {}
        for row in rows:
            text = record_text(json.loads(db.execute("SELECT record_json FROM points WHERE point_key=?", (row[0],)).fetchone()[0])).casefold()
            scores[row[0]] = sum(term in text for term in primary_terms)
        rows = sorted(rows, key=lambda row: (-scores[row[0]], row[1], row[0]))
    result = [(row[0], False, "native_lexical_candidate") for row in rows]
    plain = unicodedata.normalize("NFKC", query).casefold().strip()
    forms = [plain]
    # Inflections are candidate generation only. They never rewrite IDs or
    # declare two senses equivalent, and source/scope checks still follow.
    if re.fullmatch(r"[a-z]+", plain):
        if plain.endswith("ies"):
            forms.append(plain[:-3] + "y")
        if plain.endswith("s") and not plain.endswith("ss"):
            forms.append(plain[:-1])
        if plain.endswith("ed"):
            forms.extend([plain[:-2], plain[:-1]])
        if plain.endswith("ing"):
            forms.extend([plain[:-3], plain[:-3] + "e"])
    direct = []
    for form in dict.fromkeys(forms):
        for row in db.execute("SELECT a.point_key FROM aliases a JOIN points p ON p.point_key=a.point_key WHERE p.answer_safe=1 AND substr(a.alias,instr(a.alias,':')+1)=? ORDER BY a.point_key LIMIT 8", (form,)):
            direct.append((row[0], False, "exact_label_candidate" if form == plain else "inflection_candidate"))
    return direct + result


def _fallback_exact(state: Path, index: dict[str, Any], keys: list[str], ids: list[str], result: dict[str, Any],
                    lifecycle: dict[str, Any], view: str, cutoff: datetime | None,
                    source_id: str | None, unit_id: str | None, question_id: str | None,
                    unlocked_question_ids: list[str] | None, max_bytes: int) -> dict[str, Any]:
    """Retain the old exact safe path, with explicit missing coverage."""
    for identity in ids:
        result["unavailable"].append({"id": identity, "reason": "identity_index_unavailable"})
    seen = set()
    for key in keys:
        pointer = index["routes"].get(key_hash(key))
        if pointer is None:
            result["misses"].append(key)
            continue
        canonical = pointer["key"]
        if canonical in seen:
            continue
        seen.add(canonical)
        if lifecycle.get("global_pending") or canonical in lifecycle.get("pending_keys", {}):
            result["pending"].append({"key": canonical, "reason": "memory_update_pending"})
            continue
        if canonical in lifecycle.get("invalidated_summary_keys", []) and lifecycle.get("accepted_summary_hashes", {}).get(canonical) != pointer["sha256"]:
            result["pending"].append({"key": canonical, "reason": "invalidated_summary_requires_refresh"})
            continue
        try:
            record = _read_record(state / "personal-summary", pointer, bounded=True)
        except (OSError, ValueError, KeyError, TypeError, ValidationError):
            result["unavailable"].append({"key": canonical, "reason": "invalid_or_missing_summary"})
            continue
        ancestor_unverified = bool(record.get("maintenance_parent_sha256"))
        for episode in record.get("episodes", []):
            for ref in episode.get("evidence", []):
                path = ref.get("path")
                if isinstance(path, str):
                    resolved = (Path(path) if Path(path).is_absolute() else state.parent / path).resolve()
                    ancestor_unverified |= resolved.parent == (state / "personal-summary/objects").resolve()
        if ancestor_unverified:
            result["unavailable"].append({"key": canonical, "reason": "inherited_visibility_unverified"})
            continue
        if not is_answer_safe(record):
            result["unavailable"].append({"key": canonical, "reason": "protected_or_scope_unverified"})
            continue
        if source_id or unit_id or question_id:
            result["unavailable"].append({"key": canonical, "reason": "scope_index_unavailable"})
            continue
        invalid = set(lifecycle.get("invalidated_episode_ids", []))
        if invalid & {episode["episode_id"] for episode in record["episodes"]}:
            result["pending"].append({"key": canonical, "reason": "invalidated_evidence_requires_summary_refresh"})
            continue
        episodes = [e for e in record["episodes"] if not cutoff or (e.get("observed_at") or e.get("study_date"))
                    and _time(e.get("observed_at") or e["study_date"], end_of_day=True) <= cutoff]
        if not episodes:
            result["unavailable"].append({"key": canonical, "reason": "no_evidence_as_of"})
            continue
        group, episodes = select_observations(episodes, record if not cutoff else None)
        ambiguous = len(group) > 1
        if view == "teaching" and not cutoff and ambiguous:
            result["unavailable"].append({"key": canonical, **ambiguous_observation_order(group)})
            continue
        point = {"key": canonical, "summary_sha256": pointer["sha256"], "retrieval_method": "degraded_exact_summary",
                 "teaching_hint_status": "legacy_unclassified", "coverage": "source_and_scope_index_unavailable"}
        if ambiguous:
            point["observation_order"] = ambiguous_observation_order(group)
        elif not cutoff:
            point["teaching_hint"] = record["teaching_hint"]
        if view == "teaching" and not cutoff:
            point["latest_observation"] = _episode_public(episodes[0])
        else:
            point["episodes"] = [_episode_public(e, cutoff) for e in episodes]
        result["matched"].append(point)
    result["coverage"]["points_returned"] = len(result["matched"])
    result["status"] = "degraded" if result["matched"] else "pending" if result["pending"] else "unavailable"
    return _fit(result, max_bytes)
