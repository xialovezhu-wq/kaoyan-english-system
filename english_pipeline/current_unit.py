"""One foreground save of a bounded, real English learning conversation."""
from __future__ import annotations

import argparse
import base64
import contextlib
from datetime import datetime
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from .constants import REPO_ROOT
from .errors import IdempotencyConflict, PipelineError, SourceHashMismatch, ValidationError
from .packages import _ATTACHMENT_ROLES, create_conversation_package, unit_binding_path
from .reading_preparation import DEFAULT_OUTPUT, SCHEMA as READING_SCHEMA, UnknownReadingSource, query
from .unit_dialogue import visible_content
from .util import load_json, object_sha256, sentence_sha256, text_sha256
from .web_review import package_root, segment_zip


def locate_rollout(session_id: str) -> Path:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    databases = sorted((p for p in home.glob("state_*.sqlite") if re.fullmatch(r"state_\d+\.sqlite", p.name)),
                       key=lambda p: int(p.stem.split("_")[1]), reverse=True)
    for database in databases:
        with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            try:
                row = conn.execute("SELECT rollout_path FROM threads WHERE id=?", (session_id,)).fetchone()
            except sqlite3.Error:
                continue
        if row and Path(row[0]).is_file():
            return Path(row[0])
    raise ValidationError("current session log was not found; provide its exact --rollout path")


def read_messages(rollout: Path, session_id: str, *, tool_trace=None) -> list[dict]:
    messages = []
    actual = None
    turn = None
    with rollout.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            payload = row.get("payload", {})
            kind = row.get("type")
            if kind == "session_meta":
                actual = payload.get("id")
                if actual != session_id:
                    raise ValidationError("rollout session identity differs from --session-id; do not use a context-window ID")
            if kind == "turn_context" or (kind == "event_msg" and payload.get("type") == "task_started"):
                turn = payload.get("turn_id", turn)
            if tool_trace is not None:
                tool_trace.observe(kind, payload, number, turn)
            if kind != "response_item":
                continue
            visible = visible_content(payload)
            if visible is None:
                continue
            text, blocks = visible
            messages.append({"role": payload["role"], "content": text,
                             "message_id": payload.get("id") or f"{session_id}:line-{number}",
                             "created_at": row.get("timestamp"),
                             "metadata": {"rollout_line": number, "turn_id": turn,
                                          "original_content_blocks": blocks}})
    if actual != session_id:
        raise ValidationError("rollout has no matching session_meta identity")
    return messages


def image_role(value: str) -> tuple[int, int, str]:
    parts = value.split(":")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit() or parts[2] not in _ATTACHMENT_ROLES:
        raise argparse.ArgumentTypeError("expected LINE:BLOCK:ROLE")
    return int(parts[0]), int(parts[1]), parts[2]


def attachment_argument(value: str) -> tuple[str, Path]:
    role, separator, name = value.partition(":")
    if not separator or role not in _ATTACHMENT_ROLES or not Path(name).is_absolute():
        raise argparse.ArgumentTypeError("expected ROLE:/absolute/path")
    return role, Path(name)


def _local_path(value) -> Path | None:
    if isinstance(value, dict):
        value = value.get("url") or value.get("path")
    if not isinstance(value, str):
        return None
    if value.startswith("file://"):
        value = unquote(urlparse(value).path)
    if not value.startswith("/"):
        return None
    path = Path(value)
    return path if path.is_file() else None


def _attachment_input(path: Path, role: str) -> dict:
    from .lesson_display import REVISION_DIR
    item = {"path": str(path), "role": role}
    if path.parent.name == REVISION_DIR:
        if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{64}\.html", path.name):
            raise ValidationError("invalid saved lesson revision")
        item["expected_sha256"] = path.stem
    return item


