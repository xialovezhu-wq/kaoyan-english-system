"""Rebuildable English memory projection. Formal evidence remains read-only.

The SQLite database contains only previously published point observations and
their locators. It is an index, never a second learning-event or mastery writer.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from .answer_safety import point_answer_risk
from .errors import ValidationError
from .personal_summary import INDEX, SCHEMA, _read_record, key_hash, normalize_key, teaching_header
from .util import (atomic_write_json, atomic_write_text, canonical_bytes,
                   exclusive_lock, file_sha256, load_json, object_sha256, utc_now)

PROJECTION_SCHEMA = "english_memory_projection_v1"
RECIPE = "english-memory-evidence-v6"
_STOP_WORDS = {"the", "a", "an", "is", "are", "was", "were", "i", "my", "me", "of", "to", "and", "or",
               "以前", "之前", "这个", "那个", "怎样", "怎么", "哪里", "什么", "有没有", "是否", "我的", "现在"}


def _json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def search_tokens(value: str) -> list[str]:
    """Deterministic English words plus CJK bigrams; no guessed semantics."""
    value = unicodedata.normalize("NFKC", value).casefold()
    tokens = []
    for part in re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*|[\u3400-\u9fff]+", value):
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            if len(part) <= 5:
                tokens.append(part)
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
            tokens.extend(part[i:i + 3] for i in range(len(part) - 2))
        else:
            tokens.append(part)
    return list(dict.fromkeys(t for t in tokens if t and t not in _STOP_WORDS))


def record_text(record: dict[str, Any]) -> str:
    """Only the audited summary projection, never raw source excerpts."""
    values = [record.get("key", ""), *record.get("aliases", []), record.get("teaching_hint", "")]
    for episode in record.get("episodes", []):
        values.extend(str(episode.get(k, "")) for k in
                      ("first_attempt", "correction", "later_observation", "next_action"))
    for contrast in record.get("contrasts", []):
        values.extend(str(contrast.get(k, "")) for k in
                      ("target", "target_meaning", "other", "other_meaning", "distinction", "applies_when"))
    return "\n".join(str(v) for v in values if v)


def is_answer_safe(record: dict[str, Any], *, inherited_protected: bool = False, question_bound: bool = False) -> bool:
    # A protected source package may support a separately reviewed answer-free
    # language projection. Its original text is never copied into this index.
    return (record.get("visibility") == "answer_free" and not inherited_protected
            and not point_answer_risk(record, question_bound=question_bound))


def read_lifecycle(state: Path) -> dict[str, Any]:
    path = state / "memory/lifecycle.json"
    if not path.exists():
        return {"pending_keys": {}, "invalidated_episode_ids": [], "invalidated_claim_ids": [],
                "invalidated_summary_keys": [], "global_pending": False, "accepted_summary_hashes": {}}
    try:
        value = load_json(path)
        if not isinstance(value.get("pending_keys", {}), dict):
            raise ValueError("pending keys are not a mapping")
        for name in ("invalidated_episode_ids", "invalidated_claim_ids", "invalidated_summary_keys"):
            if not isinstance(value.get(name, []), list):
                raise ValueError("invalid lifecycle identifiers")
        return value
    except (OSError, ValueError, TypeError):
        return {"pending_keys": {}, "global_pending": True, "error": "invalid_lifecycle_state",
                "invalidated_episode_ids": [], "invalidated_claim_ids": []}


def lifecycle_fingerprint(value: dict[str, Any]) -> str:
    """Bind effective lifecycle state, excluding its write-only revision counter."""
    return object_sha256({key: item for key, item in value.items() if key != "revision"})


def _projection_snapshot(state: Path, manifest: dict[str, Any]) -> tuple[str | None, ...]:
    values = tuple(_optional_hash(state / relative) for relative in
                   ("personal-summary/current.json", "memory/lifecycle.json", "memory/claims.json", "memory/current.json"))
    if values[0] != manifest.get("summary_index_sha256"):
        raise ValidationError("memory projection is stale; rebuild before exporting or publishing pages")
    current = _current_manifest(state)
    if current is None or current.get("generation") != manifest.get("generation"):
        raise ValidationError("memory generation changed before export")
    return values


def _assert_snapshot(state: Path, manifest: dict[str, Any], expected: tuple[str | None, ...]) -> None:
    if _projection_snapshot(state, manifest) != expected:
        raise ValidationError("memory sources or lifecycle changed during export")


def _optional_hash(path: Path) -> str | None:
    return file_sha256(path) if path.is_file() else None


def _schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS points (
      point_key TEXT PRIMARY KEY, summary_sha256 TEXT NOT NULL, visibility TEXT NOT NULL,
      answer_safe INTEGER NOT NULL, record_json TEXT, audit_json TEXT NOT NULL,
      source_ids_json TEXT NOT NULL, unit_ids_json TEXT NOT NULL, question_ids_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS teaching_headers (
      point_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, point_key TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS episodes (
      point_key TEXT NOT NULL, episode_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
      occurred_at TEXT, episode_json TEXT NOT NULL, source_ids_json TEXT NOT NULL,
      unit_ids_json TEXT NOT NULL, question_ids_json TEXT NOT NULL, scope_bindings_json TEXT NOT NULL,
      PRIMARY KEY(point_key,episode_id));
    CREATE INDEX IF NOT EXISTS episodes_by_identity ON episodes(episode_id);
    CREATE INDEX IF NOT EXISTS episodes_by_time ON episodes(occurred_at);
    CREATE TABLE IF NOT EXISTS sources (source_key TEXT PRIMARY KEY, audit_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS dependencies (
      source_key TEXT NOT NULL, point_key TEXT NOT NULL, episode_id TEXT NOT NULL,
      PRIMARY KEY(source_key,point_key,episode_id));
    CREATE INDEX IF NOT EXISTS deps_by_point ON dependencies(point_key);
    CREATE TABLE IF NOT EXISTS identities (
      identity TEXT NOT NULL, point_key TEXT NOT NULL, kind TEXT NOT NULL,
      PRIMARY KEY(identity,point_key,kind));
    CREATE INDEX IF NOT EXISTS identities_by_point ON identities(point_key);
    CREATE TABLE IF NOT EXISTS relations (
      source_key TEXT NOT NULL, target_key TEXT NOT NULL, kind TEXT NOT NULL,
      evidence_id TEXT NOT NULL, PRIMARY KEY(source_key,target_key,kind,evidence_id));
    CREATE VIRTUAL TABLE IF NOT EXISTS safe_search USING fts5(point_key UNINDEXED, tokens, tokenize='unicode61');
    """)
    if "scope_bindings_json" not in {row[1] for row in db.execute("PRAGMA table_info(episodes)")}:
        db.execute("ALTER TABLE episodes ADD COLUMN scope_bindings_json TEXT NOT NULL DEFAULT '[]'")


