"""Optional local candidate search over an explicitly prepared safe mirror.

The authoritative English store never becomes a Basic Memory or QMD project.
Preparation copies the answer-free mirror, indexes that copy, and records its
identity. Teaching only talks to an already warm, private Unix socket; missing
workers, models, changed notes and timeouts are explicit degradations.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any

from .personal_summary import normalize_key
from .util import atomic_write_json, file_sha256, object_sha256, utc_now
from .runtime_paths import memory_runtime_root, storage_map_root

RUNTIME_ROOT = memory_runtime_root()
SCHEMA = "english_memory_backend_v1"
BACKENDS = {"basic-memory", "qmd"}
BM_MODEL = "minishlab/potion-multilingual-128M"
BM_DIMENSIONS = 256
BM_STATIC_MAX_TOKENS = 4096
QMD_MODEL_FILE = "Qwen3-Embedding-0.6B-Q8_0.gguf"
QMD_MODEL_URL = "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/" + QMD_MODEL_FILE
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

BASIC_MEMORY_CLI_CODE = """import basic_memory.config_logging as logging_config
import fastembed.text.static_embedding as static_embedding
# This static model has no positional window. Keep complete 900-character
# Basic Memory chunks rather than FastEmbed's conservative 512-token default.
static_embedding.MAX_LENGTH = 4096
def console_logging(*, log_level, setup_logging, **kwargs):
    setup_logging(log_level=log_level, log_to_stdout=True)
