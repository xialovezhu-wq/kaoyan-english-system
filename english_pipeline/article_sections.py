"""Compile one authorized block from the foreground lesson and native evidence."""
from __future__ import annotations

import re
from uuid import uuid4

from .answer_safety import contains_answer_signal
from .article_source import offered_article
from .errors import ValidationError
from .expression_comparison import _excerpt, render_comparison
from .personal_summary import _validate_followup_text
from .reading_preparation import DEFAULT_OUTPUT, query_review
from .util import object_sha256

USER_ROLES = {"first_attempt", "transcript_note", "revision", "acknowledgment", "direct_request",
              "question_attempt", "question_request"}
TEACHING = {"explanation", "translation", "hint", "vocabulary", "structure", "comparison", "figure"}
LABELS = {"first_attempt":"用户首译", "transcript_note":"语音转录文本，未核对音频",
          "revision":"用户实际修订", "acknowledgment":"用户确认", "hint":"提示",
          "explanation":"本句讲解", "translation":"参考译文", "vocabulary":"词义与搭配",
          "structure":"句子结构", "comparison":"模型建议", "figure":"句图",
          "review_figure":"题目证据图", "question_attempt":"用户首答", "question_language":"语言讲解", "answer":"答案反馈"}


def plain(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 24000:
        raise ValidationError("nonempty_bounded_text_required")
    if contains_answer_signal(value):
        raise ValidationError("question_answer_outside_review")
    return value


def new_section():
    return dict(stage="before_translation", blocks=[], grant=None)


def first(sec, role):
    return next((b for b in sec["blocks"] if b["role"] == role), None)


def evidence(convo, change, original, *, min_line):
    ref = change.get("evidence", {})
    quote, line = _excerpt(convo.messages, ref, "user", original=original)
    current = convo.current_user()
    if not min_line <= line <= current["metadata"]["rollout_line"]:
        raise ValidationError("evidence_outside_attempt")
    return dict(text=quote, message_id=ref["message_id"], line=line, actor="user")


def question_language_request(ctx, change, original, min_line, tab):
    request = evidence(ctx, change, original, min_line=min_line)
    current = ctx.current_user()
    if request["message_id"] != current["message_id"]:
        raise ValidationError("current_language_request_required")
    full = current["content"]
    quote = request["text"]
    only_record = re.search(r"(?:只|仅)(?:需|需要|要|用|帮我)?(?:先)?(?:记录|记下|记一下|保存)", full)
    asks_separately = re.search(r"(?:请|帮我|只讲|我想知道).{0,8}(?:翻译|词义|怎么译|意思|讲解|解释)", quote)
    holds_language = re.search(r"(?:不要|不用|别|先不)(?:给我)?(?:翻译|讲解|解释|纠正)"
                               r"(?!\s*(?:这(?:道)?题的?)?(?:正确|标准)?答案)", full)
    if holds_language or (only_record and not asks_separately):
        raise ValidationError("language_feedback_withheld")
    explicit = re.search(r"翻译|解释|意思|词义|语言|结构|讲解|怎么(?:看懂|读|译|翻|理解)|讲.{0,5}(?:句|词|选项)", quote)
    states_choice = re.search(r"(?:我选(?:择)?|答案是|应该选)\s*[A-D]", full, re.I)
    doubt = (re.search(r"(?:没|不)(?:太|大|怎么)?(?:读懂|看懂|理解|翻(?:译)?出来)", quote)
             or (not states_choice and re.search(r"是.{1,45}(?:对吧|吗|么)[？?]?", quote)))
    paraphrase = tab != "STEM" and re.search(re.escape(tab) + r"\s*(?:选项|项)\s*(?:是说|说的是|是指)", quote, re.I)
    if tab in {"A", "B", "C", "D"} and not states_choice:
        # A separately labelled Chinese gloss is a language attempt, even without “翻译” in the message.
        marker = rf"(?:\[{tab}\]|{tab}\s*(?:选项|项|[\\]?[.．、:：)）]))"
        labelled = re.fullmatch(rf"{marker}[ \t]*[“「『（(]*([\u3400-\u9fff][^\n]*)", quote, re.I)
        if labelled and not re.fullmatch(
            r"(?:对|错|正确|错误|有误|不选|排除|(?:我)?(?:就|不)?选(?:这项|这个))[”」』）)\s。.!！?？\\]*",
            labelled[1],
        ):
            paraphrase = True
    if not (explicit or doubt or (paraphrase and not states_choice)):
        raise ValidationError("language_request_required")
    return request


def add_figure(ctx, state, section, change, block, *, src, config, reading):
    from .article_figures import compile_figure
    unit = change["unit"] + ("-" + block["tab"] if "tab" in block else "")
    if "reading_evidence" in change:
        reading = evidence(ctx, {"evidence": change["reading_evidence"]}, src["text"],
                           min_line=max(state["start_line"], section.get("since_line", 0)))
    block["figure"], words = compile_figure(ctx, state["binding"], unit, src, config, reading=reading,
                                            reading_points=change.get("reading_points", []),
                                            exam_usages=change.get("exam_usages"))
    if words:
        # New diagrams can refine a phrase without replacing other observations in this unit.
        saved = {(w.get("tab"), w["source_form"]): w for w in section.get("words", [])}
        for word in words:
            saved[block.get("tab"), word["source_form"]] = {
                **word, "tab": block.get("tab"), "message_id": reading["message_id"]}
        section["words"] = list(saved.values())


def explicit_question(convo, ref, scope, unit, permission, state):
    """Interpret a whole current request, after checking the article/question context.
    The tutor determines intended scope. An ambiguous or negative request never
    becomes a full-answer grant on the display channel.
    """
    row = convo.current_user(ref["message_id"])
    body = row["content"].strip()
    if ref["text"] != body or unit != state["active_unit_id"]:
        raise ValidationError("exact_current_question_request_required")
    body = body.rstrip("。？！?! .").strip()
    key = scope["source_id"]
    explicit = key in body and re.search(rf"(?<!\w){unit}(?!\d)", body, re.I)
    if not explicit:
        # Taking the same question number to another article cannot reuse this request.
        previous = next((r for r in reversed(convo.messages)
                         if r["metadata"]["rollout_line"] < ref["line"] and r["role"] == "assistant"), {})
        text = previous.get("content", "")
        if (not offered_article(text,convo.root,key)
                or not re.search(r"(?<![A-Za-z0-9])" + unit + r"(?!\d)", text)):
            raise ValidationError("article_and_question_context_required")
    text = body.replace(key, "").strip(" /，,：:")
    number = unit[1:]
    if any(q != number for q in re.findall(r"(?:\bQ\s*|第\s*)(\d+)", text, re.I)):
        raise ValidationError("different_question_named")
    # Normalize only the addressed question and a polite prefix.
    text = re.sub(rf"^(?:请问|请)?\s*(?:{unit}|第\s*{number}\s*题|这道题|这一题|这题)?[ ，,：:/]*",
                  "", text, count=1, flags=re.I)
    patterns = {
        "answer": r"(?:请)?(?:正确答案是什么|[选答]什么|选哪[一个项]+|答案是什么|(?:告诉我|给我|公布|揭晓|看)[一下]*(?:这题的?)?(?:正确)?答案)",
        "review": r"(?:请)?(?:(?:给我|讲解|公布|给出|看一下)?(?:正确)?答案[和与及、](?:解析|解释)|(?:告诉我)?选什么[，, ]*(?:为什么|并解释原因))",
        "verdict": r"(?:请)?(?:只)?(?:告诉我)?(?:我选[的得]?|我的选择|我选\s*[A-D])?(?:对不对|选对了吗|正确吗|对吗)(?:[，, ]*(?:不(?:要|用)(?:告诉我|公布|说)?(?:正确)?答案))?"
    }
    if permission not in patterns or not re.fullmatch(patterns[permission], text, re.I):
        raise ValidationError("question_permission_not_confirmed")


def prepare_block(ctx, state, section, change, article):
    binding = article["binding"]
    if not isinstance(change, dict):
        raise ValidationError("invalid_section_patch")
    unit, role = change["unit"], change["role"]
    src = article["sections"][unit]
    question = "tabs" in src
    if role == "protect":
        section.clear()
        section.update(new_section())
        section["since_line"] = ctx.current_user()["metadata"]["rollout_line"]
        state["access_epoch"] = uuid4().hex
        return
    if role == "focus":
        return
    if role == "refresh_figure":
        from .article_figures import refresh_sentence_figure
        refresh_sentence_figure(ctx, section, src, change, binding["source_id"])
        return
    first_try = first(section, "first_attempt")
    original = src.get("text", "")
    block = dict(role=role, actor="assistant", turn=ctx.turn_id)
    if role in USER_ROLES:
        block.update(evidence(ctx, change, original,
                              min_line=max(state["start_line"], section.get("since_line", 0))))
    if question:
        if role == "question_attempt":
            if first(section, role):
                raise ValidationError("first_answer_is_immutable")
            # Only a verified stated choice can be used for verdict-only feedback.
            choice = re.fullmatch(r"(?:第?\d+题[，,：: ]*)?(?:(?:我选(?:择)?|我的答案[是为：:]?)\s*)?([A-D])[。.!！]?", block["text"], re.I)
            block["choice"] = choice[1].upper() if choice else None
        elif role == "question_request":
            if change.get("unlocked_question") != binding["source_id"] + "/" + unit:
                raise ValidationError("exact_question_binding_required")
            permission = change.get("permission")
            explicit_question(ctx, block, binding, unit, permission, state)
            result = query_review(ctx.repo / DEFAULT_OUTPUT, repo_root=ctx.repo, source_id=binding["source_id"],
                                  question_id=unit, unlocked_question=change["unlocked_question"],
                                  source_hash=binding["source_hash"], record_sha256=binding["record_sha256"])
            answer = result["correct_answer"]
            if permission == "verdict":
                attempted = first(section, "question_attempt") or {}
                if not attempted.get("choice"):
                    raise ValidationError("verified_choice_required")
                answer = "你的选择正确。" if attempted["choice"] == answer else "你的选择有误。"
            elif permission == "review":
                answer += "\n" + result["analysis_text"]
            section["grant"] = {"source_binding": binding, "unit": unit, "permission": permission,
                                "message_id": block["message_id"], "analysis_hash": result["analysis_record_sha256"]}
            state["access_epoch"] = uuid4().hex
            section["blocks"] = [b for b in section["blocks"] if b["role"] != "answer"]
            block = dict(role="answer", actor="source", turn=ctx.turn_id, text=answer, grant=object_sha256(section["grant"]))
        elif role == "review_figure":
            grant = section.get("grant") or {}
            if grant.get("permission") != "review" or grant.get("source_binding") != binding or grant.get("unit") != unit:
                raise ValidationError("exact_review_grant_required")
            from .article_figures import review_figure
            block.update(figure=review_figure(ctx, binding, unit, change["review"]), grant=object_sha256(grant))
        elif role == "question_language":
            tab = change.get("tab")
            if tab not in src["tabs"]:
                raise ValidationError("exact_question_tab_required")
            request = question_language_request(ctx, change, src["tabs"][tab],
                        max(state["start_line"], section.get("since_line", 0)), tab)
            block.update(tab=tab, text=plain(change.get("text")), request=request["message_id"])
            if "diagram" in change:
                figure_input = {"kind":"diagram","data":change["diagram"]}
                if "context" in change:
                    figure_input["context"] = change["context"]
                add_figure(ctx, state, section, change, block, src={"text": src["tabs"][tab]},
                           config=figure_input, reading=request)
        else:
            raise ValidationError("question_role_requires_exact_scope")
    elif role == "first_attempt":
        if first_try:
            raise ValidationError("first_translation_is_immutable")
        attempted = re.sub(r"^(?:第[一二三四五六七八九十0-9]+句|这句|这一句)[，,：: ]*", "", block["text"])
        _validate_followup_text({"followup_status":"prompted"}, attempted)
        dependence = change.get("hint_dependence", "not_observed")
        if dependence not in ("none", "hint", "explanation", "not_observed"):
            raise ValidationError("invalid_hint_dependence")
        if change.get("transcribed") is True:
            block["transcribed"] = True
        block["hint_dependence"] = dependence
        section["stage"] = "after_translation"
    elif role == "direct_request":
        if block["message_id"] != ctx.current_user()["message_id"]:
            raise ValidationError("current_direct_request_required")
        extent = change.get("extent", "point")
        request = ctx.current_user()["content"]
        quote = block["text"]
        words = set(re.findall(r"[a-z]+", original.lower()))
        named_point = (extent == "point" and words.intersection(re.findall(r"[a-z]+", quote.lower()))
                       and re.search(r"没读懂|看不懂|什么意思|是不是|是.{1,45}(?:对吧|吗)", quote))
        if (not (named_point or re.search(r"翻译|译文|怎么译|怎么翻|讲[一解讲下]|解[释析]|意思|词义|提示|结构|时态|比较|怎么理解|translate|explain|mean", quote, re.I))
                or (extent == "sentence" and re.search(r"别翻译|不(?:要|用|必)(?:给|说|讲|翻|译)|先让我|先自己|只(?:看|问|讲|解释).{0,8}(?:词|短语|时态)", request))):
            raise ValidationError("explicit_language_request_required")
        if extent not in {"point", "sentence"}:
            raise ValidationError("invalid_request_extent")
        if not first_try:
            section["stage"] = "direct_explanation"
            section["extent"] = extent
        section["direct_request"] = block
        return
    elif role in {"acknowledgment", "transcript_note", "revision"}:
        if not first_try or block["line"] < first_try["line"]:
            raise ValidationError("first_translation_required")
        if role != "transcript_note" and block["line"] <= first_try["line"]:
            raise ValidationError("later_user_message_required")
        if role == "revision":
            _validate_followup_text({"followup_status":"prompted"}, block["text"])
            if change.get("hint"):
                _, hint_line = _excerpt(ctx.messages, change["hint"], "assistant")
                if not first_try["line"] < hint_line < block["line"]:
                    raise ValidationError("invalid_hint_chronology")
                block["hint_message_id"] = change["hint"]["message_id"]
            block["reason"] = change.get("reason", "not_observed")
            if block["reason"] not in {"transcription_correction", "prompted", "not_observed"} or (
                    block["reason"] == "prompted" and "hint_message_id" not in block):
                raise ValidationError("invalid_revision_basis")
    elif role in TEACHING:
        if section["stage"] == "before_translation":
            raise ValidationError("sentence_is_protected")
        if (section["stage"] == "direct_explanation" and section.get("extent") == "point"
                and role in {"translation", "comparison", "figure"}):
            raise ValidationError("only_requested_language_point_is_open")
        if role == "comparison":
            source = {**binding, "unit_id": unit}
            if binding["source_id"].startswith("TEMP-"):
                from .util import load_json, text_sha256
                doc = load_json(ctx.root / "materials" / (binding["source_id"] + ".json"))
                source = dict(source_kind="user_provided", source_id=binding["source_id"], unit_id=unit,
                              source_hash=text_sha256(original),
                              original_message=dict(message_id=doc["origin"]["message_id"], quote=original))
            data = dict(source=source, stage=section["stage"], suggestions=change["suggestions"])
            witness = first_try or section["direct_request"]
            data["first_attempt" if first_try else "direct_request"] = dict(
                message_id=witness["message_id"], quote=witness["text"])
            reviewed = render_comparison(ctx.repo, data, lambda: ctx.messages)
            block["suggestions"] = reviewed["suggestions"]
        elif role == "figure":
            add_figure(ctx, state, section, change, block, src=src, config=change, reading=first_try)
        elif role == "vocabulary" and "diagram" in change:
            # A word-only request can display the requested phrase without an entire sentence translation.
            diagram = change["diagram"]
            phrase = diagram.get("sentence") if isinstance(diagram, dict) else None
            if not isinstance(phrase, str) or not phrase.strip() or phrase not in original or phrase == original:
                raise ValidationError("exact_language_point_required")
            block["text"] = plain(change.get("text"))
            add_figure(ctx, state, section, change, block, src={"text": phrase},
                       config={"kind":"diagram","data":diagram},
                       reading=first_try or section.get("direct_request"))
        else:
            block["text"] = plain(change.get("text"))
    else:
        raise ValidationError("unknown_sentence_role")

    if role in USER_ROLES and role != "question_request" and not (role == "question_attempt" and block.get("choice")):
        plain(block["text"])
    block_id = change.get("block_id", role)
    if not isinstance(block_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", block_id):
        raise ValidationError("invalid_block_id")
    if block["actor"] == "user":
        block_id = role + "-" + object_sha256([block["message_id"], block["text"]])[:16]
    if role == "question_language":
        block_id = block["tab"] + "-" + block_id
    block["id"] = block_id
    for i, before in enumerate(section["blocks"]):
        if before["id"] == block_id:
            if before["role"] != block["role"] or (before["actor"] == "user" and block != before):
                raise ValidationError("block_identity_conflict")
            section["blocks"][i] = block
            break
    else:
        section["blocks"].append(block)
