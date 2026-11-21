"""Video lesson points on the native writing workspace. Only the course tutor judges meaning."""
from datetime import datetime, timezone
import os
import re
from uuid import uuid4

from .reading_writing_tasks import anchor, check, checked_id, task_contract
from .util import atomic_write_json, load_json, object_sha256, text_sha256
from .errors import ValidationError

SCHEMA="english-lesson-point/1"
STATUSES=("needs_practice","ready_for_next","paused","skipped_by_user")
FOLLOW=("repair","clarify","scaffold","check_variant")
RATINGS=("met","not_met","unknown")
MEDIA=("typed","submitted_text","handwritten","ocr","speech_to_text","copied","mixed","unknown")


def enabled(root=None):
    from .article_workspace import RUNTIME
    if os.environ.get("KAOYAN_ENGLISH_GUIDED_POINTS","on").lower() in {"off","false","0"}:return False
    flag=(root or RUNTIME)/"guided-points-feature.json"
    return not flag.exists() or load_json(flag).get("enabled") is True


def line(value, label):
    check(isinstance(value,str) and 0<len(value.strip())<=4000,"missing "+label)
    return value


def course_source(repo, lesson_id, source, active_session=None):
    from .writing_workspace import lesson_file
    from .writing_contract import VERSION
    kind=source["kind"]
    if kind=="registered_lesson":
        sha=source["lesson_sha256"]; lesson=load_json(lesson_file(repo,sha))
        check(object_sha256(lesson)==sha and lesson.get("contract")==VERSION and lesson["lesson_id"]==lesson_id,
              "point is not bound to this original course lesson")
        src=lesson["source"]
        if src.get("source_kind")=="video":
            from pathlib import Path
            from .util import file_sha256
            check(file_sha256(Path(src["original_path"]))==src["sha256"],"original lesson video changed")
        else:
            from .writing_materials import locate
            same=locate(repo,src["edition"],src["source_id"],src["pdf_page"])
            check(same["sha256"]==src["sha256"],"course material edition changed")
        return dict(kind=kind,lesson_sha256=sha,material=src,video=lesson.get("video"))
    check(kind=="reported_video","unsupported course source")
    msg=native_message(source,"user",active_session)
    excerpt=line(source["excerpt"],"reported video point")
    check(excerpt in msg["content"],"reported point differs from the original learner message")
    return dict(kind=kind,link=source["link"],message_id=msg["message_id"],sha256=text_sha256(msg["content"]),
                excerpt=excerpt,verification="user_report_original_video_unverified")


def native_message(ref,role,session=None):
    from .writing_sources import read_source
    tr=read_source(ref["link"])
    if session is not None:
        check(session in (tr.native,tr.session),"wrong teaching conversation")
    found=next((m for m in tr.messages if m["message_id"]==ref["message_id"] and m["role"]==role),None)
    check(found and text_sha256(found["content"])==ref["sha256"],"original message identity, role or hash mismatch")
    return found


