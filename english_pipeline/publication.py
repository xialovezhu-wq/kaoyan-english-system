"""Subject-owned complete source descriptors and the lock-free publication hook."""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .constants import REPO_ROOT
from .errors import ValidationError
from .runtime_paths import bridge_root
from .learning_state import formal_versions, invalidate_after_formal
from .util import atomic_write_json, file_sha256, load_json, object_sha256


PUBLISHER = bridge_root() / "study_publication.py"
LOCAL_QUALITY_POLICY = "native_teaching_readback_v1"
_PRIVATE_PARTS = {".git", ".obsidian", ".idea", "__pycache__", "node_modules", ".venv", "locks", "preimages", "staged", "transactions"}
_PRIVATE_NAMES = {".DS_Store", ".env", "config.toml", "credentials.json", "id_rsa", "id_ed25519"}
_PRIVATE_SUFFIXES = {".pyc", ".lock", ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".bak", ".backup"}


def _stamp(path: Path) -> tuple[int, int, int, int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _eligible(relative: Path) -> bool:
    if (relative.parts[:2] in {("intake","writing-sessions"),("intake","writing-view")} or
            relative.parts[:3]==("wiki","writing","每日作文摘要") or relative.as_posix()=="wiki/writing/个人作文学习摘要.md"):
        return False
    if any(part in _PRIVATE_PARTS or part.startswith(".staging-") for part in relative.parts):
        return False
    if relative.name in _PRIVATE_NAMES or relative.name.startswith(".env.") or relative.suffix in _PRIVATE_SUFFIXES:
        return False
    if relative.parts[:2] in {("intake", "learning-context"), ("intake", "publication"), ("intake", "memory"),
                             ("intake", "personal-summary"), ("wiki", "memory"),
                             ("intake", "memory-use"), ("intake", "sentence-teaching"),
                             ("intake", "evening-feedback")}:
        # Full point memory (including its source refs and old versions) is a
        # local derived asset. It has no protected per-question Study Library
        # export contract; do not include it in the generic raw file manifest.
        return False
    # Day pages are local teaching material. Older, separately scoped review artifacts
    # keep their existing publication behavior.
    if (relative.parts[:2] == ("review", "personalized") and len(relative.parts) == 4
            and relative.name == "index.html" and (relative.parts[2] == "history" or
            re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative.parts[2]))):
        return False
    # The semantic proposal is formal evidence. Rebuildable lookup generations,
    # objects and current pointers are not a substitute for that evidence.
    if relative.parts[:2] == ("intake", "sentence-support") and (len(relative.parts) < 3 or relative.parts[2] != "proposals"):
        return False
    if relative.parts[:2] == ("wiki", "old_words") and relative.suffix in {".csv", ".json"}:
        return False
    return True


def _role(relative: Path) -> tuple[str, str]:
    if relative.parts[:2] == ("wiki", "raw_archives"):
        return "private_archive_locator", "protected"
    if relative.parts[0] == "bank":
        return "formal_learning_record", "protected"  # user evidence can contain answers
    if relative.parts[0] == "review":
        return "formal_review_history", "protected"
    if relative.parts[0] == "articles":
        return "article_and_learning_history", "protected"  # practice source is separately addressable
    if relative.parts[:2] == ("raw", "protected"):
        return "answer_and_analysis", "protected"
    if relative.parts[:3] == ("raw", "articles", "exam-reading-corpus"):
        return "practice_source", "practice_safe"
    if relative.parts[0] in {"intake", "deferred-intake"}:
        return "learning_evidence_and_receipt", "protected"
    return "subject_source_and_index", "reference"


