"""Point-hash routing to precomputed personal notes; no runtime history scans."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .answer_safety import point_answer_risk
from .errors import IdempotencyConflict, ValidationError
from .util import (LOCAL_ZONE, atomic_write_bytes, atomic_write_json, canonical_bytes, captured_reference_text,
                   exclusive_lock, file_sha256, learner_message_spans, load_json, object_sha256, parse_iso_date)

SCHEMA = "english_point_personal_summary_v1"
INDEX = "english_point_personal_summary_index_v1"
PREFIXES = {"word", "phrase", "syntax", "option_type", "error_type"}
CONTRAST_STATUSES = {"confirmed", "candidate", "recorded_form_uncertain", "sense"}
TEACHING_OBJECT_BYTES = 12 * 1024
EPISODE_BYTES = 12 * 1024
OBSERVATION_ORDER_AMBIGUOUS = "observation_order_ambiguous"
_MESSAGE_LOCATOR = re.compile(r"#?/?messages/(\d+)(?:/content)?")
_FOLLOWUP_HINTS = {"not_observed": {"not_observed"}, "acknowledgment_only": {"not_observed"},
                   "prompted": {"hint", "explanation"}, "self_corrected": {"none"}, "independent": {"none"}}
_ACK = re.compile(r"(?:懂了|我懂了|明白了|理解了|知道了|了解了|看懂了|学会了|好|好的|对|嗯+|收到|谢谢|原来如此|"
                  r"got it|i (?:get it|understand)|ok(?:ay)?|thanks|yes)", re.I)


def _user_message_text(path: Path, locator: Any) -> str | None:
    match = _MESSAGE_LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
    if not match:
        return None
    messages = load_json(path).get("messages", [])
    index = int(match[1])
    if not isinstance(messages, list) or index >= len(messages) or not isinstance(messages[index], dict):
        return None
    spans = learner_message_spans(messages[index], captured_reference_text(path))
    return "\n\n".join(spans).strip() or None


def _followup_index(episode: dict[str, Any]) -> int | None:
    if ("first_attempt_hint_dependence" in episode
            and episode["first_attempt_hint_dependence"] not in ("none", "hint", "explanation", "not_observed")):
        raise ValidationError("first-attempt hint dependence is invalid")
    status = episode.get("followup_status")
    index = episode.get("followup_evidence_index")
    if status is None and index is None:  # Legacy observations keep their original semantics.
        return None
    if not isinstance(status, str) or status not in _FOLLOWUP_HINTS or episode.get("hint_dependence") not in _FOLLOWUP_HINTS[status]:
        raise ValidationError("follow-up status must agree with actual hint dependence")
    if status == "not_observed":
        if index is not None:
            raise ValidationError("unobserved follow-up cannot cite a user follow-up message")
        return None
    if type(index) is not int or not 0 <= index < len(episode["evidence"]):
        raise ValidationError("observed follow-up needs an exact evidence index")
    return index


def _validate_followup_text(episode: dict[str, Any], text: str | None) -> None:
    if not text:
        raise ValidationError("observed follow-up needs an exact original user message")
    # This cheap check catches bare acknowledgments; the reviewer must still
    # judge chronology, prompt exposure, and whether a response shows the skill.
    parts = [p.strip() for p in re.split(r"[，。！!、,.；;？?]+", text) if p.strip()]
    if episode["followup_status"] != "acknowledgment_only" and parts and all(_ACK.fullmatch(p) for p in parts):
        raise ValidationError("an acknowledgment is not a learner follow-up performance")


def teaching_header(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record[k] for k in ("key", "visibility", "teaching_hint", "latest_episode_id",
                                   "latest_episode_order", "contrasts") if k in record}


def observation_time(value: Any) -> datetime:
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (instant if instant.tzinfo is not None else instant.replace(tzinfo=LOCAL_ZONE)).astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _observation_bounds(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
    as_recorded = row.get("occurred_at", row.get("observed_at") or row.get("study_date"))
    if not isinstance(as_recorded, str):
        return None
    precision = re.match(r"^\d{4}-\d{2}-\d{2}(?:[T ](\d{2})(?::?(\d{2})(?::?(\d{2})(?:[.,](\d+))?)?)?)?", as_recorded)
    if precision is None:
        return None
    try:
        raw_time = datetime.fromisoformat(as_recorded.replace("Z", "+00:00"))
        start = raw_time if raw_time.tzinfo is not None else raw_time.replace(tzinfo=LOCAL_ZONE)
        hour, minute, second, fraction = precision.groups()
        if hour is None:
            step = timedelta(days=1)
        elif minute is None:
            step = timedelta(hours=1)
        elif second is None:
            step = timedelta(minutes=1)
        elif fraction is None:
            step = timedelta(seconds=1)
        else:
            step = timedelta(microseconds=10 ** max(0, 6 - len(fraction)))
        return (start.astimezone(timezone.utc), (start + step).astimezone(timezone.utc))
    except (OverflowError, ValueError):
        return None


def select_observations(episodes: list[dict[str, Any]], record: dict[str, Any] | None = None
                        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the possible latest group and a deterministic display order."""
    timed = [(ep, _observation_bounds(ep)) for ep in episodes]
    latest_start = max((bounds[0] for _, bounds in timed if bounds), default=None)
    group = sorted((ep for ep, bounds in timed if bounds is None or bounds[1] > latest_start),
                   key=lambda ep: ep["episode_id"])
    ids = {ep["episode_id"] for ep in group}
    proof = (record or {}).get("latest_episode_order")
    if isinstance(proof, dict) and len(group) > 1:
        preferred = record.get("latest_episode_id")
        # A new or differently scoped observation can make a previously proven
        # choice inapplicable. A stored preference alone is never chronology.
        if (proof.get("episode_id") == preferred and preferred in ids
                and proof.get("episode_ids") == sorted(ids)):
            group = [ep for ep in group if ep["episode_id"] == preferred]
    current = {ep["episode_id"] for ep in group}
    past = sorted((ep for ep, _ in timed if ep["episode_id"] not in current), key=lambda ep: ep["episode_id"])
    starts = {ep["episode_id"]: bounds[0] if bounds else datetime.min.replace(tzinfo=timezone.utc)
              for ep, bounds in timed}
    past.sort(key=lambda ep: starts[ep["episode_id"]], reverse=True)
    return group, group + past