def collect_attachments(messages: list[dict], roles: list[tuple[int, int, str]], temporary: Path,
                        *, explicit_paths: set[Path] | None = None) -> tuple[list[dict], list[str]]:
    overrides = {(line, block): role for line, block, role in roles}
    used = set()
    attachments = []
    missing = []
    by_path = {}

    def attach(message, path, role):
        key = (str(path.resolve()), role)
        if key not in by_path:
            by_path[key] = f"ATT-{len(attachments) + 1:03d}"
            attachments.append(_attachment_input(path, role))
        message.setdefault("attachment_refs", []).append(by_path[key])

    for message in messages:
        blocks = message["metadata"]["original_content_blocks"]
        line = message["metadata"]["rollout_line"]
        for index, block in enumerate(blocks):
            if block.get("type") in {"input_text", "output_text", "text"}:
                continue
            key = (line, index)
            role = overrides.get(key, "other_attachment")
            if key in overrides:
                used.add(key)
            value = block.get("image_url") or block.get("url") or block.get("path") or block.get("file_path")
            path = _local_path(value)
            if path is None and 0 < index < len(blocks) - 1:
                opening = re.fullmatch(r'<image name=\[[^\]\n]+\] path="([^"\n]+)">', blocks[index - 1].get("text", ""))
                if opening and blocks[index + 1].get("text", "") == "</image>":
                    path = _local_path(opening.group(1))
            if path is None:
                raw_url = value.get("url") if isinstance(value, dict) else value
                embedded = re.fullmatch(r"data:([^;,]+);base64,(.+)", raw_url, re.DOTALL) if isinstance(raw_url, str) else None
                if embedded:
                    data = base64.b64decode(embedded.group(2), validate=True)
                    suffix = mimetypes.guess_extension(embedded.group(1)) or ".bin"
                    path = temporary / f"message-{line}-block-{index}{suffix}"
                    path.write_bytes(data)
            if path is None:
                missing.append(f"attachments.message-{line}.block-{index}.original_unavailable")
            else:
                attach(message, path, role)
        # App attachment headers and local Markdown embeds are explicit locators.
        text_paths = re.findall(r"^## [^\n]+: (/[^\n]+)$", message["content"], re.MULTILINE)
        text_paths += re.findall(r"!\[[^\]]*\]\((/[^)]+)\)", message["content"])
        # The explanation can live entirely inside the referenced interactive
        # HTML. Seal its available bytes, not only a task-local display path.
        visuals = re.findall(r"visualize(.*?)", message["content"], re.DOTALL)
        if message["content"].count("visualize") > len(visuals):
            missing.append(f"attachments.message-{line}.visualization_reference_invalid")
        for encoded in visuals:
            try:
                visual = json.loads(encoded)
                value = visual.get("path") if isinstance(visual, dict) else None
                local_name = unquote(urlparse(value).path) if isinstance(value, str) and value.startswith("file://") else value
                if (not isinstance(value, str) or not value.startswith(("/", "file://"))
                        or Path(local_name).suffix.casefold() != ".html"):
                    raise ValueError("visualization must reference an absolute HTML file")
                # An existing --attachment invocation keeps its original order
                # and message metadata, so retrying that exact call stays stable.
                if Path(local_name).resolve() in (explicit_paths or set()):
                    continue
                text_paths.append(value)
            except (ValueError, TypeError):
                missing.append(f"attachments.message-{line}.visualization_reference_invalid")
        for value in dict.fromkeys(text_paths):
            path = _local_path(value)
            if path is None:
                missing.append(f"attachments.message-{line}.{Path(value).name}.original_unavailable")
            else:
                existing = next((ref for (name, _), ref in by_path.items() if name == str(path.resolve())), None)
                if existing is None:
                    attach(message, path, "other_attachment")
                elif existing not in message.setdefault("attachment_refs", []):
                    message["attachment_refs"].append(existing)
    if set(overrides) != used:
        raise ValidationError("an image-role does not point to a selected attachment block")
    return attachments, missing


