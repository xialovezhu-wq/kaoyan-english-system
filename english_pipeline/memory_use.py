"""Private, bounded evidence of which memory was supplied to a saved English unit."""
from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path
from typing import Any

from .errors import IdempotencyConflict, ValidationError
from .packages import _write_immutable
from .util import bytes_sha256, canonical_bytes, load_json, object_sha256

SCHEMA = "english_memory_use_observation_v1"
REVIEW_SCHEMA = "english_memory_use_review_v1"
MAX_BYTES = 64 * 1024
MAX_CALLS = 12
MAX_ASSISTANT_MESSAGES = 32
WRITE_RESERVE = .075
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
PACKAGE_ID = re.compile(r"EN-PKG-\d{8}-[A-F0-9]{16}\Z")
CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
COMMANDS = {"query-memory-context", "render-tense-timeline"}
VERDICTS = {"applied", "inapplicable", "contradicted_by_current_answer", "unknown"}
_KINDS = {"function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"}


def _safe(value, size):
    return isinstance(value, str) and 0 < len(value.encode()) <= size and not re.search(r"[\x00-\x1f]", value)


def _digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def _options(tokens: list[str], name: str) -> list[str]:
    values = []
    for i, token in enumerate(tokens):
        if token.startswith(name + "="):
            values.append(token.split("=", 1)[1])
        elif token == name and i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            values.append(tokens[i + 1])
    return values


def _one(tokens: list[str], name: str) -> str | None:
    result = _options(tokens, name)
    if len(result) > 1:
        raise ValueError("ambiguous argument")
    return result[0] if result else None


def _exec_args(payload: dict) -> dict | None:
    kind, name = payload.get("type"), payload.get("name")
    if kind == "function_call" and name == "exec_command":
        args = payload.get("arguments")
        return json.loads(args) if isinstance(args, str) and len(args) <= 20000 else None
    if kind != "custom_tool_call" or name not in {"exec", "functions.exec"}:
        return None
    code = payload.get("input")
    if not isinstance(code, str) or len(code) > 20000:
        return None
    # Only accept an unambiguous one-shot exec call that exposes its actual
    # output. More general JS transports remain unknown.
    code = re.sub(r"^\s*// @exec:[^\n]*\n", "", code, count=1).strip()
    quoted = r"""(?:"(?:[^"\\]|\\.)*"|'[^'\\]*'|`[^`\\]*`)"""
    simple_fields = r"(?:cmd|workdir|max_output_tokens|yield_time_ms|login)"
    field = rf"{simple_fields}\s*:\s*(?:{quoted}|\d+|true|false)"
    call = rf"await\s+tools\.exec_command\(\s*\{{\s*(?P<fields>{field}(?:\s*,\s*{field})*\s*,?)\s*\}}\s*\)"
    binding = re.fullmatch(rf"const\s+(?P<var>[A-Za-z_][A-Za-z_0-9]*)\s*=\s*{call}\s*;\s*"
                           r"text\(\s*(?P=var)(?:\.output)?\s*\)\s*;?", code, flags=re.S)
    direct = binding or re.fullmatch(rf"text\(\s*{call}\s*\)\s*;?", code, flags=re.S)
    if not direct:
        return None
    fields = {}
    for match in re.finditer(rf"(?P<key>{simple_fields})\s*:\s*(?P<value>{quoted}|\d+|true|false)",
                             direct["fields"]):
        name, literal = match["key"], match["value"]
        if name in fields or "${" in literal:
            return None
        fields[name] = json.loads(literal) if literal[0] not in {"'", "`"} else literal[1:-1]
    return fields


