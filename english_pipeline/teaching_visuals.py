"""Render only explicitly annotated English tense relationships."""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from .answer_safety import contains_answer_signal
from .errors import PipelineError, ValidationError
from .reading_preparation import DEFAULT_OUTPUT, query
from .sources import resolve_article_source
from .util import atomic_write_bytes, bytes_sha256, object_sha256, sentence_sha256

TEMPLATE_VERSION = "english-tense-timeline-v1"
INPUT_SCHEMA = "english_tense_timeline_input_v1"
RECEIPT_SCHEMA = "english_tense_timeline_receipt_v1"
TEMPLATE = Path("assets/english/tense_timeline.html")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
MODES = {
    "result": ("completed", "before_now_to_completed_before_now",
               "写作等有结果动作在此前达到完成点，结果与现在相关。",
               "这里比较的是动作本身的完成。写完邮件只说明形成了写作结果，发送属于另一项动作。"),
    "process": ("unknown_without_context", "before_now_to_near_now",
                "此前的过程延续至说话时刻附近，终点由语境确定。",
                "本句突出持续过程。动作是否继续，成果是否已经完成，都需要更多语境。"),
}
STATE_FALLBACK = "这里可以表达居住从过去持续到现在，后续是否继续由语境决定。"

def example_cases() -> list[dict[str, Any]]:
    out = []
    for unit, text, phrase, mode in (
        ("WRITTEN", "I have written the email.", "have written", "result"),
        ("WRITING", "I have been writing the email.", "have been writing", "process"),
    ):
        start = text.index(phrase)
        digest = sentence_sha256(text)
        out.append({"source_kind": "synthetic", "source_id": "SYNTHETIC-TENSE", "unit_id": unit,
                    "sentence": text, "sentence_sha256": digest, "source_hash": digest, "context": "",
                    "span": {"start": start, "end": start + len(phrase), "text": phrase},
                    "speech_time": "now", "reference_time": "now", "event_interval": MODES[mode][1],
                    "focus": mode, "endpoint_status": MODES[mode][0], "visibility": "answer_free"})
    return out


def _syntax_span(case: dict) -> str:
    text, span = case["sentence"], case["span"]
    if not isinstance(span, dict) or set(span) != {"start", "end", "text"}:
        raise ValidationError("invalid_span")
    start, end = span["start"], span["end"]
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or text[start:end] != span["text"]):
        raise ValidationError("span_not_in_current_sentence")
    return span["text"]


def _scope(repo: Path, case: dict) -> dict:
    source_id, unit_id = case["source_id"], case["unit_id"]
    kind, text, context = case["source_kind"], case["sentence"], case.get("context", "")
    if (not isinstance(source_id, str) or not IDENTIFIER.fullmatch(source_id)
            or not isinstance(unit_id, str) or not IDENTIFIER.fullmatch(unit_id)):
        raise ValidationError("invalid_source_identity")
    if (not isinstance(text, str) or not text.strip() or len(text) > 850
            or not isinstance(context, str) or len(context) > 450 or contains_answer_signal([text, context])):
        raise ValidationError("sentence_or_context_not_available")
    if case["sentence_sha256"] != sentence_sha256(text) or not DIGEST.fullmatch(case["source_hash"]):
        raise ValidationError("source_hash_mismatch")
    if case.get("visibility") != "answer_free":
        raise ValidationError("protected_scope_withheld")
    question_id = case.get("question_id")
    if kind in {"synthetic", "user_provided"}:
        if question_id or re.match(r"^Q\d+(?:\b|[-_])", unit_id, re.I) or case["source_hash"] != sentence_sha256(text):
            raise ValidationError("unverified_current_text_scope")
        if kind == "synthetic" and case not in example_cases():
            raise ValidationError("unregistered_synthetic_example")
    elif kind in {"article", "question", "option"}:
        source = query(repo / DEFAULT_OUTPUT, repo_root=repo, source_id=source_id, unit_id=unit_id)
        current = resolve_article_source(repo, source["source_article"])
        unit = source["unit"]
        if (current["source_hash"] != case["source_hash"] or source["source_hash"] != case["source_hash"]
                or unit["text"] != text or unit["kind"] != kind or unit["answer_exposure"] != "answer_free"
                or unit.get("question_id") != question_id):
            raise ValidationError("current_unit_identity_mismatch")
        source_id = source["source_id"]
        if question_id and context:
            raise ValidationError("question_timeline_uses_only_the_named_language_unit")
        if context:
            record = query(repo / DEFAULT_OUTPUT, repo_root=repo, source_id=source_id)
            paragraphs = (p["text"] for p in record["paragraphs"] if p["paragraph_id"] == unit.get("paragraph_id"))
            if not any(context in passage for passage in paragraphs):
                raise ValidationError("context_outside_current_paragraph")
    else:
        raise ValidationError("unsupported_or_protected_scope")
    return {"source_id": source_id, "unit_id": unit_id, "source_hash": case["source_hash"],
            "sentence_sha256": case["sentence_sha256"], "question_id": question_id,
            "visibility": "answer_free"}


