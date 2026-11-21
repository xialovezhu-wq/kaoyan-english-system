"""Versioned short writing tasks and evidence rules, shared by teaching, Capture and publication."""
import re
from pathlib import Path

from .errors import ValidationError
from .util import file_sha256, load_json, object_sha256, text_sha256

VERSION = "reading-writing/2"
RULE = "open-opportunity/1"
SURFACES = ("prompt", "sidebar", "adjacent")
SUPPORT = ("none", "hint", "example", "ai_rewrite")
TEMPLATES = {
    "relation": {
        "goal": "保留原意，用一到两句话说清两项信息的关系。",
        "criteria": {"meaning": "双方意思及限定保留", "relation": "关系方向正确，主从或指代清楚"},
        "variants": ["合为一句，用从句限定主句", "分为两句，用衔接或指代明确关系"],
        "counterexamples": ["颠倒原因与结果", "把有限条件写成无条件结论"],
        "genre_constraint": "原意、关系和读者理解优先，不按句长评分。"},
    "evidence": {
        "goal": "用自己的话写两到三句，说明材料怎样支持主张。",
        "criteria": {"traceable": "证据能定位到获准材料", "qualified": "保留范围或不确定性", "reason": "说明证据怎样支持主张"},
        "variants": ["先给主张，再解释一条具体证据", "从材料中的观察出发，引出有限定的判断"],
        "counterexamples": ["把材料外的常识归给原文", "只重复主张，没有给出支持理由"],
        "genre_constraint": "归给材料的事实和限定须逐项对照原句。"},
    "genre": {
        "goal": "按读者和目的写一到三句，完成一个段落的交际任务。",
        "criteria": {"purpose": "完成题面要求的行动或论证", "audience": "信息和语域适合指定读者"},
        "variants": ["先讲目的，再给行动或依据", "先交代读者需要的背景，再提出具体请求或立场"],
        "counterexamples": ["读者无从判断下一步要做什么", "给同学的建议写成上级命令"],
        "genre_constraint": "允许切题的观点和建议，材料引用按本题约束核验。"}
}


def check(condition, reason):
    if not condition:
        raise ValidationError(reason)


def checked_id(value):
    check(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value),
          "missing or invalid stable writing identifier")
    return value


def source_return(repo, runtime, ref):
    """Re-enter only a currently visible sentence of the exact completed article."""
    from .article_source import read_article
    from .article_workspace import project
    key, unit = ref["source_id"], ref["unit_id"]
    article = read_article(repo, runtime, key)
    view = project(repo, runtime, key)
    sentence = article["sections"].get(unit, {})
    visible = {row["id"] for row in view["sections"] if "text" in row or "tabs" in row}
    question = ref.get("question_id")
    check(re.fullmatch(r"S[0-9]+", unit) and unit in visible and
          article["binding"]["source_hash"] == ref["source_hash"] and
          sentence["text_sha256"] == ref["sentence_sha256"] and
          (question is None or re.fullmatch(r"Q[0-9]+", question) and question in visible),
          "article, sentence, question, hash or current authorization mismatch")
    fragment="~".join([unit,article["source_version"],sentence["text_sha256"],question or ""])
    return dict(href="/english/a/" + key + "#" + fragment, text=sentence["text"], unit_id=unit,
                question_id=question, sentence_sha256=sentence["text_sha256"])