def _command(payload: dict, cwd: Path, repo: Path, state: Path) -> dict | None:
    args = _exec_args(payload)
    if not isinstance(args, dict) or not isinstance(args.get("cmd"), str):
        return None
    body = args["cmd"].strip()
    if "\n" in body or "\r" in body or len(body) > 16000:
        return None
    lexer = shlex.shlex(body, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    words = list(lexer)
    if not words or any(re.fullmatch(r"[;&|<>]+", word) for word in words):
        return None
    if not re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(words[0]).name):
        return None
    working = Path(args.get("workdir") or cwd).resolve()
    if len(words) >= 3 and (working / words[1]).resolve() == repo / "scripts/prepare_english_readings.py":
        if words[2] != "query" or len(words[3:]) % 2:
            return None
        options = dict(zip(words[3::2], words[4::2]))
        if (len(options) != len(words[3::2]) or not {"--source-id", "--unit-id"} <= options.keys()
                or options.keys() - {"--source-id", "--unit-id", "--source-hash", "--record-sha256"}):
            return None
        return {"command": "reading_unit", "source_id": options["--source-id"], "unit_id": options["--unit-id"]}
    if len(words) >= 5 and Path(words[1]).name == "english_skill.py" and words[2:4] == ["run", "english_learning_pipeline.py"]:
        wrapper = (working / words[1]).resolve()
        if wrapper != (Path.home() / ".codex/skills/kaoyan-english-intensive-reading/scripts/english_skill.py").resolve():
            return None
        command, options = words[4], words[5:]
    elif len(words) >= 3 and (working / words[1]).resolve() == repo / "scripts/english_learning_pipeline.py":
        command, options = words[2], words[3:]
    else:
        return None
    if command not in COMMANDS:
        return None
    repo_option, state_option = _one(options, "--repo-root"), _one(options, "--state-dir")
    if (repo_option and (working / repo_option).resolve() != repo
            or state_option and (working / state_option).resolve() != state):
        return None
    if command == "render-tense-timeline":
        return {"command": command}
    if _one(options, "--view") not in {None, "teaching"} or any(
            _options(options, flag) for flag in ("--as-of", "--known-publication")):
        return None
    return {"command": command, "source_id": _one(options, "--source-id"),
            "unit_id": _one(options, "--unit-id"), "question_id": _one(options, "--question-id"),
            "unlocked": _options(options, "--unlocked-question-id"),
            "prepared": bool(_options(options, "--prepare-diagram"))}


def _output(payload: dict) -> dict | None:
    value = payload.get("output")
    if isinstance(value, list):
        if len(value) not in {1, 2} or any(
                not isinstance(block, dict) or block.get("type") not in {"input_text", "text"}
                or not isinstance(block.get("text"), str) for block in value):
            return None
        blocks = [block["text"] for block in value]
        # The live Codex tool transport may journal its fixed status header as
        # one text block and the single actual stdout as the next.
        if len(blocks) == 2 and not re.fullmatch(
                r"Script completed\n(?:Wall time [0-9.]+ seconds\n)?Output:\n", blocks[0]):
            return None
        value = "".join(blocks)
    if not isinstance(value, str) or len(value.encode()) > MAX_BYTES:
        return None
    if not value.lstrip().startswith("{"):
        match = re.search(r"(?m)^Output:\s*\n", value)
        if not match or "truncated" in value[:match.start()].lower() or not (
                value.startswith("Script completed") or re.search(r"exited with code 0\b", value[:match.start()])):
            return None
        value = value[match.end():]
    result = json.loads(value)
    if isinstance(result, dict) and result.get("exit_code") == 0 and isinstance(result.get("output"), str):
        result = json.loads(result["output"])
    return result if isinstance(result, dict) else None