def build_source_descriptors(repo_root: Path, *, include_pending: bool = False) -> dict[str, Any]:
    """Return exact files, not summaries; never reads configuration or credentials.

    No publisher/source lock is created. Every enumerated source is hash-bound and
    restatted, and the publisher verifies it during its independent copy. A race
    fails instead of publishing a mixed version. Pending packages are retained as
    explicitly raw evidence, never counted as formally completed learning.
    """
    repo = repo_root.resolve(strict=True)
    before = formal_versions(repo)
    from .writing_publication import freeze_projection, summary_identity, verify_graph
    if (repo / 'raw/reference_relations/writing-vocabulary-foundation/manifest.json').exists() or repo == REPO_ROOT.resolve():
        verify_graph(repo)
    files: dict[str, dict[str, Any]] = {}
    stamps: dict[Path, tuple[int, int, int, int, int]] = {}
    gaps: list[dict[str, Any]] = []
    exclusions: list[dict[str, str]] = []

    def add(path: Path, relative: str, role: str, visibility: str, *, expected_sha: str | None = None) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"publication source is not a regular file: {relative}")
        stamps[path] = _stamp(path)
        digest = file_sha256(path)
        if expected_sha and digest != expected_sha:
            raise ValidationError(f"publication source differs from its canonical manifest: {relative}")
        row = {"source_path": str(path), "relative_path": relative, "role": role,
               "visibility": visibility, "sha256": digest, "bytes": path.stat().st_size,
               "required": True, "subject": "english", "storage": "git-lfs" if path.stat().st_size > 100 * 1024 * 1024 else "git"}
        if relative in files and files[relative]["sha256"] != digest:
            raise ValidationError("publication target maps to different English source bytes")
        files[relative] = row
        return row

    def private_writing_package(package):
        identity_path = package / "source.json"
        if not identity_path.is_file():
            return False
        identity = load_json(identity_path).get("identity", {})
        return bool(identity.get("writing_session") or str(identity.get("source_id","")).startswith("WRCOURSE-"))

    private_packages = {path.parent for path in (repo / "intake/packages").glob("*/*/source.json")
                        if private_writing_package(path.parent)}
    for directory in ("articles", "bank", "review", "raw", "wiki", "intake", "deferred-intake"):
        root = repo / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(repo)
            if any(parent in private_packages for parent in path.parents):
                continue
            if not _eligible(relative):
                prefix = relative.parts[:2]
                if prefix not in {("intake", "memory-use"), ("intake", "sentence-teaching")}:
                    exclusions.append({"path": relative.as_posix(), "reason": "machine_or_rebuildable_runtime_state"})
                continue
            role, visibility = _role(relative)
            if relative.parts[:2] == ("intake", "packages"):
                role = "immutable_conversation_package"
            add(path, relative.as_posix(), role, visibility)

    exclusions.extend({"path": folder, "reason": "local_only_personalized_derived_assets"}
                      for folder in ("intake/memory-use", "intake/sentence-teaching"))
    originals: list[dict[str, Any]] = []
    manifests = [
        ("raw/reference_sources/exam_pdf_manifest.json", "sources", "external_path"),
        ("raw/writing_reference/manifest.json", "sources", "external_source_path"),
    ]
    for rel, collection, locator in manifests:
        path = repo / rel
        if path.is_file():
            originals.extend({**row, "source_path": row[locator]} for row in load_json(path).get(collection, []))
    syllabus_path = repo / "raw/reference_sources/syllabus_vocabulary/manifest.json"
    if syllabus_path.is_file():
        row = load_json(syllabus_path)
        originals.append({"source_id": row["source_id"], "source_path": row["source_pdf"],
                          "sha256": row["source_sha256"], "pages": row["pdf_pages"], "role": "syllabus_reference"})
    source_pages: dict[str, dict[int, list[str]]] = {}
    for path in sorted((repo / "raw/articles/exam-reading-corpus").glob("20*/text-*.json")):
        row = load_json(path)
        pages = row.get("truth_pages", [])
        if pages:
            for page in range(int(pages[0]), int(pages[-1]) + 1):
                source_pages.setdefault(row["truth_pdf"], {}).setdefault(page, []).append(path.relative_to(repo).as_posix())
    for path in sorted((repo / "raw/protected/exam-reading-analysis").glob("20*/text-*.json")):
        row = load_json(path)
        for page in row.get("preprocessed_source_pages", []):
            rel = f"raw/protected/exam-reading-analysis/source-pages/{row['year']}/page-{int(page):03d}.json"
            source_pages.setdefault(row["source_pdf"], {}).setdefault(int(page), []).append(rel)
    original_mappings = []
    for row in originals:
        source = Path(row["source_path"])
        source_id = str(row["source_id"])
        relative = (Path("published-originals") / source_id / source.name).as_posix()
        visibility = "protected" if "analysis" in str(row.get("role", "")) or "解析" in source.name else "reference"
        if row.get("role") == "exam_paper_authority":
            visibility = "practice_safe"
        if not source.is_file():
            gaps.append({"code": "original_missing", "source_id": source_id, "source_path": str(source)})
            continue
        descriptor = add(source, relative, "original_pdf", visibility, expected_sha=row.get("sha256"))
        pages = source_pages.get(str(source), {})
        if row.get("role") == "syllabus_reference" or source_id.startswith("WRITING-"):
            pages = {page: [] for page in range(1, int(row.get("pages", 0)) + 1)}
        original_mappings.append({"source_id": source_id, "source_path": str(source), "publish_path": relative,
                                  "sha256": descriptor["sha256"], "visibility": visibility,
                                  "pages": [{"source_id": source_id, "source_path": str(source), "page_number": page,
                                             "existing_page_file": None,
                                             "raw_evidence_paths": sorted(set(refs)),
                                             "publish_path": f"published-originals/{source_id}/pages/page-{page:04d}.pdf"}
                                            for page, refs in sorted(pages.items())]})

    # Archived packages are reopened only through their exact local pointer.
    pointers = sorted((repo / "intake/archive-pointers").glob("*/*.json"))
    if pointers:
        if repo != REPO_ROOT.resolve():
            raise ValidationError("fixture publication cannot access the production archive")
        from .archive import resolve_archived_package_from_pointer, verify_volume_contract, VolumeContract
        volume = verify_volume_contract(VolumeContract())
        for pointer in pointers:
            document = load_json(pointer)
            package_id = document["package_id"]
            package_root = resolve_archived_package_from_pointer(repo / "intake", repo, package_id=package_id,
                                                                 study_date=pointer.parent.name)
            package_root.relative_to(volume["subject_root"])
            if private_writing_package(package_root):
                exclusions.append({"path": f"intake/packages/{pointer.parent.name}/{package_id}", "reason": "private_writing_original"})
                continue
            for path in sorted(package_root.rglob("*")):
                if path.is_file():
                    target = f"intake/packages/{pointer.parent.name}/{package_id}/{path.relative_to(package_root).as_posix()}"
                    add(path, target, "archived_immutable_conversation_package", "protected")
    if not (repo / "intake/packages").exists() and not pointers:
        gaps.append({"code": "no_current_complete_conversation_packages", "scope": "existing articles and legacy events preserved; historical full dialogue not synthesized"})
    personal = None
    bindings = []
    state = repo / "intake"
    if (state / "memory/current.json").is_file() or repo == REPO_ROOT.resolve():
        personal, members = freeze_projection(state)
        for name, path in members.items():
            add(path, name, "bounded_teaching_projection", "answer_free")
        for name in ("personal-summary/current.json", "memory/current.json", "memory/lifecycle.json"):
            path = state / name
            if path.is_file():
                bindings.append(dict(source_path=str(path), bytes=path.stat().st_size, sha256=file_sha256(path)))
        if summary_identity(state).get("version") != personal["version"]:
            raise ValidationError("personal summary changed before descriptor completion")
    after = formal_versions(repo)
    if before != after or any(not path.exists() or _stamp(path) != stamp for path, stamp in stamps.items()):
        raise ValidationError("English publication sources changed during descriptor construction")
    return {"schema_version": "english_publication_sources_v1", "subject": "english", "repo_root": str(repo),
            "files": [files[key] for key in sorted(files)], "file_count": len(files),
            "local_version": object_sha256({"formal": after, "files": {key: files[key]["sha256"] for key in sorted(files)}}),
            "formal_version": object_sha256(after), "formal_source_hashes": after,
            "additional_source_bindings": bindings,
            **({"personalization": personal} if personal else {}),
            "source_lock": str(repo / "intake/locks/formal.lock"),
            "source_consistency": "hash_bound_and_restat; publisher verifies during copy",
            "entrypoints": ["articles", "bank/master_bank.csv", "bank/sentence_patterns.md", "review",
                            "raw/articles/exam-reading-corpus/index.md", "raw/protected/exam-reading-analysis/index.json"],
            "history_index": [entry for entry in ("articles", "review", "bank/learning_events.jsonl", "bank/review_exclusion_ledger.jsonl", "intake/events", "intake/packages")
                              if entry in files or any(path.startswith(entry + "/") for path in files)],
            "history_state": {entry: "present" if (repo / entry).exists() else "not_created"
                              for entry in ("bank/learning_events.jsonl", "bank/review_exclusion_ledger.jsonl")},
            "original_mappings": original_mappings, "gaps": gaps, "exclusions": exclusions,
            "pending_package_policy": "raw_only_never_a_formal_terminal", "include_pending_requested": include_pending,
            "formal_write_count": 0}