def specification(repo,req,session):
    from .writing_personal import skill_key
    lesson_id=checked_id(req["lesson_id"]); point_id=checked_id(req["lesson_point_id"])
    source=course_source(repo,lesson_id,req["source"],session)
    criteria=req["criteria"]
    check(isinstance(criteria,dict) and 1<=len(criteria)<=6,"a point needs bounded observable criteria")
    for key,row in criteria.items():
        checked_id(key)
        check(isinstance(row,dict) and set(row)=={"description","required","evidence"} and type(row["required"]) is bool
              and row["evidence"] in {"meaning","written_form","reading_judgment"},"invalid point criterion")
        line(row["description"],"criterion description")
    check(any(row["required"] for row in criteria.values()),"no required criterion")
    goal=line(req["goal"],"lesson point goal"); prompt=line(req["question"],"first question")
    focus=req["target_kind"];check(focus in {"function","specified_form","reading_judgment"},"invalid target kind")
    point=dict(schema=SCHEMA,lesson_id=lesson_id,lesson_point_id=point_id,version=checked_id(req["point_version"]),
               source=source,target_kind=focus,criteria=criteria,shared_keys=req.get("shared_keys",[]),prerequisites=req.get("prerequisites",[]))
    check(isinstance(point["shared_keys"],list) and len(point["shared_keys"])<=8 and
          all(re.fullmatch(r"(?:word|syntax|phrase):.{1,120}",v) for v in point["shared_keys"]),"invalid related knowledge key")
    check(isinstance(point["prerequisites"],list) and len(point["prerequisites"])<=6,"invalid prerequisites")
    for ident in point["prerequisites"]: checked_id(ident)
    point["next_point"]=None
    if source["kind"]=="registered_lesson":
        from .writing_workspace import lesson_file
        lesson=load_json(lesson_file(repo,source["lesson_sha256"]))
        known=lesson.get("points",[])
        if known:
            index=next((i for i,p in enumerate(known) if p["lesson_point_id"]==point_id),None)
            check(index is not None,"point is outside the bound lesson")
            for field,value in known[index].items():
                check(req.get(field)==value,"point differs from the frozen lesson plan")
            if index+1<len(known):
                point["next_point"]=known[index+1]["lesson_point_id"]
    task=dict(task_id=checked_id(req.get("task_id",point_id)),task_version=point["version"],rubric_version=checked_id(req["rubric_version"]),
              task_type=req.get("task_type","relation"),task_genre=req.get("task_genre","sentence"),target_skill_id=skill_key(req["target_skill_id"]),
              target_description=goal,prompt=prompt,mode="training",teaching_mode="guided_course_point",context_id=lesson_id,
              context_basis=goal,opportunity="not_applicable",opportunity_reason="Guided course practice.",task_relation="same_task",
              observed_scope_complete=True,surfaces={k:dict(text=prompt if k=="prompt" else "",cue=("form" if focus=="specified_form" else "function")
              if k=="prompt" else "none",support="none") for k in ("prompt","sidebar","adjacent")},
              success_criteria={k:r["description"] for k,r in criteria.items()},qualified_variants=req["qualified_variants"],
              counterexamples=req["counterexamples"],course_point=point)
    for key in ("hint_ladder","hint_policy","genre_constraint","source_ref"):
        if key in req:task[key]=req[key]
    return task_contract(task)


def read_scope(repo,root,task):
    ref=task.get("source_ref")
    if not ref:return
    from .reading_writing_tasks import source_return
    source_return(repo,root,ref)
    question=ref.get("question_id")
    if question:
        from .article_workspace import database, head, saved_section, verified
        with database(root,ref["source_id"]) as db:
            state=head(db)
            article=verified(repo,root,state)
            grant=saved_section(db,state,question).get("grant")
            check(grant and grant.get("source_binding")==article["binding"] and grant.get("unit")==question and grant.get("permission")=="review",
                  "reading_question_scope_required: continue the exact original article in its authorized view")


def begin(repo,root,key,req,session):
    from . import writing_workspace as ws, writing_practice as practice
    check(enabled(root),"guided_points_disabled")
    task=specification(repo,req,session)
    read_scope(repo,root,task)
    directory=ws.folder(root,key)
    old=ws.current(root,key) if (directory/"current.json").exists() else None
    check(not old or (old.get("micro") or {}).get("task",{}).get("mode")!="open" and
          (old.get("practice") or {}).get("phase")!="transfer","finish the protected assessment before opening a course point")
    # Start identity belongs to this practice; deliberately redoing the point uses a new practice ID.
    registration=checked_id(req["practice_id"])
    source=task["course_point"]["source"]
    control_ref={"kind":"native_start_request","operation":registration}
    if req.get("initial_message") or source["kind"]=="reported_video":
        ref=req.get("initial_message") or source
        original=native_message(ref,"user",session)
        control_ref=dict(kind="original_user_message",message_id=original["message_id"],sha256=text_sha256(original["content"]))
    return practice._start(root,repo,key,dict(expected_revision=req.get("revision"),guided_practice_id=registration,initial_control=control_ref),task,None)


def initial(task,practice_id):
    question=dict(id=uuid4().hex,text=task["prompt"],criteria=[k for k,r in task["course_point"]["criteria"].items() if r["required"]],
                  reason=task["target_description"],expected=task["target_description"],source_response=None,status="planned")
    return dict(practice_id=practice_id,status="needs_practice",questions=[question],current_question=question["id"],
                decisions=[],control_events=[],awaiting_decision=None,return_to_point=None)