class UnitToolTrace:
    """Collect only matching call/results during the existing visible-message scan."""

    def __init__(self, repo: Path, state: Path, *, start_line: int | None, end_line: int | None,
                 turn_ids: list[str], deadline: float, source_unit_id: str | None = None):
        self.repo, self.state = repo.resolve(), state.resolve()
        self.cwd = self.repo
        self.start, self.end = start_line, end_line
        self.turn_ids = set(turn_ids)
        self.deadline = deadline
        self.source_unit_id = source_unit_id
        self.pending: dict[str, tuple] = {}
        self.paired: list[tuple] = []
        self.final_messages: set[int] = set()
        self.omissions: set[str] = set()
        self.finished_ids: set[str] = set()

    def observe(self, kind: str, row: dict, number: int, turn: str | None):
        if kind in {"turn_context", "session_meta"} and isinstance(row.get("cwd"), str):
            # Resolve only if a candidate command is seen.
            self.cwd = Path(row["cwd"])
        if (kind != "response_item" or (self.start is not None and number <= self.start)
                or (self.end is not None and number > self.end)
                or self.turn_ids and turn not in self.turn_ids):
            return
        typ = row.get("type")
        if (typ == "message" and row.get("role") == "assistant" and row.get("recipient") in {None, "all"}
                and row.get("phase") in {None, "final", "final_answer"} and row.get("channel") in {None, "final"}):
            if len(self.final_messages) < MAX_ASSISTANT_MESSAGES:
                self.final_messages.add(number)
            else:
                self.omissions.add("assistant_message_limit")
        if typ not in _KINDS:
            return
        cid = row.get("call_id")
        if not isinstance(cid, str) or not CALL_ID.fullmatch(cid):
            return
        if time.monotonic() >= self.deadline - WRITE_RESERVE:
            self.omissions.add("diagnostic_budget")
            return
        try:
            if typ in {"function_call", "custom_tool_call"}:
                if cid in self.pending or cid in self.finished_ids:
                    self.pending.pop(cid, None)
                    self.paired = [pair for pair in self.paired if pair[0] != cid]
                    self.omissions.add("ambiguous_call_id")
                    self.finished_ids.add(cid)
                    return
                request = _command(row, self.cwd, self.repo, self.state)
                if request is not None:
                    reading = request["command"] == "reading_unit"
                    if reading and request["unit_id"] != self.source_unit_id:
                        return
                    seen = [r[2] for r in self.pending.values()] + [r[4] for r in self.paired]
                    if sum((r["command"] == "reading_unit") == reading for r in seen) < MAX_CALLS:
                        self.pending[cid] = (number, turn, request, typ + "_output")
                    else:
                        self.omissions.add("call_limit")
            elif cid in self.pending:
                opened, original_turn, request, expected_type = self.pending.pop(cid)
                self.finished_ids.add(cid)
                if typ != expected_type:
                    self.omissions.add("call_result_type_mismatch")
                    return
                if original_turn != turn:
                    self.omissions.add("call_result_turn_mismatch")
                    return
                result = _output(row)
                if result is None:
                    self.omissions.add("incomplete_tool_result")
                else:
                    self.paired.append((cid, opened, number, turn, request, result))
            elif cid in self.finished_ids:
                self.paired = [pair for pair in self.paired if pair[0] != cid]
                self.omissions.add("ambiguous_call_id")
        except (ValueError, TypeError, OSError, RecursionError):
            self.omissions.add("unreadable_tool_envelope")


def _query_result(request: dict, result: dict, source: dict) -> tuple[dict, list[dict]] | None:
    match_scope = all(request[k] == source.get(k) for k in ("source_id", "unit_id", "question_id"))
    if not match_scope or not request["source_id"] or not request["unit_id"]:
        return None
    question = source.get("question_id")
    fully_unlocked = bool(question and source.get("answer_exposure") == "protected"
                          and request["unlocked"] == [question])
    if request["unlocked"] and not fully_unlocked:
        return None
    if request["prepared"]:
        scope = result.get("scope")
        if (result.get("schema_version") != "english_diagram_context_v1" or result.get("status") != "prepared"
                or not isinstance(scope, dict)
                or any(scope.get(k) != source.get(k) for k in ("source_id", "unit_id", "source_hash", "question_id"))):
            return None
        result = result.get("memory")
    if (not isinstance(result, dict) or result.get("schema_version") != "english_memory_context_v1"
            or result.get("view") != "teaching" or result.get("status") not in {"ready", "partial"}
            or result.get("formal_write_count") != 0 or not _digest(result.get("generation"))
            or not isinstance(result.get("matched"), list) or len(result["matched"]) > 8):
        return None
    evidence = []
    for point in result["matched"]:
        if not isinstance(point, dict):
            return None
        if point.get("role") == "explicit_preference":
            continue
        latest = point.get("latest_observation")
        if not isinstance(latest, dict):
            continue
        visibility = point.get("visibility")
        if visibility != "answer_free" and (visibility != "protected" or not fully_unlocked):
            return None
        entry = {"episode_id": latest.get("episode_id"), "point_key": point.get("key"),
                 "summary_sha256": point.get("summary_sha256"), "scope_match": point.get("scope_match", "exact_unit"),
                 "visibility": visibility, "status": "supplied"}
        if (not _safe(entry["episode_id"], 512) or not _safe(entry["point_key"], 256)
                or not _digest(entry["summary_sha256"]) or entry["scope_match"] not in {"same_article", "exact_unit", "other_article"}):
            return None
        if entry["scope_match"] == "other_article" and not (
                request["prepared"] and not question and visibility == "answer_free"
                and entry["point_key"].startswith(("word:", "phrase:"))
                and (point.get("audit") or {}).get("status") == "verified"
                and _safe(latest.get("source_article"), 256)
                and latest["source_article"] not in {source.get("source_id"), source.get("reference_id")}):
            return None
        evidence.append(entry)
    return (result, evidence) if evidence else None