def _sent_unit(args, trace, start, end):
    """Use only complete, paired stdout from a source read inside this learning unit."""
    readings = []
    for call_id, opened, closed, _, request, result in trace.paired:
        if request["command"] != "reading_unit" or not start < opened < closed <= end:
            continue
        names = (result.get("source_id"), result.get("reference_id"))
        unit = result.get("unit")
        if (not args.source_id or args.source_id not in names or request["source_id"] not in names
                or request["unit_id"] != args.unit_id or result.get("schema_version") != READING_SCHEMA
                or not all(isinstance(result.get(k), str) and result[k] for k in ("source_id", "source_article"))
                or "reference_id" not in result
                or not isinstance(unit, dict) or unit.get("unit_id") != args.unit_id
                or unit.get("answer_exposure") != "answer_free" or not isinstance(unit.get("text"), str)
                or unit.get("text_sha256") != text_sha256(unit["text"])
                or not re.fullmatch(r"[a-f0-9]{64}", str(result.get("source_hash")))):
            continue
        readings.append((result, {"call_id": call_id, "rollout_line": closed, "result_sha256": object_sha256(result)}))
    if not readings:
        return None
    if args.source_hash:
        readings = [r for r in readings if r[0]["source_hash"] == args.source_hash.removeprefix("sha256:")]
    if len({(r["source_hash"], object_sha256(r["unit"])) for r, _ in readings}) != 1:
        raise ValidationError("the observed source version is ambiguous or differs from the supplied hash")
    return readings[-1]