def completion(repo, ref, runtime=None):
    if "completion_message" in ref:
        from .writing_sources import read_source, digest_message
        from .article_workspace import database, head
        from datetime import datetime
        mark=ref["completion_message"]
        tr=read_source(mark["link"])
        with database(runtime, ref["source_id"]) as db:
            state=head(db)
            opened=db.execute("SELECT min(written_at) FROM revisions WHERE attempt=?",(state["attempt_id"],)).fetchone()[0]
        row=next((m for m in tr.messages if m["message_id"]==mark["message_id"] and m["role"]=="user"),None)
        check(state["session"]==tr.native and row and digest_message(row)==mark["sha256"]
              and datetime.fromisoformat(row["created_at"].replace("Z","+00:00")).timestamp()>=opened
              and isinstance(mark.get("quote"),str) and mark["quote"] in row["content"]
              and re.fullmatch(r"(?:这篇|本篇|这篇阅读|本篇阅读|这篇文章)?(?:我)?(?:已|已经)?(?:读完了?|学完了?|精读结束了?|阅读完成了?)",mark["quote"].strip()),
              "learner completion must bind this article, attempt and exact original message")
        check(ref["completion_id"]==object_sha256([state["attempt_id"],mark["sha256"]]),"completion identity mismatch")
        return ref["completion_id"]
    date, ident = ref["completion_day"], checked_id(ref["completion_id"])
    check(re.fullmatch(r"\d{4}-\d\d-\d\d", date), "invalid reading completion date")
    path = repo / "intake/receipts/completion" / date / (ident + ".json")
    row = load_json(path)
    check(row.get("schema_version") == "english_article_completion_receipt_v2" and
          row.get("completion_id") == ident and row.get("source_id") == ref["source_id"] and
          row.get("package_ids") and file_sha256(path) == ref["completion_sha256"], "completed reading receipt required")
    return ident


def cue_level(text, declared, forms, functions):
    # Tutor codes the meaning of the actual instruction. Explicit cues can only raise that rating.
    check(declared in {"none", "form", "function", "unknown"}, "unreviewed cue surface")
    if declared == "form" or any(re.search(r"(?<![a-z])" + re.escape(w.casefold()) + r"(?![a-z])", text.casefold()) for w in forms):
        return "form"
    if declared == "function" or any(w.casefold() in text.casefold() for w in functions):
        return "function"
    return declared


def checked_rubric(spec, template):
    criteria=spec.get("success_criteria",template["criteria"])
    check(isinstance(criteria,dict) and 1<=len(criteria)<=8,"one to eight explicit criteria required")
    for key,meaning in criteria.items():
        checked_id(key)
        check(isinstance(meaning,str) and 0<len(meaning.strip())<=1200,"invalid success condition")
    result={"success_criteria":dict(criteria)}
    for name,default in (("counterexamples",template["counterexamples"]),("hint_ladder",["指出当前目标与位置","提供局部方向","按需给例子或建议"])):
        values=spec.get(name,default)
        check(isinstance(values,list) and 1<=len(values)<=12 and all(isinstance(s,str) and 0<len(s.strip())<=2000 for s in values),"invalid "+name)
        result[name]=list(values)
    genre=spec.get("genre_constraint",template["genre_constraint"])
    check(isinstance(genre,str) and 0<len(genre.strip())<=2000,"invalid genre constraint")
    if "hint_policy" in spec:
        policy=spec["hint_policy"]
        check(isinstance(policy,dict) and 0<len(policy)<=8 and all(isinstance(v,str) and 0<len(v.strip())<=1200 for v in policy.values()),
              "invalid task-specific help policy")
        for key in policy:checked_id(key)
        result["hint_policy"]=dict(policy)
    return {**result,"genre_constraint":genre}