def _visual_result(result: dict, source: dict, attachment_hashes: set[str]) -> dict | None:
    if (result.get("schema_version") != "english_tense_timeline_receipt_v1" or result.get("status") != "rendered"
            or result.get("formal_write_count") != 0 or result.get("learning_event_write_count") != 0
            or not all(_digest(result.get(key)) for key in ("asset_sha256", "template_sha256", "config_sha256"))
            or result.get("template_version") != "english-tense-timeline-v1"
            or result.get("asset_sha256") not in attachment_hashes
            or not isinstance(result.get("sources"), list) or len(result["sources"]) not in (1, 2)):
        return None
    keys = ("source_id", "unit_id", "source_hash", "sentence_sha256", "question_id")
    if any(not isinstance(scope, dict) or any(scope.get(k) != source.get(k) for k in keys)
           or scope.get("visibility") != "answer_free" for scope in result["sources"]):
        return None
    return {key: result[key] for key in ("asset_sha256", "template_version", "template_sha256", "config_sha256")}


def _document(trace: UnitToolTrace, session_id: str, source: dict, selected: list[dict],
              manifest: dict) -> dict:
    start, end = (selected[i]["metadata"]["rollout_line"] for i in (0, -1))
    turns = {m["metadata"].get("turn_id") for m in selected}
    binding = {"session_id": session_id, "package_id": manifest["package_id"],
               "package_canonical_sha256": manifest["package_canonical_sha256"],
               "source_id": source.get("source_id"), "source_hash": source.get("source_hash"),
               "unit_id": source.get("unit_id"), "sentence_sha256": source.get("sentence_sha256"),
               "question_id": source.get("question_id"), "answer_exposure": source.get("answer_exposure"),
               "start_line": start, "end_line": end,
               "turn_ids": sorted(turn for turn in turns if isinstance(turn, str))}
    assistants = [{"message_id": m["message_id"], "line": m["metadata"]["rollout_line"]}
                  for m in selected if m["role"] == "assistant"
                  and m["metadata"]["rollout_line"] in trace.final_messages]
    attachments = {a.get("sha256") for a in manifest.get("attachments", []) if isinstance(a, dict)}
    queries, visuals = [], []
    omissions = set(trace.omissions)
    for cid, opened, closed, turn, request, payload in trace.paired:
        if not (start < opened < closed < end and turn in turns):
            continue
        if request["command"] == "reading_unit":
            continue
        if request["command"] == "render-tense-timeline":
            asset = _visual_result(payload, source, attachments)
            if asset:
                visuals.append({"call_id": cid, **asset})
            continue
        found = _query_result(request, payload, source)
        if found is None:
            omissions.add("query_scope_or_result_unverified")
            continue
        result, evidence = found
        queries.append({"call_id": cid, "call_line": opened, "result_line": closed,
                        "result_sha256": object_sha256(payload), "generation": result["generation"],
                        "evidence": evidence})
    if any(start < opened < end and turn in turns for opened, turn, _, _ in trace.pending.values()):
        omissions.add("missing_paired_result")
    doc = {"schema_version": SCHEMA, "unit_binding": binding, "queries": queries,
           "visual_assets": visuals, "public_explanations": assistants,
           "status": "supplied" if queries else "unknown", "omissions": sorted(omissions),
           "learning_gain": "not_assessed", "formal_write_count": 0, "learning_event_write_count": 0}
    while len(canonical_bytes(doc)) + 100 > MAX_BYTES and doc["queries"]:
        doc["queries"].pop()
        doc["omissions"] = sorted(set(doc["omissions"]) | {"sidecar_limit"})
    if not doc["queries"]:
        doc["status"] = "unknown"
    return {**doc, "observation_sha256": object_sha256(doc)}


def _path(state: Path, pid: str) -> Path:
    if not isinstance(pid, str) or not PACKAGE_ID.fullmatch(pid):
        raise ValidationError("memory-use requires an exact package ID")
    return state / "memory-use" / "observations" / f"{pid}.json"


def _sealed_read(path: Path, field: str, schema: str) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValidationError("missing or unbounded memory-use record")
    raw = load_json(path)
    if raw.get("schema_version") != schema or object_sha256({k: v for k, v in raw.items() if k != field}) != raw.get(field):
        raise ValidationError("memory-use record hash mismatch")
    return raw


def _reference(doc: dict, path: Path) -> dict:
    return {"status": doc["status"], "observation_path": str(path), "observation_sha256": doc["observation_sha256"],
            "supplied_count": sum(len(q["evidence"]) for q in doc["queries"]),
            "visual_count": len(doc["visual_assets"]), "learning_gain": "not_assessed",
            "formal_write_count": 0, "learning_event_write_count": 0}


