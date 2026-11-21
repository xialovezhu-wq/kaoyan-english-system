"""Recoverable publication of derived memory, never a second formal writer.

Only the existing formal writer and explicitly reviewed summary publisher call
the mutation functions here. Original episodes and learning ledgers are never
edited. Runtime readers consume the small lifecycle projection, not this log.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import IdempotencyConflict, ValidationError
from .util import atomic_write_json, bytes_sha256, canonical_bytes, exclusive_lock, file_sha256, load_json, object_sha256, utc_now

SCHEMA = "english_memory_lifecycle_v1"
_RID = re.compile(r"EN-RECEIPT-[A-F0-9]{16}$")


def memory_enabled(state: Path) -> bool:
    path = state / "memory/enabled.json"
    return path.is_file() and load_json(path).get("enabled") is True


def read_lifecycle(state: Path) -> dict[str, Any]:
    path = state / "memory/lifecycle.json"
    value = load_json(path) if path.is_file() else {}
    return {"schema_version": SCHEMA, "pending_keys": {}, "pending_receipts": {}, "pending_transactions": {},
            "invalidated_episode_ids": [], "invalidated_claim_ids": [],
            "invalidated_summary_keys": [],
            "accepted_summary_hashes": {}, "global_pending": False, **value}


def _save(state: Path, value: dict[str, Any]) -> None:
    value["revision"] = int(value.get("revision", 0)) + 1
    atomic_write_json(state / "memory/lifecycle.json", value)


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _required(state: Path, receipt: dict[str, Any]) -> bool:
    if not memory_enabled(state):
        return False
    config = load_json(state / "memory/enabled.json")
    # Enabling a new derived layer never invalidates old formal terminals.
    if config.get("activated_at") and receipt.get("completed_at"):
        return _instant(receipt["completed_at"]) >= _instant(config["activated_at"])
    return True


def _current_records(state: Path) -> dict[str, dict[str, Any]]:
    from .personal_summary import _read_record
    root = state / "personal-summary"
    if not (root / "current.json").is_file():
        return {}
    index = load_json(root / "current.json")
    pointers = {row["key"]: row for row in index.get("routes", {}).values()}
    return {key: _read_record(root, pointer) for key, pointer in pointers.items()}


def _summary_hashes(state: Path) -> dict[str, str]:
    path = state / "personal-summary/current.json"
    if not path.is_file():
        return {}
    return {row["key"]: row["sha256"] for row in load_json(path).get("routes", {}).values()}


def completed_summary_replay(state: Path, document: dict[str, Any]) -> dict[str, Any] | None:
    """A completed input is a receipt lookup, never permission to rewind current."""
    digest = object_sha256(document)
    pid = "EN-MEMORY-" + digest[:24].upper()
    path = state / "memory/publications" / f"{pid}.json"
    if not path.is_file():
        return None
    row = load_json(path)
    if row.get("input_sha256") != digest:
        raise IdempotencyConflict("memory publication ID conflict")
    if row.get("status") != "ready" or pid in read_lifecycle(state).get("pending_publications", {}):
        return None
    current_path = state / "personal-summary/current.json"
    return {"status": "published", "idempotent_replay": True, "updated_keys": [],
            "index_path": str(current_path), "current_index_sha256": file_sha256(current_path) if current_path.is_file() else None,
            "historical_publication_id": pid, "historical_receipt_path": str(path),
            "current_pointer_rewritten": False, "formal_write_count": 0}


def _receipt_path(state: Path, receipt: dict[str, Any]) -> Path | None:
    batch = re.fullmatch(r"EN-BATCH-(\d{8})-[A-F0-9]{12}", str(receipt.get("batch_id", "")))
    if batch:
        date = batch[1]
        path = state / "receipts/nightly" / f"{date[:4]}-{date[4:6]}-{date[6:]}" / f"{receipt['receipt_id']}.json"
        if path.is_file():
            if load_json(path) != receipt:
                raise ValidationError("canonical writer receipt differs from memory closeout")
            return path
    return None


def _refresh_pending(value: dict[str, Any]) -> None:
    pending: dict[str, dict[str, Any]] = {}
    global_pending = False
    for rid, row in value.get("pending_transactions", {}).items():
        # An unresolved formal transaction is not an accepted learning event.
        # It is only an instruction to stop serving potentially stale memory.
        global_pending |= bool(row.get("unmapped_scope", True))
        for key in row.get("affected_keys", []):
            entry = pending.setdefault(key, {"reason": "formal_transaction", "transaction_ids": []})
            entry.setdefault("transaction_ids", []).append(rid)
    for rid, row in value.get("pending_receipts", {}).items():
        if row.get("status") == "ready":
            continue
        global_pending |= bool(row.get("unmapped_scope"))
        for key in row.get("affected_keys", []):
            entry = pending.setdefault(key, {"reason": row.get("pending_component", "personal_summary"), "receipt_ids": []})
            entry["receipt_ids"].append(rid)
    for tid, row in value.get("pending_publications", {}).items():
        for key in row.get("keys", []):
            entry = pending.setdefault(key, {"reason": row.get("pending_component", "memory_index"), "publication_ids": []})
            entry.setdefault("publication_ids", []).append(tid)
    value["pending_keys"] = pending
    value["global_pending"] = global_pending


def _transaction_binding(repo: Path, state: Path, transaction_path: Path, transaction: dict[str, Any]) -> dict[str, Any]:
    rid, batch, day = (str(transaction.get(name, "")) for name in ("receipt_id", "batch_id", "study_date"))
    if (not _RID.fullmatch(rid) or not re.fullmatch(r"EN-BATCH-\d{8}-[A-F0-9]{12}", batch)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)):
        raise ValidationError("memory invalidation requires a canonical formal transaction identity")
    expected = state / "nightly" / day / batch / "transactions" / rid / "transaction.json"
    if transaction_path.resolve() != expected.resolve():
        raise ValidationError("memory invalidation transaction path is not canonical")
    allowed = {name: str((repo / relative).resolve()) for name, relative in {
        "master_bank": "bank/master_bank.csv", "mastered_items": "bank/mastered_items.csv",
        "sentence_patterns": "bank/sentence_patterns.md", "review_exclusion_ledger": "bank/review_exclusion_ledger.jsonl",
        "learning_events": "bank/learning_events.jsonl"}.items()}
    paths = transaction.get("formal_paths", {})
    if not paths or any(name not in allowed or path != allowed[name] for name, path in paths.items()):
        raise ValidationError("memory invalidation formal paths differ from the owning repository")
    if set(transaction.get("prehashes", {})) != set(paths) or set(transaction.get("proposed_hashes", {})) != set(paths):
        raise ValidationError("memory invalidation requires all pre/proposed hashes")
    return {"receipt_id": rid, "batch_id": batch, "study_date": day,
            "transaction_path": str(expected.resolve()), "formal_paths": paths,
            "prehashes": transaction["prehashes"], "proposed_hashes": transaction["proposed_hashes"]}


def mark_transaction_memory_pending(repo: Path, state: Path, transaction_path: Path, transaction: dict[str, Any], *,
                                    manifest_sha256: str, actions_sha256: str,
                                    package_ids: list[str], package_sha256s: list[str],
                                    action_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Called under the formal lock, before the first formal-file replacement."""
    if not memory_enabled(state):
        return {"status": "NOT_ENABLED", "formal_write_count": 0}
    if transaction.get("status") != "prepared":
        raise ValidationError("memory invalidation must precede the formal replacements")
    binding = _transaction_binding(repo, state, transaction_path, transaction)
    binding.update(manifest_sha256=manifest_sha256, actions_sha256=actions_sha256,
                   package_ids=list(package_ids), package_sha256s=list(package_sha256s))
    try:
        affected, unmapped = _affected_keys(repo, state, {"action_results": action_results,
            "package_ids": package_ids, "formal_files_changed": transaction["prehashes"] != transaction["proposed_hashes"]})
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        # A broken derived index cannot authorize serving old memory, but also
        # need not prevent otherwise valid formal data from committing.
        affected, unmapped = [], True
    # Even a fully identified change can affect uncompiled semantic judgments.
    # The writer's explicit coverage, not this mechanical intent, closes scope.
    row = {"schema_version": "english_memory_formal_invalidation_intent_v1", "binding": binding,
           "binding_sha256": object_sha256(binding), "affected_keys": affected,
           "unmapped_scope": unmapped, "reason": "formal_transaction_unresolved", "formal_evidence": False}
    with exclusive_lock(state / "memory/.lifecycle.lock"):
        lifecycle = read_lifecycle(state)
        previous = lifecycle["pending_transactions"].get(transaction["receipt_id"])
        if previous and previous != row:
            raise IdempotencyConflict("formal memory invalidation intent changed")
        if not previous:
            lifecycle["pending_transactions"][transaction["receipt_id"]] = row
            _refresh_pending(lifecycle)
            _save(state, lifecycle)
    return {"status": "pending", "pending_component": "formal_transaction", "receipt_id": transaction["receipt_id"],
            "binding_sha256": row["binding_sha256"], "formal_write_count": 0}