def ambiguous_observation_order(group: list[dict[str, Any]]) -> dict[str, Any]:
    return {"reason": OBSERVATION_ORDER_AMBIGUOUS, "episode_ids": sorted(ep["episode_id"] for ep in group)[:8],
            "episode_ids_omitted": max(0, len(group) - 8)}


def _verified_latest_order(repo: Path, episode_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prove a tied explicit selection by distinct original user turns in one file."""
    if len(candidates) < 2 or episode_id not in {ep["episode_id"] for ep in candidates}:
        return None
    anchors = {}
    documents = {}
    for ep in candidates:
        possible = set()
        for ref in ep.get("evidence", ()):
            path = ref.get("path")
            locator = ref.get("locator")
            match = _MESSAGE_LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
            if not isinstance(path, str) or not match:
                continue
            original = (repo / path).resolve()
            if not original.is_relative_to(repo.resolve()) or original.suffix != ".json":
                continue
            if original not in documents:
                try:
                    documents[original] = (file_sha256(original), load_json(original).get("messages", []),
                                           captured_reference_text(original))
                except (OSError, ValueError):
                    documents[original] = ("", [], "")
            digest, messages, source_text = documents[original]
            turn = int(match[1])
            if digest != ref["sha256"] or not isinstance(messages, list) or turn >= len(messages):
                continue
            spans = learner_message_spans(messages[turn], source_text)
            if any(ep["first_attempt"].strip() == span.strip() for span in spans):
                possible.add((original, digest, turn))
        if len(possible) != 1:
            return None
        anchors[ep["episode_id"]], = possible
    origin, digest, turn = anchors[episode_id]
    if any(other[:2] != (origin, digest) or other[2] >= turn
           for other_id, other in anchors.items() if other_id != episode_id):
        return None
    if len({anchor[2] for anchor in anchors.values()}) != len(anchors):
        return None
    return {"episode_id": episode_id, "episode_ids": sorted(anchors), "source_sha256": digest}


def _is_archive_evidence(ref: Any) -> bool:
    return isinstance(ref, dict) and bool({"package_id", "package_sha256", "member"} & set(ref))


def _validate_archive_locator(target: Path, locator: Any, *, attempt: str | None = None) -> bool:
    """Resolve an exact node while retaining the original whole-member hash."""
    if not isinstance(locator, str) or not locator:
        raise ValidationError("archive summary evidence requires an exact locator")
    if re.fullmatch(r"messages/\d+", locator):
        pointer = "/" + locator
    elif locator.startswith("#"):
        pointer = locator[1:]
    else:
        pointer = locator
    if pointer and not pointer.startswith("/"):
        raise ValidationError("archive summary locator must be a JSON pointer or messages/N")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        value = document
        tokens = []
        for token in ([] if not pointer else pointer[1:].split("/")):
            if re.search(r"~(?![01])", token):
                raise ValidationError("archive summary locator has an invalid JSON pointer escape")
            token = token.replace("~1", "/").replace("~0", "~")
            tokens.append(token)
            if isinstance(value, list) and token.isdigit():
                value = value[int(token)]
            elif isinstance(value, dict):
                value = value[token]
            else:
                raise ValidationError("archive summary locator traverses a scalar or invalid array index")
        # A whole user message or its content establishes learner evidence.
        # Arrays, assistant text, source text and proof metadata remain auxiliary.
        if (len(tokens) not in {2, 3} or tokens[0] != "messages" or not tokens[1].isdigit()
                or (len(tokens) == 3 and tokens[2] != "content") or not isinstance(document, dict)):
            return False
        messages = document.get("messages")
        if not isinstance(messages, list) or int(tokens[1]) >= len(messages):
            return False
        message = messages[int(tokens[1])]
        spans = learner_message_spans(message, captured_reference_text(target))
        return bool(spans) and (attempt is None or spans == (message["content"],)
                                or any(attempt in span for span in spans))
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise ValidationError("archive summary locator does not resolve") from exc


def _validate_archive_evidence(repo: Path, state: Path, ref: dict[str, Any],
                               formal_receipt: dict[str, Any] | None, *,
                               attempt: str | None = None,
                               followup_text: list[str | None] | None = None) -> bool:
    """Use only the native receipt -> pointer -> original package authority."""
    required = {"package_id", "package_sha256", "member", "sha256", "locator"}
    if not required <= set(ref) or set(ref) - required - {"path"}:
        raise ValidationError("archive summary evidence fields are invalid")
    if (not formal_receipt or formal_receipt.get("schema_version") != "english_formal_receipt_validation_v1"
            or formal_receipt.get("status") != "PASS" or formal_receipt.get("subject") != "english"
            or not formal_receipt.get("receipt_id")
            or not isinstance(formal_receipt.get("writer_receipt_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", formal_receipt["writer_receipt_sha256"])):
        raise ValidationError("archive summary evidence requires a verified formal receipt")
    package_id = ref["package_id"]
    match = re.fullmatch(r"EN-PKG-(\d{8})-[A-F0-9]{16}", package_id) if isinstance(package_id, str) else None
    if (not match or not isinstance(ref["package_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", ref["package_sha256"])
            or not isinstance(ref["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", ref["sha256"])):
        raise ValidationError("archive summary package/member identity is invalid")
    ids, hashes = formal_receipt.get("package_ids"), formal_receipt.get("package_sha256s")
    if (not isinstance(ids, list) or not isinstance(hashes, list) or len(ids) != len(hashes)
            or any(not isinstance(pid, str) for pid in ids) or len(ids) != len(set(ids))
            or any(not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in hashes)
            or package_id not in ids or hashes[ids.index(package_id)] != ref["package_sha256"]):
        raise ValidationError("archive summary package is not bound by the formal receipt")
    member = ref["member"]
    if (not isinstance(member, str) or not member or Path(member).is_absolute()
            or ".." in Path(member).parts or member != Path(member).as_posix()):
        raise ValidationError("archive summary member path is unsafe")
    if "path" in ref and not isinstance(ref["path"], str):
        raise ValidationError("archive summary logical path must be text")
    date = match.group(1)
    from .archive import resolve_archived_package_from_pointer
    try:
        study_date = parse_iso_date(f"{date[:4]}-{date[4:6]}-{date[6:]}")
        root = resolve_archived_package_from_pointer(
            state, repo, study_date=study_date, package_id=package_id,
            expected_package_sha256=ref["package_sha256"],
        ).resolve(strict=True)
        manifest = load_json(root / "manifest.json")
        if (manifest.get("package_id") != package_id
                or manifest.get("package_canonical_sha256") != ref["package_sha256"]
                or member not in {"manifest.json", "receipt.json", *manifest.get("files", {})}):
            raise ValidationError("archive summary member is outside its verified native package")
        target = (root / member).resolve(strict=True)
        target.relative_to(root)
        if not target.is_file() or file_sha256(target) != ref["sha256"]:
            raise ValidationError("archive summary evidence changed")
        if followup_text is not None and member == "conversation.json":
            followup_text.append(_user_message_text(target, ref["locator"]))
        user_node = _validate_archive_locator(target, ref["locator"], attempt=attempt)
        return member == "conversation.json" and user_node
    except (OSError, ValueError, TypeError) as exc:
        raise ValidationError("archive summary evidence is unavailable or escapes its package") from exc


def _validate_contrasts(record: dict[str, Any]) -> None:
    """Keep the teaching comparison bound to actual, preserved observations."""
    contrasts = record.get("contrasts", [])
    if not isinstance(contrasts, list) or len(contrasts) > 4:
        raise ValidationError("a point supports at most four specific contrasts; split distinct senses")
    episode_ids = {e["episode_id"] for e in record["episodes"]}
    identities = set()
    for contrast in contrasts:
        if (not isinstance(contrast, dict) or not isinstance(contrast.get("status"), str)
                or contrast["status"] not in CONTRAST_STATUSES):
            raise ValidationError("contrast needs an explicit evidence status")
        for field in ("target", "target_meaning", "other", "other_meaning", "distinction", "evidence_note"):
            if not isinstance(contrast.get(field), str) or not contrast[field].strip():
                raise ValidationError(f"contrast requires {field}")
        sources = contrast.get("source_episode_ids")
        if (not isinstance(sources, list) or not sources
                or any(not isinstance(s, str) or s not in episode_ids for s in sources)):
            raise ValidationError("contrast must cite preserved source episodes")
        if contrast["status"] == "sense" and contrast["target"] != contrast["other"]:
            raise ValidationError("a sense contrast must retain the same English word")
        identity = (contrast["target"].casefold(), contrast["other"].casefold())
        if identity in identities:
            raise ValidationError("duplicate contrast in one point")
        identities.add(identity)


def normalize_key(value: str) -> str:
    value = " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())
    if ":" not in value or value.split(":", 1)[0] not in PREFIXES or not value.split(":", 1)[1]:
        raise ValidationError("point key must be word:, phrase:, syntax:, option_type: or error_type:")
    return value


def key_hash(value: str) -> str:
    return object_sha256(normalize_key(value))


def publish_personal_summaries(repo: Path, state: Path, document: dict[str, Any], *,
                                formal_receipt: dict[str, Any] | None = None,
                                bootstrap_existing_formal: bool = False,
                                source_review: dict[str, Any] | None = None) -> dict[str, Any]:
    """Publish after verified formal intake, or explicitly bootstrap formal history.

    Local curation supplies semantics. Raw Capture never calls this function.
    Merge episodes by identity, retaining earlier contributions from other parts.
    """
    if not bootstrap_existing_formal and source_review is None and (not formal_receipt or formal_receipt.get("status") != "PASS"):
        raise ValidationError("personal summaries update only after verified formal intake")
    if (document.get("schema_version") != SCHEMA or not isinstance(document.get("records"), list)
            or (not document["records"] and not document.get("coverage"))):
        raise ValidationError("point summary input must contain explicit records")
    from .memory_lifecycle import completed_summary_replay
    replay = completed_summary_replay(state, document)
    if replay is not None:
        return replay
    review = None
    if source_review is not None:
        if formal_receipt is not None or bootstrap_existing_formal:
            raise ValidationError("raw source review must not masquerade as formal intake or bootstrap")
        from .summary_source_review import SummarySourceReview
        review = SummarySourceReview(repo, source_review, document)
    root = state / "personal-summary"
    root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(root / ".publish.lock"):
        replay = completed_summary_replay(state, document)
        if replay is not None:
            return replay
        index_path = root / "current.json"
        index = load_json(index_path) if index_path.exists() else {"schema_version": INDEX, "routes": {}}
        if index.get("schema_version") != INDEX:
            raise ValidationError("personal summary index version mismatch")
        routes = dict(index["routes"])
        # A maintenance split may cite a previously published observation. Freeze
        # this set before the batch so pending/orphan/new objects cannot become
        # their own authority merely by being present in the object directory.
        published = {p["sha256"]: p for p in routes.values()}
        verified_parents = {}
        updated = []
        prepared_records = {}
        for supplied in document["records"]:
            canonical = normalize_key(supplied["key"])
            pointer = routes.get(key_hash(canonical))
            old = _read_record(root, pointer) if pointer else None
            if old and old["key"] != canonical:
                raise ValidationError("canonical key collides with an existing alias")
            if review is not None and "expected_summary_sha256" in supplied:
                if supplied["expected_summary_sha256"] != (pointer["sha256"] if pointer else None):
                    raise ValidationError("current summary changed since original-source review")
            # Explicit maintenance changes teaching projections only. It binds
            # to the current immutable object and never edits/replays learning
            # episodes or revalidates mutable whole-file history as new evidence.
            maintenance = "base_summary_sha256" in supplied
            if maintenance:
                if (not bootstrap_existing_formal or not old
                        or supplied["base_summary_sha256"] != pointer["sha256"]):
                    raise ValidationError("maintenance requires the exact current summary hash")
                if set(supplied) - {"key", "base_summary_sha256", "teaching_hint", "aliases", "contrasts", "latest_episode_id"}:
                    raise ValidationError("maintenance can update teaching projections only")
                record = {**old, **{k: v for k, v in supplied.items() if k != "base_summary_sha256"}}
                incoming_episodes = []
            else:
                record = dict(supplied)
                record.pop("expected_summary_sha256", None)
                incoming_episodes = record.get("episodes", [])
                if old and "contrasts" not in record and old.get("contrasts"):
                    record["contrasts"] = old["contrasts"]
            record["key"] = canonical
            if record.get("visibility") not in {"answer_free", "protected"}:
                raise ValidationError("summary requires explicit answer visibility")
            if not record.get("teaching_hint") or not record.get("episodes"):
                raise ValidationError("summary needs a teaching hint and actual observations")
            episodes = {e["episode_id"]: e for e in old["episodes"]} if old else {}
            new_ids = set()
            for episode in incoming_episodes:
                for field in ("episode_id", "first_attempt", "correction", "later_observation", "evidence"):
                    if not episode.get(field):
                        raise ValidationError(f"summary episode requires {field}; explicitly mark unobserved results")
                eid = episode["episode_id"]
                if not isinstance(eid, str) or not eid.strip() or not isinstance(episode["evidence"], list):
                    raise ValidationError("summary episode requires a stable ID and evidence array")
                if eid in episodes and episodes[eid] != episode:
                    raise IdempotencyConflict("existing observation cannot be silently rewritten")
                if eid not in episodes:
                    if len(canonical_bytes(episode)) > EPISODE_BYTES:
                        raise ValidationError("one summary observation exceeds 12 KiB; retain the full source and cite it")
                    new_ids.add(eid)
                follow_index = _followup_index(episode)
                archive_refs = any(_is_archive_evidence(ref) for ref in episode["evidence"])
                if (review is not None or bootstrap_existing_formal) and archive_refs:
                    raise ValidationError("archive summary evidence is available only after normal formal intake")
                if review is not None:
                    review.validate_episode(episode)
                archive_user_evidence = False
                followup_text = []
                for i, ref in enumerate([] if review is not None else episode["evidence"]):
                    if _is_archive_evidence(ref):
                        archive_user_evidence |= _validate_archive_evidence(
                            repo, state, ref, formal_receipt, attempt=episode["first_attempt"],
                            followup_text=followup_text if i == follow_index else None)
                        continue
                    target = (repo / ref["path"]).resolve(strict=True)
                    relative = target.relative_to(repo.resolve())
                    if bootstrap_existing_formal and relative.parts[0] not in {"articles", "bank"}:
                        digest = target.stem
                        if target.parent != (root / "objects").resolve() or digest not in published:
                            raise ValidationError("bootstrap reads formal article/bank records or current published observations only")
                        if digest not in verified_parents:
                            verified_parents[digest] = _read_record(root, published[digest])
                        parent = verified_parents[digest]
                        source_id = episode.get("source_episode_id")
                        source = next((e for e in parent["episodes"] if e["episode_id"] == source_id), None)
                        if source is None:
                            raise ValidationError("summary refinement must identify an existing source episode")
                        if ref.get("locator") != f"episodes/{parent['episodes'].index(source)}":
                            raise ValidationError("summary refinement locator must match its source episode")
                        if episode.get("study_date") != source.get("study_date"):
                            raise ValidationError("summary refinement must preserve the original study date")
                        if parent["visibility"] == "protected" and record["visibility"] != "protected":
                            raise ValidationError("summary refinement cannot remove source answer protection")
                    if file_sha256(target) != ref["sha256"]:
                        raise ValidationError("summary evidence changed")
                    if i == follow_index:
                        followup_text.append(_user_message_text(target, ref.get("locator")) if target.suffix == ".json" else None)
                if archive_refs and not archive_user_evidence:
                    raise ValidationError("archive summary episode requires an exact original user message")
                if follow_index is not None:
                    if review is not None:
                        ref = episode["evidence"][follow_index]
                        target = (repo / ref["path"]).resolve(strict=True)
                        if ref.get("locator") in review.sources[target]["locators"] and target.suffix == ".json":
                            followup_text.append(_user_message_text(target, ref["locator"]))
                    _validate_followup_text(episode, next(iter(followup_text), None))
                episodes[eid] = episode
            record["episodes"] = list(episodes.values())
            _validate_contrasts(record)
            if record.get("latest_episode_id") is not None and record["latest_episode_id"] not in episodes:
                raise ValidationError("latest observation must identify a preserved episode")
            aliases = set(old.get("aliases", []) if old else []) | set(record.get("aliases", []))
            record["aliases"] = sorted({normalize_key(a) for a in aliases} - {canonical})
            if old and old.get("visibility") == "protected":
                record["visibility"] = "protected"
            group, _ = select_observations(record["episodes"])
            group_ids = {ep["episode_id"] for ep in group}
            if old and "latest_episode_id" not in supplied:
                if old.get("latest_episode_id") in group_ids:
                    record["latest_episode_id"] = old["latest_episode_id"]
                if new_ids and not new_ids & group_ids:
                    record["teaching_hint"] = old["teaching_hint"]
            # This proof is derived from original turns, never accepted from
            # the authored summary. Preserve a still applicable prior proof.
            record.pop("latest_episode_order", None)
            preferred = record.get("latest_episode_id")
            if preferred in group_ids and len(group) > 1:
                previous_proof = (old or {}).get("latest_episode_order")
                if (isinstance(previous_proof, dict) and previous_proof.get("episode_id") == preferred
                        and previous_proof.get("episode_ids") == sorted(group_ids)):
                    record["latest_episode_order"] = previous_proof
                elif "latest_episode_id" in supplied:
                    proof = _verified_latest_order(repo, preferred, group)
                    if proof:
                        record["latest_episode_order"] = proof
            if record["visibility"] == "answer_free" and point_answer_risk(record):
                raise ValidationError("answer-free summary contains a concrete answer or answer-analysis reference; review and use protected")
            record["schema_version"] = SCHEMA
            record["publication_basis"] = ("explicit_raw_source_review:" + review.review_id if review else
                                           "existing_formal_maintenance" if maintenance else
                                           "existing_formal_bootstrap" if bootstrap_existing_formal
                                           else formal_receipt["receipt_id"])
            if maintenance:
                record["maintenance_parent_sha256"] = pointer["sha256"]
            data = canonical_bytes(record)
            if len(canonical_bytes({k: v for k, v in record.items() if k != "episodes"})) > TEACHING_OBJECT_BYTES:
                raise ValidationError(f"point teaching metadata exceeds 12 KiB: {canonical}; retain full observations and shorten the projection")
            digest = object_sha256(record)
            target = root / "objects" / f"{digest}.json"
            target.parent.mkdir(exist_ok=True)
            if target.exists() and target.read_bytes() != data:
                raise ValidationError("summary object hash collision or corruption")
            if not target.exists():
                atomic_write_bytes(target, data)
            route = {"key": canonical, "sha256": digest}
            if len(data) > TEACHING_OBJECT_BYTES:
                route["teaching_sha256"] = object_sha256(teaching_header(record))
            for alias in [canonical, *record["aliases"]]:
                hashed = key_hash(alias)
                previous = routes.get(hashed)
                if previous and previous["key"] != canonical:
                    raise ValidationError(f"ambiguous point alias: {alias}")
                routes[hashed] = route
            updated.append(canonical)
            prepared_records[canonical] = record
        from .memory_lifecycle import preflight_summary_memory, stage_summary_memory
        memory_plan = preflight_summary_memory(repo, state, document, prepared_records,
            formal_receipt=formal_receipt, review=review, bootstrap=bootstrap_existing_formal)
        stage_summary_memory(state, memory_plan)
        atomic_write_json(index_path, {"schema_version": INDEX, "routes": routes})
    from .memory_lifecycle import finish_summary_memory
    memory = finish_summary_memory(repo, state, memory_plan["publication_id"] if memory_plan else None)
    return {"status": "FORMAL_COMMITTED_MEMORY_PENDING" if memory and memory.get("status") != "ready" else "published",
            "updated_keys": updated, "route_count": len(routes), "index_path": str(index_path),
            "formal_write_count": 0,
            **({"memory": memory, "pending_component": memory.get("pending_component")} if memory else {})}


def _read_record(root: Path, pointer: dict[str, Any], *, bounded: bool = False) -> dict[str, Any]:
    digest = pointer["sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValidationError("invalid summary object identity")
    path = root / "objects" / f"{digest}.json"
    if bounded and path.stat().st_size > TEACHING_OBJECT_BYTES:
        raise ValidationError("long point requires the bounded memory projection")
    record = load_json(path)
    if object_sha256(record) != digest or record["key"] != pointer["key"] or record.get("schema_version") != SCHEMA:
        raise ValidationError("summary object binding changed")
    if pointer.get("teaching_sha256") is not None and object_sha256(teaching_header(record)) != pointer["teaching_sha256"]:
        raise ValidationError("summary teaching header binding changed")
    return record


def _fit_query_budget(result: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Fit the current group together, then whole historical observations.

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
            group, ordered = select_observations(matched["episodes"])
            if preferred := matched.get("latest_episode_id"):
                group = [ep for ep in ordered if ep["episode_id"] == preferred]
            ids = {ep["episode_id"] for ep in group}
            pending.append((point, [group] + [[ep] for ep in ordered if ep["episode_id"] not in ids]))
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
        for point, groups in pending:
            if not groups:
                continue
            batch = groups[0]
            point["episodes"].extend(batch)
            point["episodes_returned"] += len(batch)
            point["episodes_omitted"] -= len(batch)
            if len(canonical_bytes(compact)) <= max_bytes:
                next_pending.append((point, groups[1:]))
            else:
                del point["episodes"][-len(batch):]
                point["episodes_returned"] -= len(batch)
                point["episodes_omitted"] += len(batch)
        pending = next_pending
    return compact


def _latest_episode(record: dict[str, Any]) -> dict[str, Any]:
    group, _ = select_observations(record["episodes"], record)
    return group[0] if len(group) == 1 else {}


def _fit_teaching_budget(result: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Keep each teaching point whole; never hide the latest observation's caveat."""
    if len(canonical_bytes(result)) <= max_bytes:
        return result
    compact = {**result, "status": "partial", "matched": [], "misses": [], "unavailable": [],
               "diagnostics_omitted": len(result["matched"]) + len(result["misses"]) + len(result["unavailable"])}
    unavailable = list(result["unavailable"])
    for point in result["matched"]:
        compact["matched"].append(point)
        compact["diagnostics_omitted"] -= 1
        if len(canonical_bytes(compact)) > max_bytes:
            compact["matched"].pop()
            compact["diagnostics_omitted"] += 1
            unavailable.append({"key": point["key"], "reason": "response_budget"})
    for field, entries in (("unavailable", unavailable), ("misses", result["misses"])):
        for entry in entries:
            compact[field].append(entry)
            compact["diagnostics_omitted"] -= 1
            if len(canonical_bytes(compact)) > max_bytes:
                compact[field].pop()
                compact["diagnostics_omitted"] += 1
    return compact


def query_personal_summary(state: Path, *, keys: list[str], question_review: bool = False,
                           max_bytes: int = 8192, view: str = "full") -> dict[str, Any]:
    keys = list(dict.fromkeys(normalize_key(key) for key in keys))
    if not 1 <= len(keys) <= 8 or not 512 <= max_bytes <= 16384:
        raise ValidationError("query supports 1..8 point keys and a 512..16384 byte response")
    if view not in {"full", "teaching"}:
        raise ValidationError("summary view must be full or teaching")
    fit_budget = _fit_teaching_budget if view == "teaching" else _fit_query_budget
    root = state / "personal-summary"
    path = root / "current.json"
    result = {"status": "ready", "matched": [], "misses": [], "unavailable": [],
              "formal_write_count": 0, "history_scanned": False}
    if view == "teaching":
        result["view"] = "teaching"
    if not path.exists():
        return fit_budget({**result, "misses": keys}, max_bytes)
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
            if view == "teaching" and pointer.get("teaching_sha256"):
                result["unavailable"].append({"key": key, "reason": "use_bounded_memory_query"})
                continue
            record = _read_record(root, pointer, bounded=view == "teaching")
            from .memory_lifecycle import summary_read_block, memory_enabled, read_lifecycle
            enabled = memory_enabled(state)
            blocked = summary_read_block(state, record["key"], pointer["sha256"],
                                         [e["episode_id"] for e in record.get("episodes", [])])
            if blocked:
                result["unavailable"].append({"key": key, "reason": blocked})
                continue
            if enabled:
                invalidated = set(read_lifecycle(state)["invalidated_episode_ids"])
                if invalidated:
                    record = {**record, "episodes": [e for e in record["episodes"] if e["episode_id"] not in invalidated],
                              "contrasts": [c for c in record.get("contrasts", [])
                                            if not set(c.get("source_episode_ids", [])) & invalidated]}
                    if not record["episodes"]:
                        result["unavailable"].append({"key": key, "reason": "no_valid_learning_evidence"})
                        continue
                    if record.get("latest_episode_id") in invalidated:
                        record.pop("latest_episode_id", None)
            if record["visibility"] != "answer_free" and (not question_review or enabled):
                result["unavailable"].append({"key": key, "reason": "protected_use_scoped_memory_query" if enabled else "protected"})
                continue
            if record["visibility"] == "answer_free" and point_answer_risk(record):
                result["unavailable"].append({"key": key, "reason": "protected_use_scoped_memory_query"})
                continue
            if enabled and ((state / "memory/current.json").exists() or (state / "memory/generations").is_dir()):
                # The legacy response still has its old shape, but cannot
                # bypass scoped/inherited protection or a source conflict.
                from .memory_store import open_memory_projection
                db, _ = open_memory_projection(state)
                try:
                    projected = db.execute("SELECT summary_sha256,answer_safe,question_ids_json FROM points WHERE point_key=?",
                                           (record["key"],)).fetchone()
                finally:
                    db.close()
                if not projected or projected[0] != pointer["sha256"]:
                    result["unavailable"].append({"key": key, "reason": "summary_generation_pending"})
                    continue
                if not projected[1] or point_answer_risk(record, question_bound=bool(json.loads(projected[2]))):
                    result["unavailable"].append({"key": key, "reason": "use_scoped_memory_query"})
                    continue
            elif enabled and (record["key"].startswith("option_type:") or record.get("maintenance_parent_sha256")
                    or any((state.parent / ref["path"]).resolve().parent == (root / "objects").resolve()
                           for ep in record["episodes"] for ref in ep.get("evidence", [])
                           if isinstance(ref.get("path"), str))):
                result["unavailable"].append({"key": key, "reason": "scope_index_unavailable"})
                continue
            group, _ = select_observations(record["episodes"], record)
            latest = group[0] if len(group) == 1 else {}
            if view == "teaching":
                if not latest:
                    result["unavailable"].append({"key": record["key"], **ambiguous_observation_order(group)})
                    continue
                point = {"key": record["key"], "summary_sha256": pointer["sha256"],
                         "teaching_hint": record["teaching_hint"],
                         "latest_observation": {k: latest[k] for k in
                            ("study_date", "first_attempt", "correction", "later_observation", "hint_dependence",
                             "first_attempt_hint_dependence", "followup_status") if k in latest}}
                if record.get("contrasts"):
                    point["contrasts"] = [{k: v for k, v in c.items()
                                            if k not in {"source_episode_ids", "evidence_note"}}
                                          for c in record["contrasts"]]
                result["matched"].append(point)
                continue
            # Evidence is verified at formal publication, never reread in tutoring.
            result["matched"].append({"key": record["key"], "summary_sha256": pointer["sha256"],
                                      **({"teaching_hint": record["teaching_hint"]} if latest else
                                         {"observation_order": ambiguous_observation_order(group)}),
                                      **({"latest_episode_id": record["latest_episode_id"]}
                                         if record.get("latest_episode_id") == latest.get("episode_id") and latest else {}),
                                      **({"contrasts": record["contrasts"]} if record.get("contrasts") else {}),
                                      "episodes": [{k: v for k, v in e.items() if k != "evidence"}
                                                   for e in record["episodes"]]})
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ValidationError):
            result["unavailable"].append({"key": key, "reason": "invalid_or_missing_summary"})
    ambiguous_count = (sum(item["reason"] == OBSERVATION_ORDER_AMBIGUOUS for item in result["unavailable"])
                       + sum("observation_order" in point for point in result["matched"]))
    if ambiguous_count:
        result["observation_order_ambiguous_count"] = ambiguous_count
        result["status"] = "partial" if result["matched"] else "unavailable"
    return fit_budget(result, max_bytes)