def _complete_snapshot_closeout(repo_root: Path, state_dir: Path, receipt: dict[str, Any], *,
                             runner: Callable[..., dict[str, Any]] | None = None,
                             retry_publication: bool = False,
                             archive_basis_sha256: str | None = None,
                             memory_generation: str | None = None) -> dict[str, Any]:
    """Call only after releasing the formal lock; retries never invoke a writer."""
    if receipt.get("mode") != "apply" or receipt.get("status") not in {"APPLIED", "PARTIAL", "NO_ACTION"}:
        raise ValidationError("publication requires a successful formal closeout")
    writer_event_id = str(receipt["receipt_id"])
    if not re.fullmatch(r"EN-RECEIPT-[0-9A-F]{16}", writer_event_id):
        raise ValidationError("publication closeout receipt identity is invalid")
    event_id = writer_event_id
    if archive_basis_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", archive_basis_sha256):
            raise ValidationError("archive publication basis must be a SHA-256")
        # This is a publication version, never a new formal learning event.
        event_id += ":closeout:" + archive_basis_sha256[:24]
    if memory_generation:
        # A completed teaching generation needs its own snapshot identity. This
        # never creates a second learning event or invokes the formal writer.
        event_id += ":memory:" + object_sha256(memory_generation)[:24]
    if memory_generation:
        from .writing_publication import summary_identity
        identity=summary_identity(state_dir)
        if identity.get("status")=="available":
            event_id+=":teaching:"+identity["version"][:24]
    target = state_dir / "publication" / f"{event_id}.json"
    version: dict[str, Any] = {}
    pending_component = "local_context"
    try:
        version = invalidate_after_formal(repo_root, state_dir, event_id=writer_event_id)
        if runner is None and repo_root.resolve() != REPO_ROOT.resolve():
            return {"status": "NOT_PRODUCTION_REPO", "formal_version": version["formal_version"]}
        pending_component = "publication"
        previous = load_json(target) if target.is_file() else None
        previous_delivery = (previous or {}).get("publication", {})
        if (previous and (previous.get("snapshot_status") or previous.get("status")) == "PUBLISHED"
                and (runner is not None or previous_delivery.get("publication_target") != "github")):
            return previous
        if previous and runner is None and not retry_publication and previous.get("formal_version") == version["formal_version"]:
            public_path = Path(str(previous.get("publication", {}).get("receipt_path") or ""))
            try:
                public_path.resolve().relative_to((PUBLISHER.parent / "publication").resolve())
                public_status = load_json(public_path).get("status") if public_path.is_file() else None
            except (ValueError, OSError):
                public_status = None
            if public_status == "LOCAL_SNAPSHOT_PENDING" or (
                public_status is None and previous.get("status") == "local_applied_snapshot_pending"
            ):
                return {**previous, "status": "local_applied_snapshot_pending", "retry_suppressed": "unchanged_failed_publication"}
            # A historical remote failure cannot suppress the local reader snapshot.
        if runner is None:
            if not PUBLISHER.is_file():
                raise FileNotFoundError("approved publisher is unavailable")
            spec = importlib.util.spec_from_file_location("english_study_publication", PUBLISHER)
            if spec is None or spec.loader is None:
                raise ImportError("publisher loader unavailable")
            module = importlib.util.module_from_spec(spec)
            if str(PUBLISHER.parent) not in sys.path:
                sys.path.insert(0, str(PUBLISHER.parent))
            spec.loader.exec_module(module)
            runner = module.request_publication
        result = runner(subject="english", event_id=event_id, repo_root=repo_root.resolve())
        value = {"schema_version": "english_publication_hook_receipt_v1", "event_id": event_id,
                 "archive_basis_sha256": archive_basis_sha256,
                 "formal_version": version["formal_version"], "publication": result,
                 "status": result.get("status", "PENDING"), "pending_component": None if result.get("status") == "PUBLISHED" else "publication",
                 "formal_apply_invocation_count": 0}
    except Exception as exc:
        value = {"schema_version": "english_publication_hook_receipt_v1", "event_id": event_id,
                 "archive_basis_sha256": archive_basis_sha256,
                 "formal_version": version.get("formal_version"),
                 "status": "local_applied_snapshot_pending" if pending_component == "publication" else "FORMAL_COMMITTED_CONTEXT_PENDING",
                 "pending_component": pending_component, "error_type": type(exc).__name__, "formal_apply_invocation_count": 0}
    try:
        atomic_write_json(target, value)
    except OSError as exc:
        value["receipt_write_error"] = type(exc).__name__
    return value