logging_config.initialize_file_logging = console_logging
logging_config.initialize_api_logging = console_logging
from basic_memory.cli.main import app
app()
"""


def _backend_name(value: str) -> str:
    if value not in BACKENDS:
        raise ValueError("backend must be basic-memory or qmd")
    return value


def _metadata(path: Path) -> dict[str, str]:
    """Read only the small scalar envelope we own, never evaluate YAML."""
    if path.stat().st_size > 512 * 1024:
        raise ValueError("mirror note exceeds the candidate-search size limit")
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("mirror note requires explicit frontmatter")
    end = text.find("\n---", 4)
    if end < 0 or end > 16384:
        raise ValueError("invalid mirror frontmatter")
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        match = re.fullmatch(r"(canonical_key|visibility|summary_sha256):\s*(.*?)\s*", line)
        if not match:
            continue
        key, value = match.groups()
        if key in values:
            raise ValueError("duplicate mirror identity field")
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        if not isinstance(value, str):
            raise ValueError("mirror identity must be text")
        values[key] = value
    if values.get("visibility") != "answer_free":
        raise ValueError("protected or unattested notes cannot enter an ordinary backend")
    values["canonical_key"] = normalize_key(values.get("canonical_key", ""))
    if not re.fullmatch(r"[a-f0-9]{64}", values.get("summary_sha256", "")):
        raise ValueError("mirror note requires a summary hash")
    return values


def inspect_mirror(mirror_dir: Path) -> dict[str, Any]:
    mirror = mirror_dir.resolve(strict=True)
    if not mirror.is_dir():
        raise ValueError("safe mirror must be a directory")
    members: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for path in sorted(mirror.rglob("*.md")):
        if path.is_symlink():
            raise ValueError("mirror symlinks are not indexed")
        path.resolve(strict=True).relative_to(mirror)
        meta = _metadata(path)
        if meta["canonical_key"] in seen:
            raise ValueError("safe mirror contains duplicate canonical keys")
        seen.add(meta["canonical_key"])
        rel = path.relative_to(mirror).as_posix()
        members[rel] = {**meta, "sha256": file_sha256(path), "size": path.stat().st_size}
    if not members:
        raise ValueError("safe mirror contains no explicitly answer-free notes")
    fingerprint = object_sha256(members)
    return {"mirror_dir": str(mirror), "mirror_sha256": fingerprint,
            "corpus_id": object_sha256([str(mirror), fingerprint])[:16], "members": members}


def _environment(runtime: Path, *, config_dir: Path | None = None,
                 report_dir: Path | None = None, offline: bool = True) -> dict[str, str]:
    # No provider credentials are inherited by candidate workers. Cloud routing
    # is also forced off; embeddings are local in both prepared configurations.
    env = {key: value for key, value in os.environ.items()
           if not any(part in key.upper() for part in ("API_KEY", "API_TOKEN", "HF_TOKEN", "ACCESS_TOKEN"))
           and not key.startswith(("BASIC_MEMORY_", "QMD_"))}
    env.update({"BASIC_MEMORY_FORCE_LOCAL": "1", "BASIC_MEMORY_AUTO_UPDATE": "false",
                "BASIC_MEMORY_LOG_LEVEL": "WARNING",
                "BASIC_MEMORY_LOGFIRE_ENABLED": "false", "BASIC_MEMORY_LOGFIRE_SEND_TO_LOGFIRE": "false",
                "HF_HOME": str(runtime / "models/huggingface"), "HF_HUB_DISABLE_TELEMETRY": "1",
                "DO_NOT_TRACK": "1", "TOKENIZERS_PARALLELISM": "false",
                "VECLIB_MAXIMUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2", "OMP_NUM_THREADS": "2",
                "XDG_CACHE_HOME": str(runtime / "cache"),
                "QMD_CONFIG_DIR": str(runtime / "qmd-config"), "QMD_EMBED_PARALLELISM": "1",
                "GGML_METAL_NO_RESIDENCY": "1", "LLAMA_LOG_LEVEL": "error",
                "GGML_LOG_LEVEL": "error", "GGML_BACKEND_SILENT": "1"})
    if offline:
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
    if config_dir:
        env["BASIC_MEMORY_CONFIG_DIR"] = str(config_dir)
    if report_dir:
        temp = report_dir / "tmp"
        temp.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(temp)
    return env


def _state_dir(runtime: Path, backend: str, corpus_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", corpus_id):
        raise ValueError("invalid corpus id")
    return runtime / "states" / _backend_name(backend) / corpus_id


def _family_id(mirror: Path) -> str:
    if mirror.parent.parent.name in {"generations", "exports"}:
        identity = str(mirror.parent.parent.parent / mirror.name)
    else:
        identity = str(mirror)
    return object_sha256(identity)[:16]


def _seed_previous_index(runtime: Path, backend: str, state: Path, info: dict[str, Any]) -> dict[str, Any] | None:
    """Clone a previous derived index, never mutate its generation or receipts."""
    pointer = runtime / "families" / f"{backend}-{_family_id(Path(info['mirror_dir']))}.json"
    if not pointer.is_file():
        return None
    try:
        previous_pointer = json.loads(pointer.read_text())
        previous = _state_dir(runtime, backend, previous_pointer["corpus_id"])
        seed_file = previous_pointer.get("seed_file", "ready.json")
        if seed_file not in {"ready.json", "seed.json"}:
            return None
        prior = json.loads((previous / seed_file).read_text())
        if object_sha256(prior) != previous_pointer.get("ready_sha256"):
            return None
        if previous == state or prior.get("backend") != backend or prior.get("status") not in {"ready", "partial_text_seed"}:
            return None
        expected_model = BM_MODEL if backend == "basic-memory" else str(runtime / "models" / QMD_MODEL_FILE)
        if prior.get("model") != expected_model and backend != "basic-memory":
            return None
        if (previous / "worker.json").exists():
            raise RuntimeError("stop the previous candidate worker before incremental preparation")
        shutil.copytree(previous / "mirror", state / "mirror", dirs_exist_ok=True)
        rel_db = "config/memory.db" if backend == "basic-memory" else "qmd.sqlite"
        old_db, new_db = previous / rel_db, state / rel_db
        new_db.parent.mkdir(parents=True, exist_ok=True)
        if new_db.exists():
            return None
        with closing(sqlite3.connect("file:" + str(old_db) + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(new_db)) as destination:
                source.backup(destination)
                if backend == "basic-memory":
                    # Config reconciliation does not relocate an existing
                    # project row. Rebind this disposable clone, never its
                    # source database, before the official index pass.
                    destination.execute("UPDATE project SET path=?,last_scan_timestamp=NULL,last_file_count=NULL,last_indexed_at=NULL WHERE name='english-memory'",
                                        (str(state / "mirror"),))
                destination.commit()
        return prior
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return None


def _socket_path(runtime: Path, backend: str, corpus_id: str) -> Path:
    short = "bm" if backend == "basic-memory" else "qmd"
    path = runtime / "run" / f"{short}-{corpus_id}.sock"
    if len(os.fsencode(path)) >= 104:
        raise ValueError("runtime path is too long for a private Unix socket")
    return path


def _check_report_root(report_root: Path) -> Path:
    script = storage_map_root() / "storage_map.py"
    subprocess.run([sys.executable, str(script), "check-output", str(report_root), "--subject", "english"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    report_root.mkdir(parents=True, exist_ok=True)
    return report_root.resolve(strict=True)


def verify_prepared_index(backend: str, state: Path, expected_notes: int) -> dict[str, Any]:
    """A successful CLI exit alone does not prove semantic indexing succeeded."""
    path = state / ("config/memory.db" if backend == "basic-memory" else "qmd.sqlite")
    with closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)) as db:
        if backend == "basic-memory":
            paths = [row[0] for row in db.execute("SELECT path FROM project WHERE is_active=1")]
            if paths != [str(state / "mirror")]:
                raise ValueError("backend project is not bound to its private mirror")
            notes = db.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
            embedded = db.execute("SELECT COUNT(DISTINCT entity_id) FROM search_vector_chunks WHERE embedding_status='ready'").fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM search_vector_chunks WHERE embedding_status!='ready'").fetchone()[0]
            vectors = db.execute("SELECT COUNT(*) FROM search_vector_embeddings_rowids").fetchone()[0]
        else:
            notes = db.execute("SELECT COUNT(*) FROM documents WHERE active=1").fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM documents d WHERE d.active=1 AND NOT EXISTS (SELECT 1 FROM content_vectors v WHERE v.hash=d.hash)").fetchone()[0]
            embedded = notes - pending
            vectors = db.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0]
    if notes != expected_notes or embedded != expected_notes or pending or vectors < expected_notes:
        raise ValueError("backend semantic index is incomplete")
    return {"note_count": notes, "embedded_note_count": embedded, "pending_chunks": pending,
            "vector_count": vectors, "status": "PASS"}


def _load_ready(runtime: Path, backend: str, mirror_dir: Path) -> tuple[Path, dict[str, Any]]:
    pointer = runtime / "prepared" / f"{_backend_name(backend)}-{object_sha256(str(mirror_dir.resolve()))[:16]}.json"
    data = json.loads(pointer.read_text(encoding="utf-8"))
    if (data.get("schema_version") != SCHEMA or data.get("backend") != backend
            or data.get("mirror_dir") != str(mirror_dir.resolve()) or data.get("status") != "ready"):
        raise ValueError("backend readiness identity mismatch")
    state = _state_dir(runtime, backend, data["corpus_id"])
    saved = json.loads((state / "ready.json").read_text(encoding="utf-8"))
    if object_sha256(saved) != data.get("ready_sha256"):
        raise ValueError("backend readiness receipt changed")
    if saved.get("api_requests") != 0 or saved.get("provider") != "local":
        raise ValueError("only local backends are supported")
    config_path = state / ("config/config.json" if backend == "basic-memory" else "qmd.json")
    if saved.get("config_sha256") is not None and file_sha256(config_path) != saved["config_sha256"]:
        raise ValueError("prepared backend configuration changed")
    return state, saved


def _degraded(backend: str, reason: str, started: float) -> dict[str, Any]:
    return {"schema_version": SCHEMA, "status": "degraded", "backend": backend,
            "candidate_keys": [], "candidates": [], "degraded": reason,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "api_requests": 0}


def _exchange(path: Path, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("candidate search deadline exceeded")
            sock.settimeout(remaining)
            block = sock.recv(65536)
            if not block:
                raise ValueError("candidate worker closed without a complete reply")
            chunks.extend(block)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise ValueError("candidate worker exceeded response limit")
    result = json.loads(chunks)
    if not isinstance(result, dict):
        raise ValueError("invalid candidate worker result")
    return result


def search_candidates(backend: str, query: str, mirror_dir: Path, *, timeout: float = 2.0,
                      limit: int = 8, runtime_root: Path = RUNTIME_ROOT,
                      mode: str = "hybrid", valid_at: str | None = None,
                      valid_overlaps: str | None = None, time_kind: str | None = None,
                      items: list[str] | None = None,
                      allowed_keys: list[str] | None = None,
                      source_id: str | None = None, unit_id: str | None = None,
                      as_of: str | None = None, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read candidates only; this call never prepares indexes or starts workers."""
    _backend_name(backend)
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("query must be 1–2000 characters")
    if not 0 < timeout <= 2.0 or not 1 <= limit <= 32 or mode not in {"hybrid", "text", "semantic"}:
        raise ValueError("invalid candidate search budget or mode")
    started = time.monotonic()
    # Authored valid-time in Basic Memory is not the native English event-cutoff
    # contract. QMD 2.8.3 has no metadata prefilter in its published SDK. Never
    # approximate either requirement by discarding globally ranked hits later.
    if as_of is not None or (backend == "qmd" and any(value is not None for value in
            (source_id, unit_id, allowed_keys, filters, valid_at, valid_overlaps, time_kind))):
        return _degraded(backend, "scoped_query_native_only", started)
    metadata_filters = dict(filters or {})
    if set(metadata_filters) - {"canonical_key", "source_ids", "unit_ids", "visibility"}:
        raise ValueError("unsupported candidate metadata filter")
    if source_id is not None:
        metadata_filters["source_ids"] = {"$contains": source_id}
    if unit_id is not None:
        metadata_filters["unit_ids"] = {"$contains": unit_id}
    if allowed_keys is not None:
        if not allowed_keys:
            return {**_degraded(backend, "empty_allowed_scope", started), "status": "ready", "degraded": None}
        metadata_filters["canonical_key"] = {"$in": [normalize_key(key) for key in allowed_keys]}
    metadata_filters["visibility"] = "answer_free"
    runtime = runtime_root.resolve()
    try:
        _, ready = _load_ready(runtime, backend, mirror_dir)
        request = {"query": query, "limit": limit, "mode": mode, "corpus_id": ready["corpus_id"],
                   "valid_at": valid_at, "valid_overlaps": valid_overlaps, "time_kind": time_kind,
                   "items": items, "allowed_keys": allowed_keys, "filters": metadata_filters}
        result = _exchange(_socket_path(runtime, backend, ready["corpus_id"]), request,
                           max(0.001, timeout - (time.monotonic() - started)))
        if result.get("status") != "ready":
            return _degraded(backend, result.get("degraded", "backend_error"), started)
        # Validate selected original mirror files again after retrieval. A copy
        # may still contain an old index after the source is changed or revoked.
        safe = []
        changed = False
        allowed = set(allowed_keys) if allowed_keys is not None else None
        mirror = mirror_dir.resolve(strict=True)
        for candidate in result.get("candidates", []):
            rel = candidate.get("path")
            if not isinstance(rel, str) or rel not in ready["members"]:
                continue
            source = mirror / rel
            if source.is_symlink():
                changed = True
                continue
            source.resolve(strict=True).relative_to(mirror)
            expected = ready["members"][rel]
            meta = _metadata(source)
            if (meta["canonical_key"] != expected["canonical_key"]
                    or file_sha256(source) != expected["sha256"]):
                changed = True
                continue
            key = meta["canonical_key"]
            if allowed is not None and key not in allowed:
                continue
            safe.append({"key": key, "path": rel, "summary_sha256": meta["summary_sha256"],
                         "score": candidate.get("score")})
            if len(safe) >= limit:
                break
        if time.monotonic() - started > timeout:
            return _degraded(backend, "timeout", started)
        if changed:
            return _degraded(backend, "mirror_changed", started)
        return {"schema_version": SCHEMA, "status": "ready", "backend": backend,
                "candidate_keys": [r["key"] for r in safe], "candidates": safe,
                "degraded": None, "mirror_sha256": ready["mirror_sha256"],
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "api_requests": 0,
                "scope_filtered": bool(allowed is not None or source_id is not None or unit_id is not None or filters),
                "temporal_applied": result.get("temporal_applied", False)}
    except (TimeoutError, socket.timeout):
        return _degraded(backend, "timeout", started)
    except (FileNotFoundError, ConnectionRefusedError):
        return _degraded(backend, "not_prepared_or_not_warm", started)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _degraded(backend, "unavailable:" + type(exc).__name__, started)


