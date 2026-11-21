"""Frozen, answer-free English teaching projection for the existing local publisher."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from .errors import ValidationError
from .memory_store import lifecycle_fingerprint, open_memory_projection, read_lifecycle
from .util import atomic_write_bytes, bytes_sha256, canonical_bytes, file_sha256, object_sha256

PREFIX = "context/personal-summary/"
ENTRY = PREFIX + "current.json"
SCHEMA = "english-teaching-publication/2"


def verify_graph(repo):
    builder = repo / "scripts/build_writing_vocabulary_relationship_graph.py"
    result = subprocess.run([sys.executable, str(builder), "--verify-only"], cwd=repo,
                            capture_output=True, text=True, timeout=120)
    if result.returncode or json.loads(result.stdout).get("status") != "PASS":
        raise ValidationError("writing relationship graph not current")


def summary_identity(state):
    """Read current native identity. Unavailable and in-progress sources remain distinct."""
    try:
        lifecycle = read_lifecycle(state)
        if lifecycle.get("global_pending"):
            return {"status": "pending"}
        db, manifest = open_memory_projection(state)
        db.close()
        index_sha = file_sha256(state / "personal-summary/current.json")
        if index_sha != manifest["summary_index_sha256"]:
            return {"status": "pending"}
        basis = dict(schema=SCHEMA, source_summary_version=index_sha,
                     memory_generation=manifest["generation"], lifecycle_sha256=lifecycle_fingerprint(lifecycle))
        return dict(basis, status="available", version=object_sha256(basis))
    except (OSError, KeyError, ValueError, sqlite3.Error, ValidationError):
        return {"status": "unknown"}


def freeze_projection(state):
    """Only the existing reviewed teaching export supplies records. Fixed shard paths limit the closure."""
    from .web_review import _web_teaching_context
    from .personal_summary import key_hash

    identity = summary_identity(state)
    if identity["status"] != "available":
        raise ValidationError("English personal summary identity is unavailable")
    exported = _web_teaching_context(state)
    index = json.loads(exported[ENTRY])
    coverage = index["coverage"]
    if coverage["state"] != "available" or coverage["generation"] != identity["memory_generation"]:
        raise ValidationError("English teaching view is not current")
    buckets = {name: [] for name in ["writing", *"0123456789abcdef"]}
    for path, body in sorted(exported.items()):
        if path.startswith(PREFIX + "objects/"):
            record = json.loads(body)
            key = record["key"]
            buckets["writing" if key.startswith("writing_skill:") else key_hash(key)[0]].append(record)
    members = {}
    data = {}
    for name, records in buckets.items():
        path = PREFIX + name + ".json"
        data[path] = canonical_bytes({"schema": SCHEMA, "records": records})
        members[name] = dict(path=path, sha256=bytes_sha256(data[path]), points=len(records))
    coverage = {**coverage, "excluded_points": coverage["total_local_points"] - coverage["included_points"],
                "excluded": ["protected", "pending_or_invalidated", "older_episodes", "raw_evidence", "quick_only"]}
    data[ENTRY] = canonical_bytes(dict(identity, snapshot_kind="bounded_answer_free_advisory",
                                       scope="latest reviewed observation per eligible point", coverage=coverage, members=members))
    if summary_identity(state) != identity:
        raise ValidationError("personal summary changed during publication freeze")
    sha = bytes_sha256(data[ENTRY])
    root = state / "publication" / "safe-teaching" / sha
    files = {}
    for key, body in data.items():
        target = root / Path(key).name
        if target.exists():
            if file_sha256(target) != bytes_sha256(body):
                raise ValidationError("immutable teaching export hash mismatch")
        else:
            atomic_write_bytes(target, body)
        files[key] = target
    return dict(identity, entrypoint=ENTRY, projection_sha256=sha, coverage=coverage), files
