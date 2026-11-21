"""Native transport and preparation for the English web-review workflow.

Models supply semantic decisions. This module preserves their original artifacts,
binds them to native packages and delegates every formal write to the existing writer.
An optional evidence pre-screen produces advisory sidecars only; it never changes
the deterministic validation, review template, receipt identity or writer gates.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import stat
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from .errors import PipelineError, ValidationError, IdempotencyConflict
from .cold_storage import cold_artifacts, cold_directory, guard_write
from .runtime_paths import REPO_ROOT, bridge_root
from .packages import (validate_conversation_package, processed_package_sha256s,
                       create_conversation_package, validate_evidence_refs,
                       whole_article_unit_id)
from .util import (canonical_bytes, file_sha256, bytes_sha256, object_sha256,
                   load_json, atomic_write_json, utc_now, exclusive_lock)

CONTRACT = "english-web-review-v1"
PACKAGE_ID = re.compile(r"^EN-PKG-(\d{8})-[A-F0-9]{16}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
BRIDGE = bridge_root()


def day(value: str | None = None) -> str:
    value = value or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if datetime.strptime(value, "%Y-%m-%d").date().isoformat() != value:
        raise ValidationError("date must be YYYY-MM-DD in Asia/Shanghai")
    return value


def immutable(path: Path, data: bytes) -> None:
    from .packages import _write_immutable
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise IdempotencyConflict(f"immutable web artifact differs: {path}")
    else:
        guard_write(path, data)
        _write_immutable(path, data)


def jwrite(path: Path, value: Any) -> None:
    immutable(path, canonical_bytes(value))


def local_snapshot() -> dict[str, Any]:
    sys.path.insert(0, str(BRIDGE)) if str(BRIDGE) not in sys.path else None
    from study_mcp import LibraryStore
    store = LibraryStore(BRIDGE)
    try:
        snap = store.snapshot("english")
        fresh = store.freshness(snap)
        if fresh["status"] != "current":
            raise ValidationError("MCP_SNAPSHOT_PENDING: refresh the existing English publication before bundling")
        identity = snap.identity()
        return {"source_commit": identity["source_commit"],
                "snapshot_id": identity.get("snapshot_id", identity["source_commit"]),
                "publication_target": "local", "formal_version": fresh["current_formal_version"]}
    finally:
        store.close()


def package_root(state: Path, repo: Path, package_id: str) -> Path:
    match = PACKAGE_ID.fullmatch(package_id)
    if not match:
        raise ValidationError("invalid English package ID")
    token = match[1]
    date = day(f"{token[:4]}-{token[4:6]}-{token[6:]}")
    root = state / "packages" / date / package_id
    if root.exists():
        validate_conversation_package(root)
        return root
    from .archive import resolve_archived_package_from_pointer, verify_volume_contract, VolumeContract
    volume = verify_volume_contract(VolumeContract())
    root = resolve_archived_package_from_pointer(state, repo, study_date=date, package_id=package_id)
    root.relative_to(volume["subject_root"])
    validate_conversation_package(root)
    return root


def package_files(root: Path, prefix: str = "") -> dict[str, bytes]:
    m = validate_conversation_package(root)
    paths = set(m["files"]) | {"manifest.json", "receipt.json"}
    return {prefix + p: (root / p).read_bytes() for p in sorted(paths)}


def zipped(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def inventory(files: dict[str, bytes]) -> list[dict[str, Any]]:
    return [{"path": k, "size": len(v), "sha256": bytes_sha256(v)} for k, v in sorted(files.items())]


def checked_zip(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 20000 or sum(i.file_size for i in infos) > 512 * 1024 * 1024:
            raise ValidationError("web ZIP exceeds the bounded archive size")
        files = {}
        for info in infos:
            p = PurePosixPath(info.filename)
            if (info.is_dir() or p.is_absolute() or ".." in p.parts or "\\" in info.filename
                    or str(p) != info.filename or info.filename in files
                    or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                raise ValidationError("web ZIP contains an unsafe or duplicate member")
            files[info.filename] = archive.read(info)
    if "manifest.json" not in files:
        raise ValidationError("web ZIP is missing its root manifest.json")
    manifest = json.loads(files["manifest.json"])
    listed = manifest.get("files", [])
    if not isinstance(listed, list) or listed != inventory({k: v for k, v in files.items() if k != "manifest.json"}):
        raise ValidationError("web ZIP file inventory or actual hash differs")
    return manifest, files


@cold_artifacts("segment_zip")
def segment_zip(repo: Path, state: Path, package_id: str) -> dict[str, Any]:
    root = package_root(state, repo, package_id)
    m = validate_conversation_package(root)
    data = zipped(package_files(root))
    target = state / "web-review" / "segments" / m["study_date"] / (package_id + ".zip")
    if not target.exists():
        target = cold_directory(target.parent / package_id, "segments", package_id) / target.name
    immutable(target, data)
    with zipfile.ZipFile(target) as archive:
        if any(archive.read(k) != v for k, v in package_files(root).items()):
            raise ValidationError("segment ZIP differs from native package")
    return {"status": "READY", "package_id": package_id, "zip_path": str(target),
            "zip_sha256": bytes_sha256(data), "formal_write_count": 0}


def empty_support(root: Path) -> dict[str, Any]:
    from .sentence_support import _source_identity_from_package
    identity = _source_identity_from_package(root)
    pid, sha = identity.pop("package_id"), identity.pop("package_sha256")
    safe = (identity["source_kind"] in {"article", "user_provided"}
            and identity["answer_exposure"] == "answer_free" and identity["question_id"] is None
            and identity["segment_type"] == "sentence")
    return {"package_id": pid, "package_sha256": sha,
            "record_kind": "sentence" if safe else "protected_sidecar",
            "source_identity": identity,
            "evidence_refs": [{"package_id": pid, "package_sha256": sha,
                               "pointer": "conversation.json#/messages/0",
                               "kind": load_json(root / "conversation.json")["messages"][0]["role"] + "_message"}],
            "paragraph_context": {"text": None, "locator": None, "evidence_ref_indexes": []},
            "history_episodes": [],
            "teaching_cues": {k: [] for k in ("correct_observations", "first_breaks", "structure_cues",
                                              "meaning_constraints", "effective_explanations", "next_read_actions")},
            "vocabulary_candidates": []}


def select_bundle_roots(repo: Path, state: Path, *, date: str | None = None, source_id: str | None = None,
                        package_ids: list[str] | None = None):
    requested_date = date
    date = day(date)
    ids = set(package_ids or [])
    if not ids:
        date_pattern = requested_date or "*"
        ids = {p.parent.name for p in (state / "packages").glob(date_pattern + "/*/manifest.json")}
        ids |= {p.stem for p in (state / "archive-pointers").glob(date_pattern + "/*.json")}
    roots = []
    for pid in sorted(ids):
        if not package_ids:
            from .backlog import _trusted_consumed_pointer
            match = PACKAGE_ID.fullmatch(pid)
            if not match:
                raise ValidationError("invalid English package ID in pending inventory")
            token = match[1]
            original_date = f"{token[:4]}-{token[4:6]}-{token[6:]}"
            if _trusted_consumed_pointer(state, original_date, pid):
                continue
        root = package_root(state, repo, pid)
        m = validate_conversation_package(root)
        if not package_ids and requested_date and m["study_date"] != requested_date:
            raise ValidationError("daily bundle accidentally includes another study date")
        if source_id and m["source_identity"].get("source_id") != source_id:
            continue
        roots.append(root)
    legacy = list((state / "events").glob((requested_date or "*") + "/*.json"))
    return date, roots, legacy


def _web_teaching_context(state: Path) -> dict[str, bytes]:
    """Export a bounded, answer-free view; old protected bytes stay local."""
    import sqlite3

    from .memory_query import _episode_public
    from .memory_store import is_answer_safe, lifecycle_fingerprint, open_memory_projection, read_lifecycle
    from .personal_summary import INDEX, SCHEMA, _latest_episode, _read_record, key_hash

    root = state / "personal-summary"
    index_path = root / "current.json"
    if not index_path.is_file():
        return {}
    prefix = "context/personal-summary/"
    routes, exported = {}, {}
    index_sha = file_sha256(index_path)
    index = load_json(index_path)
    if index.get("schema_version") != INDEX:
        raise ValidationError("personal summary index version mismatch")
    pointers = {p["key"]: p for p in index["routes"].values()}
    lifecycle = read_lifecycle(state)
    lifecycle_sha = lifecycle_fingerprint(lifecycle)
    invalid = set(lifecycle.get("invalidated_episode_ids", []))
    accepted = lifecycle.get("accepted_summary_hashes", {})
    generation = None
    reason = None
    try:
        db, manifest = open_memory_projection(state)
    except (OSError, ValueError, sqlite3.Error, ValidationError):
        reason = "projection_unavailable"
    else:
        try:
            if manifest["summary_index_sha256"] != index_sha:
                reason = "projection_not_current"
            elif lifecycle.get("global_pending"):
                reason = "summary_update_pending"
            else:
                generation = manifest["generation"]
                for key, digest, question_ids in db.execute(
                        "SELECT point_key,summary_sha256,question_ids_json FROM points WHERE answer_safe=1 ORDER BY point_key"):
                    pointer = pointers.get(key, {})
                    if pointer.get("sha256") != digest or key in lifecycle.get("pending_keys", {}):
                        continue
                    record = _read_record(root, pointer)
                    if not is_answer_safe(record, question_bound=bool(json.loads(question_ids))):
                        continue
                    ids = {e["episode_id"] for e in record["episodes"]}
                    if (ids & invalid or key in lifecycle.get("invalidated_summary_keys", [])) and accepted.get(key) != digest:
                        continue
                    active = [e for e in record["episodes"] if e["episode_id"] not in invalid]
                    if not active:
                        continue
                    current = {**record, "episodes": active}
                    if current.get("latest_episode_id") in invalid:
                        current.pop("latest_episode_id")
                    latest = _latest_episode(current)
                    if not latest:
                        continue
                    # No raw refs, quotes, or previous answer trajectories go
                    # in the ZIP. The compact view is advisory, not evidence.
                    view = {"schema_version": SCHEMA, "key": key, "aliases": record["aliases"],
                            "visibility": "answer_free", "publication_basis": "bounded_web_teaching_view",
                            "source_summary_sha256": digest, "teaching_hint": record["teaching_hint"],
                            "episodes": [_episode_public(latest)], "episodes_omitted": len(record["episodes"]) - 1}
                    if record.get("contrasts"):
                        contrasts = [{k: v for k, v in c.items() if k != "evidence_note"}
                                     for c in record["contrasts"] if set(c["source_episode_ids"]) == {latest["episode_id"]}]
                        if contrasts:
                            view["contrasts"] = contrasts
                    view_sha = object_sha256(view)
                    exported[prefix + "objects/" + view_sha + ".json"] = canonical_bytes(view)
                    for alias in [key, *record["aliases"]]:
                        routes[key_hash(alias)] = {"key": key, "sha256": view_sha}
        finally:
            db.close()
    if (file_sha256(index_path) != index_sha or lifecycle_fingerprint(read_lifecycle(state)) != lifecycle_sha
            or generation and load_json(state / "memory/current.json").get("generation") != generation):
        raise ValidationError("personal memory changed during web bundle freeze")
    exported[prefix + "current.json"] = canonical_bytes(
        {"schema_version": INDEX, "routes": routes, "snapshot_kind": "bounded_answer_free_advisory",
         "coverage": {"state": reason or "available", "generation": generation,
                      "included_points": len({p["key"] for p in routes.values()}), "total_local_points": len(pointers)}})
    exported[prefix + "README.txt"] = (
        "Bounded answer-free advice context. Latest intact observation only; no raw evidence is embedded here.\n"
        "Objects are content-addressed exports; source_summary_sha256 names the canonical local source.\n"
        "An absent route can be protected, pending, unavailable, or missing. Do not infer learner state.\n"
        "Selected original Capture packages are a separate, authorized curation surface.\n"
    ).encode("utf-8")
    return exported


def web_reference_files(repo: Path, state: Path) -> dict[str, bytes]:
    """Freeze the instructions and existing point notes once for every part."""
    paths = {
        "contracts/typed-actions.md": "codex-skill-sources/kaoyan-english-daily-intake-curation/references/typed-actions-contract.md",
        "contracts/sentence-support.schema.json": "schema/english_pipeline/sentence-support-proposal-v1.schema.json",
        "contracts/formal-fields.json": "schema/english_pipeline/web-formal-fields.json",
        "contracts/learning-event.schema.json": "schema/english_pipeline/learning-event-v1.schema.json",
        "contracts/enhancement-deliverables.md": "web-projects/english/ENHANCEMENT_DELIVERABLES.md",
        "contracts/point-personal-summary.md": "codex-skill-sources/kaoyan-english-intensive-reading/references/point-personal-summary.md",
        "contracts/abc-priority-rules.md": "codex-skill-sources/kaoyan-english-vocab-export/references/abc-priority-rules.md",
        "contracts/bbdc-export-format.md": "codex-skill-sources/kaoyan-english-vocab-export/references/bbdc-export-format.md",
        "contracts/reference-grounded-examples.md": "schema/reference_grounded_examples.md",
        "WEB_INSTRUCTIONS.txt": "web-projects/english/PROJECT_INSTRUCTIONS.txt",
        "build_return.py": "scripts/build_english_web_return.py",
    }
    files = {name: (repo / path).read_bytes() for name, path in paths.items() if (repo / path).is_file()}
    files.update(_web_teaching_context(state))
    return files


@cold_artifacts("bundle")
def bundle(repo: Path, state: Path, *, date: str | None = None, source_id: str | None = None,
           package_ids: list[str] | None = None, snapshot: dict[str, Any] | None = None,
           part_prompt: str | None = None, reference_files: dict[str, bytes] | None = None) -> dict[str, Any]:
    requested_date = date
    date, roots, legacy = select_bundle_roots(repo, state, date=date, source_id=source_id, package_ids=package_ids)
    if not roots:
        return {"status": "NOOP", "date": date,
                "package_count": 0, "legacy_event_count": len(legacy), "zip_path": None,
                "formal_write_count": 0, "reason": "No complete conversation packages in the requested scope"}
    snapshot = snapshot or local_snapshot()
    processed = processed_package_sha256s(state)
    files: dict[str, bytes] = {}
    rows, items = [], []
    for root in roots:
        m = validate_conversation_package(root)
        pid, sha = m["package_id"], m["package_canonical_sha256"]
        files.update(package_files(root, f"packages/{pid}/"))
        row = {"package_id": pid, "package_sha256": sha, "study_date": m["study_date"],
               "source_id": m["source_identity"].get("source_id"),
               "formal_status": "already_formal" if sha in processed else "awaiting_review",
               "missing_fields": m.get("missing_fields", [])}
        rows.append(row)
        try:
            support = empty_support(root)
            role = load_json(root / "conversation.json")["messages"][0]["role"]
            support["evidence_refs"][0]["kind"] = role + "_message"
        except ValidationError:
            support = None
        items.append({**row, "decision": "needs_context", "reason": "", "actions": [],
                      "support_record": support, "uncertainties": []})
    files.update(web_reference_files(repo, state) if reference_files is None else reference_files)
    if part_prompt is not None:
        files["PART_PROMPT.txt"] = part_prompt.encode("utf-8")
    core = {"schema_version": "english_web_bundle_v1", "contract_version": CONTRACT,
            "subject": "english", "date": date, "timezone": "Asia/Shanghai",
            "selection": "exact_packages" if package_ids else "date_pending" if requested_date else "source_pending" if source_id else "all_pending",
            "study_date_filter": requested_date,
            "snapshot": snapshot, "packages": rows, "source_id_filter": source_id,
            "original_files": inventory(files)}
    bid = "EN-BUNDLE-" + object_sha256(core)[:24].upper()
    template = {"schema_version": "english_web_advice_v1", "bundle_id": bid,
                "source_commit": snapshot["source_commit"], "items": items}
    files["advice.template.json"] = canonical_bytes(template)
    required_return_files = []
    if "contracts/enhancement-deliverables.md" in files:
        for name, schema, field in (
            ("personal-summary", "english_personal_summary_draft_v1", "records"),
            ("vocab-export", "english_vocab_export_draft_v1", "items"),
        ):
            files[name + ".template.json"] = canonical_bytes({
                "schema_version": schema, "bundle_id": bid,
                "source_commit": snapshot["source_commit"],
                "package_ids": [r["package_id"] for r in rows],
                "package_coverage": [{"package_id": r["package_id"], "status": "needs_context", "reason": ""} for r in rows],
                field: [], "uncertainties": [],
            })
            required_return_files.append(name + "-draft.json")
    manifest = {k: v for k, v in core.items() if k != "original_files"}
    manifest.update(bundle_id=bid, files=inventory(files), formal_write_count=0,
                    required_return_files=required_return_files)
    files["manifest.json"] = canonical_bytes(manifest)
    target_root = cold_directory(state / "web-review" / "bundles" / bid, "bundles", bid)
    target = target_root / "study.zip"
    immutable(target, zipped(files))
    checked_zip(target)
    jwrite(target_root / "manifest.json", manifest)
    with exclusive_lock(state / "locks/formal.lock"):
        from .learning_state import formal_versions
        if object_sha256(formal_versions(repo)) != snapshot["formal_version"]:
            raise ValidationError("FORMAL_CHANGED_DURING_BUNDLE: rebuild against the current published snapshot")
        for row in rows:
            if row["formal_status"] == "awaiting_review":
                jwrite(state / "web-review/holds" / (row["package_id"] + ".json"),
                       {"package_id": row["package_id"], "package_sha256": row["package_sha256"]})
    return {"status": "READY", "bundle_id": bid, "date": date, "package_count": len(rows),
            "article_count": len({r["source_id"] for r in rows}), "zip_path": str(target),
            "zip_sha256": file_sha256(target), "snapshot": snapshot,
            "web_project_url": "https://chatgpt.com/g/g-p-6aa5e7f0bd1c81919d6d71ed7a84150f/project",
            "web_start_prompt": "按本包 WEB_INSTRUCTIONS.txt 和固定 source_commit 核对证据，按 manifest 的包范围返回 advice.zip。",
            "legacy_event_count": len(legacy), "formal_write_count": 0}


@cold_artifacts("bundle_parts")
def bundle_parts(repo: Path, state: Path, *, date: str | None = None, source_id: str | None = None,
                 package_ids: list[str] | None = None, snapshot: dict[str, Any] | None = None,
                 max_packages: int = 12, max_text_bytes: int = 65536,
                 max_attachments: int = 8, max_bytes: int = 32 * 1024 * 1024) -> dict[str, Any]:
    """Partition complete captures, never build a giant aggregate ZIP first."""
    if min(max_packages, max_text_bytes, max_attachments, max_bytes) < 1:
        raise ValidationError("partition budgets must be positive")
    packing_date, roots, legacy = select_bundle_roots(repo, state, date=date, source_id=source_id, package_ids=package_ids)
    if not roots:
        return {"status": "NOOP", "parts": [], "package_count": 0, "legacy_event_count": len(legacy), "formal_write_count": 0}
    snapshot = snapshot or local_snapshot()
    rows = []
    for root in roots:
        m = validate_conversation_package(root)
        content = package_files(root)
        rows.append({"package_id": m["package_id"], "package_sha256": m["package_canonical_sha256"],
                     "source_id": m["source_identity"].get("source_id") or m["package_id"],
                     "study_date": m["study_date"], "created_at": m["created_at"],
                     "text_bytes": sum(len(v) for k,v in content.items() if Path(k).suffix.lower() in {".json", ".txt", ".md", ".csv", ".jsonl"}),
                     "bytes": sum(map(len, content.values())), "attachments": len(m.get("attachments", []))})
    rows.sort(key=lambda r: (r["source_id"], r["study_date"], r["created_at"], r["package_id"]))
    groups, current = [], []
    def oversized(group):
        return (len(group) > max_packages or sum(r["text_bytes"] for r in group) > max_text_bytes
                or sum(r["bytes"] for r in group) > max_bytes
                or sum(r["attachments"] for r in group) > max_attachments)
    for row in rows:
        if current and (current[0]["source_id"] != row["source_id"] or oversized(current + [row])):
            groups.append(current)
            current = []
        current.append(row)
    if current:
        groups.append(current)
    budgets = {"max_packages": max_packages, "max_text_bytes": max_text_bytes,
               "max_attachments": max_attachments, "max_bytes": max_bytes}
    references = web_reference_files(repo, state)
    collection_id = "EN-PARTS-" + object_sha256({"rows": rows, "snapshot": snapshot, "budgets": budgets,
        "packing_date": packing_date, "reference_files": inventory(references)})[:24].upper()
    destination = cold_directory(state / "web-review/collections" / collection_id, "collections", collection_id)
    parts = []
    for number, group in enumerate(groups, 1):
        prompt = (f"这是英语审阅集合 {collection_id} 的第 {number}/{len(groups)} 部分。\n"
                  f"只处理本 ZIP manifest 中的 {len(group)} 个 capture；不要获取其他部分或扫描无关全库。\n"
                  f"固定 Study Library 快照 {snapshot['source_commit']}，按本 ZIP 的 WEB_INSTRUCTIONS.txt 审阅。\n"
                  "本 ZIP 的 PART_PROMPT.txt 和 WEB_INSTRUCTIONS.txt 优先于项目来源旧版规则。本批固定版本变为历史版时，允许 allow_historical=true 读取同版，不能切换版本。\n"
                  "其他部分会在独立网页会话并行处理；每个 capture 只由所属部分负责。保留原始学习日期和证据。\n"
                  "按本片词条、句式和文章来源查询必要历史。未命中时标未知，继续处理有原件依据的项。\n"
                  "完整交付三项加强：advice.json 正式入库成稿、personal-summary-draft.json 按点个人摘要增量、vocab-export-draft.json 不背单词 A/B/C 导出成稿建议。\n"
                  "分别填写三个模板并覆盖本片全部 capture；读取 contracts/enhancement-deliverables.md。现有个人摘要在 context/personal-summary/，只读相关点，不重复计作新学习。\n"
                  "新增词条不分配正式 ID；对于已有词条/句式，给出当前部分证据支持的修改，不覆盖未读历史。\n"
                  f"本部分独立返回 advice-part-{number:03d}.zip，保留自己的 bundle_id、input_zip_sha256 与 source_commit。\n"
                  "使用包内 build_return.py 构建返回包。本地将串行核验和入库，网页不能直接写库。\n")
        result = bundle(repo, state, package_ids=[r["package_id"] for r in group], snapshot=snapshot,
                        part_prompt=prompt, reference_files=references)
        prompt_path = destination / f"part-{number:03d}-prompt.txt"
        jwrite_path = destination / f"part-{number:03d}.zip"
        # A copy preserves the exact input ZIP hash used by the native return contract.
        immutable(jwrite_path, Path(result["zip_path"]).read_bytes())
        immutable(prompt_path, prompt.encode("utf-8"))
        parts.append({"part": number, "bundle_id": result["bundle_id"], "zip_path": str(jwrite_path),
                      "zip_sha256": result["zip_sha256"], "prompt_path": str(prompt_path), "prompt": prompt,
                      "source_id": group[0]["source_id"], "package_ids": [r["package_id"] for r in group],
                      "study_dates": sorted({r["study_date"] for r in group}),
                      "raw_text_bytes": sum(r["text_bytes"] for r in group),
                      "oversized_single_capture": oversized(group)})
    actual = [pid for part in parts for pid in part["package_ids"]]
    if len(actual) != len(set(actual)) or set(actual) != {r["package_id"] for r in rows}:
        raise ValidationError("partition coverage is incomplete or duplicated")
    result = {"schema_version": "english_web_parts_v1", "status": "READY_WITH_OVERSIZED_CAPTURE" if any(p["oversized_single_capture"] for p in parts) else "READY",
              "collection_id": collection_id, "snapshot": snapshot, "budgets": budgets,
              "budget_note": "Raw UTF-8 text bytes are a conservative input-size signal, not measured model tokens. Leave room for contracts, history and output; large PDFs/images require additional judgement.",
              "package_count": len(actual), "part_count": len(parts), "parts": parts,
              "legacy_event_count": len(legacy), "formal_write_count": 0}
    jwrite(destination / "manifest.json", result)
    return {**result, "manifest_path": str(destination / "manifest.json")}


@cold_artifacts("prepare_return")
def prepare_return(repo: Path, state: Path, path: Path) -> dict[str, Any]:
    manifest, files = checked_zip(path)
    if (manifest.get("schema_version") != "english_web_return_v1"
            or manifest.get("subject") != "english" or manifest.get("contract_version") != CONTRACT):
        raise ValidationError("not an English web-review return ZIP")
    bid = manifest.get("bundle_id", "")
    if not re.fullmatch(r"EN-BUNDLE-[A-F0-9]{24}", bid):
        raise ValidationError("invalid original bundle ID")
    original_path = state / "web-review/bundles" / bid / "study.zip"
    original, original_files = checked_zip(original_path)
    if manifest.get("input_zip_sha256") != file_sha256(original_path):
        raise ValidationError("return ZIP does not bind the original study ZIP bytes")
    if manifest.get("source_commit") != original["snapshot"]["source_commit"]:
        raise ValidationError("return uses a different MCP snapshot")
    advice = json.loads(files.get("advice.json", b"null"))
    if (not isinstance(advice, dict) or advice.get("schema_version") != "english_web_advice_v1"
            or advice.get("bundle_id") != bid or advice.get("source_commit") != manifest["source_commit"]):
        raise ValidationError("advice identity or version differs")
    bindings = {r["package_id"]: r for r in original["packages"]}
    items = advice.get("items")
    if not isinstance(items, list) or len(items) != len(bindings) or {i.get("package_id") for i in items} != set(bindings):
        raise ValidationError("advice must cover the exact input package set once")
    for item in items:
        if item.get("package_sha256") != bindings[item["package_id"]]["package_sha256"]:
            raise ValidationError("advice package hash differs")
        for key in ("study_date", "source_id", "formal_status"):
            if item.get(key) != bindings[item["package_id"]].get(key):
                raise ValidationError("advice changed an original package identity or formal status")
        if item.get("decision") not in {"propose", "skip", "needs_context", "already_formal"}:
            raise ValidationError("invalid web item decision")
        if not isinstance(item.get("actions"), list) or not isinstance(item.get("reason"), str):
            raise ValidationError("advice actions/reason are missing")
    rid = "EN-RETURN-" + file_sha256(path)[:24].upper()
    target = cold_directory(state / "web-review/returns" / rid, "returns", rid)
    immutable(target / "return.zip", path.read_bytes())
    for name, data in files.items():
        immutable(target / "original" / name, data)
    current_version = object_sha256(__import__("english_pipeline.learning_state", fromlist=["formal_versions"]).formal_versions(repo))
    template = {"schema_version": "english_local_web_review_v1", "return_id": rid,
                "reviewed_formal_version": current_version,
                "reviewer_model": "unknown", "items": [
                    {**i, "local_decision": "needs_user"} for i in items]}
    template_path = target / "review.template.json"
    if not template_path.exists():
        jwrite(template_path, template)
    result = {"status": "PREPARED", "return_id": rid, "bundle_id": bid,
              "return_zip_sha256": file_sha256(path), "package_count": len(items),
              "review_template": str(template_path), "original_directory": str(target / "original"),
              "base_formal_version": original["snapshot"]["formal_version"],
              "current_formal_version": current_version,
              "formal_changed_since_web": current_version != original["snapshot"]["formal_version"],
              "formal_write_count": 0}
    jwrite(target / "prepared.json", {k: v for k, v in result.items() if k not in {"current_formal_version", "formal_changed_since_web"}})
    # Only the returned envelope gets this additive advisory. The immutable
    # prepare receipt and review template remain byte-for-byte native inputs.
    try:
        from .web_prescreen import prescreen_return
        result['evidence_prescreen'] = prescreen_return(repo=repo, state=state,
            original=original, manifest=manifest, items=items, return_id=rid,
            return_sha256=result['return_zip_sha256'], output_dir=target / 'evidence-prescreen')
    except Exception as exc:
        result['evidence_prescreen'] = {'status': 'not_screened', 'advisory_only': True,
            'not_flagged_is_not_approved': True, 'reason': 'prescreen_unavailable',
            'error_type': type(exc).__name__, 'report_path': None,
            'formal_write_count': 0, 'learner_evidence_write_count': 0}
    return result


def resolved_review_source(repo: Path, root: Path, resolution: dict[str, Any]) -> dict[str, Any]:
    """Validate a local correction against the native source and a real user quote."""
    from .reading_preparation import DEFAULT_OUTPUT, query
    from .util import sentence_sha256
    original = validate_conversation_package(root)
    identity = copy.deepcopy(original["source_identity"])
    if (isinstance(resolution, dict)
            and set(resolution) == {"source_id", "attachment_pointer", "reason"}):
        if (identity.get("source_kind") != "user_provided"
                or identity.get("source_id") or identity.get("source_hash")
                or not str(resolution["reason"]).strip()):
            raise ValidationError("attachment source binding requires unbound user text")
        match = re.fullmatch(r"manifest.json#/attachments/(\d+)", str(resolution["attachment_pointer"]))
        attachments = original.get("attachments", [])
        if match is None or int(match[1]) >= len(attachments):
            raise ValidationError("source binding requires an exact article attachment")
        record = query(repo / DEFAULT_OUTPUT, source_id=resolution["source_id"], repo_root=repo)
        attached = (root / attachments[int(match[1])]["path"]).read_text(encoding="utf-8")
        if (record["source_id"] not in attached
                or not record.get("paragraphs")
                or any(p["text"] not in attached for p in record["paragraphs"])
                or any(u["text"] not in attached for q in record.get("questions", []) for u in q["units"])):
            raise ValidationError("article attachment does not prove the complete canonical source")
        identity.update(source_id=record["source_id"], source_hash=record["source_hash"],
                        source_article=record["source_article"], answer_exposure="protected")
        identity["source_unit_correction"] = {
            "original_package_id": original["package_id"],
            "original_package_sha256": original["package_canonical_sha256"],
            "original_unit_id": identity.get("sentence_id"),
            "canonical_record_sha256": record["record_sha256"], **resolution,
            "new_learner_observation": False,
        }
        return identity
    additional = isinstance(resolution, dict) and resolution.get("additional_unit") is True
    fields = {"unit_id", "evidence_pointer", "reason"} | ({"additional_unit"} if additional else set())
    if (identity.get("source_kind") != "article" or identity.get("answer_exposure") != "answer_free"
            or not isinstance(resolution, dict) or set(resolution) != fields
            or not str(resolution.get("reason", "")).strip()):
        raise ValidationError("source resolution requires a bounded local article correction")
    match = re.fullmatch(r"conversation.json#/messages/(\d+)", str(resolution["evidence_pointer"]))
    messages = load_json(root / "conversation.json")["messages"]
    if match is None or int(match[1]) >= len(messages):
        raise ValidationError("source resolution requires an exact user message pointer")
    message = messages[int(match[1])]
    record = query(repo / DEFAULT_OUTPUT, source_id=identity["source_id"], repo_root=repo)
    units = [u for u in record["units"] if u["unit_id"] == resolution["unit_id"]]
    if (record["source_hash"] != identity["source_hash"] or record["source_article"] != identity["source_article"]
            or len(units) != 1 or units[0]["kind"] != "article" or message["role"] != "user"
            or units[0]["text"] not in message["content"]):
        raise ValidationError("source resolution is not proved by the same source and verbatim user quote")
    unit = units[0]
    if unit["unit_id"] == identity.get("sentence_id"):
        raise ValidationError("source resolution does not change the mistaken unit")
    if additional and not any(m["role"] == "user" and identity["source_sentence"] in m["content"] for m in messages):
        raise ValidationError("additional unit requires the original sentence to remain proved by a user quote")
    identity.update(unit_id=unit["unit_id"], sentence_id=unit["sentence_id"],
                    source_sentence=unit["text"], sentence_sha256=sentence_sha256(unit["text"]),
                    paragraph_id=unit["paragraph_id"],
                    paragraph_context=next(p["text"] for p in record["paragraphs"] if p["paragraph_id"] == unit["paragraph_id"]))
    identity["source_unit_correction"] = {
        "original_package_id": original["package_id"], "original_package_sha256": original["package_canonical_sha256"],
        "original_unit_id": original["source_identity"].get("sentence_id"),
        "canonical_record_sha256": record["record_sha256"], **resolution,
        "new_learner_observation": False,
    }
    return identity


def _preserved_grouped_identity(identity: dict[str, Any], canonical: dict[str, Any]) -> dict[str, Any]:
    """Fill a missing article locator only from a verified, same-source record."""
    if (not identity.get("source_id")
            or identity["source_id"] not in {canonical.get("source_id"), canonical.get("reference_id")}
            or not identity.get("source_hash")
            or canonical.get("source_hash") != identity["source_hash"]
            or (identity.get("source_article") is not None
                and canonical.get("source_article") != identity["source_article"])):
        raise ValidationError("grouped source preservation differs from the original source")
    preserved = copy.deepcopy(identity)
    if preserved.get("source_article") is None and canonical.get("source_article") is not None:
        preserved["source_article"] = canonical["source_article"]
    return preserved


@cold_artifacts("approve_return")
def approve_return(repo: Path, state: Path, review_path: Path) -> dict[str, Any]:
    """Seal local model decisions and original returned bytes, never apply them."""
    from .learning_state import formal_versions
    review = load_json(review_path)
    if review.get("schema_version") != "english_local_web_review_v1":
        raise ValidationError("expected a local English web review decision document")
    preserve_grouped = review.get("preserve_grouped_source_evidence", False)
    if type(preserve_grouped) is not bool:
        raise ValidationError("grouped source evidence preservation must be an explicit boolean")
    rid = str(review.get("return_id", ""))
    if not re.fullmatch(r"EN-RETURN-[A-F0-9]{24}", rid):
        raise ValidationError("invalid return ID")
    imported = state / "web-review/returns" / rid
    prepared = load_json(imported / "prepared.json")
    manifest, files = checked_zip(imported / "return.zip")
    original = load_json(state / "web-review/bundles" / manifest["bundle_id"] / "manifest.json")
    wanted = {p["package_id"]: p for p in original["packages"]}
    items = review.get("items", [])
    if (len(items) != len(wanted) or {i.get("package_id") for i in items} != set(wanted)
            or not str(review.get("reviewer_model", "")).strip()):
        raise ValidationError("local review must cover the input set exactly and name its actual reviewer")
    review_bytes = canonical_bytes(review)
    review_id = "EN-REVIEW-" + bytes_sha256(review_bytes)[:24].upper()
    root = cold_directory(state / "web-review/reviews" / review_id, "reviews", review_id)
    if (root / "approval.json").exists():
        from .web_review_gate import approval
        return approval(state, review_id)
    if review.get("reviewed_formal_version") != object_sha256(formal_versions(repo)):
        raise ValidationError("LOCAL_REVIEW_STALE: reread changed formal values and update the local decision")
    processed = processed_package_sha256s(state)
    superseded = None
    previous_id = review.get("supersedes_review_id") or review.get("continues_review_id")
    if previous_id:
        previous_id = str(previous_id)
        if not re.fullmatch(r"EN-REVIEW-[A-F0-9]{24}", previous_id):
            raise ValidationError("invalid superseded review ID")
        superseded = load_json(state / "web-review/reviews" / previous_id / "approval.json")
        if (superseded["return_id"] != rid or superseded["bundle_id"] != manifest["bundle_id"]
                or (review.get("supersedes_review_id") and any(p["package_sha256"] in processed for p in superseded["packages"]))):
            raise ValidationError("only an uncommitted review of this same return can be superseded")
    available, selected, unresolved = {}, [], []
    for item in items:
        pid = item["package_id"]
        if item.get("package_sha256") != wanted[pid]["package_sha256"]:
            raise ValidationError("local review package differs from original bundle")
        if item.get("local_decision") not in {"accepted", "modified", "rejected", "needs_user", "already_formal"}:
            raise ValidationError("local decision must be accepted/modified/rejected/needs_user/already_formal")
        path = package_root(state, repo, pid)
        m = validate_conversation_package(path)
        if m["package_canonical_sha256"] != item["package_sha256"]:
            raise ValidationError("local original package changed after export")
        available[pid] = {"path": str(path), "package_id": pid, "package_sha256": item["package_sha256"]}
        if item["package_sha256"] in processed:
            continue  # A previously committed original never becomes a second learning episode.
        if item["local_decision"] == "already_formal":
            raise ValidationError("already_formal was claimed without a native formal receipt")
        if item["local_decision"] == "needs_user":
            unresolved.append(pid)
            continue
        if not item.get("reason") or not isinstance(item.get("support_record"), dict):
            raise ValidationError("completed local decisions need a reason and a completed native support record")
        if not isinstance(item.get("actions"), list):
            raise ValidationError("local actions must be an array")
        if item.get("source_resolution"):
            resolved_review_source(repo, path, item["source_resolution"])
        if item["local_decision"] == "rejected" and item["actions"]:
            raise ValidationError("rejected web advice must not retain executable formal actions")
        selected.append(item)
    for item in selected:
        for action in item["actions"]:
            resolved = validate_evidence_refs(action.get("evidence_refs", []), list(available.values()))
            if any(wanted[r["binding"]["package_id"]]["study_date"] != wanted[item["package_id"]]["study_date"] for r in resolved):
                raise ValidationError("formal actions cannot mix original study dates")
            if action.get("action_type") == "master_bank_insert" and action.get("row", {}).get("date") != wanted[item["package_id"]]["study_date"]:
                raise ValidationError("new formal vocabulary must preserve its original study date")
    jwrite(root / "review.json", review)
    started_path = root / "started.json"
    if not started_path.exists():
        jwrite(started_path, {"captured_at": utc_now()})
    captured_at = load_json(started_path)["captured_at"]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in selected:
        row = wanted[item["package_id"]]
        groups.setdefault((row["study_date"], row["source_id"]), []).append(item)
    supplements, bound, corrections = [], [wanted[i["package_id"]] for i in selected], []
    for item in selected:
        if not item.get("source_resolution"):
            continue
        pid = item["package_id"]
        original_root = Path(available[pid]["path"])
        identity = resolved_review_source(repo, original_root, item["source_resolution"])
        original_manifest = validate_conversation_package(original_root)
        key = review_id + ":source-resolution:" + pid
        corrected_path, _ = create_conversation_package(state, {
            "idempotency_key": key, "segment_key": key, "thread_ref": "english-source-resolution:" + key,
            "capture_mode": "independent_unit", "occurred_at": captured_at,
            "study_date": wanted[pid]["study_date"], "source": identity,
            "conversation": load_json(original_root / "conversation.json")["messages"],
            "attachments": [{"path": str(original_root / a["path"]), "role": a["role"]}
                            for a in original_manifest.get("attachments", [])],
            "missing_fields": original_manifest.get("missing_fields", []),
        })
        corrected = validate_conversation_package(corrected_path)
        row = {"package_id": corrected["package_id"], "package_sha256": corrected["package_canonical_sha256"],
               "study_date": wanted[pid]["study_date"], "source_id": identity["source_id"],
               "original_package_id": pid, "unit_id": identity["sentence_id"]}
        if item["source_resolution"].get("additional_unit") is True:
            row["additional_unit"] = True
        corrections.append(row)
        bound.append(row)
    supplement_group_size = review.get("supplement_group_size", 2)
    if type(supplement_group_size) is not int or supplement_group_size not in {1, 2}:
        raise ValidationError("supplement_group_size must be 1 or 2")
    for (study_date, source_id), group in sorted(groups.items()):
        # Each supplemental closure lists its bound formal actions. Bound that
        # list so the native 12-KiB protected support record stays readable.
        for start in range(0, len(group), supplement_group_size):
            chunk = group[start:start + supplement_group_size]
            identity = copy.deepcopy(validate_conversation_package(Path(available[chunk[0]["package_id"]]["path"]))["source_identity"])
            if chunk[0].get("source_resolution"):
                identity = resolved_review_source(repo, Path(available[chunk[0]["package_id"]]["path"]),
                                                 chunk[0]["source_resolution"])
            supplement_bindings = []
            for item in chunk:
                binding = item
                if identity.get("source_id") != wanted[item["package_id"]].get("source_id"):
                    binding = next(c for c in corrections if c["original_package_id"] == item["package_id"])
                supplement_bindings.append({"package_id": binding["package_id"],
                                            "package_sha256": binding["package_sha256"]})
            identity.update(source_kind="explanation", answer_exposure="protected", review_route="web",
                            web_result={"origin": "project-E", "task_id": rid, "source_commit": manifest["source_commit"]},
                            supplement_of=supplement_bindings,
                            artifact_capture=True)
            key = review_id + ":" + object_sha256([study_date, source_id, start])[:16]
            messages = [
                {"role": "assistant", "content": files["advice.json"].decode("utf-8"),
                 "metadata": {"origin": "project-E", "evidence_type": "exact_downloaded_advice_artifact", "not_learner_attempt": True}},
                {"role": "assistant", "content": review_bytes.decode("utf-8"),
                 "metadata": {"origin": "local-GPT6", "evidence_type": "exact_local_review_artifact",
                              "not_learner_attempt": True, "review_document": review}},
            ]
            attachments = [{"path": str(imported / "return.zip"), "role": "other_attachment"}]
            if preserve_grouped:
                # The original Capture stays immutable. Preserve exact user nodes
                # and a verified canonical source in this labelled supplement so
                # grouped units can use the existing evidence validator unchanged.
                from .reading_preparation import DEFAULT_OUTPUT, query
                canonical = query(repo / DEFAULT_OUTPUT, source_id=source_id, repo_root=repo)
                identity = _preserved_grouped_identity(identity, canonical)
                canonical_path = root / "source-evidence" / (object_sha256(source_id) + ".json")
                immutable(canonical_path, canonical_bytes(canonical))
                attachments.append({"path": str(canonical_path), "role": "other_attachment"})
                for item in chunk:
                    original_root = Path(available[item["package_id"]]["path"])
                    for index, message in enumerate(load_json(original_root / "conversation.json")["messages"]):
                        if message["role"] != "user":
                            continue
                        independent_actions = [
                            action["action_id"] for action in item["actions"]
                            if action.get("action_type") == "learning_event_append"
                            and action["event"]["kind"] in {"answer", "review"}
                            and action["event"]["origin"] == "daily"
                            and action["event"]["outcome"] == "correct"
                            and action["event"]["hint_dependence"] == "none"
                            and action["event"]["reasoning_status"] == "valid"
                            and action["event"]["user_response"].strip()
                            and action["event"]["reasoning"].strip()
                            and action["event"]["user_response"] in message["content"]
                            and action["event"]["reasoning"] in message["content"]
                            and any(ref["package_id"] == item["package_id"]
                                    and ref["pointer"] == f"conversation.json#/messages/{index}"
                                    and ref["kind"] == "user_message"
                                    for ref in action["evidence_refs"])
                        ]
                        # This is a formal-review annotation of an exact copy,
                        # never a new tag written into the immutable quick Capture.
                        # Scope it to the accepted action so another answer in
                        # the same user message cannot inherit correct-use proof.
                        reviewed_tags = ({
                            "evidence_tags": ["independent_correct_use"],
                            "independent_correct_use_action_ids": independent_actions,
                            "independent_correct_use_review_id": review_id,
                        } if independent_actions else {})
                        messages.append({
                            "role": "user", "content": message["content"],
                            "created_at": message.get("created_at", captured_at),
                            "metadata": {"origin": "original_package_preservation",
                                         "original_package_id": item["package_id"],
                                         "original_package_sha256": item["package_sha256"],
                                         "original_message_pointer": f"conversation.json#/messages/{index}",
                                         "original_message_sha256": object_sha256(message),
                                         "not_new_learner_observation": True, **reviewed_tags},
                        })
            request = {"idempotency_key": key, "segment_key": key, "thread_ref": "english-web-review:" + key,
                       "occurred_at": captured_at, "study_date": study_date, "source": identity,
                       "conversation": messages,
                       "attachments": attachments,
                       "missing_fields": [] if "web_conversation.json" in files else ["web_full_conversation_not_supplied"]}
            path, receipt = create_conversation_package(state, request)
            m = validate_conversation_package(path)
            row = {"package_id": m["package_id"], "package_sha256": m["package_canonical_sha256"],
                   "study_date": study_date, "source_id": source_id,
                   "original_package_ids": [i["package_id"] for i in chunk]}
            supplements.append(row)
            bound.append(row)
    if superseded:
        # Keep failed pre-commit evidence in the successful native archive set;
        # it is an administrative artifact, never another learner observation.
        for previous in [*superseded["supplements"], *superseded.get("corrections", [])]:
            if previous["package_sha256"] in processed:
                continue
            row = {**previous, "original_package_ids": [], "superseded_review_id": superseded["review_id"]}
            supplements.append(row)
            bound.append(row)
    result = {"status": "APPROVED_FOR_PREPARATION", "schema_version": "english_web_local_approval_v1", "review_id": review_id,
              "return_id": rid, "bundle_id": manifest["bundle_id"], "review_sha256": bytes_sha256(review_bytes),
              "return_zip_sha256": prepared["return_zip_sha256"], "captured_at": captured_at,
              "reviewed_formal_version": review["reviewed_formal_version"],
              "packages": bound, "supplements": supplements, "corrections": corrections, "needs_user": unresolved,
              "daily_dates": sorted({r["study_date"] for r in bound}), "formal_write_count": 0}
    jwrite(root / "approval.json", result)
    if superseded and review.get("supersedes_review_id"):
        previous_root = cold_directory(state / "web-review/reviews" / superseded["review_id"], "reviews", superseded["review_id"])
        jwrite(previous_root / "superseded.json",
               {"review_id": superseded["review_id"], "replacement_review_id": review_id,
                "reason": "Uncommitted review corrected after native validation; evidence retained in replacement archive set."})
    return result


@cold_artifacts("prepare_day")
def prepare_day(repo: Path, state: Path, review_id: str, date: str) -> dict[str, Any]:
    from .web_review_gate import approval
    from .nightly import freeze_nightly
    from .learning_state import formal_versions
    from .sentence_support import current_index_sha256
    date = day(date)
    superseded_path = state / "web-review/reviews" / review_id / "superseded.json"
    if superseded_path.exists():
        raise ValidationError("review superseded; resume " + load_json(superseded_path)["replacement_review_id"])
    approved = approval(state, review_id)
    root = state / "web-review/reviews" / review_id
    dest = cold_directory(root / date, "review-days", review_id + "-" + date)
    if (dest / "prepared.json").exists():
        prepared = load_json(dest / "prepared.json")
        for key in ("manifest", "actions", "support"):
            if file_sha256(Path(prepared[key + "_path"])) != prepared[key + "_sha256"]:
                raise ValidationError("prepared web formal files changed; preserve them and create a new local review")
        return prepared
    review = load_json(root / "review.json")
    wanted = [r for r in approved["packages"] if r["study_date"] == date]
    if not wanted:
        raise ValidationError("review has no approved packages for this date")
    earlier = [d for d in approved["daily_dates"] if d < date]
    expected_version = approved["reviewed_formal_version"]
    if earlier:
        previous = root / earlier[-1] / "complete.json"
        if not previous.exists():
            raise ValidationError("process original learning dates in order")
        expected_version = load_json(previous)["formal_version"]
    if object_sha256(formal_versions(repo)) != expected_version:
        raise ValidationError("LOCAL_REVIEW_STALE: formal values changed outside this reviewed sequence")
    manifest_path, m = freeze_nightly(state, repo, study_date=date,
                                    package_ids={r["package_id"] for r in wanted}, web_review_id=review_id)
    if m["status"] == "NOOP":
        raise ValidationError("already committed packages require existing native closeout recovery, not another prepare")
    selected = {r["package_id"] for r in wanted}
    native = {r["package_id"]: r for r in m["package_documents"]}
    actions: dict[str, Any] = {}
    supports = []
    for item in review["items"]:
        pid = item["package_id"]
        if pid not in selected:
            continue
        supplement = next(r for r in approved["supplements"] if pid in r["original_package_ids"])
        sref = {"package_id": supplement["package_id"], "package_sha256": supplement["package_sha256"],
                "pointer": "conversation.json#/messages/1", "kind": "assistant_message"}
        corrected = next((r for r in approved.get("corrections", []) if r["original_package_id"] == pid), None)
        correction_ref = ({"package_id": corrected["package_id"], "package_sha256": corrected["package_sha256"],
                           "pointer": "source.json#/identity/source_sentence", "kind": "source_text"} if corrected else None)
        for supplied in item["actions"]:
            action = copy.deepcopy(supplied)
            if sref not in action["evidence_refs"]:
                action["evidence_refs"].append(sref)
            if review.get("preserve_grouped_source_evidence") and action.get("action_type") == "learning_event_append":
                supplemental_root = Path(native[supplement["package_id"]]["path"])
                supplemental_messages = load_json(supplemental_root / "conversation.json")["messages"]
                original_user_pointers = {
                    ref["pointer"] for ref in action["evidence_refs"]
                    if ref["package_id"] == pid and ref["kind"] in {"user_message", "independent_correct_use"}
                }
                for index, message in enumerate(supplemental_messages):
                    metadata = message.get("metadata", {})
                    if (message["role"] == "user" and metadata.get("original_package_id") == pid
                            and action["event"]["user_response"]
                            and (action["event"]["user_response"] in message["content"]
                                 or metadata.get("original_message_pointer") in original_user_pointers)):
                        action["evidence_refs"].append({**sref, "pointer": f"conversation.json#/messages/{index}",
                                                        "kind": ("independent_correct_use"
                                                                 if action["action_id"] in metadata.get("independent_correct_use_action_ids", [])
                                                                 else "user_message")})
                for index, attachment in enumerate(validate_conversation_package(supplemental_root)["attachments"]):
                    if attachment["mime_type"] == "application/json":
                        action["evidence_refs"].append({**sref, "pointer": f"manifest.json#/attachments/{index}",
                                                        "kind": "other_attachment"})
            if correction_ref:
                action["evidence_refs"].append(correction_ref)
            aid = action["action_id"]
            if aid in actions and actions[aid] != action:
                raise ValidationError("two items gave different actions with the same action_id")
            actions[aid] = action
        source = validate_conversation_package(Path(native[corrected["package_id"] if corrected else pid]["path"]))["source_identity"]
        unit = next((source.get(k) for k in ("sentence_id", "question_id", "paragraph_id", "knowledge_point_id") if source.get(k)), None)
        if unit is None:
            unit = whole_article_unit_id(source)
        if not unit:
            raise ValidationError("web adjudication requires the original exact learning unit")
        original_first = load_json(Path(native[pid]["path"]) / "conversation.json")["messages"][0]
        refs = [{"package_id": pid, "package_sha256": item["package_sha256"],
                 "pointer": "conversation.json#/messages/0", "kind": original_first["role"] + "_message"}, sref]
        if correction_ref:
            refs.append(correction_ref)
        key = approved["return_id"] + ":" + pid
        actions["WEB-" + pid] = {
            "action_id": "WEB-" + pid, "action_type": "learning_event_append",
            "reason": item["reason"], "evidence_refs": refs,
            "event": {"event_key": key, "kind": "adjudication", "origin": "project-E",
                      "task_id": approved["return_id"], "source_commit": load_json(state / "web-review/returns" / approved["return_id"] / "original/manifest.json")["source_commit"],
                      "source_id": source["source_id"], "unit_id": unit,
                      "bank_ids": [], "sentence_pattern_ids": [], "concept_ids": [],
                      "observed_at": approved["captured_at"], "outcome": item["local_decision"],
                      "hint_dependence": "not_observed", "reasoning_status": "not_observed",
                      "user_response": "", "reasoning": "", "correction": ""}}
        support = copy.deepcopy(item["support_record"])
        if support.get("package_id") != pid or support.get("package_sha256") != item["package_sha256"]:
            raise ValidationError("local support proposal must retain original package identity")
        if corrected and not corrected.get("additional_unit"):
            if (support["history_episodes"] or support["vocabulary_candidates"]
                    or any(support["teaching_cues"].values()) or support["paragraph_context"]["text"] is not None):
                raise ValidationError("superseded source identity must retain only a protected locator")
            support["record_kind"] = "protected_sidecar"
        supports.append(support)
        if corrected:
            corrected_support = empty_support(Path(native[corrected["package_id"]]["path"]))
            supplied_support = item.get("resolved_support_record")
            if supplied_support:
                for field in ("paragraph_context", "history_episodes", "teaching_cues", "vocabulary_candidates"):
                    corrected_support[field] = copy.deepcopy(supplied_support[field])
                corrected_support["evidence_refs"] = []
                for ref in supplied_support["evidence_refs"]:
                    if ref["package_id"] != pid or ref["package_sha256"] != item["package_sha256"]:
                        raise ValidationError("resolved support must cite only its exact original capture")
                    corrected_support["evidence_refs"].append({**ref, "package_id": corrected["package_id"],
                                                               "package_sha256": corrected["package_sha256"]})
            supports.append(corrected_support)
    for row in approved["supplements"]:
        if row["study_date"] == date:
            supports.append(empty_support(Path(native[row["package_id"]]["path"])))
            if row.get("superseded_review_id"):
                pid = row["package_id"]
                actions["RETAIN-" + pid] = {
                    "action_id": "RETAIN-" + pid, "action_type": "skip_duplicate",
                    "reason": "Retain superseded local review evidence without repeating a learner observation or formal update.",
                    "target": "superseded_web_review_artifact", "item": pid,
                    "evidence_refs": [{"package_id": pid, "package_sha256": row["package_sha256"],
                                       "pointer": "conversation.json#/messages/0", "kind": "assistant_message"}],
                }
    producer = {"role": "sol_nightly_reviewer", "model": review["reviewer_model"], "prompt_version": CONTRACT}
    action_id = "EN-ACTIONS-" + review_id + "-" + date
    action_doc = {"schema_version": "english_sol_actions_v2", "action_set_id": action_id,
                  "batch_id": m["batch_id"], "batch_manifest_sha256": file_sha256(manifest_path),
                  "created_at": approved["captured_at"], "producer": producer,
                  "formal_write_count": 0, "formal_writeback": "none", "actions": list(actions.values()), "unresolved": []}
    support_doc = {"schema_version": "english_sentence_support_proposal_v1", "proposal_id": "SUPPORT-" + action_id,
                   "batch_id": m["batch_id"], "batch_manifest_sha256": file_sha256(manifest_path),
                   "action_set_id": action_id, "expected_index_sha256": current_index_sha256(state),
                   "created_at": approved["captured_at"], "producer": producer, "records": supports,
                   "formal_write_count": 0, "background_processing": "none"}
    jwrite(dest / "actions.json", action_doc)
    jwrite(dest / "support.json", support_doc)
    result = {"status": "PREPARED", "review_id": review_id, "study_date": date, "batch_id": m["batch_id"],
              "package_ids": m["package_ids"], "formal_write_count": 0}
    for key, path in (("manifest", manifest_path), ("actions", dest / "actions.json"), ("support", dest / "support.json")):
        result[key + "_path"] = str(path)
        result[key + "_sha256"] = file_sha256(path)
    jwrite(dest / "prepared.json", result)
    return result


@cold_artifacts("run_day")
def run_day(repo: Path, state: Path, review_id: str, date: str, *, apply: bool = False,
            authorization: str | None = None, archive_contract: Any = None) -> dict[str, Any]:
    from .writer import apply_nightly, recover_nightly, OPEN_TRANSACTION_STATUSES
    from .backlog import (_existing_closeout_for_batch, _trusted_consumed_pointer,
                          _trusted_closeouts_for_package, _ensure_support_refresh)
    from .sentence_support import preflight_sentence_support
    from .archive import archive_committed_batch, VolumeContract
    from .learning_state import formal_versions
    if apply and authorization != review_id:
        raise ValidationError("apply requires --authorization equal to the reviewed review_id")
    date = day(date)
    with exclusive_lock(state / "locks/english-web-review.lock"):
        prepared = prepare_day(repo, state, review_id, date)
        root = cold_directory(state / "web-review/reviews" / review_id / date, "review-days", review_id + "-" + date)
        mp, ap, sp = (Path(prepared[k + "_path"]) for k in ("manifest", "actions", "support"))
        m = load_json(mp)
        result = {"review_id": review_id, "study_date": date, "batch_id": m["batch_id"],
                  "package_ids": m["package_ids"], "formal_write_count": 0}
        if (root / "complete.json").exists():
            for p in m["package_documents"]:
                if not _trusted_consumed_pointer(state, date, p["package_id"], p["package_sha256"]):
                    raise ValidationError("completed web day lacks current native archive closure proof")
            return {**load_json(root / "complete.json"), "status": "ALREADY_COMPLETE", "formal_write_count": 0}
        transactions = sorted((state / "nightly" / date / m["batch_id"] / "transactions").glob("*/transaction.json"))
        all_closed = all(_trusted_consumed_pointer(state, date, p["package_id"], p["package_sha256"])
                         for p in m["package_documents"])
        if all_closed:
            if not apply:
                return {**result, "status": "FORMAL_COMMITTED_LOCAL_VERIFICATION_PENDING"}
            quality_receipts = []
            for transaction in transactions:
                recovered = recover_nightly(state, repo, transaction_path=transaction,
                                            archive_contract=archive_contract)
                if recovered["status"] != "PASS":
                    return {**result, "status": "FORMAL_COMMITTED_LOCAL_VERIFICATION_PENDING", "recovery": recovered}
                quality_receipts.extend(recovered.get("local_quality", []))
            result.update(status="COMPLETE", formal_version=object_sha256(formal_versions(repo)),
                          support_status="PASS", archive_status="COMPLETE", local_quality=quality_receipts)
            jwrite(root / "complete.json", result)
            return result
        for transaction in transactions:
            if load_json(transaction).get("status") in OPEN_TRANSACTION_STATUSES:
                if not apply:
                    return {**result, "status": "RECOVERY_REQUIRED", "transaction_path": str(transaction)}
                recovered = recover_nightly(state, repo, transaction_path=transaction,
                                            archive_contract=archive_contract)
                if recovered["status"] != "PASS":
                    return {**result, "status": "RECOVERY_PENDING", "recovery": recovered}
        existing = _existing_closeout_for_batch(state, m, load_json(ap)["action_set_id"])
        if existing is None:
            # After partial cleanup the ordinary native root is gone. Every
            # package must independently bind the same canonical closeout.
            candidates = None
            for p in m["package_documents"]:
                paths = {r["receipt_path"] for r in _trusted_closeouts_for_package(state, p["package_id"], p["package_sha256"])
                         if r["batch_id"] == m["batch_id"] and r["disposition"] == "completed"}
                candidates = paths if candidates is None else candidates & paths
            if candidates:
                path = Path(sorted(candidates)[0])
                receipt = load_json(path)
                if receipt.get("action_set_id") == load_json(ap)["action_set_id"]:
                    existing = (path, receipt)
        if not existing:
            dry_binding_path = root / "dry-binding.json"
            if dry_binding_path.exists():
                binding = load_json(dry_binding_path)
                dry_path, pf_path = Path(binding["dry_run_path"]), Path(binding["preflight_path"])
                if file_sha256(dry_path) != binding["dry_run_sha256"] or file_sha256(pf_path) != binding["preflight_sha256"]:
                    raise ValidationError("reviewed dry-run/preflight evidence changed")
                from .sentence_support import validate_sentence_support_preflight
                validate_sentence_support_preflight(state, manifest_path=mp, actions_path=ap, receipt_path=pf_path)
            else:
                dry_path, dry = apply_nightly(state, repo, manifest_path=mp, actions_path=ap, apply=False)
                if dry["status"] not in {"DRY_RUN_VALID", "DRY_RUN_NO_ACTION"}:
                    return {**result, "status": dry["status"], "dry_run_receipt": str(dry_path)}
                pf_path, pf = preflight_sentence_support(state, manifest_path=mp, proposal_path=sp,
                                                        dry_run_receipt_path=dry_path, expected_completed_ids=set(m["package_ids"]))
                jwrite(dry_binding_path, {"dry_run_path": str(dry_path), "dry_run_sha256": file_sha256(dry_path),
                                         "preflight_path": str(pf_path), "preflight_sha256": file_sha256(pf_path)})
            if not apply:
                return {**result, "status": "DRY_RUN_VALID", "dry_run_receipt": str(dry_path),
                        "support_preflight": str(pf_path)}
            writer_path, writer = apply_nightly(state, repo, manifest_path=mp, actions_path=ap, apply=True,
                                               authorization=m["batch_id"], support_preflight_path=pf_path)
            if writer["status"] not in {"APPLIED", "PARTIAL", "NO_ACTION"}:
                return {**result, "status": writer["status"], "writer_receipt": str(writer_path)}
            result["formal_write_count"] = writer.get("formal_write_count", 0)
        else:
            writer_path, writer = existing
            if not apply:
                return {**result, "status": "FORMAL_COMMITTED_CLOSEOUT_PENDING", "writer_receipt": str(writer_path)}
        result.update(writer_receipt=str(writer_path), writer_status=writer["status"])
        try:
            support_path, support = _ensure_support_refresh(
                state, repo, study_date=date, manifest_path=mp, writer_receipt_path=writer_path,
                support_provider=None, support_dir=None, actions_path=ap, proposal_path_override=sp)
            result["support_status"] = support["status"]
        except (OSError, ValueError, PipelineError) as exc:
            return {**result, "status": "FORMAL_COMMITTED_SUPPORT_PENDING", "error": str(exc)}
        try:
            archived = archive_committed_batch(state, repo, manifest_path=mp, writer_receipt_path=writer_path,
                                              contract=archive_contract or VolumeContract())
            result["archive_status"] = archived["status"]
            if archived["status"] != "COMPLETE":
                return {**result, "status": "FORMAL_COMMITTED_ARCHIVE_PENDING", "archive": archived}
            result["local_quality"] = archived["local_quality"]
            if result["local_quality"]["status"] != "VERIFIED_CURRENT":
                return {**result, "status": result["local_quality"]["status"]}
            for p in m["package_documents"]:
                if not _trusted_consumed_pointer(state, date, p["package_id"], p["package_sha256"]):
                    raise ValidationError("native package archive/locator/cleanup proof is incomplete")
        except (OSError, ValueError, PipelineError) as exc:
            return {**result, "status": "FORMAL_COMMITTED_ARCHIVE_PENDING", "error": str(exc)}
        result.update(status="COMPLETE", formal_version=object_sha256(formal_versions(repo)))
        jwrite(root / "complete.json", result)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--state-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("bundle")
    b.add_argument("--date")
    b.add_argument("--source-id")
    b.add_argument("--package-id", action="append", default=[])
    b.add_argument("--max-packages", type=int, default=12)
    b.add_argument("--max-text-bytes", type=int, default=65536)
    b.add_argument("--max-attachments", type=int, default=8)
    b.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    s = sub.add_parser("segment-zip")
    s.add_argument("--package-id", required=True)
    r = sub.add_parser("prepare-return")
    r.add_argument("--zip", type=Path, required=True)
    a = sub.add_parser("approve-return")
    a.add_argument("--review-json", type=Path, required=True)
    for command in ("prepare-day", "run-day"):
        c = sub.add_parser(command)
        c.add_argument("--review-id", required=True)
        c.add_argument("--date", required=True)
        if command == "run-day":
            c.add_argument("--apply", action="store_true")
            c.add_argument("--authorization")
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()
    state = (args.state_dir or repo / "intake").resolve()
    try:
        if args.command == "bundle":
            result = bundle_parts(repo, state, date=args.date, source_id=args.source_id, package_ids=args.package_id,
                                  max_packages=args.max_packages, max_text_bytes=args.max_text_bytes,
                                  max_attachments=args.max_attachments, max_bytes=args.max_bytes)
        elif args.command == "segment-zip":
            result = segment_zip(repo, state, args.package_id)
        elif args.command == "prepare-return":
            result = prepare_return(repo, state, args.zip)
        elif args.command == "approve-return":
            result = approve_return(repo, state, args.review_json)
        elif args.command == "prepare-day":
            result = prepare_day(repo, state, args.review_id, args.date)
        else:
            result = run_day(repo, state, args.review_id, args.date, apply=args.apply, authorization=args.authorization)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"READY", "PREPARED", "NOOP", "DRY_RUN_VALID", "COMPLETE", "ALREADY_COMPLETE", "APPROVED_FOR_PREPARATION"} else 2
    except (PipelineError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc), "command": args.command}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