def current_question(g):
    return next(q for q in g["questions"] if q["id"]==g["current_question"])


def state_view(s):
    m=s["micro"];g=m["guided"];p=m["task"]["course_point"];q=current_question(g)
    last=g["decisions"][-1] if g["decisions"] else None
    outbound=next((ev for ev in reversed(m["events"]) if ev.get("prepared_feedback")),None)
    return dict(lesson_id=p["lesson_id"],lesson_point_id=p["lesson_point_id"],point_version=p["version"],practice_id=g["practice_id"],
                outbound_id=outbound["id"] if outbound else None,feedback_seen=bool(outbound and outbound.get("seen_at")),
                attempt_id=s["attempt_id"],revision=s["revision"],workspace=s["entity_key"],status=g["status"],question=q,
                pending_response=g["awaiting_decision"] or (g.get("incoming") or {}).get("id"),unresolved=last["unresolved"] if last else q["criteria"],
                feedback=outbound["prepared_feedback"] if outbound else (last["feedback"] if last else ""),next_point=p["next_point"] if g["status"]=="ready_for_next" else None,
                controls=g["control_events"][-1:])


def confirm_feedback(m,ident,event,native_id=None):
    if not ident:return
    source=next((ev for ev in m["events"] if ev["id"]==ident and ev.get("prepared_feedback")),None)
    check(source is not None,"unknown tutor feedback")
    if not source.get("seen_at"):
        source["seen_at"]=event["at"];source["visible_text"]=source["prepared_feedback"]
        if native_id:source["outbound_native_message_id"]=native_id
        if source.get("prepared_support"):source["support"]=source["prepared_support"]
    event["feedback_id"]=ident


def mark_sent(g,posted,event):
    q=current_question(g)
    check(g["status"]=="needs_practice" and q["id"]==posted["question_id"] and text_sha256(q["text"])==posted["question_sha256"],
          "stale question delivery")
    check(q["status"]=="planned","question already delivered")
    q.update(status="sent",delivery_id=event["id"],delivered_at=posted.get("_native_at",event["at"]),channel=posted.get("_channel","reader"))
    if "_message_id" in posted:
        q["message_id"]=posted["_message_id"]
        event["native_message_id"]=posted["_message_id"]
    event.update(question_id=q["id"],visible_text=q["text"])


def bind_submission(m,posted):
    g=m["guided"];q=current_question(g)
    check(enabled() and g["status"]=="needs_practice" and g["awaiting_decision"] is None and
          q["status"]=="sent" and q["id"]==posted.get("question_id"),"submission requires its actually delivered question")
    medium=posted.get("medium","typed")
    check(medium in MEDIA,"invalid submission medium")
    proof=posted.get("verification","as_submitted")
    check(proof in {"as_submitted","unverified","checked_original"},"invalid verification")
    if proof=="checked_original":
        check(posted.get("_original_verified") is True,"original attachment must be verified through Capture")
    if "_native_at" in posted:
        parse=lambda s:datetime.fromisoformat(s.replace("Z","+00:00"))
        check(parse(posted["_native_at"])>=parse(q["delivered_at"]),"submission predates the question")


def recorded_submission(m,d,posted):
    g=m["guided"];q=current_question(g)
    d.update(question_id=q["id"],medium=posted.get("medium","typed"),verification=posted.get("verification","as_submitted"))
    if "_message_id" in posted:
        d.update(native_message_id=posted["_message_id"],native_session=posted["_session"],at=posted["_native_at"])
    if d["medium"] in {"copied","mixed"}:d["conditions"]["production_origin"]="ai_or_mixed"
    g["awaiting_decision"]=d["id"];q["status"]="answered"


def production_origin(m,raw):
    normalize=lambda text:" ".join(text.casefold().split())
    answers=[e.get("example_text") or e.get("visible_text","") for e in m["events"] if e.get("support") in {"example","ai_rewrite"}]
    if raw["medium"] in {"copied","mixed"} or any(normalize(t) in normalize(raw["text"]) for t in answers if t.strip()):
        return "ai_or_mixed"
    return "human_revision" if m["drafts"] else "human_initial"