def verify_native_readback(repo: Path, state: Path, formal_version: str,
                           memory: dict[str, Any]) -> dict[str, Any]:
    """Reopen the same guarded point view that local English teaching uses."""
    from .memory_query import query_memory_context
    from .memory_store import open_memory_projection
    from .personal_summary import OBSERVATION_ORDER_AMBIGUOUS, key_hash

    result = {"formal_version": formal_version, "points": {}, "memory_status": memory["status"]}
    if memory["status"] == "ready" and memory.get("affected_keys"):
        index_path = state / "personal-summary/current.json"
        index_sha = file_sha256(index_path)
        routes = load_json(index_path)["routes"]
        db, manifest = open_memory_projection(state)
        generation = manifest["generation"]
        if manifest["summary_index_sha256"] != index_sha:
            db.close()
            raise ValidationError("native memory generation differs from current summary index")
        with closing(db):
            for key in sorted(set(memory["affected_keys"])):
                row = db.execute("SELECT summary_sha256, answer_safe FROM points WHERE point_key=?", (key,)).fetchone()
                expected = routes.get(key_hash(key), {}).get("sha256")
                if not row or not expected or row["summary_sha256"] != expected:
                    raise ValidationError("native memory summary identity mismatch:" + key)
                output = query_memory_context(repo, state, keys=[key], view="teaching", max_bytes=16384)
                if (output.get("generation") != generation or output.get("summary_index_sha256") != index_sha
                        or not output.get("coverage", {}).get("projection_available")):
                    raise ValidationError("native memory readback version mismatch:" + key)
                matches = output.get("matched", [])
                invalidation = {"key": key, "reason": "invalidated_summary_requires_refresh"}
                if not matches and output.get("status") == "pending" and output.get("pending") == [invalidation]:
                    visibility = "invalidated"
                elif (row["answer_safe"] and not matches and output.get("status") == "unavailable"
                      and any(item.get("key") == key and item.get("reason") == OBSERVATION_ORDER_AMBIGUOUS
                              and len(item.get("episode_ids", [])) > 1 for item in output.get("unavailable", []))):
                    visibility = OBSERVATION_ORDER_AMBIGUOUS
                elif row["answer_safe"]:
                    if (output.get("status") != "ready" or len(matches) != 1 or matches[0].get("key") != key
                            or matches[0].get("summary_sha256") != expected
                            or matches[0].get("visibility") != "answer_free"
                            or not matches[0].get("latest_observation")):
                        raise ValidationError("native teaching summary not available:" + key)
                    visibility = "answer_free"
                else:
                    if matches or not any(item.get("key") == key and item.get("reason") == "protected"
                                          for item in output.get("unavailable", [])):
                        raise ValidationError("native teaching answer protection failed:" + key)
                    visibility = "withheld"
                result["points"][key] = {"summary_sha256": expected, "teaching_visibility": visibility,
                                          "readback_sha256": object_sha256(output)}
        current_db, current = open_memory_projection(state)
        with closing(current_db):
            if (current["generation"], file_sha256(index_path)) != (generation, index_sha):
                raise ValidationError("native memory changed during readback")
        result.update(memory_generation=generation, summary_index_sha256=index_sha)
    if object_sha256(formal_versions(repo)) != formal_version:
        raise ValidationError("formal version changed during native readback")
    return result