def task_contract(spec):
    from .writing_personal import skill_key
    template = TEMPLATES[spec["task_type"]]
    for k in ("task_id", "task_version", "rubric_version", "context_id"):
        checked_id(spec[k])
    for k in ("prompt", "task_genre", "context_basis", "target_description", "opportunity_reason"):
        check(isinstance(spec.get(k), str) and 0 < len(spec[k].strip()) <= 4000, "missing task " + k)
    skill_key(spec["target_skill_id"])
    check(spec["task_genre"] in {"argument","practical","chart","notice","letter","source_response","sentence"}, "unknown exam genre")
    check(spec["mode"] in {"training", "open"} and spec["opportunity"] in {"applicable","not_applicable","unknown"},
          "invalid assessment mode or opportunity")
    check(spec["task_relation"] in {"same_task","new_task_same_context","new_context","unknown"}, "relation not reviewed")
    check(spec["task_type"] != "evidence" or bool(spec.get("source_ref")), "evidence task requires authorized reading")
    variants = spec.get("qualified_variants", template["variants"])
    check(isinstance(variants,list) and len(set(variants)) >= 2 and all(isinstance(v,str) and v.strip() for v in variants),
          "two distinct acceptable approaches required")
    surfaces = spec["surfaces"]
    check(set(surfaces) == set(SURFACES) and surfaces["prompt"]["text"] == spec["prompt"], "review all visible cue surfaces")
    forms = spec.get("forms", [])
    functions = spec.get("functions", [])
    if spec["target_skill_id"] == "writing_skill:concession":
        forms = list(set([*forms,"although","even though"]))
        functions = list(set([*functions,"让步","concession","concede"]))
    levels = [cue_level(surfaces[k]["text"], surfaces[k]["cue"], forms, functions) for k in SURFACES]
    cue = next((v for v in ("form","function","unknown") if v in levels), "none")
    if spec["mode"] == "training" and cue == "none":
        cue = "function"  # The training view shows the goal.
    mode=spec.get("teaching_mode","open_assessment" if spec["mode"]=="open" else "reading_micro")
    check(mode in {"guided_course_point","reading_micro","open_assessment"} and
          (mode=="open_assessment")== (spec["mode"]=="open"),"incompatible teaching/assessment modes")
    return {**spec, "forms":sorted(forms), "functions":sorted(functions), "contract": VERSION, "exam_family":"kaoyan_english_i", "task_cue":cue, **checked_rubric(spec,template),
            "qualified_variants":variants, "example_hidden":True,
            "denominator_rule":RULE}


def anchor(draft, supplied):
    text, quote, offset = draft["text"], supplied.get("quote"), supplied.get("offset")
    check(supplied.get("draft_id") == draft["id"] and supplied.get("draft_sha256") == text_sha256(text),
          "anchor draft version changed")
    check(isinstance(quote,str) and quote.strip(), "empty sentence anchor")
    if offset is None:
        offset = text.find(quote)
        check(offset >= 0 and text.find(quote,offset+1)<0, "sentence missing or ambiguous")
    check(type(offset) is int and 0 <= offset <= len(text) and text[offset:offset+len(quote)] == quote, "quote or range changed")
    return dict(draft_id=draft["id"], draft_sha256=text_sha256(text), offset=offset, length=len(quote), quote=quote,
                offset_unit="unicode_codepoint", paragraph=1+text[:offset].count("\n"), before=text[max(0,offset-24):offset],
                after=text[offset+len(quote):offset+len(quote)+24])


def judge(task, draft, review):
    """A reviewer supplies criterion-level readings of actual spans. This is not a text matching grader."""
    checked_id(review["reviewer"])
    check(review["rubric_version"] == task["rubric_version"] and review["target_skill_id"] == task["target_skill_id"],
          "wrong rubric version or feedback target")
    rows=review["criteria"]
    check(set(rows)==set(task["success_criteria"]), "each criterion needs a reason and a located finding")
    statuses=[]
    checked={}
    for key,row in rows.items():
        check(row["status"] in {"met","not_met","unknown"} and row["reason"].strip(), "invalid criterion finding")
        checked[key] = {**row, "anchor":anchor(draft,row["anchor"])}
        statuses.append(row["status"])
    language=review["language_findings"]
    check(isinstance(language,list) and all(isinstance(f,dict) and f["reason"].strip()
          and type(f["blocks_meaning"]) is bool for f in language), "invalid language findings")
    for item in language:
        anchor(draft,item["anchor"])
    result = ("not_applicable" if task["opportunity"] == "not_applicable" else
              "unknown" if any(f["blocks_meaning"] for f in language) else
              "not_met" if "not_met" in statuses else "unknown" if "unknown" in statuses else "met")
    return {**review,"criteria":checked,"target_result":result,"draft_id":draft["id"]}


