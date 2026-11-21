"""Compile trusted English renderers into scope-checked in-page figure assets."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from .answer_safety import contains_answer_signal
from .errors import ValidationError
from . import exam_sense_examples
from .util import atomic_write_bytes, bytes_sha256


SENTENCE_PAYLOAD = re.compile(
    r'<script type="application/json" id="sentence-[0-9a-f]{12}-data">(.*?)</script>', re.S
)


def native_renderer(repo):
    module_path = Path(__file__).resolve().parents[1] / (
        "codex-skill-sources/kaoyan-english-intensive-reading/scripts/render_sentence_diagram.py")
    spec = importlib.util.spec_from_file_location("english_article_figure", module_path)
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    renderer.project_root = lambda: repo
    return renderer


def store_asset(folder, data):
    digest = bytes_sha256(data)
    path = folder / (digest + ".html")
    if not path.exists():
        atomic_write_bytes(path, data)
    return digest


def review_figure(ctx, binding, uid, review):
    renderer = native_renderer(ctx.repo)
    config = dict(source_id=binding["source_id"], question_id=uid, source_hash=binding["source_hash"],
                  unlocked_question=binding["source_id"]+"/"+uid, review=review)
    html = renderer.review_fragment(config, "english-review-"+uid, record_sha256=binding["record_sha256"])
    return store_asset(ctx.root / "figures", html.encode())


def reading_feedback(model, reading, points):
    """Bind a visible correction to a source phrase and the learner's own words."""
    if not isinstance(points, list) or len(points) > 24:
        raise ValidationError("invalid_reading_points")
    nodes = {n["id"]: n for n in model["nodes"]}
    feedback, words = [], []
    for point in points:
        if not isinstance(point, dict) or set(point) != {"node","kind","span","quote","correction"}:
            raise ValidationError("invalid_reading_point")
        if not all(isinstance(v, str) and v.strip() for v in point.values()):
            raise ValidationError("invalid_reading_point")
        node = nodes.get(point["node"])
        if (not node or point["span"] not in model["sentence"] or node["text"] not in point["span"]
                or not reading or point["quote"] not in reading["text"]):
            raise ValidationError("reading_point_evidence_mismatch")
        if (point["kind"] not in {"structure","omission"} or model.get("translation_protected")
                or node.get("translation_protected") or any(p.get("translation_protected") for p in node["parts"])
                or contains_answer_signal(point)):
            raise ValidationError("invalid_reading_point")
        feedback.append({**point, "label": "句子结构" if point["kind"] == "structure" else "漏译成分"})
    for node in model["nodes"]:
        for w in node.get("vocabulary", []):
            if w["evidence_kind"] != "current_translation" or not reading or w["user_translation"] not in reading["text"]:
                continue
            words.append({k:w[k] for k in ("source_form","context_meaning","issue","user_translation")})
            if any(p["node"] == node["id"] and w["source_form"] in p["span"]
                   and w["user_translation"] in p["quote"] for p in points):
                continue
            label = {"untranslated":"漏译限定","uncertain":"未读清","form":"词形"}.get(w["issue"])
            feedback.append({"node": node["id"], "label": label or ("词组" if " " in w["source_form"] else "词义"),
                             "span": w["source_form"], "quote": w["user_translation"],
                             "correction": w["context_meaning"], "cue": w.get("sense_cue","")})
    return feedback, words


def compile_figure(ctx, binding, uid, src, change, *, reading=None, reading_points=None, exam_usages=None):
    kind = change.get("kind", "diagram")
    config = change.get("data", {})
    if not isinstance(config, dict) or contains_answer_signal(config):
        raise ValidationError("invalid_language_figure")
    folder = ctx.root / "figures"
    folder.mkdir(parents=True, exist_ok=True)
    original = src["text"]
    words = []
    with TemporaryDirectory(dir=folder) as temp:
        path = Path(temp) / "figure.html"
        if kind in ("diagram", "word"):
            renderer = native_renderer(ctx.repo)
            context = None
            if kind == "word":
                if any(config.get(k) != value for k,value in
                       {"source_id":binding["source_id"], "unit_id":uid, "source_hash":binding["source_hash"],
                        "record_sha256":binding["record_sha256"]}.items()):
                    raise ValidationError("word_figure_source_mismatch")
                from .word_focus import prepare_focus
                config, context = prepare_focus(ctx.repo, ctx.repo / "intake", config)
            elif change.get("context"):
                from .diagram_context import load_diagram_context
                context = load_diagram_context(ctx.repo, ctx.repo / "intake", Path(change["context"]))
                if context["scope"]["source_id"] != binding["source_id"] or context["scope"]["unit_id"] != uid:
                    raise ValidationError("diagram_context_mismatch")
            if config.get("sentence") != original or contains_answer_signal(config):
                raise ValidationError("figure_source_mismatch")
            model = renderer.prepare_sentence(config, context=context)
            feedback, words = reading_feedback(model, reading, [] if reading_points is None else reading_points)
            if feedback:
                model["reading_feedback"] = feedback
            exam_sense_examples.attach(ctx.repo, model, binding["source_id"], uid, exam_usages,
                                       prepare_example=renderer.prepare_sentence)
            _, html = renderer.render_sentence(model, folder / (uid + ".html"))
            data = html.encode()
        elif kind == "reviewed":
            from .sentence_visuals import export_result, query_sentence
            result = query_sentence(ctx.repo, ctx.repo / "intake", binding["source_id"], uid,
                                    source_hash=binding["source_hash"], sentence_hash=src["sentence_sha256"])
            if result["status"] != "ready" or result["freshness"] != "current" or result["original_sentence"] != original:
                raise ValidationError("reviewed_figure_unavailable")
            export_result(ctx.repo / "intake", result, path, None)
            data = path.read_bytes()
        elif kind == "tense":
            from .teaching_visuals import render
            cases = config["cases"]
            if not cases or any(c["source_id"] != binding["source_id"] or c["unit_id"] != uid or
                                c["sentence"] != original for c in cases):
                raise ValidationError("timeline_source_mismatch")
            result = render(ctx.repo, cases, path, None)
            if result["status"] != "rendered":
                raise ValidationError("timeline_unavailable")
            data = path.read_bytes()
        elif kind == "concordance":
            from .concordance import query
            from .sentence_visuals import render_concordance
            result = query(ctx.repo, ctx.repo / "intake", config["phrase"], current=(binding["source_id"], uid),
                           limit=2)
            if result["status"] != "ready":
                raise ValidationError("concordance_unavailable")
            data = render_concordance(result)
        else:
            raise ValidationError("unknown_figure_kind")
    return store_asset(folder, data), words