def _explain(case: dict) -> str:
    phrase = _syntax_span(case)
    focus = case["focus"]
    if focus == "continuing_state_possible":
        return STATE_FALLBACK
    if focus not in MODES or (case["speech_time"], case["reference_time"]) != ("now", "now"):
        return "现有资料不足以标定参照时刻或动作终点。"
    end, interval, _, _ = MODES[focus]
    if (case.get("endpoint_status"), case.get("event_interval")) != (end, interval):
        return "现有语境尚未支持这个动作终点。"
    progressive = re.fullmatch(r"(?:have|has) been [a-z]+ing", phrase, flags=re.I)
    completed = re.fullmatch(r"(?:have|has) (?!been\b|not\b|never\b)[a-z]+", phrase, flags=re.I)
    if not (progressive if focus == "process" else completed):
        return "该跨度与所选对照模式不匹配。"
    # The agent supplies the contextual interpretation. The form alone never
    # selects the mode, and familiar stative/duration phrasing is left in text.
    if focus == "result" and (
        re.search(r"\bfor\s+(?:\d+|[a-z]+)\s+(?:years?|months?|weeks?|days?|hours?)\b", case["sentence"], re.I)
        or re.search(r"\b(?:lived|known|owned|belonged|existed|been)\b", phrase, re.I)
    ):
        return "这个时间表达可以覆盖延续的状态或动作，现有语境尚未确认结束。"
    return ""


def _axis(mode: str) -> str:
    baseline = '<line x1="15" y1="43" x2="263" y2="43" stroke="currentColor"/>'
    now = '<line x1="238" y1="15" x2="238" y2="49" stroke="currentColor" stroke-dasharray="3 3"/>'
    if mode == "result":
        action = ('<path d="M38 24H149" stroke="currentColor" stroke-width="4"/>'
                  '<circle cx="149" cy="24" r="5" fill="currentColor"/>'
                  '<text x="149" y="12" text-anchor="middle">完成点</text>')
    else:
        action = ('<path d="M38 24H192" stroke="currentColor" stroke-width="4"/>'
                  '<path d="M192 24H255" stroke="currentColor" stroke-width="3" stroke-dasharray="5 4"/>'
                  '<text x="189" y="12" text-anchor="middle">终点待定</text>')
    return (f'<svg viewBox="0 0 278 76" role="img" aria-label="{MODES[mode][2]}" '
            f'font-size="11" fill="currentColor">{baseline}{now}{action}'
            '<text x="16" y="61">此前</text><text x="263" y="61" text-anchor="end">现在 · 话语与参照时刻</text></svg>')