def local_quality_pending(state: Path, writer_receipt_path: Path) -> str | None:
    """Check the local seal for a verified writer receipt, including legacy inputs."""
    try:
        receipt = load_json(writer_receipt_path)
        if not isinstance(receipt, dict):
            return "native_readback"
        rid = str(receipt.get("receipt_id", ""))
        if not re.fullmatch(r"EN-RECEIPT-[0-9A-F]{16}", rid):
            return "native_readback"
        path = state / "receipts/local-quality" / f"{rid}.json"
        if not path.is_file() and receipt.get("local_quality_policy") is None:
            return None
        result = load_json(path)
        if (not isinstance(result, dict) or result.get("schema_version") != "english_native_closeout_receipt_v1"
                or result.get("writer_receipt_id") != rid
                or result.get("writer_receipt_sha256") != object_sha256(receipt)):
            return "native_readback"
        return None if result.get("status") == "VERIFIED_CURRENT" else result.get("pending_component") or "native_readback"
    except (OSError, ValueError, TypeError, KeyError):
        return "native_readback"


def complete_formal_closeout(repo_root: Path, state_dir: Path, receipt: dict[str, Any], *,
                             runner: Callable[..., dict[str, Any]] | None = None,
                             retry_publication: bool = False,
                             archive_basis_sha256: str | None = None) -> dict[str, Any]:
    """Seal the local readback and optionally request a separate replica."""
    from .memory_lifecycle import mark_formal_memory_pending, recover_memory_closeout
    rid = str(receipt.get("receipt_id", ""))
    if (receipt.get("mode") != "apply" or receipt.get("status") not in {"APPLIED", "PARTIAL", "NO_ACTION"}
            or not re.fullmatch(r"EN-RECEIPT-[0-9A-F]{16}", rid)):
        raise ValidationError("local verification requires a successful formal closeout")
    if archive_basis_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", archive_basis_sha256):
        raise ValidationError("archive closeout basis must be a SHA-256")
    try:
        memory = mark_formal_memory_pending(repo_root, state_dir, receipt)
        from .writing_author import applies, automatic_profile
        if applies(state_dir,receipt) and memory.get("pending_component")!="memory_index":
            memory=automatic_profile(repo_root,state_dir,receipt)
        if memory.get("pending_component") == "memory_index":
            memory = recover_memory_closeout(repo_root, state_dir, receipt["receipt_id"])
    except (OSError, ValueError, ValidationError) as exc:
        memory = {"status": "pending", "pending_component": "personal_summary",
                  "error_type": type(exc).__name__, "formal_write_count": 0}
    from .sentence_visuals import stage_status
    teaching = stage_status(state_dir, rid)
    local = {"schema_version": "english_native_closeout_receipt_v1", "writer_receipt_id": rid,
             "writer_receipt_sha256": object_sha256(receipt), "archive_basis_sha256": archive_basis_sha256,
             "formal_apply_invocation_count": 0, "memory": memory}
    if teaching:
        local["sentence_teaching"] = teaching
    component = "local_context"
    try:
        if any(row.get("writing_contract")=="english-read-write/1" for row in receipt.get("action_results", [])):
            component = "writing_view"
            from .writing_workspace import rebuild
            local["writing_view"] = rebuild(repo_root, state_dir, receipt)
            component = "local_context"
        version = invalidate_after_formal(repo_root, state_dir, event_id=rid)["formal_version"]
        local["formal_version"] = version
        component = "native_readback"
        if memory.get("status") in {"ready", "LEGACY_NOT_REQUIRED", "NOT_ENABLED"}:
            local["native_teaching_readback"] = verify_native_readback(repo_root, state_dir, version, memory)
            local.update(status="VERIFIED_CURRENT", verification_basis="native_teaching_readback", pending_component=None)
        else:
            local.update(status="FORMAL_COMMITTED_MEMORY_PENDING",
                         pending_component=memory.get("pending_component", "personal_summary"))
    except (OSError, ValueError, KeyError, sqlite3.Error, ValidationError) as exc:
        local.update(status="FORMAL_COMMITTED_CONTEXT_PENDING", pending_component=component,
                     reason=str(exc))
    try:
        atomic_write_json(state_dir / "receipts/local-quality" / f"{rid}.json", local)
    except OSError as exc:
        local.update(status="FORMAL_COMMITTED_CONTEXT_PENDING", pending_component="quality_receipt", reason=type(exc).__name__)
    if runner is not None or retry_publication:
        delivery = _complete_snapshot_closeout(repo_root, state_dir, receipt, runner=runner,
            retry_publication=retry_publication, archive_basis_sha256=archive_basis_sha256,
            memory_generation=memory.get("memory_generation") if memory.get("status") == "ready" else None)
        if memory.get("status") not in {"NOT_ENABLED", "LEGACY_NOT_REQUIRED"}:
            delivery = {**delivery, "snapshot_status": delivery.get("snapshot_status") or delivery.get("status")}
            if memory.get("status") != "ready":
                delivery.update(status="FORMAL_COMMITTED_MEMORY_PENDING", pending_component=memory.get("pending_component"))
    else:
        delivery = {"status": "NOT_REQUESTED", "reason": "explicit_publication_required"}
    from .evening_feedback import after_formal
    return {**delivery, "memory": memory, "local_quality": local, "evening_feedback": after_formal(repo_root,state_dir,receipt,local),
            "formal_apply_invocation_count": 0}