QMD_WORKER = r'''import {createStore} from './qmd/node_modules/@tobilu/qmd/dist/index.js';
import fs from 'node:fs';
import readline from 'node:readline';
const settings=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
process.stderr.write('Opening isolated QMD store\n');
const store=await createStore({dbPath:settings.dbPath,config:settings.config});
async function initializeLocalRuntime(){
 let timer;
 try{
  await Promise.race([store.internal.llm.ensureLlama(false),new Promise((_,reject)=>{
   timer=setTimeout(()=>reject(new Error('native llama initialization exceeded 45 seconds')),45000);
  })]);
 }catch(e){process.stderr.write(e.message+'\n');process.exit(2);}
 finally{clearTimeout(timer);}
}
if(process.argv[3]==='prepare'){
 const update=await store.update(); process.stderr.write('Local embedding preparation\n');
 await initializeLocalRuntime();
 const embed=await store.embed({maxDocsPerBatch:8});
 process.stdout.write(JSON.stringify({update,embed})+'\n'); await store.close(); process.exit(0);
}
await initializeLocalRuntime();
await store.search({queries:[{type:'vec',query:'英语学习'}],rerank:false,limit:1});
process.stdout.write(JSON.stringify({status:'warm'})+'\n');
const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
for await(const line of input){
 try{
  const r=JSON.parse(line);let rows;
  if(r.mode==='text') rows=await store.searchLex(r.query,{limit:r.limit,collection:'english'});
  else if(r.mode==='semantic') rows=await store.searchVector(r.query,{limit:r.limit,collection:'english'});
  else rows=await store.search({queries:[{type:'lex',query:r.query},{type:'vec',query:r.query}],collections:['english'],rerank:false,limit:r.limit});
  process.stdout.write(JSON.stringify({results:rows})+'\n');
 }catch(e){process.stdout.write(JSON.stringify({error:e.name+': '+e.message})+'\n');}
}
await store.close();
'''