def _pointer(value: Any, pointer: str) -> Any:
    if pointer.startswith("#"):
        pointer = pointer[1:]
    if pointer and not pointer.startswith("/"):
        pointer = "/" + pointer
    for token in pointer.split("/")[1:]:
        if re.search(r"~(?![01])", token):
            raise KeyError("invalid pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list) and token.isdigit():
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise KeyError(token)
    return value


def _scope_from_document(document: Any) -> dict[str, set[str]]:
    result = {"source_ids": set(), "unit_ids": set(), "question_ids": set(), "formal_ids": set(), "bindings": set()}
    if not isinstance(document, dict):
        return result
    candidates = [document]
    for name in ("source", "identity", "manifest"):
        if isinstance(document.get(name), dict):
            candidates.append(document[name])
            if isinstance(document[name].get("identity"), dict):
                candidates.append(document[name]["identity"])
    for item in candidates:
        bound_sources, bound_units, bound_questions = set(), set(), set()
        for field, target in (("source_id", "source_ids"), ("source_article", "source_ids"),
                              ("unit_id", "unit_ids"), ("sentence_id", "unit_ids"),
                              ("question_id", "question_ids")):
            if isinstance(item.get(field), str) and item[field]:
                result[target].add(item[field])
                {"source_ids": bound_sources, "unit_ids": bound_units, "question_ids": bound_questions}[target].add(item[field])
        if bound_sources or bound_units or bound_questions:
            result["bindings"].add((tuple(sorted(bound_sources)), tuple(sorted(bound_units)), tuple(sorted(bound_questions))))
        for field in ("bank_ids", "formal_ids", "sentence_pattern_ids"):
            if isinstance(item.get(field), list):
                result["formal_ids"].update(x for x in item[field] if isinstance(x, str))
    return result


def _bindings_json(scope: dict[str, Any]) -> str:
    return _json([{"source_ids": list(sources), "unit_ids": list(units), "question_ids": list(questions)}
                  for sources, units, questions in sorted(scope["bindings"])])


def _merge_scope(target: dict[str, set[str]], source: dict[str, set[str]]) -> None:
    for key in target:
        target[key].update(source.get(key, set()))


class _SourceAudit:
    def __init__(self, repo: Path, state: Path, enabled: bool):
        self.repo, self.state, self.enabled = repo.resolve(), state.resolve(), enabled
        self.files: dict[str, dict[str, Any]] = {}
        self.refs: dict[str, tuple[dict[str, Any], dict[str, set[str]]]] = {}
        self.archives: dict[str, Path | None] = {}
        self.parents: dict[str, dict[str, Any] | None] = {}

    def _file(self, path: Path) -> dict[str, Any]:
        key = str(path.resolve())
        if key in self.files:
            return self.files[key]
        try:
            before = path.stat()
            digest = file_sha256(path)
            data = path.read_bytes() if path.suffix.lower() != ".zip" and before.st_size <= 32 * 1024 * 1024 else b""
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise OSError("source changed during audit")
            text = data.decode("utf-8", errors="replace")
            try:
                document = json.loads(text) if path.suffix.lower() in {".json", ".jsonl"} else None
            except ValueError:
                document = None
            item = {"sha256": digest, "text": text, "document": document,
                    "size": before.st_size, "available": True}
            if path.suffix.lower() == ".zip":
                item["zip_path"] = path
                item["zip_members"] = {}
        except OSError:
            item = {"available": False}
        self.files[key] = item
        return item

    def _zip_member(self, source: dict[str, Any], member: str) -> Any:
        if member in source["zip_members"]:
            return source["zip_members"][member]
        if Path(member).is_absolute() or ".." in Path(member).parts:
            raise KeyError("unsafe archive member")
        with zipfile.ZipFile(source["zip_path"]) as archive:
            info = archive.getinfo(member)
            if info.file_size > 16 * 1024 * 1024:
                raise ValueError("archive text member exceeds audit budget")
            document = json.loads(archive.read(member))
        source["zip_members"][member] = document
        return document

    def _path(self, ref: dict[str, Any]) -> Path | None:
        path = ref.get("path")
        if isinstance(path, str) and path:
            target = Path(path) if Path(path).is_absolute() else self.repo / path
            # These are already published evidence refs. Never follow new parent
            # traversal from malformed input or use an arbitrary guessed file.
            if ".." in Path(path).parts:
                return None
            if target.is_file():
                return target
        package_id, member = ref.get("package_id"), ref.get("member")
        if isinstance(package_id, str) and isinstance(member, str) and member and not Path(member).is_absolute() and ".." not in Path(member).parts:
            if package_id not in self.archives:
                match = re.fullmatch(r"EN-PKG-(\d{4})(\d{2})(\d{2})-[A-F0-9]{16}", package_id)
                root = None
                if match:
                    from .archive import resolve_archived_package_from_pointer
                    try:
                        root = resolve_archived_package_from_pointer(
                            self.state, self.repo, study_date="-".join(match.groups()),
                            package_id=package_id, expected_package_sha256=ref.get("package_sha256"))
                    except (OSError, ValueError, KeyError, TypeError, ValidationError):
                        pass
                self.archives[package_id] = root
            root = self.archives[package_id]
            if root is not None:
                return root / member
        return None

    def parent_record(self, ref: dict[str, Any]) -> dict[str, Any] | None:
        path = ref.get("path", "")
        if not isinstance(path, str):
            return None
        candidate = (Path(path) if Path(path).is_absolute() else self.repo / path).resolve()
        try:
            if candidate.parent.resolve() != (self.state / "personal-summary/objects").resolve():
                return None
        except OSError:
            return None
        digest = candidate.stem
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            return None
        if digest not in self.parents:
            try:
                record = load_json(candidate)
                self.parents[digest] = record if object_sha256(record) == digest and record.get("schema_version") == SCHEMA else None
            except (OSError, ValueError):
                self.parents[digest] = None
        return self.parents[digest]

    def audit(self, ref: dict[str, Any]) -> tuple[dict[str, Any], dict[str, set[str]]]:
        identity = object_sha256(ref)
        if identity in self.refs:
            return self.refs[identity]
        scope = {"source_ids": set(), "unit_ids": set(), "question_ids": set(), "formal_ids": set(), "bindings": set()}
        path = ref.get("path")
        if isinstance(path, str) and path:
            scope["source_ids"].add(path)
            try:
                scope["source_ids"].add(str(Path(path).relative_to(self.repo)))
            except ValueError:
                pass
        if isinstance(ref.get("package_id"), str):
            scope["source_ids"].add(ref["package_id"])
        audit = {"source_key": identity, "reference": ref, "status": "not_checked", "locator_status": "not_checked"}
        if not self.enabled:
            self.refs[identity] = (audit, scope)
            return audit, scope
        target = self._path(ref)
        source = self._file(target) if target else {"available": False}
        if target is not None:
            audit["resolved_path"] = str(target.resolve())
        if not source["available"]:
            audit["status"] = "unavailable"
            self.refs[identity] = (audit, scope)
            return audit, scope
        audit["observed_sha256"] = source["sha256"]
        expected = ref.get("sha256")
        mutable = bool(path and (str(path).startswith(("articles/", "bank/", "review/"))
                       or any(str(path).startswith(str(self.repo / part) + "/") for part in ("articles", "bank", "review"))))
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            audit["status"] = "missing_source_hash"
        elif source["sha256"] == expected:
            audit["status"] = "verified"
        else:
            audit["status"] = "mutable_revision_drift" if mutable else "source_hash_conflict"
        document = source.get("document")
        snapshot = document
        zip_prefix = ""
        if "zip_path" in source:
            package_id = ref.get("package_id")
            if package_id:
                # A multi-package bundle also has its own root manifest. Never
                # resolve a package's manifest/attachments against that file.
                if not isinstance(package_id, str) or not re.fullmatch(r"EN-PKG-\d{8}-[A-F0-9]{16}", package_id):
                    audit.update(status="source_hash_conflict", locator_status="invalid")
                    self.refs[identity] = (audit, scope)
                    return audit, scope
                zip_prefix = f"packages/{package_id}/"
                try:
                    packaged = self._zip_member(source, zip_prefix + "manifest.json")
                    if (packaged.get("package_id") != package_id or
                            (ref.get("package_sha256") and packaged.get("package_canonical_sha256") != ref["package_sha256"])):
                        raise ValueError("package identity differs from its frozen reference")
                except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
                    audit.update(status="source_hash_conflict", locator_status="invalid")
                    self.refs[identity] = (audit, scope)
                    return audit, scope
            try:
                _merge_scope(scope, _scope_from_document(self._zip_member(source, zip_prefix + "source.json")))
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                pass
        if ref.get("snapshot_pointer"):
            try:
                snapshot = _pointer(document, ref["snapshot_pointer"])
            except (KeyError, IndexError, TypeError):
                audit["locator_status"] = "invalid"
                snapshot = None
        _merge_scope(scope, _scope_from_document(snapshot))
        # An exact package member may carry source metadata in its sibling.
        if target and target.name in {"conversation.json", "manifest.json", "source.json"}:
            src = self._file(target.parent / "source.json")
            if src["available"]:
                _merge_scope(scope, _scope_from_document(src.get("document")))
        locators = []
        for field in ("locator", "pointer"):
            if isinstance(ref.get(field), str):
                locators.append(ref[field])
        for field in ("locators", "pointers"):
            if isinstance(ref.get(field), list):
                locators.extend(x for x in ref[field] if isinstance(x, str))
        expanded = []
        for locator in locators:
            parts = [part.strip() for part in re.split(r"[;；]", locator) if part.strip()]
            if len(parts) > 1 and (parts[0].startswith(("/", "#/", "messages/", "episodes/", "identity/", "items/", "attachments/")) or ".json#" in parts[0]):
                expanded.extend(parts)
            else:
                expanded.append(locator)
        locators = expanded
        statuses = []
        for locator in locators:
            current = snapshot
            pointer = locator
            if ".json#" in locator and "zip_path" in source:
                member, pointer = locator.split("#", 1)
                try:
                    current = self._zip_member(source, member if member.startswith(zip_prefix) and zip_prefix else zip_prefix + member)
                except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                    current = None
            elif ".json#" in locator and isinstance(snapshot, dict):
                member, pointer = locator.split("#", 1)
                current = snapshot.get(Path(member).stem)
            if pointer.startswith(("/", "#/", "messages/", "episodes/", "identity/", "items/", "attachments/")):
                if current is None:
                    statuses.append("unresolved")
                else:
                    try:
                        _pointer(current, pointer)
                        statuses.append("verified")
                    except (KeyError, IndexError, TypeError):
                        statuses.append("invalid")
            elif isinstance(current, dict) and pointer in current:
                # Some reviewed legacy locators name a second exact root field
                # after a semicolon, e.g. items/0/reason;reference_audit.
                statuses.append("verified")
            elif (target and target.suffix.casefold() in {".md", ".markdown"}
                  and (question := re.fullmatch(r"(Q\d+)(?:[;；].*)?", locator))):
                # Legacy analysis references use a question heading followed by
                # page/purpose notes. Verify one exact, unambiguous heading;
                # neither body mentions nor duplicate/multi-question headings qualify.
                qid = question.group(1)
                headings = [line for line in source["text"].splitlines()
                            if re.match(r"^#{1,6}\s+", line)
                            and set(re.findall(r"\bQ\d+\b", line)) == {qid}]
                statuses.append("verified" if len(headings) == 1 else "unresolved")
            else:
                ranges = re.findall(r"(?:lines?\s*[:/ ]\s*|\bL)(\d+)\s*[-–]\s*L?(\d+)", locator, re.I)
                if not ranges:
                    ranges = re.findall(r"^S\d+:(\d+)-(\d+)$", locator)
                if ranges:
                    count = len(source["text"].splitlines())
                    statuses.append("verified" if all(1 <= int(a) <= int(b) <= count for a, b in ranges) else "invalid")
                elif "id=" in locator and target and target.suffix == ".csv":
                    ids = re.findall(r"\b\d{8}-\d{3}\b", locator)
                    actual = {row.get("id") for row in csv.DictReader(io.StringIO(source["text"]))}
                    statuses.append("verified" if ids and all(item in actual for item in ids) else "unresolved")
                    scope["formal_ids"].update(ids)
                else:
                    statuses.append("unresolved")
        if audit["locator_status"] != "invalid":
            audit["locator_status"] = ("invalid" if "invalid" in statuses else
                                       "unresolved" if "unresolved" in statuses else
                                       "verified" if statuses or ref.get("snapshot_pointer") else "legacy_file_only")
        if path and scope["bindings"]:
            # A referenced file is an alias for the exact selected snapshot,
            # not an independent source/unit relationship.
            scope["bindings"] = {(tuple(sorted(set(sources) | {path})), units, questions)
                                  for sources, units, questions in scope["bindings"]}
        self.refs[identity] = (audit, scope)
        return audit, scope


def _current_manifest(state: Path) -> dict[str, Any] | None:
    path = state / "memory/current.json"
    if not path.exists():
        return None
    value = load_json(path)
    if value.get("schema_version") != PROJECTION_SCHEMA:
        raise ValidationError("memory projection schema is unsupported")
    return value


def open_memory_projection(state: Path) -> tuple[sqlite3.Connection, dict[str, Any]]:
    manifest = _current_manifest(state)
    if manifest is None:
        raise FileNotFoundError("memory projection is not built")
    generation = manifest.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{64}", generation):
        raise ValidationError("invalid memory generation")
    expected = Path("generations") / generation / "memory.sqlite3"
    if manifest.get("database") != expected.as_posix():
        raise ValidationError("memory database must be generation-bound")
    path = state / "memory" / expected
    if not path.is_file() or path.resolve().parent != (state / "memory/generations" / generation).resolve():
        raise ValidationError("memory database is unavailable or outside generation")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        metadata = dict(db.execute("SELECT name,value FROM metadata"))
        if metadata.get("generation") != generation or metadata.get("summary_index_sha256") != manifest.get("summary_index_sha256"):
            raise ValidationError("memory generation binding changed")
    except (sqlite3.Error, ValidationError):
        db.close()
        raise
    return db, manifest


def source_dependents(repo: Path, state: Path, paths: list[Path]) -> list[str]:
    """Resolve known source changes through the current reverse dependency index."""
    db, _ = open_memory_projection(state)
    try:
        keys = set()
        for path in paths:
            absolute = (repo / path).absolute()
            resolved = absolute.resolve()
            terms = {str(path), str(absolute), str(resolved)}
            if absolute.is_relative_to(repo):
                terms.add(absolute.relative_to(repo).as_posix())
            if resolved.is_relative_to(repo):
                terms.add(resolved.relative_to(repo).as_posix())
            bind = tuple(sorted(terms))
            placeholders = ",".join("?" for _ in bind)
            rows = db.execute("SELECT DISTINCT d.point_key FROM dependencies d JOIN sources s USING(source_key) "
                              f"WHERE json_extract(s.audit_json,'$.reference.path') IN ({placeholders}) "
                              f"OR json_extract(s.audit_json,'$.resolved_path') IN ({placeholders})", (*bind, *bind))
            found = {row[0] for row in rows}
            if not found:
                raise ValidationError("no indexed English memory dependency for the supplied source path")
            keys.update(found)
        return sorted(keys)
    finally:
        db.close()


def _remove_point(db: sqlite3.Connection, key: str) -> None:
    for table in ("aliases", "episodes", "dependencies", "identities", "teaching_headers"):
        db.execute(f"DELETE FROM {table} WHERE point_key=?", (key,))
    db.execute("DELETE FROM relations WHERE source_key=?", (key,))
    db.execute("DELETE FROM safe_search WHERE point_key=?", (key,))
    db.execute("DELETE FROM points WHERE point_key=?", (key,))


def _insert_point(db: sqlite3.Connection, key: str, pointer: dict[str, Any], audit: _SourceAudit) -> None:
    _remove_point(db, key)
    try:
        record = _read_record(audit.state / "personal-summary", pointer)
        if normalize_key(key) != key or not isinstance(record.get("episodes"), list):
            raise ValidationError("invalid point structure")
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        db.execute("INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)", (key, str(pointer.get("sha256", "")),
                   "protected", 0, None, _json({"status": "invalid_summary", "issues": ["invalid_or_missing_summary"]}), "[]", "[]", "[]"))
        return
    scope = {"source_ids": set(), "unit_ids": set(), "question_ids": set(), "formal_ids": set(), "bindings": set()}
    _merge_scope(scope, _scope_from_document(record))
    issues, inherited_protected, identities = [], False, []
    episode_ids = set()
    for ordinal, episode in enumerate(record["episodes"]):
        eid = episode.get("episode_id")
        if not isinstance(eid, str) or not eid or eid in episode_ids:
            issues.append("duplicate_or_missing_episode_id")
            continue
        episode_ids.add(eid)
        escope = _scope_from_document(episode)
        if isinstance(episode.get("resolved_sentence_id"), str):
            escope["unit_ids"].add(episode["resolved_sentence_id"])
        for value in episode.get("formal_ids", []):
            if isinstance(value, str):
                escope["formal_ids"].add(value)
        refs = episode.get("evidence", [])
        if not isinstance(refs, list) or not refs:
            issues.append("missing_evidence")
            refs = []
        pending_refs = [(ref, 0, set()) for ref in refs if isinstance(ref, dict)]
        while pending_refs:
            ref, depth, ancestry = pending_refs.pop()
            source, rscope = audit.audit(ref)
            _merge_scope(escope, rscope)
            db.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (source["source_key"], _json(source)))
            db.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?,?)", (source["source_key"], key, eid))
            if source["status"] != "verified":
                issues.append(source["status"])
            if source["locator_status"] not in {"verified", "not_checked"}:
                issues.append("locator_" + source["locator_status"])
            parent = audit.parent_record(ref)
            raw_parent_path = ref.get("path", "")
            if isinstance(raw_parent_path, str):
                candidate_parent = (Path(raw_parent_path) if Path(raw_parent_path).is_absolute() else audit.repo / raw_parent_path).resolve()
                if candidate_parent.parent.resolve() == (audit.state / "personal-summary/objects").resolve() and parent is None:
                    issues.append("invalid_parent_summary")
            if parent is not None:
                inherited_protected |= not is_answer_safe(parent)
                digest = ref.get("sha256")
                if digest in ancestry or depth >= 16:
                    issues.append("parent_dependency_cycle_or_depth")
                    continue
                source_eid = episode.get("source_episode_id")
                parent_episodes = [e for e in parent.get("episodes", []) if e.get("episode_id") == source_eid] if source_eid else parent.get("episodes", [])
                for pe in parent_episodes:
                    _merge_scope(escope, _scope_from_document(pe))
                    for pref in pe.get("evidence", []):
                        if isinstance(pref, dict):
                            pending_refs.append((pref, depth + 1, ancestry | {digest}))
                if source_eid and not parent_episodes:
                    issues.append("missing_parent_episode")
        _merge_scope(scope, escope)
        occurred = episode.get("observed_at") or episode.get("study_date")
        if occurred is not None:
            from datetime import datetime
            try:
                datetime.fromisoformat(str(occurred).replace("Z", "+00:00"))
            except ValueError:
                issues.append("invalid_observation_date")
                occurred = None
        else:
            issues.append("unknown_observation_date")
        db.execute("INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?,?)", (key, eid, ordinal, occurred, _json(episode),
                   _json(sorted(escope["source_ids"])), _json(sorted(escope["unit_ids"])), _json(sorted(escope["question_ids"])), _bindings_json(escope)))
        identities.extend((value, key, kind) for kind, values in (("episode", [eid]), ("formal", escope["formal_ids"])) for value in values)
        for source_id in sorted(escope["source_ids"]):
            db.execute("INSERT OR IGNORE INTO relations VALUES(?,?,?,?)", (key, source_id, "observed_in", eid))
    safe = is_answer_safe(record, inherited_protected=inherited_protected, question_bound=bool(scope["question_ids"]))
    if record.get("visibility") == "answer_free" and not safe:
        issues.append("inherited_protection" if inherited_protected else "answer_content_detected")
    if record.get("latest_episode_id") and record["latest_episode_id"] not in episode_ids:
        issues.append("invalid_latest_episode_id")
    severe = {"duplicate_or_missing_episode_id", "parent_dependency_cycle_or_depth", "missing_parent_episode",
              "invalid_latest_episode_id", "source_hash_conflict", "locator_invalid", "missing_evidence", "invalid_parent_summary"}
    point_audit = {"status": "conflict" if severe & set(issues) else "coverage_limited" if issues else "verified",
                   "issues": sorted(set(issues)), "claim_status": "legacy_unclassified"}
    safe = safe and point_audit["status"] != "conflict"
    db.execute("INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)", (key, pointer["sha256"], record["visibility"], int(safe),
               _json(record), _json(point_audit), _json(sorted(scope["source_ids"])),
               _json(sorted(scope["unit_ids"])), _json(sorted(scope["question_ids"]))))
    if pointer.get("teaching_sha256"):
        db.execute("INSERT INTO teaching_headers VALUES(?,?)", (key, _json(teaching_header(record))))
    for alias in [key, *record.get("aliases", [])]:
        alias = normalize_key(alias)
        existing = db.execute("SELECT point_key FROM aliases WHERE alias=?", (alias,)).fetchone()
        if existing and existing[0] != key:
            raise ValidationError("ambiguous memory alias")
        db.execute("INSERT OR REPLACE INTO aliases VALUES(?,?)", (alias, key))
    for row in identities + [(key, key, "point")]:
        db.execute("INSERT OR IGNORE INTO identities VALUES(?,?,?)", row)
    if safe and point_audit["status"] != "conflict":
        db.execute("INSERT INTO safe_search VALUES(?,?)", (key, " ".join(search_tokens(record_text(record)))))
    # Only already-authored links become relations, never lexical similarity.
    for related in record.get("related_points", []):
        if isinstance(related, str):
            db.execute("INSERT OR IGNORE INTO relations VALUES(?,?,?,?)", (key, related, "authored_related_point", pointer["sha256"]))