PUBLIC = ("activity","exam_family","task_id","task_type","task_version","rubric_version","task_genre_code","target_skill_id",
          "draft_id","parent_draft_id","feedback_id","denominator_rule",
          "task_cue","answer_support","task_relation","production_origin","target_result",
          "opportunity","eligibility","open_task_transfer_observed","draft_sha256","task_sha256",
          "review_sha256","language_issue_count","source_sentence_sha256","source_binding_sha256","observation_id")


def observation(task, draft, review=None, *, opportunity=None):
    cond = draft["conditions"]
    result = review["target_result"] if review else "unknown"
    opportunity = opportunity or task["opportunity"]
    if opportunity == "not_applicable":
        result = "not_applicable"
    elif result == "not_applicable":
        result = "unknown"
    conditions = [(opportunity,"applicable"),(cond["task_cue"],"none"),(cond["answer_support"],"none"),
                  (cond["task_relation"],"new_context"),(cond["production_origin"],"human_initial")]
    if any(actual not in (expected,"unknown") for actual,expected in conditions) or task["mode"]!="open":
        eligible="ineligible"
    elif any(actual=="unknown" for actual,expected in conditions):
        eligible="eligibility_pending"
    else:
        eligible="eligible"
    ref=task.get("source_ref") or {}
    return dict(activity="writing", exam_family=task["exam_family"],task_id=task["task_id"],draft_id=draft["id"],
                parent_draft_id=draft.get("parent"),feedback_id=draft.get("feedback_id"),denominator_rule=RULE,
                task_type=task["task_type"], task_version=task["task_version"],rubric_version=task["rubric_version"],
                task_genre_code=task["task_genre"], target_skill_id=task["target_skill_id"], task_sha256=object_sha256(task),
                opportunity=opportunity, eligibility=eligible, target_result=result, **cond,
                open_task_transfer_observed=eligible=="eligible" and result=="met", draft_sha256=text_sha256(draft["text"]),
                review_sha256=object_sha256(review) if review else None, language_issue_count=len(review["language_findings"]) if review else None,
                source_sentence_sha256=ref.get("sentence_sha256"), source_binding_sha256=object_sha256(ref) if ref else None,
                observation_id=object_sha256([draft["id"],task["target_skill_id"]]))


def safe_fields(episode):
    value=episode.get("writing_task") or {}
    if value:
        fields={k:value[k] for k in PUBLIC if k in value}
        if value.get("lesson_point"):
            from .lesson_point_memory import validate
            point=validate(value["lesson_point"])
            if point["shareable"]:fields["lesson_point"]=point
        return fields
    return dict(activity="writing", task_cue="unknown",answer_support="unknown",task_relation="unknown",
                production_origin="unknown",target_result="unknown",eligibility="eligibility_pending",
                open_task_transfer_observed=False)


def metrics(attempts):
    counts=dict(offered=0, skipped=0, switched_to_training=0, independent_initial=0, eligible=0,
                met=0, not_met=0, result_unknown=0, eligibility_pending=0, not_applicable=0)
    for entry in attempts:
        counts["offered"]+=1
        counts["skipped"]+=int(entry["skipped"])
        counts["switched_to_training"]+=int(entry["switched"])
        obs=entry.get("first")
        if not obs:
            continue
        counts["independent_initial"]+=int(obs["production_origin"]=="human_initial" and obs["answer_support"]=="none" and obs["task_cue"]=="none")
        counts["not_applicable"]+=int(obs["target_result"]=="not_applicable")
        counts["eligibility_pending"]+=int(obs["eligibility"]=="eligibility_pending")
        if obs["eligibility"]=="eligible":
            counts["eligible"]+=1
            counts["result_unknown" if obs["target_result"]=="unknown" else obs["target_result"]]+=1
    n=counts["eligible"]
    return {**counts,"observed_success_over_all_eligible":counts["met"]/n if n else None}