def release_rolled_back_memory_intent(repo: Path, state: Path, transaction_path: Path,
                                      transaction: dict[str, Any]) -> dict[str, Any]:
    """Release only a verified rollback; unknown or committed states stay gated."""
    if not memory_enabled(state):
        return {"status": "NOT_ENABLED", "formal_write_count": 0}
    if transaction.get("status") not in {"rolled_back", "recovered_rollback"}:
        raise ValidationError("only a confirmed rollback can clear a formal invalidation intent")
    binding = _transaction_binding(repo, state, transaction_path, transaction)
    current = {name: file_sha256(Path(path)) if Path(path).is_file() else bytes_sha256(b"")
               for name, path in binding["formal_paths"].items()}
    if current != binding["prehashes"]:
        raise ValidationError("formal rollback is not verified against its original prehashes")
    with exclusive_lock(state / "memory/.lifecycle.lock"):
        lifecycle = read_lifecycle(state)
        previous = lifecycle["pending_transactions"].get(transaction["receipt_id"])
        if previous is None:
            return {"status": "NOT_REQUIRED", "formal_write_count": 0}
        if any(previous["binding"].get(k) != v for k, v in binding.items()):
            raise ValidationError("rollback does not bind the pending memory transaction")
        lifecycle["pending_transactions"].pop(transaction["receipt_id"])
        _refresh_pending(lifecycle)
        _save(state, lifecycle)
    return {"status": "rolled_back", "receipt_id": transaction["receipt_id"], "formal_write_count": 0}


