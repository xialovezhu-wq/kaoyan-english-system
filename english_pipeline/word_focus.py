"""Expand one authorized word focus into the ordinary bilingual sentence renderer."""
import re

from .diagram_context import prepare_diagram_context
from .errors import ValidationError
from .exam_lexicon import contains_form, key
from .reviewed_senses import expand_sense

SCHEMA = "english-word-focus-v1"
RELATIONS = {"对照": "logic", "并列": "logic", "指代": "reference", "修饰": "modifier"}


def prepare_focus(repo, state, data, *, lexicon_db=None):
    fields = {"schema", "source_id", "unit_id", "translation", "stage", "vocabulary",
              "fragment", "fragment_translation", "role", "related", "source_hash", "record_sha256", "comparisons"}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or set(data) - fields:
        raise ValidationError("invalid word-focus input")
    if data.get("stage") not in {"after_translation", "direct_explanation"}:
        raise ValidationError("word-focus requires an authorized bilingual explanation")
    if not isinstance(data.get("source_id"), str) or not re.fullmatch(r"S\d+", str(data.get("unit_id", ""))):
        raise ValidationError("word-focus requires an indexed article sentence")
    if not isinstance(data.get("translation"), str) or not data["translation"].strip():
        raise ValidationError("supply the reviewed translation of the current sentence")
    note = expand_sense(data.get("vocabulary"))
    if not isinstance(note.get("sense_cue"), str) or not note["sense_cue"].strip():
        raise ValidationError("explain which feature of this sentence selects the intended meaning")
    item = key(note.get("item", ""))
    form = note.setdefault("source_form", item)
    point_key = ("phrase:" if " " in item else "word:") + item
    if "issue" not in note and "user_translation" not in note:
        note.setdefault("evidence_kind", "language_reference")
    context = prepare_diagram_context(
        repo, state, None, source_id=data["source_id"], unit_id=data["unit_id"],
        source_hash=data.get("source_hash"), record_sha256=data.get("record_sha256"),
        focus_key=point_key, vocabulary=[{k:note[k] for k in ("item","source_form","frequency_mode") if k in note}],
        lexicon_db=lexicon_db,
    )
    if context["scope"].get("kind") != "article" or context["scope"].get("question_id"):
        raise ValidationError("use the scoped question-panel renderer for question material")
    phrase = data.get("fragment", form)
    if not isinstance(phrase, str) or phrase not in context["sentence"] or not contains_form(phrase, form):
        raise ValidationError("the focused fragment must be an exact part of this sentence")
    gloss = data.get("fragment_translation") if phrase != form else data.get("fragment_translation",note.get("context_meaning"))
    if not isinstance(gloss,str) or not gloss.strip():
        raise ValidationError("supply a Chinese gloss for the whole focused fragment")
    node = {"vocabulary":[note]}
    if "comparisons" in data:
        node["comparisons"] = data["comparisons"]
    for point in (context.get("memory") or {}).get("matched",[]):
        latest = point.get("latest_observation") or {}
        if (point.get("visibility") == "answer_free" and point.get("key") == point_key
                and (point.get("audit") or {}).get("status") == "verified"
                and latest.get("episode_id")):
            node["memory_ref"] = {"key":point_key, "episode_id":latest["episode_id"]}
            break
    row = [["focus", phrase, gloss, data.get("role", "词义与搭配"), node]]
    diagram = {"sentence": context["sentence"], "translation": data["translation"],
               "initial_node": "focus", "rows": [row]}
    if "related" in data:
        related = data["related"]
        if not isinstance(related, dict) or set(related) != {"en", "zh", "relation"}:
            raise ValidationError("a related fragment requires exact bilingual spans and a relationship")
        en, zh, relation = related["en"], related["zh"], related["relation"]
        if not all(isinstance(value, str) and value.strip() for value in (en, zh, relation)):
            raise ValidationError("related fragments and their relationship must contain text")
        if relation not in RELATIONS:
            raise ValidationError("unsupported reading relationship")
        for text, focus, other in ((context["sentence"], phrase, en), (data["translation"], gloss, zh)):
            if text.count(focus) != 1 or text.count(other) != 1:
                raise ValidationError("choose two unambiguous fragments from the current sentence and translation")
            first, second = text.index(focus), text.index(other)
            if first < second + len(other) and second < first + len(focus):
                raise ValidationError("the focused and related fragments overlap")
        row.append(["related", en, zh, relation])
        diagram["edges"] = [["focus", "related", RELATIONS[relation], relation]]
    return diagram, context


def teaching_brief(diagram, context):
    """Return just the cue and the bound observation actually selected for the displayed node."""
    node = diagram["rows"][0][0][4]
    note = node["vocabulary"][0]
    result = {k: note[k] for k in ("item", "context_meaning", "sense_cue") if k in note}
    ref = node.get("memory_ref")
    if ref:
        for point in (context.get("memory") or {}).get("matched", []):
            ep = point.get("latest_observation") or {}
            if point["key"] == ref["key"] and ep.get("episode_id") == ref["episode_id"]:
                result["history"] = {"scope_match": point["scope_match"], **{
                    k: ep[k] for k in ("episode_id", "study_date", "source_article", "first_attempt",
                                       "correction", "followup_status") if k in ep}}
                break
    return result