def _counts(db: sqlite3.Connection) -> dict[str, Any]:
    issues = Counter()
    for (payload,) in db.execute("SELECT audit_json FROM points"):
        issues.update(json.loads(payload).get("issues", []))
    return {"point_count": db.execute("SELECT count(*) FROM points").fetchone()[0],
            "safe_point_count": db.execute("SELECT count(*) FROM points WHERE answer_safe=1").fetchone()[0],
            "episode_count": db.execute("SELECT count(*) FROM episodes").fetchone()[0],
            "source_count": db.execute("SELECT count(*) FROM sources").fetchone()[0],
            "dependency_count": db.execute("SELECT count(*) FROM dependencies").fetchone()[0],
            "issues": dict(sorted(issues.items()))}


def build_memory_projection(repo: Path, state: Path, *, keys: list[str] | None = None,
                            publish: bool = True, audit_sources: bool = True) -> dict[str, Any]:
    """Build a full structural audit or incrementally replace touched points.

    ``publish=False`` uses an in-memory SQLite database and performs zero writes.
    Every publication retains the preceding generation for rollback.
    """
    repo, state = repo.resolve(), state.resolve()
    index_path = state / "personal-summary/current.json"
    raw = index_path.read_bytes()
    index = json.loads(raw)
    if index.get("schema_version") != INDEX or not isinstance(index.get("routes"), dict):
        raise ValidationError("personal summary index is invalid")
    index_sha = hashlib.sha256(raw).hexdigest()
    pointers = {}
    for route in index["routes"].values():
        if not isinstance(route, dict) or not isinstance(route.get("key"), str):
            raise ValidationError("summary route is invalid")
        key = normalize_key(route["key"])
        if key in pointers and pointers[key] != route:
            raise ValidationError("one point routes to conflicting summary versions")
        pointers[key] = route
    root = state / "memory"
    if publish:
        root.mkdir(parents=True, exist_ok=True)
    from contextlib import nullcontext
    with exclusive_lock(root / ".projection.lock") if publish else nullcontext():
        db = sqlite3.connect(":memory:")
        previous = None
        try:
            if keys is not None:
                try:
                    old, previous = open_memory_projection(state)
                    try:
                        old.backup(db)
                        if dict(old.execute("SELECT name,value FROM metadata")).get("recipe") != RECIPE:
                            previous = None
                    finally:
                        old.close()
                except (OSError, sqlite3.Error, ValueError, ValidationError):
                    previous = None
            _schema(db)
            old_hashes = dict(db.execute("SELECT point_key,summary_sha256 FROM points"))
            if keys is None or previous is None:
                touched = set(pointers)
            else:
                touched = {normalize_key(key) for key in keys}
                touched.update(k for k, v in pointers.items() if old_hashes.get(k) != v["sha256"])
            for key in set(old_hashes) - set(pointers):
                _remove_point(db, key)
            audit = _SourceAudit(repo, state, audit_sources)
            for key in sorted(touched):
                if key in pointers:
                    _insert_point(db, key, pointers[key], audit)
            db.execute("DELETE FROM sources WHERE source_key NOT IN (SELECT source_key FROM dependencies)")
            counts = _counts(db)
            fingerprint = {"recipe": RECIPE, "summary_index_sha256": index_sha,
                           "points": list(db.execute("SELECT point_key,summary_sha256,answer_safe,audit_json,source_ids_json,unit_ids_json,question_ids_json FROM points ORDER BY point_key")),
                           "sources": list(db.execute("SELECT source_key,audit_json FROM sources ORDER BY source_key"))}
            generation = object_sha256(fingerprint)
            db.execute("INSERT OR REPLACE INTO metadata VALUES('generation',?)", (generation,))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('summary_index_sha256',?)", (index_sha,))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('schema_version',?)", (PROJECTION_SCHEMA,))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('recipe',?)", (RECIPE,))
            db.commit()
            receipt = {"schema_version": PROJECTION_SCHEMA, "status": "preview", "generation": generation,
                       "summary_index_sha256": index_sha, "counts": counts,
                       "keys": sorted(pointers), "updated_keys": sorted(touched & set(pointers)),
                       "formal_write_count": 0, "learning_event_write_count": 0,
                       "source_audit_performed": audit_sources, "previous_generation": (previous or {}).get("generation")}
            if not publish:
                return receipt
            if file_sha256(index_path) != index_sha:
                raise ValidationError("personal summaries changed during projection build")
            generation_dir = root / "generations" / generation
            generation_dir.parent.mkdir(parents=True, exist_ok=True)
            if not generation_dir.exists():
                stage = Path(tempfile.mkdtemp(prefix=".building-", dir=generation_dir.parent))
                try:
                    target_db = sqlite3.connect(stage / "memory.sqlite3")
                    db.backup(target_db)
                    target_db.close()
                    check = sqlite3.connect(stage / "memory.sqlite3")
                    try:
                        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise ValidationError("memory database integrity check failed")
                    finally:
                        check.close()
                    _write_mirror(db, stage / "safe-mirror", generation, lifecycle={})
                    os.replace(stage, generation_dir)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            manifest = {"schema_version": PROJECTION_SCHEMA, "generation": generation,
                        "summary_index_sha256": index_sha, "database": f"generations/{generation}/memory.sqlite3",
                        "database_sha256": file_sha256(generation_dir / "memory.sqlite3"),
                        "mirror": f"generations/{generation}/safe-mirror", "counts": counts,
                        "source_audit_performed": audit_sources,
                        "created_at": utc_now(), "previous_generation": (previous or {}).get("generation")}
            if (generation_dir / "manifest.json").exists():
                stored = load_json(generation_dir / "manifest.json")
                if stored.get("database_sha256") != manifest["database_sha256"] or stored.get("summary_index_sha256") != index_sha:
                    raise ValidationError("existing immutable memory generation changed")
                manifest = stored
            else:
                atomic_write_json(generation_dir / "manifest.json", manifest)
            atomic_write_json(root / "current.json", manifest)
            return {**receipt, "status": "published", "manifest_path": str(root / "current.json"),
                    "database_path": str(generation_dir / "memory.sqlite3"), "mirror_path": str(generation_dir / "safe-mirror")}
        finally:
            db.close()


