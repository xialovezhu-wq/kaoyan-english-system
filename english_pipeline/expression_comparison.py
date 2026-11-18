"""Read-only display of a learner's translation alongside tutor-authored alternatives."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .answer_safety import contains_answer_signal
from .errors import ValidationError
from .reading_preparation import DEFAULT_OUTPUT, query
from .util import learner_message_spans, object_sha256, text_sha256


def _excerpt(messages, ref, role, *, original=""):
    if not isinstance(ref, dict) or not isinstance(ref.get("quote"), str) or not ref["quote"].strip():
        raise ValidationError("supply an exact nonempty quote and message ID")
    rows = [m for m in messages if m["message_id"] == ref.get("message_id") and m["role"] == role]
    if len(rows) != 1:
        raise ValidationError("the cited message and speaker do not match the conversation")
    row, quote = rows[0], ref["quote"]
    spans = learner_message_spans(row, original) if original and role == "user" else (row["content"],)
    if not any(quote in span for span in spans):
        raise ValidationError("the quote differs from the cited speaker's own words")
    return quote, row["metadata"]["rollout_line"]


def render_comparison(repo, data, read_messages):
    """Only open conversation evidence when the requested stage actually needs it."""
    if not isinstance(data, dict) or not isinstance(data.get("source"), dict):
        raise ValidationError("a comparison needs a source and stage")
    source, stage = data["source"], data.get("stage")
    if stage not in ("before_translation", "after_translation", "direct_explanation"):
        raise ValidationError("unknown translation stage")
    sid, unit = source.get("source_id"), source.get("unit_id")
    if ("question_id" in source or "question_id" in data or not isinstance(sid, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", sid)
            or not isinstance(unit, str) or not re.fullmatch(r"S[0-9]+", unit)):
        raise ValidationError("use the authorized article sentence; question panels have their own scope")
    messages = None
    source_line = None
    kind = source.get("source_kind", "article")
    if kind == "user_provided":
        messages = read_messages()
        original, source_line = _excerpt(messages, source.get("original_message"), "user")
        if contains_answer_signal(original):
            raise ValidationError("answer-bearing material belongs in the scoped question view")
        version = text_sha256(original)
    elif kind == "article" and "original_message" not in source:
        record = query(repo / DEFAULT_OUTPUT, repo_root=repo, source_id=sid, unit_id=unit)
        item = record["unit"]
        if item["kind"] != "article" or item["answer_exposure"] != "answer_free":
            raise ValidationError("comparison requires an answer-free article sentence")
        sid, version, original = record["source_id"], record["source_hash"], item["text"]
        if source.get("record_sha256") is not None and source["record_sha256"] != record.get("record_sha256"):
            raise ValidationError("the prepared sentence locations changed")
    else:
        raise ValidationError("the source kind must match the article or original user message")
    if source.get("source_hash") is not None and source["source_hash"] != version:
        raise ValidationError("source version changed; read the sentence again")
    binding = dict(source_kind=kind, source_id=sid, unit_id=unit, source_hash=version, sentence_sha256=text_sha256(original))
    if kind == "article" and record.get("record_sha256"):
        binding["record_sha256"] = record["record_sha256"]
    result = dict(stage=stage, source_binding=binding, original=original, formal_write_count=0,
                  learning_event_write_count=0)
    if stage == "before_translation":
        return {**result, "chat_text": original + "\n\n请先给出你的首译。"}

    from .personal_summary import _validate_followup_text
    if messages is None:
        messages = read_messages()
    lines = ["原句", original]
    first = data.get("first_attempt")
    first_line = None
    if stage == "direct_explanation":
        _excerpt(messages, data.get("direct_request"), "user")
    if first is not None or stage == "after_translation":
        quote, first_line = _excerpt(messages, first, "user", original=original)
        if source_line is not None and first_line < source_line:
            raise ValidationError("the cited translation predates this source message")
        # A bare acknowledgment cannot stand in for a translation at either stage.
        _validate_followup_text({"followup_status": "prompted"}, quote)
        hint = data.get("first_attempt_hint_dependence", "not_observed")
        labels = {"none": "独立首译", "hint": "提示后的首译", "explanation": "讲解后的首译",
                  "not_observed": "用户首译（提示情况未定）"}
        if hint not in labels:
            raise ValidationError("invalid first-attempt hint dependence")
        lines.extend([labels[hint], quote])
        result.update(first_attempt=quote, first_attempt_hint_dependence=hint, first_attempt_message_id=first["message_id"])
    followup = data.get("followup")
    result["followup_status"] = "not_observed"
    if followup is not None:
        quote, later = _excerpt(messages, followup, "user", original=original)
        status = followup.get("status")
        if first_line is None or later <= first_line or status not in ("prompted", "acknowledgment_only"):
            raise ValidationError("follow-up must be a later learner turn in this unit")
        _validate_followup_text({"followup_status": status}, quote)
        if status == "prompted":
            _, hint_line = _excerpt(messages, followup.get("hint"), "assistant")
            if not first_line < hint_line < later:
                raise ValidationError("the cited hint must precede the revision and follow the first attempt")
        lines.extend(["提示后用户修订" if status == "prompted" else "用户确认（未见复译）", quote])
        result.update(followup_status=status, followup_text=quote, followup_message_id=followup["message_id"])
    else:
        lines.append("尚未观察到用户复译。")

    suggestions = data.get("suggestions")
    if not isinstance(suggestions, list) or not 1 <= len(suggestions) <= 2:
        raise ValidationError("a requested comparison has one or two tutor suggestions")
    choices = []
    for index, suggestion in enumerate(suggestions, 1):
        if not isinstance(suggestion, dict):
            raise ValidationError("invalid tutor suggestion")
        style, relation = suggestion.get("style"), suggestion.get("meaning_relation")
        if style not in ("正式", "简洁", "口语") or relation not in ("preserved", "changed"):
            raise ValidationError("specify the register and the reviewed meaning relation")
        text, basis = suggestion.get("text"), suggestion.get("basis")
        if not all(isinstance(s, str) and s.strip() for s in (text, basis)):
            raise ValidationError("a model rewrite needs text and a concrete semantic comparison")
        if contains_answer_signal([text, basis]):
            raise ValidationError("answer-revealing suggestions need the scoped question view")
        choices.append(dict(selection_id=f"s{index}", actor="assistant", style=style, text=text,
                            meaning_relation=relation, basis=basis))
    rows = [(f"模型改写（{s['style']}）", s["text"],
             ("含义变化：" if s["meaning_relation"] == "changed" else "保留的含义：") + s["basis"]) for s in choices]
    if len(rows) == 2 and not any(re.search(r"[\r\n|]", cell) for row in rows for cell in row):
        lines.append("\n".join(["| 类型 | 表达 | 与原句的关系 |", "|---|---|---|",
                                *["| " + " | ".join(row) + " |" for row in rows]]))
    else:
        lines.extend("\n".join(row) for row in rows)
    direct_message = data["direct_request"]["message_id"] if stage == "direct_explanation" else None
    scene = object_sha256([result, choices, direct_message])
    states = []
    for choice in choices:
        state = dict(source_binding=binding, stage=stage, selection_id=choice["selection_id"],
                     suggestion_version=object_sha256(choice), scene_version=scene, learning_evidence=False)
        states.append({**state, "state_version": object_sha256(state)})
    return {**result, "chat_text": "\n\n".join(lines), "suggestions": choices, "copy_states": states}


def run(args):
    data = json.loads(sys.stdin.read() if args.input_json == "-" else Path(args.input_json).read_text(encoding="utf-8"))
    def messages():
        from .current_unit import locate_rollout, read_messages
        session = args.session_id or os.environ.get("CODEX_THREAD_ID")
        if not session:
            raise ValidationError("original learner quotes require the current session identity")
        return read_messages(args.rollout or locate_rollout(session), session)
    return render_comparison(args.repo_root.resolve(), data, messages)