def source_for(args, observed=None) -> dict:
    source = {"review_route": args.review_route or "local", "source_kind": args.source_kind,
              "answer_exposure": args.answer_exposure, "unit_id": args.unit_id}
    if args.source_id:
        source["source_id"] = args.source_id
    prepared = args.repo_root / DEFAULT_OUTPUT
    record = None
    if args.source_id and ((prepared / "index.json").is_file()
                           or (prepared / "articles" / f"{args.source_id}.json").is_file()):
        try:
            record = query(prepared, source_id=args.source_id, repo_root=args.repo_root)
        except UnknownReadingSource:
            pass  # New, unprepared sources can carry their own exact text below.
        except (ValidationError, SourceHashMismatch, OSError):
            if not observed:
                raise
    if observed:
        sent, locator = observed
        source["source_read"] = locator
        if (record is None or record["source_hash"] != sent["source_hash"]
                or (sent.get("record_sha256") and record["record_sha256"] != sent["record_sha256"])
                or sent["unit"] not in record["units"]):
            # Only the original unit was returned. A later paragraph or question
            # would supply context the learner never saw at this location.
            record = {**sent, "units": [sent["unit"]], "paragraphs": [], "questions": []}
    if record is not None:
        if args.source_hash and args.source_hash.removeprefix("sha256:") != record["source_hash"]:
            raise ValidationError("source hash differs from the prepared article")
        if record.get("record_sha256"):
            source["record_sha256"] = record["record_sha256"]
        if not observed:
            source["source_binding_basis"] = "caller_source_hash" if args.source_hash else "prepared_at_capture"
        source.update({key: record[key] for key in ("source_id", "source_hash", "source_article", "reference_id")})
        units = [unit for unit in record["units"] if unit["unit_id"] == args.unit_id]
        paragraphs = [p for p in record["paragraphs"] if p["paragraph_id"] == args.unit_id]
        questions = [q for q in record["questions"] if q["question_id"] == args.unit_id]
        if args.unit_id == record.get("reference_id"):
            if args.source_kind not in {"article", "explanation"}:
                raise ValidationError("full article source kind is invalid")
            # A complete lesson uses the article's existing identity, rather
            # than borrowing one sentence/question ID for unrelated units.
            source["source_sentence"] = "\n\n".join(unit["text"] for unit in record["units"])
        elif units:
            unit = units[0]
            if args.source_kind not in {unit["kind"], "explanation"}:
                raise ValidationError("source kind conflicts with the exact prepared unit")
            source["source_sentence"] = unit["text"]
            for key in ("sentence_id", "paragraph_id", "question_id"):
                if unit.get(key):
                    source[key] = unit[key]
            if unit.get("paragraph_id") and record["paragraphs"]:
                source["paragraph_context"] = next(p["text"] for p in record["paragraphs"] if p["paragraph_id"] == unit["paragraph_id"])
        elif paragraphs:
            if args.source_kind not in {"article", "explanation"}:
                raise ValidationError("paragraph source kind is invalid")
            source.update(paragraph_id=args.unit_id, source_sentence=paragraphs[0]["text"])
        elif questions:
            if args.source_kind not in {"question", "explanation"}:
                raise ValidationError("question source kind is invalid")
            source.update(question_id=args.unit_id,
                          source_sentence="\n".join(unit["text"] for unit in questions[0]["units"]))
        else:
            raise ValidationError("unit is not present in the named prepared article")
        if source.get("question_id") and record["questions"]:
            source["question_context"] = next(q for q in record["questions"] if q["question_id"] == source["question_id"])
    else:
        if args.source_hash:
            source["source_hash"] = args.source_hash.removeprefix("sha256:")
        source["sentence_id"] = args.unit_id
        question = re.match(r"^(Q\d+)(?:-|$)", args.unit_id)
        if question:
            if args.source_kind in {"article", "user_provided"}:
                raise ValidationError("a question unit cannot be an ordinary article sentence")
            source["question_id"] = question.group(1)
    supplied = args.source_text_file.read_bytes().decode("utf-8") if args.source_text_file else args.source_text
    if supplied is not None:
        if source.get("source_sentence") and source["source_sentence"] != supplied:
            raise ValidationError("supplied source text differs from the selected article unit")
        source["source_sentence"] = supplied
    if source.get("source_sentence"):
        source["sentence_sha256"] = sentence_sha256(source["source_sentence"])
    if args.source_kind == "explanation" and args.answer_exposure != "protected":
        raise ValidationError("an explanation must preserve protected answer exposure")
    if args.supplement_of:
        if any(not re.fullmatch(r"EN-PKG-[0-9]{8}-[A-F0-9]{16}", pid) for pid in args.supplement_of):
            raise ValidationError("supplement-of must be an exact existing package ID")
        state = (args.state_dir or args.repo_root / "intake").resolve()
        hashes = {}
        for pid in dict.fromkeys(args.supplement_of):
            date_token = pid.split("-")[2]
            day = f"{date_token[:4]}-{date_token[4:6]}-{date_token[6:]}"
            if not ((state / "packages" / day / pid).is_dir()
                    or (state / "archive-pointers" / day / f"{pid}.json").is_file()):
                raise ValidationError("supplement-of has no existing package or archive pointer")
            receipt = load_json(package_root(state, args.repo_root, pid) / "receipt.json")
            if receipt["package_id"] != pid:
                raise ValidationError("supplement-of package identity mismatch")
            hashes[pid] = receipt["package_sha256"]
        source["prior_package_ids"] = args.supplement_of
        source["prior_package_sha256s"] = hashes
    return source


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["save", "prepare"], nargs="?", default="save")
    p.add_argument("--session-id", default=os.environ.get("CODEX_THREAD_ID"))
    p.add_argument("--rollout", type=Path)
    p.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p.add_argument("--state-dir", type=Path)
    p.add_argument("--start-line", type=int)
    p.add_argument("--end-line", type=int, help="Inclusive message line; default is the latest real user message")
    p.add_argument("--turn-id", action="append", default=[], help="Optional exact selection for a noncontiguous unit")
    p.add_argument("--unit-id")
    p.add_argument("--review-route", choices=["local", "web"], help="Default local; web explicitly requests a legacy segment ZIP.")
    p.add_argument("--source-id")
    p.add_argument("--source-hash")
    p.add_argument("--source-text")
    p.add_argument("--source-text-file", type=Path)
    p.add_argument("--source-kind", choices=["article", "question", "option", "explanation", "user_provided"])
    p.add_argument("--answer-exposure", choices=["answer_free", "protected"])
    p.add_argument("--image-role", action="append", type=image_role, default=[])
    p.add_argument("--attachment", action="append", type=attachment_argument, default=[])
    p.add_argument("--supplement-of", action="append", default=[])
    p.add_argument("--budget-ms", type=int, default=20000)
    return p