def unclassified(s, raw):
    m=s["micro"];g=m["guided"];t=m["task"];p=t["course_point"];q=current_question(g)
    check(raw["question_id"]==q["id"] and raw["medium"] in MEDIA,"incorrect question or submission medium")
    check(text_sha256(raw["text"])==raw["sha256"],"response hash mismatch")
    bind=dict(lesson_id=p["lesson_id"],lesson_point_id=p["lesson_point_id"],practice_id=g["practice_id"],
              task_sha256=s["source_version"],rubric_version=t["rubric_version"],attempt_id=s["attempt_id"],
              question_id=q["id"],response_id=raw["id"],response_sha256=raw["sha256"],revision=s["revision"])
    public=dict(binding=bind,goal=t["target_description"],target_kind=p["target_kind"],criteria=p["criteria"],
                question=q["text"],question_purpose=q.get("purpose","prerequisite" if g.get("return_to_point") else "parent_output"),question_status=q["status"],classroom_status=g["status"],answer=raw["text"],medium=raw["medium"],
                verification=raw["verification"],production_origin=production_origin(m,raw),prior=g["decisions"][-3:],
                previous_answers=[{"response_id":d["id"],"text":d["text"],"production_origin":d["conditions"]["production_origin"]} for d in m["drafts"][-4:]],
                help_exposure=[{"support":ev["support"],"text":ev.get("example_text") or ev.get("visible_text","")} for ev in m["events"] if ev.get("support")][-4:],
                next_point_known=bool(p["next_point"]))
    public["task_scope"]={"qualified_variants":t["qualified_variants"],"counterexamples":t["counterexamples"],
                          "genre_constraint":t["genre_constraint"],"hint_ladder":t["hint_ladder"],"hint_policy":t.get("hint_policy",{}),
                          "prerequisites":p["prerequisites"]}
    return dict(public,packet_sha256=object_sha256(public))


def resolve_turn(m,s,posted,event):
    g=m["guided"];raw=posted["_turn"];q=current_question(g);source=unclassified(s,raw)
    check(source["packet_sha256"]==posted["packet_sha256"],"stale original turn")
    obj=posted["decision"]
    check(not g.get("incoming") or g["incoming"]["id"]==raw["id"] or obj["kind"]=="control","another submitted message awaits its own decision")
    if obj["kind"]=="control":
        action={"control":obj["control"],"explanation":line(obj["feedback"],"control response"),"_message_id":raw["id"],
                "support":"example" if obj.get("example") else "hint"}
        if obj.get("example"):action["explanation"]+="\n"+line(obj["example"],"example")
        control(m,action,event)
        if obj.get("example"):event["example_text"]=obj["example"]
    else:
        check(obj["kind"]=="answer","invalid tutor output")
        bind_submission(m,{"question_id":raw["question_id"],"medium":raw["medium"],"verification":raw["verification"],
                           "_native_at":raw["at"],"_original_verified":raw.get("original_verified",False)})
        check(not any(d.get("native_message_id")==raw["id"] and d.get("native_session")==raw["session"] for d in m["drafts"]),
              "original message already assessed")
        from .writing_practice import help_level
        origin=production_origin(m,raw)
        d=dict(id=raw["id"],at=raw["at"],text=raw["text"],sha256=raw["sha256"],parent=m["drafts"][-1]["id"] if m["drafts"] else None,
               feedback_id=None,help_ids=[ev["id"] for ev in m["events"] if ev.get("support")],question_id=q["id"],
               native_message_id=raw["id"],native_session=raw["session"],medium=raw["medium"],verification=raw["verification"],
               conditions=dict(task_cue=m["task"]["task_cue"],answer_support=help_level(m["events"]),task_relation="same_task",
                               production_origin=origin))
        if raw.get("attachment_sha256"):d["attachment_sha256"]=raw["attachment_sha256"]
        m["drafts"].append(d);g["awaiting_decision"]=d["id"];q["status"]="answered"
        bound=packet(s)
        decide(m,s,dict(decision=obj,packet_sha256=bound["packet_sha256"]),event)
    event["decision_input_sha256"]=object_sha256(obj)
    event.update(original_message_id=raw["id"],original_session=raw["session"],original_sha256=raw["sha256"],
                 original_medium=raw["medium"],classroom_status=g["status"],question_id=raw["question_id"])
    if (g.get("incoming") or {}).get("id")==raw["id"]:g.pop("incoming",None)