def record_capture(state: Path, package: Path, receipt: dict, trace: UnitToolTrace, *, session_id: str,
                   source: dict, selected: list[dict], deadline: float) -> dict:
    if time.monotonic() >= deadline - WRITE_RESERVE:
        return {"status": "unknown", "reason": "capture_budget_exhausted"}
    try:
        path = _path(state, receipt["package_id"])
        if path.exists():
            existing = _sealed_read(path, "observation_sha256", SCHEMA)
            binding = existing["unit_binding"]
            if binding["package_id"] != receipt["package_id"] or binding["package_canonical_sha256"] != receipt["package_sha256"]:
                return {"status": "unknown", "reason": "observation_package_mismatch"}
            return _reference(existing, path)
        manifest = load_json(package / "manifest.json")
        if (manifest.get("package_id") != receipt["package_id"]
                or manifest.get("package_canonical_sha256") != receipt["package_sha256"]):
            return {"status": "unknown", "reason": "capture_manifest_mismatch"}
        doc = _document(trace, session_id, source, selected, manifest)
        raw = canonical_bytes(doc) + b"\n"
        if len(raw) > MAX_BYTES or time.monotonic() >= deadline - WRITE_RESERVE:
            return {"status": "unknown", "reason": "observation_budget_or_size"}
        # Concurrent identical retries have the same immutable package and
        # observation bytes. Never replace a previously read, unequal record.
        try:
            _write_immutable(path, raw)
        except IdempotencyConflict:
            return inspect(state, receipt["package_id"], receipt["package_sha256"])
        return _reference(_sealed_read(path, "observation_sha256", SCHEMA), path)
    except (OSError, ValueError, TypeError, KeyError, ValidationError):
        return {"status": "unknown", "reason": "observation_unavailable"}


def inspect(state: Path, package_id: str, package_sha256: str) -> dict:
    path = _path(state, package_id)
    try:
        doc = _sealed_read(path, "observation_sha256", SCHEMA)
        if (doc["unit_binding"]["package_id"], doc["unit_binding"]["package_canonical_sha256"]) != (package_id, package_sha256):
            raise ValidationError("memory-use observation refers to another package")
        return {**_reference(doc, path), "omissions": doc["omissions"]}
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        return {"status": "unknown", "reason": "no_matching_capture_observation",
                "formal_write_count": 0, "learning_event_write_count": 0}