def _adopt_transaction_intent(lifecycle: dict[str, Any], receipt: dict[str, Any]) -> None:
    intent = lifecycle.get("pending_transactions", {}).get(receipt["receipt_id"])
    if intent is None:
        return
    binding = intent["binding"]
    checks = {"batch_id": receipt.get("batch_id"), "manifest_sha256": receipt.get("manifest_sha256"),
              "actions_sha256": receipt.get("actions_sha256"), "package_ids": receipt.get("package_ids", []),
              "package_sha256s": receipt.get("package_sha256s", []), "prehashes": receipt.get("formal_prehashes"),
              "proposed_hashes": receipt.get("formal_posthashes")}
    if object_sha256(binding) != intent.get("binding_sha256") or any(binding.get(k) != v for k, v in checks.items()):
        raise ValidationError("formal receipt cannot adopt a different memory invalidation intent")
    lifecycle["pending_transactions"].pop(receipt["receipt_id"])


def _affected_keys(repo: Path, state: Path, receipt: dict[str, Any]) -> tuple[list[str], bool]:
    from .personal_summary import normalize_key
    records = _current_records(state)
    assigned = {str(row.get("assigned_id")) for row in receipt.get("action_results", [])
                if row.get("result") in {"applied", "skipped"} and row.get("assigned_id")}
    packages = set(receipt.get("package_ids", []))
    candidates: set[str] = set()
    path = repo / "bank/master_bank.csv"
    if path.is_file() and assigned:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("id") in assigned and row.get("item"):
                    item = row["item"]
                    for prefix in ("word", "phrase"):
                        candidates.add(normalize_key(prefix + ":" + item))
    matched = {r["writing_binding"]["point_key"] for r in receipt.get("action_results", [])
               if r.get("writing_contract")=="english-writing-observation/2" and r.get("result") in {"applied","skipped"}}
    for key, record in records.items():
        if {key, *record.get("aliases", [])} & candidates:
            matched.add(key)
        for episode in record.get("episodes", []):
            if assigned & set(episode.get("formal_ids", [])):
                matched.add(key)
            for ref in episode.get("evidence", []):
                if ref.get("package_id") in packages or any(pid in str(ref.get("path", "")) for pid in packages):
                    matched.add(key)
    # A new source can introduce a new point that no reverse index knows yet.
    # Only explicit author coverage can close that uncertainty.
    return sorted(matched), bool(packages) or bool(receipt.get("formal_files_changed") and not matched)