def prepare_backend(backend: str, mirror_dir: Path, *, report_root: Path,
                    runtime_root: Path = RUNTIME_ROOT, qmd_cpu: bool = False) -> dict[str, Any]:
    """Explicit maintenance: copy, index, and seal a safe retrieval projection."""
    _backend_name(backend)
    runtime = runtime_root.resolve()
    report = _check_report_root(report_root)
    info = inspect_mirror(mirror_dir)
    info["mirror_corpus_id"] = info["corpus_id"]
    recipe = {"backend": backend, "model": BM_MODEL if backend == "basic-memory" else QMD_MODEL_FILE,
              "dimensions": BM_DIMENSIONS if backend == "basic-memory" else None,
              "static_max_tokens": BM_STATIC_MAX_TOKENS if backend == "basic-memory" else None}
    info["corpus_id"] = object_sha256([info["corpus_id"], recipe])[:16]
    state = _state_dir(runtime, backend, info["corpus_id"])
    state.mkdir(parents=True, exist_ok=True)
    prior = _seed_previous_index(runtime, backend, state, info)
    copy = state / "mirror"
    copy.mkdir(exist_ok=True)
    for old_file in copy.rglob("*.md"):
        if old_file.relative_to(copy).as_posix() not in info["members"]:
            old_file.unlink()  # Only this newly derived copy, never the old generation.
    for rel in info["members"]:
        destination = copy / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if prior and prior.get("members", {}).get(rel) == info["members"][rel] and destination.is_file():
            continue
        shutil.copy2(Path(info["mirror_dir"]) / rel, destination)
    began = time.monotonic()
    env = _environment(runtime, report_dir=report, offline=False)
    if backend == "basic-memory":
        config_dir = state / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = {"projects": {"english-memory": {"path": str(copy), "mode": "local"}},
               "default_project": "english-memory", "semantic_embedding_provider": "fastembed",
               "semantic_embedding_model": BM_MODEL, "semantic_embedding_dimensions": BM_DIMENSIONS,
               "semantic_embedding_document_prefix": None, "semantic_embedding_query_prefix": None,
               "semantic_embedding_cache_dir": str(runtime / "models/fastembed"),
               "semantic_embedding_threads": None, "semantic_embedding_batch_size": 32,
               "semantic_embedding_sync_batch_size": 8, "semantic_embedding_request_concurrency": 1,
               "reranker_enabled": False, "index_changes": False, "auto_update": False,
               "cloud_promo_opt_out": True, "logfire_enabled": False,
               "logfire_send_to_logfire": False, "ensure_frontmatter_on_sync": False}
        atomic_write_json(config_dir / "config.json", cfg)
        env["BASIC_MEMORY_CONFIG_DIR"] = str(config_dir)
        cmd = [str(runtime / "basic-memory/bin/python"), "-c", BASIC_MEMORY_CLI_CODE,
               "project", "index", "english-memory"]
        package_version = subprocess.check_output([str(runtime / "basic-memory/bin/python"), "-c",
            "import importlib.metadata;print(importlib.metadata.version('basic-memory'))"], env=env, text=True).strip()
        model = BM_MODEL
    else:
        model_path = runtime / "models" / QMD_MODEL_FILE
        if not model_path.is_file():
            import urllib.request
            model_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = model_path.with_suffix(".download")
            with urllib.request.urlopen(QMD_MODEL_URL, timeout=120) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, 1024 * 1024)
            os.replace(temporary, model_path)
        settings = {"dbPath": str(state / "qmd.sqlite"), "cpu_only": qmd_cpu,
                    "config": {"collections": {"english": {"path": str(copy), "pattern": "**/*.md"}},
                               "models": {"embed": str(model_path)}}}
        atomic_write_json(state / "qmd.json", settings)
        if qmd_cpu:
            env["QMD_FORCE_CPU"] = "1"
        bridge = runtime / "qmd-worker.mjs"
        bridge.write_text(QMD_WORKER, encoding="utf-8")
        node = shutil.which("node")
        if not node:
            raise FileNotFoundError("Node runtime unavailable")
        cmd = [node, str(bridge), str(state / "qmd.json"), "prepare"]
        package_version = json.loads((runtime / "qmd/node_modules/@tobilu/qmd/package.json").read_text())["version"]
        model = str(model_path)
    log_path = report / f"{backend}-{info['corpus_id']}-prepare-{time.time_ns()}.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=3600)
    if process.returncode:
        raise RuntimeError(f"{backend} indexing failed; see {log_path}")
    verification = verify_prepared_index(backend, state, len(info["members"]))
    model_files = {}
    model_root = runtime / "models/fastembed" if backend == "basic-memory" else runtime / "models"
    for path in model_root.rglob("*"):
        if path.is_file() and path.suffix in {".onnx", ".onnx_data", ".gguf", ".safetensors"}:
            if backend == "qmd" and path.name != QMD_MODEL_FILE:
                continue
            if backend == "basic-memory" and "potion-multilingual-128M" not in str(path):
                continue
            model_files[str(path.relative_to(runtime))] = {"sha256": file_sha256(path), "bytes": path.stat().st_size}
    if not model_files:
        raise RuntimeError("indexing did not leave a verifiable local model")
    ready = {"schema_version": SCHEMA, "status": "ready", "backend": backend, **info,
             "package_version": package_version, "model": model, "model_files": model_files,
             "provider": "local", "reranking": False, "query_expansion": False,
             "compute_requested": "cpu" if backend == "basic-memory" or qmd_cpu else "auto",
             "incremental_basis": prior["corpus_id"] if prior else None,
             "embedding_recipe_changed": bool(prior and prior.get("model") != model),
             "recipe": recipe,
             "index_verification": verification,
             "prepared_at": utc_now(), "prepare_seconds": round(time.monotonic() - began, 3),
             "report_root": str(report), "prepare_log": str(log_path), "api_requests": 0}
    ready["config_sha256"] = file_sha256(state / ("config/config.json" if backend == "basic-memory" else "qmd.json"))
    atomic_write_json(state / "ready.json", ready)
    pointer = {"schema_version": SCHEMA, "status": "ready", "backend": backend,
               "mirror_dir": info["mirror_dir"], "corpus_id": info["corpus_id"],
               "ready_sha256": object_sha256(ready)}
    atomic_write_json(runtime / "prepared" / f"{backend}-{object_sha256(info['mirror_dir'])[:16]}.json", pointer)
    atomic_write_json(runtime / "families" / f"{backend}-{_family_id(Path(info['mirror_dir']))}.json", pointer)
    atomic_write_json(report / f"{backend}-{info['corpus_id']}-ready-{time.time_ns()}.json", ready)
    return {key: value for key, value in ready.items() if key != "members"}