def packet(s):
    m=s["micro"];g=m["guided"];t=m["task"];p=t["course_point"]
    check(enabled() and g["status"]=="needs_practice" and g["awaiting_decision"],"no submitted answer awaiting review")
    d=next(x for x in m["drafts"] if x["id"]==g["awaiting_decision"]);q=current_question(g)
    check(d["question_id"]==q["id"],"answer question mismatch")
    bind=dict(lesson_id=p["lesson_id"],lesson_point_id=p["lesson_point_id"],practice_id=g["practice_id"],
              task_sha256=s["source_version"],rubric_version=t["rubric_version"],attempt_id=s["attempt_id"],
              question_id=q["id"],response_id=d["id"],response_sha256=d["sha256"],revision=s["revision"])
    public=dict(binding=bind,goal=t["target_description"],target_kind=p["target_kind"],criteria=p["criteria"],
                question=q["text"],answer=d["text"],medium=d["medium"],verification=d["verification"],
                production_origin=d["conditions"]["production_origin"],prior=g["decisions"][-3:],
                help_exposure=[{"support":r["support"],"text":r.get("visible_text","")} for r in m["events"] if r.get("support")][-4:],
                next_point_known=bool(p["next_point"]))
    public["task_scope"]={"qualified_variants":t["qualified_variants"],"counterexamples":t["counterexamples"],
                          "genre_constraint":t["genre_constraint"],"hint_ladder":t["hint_ladder"],"hint_policy":t.get("hint_policy",{})}
    return dict(public,packet_sha256=object_sha256(public))