def _source_for_save(args, state: Path, thread_ref: str, key: str, day: str, trace, start, end) -> dict:
    binding_path = unit_binding_path(state, text_sha256(thread_ref), key)
    if not binding_path.exists():
        return source_for(args, _sent_unit(args, trace, start, end))
    binding = load_json(binding_path)
    pid = binding.get("package_id", "")
    if (not re.fullmatch(r"EN-PKG-[0-9]{8}-[A-F0-9]{16}", pid)
            or binding.get("segment_key") != key or binding.get("study_date") != day
            or binding.get("thread_ref_sha256") != text_sha256(thread_ref)):
        raise IdempotencyConflict("the saved unit has a different identity or study date")
    if not ((state / "packages" / day / pid).exists()
            or (state / "archive-pointers" / day / f"{pid}.json").exists()):
        # A crash after reserving the key can leave no sealed source yet.
        return source_for(args, _sent_unit(args, trace, start, end))
    root = package_root(state, args.repo_root, pid)
    if load_json(root / "receipt.json").get("request_sha256") != binding.get("request_sha256"):
        raise ValidationError("the sealed source is not bound to this unit receipt")
    frozen = load_json(root / "source.json")["identity"]
    requested = {"unit_id": args.unit_id, "source_kind": args.source_kind,
                 "answer_exposure": args.answer_exposure,
                 "prior_package_ids": args.supplement_of or None}
    if args.source_hash is not None:
        requested["source_hash"] = args.source_hash.removeprefix("sha256:")
    if args.review_route is not None:
        requested["review_route"] = args.review_route
    supplied = args.source_text_file.read_bytes().decode() if args.source_text_file else args.source_text
    if supplied is not None:
        requested["source_sentence"] = supplied
    if ((args.source_id != frozen.get("source_id") and not (args.source_id and args.source_id == frozen.get("reference_id")))
            or any(value != frozen.get(field) for field, value in requested.items())):
        raise IdempotencyConflict("the supplied source differs from the sealed unit; use a linked supplement")
    return frozen