def _resolve_result_path(row: dict[str, Any], ready: dict[str, Any], state: Path) -> str | None:
    candidates = [row.get(k) for k in ("file_path", "filepath", "path", "file", "displayPath")]
    if isinstance(row.get("metadata"), dict):
        candidates += [row["metadata"].get(k) for k in ("file_path", "path")]
    for value in candidates:
        if not isinstance(value, str):
            continue
        if value.startswith("qmd://english/"):
            value = value[len("qmd://english/"):]
        elif value.startswith("english/"):
            value = value[len("english/"):]
        if Path(value).is_absolute():
            try:
                value = Path(value).relative_to(state / "mirror").as_posix()
            except ValueError:
                continue
        if value in ready["members"]:
            return value
    return None


def _clean_results(payload: Any, ready: dict[str, Any], state: Path,
                   allowed_keys: list[str] | None, limit: int) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    rows = payload.get("results", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("backend did not return a results array")
    allowed = set(allowed_keys) if allowed_keys is not None else None
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rel = _resolve_result_path(row, ready, state)
        if rel is None:
            continue
        key = ready["members"][rel]["canonical_key"]
        if key in seen or (allowed is not None and key not in allowed):
            continue
        seen.add(key)
        score = row.get("score")
        result.append({"key": key, "path": rel, "score": score if isinstance(score, (int, float)) else None})
        if len(result) >= limit:
            break
    return result


def serve_backend(backend: str, mirror_dir: Path, *, runtime_root: Path = RUNTIME_ROOT) -> None:
    """Explicit maintenance worker. Never called by search_candidates."""
    runtime = runtime_root.resolve()
    state, ready = _load_ready(runtime, backend, mirror_dir)
    verify_prepared_index(backend, state, len(ready["members"]))
    socket_path = _socket_path(runtime, backend, ready["corpus_id"])
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    runner = asyncio.Runner() if backend == "basic-memory" else None
    child = None
    if backend == "basic-memory":
        config = json.loads((state / "config/config.json").read_text())
        if config.get("semantic_embedding_provider") != "fastembed" or config.get("reranker_enabled"):
            raise ValueError("Basic Memory worker requires the prepared local-only configuration")
        os.environ.update(_environment(runtime, config_dir=state / "config", offline=True))
        import fastembed.text.static_embedding as static_embedding
        static_embedding.MAX_LENGTH = BM_STATIC_MAX_TOKENS
        from loguru import logger
        logger.remove()
        logger.add(sys.stderr, level="WARNING")
        from basic_memory.mcp.tools.ranked_search import bm_search
        def search(request: dict[str, Any]) -> Any:
            return runner.run(bm_search(query=request["query"], mode=request.get("mode", "hybrid"),
                project="english-memory", page_size=min(100, request.get("limit", 8) * 4),
                compact=True, output_format="json", items=request.get("items"),
                filters=request.get("filters"),
                valid_at=request.get("valid_at"), valid_overlaps=request.get("valid_overlaps"),
                time_kind=request.get("time_kind")))
        search({"query": "英语学习", "limit": 1})
    else:
        node = shutil.which("node")
        if not node:
            raise FileNotFoundError("Node runtime unavailable")
        qmd_env = _environment(runtime, offline=True)
        if json.loads((state / "qmd.json").read_text()).get("cpu_only"):
            qmd_env["QMD_FORCE_CPU"] = "1"
        child = subprocess.Popen([node, str(runtime / "qmd-worker.mjs"), str(state / "qmd.json"), "serve"],
            env=qmd_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=sys.stderr, text=True, bufsize=1)
        handshake = json.loads(child.stdout.readline())
        if handshake.get("status") != "warm":
            raise ValueError("QMD model warmup failed")
        def search(request: dict[str, Any]) -> Any:
            if request.get("valid_at") or request.get("valid_overlaps"):
                raise ValueError("QMD temporal validation is owned by the native memory layer")
            child.stdin.write(json.dumps({**request, "limit": min(100, request.get("limit", 8) * 4)}) + "\n")
            child.stdin.flush()
            result = json.loads(child.stdout.readline())
            if "error" in result:
                raise RuntimeError(result["error"])
            return result
    if socket_path.exists():
        socket_path.unlink()
    running = True
    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False
        if child is not None:
            child.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            server.listen(4)
            server.settimeout(0.5)
            atomic_write_json(state / "worker.json", {"pid": os.getpid(), "socket": str(socket_path),
                                                       "corpus_id": ready["corpus_id"], "ready_at": utc_now()})
            while running:
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(2.0)
                    try:
                        data = bytearray()
                        while not data.endswith(b"\n"):
                            block = client.recv(4096)
                            if not block:
                                break
                            data.extend(block)
                            if len(data) > 16384:
                                raise ValueError("request too large")
                        request = json.loads(data)
                        if request.get("corpus_id") != ready["corpus_id"]:
                            raise ValueError("worker corpus mismatch")
                        if request.get("operation") == "ping":
                            response = {"status": "ready", "backend": backend}
                        else:
                            if not isinstance(request.get("query"), str) or len(request["query"]) > 2000:
                                raise ValueError("invalid query")
                            raw = search(request)
                            response = {"status": "ready", "candidates": _clean_results(raw, ready, state,
                                request.get("allowed_keys"), request.get("limit", 8)),
                                "temporal_applied": bool(isinstance(raw, dict) and raw.get("temporal_applied"))}
                    except Exception as exc:
                        response = {"status": "degraded", "degraded": "backend_error:" + type(exc).__name__}
                    try:
                        client.sendall(json.dumps(response, ensure_ascii=False).encode() + b"\n")
                    except (BrokenPipeError, OSError):
                        pass
    finally:
        socket_path.unlink(missing_ok=True)
        (state / "worker.json").unlink(missing_ok=True)
        if child:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
        if runner:
            runner.close()


def backend_status(backend: str, mirror_dir: Path, *, runtime_root: Path = RUNTIME_ROOT) -> dict[str, Any]:
    start = time.monotonic()
    try:
        state, ready = _load_ready(runtime_root.resolve(), backend, mirror_dir)
        response = _exchange(_socket_path(runtime_root.resolve(), backend, ready["corpus_id"]),
                             {"operation": "ping", "corpus_id": ready["corpus_id"]}, 0.25)
        return {"status": "warm" if response.get("status") == "ready" else "prepared", "backend": backend,
                "corpus_id": ready["corpus_id"], "package_version": ready["package_version"],
                "model": ready["model"], "mirror_sha256": ready["mirror_sha256"], "api_requests": 0}
    except (OSError, ValueError, KeyError):
        return _degraded(backend, "not_prepared_or_not_warm", start)


def stop_backend(backend: str, mirror_dir: Path, *, runtime_root: Path = RUNTIME_ROOT) -> dict[str, Any]:
    try:
        state, ready = _load_ready(runtime_root.resolve(), backend, mirror_dir)
        worker = json.loads((state / "worker.json").read_text())
        # Normally verify the live socket. A busy worker can exceed the ping
        # budget; in that case verify its exact managed command before signalling.
        try:
            _exchange(Path(worker["socket"]), {"operation": "ping", "corpus_id": ready["corpus_id"]}, 0.25)
        except (OSError, ValueError):
            command = subprocess.check_output(["/bin/ps", "-p", str(int(worker["pid"])), "-o", "command="], text=True)
            script = str(Path(__file__).resolve().parents[1] / "scripts/english_memory_backends.py")
            if script + " serve " not in command or "--backend " + backend not in command or str(mirror_dir.resolve()) not in command:
                raise ValueError("worker PID no longer belongs to this backend")
        os.kill(int(worker["pid"]), signal.SIGTERM)
        return {"status": "stopping", "backend": backend, "pid": worker["pid"]}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {"status": "not_running", "backend": backend}


def start_backend(backend: str, mirror_dir: Path, *, runtime_root: Path = RUNTIME_ROOT,
                  wait_seconds: float = 90.0) -> dict[str, Any]:
    runtime = runtime_root.resolve()
    state, ready = _load_ready(runtime, backend, mirror_dir)
    if backend_status(backend, mirror_dir, runtime_root=runtime).get("status") == "warm":
        return backend_status(backend, mirror_dir, runtime_root=runtime)
    # Ensure only one heavyweight candidate model is resident, even across corpora.
    for worker_file in (runtime / "states").glob("*/*/worker.json"):
        try:
            old_ready = json.loads((worker_file.parent / "ready.json").read_text())
            stop_backend(old_ready["backend"], Path(old_ready["mirror_dir"]), runtime_root=runtime)
            deadline = time.monotonic() + 15
            while worker_file.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            if worker_file.exists():
                raise RuntimeError("previous candidate worker has not stopped; refusing concurrent models")
        except (OSError, ValueError, KeyError):
            continue
    report = _check_report_root(Path(ready["report_root"]))
    script = Path(__file__).resolve().parents[1] / "scripts/english_memory_backends.py"
    python = runtime / "basic-memory/bin/python" if backend == "basic-memory" else Path(sys.executable)
    env = _environment(runtime, config_dir=state / "config" if backend == "basic-memory" else None,
                       report_dir=report, offline=True)
    log_path = report / f"{backend}-{ready['corpus_id']}-worker.log"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen([str(python), str(script), "serve", "--backend", backend,
            "--mirror", str(mirror_dir.resolve()), "--runtime-root", str(runtime)],
            stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    started = time.monotonic()
    while time.monotonic() - started < wait_seconds:
        if process.poll() is not None:
            raise RuntimeError(f"backend warmup failed; see {log_path}")
        status = backend_status(backend, mirror_dir, runtime_root=runtime)
        if status.get("status") == "warm":
            return {**status, "warmup_seconds": round(time.monotonic() - started, 3), "pid": process.pid}
        time.sleep(0.2)
    os.killpg(process.pid, signal.SIGTERM)
    raise TimeoutError(f"backend warmup exceeded budget; see {log_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "start", "stop", "status", "search", "serve"])
    parser.add_argument("--backend", choices=sorted(BACKENDS), required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, default=RUNTIME_ROOT)
    parser.add_argument("--report-root", type=Path)
    parser.add_argument("--query")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--mode", choices=["text", "hybrid", "semantic"], default="hybrid")
    parser.add_argument("--valid-at")
    parser.add_argument("--valid-overlaps")
    parser.add_argument("--time-kind")
    parser.add_argument("--source-id")
    parser.add_argument("--unit-id")
    parser.add_argument("--as-of")
    parser.add_argument("--allowed-key", action="append")
    parser.add_argument("--item", action="append", choices=["note", "observation", "relation"])
    parser.add_argument("--qmd-cpu", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            if args.report_root is None:
                parser.error("prepare requires a verified English cold --report-root")
            result = prepare_backend(args.backend, args.mirror, report_root=args.report_root,
                                     runtime_root=args.runtime_root, qmd_cpu=args.qmd_cpu)
        elif args.command == "start":
            result = start_backend(args.backend, args.mirror, runtime_root=args.runtime_root)
        elif args.command == "stop":
            result = stop_backend(args.backend, args.mirror, runtime_root=args.runtime_root)
        elif args.command == "status":
            result = backend_status(args.backend, args.mirror, runtime_root=args.runtime_root)
        elif args.command == "serve":
            serve_backend(args.backend, args.mirror, runtime_root=args.runtime_root)
            return 0
        else:
            result = search_candidates(args.backend, args.query, args.mirror, limit=args.limit,
                mode=args.mode, valid_at=args.valid_at, valid_overlaps=args.valid_overlaps,
                time_kind=args.time_kind, source_id=args.source_id, unit_id=args.unit_id,
                as_of=args.as_of, allowed_keys=args.allowed_key, items=args.item, runtime_root=args.runtime_root)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