def _card(case: dict, scope: dict, index: int) -> str:
    escape = html.escape
    original, span, focus = case["sentence"], case["span"], case["focus"]
    sentence = escape(original[:span["start"]]) + '<mark>' + escape(span["text"]) + '</mark>' + escape(original[span["end"]:])
    _, _, explanation, detail = MODES[focus]
    context = f'<p lang="en">{escape(case["context"])}</p>' if case.get("context") else ''
    locator = escape(scope["source_id"] + " · " + scope["unit_id"])
    return (f'<article data-case="{index}"><p class="sentence" lang="en">{sentence}</p>{_axis(focus)}'
            f'<p>{explanation}</p><details><summary>语义依据</summary><p>{detail}</p>{context}'
            f'<p class="scope">{locator}</p></details></article>')


def render(repo: Path, cases: list[dict], output: Path | None, inline_root: Path | None, *, lesson_page: bool = False) -> dict:
    if lesson_page and inline_root is None:
        raise ValidationError("--lesson-page requires the chat's inline root")
    base = {"schema_version": RECEIPT_SCHEMA, "formal_write_count": 0, "learning_event_write_count": 0}
    if not isinstance(cases, list) or len(cases) not in (1, 2):
        return {**base, "status": "text_fallback", "reason": "one_or_two_explicit_cases_required"}
    try:
        scopes = [_scope(repo, case) for case in cases]
        if len(cases) == 2 and not (
            cases == example_cases() or (all(s["question_id"] is None for s in scopes) and scopes[0] == scopes[1])
        ):
            raise ValidationError("comparison_must_stay_in_one_current_unit")
        notes = [note for case in cases if (note := _explain(case))]
        if notes:
            return {**base, "status": "text_fallback", "reason": "unsupported_or_unconfirmed_interval", "text": notes}
    except (PipelineError, OSError, KeyError, TypeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValidationError) and re.fullmatch("[a-z_]+", str(exc)) else "current_source_unverifiable"
        return {**base, "status": "text_fallback", "reason": reason}
    if (output is None or not output.is_absolute() or output.is_symlink() or output.suffix != ".html"
            or (inline_root is not None and (not inline_root.is_absolute()
                or not output.resolve().is_relative_to(inline_root.resolve())))):
        raise ValidationError("tense timeline needs a safe absolute HTML output")
    template = (repo / TEMPLATE).read_bytes()
    config_sha = object_sha256({"schema_version": INPUT_SCHEMA, "cases": cases})
    ident = "en-tense-" + config_sha[:16]
    controls = ""
    if len(cases) == 2:
        labels = {"result": "完成结果", "process": "持续过程"}
        choices = [f'<button type="button" data-select="{i}" aria-pressed="false">{labels[case["focus"]]}</button>'
                   for i, case in enumerate(cases)]
        controls = ('<div class="view-controls" role="group" aria-label="切换时态图">'
                    '<button type="button" data-select="both" aria-pressed="true">并排对照</button>'
                    + "".join(choices) + '<button type="button" data-reset>重置</button></div>')
    shell = template.decode()
    fragment = (shell.replace("__ID__", ident).replace("__CONTROLS__", controls)
                .replace("__CASES__", "".join(_card(case, scope, i) for i, (case, scope) in enumerate(zip(cases, scopes, strict=True)))))
    if inline_root is None:
        fragment = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<title>完成结果与持续过程</title><body>' + fragment + '</body></html>')
    from .lesson_display import check_lesson_output, display_targets
    check_lesson_output(output)
    raw = fragment.encode()
    atomic_write_bytes(output, raw)
    receipt = {**base, "status": "rendered", "template_version": TEMPLATE_VERSION,
               "template_sha256": bytes_sha256(template), "config_sha256": config_sha,
               "asset_sha256": bytes_sha256(raw), "output_path": str(output.resolve()), "sources": scopes}
    if inline_root is not None:
        receipt.update(display_targets(output, title="完成结果与持续过程",
                                       preview_root=inline_root if lesson_page else None, fragment=raw))
    return receipt