def mark_formal_memory_pending(repo: Path, state: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    if not _required(state, receipt):
        return {"status": "LEGACY_NOT_REQUIRED" if memory_enabled(state) else "NOT_ENABLED", "pending_component": None}
    rid = str(receipt.get("receipt_id", ""))
    if (not _RID.fullmatch(rid) or receipt.get("mode") != "apply"
            or receipt.get("status") not in {"APPLIED", "PARTIAL", "NO_ACTION"}):
        raise ValidationError("memory closeout requires an actual successful writer receipt")
    completed = {pid: sha for pid, sha in zip(receipt.get("package_ids", []), receipt.get("package_sha256s", []))
                 if receipt.get("package_dispositions", {}).get(pid) == "completed"}
    canonical = _receipt_path(state, receipt)
    binding = {"receipt_id": rid, "receipt_content_sha256": object_sha256(receipt),
               "writer_receipt_sha256": file_sha256(canonical) if canonical else None,
               "writer_receipt_path": str(canonical) if canonical else None,
               "formal_posthashes": receipt.get("formal_posthashes", {}),
               "completed_packages": completed, "completed_at": receipt.get("completed_at")}
    try:
        affected, unmapped = _affected_keys(repo, state, receipt)
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        affected, unmapped = [], True
    with exclusive_lock(state / "memory/.lifecycle.lock"):
        lifecycle = read_lifecycle(state)
        previous = lifecycle["pending_receipts"].get(rid)
        if previous:
            if previous["binding"] != binding:
                raise ValidationError("memory receipt binding changed")
            if rid in lifecycle.get("pending_transactions", {}):
                _adopt_transaction_intent(lifecycle, receipt)
                _refresh_pending(lifecycle)
                _save(state, lifecycle)
            return _public_receipt(previous)
        row = {"binding": binding, "status": "pending", "pending_component": "personal_summary",
               "affected_keys": affected, "unmapped_scope": unmapped, "coverage": {}, "publications": []}
        lifecycle["pending_receipts"][rid] = row
        _adopt_transaction_intent(lifecycle, receipt)
        _refresh_pending(lifecycle)
        _save(state, lifecycle)
    return _public_receipt(row)


def _public_receipt(row: dict[str, Any]) -> dict[str, Any]:
    return {"status": row["status"], "pending_component": row.get("pending_component"),
            "receipt_id": row["binding"]["receipt_id"], "affected_keys": row.get("affected_keys", []),
            "covered_packages": sorted(row.get("coverage", {})),
            "required_packages": sorted(row["binding"].get("completed_packages", {})),
            "memory_generation": row.get("memory_generation"), "formal_write_count": 0}


def memory_closeout_status(state: Path, receipt_id: str | None = None) -> dict[str, Any]:
    if not memory_enabled(state):
        return {"status": "NOT_ENABLED", "pending_component": None, "formal_write_count": 0}
    lifecycle = read_lifecycle(state)
    if receipt_id is not None:
        if receipt_id in lifecycle.get("pending_transactions", {}):
            return {"status": "pending", "pending_component": "formal_transaction", "receipt_id": receipt_id,
                    "formal_evidence": False, "formal_write_count": 0}
        row = lifecycle["pending_receipts"].get(receipt_id)
        unfinished = [item for item in lifecycle.get("pending_publications", {}).values() if item.get("receipt_id") == receipt_id]
        if unfinished and (not row or row.get("status") == "ready"):
            return {"status": "pending", "pending_component": unfinished[0].get("pending_component", "personal_summary"),
                    "receipt_id": receipt_id, "formal_write_count": 0}
        result = _public_receipt(row) if row else {"status": "LEGACY_NOT_REQUIRED", "pending_component": None,
                                                  "receipt_id": receipt_id, "formal_write_count": 0}
        from .sentence_visuals import stage_status
        teaching = stage_status(state, receipt_id)
        if teaching:
            result["sentence_teaching"] = teaching
        return result
    pending = [_public_receipt(row) for row in lifecycle["pending_receipts"].values() if row.get("status") != "ready"]
    publications = list(lifecycle.get("pending_publications", {}).values())
    transactions = list(lifecycle.get("pending_transactions", {}))
    return {"status": "pending" if pending or publications or transactions or lifecycle["global_pending"] else "ready",
            "pending_component": "formal_transaction" if transactions else pending[0]["pending_component"] if pending else publications[0].get("pending_component") if publications else "personal_summary" if lifecycle["global_pending"] else None,
            "receipts": pending, "pending_keys": sorted(lifecycle["pending_keys"]),
            "pending_transaction_ids": transactions,
            "global_pending": lifecycle["global_pending"], "formal_write_count": 0}


def _verified_authority(formal_receipt: dict[str, Any] | None, review: Any) -> bool:
    return review is not None or bool(formal_receipt and formal_receipt.get("schema_version") == "english_formal_receipt_validation_v1"
        and formal_receipt.get("status") == "PASS" and formal_receipt.get("subject") == "english"
        and re.fullmatch(r"[a-f0-9]{64}", str(formal_receipt.get("writer_receipt_sha256", ""))))


def _learner_episode(repo: Path, state: Path, episode: dict[str, Any], formal_receipt: dict[str, Any] | None, review: Any) -> bool:
    if review is not None:
        review.validate_episode(episode)
        return True
    from .personal_summary import _is_archive_evidence, _validate_archive_evidence, _validate_archive_locator
    for ref in episode.get("evidence", []):
        if _is_archive_evidence(ref):
            if _validate_archive_evidence(repo, state, ref, formal_receipt, attempt=episode["first_attempt"]):
                return True
            continue
        path = (repo / ref.get("path", "")).resolve()
        if not path.is_file() or file_sha256(path) != ref.get("sha256") or path.name != "conversation.json":
            continue
        manifest_path = path.parent / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = load_json(manifest_path)
        bound = dict(zip((formal_receipt or {}).get("package_ids", []), (formal_receipt or {}).get("package_sha256s", [])))
        if bound.get(manifest.get("package_id")) != manifest.get("package_canonical_sha256"):
            continue
        if _validate_archive_locator(path, ref.get("locator"), attempt=episode["first_attempt"]):
            return True
    return False


def _episode_covers_package(repo: Path, state: Path, episode: dict[str, Any], pid: str, digest: str) -> bool:
    for ref in episode.get("evidence", []):
        # These archive references were independently validated by the existing
        # summary publisher against the canonical writer and package pointer.
        if ref.get("package_id") == pid and ref.get("package_sha256") == digest:
            return True
        if not isinstance(ref.get("path"), str):
            continue
        path = (repo / ref["path"]).resolve()
        try:
            relative = path.relative_to((state / "packages").resolve())
        except ValueError:
            continue
        if len(relative.parts) < 3 or relative.parts[1] != pid:
            continue
        root = state / "packages" / relative.parts[0] / pid
        manifest = load_json(root / "manifest.json")
        member = Path(*relative.parts[2:]).as_posix()
        descriptor = manifest.get("files", {}).get(member, {})
        if (manifest.get("package_id") == pid and manifest.get("package_canonical_sha256") == digest
                and descriptor.get("sha256") == ref.get("sha256") and path.is_file()
                and file_sha256(path) == ref["sha256"]):
            return True
    return False


def _episode_withdrawal_closure(episodes, roots):
    withdrawn = set(roots)
    if withdrawn:
        while True:
            dependents = {eid for eid, owners in episodes.items() if eid not in withdrawn
                          and any(ep.get("source_episode_id") in withdrawn for _, ep in owners)}
            if not dependents:
                break
            withdrawn.update(dependents)
    return withdrawn


def preflight_summary_memory(repo: Path, state: Path, document: dict[str, Any], records: dict[str, dict[str, Any]], *,
                             formal_receipt: dict[str, Any] | None = None, review: Any = None,
                             bootstrap: bool = False) -> dict[str, Any] | None:
    """Validate every new derived mutation before changing the current pointer."""
    requested = any(document.get(k) for k in ("memory_claims", "memory_corrections", "coverage"))
    if not memory_enabled(state) and not requested:
        return None
    if not memory_enabled(state):
        raise ValidationError("derived memory must be enabled before publishing lifecycle changes")
    from .personal_summary import normalize_key
    claims_in = document.get("memory_claims", [])
    corrections = document.get("memory_corrections", [])
    coverage = document.get("coverage", [])
    if any(not isinstance(x, list) for x in (claims_in, corrections, coverage)):
        raise ValidationError("memory claims, corrections and coverage must be lists")
    if (claims_in or corrections or coverage) and (bootstrap or not _verified_authority(formal_receipt, review)):
        raise ValidationError("derived claims and corrections require verified formal or original-source review authority")
    old_records = _current_records(state)
    all_records = {**old_records, **records}
    episodes: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for key, record in all_records.items():
        for episode in record.get("episodes", []):
            episodes.setdefault(episode["episode_id"], []).append((key, episode))
    claims_path = state / "memory/claims.json"
    old_claims = load_json(claims_path).get("claims", {}) if claims_path.is_file() else {}
    claims = dict(old_claims)
    verified_episodes = set()

    def evidence(ids: Any) -> list[str]:
        if (not isinstance(ids, list) or not ids or any(not isinstance(e, str) or e not in episodes for e in ids)
                or len(ids) != len(set(ids))):
            raise ValidationError("derived memory must cite known unique learning episodes")
        for eid in ids:
            if eid not in verified_episodes:
                if not any(_learner_episode(repo, state, ep, formal_receipt, review) for _, ep in episodes[eid]):
                    raise ValidationError("derived memory requires exact original learner evidence")
                verified_episodes.add(eid)
        return ids

    def stable_id(value: Any, label: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[\w:.-]{1,200}", value, re.UNICODE):
            raise ValidationError(label + " requires a stable safe ID")
        return value

    seen_claims = set()
    for claim in claims_in:
        required = {"claim_id", "point_key", "content", "basis", "status", "evidence_episode_ids"}
        if not isinstance(claim, dict) or not required <= set(claim) or set(claim) - required - {"valid_from", "valid_to"}:
            raise ValidationError("memory claim fields are invalid")
        cid = stable_id(claim["claim_id"], "claim")
        if cid in seen_claims:
            raise ValidationError("duplicate claim ID in proposal")
        seen_claims.add(cid)
        if not isinstance(claim["point_key"], str):
            raise ValidationError("claim point key must be text")
        key = normalize_key(claim["point_key"])
        if key not in all_records or not isinstance(claim["content"], str) or not claim["content"].strip():
            raise ValidationError("memory claim needs a current point and content")
        if claim["basis"] not in {"user_explicit", "observed", "inference"} or claim["status"] not in {"candidate", "confirmed"}:
            raise ValidationError("memory claim basis/status is invalid")
        if claim["basis"] == "inference" and claim["status"] == "confirmed":
            raise ValidationError("an inference cannot become a confirmed learner fact")
        ids = evidence(claim["evidence_episode_ids"])
        point_episodes = {ep["episode_id"]: ep for ep in all_records[key]["episodes"]}
        if not set(ids) <= point_episodes.keys():
            raise ValidationError("claim evidence must belong to its point")
        if review is not None and (claim["basis"] == "user_explicit" or claim["status"] == "confirmed"):
            for eid in ids:
                review.validate_episode(point_episodes[eid], assertion=claim["content"])
        for field in ("valid_from", "valid_to"):
            if claim.get(field) is not None:
                if not isinstance(claim[field], str):
                    raise ValidationError("claim valid time must be an ISO date or instant")
                _instant(claim[field])
        if claim.get("valid_from") and claim.get("valid_to") and _instant(claim["valid_to"]) <= _instant(claim["valid_from"]):
            raise ValidationError("claim valid interval must be nonempty")
        value = {**claim, "point_key": key, "visibility": all_records[key]["visibility"]}
        if cid in old_claims:
            existing = {k: v for k, v in old_claims[cid].items() if k not in {"recorded_at", "publication_id"}}
            if existing != value:
                raise IdempotencyConflict("a stable claim cannot be silently rewritten")
        claims[cid] = value
    old_corrections: dict[str, dict[str, Any]] = {}
    for path in (state / "memory/corrections").glob("*.json"):
        row = load_json(path)
        old_corrections[row["correction_id"]] = row
    invalid_claims = set(read_lifecycle(state)["invalidated_claim_ids"])
    invalid_episodes = set(read_lifecycle(state)["invalidated_episode_ids"])
    previously_invalid_claims = set(invalid_claims)
    invalidated_keys = set()
    affected = set(records)
    seen_corrections = set()
    for correction in corrections:
        required = {"correction_id", "action", "reason", "evidence_episode_ids"}
        if not isinstance(correction, dict) or not required <= set(correction) or set(correction) - required - {"target_claim_ids", "target_episode_ids", "replacement_claim_id"}:
            raise ValidationError("memory correction fields are invalid")
        cid = stable_id(correction["correction_id"], "correction")
        if cid in seen_corrections:
            raise ValidationError("duplicate correction ID in proposal")
        seen_corrections.add(cid)
        targets = correction.get("target_claim_ids", [])
        target_episodes = correction.get("target_episode_ids", [])
        if not isinstance(targets, list) or not isinstance(target_episodes, list) or not (targets or target_episodes):
            raise ValidationError("correction must name exact old claims or episodes")
        if (any(not isinstance(x, str) for x in targets + target_episodes)
                or len(targets) != len(set(targets)) or len(target_episodes) != len(set(target_episodes))):
            raise ValidationError("correction target IDs must be unique")
        if any(x not in old_claims for x in targets) or any(x not in episodes for x in target_episodes):
            raise ValidationError("correction target is unknown")
        if correction["action"] not in {"retract", "supersede"} or not str(correction["reason"]).strip():
            raise ValidationError("correction needs a reason and explicit action")
        evidence(correction["evidence_episode_ids"])
        replacement = correction.get("replacement_claim_id")
        if correction["action"] == "supersede":
            if replacement not in claims or replacement in targets or replacement in invalid_claims:
                raise ValidationError("replacement claim is missing, invalid, or cyclic")
            withdrawn = _episode_withdrawal_closure(episodes, target_episodes)
            proposed = claims[replacement]
            if withdrawn.intersection(proposed["evidence_episode_ids"]):
                raise ValidationError("replacement depends on an episode being invalidated")
            if proposed["status"] != "confirmed" or proposed["basis"] not in {"user_explicit", "observed"}:
                if any(claim["status"] == "confirmed"
                       and (old_id in targets or withdrawn.intersection(claim["evidence_episode_ids"]))
                       for old_id, claim in old_claims.items()):
                    raise ValidationError("supersede pending: confirmed claims require a confirmed non-inference replacement")
        elif replacement is not None:
            raise ValidationError("retraction must not name a replacement")
        if cid in old_corrections:
            prior = {k: v for k, v in old_corrections[cid].items() if k not in {"recorded_at", "publication_id"}}
            if prior != correction:
                raise IdempotencyConflict("correction ID already has different content")
        invalid_claims.update(targets)
        invalid_episodes.update(target_episodes)
        if cid not in old_corrections:
            invalidated_keys.update(old_claims[x]["point_key"] for x in targets)
            invalidated_keys.update(key for eid in target_episodes for key, _ in episodes[eid])
    # Refinements keep the original episode as an explicit dependency.
    withdrawn = _episode_withdrawal_closure(episodes, invalid_episodes)
    invalidated_keys.update(key for eid in withdrawn - invalid_episodes for key, _ in episodes[eid])
    invalid_episodes = withdrawn
    # Propagate an episode withdrawal into all dependent derived claims.
    invalid_claims.update(cid for cid, claim in claims.items() if set(claim["evidence_episode_ids"]) & invalid_episodes)
    invalidated_keys.update(claims[cid]["point_key"] for cid in invalid_claims - previously_invalid_claims if cid in claims)
    affected.update(invalidated_keys)
    rid = formal_receipt.get("receipt_id") if formal_receipt else None
    receipt_row = read_lifecycle(state)["pending_receipts"].get(rid) if rid else None
    completed = receipt_row["binding"]["completed_packages"] if receipt_row else {
        pid: sha for pid, sha in zip((formal_receipt or {}).get("package_ids", []), (formal_receipt or {}).get("package_sha256s", []))
        if (formal_receipt or {}).get("package_dispositions", {}).get(pid) == "completed"}
    coverage_map = {}
    for item in coverage:
        if not isinstance(item, dict) or set(item) - {"package_id", "disposition", "keys", "reason"}:
            raise ValidationError("memory coverage fields are invalid")
        pid = item.get("package_id")
        if not formal_receipt or pid not in completed or pid in coverage_map:
            raise ValidationError("coverage must bind one completed writer package")
        keys = item.get("keys", [])
        if not isinstance(keys, list) or any(not isinstance(k, str) or k not in records for k in keys) or len(keys) != len(set(keys)):
            raise ValidationError("coverage keys must be explicitly published in this input")
        if item.get("disposition") == "covered":
            if not keys:
                raise ValidationError("covered package must cite published points")
            if any(not any(_episode_covers_package(repo, state, ep, pid, completed[pid])
                           for ep in records[key]["episodes"]) for key in keys):
                raise ValidationError("covered points must cite evidence from that exact completed package")
        elif item.get("disposition") == "no_relevant_points":
            if keys or not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise ValidationError("no-point coverage needs a reason and no invented keys")
        else:
            raise ValidationError("unknown memory coverage disposition")
        coverage_map[pid] = item
    if not records and (not coverage or any(x["disposition"] != "no_relevant_points" for x in coverage)):
        raise ValidationError("empty summary input needs verified reasoned no-point coverage")
    if receipt_row and formal_receipt.get("writer_receipt_sha256") != receipt_row["binding"].get("writer_receipt_sha256"):
        # A missing canonical file in isolated tests may be bound by content only.
        if receipt_row["binding"].get("writer_receipt_sha256") is not None:
            raise ValidationError("summary and writer receipt hash bindings differ")
    input_sha = object_sha256(document)
    publication_id = "EN-MEMORY-" + input_sha[:24].upper()
    for existing_id, pending in read_lifecycle(state).get("pending_publications", {}).items():
        if existing_id != publication_id and set(pending.get("keys", [])) & set(records):
            raise ValidationError("recover the pending memory publication before revising the same points")
    return {"publication_id": publication_id, "input_sha256": input_sha,
            "receipt_id": rid, "writer_receipt_sha256": (formal_receipt or {}).get("writer_receipt_sha256"),
            "source_review_id": getattr(review, "review_id", None),
            "keys": sorted(affected), "updated_keys": sorted(records),
            "summary_hashes": {key: object_sha256(record) for key, record in records.items()},
            "coverage": coverage_map, "claims": claims, "new_claim_ids": sorted(seen_claims - set(old_claims)),
            "invalidated_keys": sorted(invalidated_keys),
            "corrections": corrections, "invalidated_claim_ids": sorted(invalid_claims),
            "invalidated_episode_ids": sorted(invalid_episodes), "verified_episode_ids": sorted(verified_episodes)}


def stage_summary_memory(state: Path, plan: dict[str, Any] | None) -> None:
    if plan is None:
        return
    path = state / "memory/publications" / f"{plan['publication_id']}.json"
    with exclusive_lock(state / "memory/.lifecycle.lock"):
        old = load_json(path) if path.is_file() else None
        if old and old.get("input_sha256") != plan["input_sha256"]:
            raise IdempotencyConflict("memory publication ID conflict")
        if old and old.get("status") == "ready":
            return
        row = {**plan, "status": "summary_pending", "pending_component": "personal_summary", "recorded_at": old.get("recorded_at") if old else utc_now()}
        lifecycle = read_lifecycle(state)
        lifecycle.setdefault("pending_publications", {})[plan["publication_id"]] = {
            "keys": plan["keys"], "pending_component": "personal_summary", "receipt_id": plan.get("receipt_id"),
            "input_sha256": plan["input_sha256"]}
        lifecycle["invalidated_claim_ids"] = sorted(set(lifecycle["invalidated_claim_ids"]) | set(plan["invalidated_claim_ids"]))
        lifecycle["invalidated_episode_ids"] = sorted(set(lifecycle["invalidated_episode_ids"]) | set(plan["invalidated_episode_ids"]))
        lifecycle["invalidated_summary_keys"] = sorted(set(lifecycle.get("invalidated_summary_keys", [])) | set(plan.get("invalidated_keys", [])))
        for key in plan.get("invalidated_keys", []):
            lifecycle["accepted_summary_hashes"].pop(key, None)
        _refresh_pending(lifecycle)
        # Gate first. A crash before the durable transaction leaves a visible
        # pending input hash, never an actionable correction without a gate.
        _save(state, lifecycle)
        atomic_write_json(path, row)


def finish_summary_memory(repo: Path, state: Path, publication_id: str | None) -> dict[str, Any] | None:
    if publication_id is None:
        return None
    path = state / "memory/publications" / f"{publication_id}.json"
    row = load_json(path)
    current = _summary_hashes(state)
    if any(current.get(key) != digest for key, digest in row["summary_hashes"].items()):
        return {"status": "pending", "pending_component": "personal_summary", "publication_id": publication_id,
                "reason": "summary_pointer_not_committed_or_changed", "formal_write_count": 0}
    if row.get("status") == "ready" and publication_id not in read_lifecycle(state).get("pending_publications", {}):
        return {"status": "ready", "publication_id": publication_id, "memory_generation": row.get("memory_generation"), "formal_write_count": 0}
    with exclusive_lock(state / "memory/.lifecycle.lock"):
        lifecycle = read_lifecycle(state)
        claims_path = state / "memory/claims.json"
        existing = load_json(claims_path).get("claims", {}) if claims_path.is_file() else {}
        # Do not replace another publication's unrelated claims.
        for cid in row["new_claim_ids"]:
            incoming = {**row["claims"][cid], "publication_id": publication_id, "recorded_at": row["recorded_at"]}
            if cid in existing and existing[cid] != incoming:
                raise IdempotencyConflict("memory claim changed during publication")
            existing[cid] = incoming
        if row["new_claim_ids"]:
            atomic_write_json(claims_path, {"schema_version": "english_memory_claims_v1", "claims": existing})
        for correction in row["corrections"]:
            dest = state / "memory/corrections" / f"{object_sha256(correction['correction_id'])}.json"
            value = {**correction, "publication_id": publication_id, "recorded_at": row["recorded_at"]}
            if dest.exists():
                saved = {k: v for k, v in load_json(dest).items() if k not in {"recorded_at", "publication_id"}}
                if saved != correction:
                    raise IdempotencyConflict("memory correction changed during publication")
            else:
                atomic_write_json(dest, value)
        row["status"] = "index_pending"
        row["pending_component"] = "memory_index"
        row["summary_index_sha256"] = file_sha256(state / "personal-summary/current.json")
        atomic_write_json(path, row)
        lifecycle["accepted_summary_hashes"].update(row["summary_hashes"])
        lifecycle.setdefault("pending_publications", {})[publication_id] = {"keys": row["keys"],
            "pending_component": "memory_index", "receipt_id": row.get("receipt_id"), "input_sha256": row["input_sha256"]}
        rid = row.get("receipt_id")
        if rid in lifecycle["pending_receipts"]:
            target = lifecycle["pending_receipts"][rid]
            target["coverage"].update(row["coverage"])
            target["affected_keys"] = sorted(set(target["affected_keys"]) | set(row["keys"]))
            if publication_id not in target["publications"]:
                target["publications"].append(publication_id)
            if set(target["coverage"]) >= set(target["binding"]["completed_packages"]):
                target["pending_component"] = "memory_index"
                target["unmapped_scope"] = False
        _refresh_pending(lifecycle)
        _save(state, lifecycle)
    try:
        from .memory_store import build_memory_projection
        built = build_memory_projection(repo, state, keys=row["keys"] or None, publish=True, audit_sources=True)
        if built.get("status") in {"FAILED", "failed", "ERROR", "error"}:
            raise ValidationError("memory projection failed")
    except (ImportError, OSError, ValueError, TypeError, RuntimeError, ValidationError) as exc:
        row["last_error"] = type(exc).__name__ + ": " + str(exc)
        atomic_write_json(path, row)
        return {"status": "pending", "pending_component": "memory_index", "publication_id": publication_id,
                "error": row["last_error"], "formal_write_count": 0}
    # Prepare the *post*-publication view without exposing it. Real readers stay
    # gated until the safe mirror, managed pages and durable receipt all exist.
    try:
        from .memory_store import export_safe_mirror, publish_managed_pages
        with exclusive_lock(state / "memory/.lifecycle.lock"):
            current = _summary_hashes(state)
            if any(current.get(key) != digest for key, digest in row["summary_hashes"].items()):
                raise ValidationError("summary changed while memory projection was building")
            lifecycle = read_lifecycle(state)
            lifecycle.setdefault("pending_publications", {}).pop(publication_id, None)
            generation = built.get("generation") or built.get("generation_id")
            rid = row.get("receipt_id")
            if rid in lifecycle["pending_receipts"]:
                target = lifecycle["pending_receipts"][rid]
                waiting = any(pid in lifecycle["pending_publications"] for pid in target["publications"])
                if set(target["coverage"]) >= set(target["binding"]["completed_packages"]) and not waiting:
                    target.update(status="ready", pending_component=None, memory_generation=generation, unmapped_scope=False)
            _refresh_pending(lifecycle)
            mirror = export_safe_mirror(repo, state, lifecycle_override=lifecycle)
            pages = publish_managed_pages(repo, state, lifecycle_override=lifecycle)
            if mirror.get("status") in {"partial", "failed", "error"} or pages.get("status") in {"partial", "failed", "error"}:
                raise ValidationError("memory mirror or managed-page closure is incomplete")
            row.update(status="ready", pending_component=None, memory_generation=generation, ready_at=utc_now(),
                       projection_receipt=built, mirror_receipt=mirror, pages_receipt=pages)
            row.pop("last_error", None)
            atomic_write_json(path, row)
            _save(state, lifecycle)
    except (ImportError, OSError, ValueError, TypeError, RuntimeError, ValidationError) as exc:
        row.update(status="index_pending", pending_component="memory_index", last_error=type(exc).__name__ + ": " + str(exc))
        atomic_write_json(path, row)
        return {"status": "pending", "pending_component": "memory_index", "publication_id": publication_id,
                "error": row["last_error"], "formal_write_count": 0}
    return {"status": "ready", "publication_id": publication_id, "receipt_path": str(path),
            "input_sha256": row["input_sha256"], "summary_index_sha256": row["summary_index_sha256"],
            "summary_hashes": row["summary_hashes"], "memory_generation": generation,
            "coverage": row["coverage"], "formal_write_count": 0}


def recover_memory_closeout(repo: Path, state: Path, receipt_id: str | None = None) -> dict[str, Any]:
    results = []
    lifecycle = read_lifecycle(state)
    for path in sorted((state / "memory/publications").glob("*.json")):
        row = load_json(path)
        if (row.get("status") == "ready" and row.get("publication_id") not in lifecycle.get("pending_publications", {})) or (receipt_id is not None and row.get("receipt_id") != receipt_id):
            continue
        if row["publication_id"] not in read_lifecycle(state).get("pending_publications", {}):
            # Recover a pre-gate transaction from an interrupted older version;
            # establish the read gate before inspecting or completing its data.
            stage_summary_memory(state, row)
        results.append(finish_summary_memory(repo, state, row["publication_id"]))
    status = memory_closeout_status(state, receipt_id)
    incomplete = next((row for row in results if row and row.get("status") != "ready"), None)
    if incomplete:
        status.update(status="pending", pending_component=incomplete.get("pending_component", "memory_index"))
    return {**status, "recovered": results, "formal_apply_invocation_count": 0}


def summary_read_block(state: Path, key: str, digest: str, episode_ids: list[str]) -> str | None:
    if not memory_enabled(state):
        return None
    lifecycle = read_lifecycle(state)
    if lifecycle["global_pending"] or key in lifecycle["pending_keys"]:
        return "memory_update_pending"
    if (key in lifecycle.get("invalidated_summary_keys", []) or set(episode_ids) & set(lifecycle["invalidated_episode_ids"])) and lifecycle["accepted_summary_hashes"].get(key) != digest:
        return "summary_invalidated"
    return None