def _mirror_markdown(record: dict[str, Any], generation: str, scopes: dict[str, Any], summary_sha256: str | None = None) -> str:
    key = record["key"]
    header = ["---", f"title: {_json(key)}", f"canonical_key: {_json(key)}", "type: english-memory-projection",
              "visibility: answer_free", f"summary_sha256: {_json(summary_sha256 or object_sha256(record))}",
              "locked: true",
              f"source_ids: {_json(scopes['source_ids'])}", f"unit_ids: {_json(scopes['unit_ids'])}",
              "qmd:", "  metadata:", f"    canonical_key: {_json(key)}", "    visibility: answer_free", "---", "",
              f"# {key}", "", "本页是已发布摘要的可重建投影；检索命中不证明本次错误或掌握。", "",
              "## 当前教学提示", "", record["teaching_hint"], "", "判断类型：legacy_unclassified（保留既有教学文本，未升级为逐结论事实）。", "",
              "## Observations", ""]
    for episode in record.get("episodes", []):
        for category, label, field, observed in (
                ("learner_first_attempt", "首答", "first_attempt", episode.get("observed_at") or episode.get("study_date")),
                ("teaching_correction", "纠正", "correction", episode.get("correction_observed_at") or episode.get("corrected_at")),
                ("learner_followup", "后续观察", "later_observation", episode.get("last_observed_at") or episode.get("later_observed_at")),
                ("hint_dependence", "提示依赖", "hint_dependence", episode.get("last_observed_at") or episode.get("later_observed_at")),
                ("teaching_action", "下一步", "next_action", None)):
            if episode.get(field):
                qualifier = ""
                if observed:
                    from datetime import datetime
                    try:
                        datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
                        qualifier = f"@occurred:{observed} "
                    except ValueError:
                        pass
                text = str(episode[field]).replace("\n", "\n  ")
                header.extend([f"- [{category}] {qualifier}{label}：{text}（观察ID：{episode['episode_id']}）", ""])
    header.extend(["## Relations", ""])
    for related in record.get("related_points", []):
        if isinstance(related, str):
            header.append(f"- authored_related_point [[{related}]]")
    for source_id in scopes["source_ids"]:
        header.append(f"- observed_in [[{source_id}]]")
    header.append("")
    header.extend(["## 来源定位", ""])
    for path in scopes["source_ids"]:
        header.append(f"- {path}")
    return "\n".join(header) + "\n"