def review(state: Path, manifest_path: Path, package_id: str, reviewer: str, verdicts: list[dict]) -> dict:
    """Validate positions from a real author decision. Semantic judgment stays with the author."""
    from .local_review import validate_local_packages
    from .packages import validate_conversation_package
    from .util import file_sha256

    if not _safe(reviewer, 200) or not isinstance(verdicts, list) or not 1 <= len(verdicts) <= 3:
        raise ValidationError("memory-use review requires a reviewer and one to three decisions")
    manifest_path = manifest_path.resolve(strict=True)
    manifest = load_json(manifest_path)
    batch, day, local_id = (manifest.get(k, "") for k in ("batch_id", "study_date", "local_review_id"))
    if (manifest.get("schema_version") != "english_nightly_manifest_v3" or not local_id
            or not re.fullmatch(r"EN-BATCH-\d{8}-[A-F0-9]{12}", batch) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
            or manifest_path != (state / "nightly" / day / f"{batch}.manifest.json").resolve()):
        raise ValidationError("memory-use review requires the canonical local authoring freeze")
    rows = manifest["package_documents"]
    validate_local_packages(state, local_id, rows, study_date=day)
    row = next((row for row in rows if row["package_id"] == package_id), None)
    if row is None:
        raise ValidationError("memory-use package is outside this formal review")
    root = Path(row["path"])
    package = validate_conversation_package(root)
    doc = _sealed_read(_path(state, package_id), "observation_sha256", SCHEMA)
    binding = doc["unit_binding"]
    if (binding["package_id"] != package_id or binding["package_canonical_sha256"] != row["package_sha256"]
            or package["package_canonical_sha256"] != row["package_sha256"]):
        raise ValidationError("memory-use observation is not bound to the frozen package")
    available = {(q["call_id"], e["episode_id"], e["summary_sha256"]): q["result_line"]
                 for q in doc["queries"] for e in q["evidence"]}
    messages = {m["message_id"]: m for m in load_json(root / "conversation.json")["messages"] if m["role"] == "assistant"}
    explanations = {m["message_id"]: m["line"] for m in doc["public_explanations"]}
    result, seen = [], set()
    for entry in verdicts:
        base = {"call_id", "episode_id", "summary_sha256", "status", "reason"}
        if (not isinstance(entry, dict) or not base <= entry.keys() <= base | {"assistant_message_id", "span"}
                or entry["status"] not in VERDICTS or not _safe(entry["reason"], 500)):
            raise ValidationError("invalid author verdict for memory-use")
        key = tuple(entry[field] for field in ("call_id", "episode_id", "summary_sha256"))
        if key not in available or key in seen:
            raise ValidationError("memory-use verdict is outside the actually supplied observations")
        seen.add(key)
        if entry["status"] == "applied":
            msg_id = entry.get("assistant_message_id")
            msg, span = messages.get(msg_id), entry.get("span")
            if (msg is None or explanations.get(msg_id, 0) <= available[key]
                    or not isinstance(span, list) or len(span) != 2 or not all(type(i) is int for i in span)):
                raise ValidationError("applied requires a public explanation after the matching result")
            raw = msg["content"].encode()
            start, end = span
            if not (0 <= start < end <= len(raw)):
                raise ValidationError("applied explanation range is outside the original")
            try:
                valid = bool(raw[start:end].decode().strip())
            except UnicodeDecodeError:
                valid = False
            if not valid:
                raise ValidationError("applied explanation must span intact UTF-8")
        elif {"assistant_message_id", "span"} & entry.keys():
            raise ValidationError("only applied verdicts cite an adopted explanation span")
        result.append({**entry, "review_status": "reviewed"})
    record = {"schema_version": REVIEW_SCHEMA, "manifest_sha256": file_sha256(manifest_path),
              "package_id": package_id, "package_sha256": row["package_sha256"],
              "observation_sha256": doc["observation_sha256"], "reviewer": reviewer, "use_observations": result,
              "learning_gain": "not_assessed", "formal_write_count": 0, "learning_event_write_count": 0}
    record["review_sha256"] = object_sha256(record)
    path = state / "memory-use" / "reviews" / package_id / (record["review_sha256"] + ".json")
    try:
        _write_immutable(path, canonical_bytes(record) + b"\n")
    except IdempotencyConflict:
        if _sealed_read(path, "review_sha256", REVIEW_SCHEMA) != record:
            raise
    _sealed_read(path, "review_sha256", REVIEW_SCHEMA)
    return {**record, "review_path": str(path)}


def replay(repo: Path, state: Path, package: Path, *, rollout: Path | None = None,
           session_id: str | None = None) -> dict:
    """Read a sealed package and, when possible, align its original exact visible log lines."""
    from .current_unit import locate_rollout, read_messages
    from .packages import validate_conversation_package
    manifest = validate_conversation_package(package)
    pid, digest = manifest["package_id"], manifest["package_canonical_sha256"]
    existing = inspect(state, pid, digest)
    if existing["status"] != "unknown" or (rollout is None and session_id is None):
        return {"package_id": pid, "package_sha256": digest, **existing}
    if not session_id or manifest.get("thread_ref_sha256") != bytes_sha256(("codex-thread:" + session_id).encode()):
        raise ValidationError("saved package does not belong to this Codex session")
    messages = load_json(package / "conversation.json")["messages"]
    lines = [m.get("metadata", {}).get("rollout_line") for m in messages]
    if any(type(i) is not int for i in lines):
        return {"package_id": pid, "package_sha256": digest, "status": "unknown", "reason": "saved_rollout_lines_unavailable"}
    turns = list(dict.fromkeys(m["metadata"].get("turn_id") for m in messages if m["metadata"].get("turn_id")))
    trace = UnitToolTrace(repo, state, start_line=lines[0], end_line=lines[-1], turn_ids=turns, deadline=time.monotonic()+8)
    try:
        raw = read_messages(rollout or locate_rollout(session_id), session_id, tool_trace=trace)
    except (OSError, ValueError, ValidationError):
        return {"package_id": pid, "package_sha256": digest, "status": "unknown", "reason": "original_rollout_unavailable"}
    actual = {m["metadata"]["rollout_line"]: m for m in raw}
    if any(i not in actual or any(m[k] != actual[i][k] for k in ("message_id", "role", "content"))
           for i, m in zip(lines, messages, strict=True)):
        return {"package_id": pid, "package_sha256": digest, "status": "unknown", "reason": "original_visible_bytes_not_matched"}
    source = load_json(package / "source.json")["identity"]
    return {"package_id": pid, "package_sha256": digest, **_document(trace, session_id, source, messages, manifest)}
