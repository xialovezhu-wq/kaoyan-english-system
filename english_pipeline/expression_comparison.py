"""Read-only display of a learner's translation alongside tutor-authored alternatives."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .answer_safety import contains_answer_signal
from .constants import REPO_ROOT
from .errors import SourceHashMismatch, ValidationError
from .expression_receipts import Conversation, Refusal, current_display, event_ref, save_display
from .reading_preparation import DEFAULT_OUTPUT, query
from .util import canonical_bytes, learner_message_spans, object_sha256, text_sha256


STAGES = ("before_translation", "after_translation", "direct_explanation")
_FENCED_JSON = re.compile(r"^```(?:json)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*$", re.M | re.S)


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate selection field")
    return result


def _copied_object(text):
    """One bare object or JSON fence, optionally introduced by an explicit choice."""
    text = text.strip()
    if text.startswith("我选"):
        text = re.sub(r"\A我(?:选择|选)(?:这版|这一版|这个|这条)[：:。]?[ \t]*\r?\n", "", text, count=1).lstrip()
    if not text.startswith(("{", "```")):
        return None
    fence = _FENCED_JSON.fullmatch(text)
    try:
        value = json.loads(fence[1] if fence else text, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _excerpt(messages, ref, role, *, original=""):
    if not isinstance(ref, dict) or not isinstance(ref.get("quote"), str) or not ref["quote"].strip():
        raise ValidationError("supply an exact nonempty quote and message ID")
    rows = [m for m in messages if m["message_id"] == ref.get("message_id") and m["role"] == role]
    if len(rows) != 1:
        raise ValidationError("the cited message and speaker do not match the conversation")
    row, quote = rows[0], ref["quote"]
    if role == "user":
        copied = _copied_object(row["content"])
        if copied and {"selection_id", "scene_version", "state_version"} <= copied.keys():
            raise ValidationError("a copied expression state cannot supply a learner translation or authorization")
    spans = learner_message_spans(row, original) if original and role == "user" else (row["content"],)
    if not any(quote in span for span in spans):
        raise ValidationError("the quote differs from the cited speaker's own words")
    return quote, row["metadata"]["rollout_line"]


def _read_source(repo, source, read_messages, *, prepared_only=False):
    sid, unit = source.get("source_id"), source.get("unit_id")
    if ("question_id" in source or not isinstance(sid, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", sid)
            or not isinstance(unit, str) or not re.fullmatch(r"S[0-9]+", unit)):
        raise ValidationError("use the authorized article sentence; question panels have their own scope")
    source_line = None
    messages = None
    kind = source.get("source_kind", "article")
    if kind == "user_provided":
        messages = read_messages()
        original, source_line = _excerpt(messages, source.get("original_message"), "user")
        if contains_answer_signal(original):
            raise ValidationError("answer-bearing material belongs in the scoped question view")
        version = text_sha256(original)
    elif kind == "article" and "original_message" not in source:
        prepared = repo / DEFAULT_OUTPUT
        direct_lookup = prepared_only and not (prepared / "index.json").is_file()
        if direct_lookup and not (prepared / "articles" / f"{sid}.json").is_file():
            raise FileNotFoundError("the current prepared sentence cannot be located")
        record = query(prepared, repo_root=repo, source_id=sid, unit_id=unit)
        if direct_lookup:
            from .sources import validate_binding
            if validate_binding(repo, record)["canonical_path"] != record["canonical_path"]:
                raise SourceHashMismatch("the prepared sentence location changed")
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
    return binding, original, source_line, messages


def render_comparison(repo, data, read_messages, *, display_id=None):
    """Only open conversation evidence when the requested stage actually needs it."""
    if not isinstance(data, dict) or not isinstance(data.get("source"), dict):
        raise ValidationError("a comparison needs a source and stage")
    source, stage = data["source"], data.get("stage")
    if stage not in STAGES:
        raise ValidationError("unknown translation stage")
    if "question_id" in data:
        raise ValidationError("use the authorized article sentence; question panels have their own scope")
    binding, original, source_line, messages = _read_source(repo, source, read_messages, prepared_only=display_id is not None)
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
    scene_inputs = [result, choices, direct_message]
    if display_id is not None:
        scene_inputs.append(display_id)
    scene = object_sha256(scene_inputs)
    states = []
    for choice in choices:
        state = dict(source_binding=binding, stage=stage, selection_id=choice["selection_id"],
                     suggestion_version=object_sha256(choice), scene_version=scene, learning_evidence=False)
        states.append({**state, "state_version": object_sha256(state)})
    if display_id is not None:
        lines.append("要继续讲某一版，请把对应的一段 JSON 复制到下一条消息。")
        for choice, state in zip(choices, states):
            encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            lines.append(f"{choice['style']}（{choice['selection_id']}）\n```json\n{encoded}\n```")
    return {**result, "chat_text": "\n\n".join(lines), "suggestions": choices, "copy_states": states}


def _check_current_unit(context, receipt, messages):
    if not isinstance(context, dict):
        raise Refusal("unavailable", "CURRENT_CONTEXT_UNAVAILABLE")
    stage, kind = context.get("stage"), context.get("source_kind")
    if (stage not in STAGES or kind not in ("article", "user_provided")
            or not all(isinstance(context.get(k), str) and context[k] for k in ("source_id", "unit_id"))):
        raise Refusal("unavailable", "CURRENT_CONTEXT_UNAVAILABLE")
    if stage != receipt["stage"]:
        raise Refusal("stale", "STAGE_CHANGED")
    if stage == "before_translation":
        raise Refusal("unavailable", "STAGE_NOT_SELECTABLE")
    saved = receipt["source_binding"]
    if "question_id" in context or kind != saved["source_kind"] or context["unit_id"] != saved["unit_id"]:
        raise Refusal("stale", "SOURCE_CHANGED")
    source = {key: context[key] for key in ("source_kind", "source_id", "unit_id")}
    if kind == "user_provided":
        if not context.get("original_message_id") or not context.get("sentence_sha256"):
            raise Refusal("unavailable", "CURRENT_CONTEXT_UNAVAILABLE")
        original = receipt["original_message"]
        if context["original_message_id"] != original["message_id"]:
            raise Refusal("stale", "SOURCE_CHANGED")
        source["original_message"] = original
    try:
        binding, original_text, _, _ = _read_source(REPO_ROOT, source, lambda: messages, prepared_only=True)
    except (ValidationError, SourceHashMismatch) as exc:
        raise Refusal("stale", "SOURCE_CHANGED") from exc
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise Refusal("unavailable", "CURRENT_SOURCE_UNAVAILABLE") from exc
    pins = ("source_hash", "sentence_sha256", "record_sha256")
    if (binding != saved or original_text != receipt["comparison"]["original"]
            or any(context.get(k) is not None and context[k] != binding.get(k) for k in pins)):
        raise Refusal("stale", "SOURCE_CHANGED")
    try:
        _excerpt(messages, receipt["stage_message"], "user",
                 original=original_text if stage == "after_translation" else "")
    except ValidationError as exc:
        raise Refusal("unavailable", "STAGE_MESSAGE_UNAVAILABLE") from exc


def _match_choice(receipt, submitted):
    if submitted.get("learning_evidence") is not False:
        raise Refusal("mismatch", "STATE_MISMATCH")
    if "scene_version" in submitted and submitted["scene_version"] != receipt["scene_version"]:
        raise Refusal("stale", "SCENE_REPLACED")
    comparison = receipt["comparison"]
    encoded = canonical_bytes(submitted)
    matches = [s for s in comparison["copy_states"] if canonical_bytes(s) == encoded]
    if len(matches) != 1:
        raise Refusal("mismatch", "STATE_MISMATCH")
    state = matches[0]
    choices = [s for s in comparison["suggestions"] if s["selection_id"] == state["selection_id"]]
    if (state["source_binding"] != receipt["source_binding"] or state["stage"] != receipt["stage"]
            or state["state_version"] != object_sha256({k: v for k, v in state.items() if k != "state_version"})
            or len(choices) != 1 or choices[0]["actor"] != "assistant"
            or object_sha256(choices[0]) != state["suggestion_version"]):
        raise Refusal("unavailable", "TRUSTED_RECEIPT_UNAVAILABLE")
    return choices[0], state


def _selection_result():
    return dict(source_binding=None, stage=None, scene_version=None, state_version=None,
                selection_event_ref=None, actor="assistant", learning_evidence=False,
                formal_write_count=0, learning_event_write_count=0, invalidate_prior_selection=True)


def resolve_selection(*, current_context, scene_ref=None, selection_message_id=None,
                      submitted_selection=None, session_id=None):
    """Resolve the current learner paste against the live English unit; never write on read."""
    result = _selection_result()
    try:
        with current_display(session_id, scene_ref) as receipt:
            session = receipt["session_id"]
            conversation = Conversation(session)
            user = conversation.current_user(selection_message_id)
            submitted = _copied_object(user["content"])
            if (not submitted or "selection_id" not in submitted
                    or any(block.get("type") not in {"input_text", "text"}
                           for block in user["metadata"]["original_content_blocks"])):
                raise Refusal("mismatch", "SELECTION_MESSAGE_MISMATCH")
            if submitted_selection is not None:
                supplied = submitted_selection
                if isinstance(supplied, str):
                    supplied = _copied_object(supplied)
                if not isinstance(supplied, dict) or canonical_bytes(supplied) != canonical_bytes(submitted):
                    raise Refusal("mismatch", "SELECTION_MESSAGE_MISMATCH")
            result["selection_event_ref"] = {"session_id": session, **event_ref(user)}
            display = conversation.preceding_display(receipt)
            _check_current_unit(current_context, receipt, conversation.messages)
            result.update(source_binding=receipt["source_binding"], stage=receipt["stage"],
                          scene_version=receipt["scene_version"])
            choice, state = _match_choice(receipt, submitted)
            # Verify the real, subsequent assistant delivery as well as the private file.
            displayed = (_copied_object(block[1]) for block in _FENCED_JSON.finditer(display))
            state_bytes = canonical_bytes(state)
            if (not all(part in display for part in (receipt["comparison"]["original"], choice["text"], choice["basis"]))
                    or not any(item is not None and canonical_bytes(item) == state_bytes for item in displayed)):
                raise Refusal("unavailable", "DISPLAY_MESSAGE_MISMATCH")
            selected = {k: choice[k] for k in ("selection_id", "text", "style", "meaning_relation", "basis")}
            return {**result, "status": "accepted", "reason_code": "SELECTION_ACCEPTED",
                    "state_version": state["state_version"], "selected_suggestion": selected,
                    "invalidate_prior_selection": False}
    except Refusal as exc:
        return {**result, "status": exc.status, "reason_code": exc.reason_code}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        # Unreadable or malformed local state never releases suggestion text.
        return {**result, "status": "unavailable", "reason_code": "TRUSTED_RECEIPT_UNAVAILABLE"}


def run_selection(args):
    if args.selection_json not in (None, "-"):
        return {**_selection_result(), "status": "mismatch", "reason_code": "SELECTION_INPUT_UNSUPPORTED"}
    context = dict(stage=args.current_stage, source_kind=args.current_source_kind, source_id=args.current_source_id,
                   unit_id=args.current_unit_id, original_message_id=args.current_original_message_id,
                   sentence_sha256=args.current_sentence_sha256, source_hash=args.current_source_hash,
                   record_sha256=args.current_record_sha256)
    try:
        supplied = sys.stdin.read() if args.selection_json == "-" else None
    except (OSError, UnicodeError):
        return {**_selection_result(), "status": "unavailable", "reason_code": "SELECTION_INPUT_UNAVAILABLE"}
    return resolve_selection(current_context=context, scene_ref=args.scene_ref, session_id=args.session_id,
                             selection_message_id=args.selection_message_id, submitted_selection=supplied)


def run(args):
    if getattr(args, "save_display_receipt", False):
        return save_display(args)
    data = json.loads(sys.stdin.read() if args.input_json == "-" else Path(args.input_json).read_text(encoding="utf-8"))
    def messages():
        from .current_unit import locate_rollout, read_messages
        session = args.session_id or os.environ.get("CODEX_THREAD_ID")
        if not session:
            raise ValidationError("original learner quotes require the current session identity")
        return read_messages(args.rollout or locate_rollout(session), session)
    return render_comparison(args.repo_root.resolve(), data, messages)