def _write_mirror(db: sqlite3.Connection, directory: Path, generation: str, lifecycle: dict[str, Any]) -> dict[str, Any]:
    if (directory / "manifest.json").exists():
        previous = load_json(directory / "manifest.json")
        if previous.get("generation") != generation or previous.get("lifecycle_sha256") != lifecycle_fingerprint(lifecycle):
            raise ValidationError("mirror destination belongs to another immutable export; choose a new directory")
    directory.mkdir(parents=True, exist_ok=True)
    pending = lifecycle.get("pending_keys", {})
    invalid = set(lifecycle.get("invalidated_episode_ids", []))
    accepted = lifecycle.get("accepted_summary_hashes", {})
    written, excluded = [], []
    for key, digest, body, source_ids, unit_ids in db.execute(
            "SELECT point_key,summary_sha256,record_json,source_ids_json,unit_ids_json FROM points WHERE answer_safe=1 ORDER BY point_key"):
        record = json.loads(body)
        if lifecycle.get("global_pending") or key in pending:
            excluded.append(key)
            continue
        if key in lifecycle.get("invalidated_summary_keys", []) and accepted.get(key) != digest:
            excluded.append(key)
            continue
        if invalid & {e["episode_id"] for e in record["episodes"]}:
            if accepted.get(key) != digest:
                excluded.append(key)
                continue
            record["episodes"] = [e for e in record["episodes"] if e["episode_id"] not in invalid]
        if not record["episodes"] or not is_answer_safe(record):
            excluded.append(key)
            continue
        filename = key_hash(key) + ".md"
        (directory / filename).write_text(_mirror_markdown(record, generation,
            {"source_ids": json.loads(source_ids), "unit_ids": json.loads(unit_ids)}, digest), encoding="utf-8")
        written.append({"key": key, "file": filename, "summary_sha256": digest})
    manifest = {"schema_version": "english_memory_safe_mirror_v1", "generation": generation,
                "lifecycle_sha256": lifecycle_fingerprint(lifecycle),
                "entries": written, "excluded_keys": excluded, "raw_source_text_copied": False,
                "answer_visibility_authority": "published_summary_plus_inherited_restrictions",
                "leak_pattern_check_is_not_a_semantic_safety_proof": True}
    atomic_write_json(directory / "manifest.json", manifest)
    return manifest