def run(args) -> dict:
    started = time.monotonic()
    if not args.session_id:
        raise ValidationError("a real session ID is required")
    args.repo_root = args.repo_root.resolve()
    state = (args.state_dir or args.repo_root / "intake").resolve()
    if args.action == "save" and (args.source_id or "").startswith("SYN-") and args.repo_root == REPO_ROOT and state == REPO_ROOT / "intake":
        raise ValidationError("synthetic display material cannot enter native learner Capture")
    rollout = (args.rollout or locate_rollout(args.session_id)).resolve()
    trace = None
    if args.action == "save":
        if not 1 <= args.budget_ms <= 40000:
            raise ValidationError("budget-ms must be 1 through 40000")
        from .memory_use import UnitToolTrace, record_capture
        deadline = started + args.budget_ms / 1000
        trace = UnitToolTrace(args.repo_root, state, start_line=args.start_line, end_line=args.end_line,
                              turn_ids=args.turn_id, deadline=deadline, source_unit_id=args.unit_id)
    messages = read_messages(rollout, args.session_id, tool_trace=trace)
    users = [m for m in messages if m["role"] == "user"]
    if not users:
        raise ValidationError("the selected session has no real user messages")
    end = args.end_line if args.end_line is not None else users[-1]["metadata"]["rollout_line"]
    if not any(m["metadata"]["rollout_line"] == end for m in messages):
        raise ValidationError("end-line must point to a visible message")
    selected = [m for m in messages if (args.start_line is None or m["metadata"]["rollout_line"] >= args.start_line)
                and m["metadata"]["rollout_line"] <= end]
    if args.action == "prepare":
        preview = selected[-80:]
        return {"status": "prepared", "session_id": args.session_id, "rollout": str(rollout),
                "end_line": end, "start_line": args.start_line,
                "messages_total": len(selected), "earlier_messages_omitted": len(selected) - len(preview),
                "previous_end_line": selected[-81]["metadata"]["rollout_line"] if len(selected) > 80 else None,
                "messages": [{"line": m["metadata"]["rollout_line"], "turn_id": m["metadata"]["turn_id"],
                              "role": m["role"], "preview": m["content"][:240],
                              "preview_truncated": len(m["content"]) > 240, "content_characters": len(m["content"]),
                              "attachment_blocks": [i for i, b in enumerate(m["metadata"]["original_content_blocks"])
                                                    if b.get("type") not in {"input_text", "output_text", "text"}]}
                             for m in preview], "capture_write_count": 0, "formal_write_count": 0}
    if not args.unit_id or not args.source_kind or not args.answer_exposure:
        raise ValidationError("save requires unit-id, source-kind and answer-exposure")
    if args.turn_id:
        selected = [m for m in selected if m["metadata"]["turn_id"] in args.turn_id]
        found = list(dict.fromkeys(m["metadata"]["turn_id"] for m in selected))
        if found != args.turn_id:
            raise ValidationError("selected turn IDs must be present once and in original order")
    elif args.start_line is None:
        raise ValidationError("supply the known start-line, or run prepare once to select it")
    if (not selected or not any(m["role"] == "user" for m in selected)
            or (args.start_line is not None and selected[0]["metadata"]["rollout_line"] != args.start_line)):
        raise ValidationError("unit boundaries do not identify a complete visible conversation")
    dates = []
    for message in selected:
        stamp = datetime.fromisoformat(str(message["created_at"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValidationError("rollout timestamps require timezones")
        dates.append(stamp.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat())
    start = selected[0]["metadata"]["rollout_line"]
    key = "EN-UNIT-" + object_sha256([args.session_id, start])[:24].upper()
    thread_ref = "codex-thread:" + args.session_id
    source = _source_for_save(args, state, thread_ref, key, min(dates), trace, start, end)
    with tempfile.TemporaryDirectory(prefix="english-unit-") as temporary:
        attachments, missing = collect_attachments(selected, args.image_role, Path(temporary),
            explicit_paths={path.resolve() for _, path in args.attachment})
        from .article_capture import article_attachments
        from .article_workspace import RUNTIME
        if args.repo_root == REPO_ROOT and state == (REPO_ROOT / "intake"):
            displays, gaps = article_attachments(RUNTIME, source, selected, args.session_id, Path(temporary))
            missing.extend(gaps)
            for message, path in displays:
                index = next((i for i,a in enumerate(attachments) if Path(a["path"]) == path), None)
                if index is None:
                    index = len(attachments)
                    attachments.append(_attachment_input(path, "other_attachment"))
                ref = f"ATT-{index + 1:03d}"
                refs = message.setdefault("attachment_refs", [])
                if ref not in refs:
                    refs.append(ref)
        extra = list(args.attachment)
        if args.source_text_file:
            extra.append(("other_attachment", args.source_text_file))
        for role, path in extra:
            if not path.is_file():
                missing.append(f"attachments.{role}.{path.name}.original_unavailable")
            elif not any(Path(a["path"]).resolve() == path.resolve() and a["role"] == role for a in attachments):
                attachments.append(_attachment_input(path, role))
        if any(a["role"] == "explanation_image" for a in attachments) and args.answer_exposure != "protected":
            raise ValidationError("an explanation image requires protected answer exposure")
        if not source.get("source_sentence"):
            missing.append("source.source_sentence")
        if source.get("source_read"):
            for field in ("paragraph", "question"):
                if source.get(f"{field}_id") and not source.get(f"{field}_context"):
                    missing.append(f"source.{field}_context")
        request = {"capture_mode": "independent_unit", "idempotency_key": key, "segment_key": key,
                   "thread_ref": thread_ref, "study_date": min(dates),
                   "source": source, "conversation": selected, "attachments": attachments, "missing_fields": missing}
        try:
            package, receipt = create_conversation_package(state, request, deadline=deadline)
        except IdempotencyConflict:
            if args.review_route is not None:
                raise
            # A retry spanning the default-route change must reuse the frozen
            # request. The existing independent-unit binding still checks all bytes.
            request["source"] = {**source, "review_route": "web"}
            package, receipt = create_conversation_package(state, request, deadline=deadline)
        transport = {}
        if args.review_route == "web":
            transport["zip_path"] = segment_zip(args.repo_root, state, receipt["package_id"])["zip_path"]
    provenance = record_capture(state, package, receipt, trace, session_id=args.session_id,
                                source=request["source"], selected=selected, deadline=deadline)
    return {**receipt, **transport, "memory_use": provenance, "message_count": len(selected),
            "attachment_count": len(attachments), "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "model_call_count": 0, "mcp_call_count": 0,
            "save_args": {"session_id": args.session_id, "rollout": str(rollout), "start_line": start, "end_line": end}}


def main(argv=None) -> int:
    try:
        result = run(parser().parse_args(argv))
    except (PipelineError, ValueError, OSError) as exc:
        print(json.dumps({"status": "capture_pending", "message": str(exc), "formal_write_count": 0}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