def decide(m,s,posted,event):
    current=packet(s);g=m["guided"];p=m["task"]["course_point"];d=m["drafts"][-1]
    check(posted["packet_sha256"]==current["packet_sha256"],"stale decision packet")
    obj=posted["decision"];feedback=line(obj["feedback"],"localized feedback");action=obj["action"]
    check(action in FOLLOW+("advance",),"invalid decision action")
    rows=obj["criteria"];check(isinstance(rows,dict) and set(rows)==set(p["criteria"]),"review the frozen criteria")
    evidence={};unresolved=[];blocking=[]
    for k,criterion in p["criteria"].items():
        r=rows[k];check(r["status"] in RATINGS,"invalid criterion result");line(r["reason"],"criterion reason")
        # Earlier sufficient evidence may stand, even if an example is viewed on a later turn.
        rd=next((x for x in m["drafts"] if x["id"]==r.get("response_id",d["id"])),None)
        check(rd is not None,"unknown evidence answer")
        anchored=anchor(rd,dict(draft_id=rd["id"],draft_sha256=rd["sha256"],quote=r["quote"],offset=r.get("offset")))
        status=r["status"]
        reason=r["reason"]
        if ((rd["medium"] in {"ocr","handwritten","unknown"} and rd["verification"]!="checked_original") or
                (rd["medium"]=="speech_to_text" and criterion["evidence"]=="written_form")):
            status="unknown";reason="原始书面形式尚未核实。"+reason
        if status=="met" and criterion["required"]:
            source_q=next(q for q in g["questions"] if q["id"]==rd["question_id"])
            if source_q.get("purpose")=="prerequisite" and p["target_kind"]!="reading_judgment" and not r.get("also_demonstrates_full_output",False):blocking.append(k)
            if rd["conditions"]["production_origin"]=="ai_or_mixed" or (rd["medium"] in {"ocr","handwritten","unknown"} and rd["verification"]!="checked_original") or (rd["medium"]=="speech_to_text" and criterion["evidence"]=="written_form"):
                blocking.append(k)
        if criterion["required"] and status!="met":unresolved.append(k)
        evidence[k]=dict(status=status,reason=reason,anchor=anchored,response_id=rd["id"])
    missing=list(dict.fromkeys(unresolved+blocking))
    check((action=="advance")== (not missing),"advancement must match sufficient required evidence")
    reason=line(obj["reason"],"next action reason")
    strategy=checked_id(obj["strategy"])
    follow=None
    if action!="advance":
        n=obj["next"];targets=n["criteria"]
        check(isinstance(targets,list) and targets and set(targets)<=set(missing),"next question must target an unresolved condition")
        follow=dict(id=uuid4().hex,purpose=n.get("purpose","prerequisite" if action=="scaffold" else "parent_output"),created_at=event["at"],text=line(n["question"],"one next task"),criteria=targets,reason=reason,
                    expected=line(n["expected"],"expected evidence"),source_response=d["id"],status="planned")
        if len(g["decisions"])>=1:
            prev=g["decisions"][-1]
            # A broad criterion can remain open for a different reason. Require a change of support
            # for an identified repeated gap, not simply because both answers involve the same criterion.
            repeated=(obj.get("same_gap_as")==prev["binding"]["response_id"] or
                      d["sha256"]==prev["binding"]["response_sha256"] or follow["text"]==current["question"])
            if repeated and set(targets)==set(prev["unresolved"]):
                check(strategy!=prev["strategy"] and follow["text"]!=current["question"],"repeat difficulty requires a different scaffold")
        check(follow["purpose"] in {"parent_output","prerequisite"},"invalid follow-up purpose")
        g["questions"].append(follow);g["current_question"]=follow["id"]
        g["return_to_point"]=p["lesson_point_id"] if action=="scaffold" else None
    else:
        check(obj.get("next") is None,"advance cannot add a compulsory problem")
        feedback+="\n这个点本轮过关，可以进入下一点。"+("下一点："+p["next_point"] if p["next_point"] else "请继续视频，把下一个知识点带过来。")
        g["status"]="ready_for_next";g["return_to_point"]=None
    criterion_result=("not_met" if any(evidence[k]["status"]=="not_met" for k in unresolved) else "unknown" if unresolved else "met")
    product=obj.get("response_result",criterion_result)
    check(product in RATINGS,"invalid submission result")
    if d["medium"] in {"ocr","handwritten"} and d["verification"]!="checked_original" or (
            d["medium"]=="speech_to_text" and any(v["required"] and v["evidence"]=="written_form" for v in p["criteria"].values())):
        product="unknown"
    if action=="advance":check(product=="met","successful point needs a successful submission or its retained sufficient evidence")
    result=dict(id=event["id"],at=event["at"],binding=current["binding"],criteria=evidence,unresolved=missing,action=action,
                response_result=product,production_evidence="assisted_success" if product=="met" and d["conditions"]["production_origin"]=="ai_or_mixed" else d["conditions"]["production_origin"],
                reason=reason,strategy=strategy,feedback=feedback,next=follow,status=g["status"],support_blocked=blocking)
    g["decisions"].append(result);g["awaiting_decision"]=None
    event.update(visible_text=feedback,support="hint")
    if obj.get("example"):
        event.update(support="example",example_text=line(obj["example"],"actually displayed example"))
        event["visible_text"]+="\n"+event["example_text"]
    m["reviews"].append(dict(id=event["id"],at=event["at"],reviewer="current_course_tutor",draft_id=d["id"],
        rubric_version=m["task"]["rubric_version"],target_skill_id=m["task"]["target_skill_id"],criteria=evidence,language_findings=[],
        target_result=product,
        lesson_decision=result))
    return result


def control(m,posted,event):
    g=m["guided"];k=posted["control"];check(k in {"explain","pause","resume","skip","acknowledge","watched","time_elapsed","change_target","clarify_intent"},"unknown control")
    if k in {"pause","skip","change_target"}:
        if g["status"] not in {"paused","skipped_by_user"}:g["before_suspend"]=g["status"]
        g["status"]="paused" if k=="pause" else "skipped_by_user"
    elif k=="resume" and g["status"] in {"paused","skipped_by_user"}:
        g["status"]=g.pop("before_suspend","needs_practice")
    entry=dict(id=event["id"],at=event["at"],kind=k,status=g["status"],message_id=posted.get("_message_id",event["id"]))
    if posted.get("explanation"):
        entry["explanation"]=line(posted["explanation"],"requested explanation")
        event["visible_text"]=entry["explanation"]
        if k=="explain" or posted.get("support")=="example":
            event["support"]=posted.get("support","hint")
            check(event["support"] in {"hint","example"},"invalid control support")
            if event["support"]=="example":event["example_text"]=entry["explanation"]
    g["control_events"].append(entry)
    return entry