def export_safe_mirror(repo: Path, state: Path, destination: Path | None = None, *,
                       lifecycle_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Export a lifecycle-filtered mirror for an explicitly initiated index job."""
    db, manifest = open_memory_projection(state)
    try:
        snapshot = _projection_snapshot(state, manifest)
        lifecycle = lifecycle_override if lifecycle_override is not None else read_lifecycle(state)
        if destination is None:
            version = object_sha256({"generation": manifest["generation"], "lifecycle": lifecycle_fingerprint(lifecycle)})
            destination = state / "memory/exports" / version / "safe-mirror"
        _assert_snapshot(state, manifest, snapshot)
        result = _write_mirror(db, destination, manifest["generation"], lifecycle)
        _assert_snapshot(state, manifest, snapshot)
        pointer = {"schema_version": "english_memory_backend_mirror_v1", "generation": manifest["generation"],
                   "summary_index_sha256": manifest["summary_index_sha256"], "lifecycle_sha256": lifecycle_fingerprint(lifecycle),
                   "lifecycle_hash_kind": "effective_state_without_revision", "claims_sha256": snapshot[2],
                   "mirror_path": str(destination.resolve()), "manifest_sha256": file_sha256(destination / "manifest.json")}
        atomic_write_json(state / "memory/backend-mirror.json", pointer)
        _assert_snapshot(state, manifest, snapshot)
        return {"status": "exported", "path": str(destination), "entry_count": len(result["entries"]),
                "excluded_count": len(result["excluded_keys"]), "generation": manifest["generation"],
                "manifest_sha256": pointer["manifest_sha256"], "backend_mirror_pointer": pointer, "formal_write_count": 0}
    finally:
        db.close()


def publish_managed_pages(repo: Path, state: Path, *, lifecycle_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write only dedicated managed blocks; all user-authored surrounding text stays."""
    db, manifest = open_memory_projection(state)
    try:
        snapshot = _projection_snapshot(state, manifest)
        directory = repo / "wiki/memory"
        _assert_snapshot(state, manifest, snapshot)
        directory.mkdir(parents=True, exist_ok=True)
        lifecycle = lifecycle_override if lifecycle_override is not None else read_lifecycle(state)
        begin, end = "<!-- english-memory:begin -->", "<!-- english-memory:end -->"
        updated, conflicts = [], []
        links = []
        current_names = set()
        safe_pages = {key: re.sub(r"[^\w.-]+", "-", key)[:70] + "-" + key_hash(key)[:8]
                      for (key,) in db.execute("SELECT point_key FROM points WHERE answer_safe=1")}
        def visible_link(label: str) -> str:
            if label in safe_pages:
                return f"[[{safe_pages[label]}|{label}]]"
            target = Path(label) if Path(label).is_absolute() else repo / label
            if target.is_file():
                try:
                    logical = target.absolute().relative_to(repo.absolute()).as_posix()
                    if target.suffix == ".md":
                        return f"[[{logical[:-3]}|{target.name}]]"
                except ValueError:
                    pass
                return f"[{target.name}]({target.resolve().as_uri()})"
            # A stable source ID is a relation label, not a nonexistent note.
            return "`" + label.replace("`", "") + "`"
        for key, digest, body, sources, units in db.execute(
                "SELECT point_key,summary_sha256,record_json,source_ids_json,unit_ids_json FROM points WHERE answer_safe=1 ORDER BY point_key"):
            name = re.sub(r"[^\w.-]+", "-", key)[:70] + "-" + key_hash(key)[:8] + ".md"
            current_names.add(name)
            path = directory / name
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            if old and (old.count(begin) != 1 or old.count(end) != 1 or old.index(begin) > old.index(end)):
                conflicts.append(name)
                continue
            record = json.loads(body)
            invalid = set(lifecycle.get("invalidated_episode_ids", [])) & {e["episode_id"] for e in record["episodes"]}
            pending = lifecycle.get("global_pending") or key in lifecycle.get("pending_keys", {})
            accepted = lifecycle.get("accepted_summary_hashes", {}).get(key) == digest
            invalid_hint = key in lifecycle.get("invalidated_summary_keys", []) and not accepted
            if pending or invalid_hint or (invalid and not accepted):
                content = f"# {key}\n\n当前摘要待更新，旧教学判断暂不作为当前依据。\n"
            else:
                record["episodes"] = [e for e in record["episodes"] if e["episode_id"] not in invalid]
                content = _mirror_markdown(record, manifest["generation"], {"source_ids": json.loads(sources), "unit_ids": json.loads(units)})
                # Frontmatter belongs to the derived mirror, not inside a block.
                content = content.split("---\n", 2)[-1].lstrip()
                content = re.sub(r"\[\[([^\]\n]+)\]\]", lambda match: visible_link(match.group(1)), content)
                content += f"\n摘要版本：`{digest}`\n\n## 原件与精确定位\n\n"
                references = {}
                for (audit_json,) in db.execute("SELECT s.audit_json FROM sources s JOIN dependencies d USING(source_key) WHERE d.point_key=? ORDER BY s.source_key", (key,)):
                    ref = json.loads(audit_json).get("reference", {})
                    path_text = ref.get("path")
                    if not isinstance(path_text, str):
                        continue
                    item = references.setdefault(path_text, set())
                    for field in ("locator", "pointer", "snapshot_pointer", "member"):
                        if isinstance(ref.get(field), str):
                            item.add(ref[field])
                    for field in ("locators", "pointers"):
                        item.update(x for x in ref.get(field, []) if isinstance(x, str))
                for path_text, locators in references.items():
                    content += f"- {visible_link(path_text)}"
                    if locators:
                        content += " — " + "；".join("`" + locator.replace("`", "") + "`" for locator in sorted(locators))
                    content += "\n"
            block = begin + "\n" + content + end
            text = (old[:old.index(begin)] + block + old[old.index(end) + len(end):]) if old else block + "\n"
            if text != old:
                _assert_snapshot(state, manifest, snapshot)
                atomic_write_text(path, text)
                updated.append(name)
            links.append(f"- [[{name[:-3]}|{key}]]")
        # Formerly published pages can become protected or removed. Retain the
        # file and user notes, replacing only our stale managed content.
        for path in directory.glob("*.md"):
            if path.name in current_names or path.name == "index.md":
                continue
            old = path.read_text(encoding="utf-8")
            if old.count(begin) == 1 and old.count(end) == 1:
                block = begin + "\n该派生页当前不可用于普通学习；原始记录仍保留。\n" + end
                text = old[:old.index(begin)] + block + old[old.index(end) + len(end):]
                if text != old:
                    _assert_snapshot(state, manifest, snapshot)
                    atomic_write_text(path, text)
                    updated.append(path.name)
        path = directory / "index.md"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        content = begin + "\n# 英语记忆导航\n\n只展示可用于普通学习的派生页面；个人判断仍以原始学习证据为依据。\n\n" + "\n".join(links) + "\n" + end
        if not old:
            _assert_snapshot(state, manifest, snapshot)
            atomic_write_text(path, content + "\n")
        elif old.count(begin) == 1 and old.count(end) == 1:
            _assert_snapshot(state, manifest, snapshot)
            atomic_write_text(path, old[:old.index(begin)] + content + old[old.index(end) + len(end):])
        else:
            conflicts.append("index.md")
        _assert_snapshot(state, manifest, snapshot)
        return {"status": "partial" if conflicts else "published", "generation": manifest["generation"],
                "page_count": len(links), "updated_count": len(updated), "conflicts": conflicts,
                "index_path": str(path), "formal_write_count": 0}
    finally:
        db.close()