def read_figure(root, digest):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValidationError("invalid_figure_identity")
    data = (root / "figures" / (digest + ".html")).read_bytes()
    if bytes_sha256(data) != digest:
        raise ValidationError("figure_changed")
    return data


def refresh_sentence_figure(ctx, section, src, change, source_id):
    """Re-render the current block's verified language data without changing the lesson."""
    block = next((b for b in section["blocks"] if b["id"] == change.get("block_id")), None)
    if not block or not block.get("figure") or block["role"] not in {"figure","vocabulary","question_language"}:
        raise ValidationError("current_sentence_figure_required")
    old = read_figure(ctx.root, block["figure"]).decode()
    models = SENTENCE_PAYLOAD.findall(old)
    if len(models) != 1:
        raise ValidationError("current_sentence_figure_required")
    model = json.loads(models[0])
    tab = block.get("tab")
    original = src["tabs"][tab] if tab else src["text"]
    phrase = model.get("sentence")
    if (model.get("schema") != "english-sentence-diagram-v1" or not phrase
            or (phrase != original and not (block["role"] == "vocabulary" and phrase in original))):
        raise ValidationError("figure_source_mismatch")
    folder = ctx.root / "figures"
    unit = change["unit"] + ("-" + tab if tab else "")
    renderer = native_renderer(ctx.repo)
    if change.get("refresh_exam_dictionary") is True or "exam_usages" in change:
        exam_sense_examples.attach(ctx.repo, model, source_id, unit, change.get("exam_usages"),
                                   prepare_example=renderer.prepare_sentence)
    _, html = renderer.render_sentence(model, folder / (unit + ".html"))
    block["figure"] = store_asset(folder, html.encode())


def article_inline(repo, root, receipt, directory, *, unit=None, tab=None, block_id=None, article_url=None):
    from .article_workspace import project
    from .lesson_display import inline_revision

    page = project(repo, root, receipt["entity_key"])
    if page["content_hash"] != receipt["content_hash"]:
        raise ValidationError("inline_revision_changed")
    uid = unit or page["active_unit_id"]
    section = next((s for s in page["sections"] if s["id"] == uid), None)
    if not section:
        raise ValidationError("inline_unit_outside_scope")
    figures = [b for b in section["blocks"] if b.get("figure")]
    if "tabs" in section:
        # This is the language view. Answer and review blocks have separate grants.
        per_tab = {b["tab"]: b for b in figures if b["role"] == "question_language"}
        if not per_tab:
            raise ValidationError("inline_language_figure_unavailable")
        active = tab or section.get("active_tab") or ("STEM" if "STEM" in per_tab else next(iter(per_tab)))
        if active not in per_tab:
            raise ValidationError("inline_language_figure_unavailable")
        if block_id:
            selected = next((b for b in figures if b["id"] == active + "-" + block_id
                             and b["role"] == "question_language"), None)
            if selected is None:
                raise ValidationError("inline_language_figure_unavailable")
            per_tab[active] = selected
        panels = [(key, "题干" if key == "STEM" else key,
                   read_figure(root, per_tab[key]["figure"]).decode() if key in per_tab else "<p>本项尚未讲解。</p>")
                  for key in section["tabs"]]
        _, fragment = native_renderer(repo).tabbed_fragments(panels, active, "question-" + page["content_hash"][:16])
        data = fragment.encode()
    else:
        selected = next((b for b in reversed(figures) if not block_id or b["id"] == block_id), None)
        if not selected:
            raise ValidationError("inline_sentence_figure_unavailable")
        data = read_figure(root, selected["figure"])
        models = SENTENCE_PAYLOAD.findall(data.decode())
        if len(models) == 1:
            model = json.loads(models[0])
            _, fragment = native_renderer(repo).render_sentence_relations(
                model, directory / (uid + "-relations.html"), article_url=article_url
            )
            return inline_revision(directory, fragment.encode(), title=uid + " · 本句关系图")
        # Reviewed sentence and tense assets on existing pages use the native document wrapper.
        if data.lstrip().lower().startswith(b"<!doctype html>"):
            _, body, tail = data.partition(b"<body>")
            content, end, _ = tail.rpartition(b"</body>")
            if not body or not end:
                raise ValidationError("inline_document_body_unavailable")
            data = content
    return inline_revision(directory, data, title=uid + " · 双语讲解")
