"""Short writing inside the existing versioned writing workspace. No formal writes."""
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

from . import writing_workspace as workspace
from .errors import ValidationError
from .reading_writing_tasks import (VERSION, anchor, check, completion, judge, metrics, observation,
                                    source_return, task_contract, SUPPORT)
from .util import atomic_write_json, exclusive_lock, load_json, object_sha256, text_sha256


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def attempts(root, key):
    latest = {}
    for p in (workspace.folder(root,key) / "revisions").glob("*.json"):
        row = load_json(p)
        if row.get("micro") and row["revision"] > latest.get(row["attempt_id"],{}).get("revision",0):
            latest[row["attempt_id"]] = row
    return latest


def start(repo, root, key, request):
    if os.environ.get("KAOYAN_ENGLISH_MICRO_WRITING","on") == "off":
        return {"status":"disabled"}
    spec = request["task"]
    check("course_point" not in spec and spec.get("teaching_mode")!="guided_course_point","use the verified lesson-point entry")
    ref = spec.get("source_ref")
    if ref:
        if not request.get("reading_completed"):
            return {"status":"reading_in_progress"}
        completion(repo,ref,root)
        source_return(repo,root,ref)
    check(ref or spec["task_type"] == "genre" or spec["mode"] == "open", "reading or writing task context required")
    task = task_contract(spec)
    if ref and task["mode"] == "training":
        claim = root / "writing" / "reading-offers" / (object_sha256([ref["source_id"],ref["completion_id"]])+".json")
        with exclusive_lock(claim.with_suffix(".lock")):
            if claim.exists() and load_json(claim)["key"]!=key:
                return {"status":"reading_offer_already_used"}
            result=_start(root,repo,key,request,task,ref)
            if result["status"] in {"offered","already_offered"}:
                atomic_write_json(claim,dict(key=key,attempt_id=result["attempt_id"]))
            return result
    return _start(root,repo,key,request,task,ref)


def _start(root,repo,key,request,task,ref):
    directory = workspace.folder(root,key)
    with exclusive_lock(directory/"display.lock"):
        prior = attempts(root,key)
        for row in prior.values():
            previous = row["micro"]["task"]
            same = previous["task_id"] == task["task_id"] and previous["task_version"] == task["task_version"]
            guided_id=request.get("guided_practice_id")
            if guided_id:
                old_id=(row["micro"].get("guided") or {}).get("practice_id")
                if guided_id==old_id:
                    check(object_sha256(task)==row["source_version"],"practice already has another frozen contract")
                    return {"status":"already_offered","attempt_id":row["attempt_id"],"revision":row["revision"]}
                if previous.get("course_point") and same:
                    check(previous==task,"use a new version for changed lesson criteria")
                same=False
            if same:
                check(object_sha256(task)==row["micro"]["request_hash"],"a changed task needs a new version")
                return {"status":"already_offered","attempt_id":row["attempt_id"],"revision":row["revision"]}
            if (ref and previous.get("teaching_mode")!="guided_course_point" and previous.get("source_ref") and ref.get("completion_id")==previous["source_ref"].get("completion_id")
                    and task["mode"]=="training" and previous["mode"]=="training"):
                return {"status":"reading_offer_already_used"}
        relation=task["task_relation"]
        previous_key=task.get("previous_entity_key",key)
        other=prior if previous_key==key else attempts(root,previous_key)
        basis=other.get(task.get("previous_attempt"))
        if relation == "new_context":
            if not basis or basis["micro"]["task"]["target_skill_id"]!=task["target_skill_id"]:
                relation="unknown"
            else:
                former=basis["micro"]["task"]
                check(bool(basis["micro"]["drafts"]), "previous context has no submitted work")
                if former["prompt"].strip().casefold()==task["prompt"].strip().casefold():
                    relation="same_task"
                elif former["context_id"]==task["context_id"] or former["context_basis"]==task["context_basis"]:
                    relation="new_task_same_context"
        if relation=="new_context":
            for row in {**other,**prior}.values():
                past=row["micro"]["task"]
                if " ".join(past["prompt"].casefold().split())==" ".join(task["prompt"].casefold().split()):
                    relation="same_task"
                    break
                if past["context_id"]==task["context_id"]:
                    relation="new_task_same_context"
        old=workspace.current(root,key) if (directory/"current.json").exists() else None
        check(not old or request.get("expected_revision")==old["revision"], "revision_conflict")
        stamp=now()
        sha=object_sha256(task)
        spec_path=workspace.lesson_file(repo,sha)
        if spec_path.exists():
            check(load_json(spec_path)==task,"immutable task collision")
        else:
            atomic_write_json(spec_path,task)
        micro=dict(task=task,request_hash=sha,relation=relation,opened_at=stamp,events=[],drafts=[],reviews=[],feedback=None,
                   skipped=False,switched=False,unsubmitted="",corrections=[])
        if request.get("guided_practice_id"):
            from .guided_points import initial
            prior_guide=(old.get("micro",{}).get("guided") if old else None)
            check(not prior_guide or prior_guide["status"] in {"ready_for_next","paused","skipped_by_user"},"finish, pause or explicitly change the current point first")
            micro["guided"]=initial(task,request["guided_practice_id"])
            micro["guided"]["start_control"]=request["initial_control"]
            check(not old or (old.get("micro") or {}).get("task",{}).get("mode")!="open" and
                  (old.get("practice") or {}).get("phase")!="transfer","protected assessment active")
        state=workspace.commit(directory,dict(entity_key=key,revision=1 if not old else old["revision"]+1,
            source_version=sha,attempt_id=uuid4().hex,access_epoch=uuid4().hex,status="active",stage="attempt",
            lesson_id=task["task_id"],started_at=stamp,notes=[],templates=[],page_release=None,micro=micro))
        return {"status":"offered","revision":state["revision"],"attempt_id":state["attempt_id"]}


def help_level(events):
    kinds={r.get("support") for r in events}
    return next((k for k in reversed(SUPPORT[1:]) if k in kinds),"unknown" if "unknown" in kinds else "none")


def current_draft(micro):
    return micro["drafts"][-1] if micro["drafts"] else None


def operate(repo, root, key, posted, *, coach=False, native=None):
    check(not any(k.startswith("_") for k in posted),"client supplied internal binding")
    if native:
        posted={**posted,**native}
    directory=workspace.folder(root,key)
    with exclusive_lock(directory/"display.lock"):
        state=workspace.current(root,key)
        m=state["micro"]
        check(posted["attempt_id"]==state["attempt_id"],"stale attempt")
        eid=str(UUID(posted["action_id"]))
        request_hash=object_sha256(posted)
        existing=next((e for e in m["events"] if e["id"]==eid),None)
        if existing:
            check(existing["request_sha256"]==request_hash,"action id conflict")
            return {"status":"saved","id":eid,"revision":state["revision"]}
        if posted.get("_turn"):
            raw=posted["_turn"]
            prior=next((ev for ev in m["events"] if ev.get("original_message_id")==raw["id"] and ev.get("original_session")==raw["session"]),None)
            if prior:
                check(prior["original_sha256"]==raw["sha256"] and prior["question_id"]==raw["question_id"] and
                      prior.get("decision_input_sha256")==object_sha256(posted["decision"]),"original point turn conflict")
                return {"status":"saved","id":prior["id"],"revision":state["revision"]}
        check(state["status"]=="active" and posted["revision"]==state["revision"], "stale draft revision")
        task=m["task"]; draft=current_draft(m); fb=m["feedback"]
        check(not m["skipped"],"this optional task was skipped")
        op=posted["kind"]; stamp=now()
        event=dict(id=eid,kind=op,at=stamp,base_revision=state["revision"],request_sha256=request_hash,
                   provider="tutor" if coach else "learner")
        if posted.get("_client_request_sha256"):event["client_request_sha256"]=posted["_client_request_sha256"]
        guided=m.get("guided")
        if guided:
            from . import guided_points as points
            check(points.enabled(root),"guided_points_disabled")
            points.read_scope(repo,root,task)
            allowed=({"point_decision","point_control","point_turn","question_seen","feedback_seen","exposure"} if coach else
                     {"question_seen","feedback_seen","point_control","submit","save_draft","locate","select_draft"})
            check(op in allowed,"use the current point's question and evidence")
            if not coach and op=="point_control":
                check(posted.get("control") in {"pause","resume","skip","explain","acknowledge","watched","time_elapsed","change_target","clarify_intent"} and "explanation" not in posted,
                      "invalid learner control")
        elif coach:
            check(op in {"feedback","assess","correct_opportunity","exposure"}, "unknown tutor action")
        else:
            check(op in {"save_draft","submit","help","example","rewrite","accept","decline","skip","locate","select_draft"}, "unknown learner action")
        if op in {"submit","save_draft","help"}:
            text=posted.get("text","")
            check(isinstance(text,str) and len(text)<=20000, "writing text too long")
        if op=="point_turn":
            points.resolve_turn(m,state,posted,event)
        elif op=="point_decision":
            points.decide(m,state,posted,event)
        elif op=="feedback_seen":
            points.confirm_feedback(m,posted["feedback_id"],event,posted.get("_message_id"))
        elif op=="question_seen":
            points.mark_sent(guided,posted,event)
            points.confirm_feedback(m,posted.get("feedback_id"),event,posted.get("_message_id"))
        elif op=="point_control":
            if not coach and posted["control"]=="explain":
                check(not guided.get("incoming"),"a submitted message awaits review")
                buffered=posted.get("text","")
                check(isinstance(buffered,str) and len(buffered)<=20000,"invalid unsubmitted buffer")
                m["unsubmitted"]=buffered
                text="请解释这个点。"
                event["submitted_text"]=text
                guided["incoming"]=dict(id=eid,text=text,sha256=text_sha256(text),at=stamp,session=state["entity_key"],
                                        question_id=points.current_question(guided)["id"],medium="typed",verification="as_submitted")
            else:
                points.control(m,posted,event)
                labels={"pause":"暂停","skip":"跳过本点","resume":"继续本点","acknowledge":"我懂了","watched":"已看完",
                        "time_elapsed":"时间到","change_target":"更换目标","clarify_intent":"请澄清"}
                if not native:
                    event["submitted_text"]=labels[posted["control"]]
                original=event.get("submitted_text",posted.get("_original_text",""))
                event.update(original_message_id=posted.get("_message_id",eid),original_sha256=text_sha256(original),
                             original_session=posted.get("_session",state["entity_key"]),original_medium="unknown",
                             question_id=points.current_question(guided)["id"])
        elif op in {"locate","select_draft"}:
            ident=posted["draft_id"]
            check(any(d["id"]==ident for d in m["drafts"]), "unknown original draft")
            if op=="locate":
                check(fb and fb["anchor"]["draft_id"]==ident, "feedback anchor is no longer current")
            m["selected_draft"]=ident
            m["focus_anchor"]=fb["anchor"] if op=="locate" else None
        elif op=="save_draft":
            m["unsubmitted"]=text
        elif op=="skip":
            check(not draft,"an existing submission cannot be counted as skipped")
            m["skipped"]=True
        elif op=="feedback":
            check(draft and posted["target_skill_id"]==task["target_skill_id"] and posted["direction"].strip(),
                  "feedback needs one current target and a submitted draft")
            m["selected_draft"]=draft["id"]
            m["focus_anchor"]=anchor(draft,posted["anchor"])
            m["feedback"]={"id":eid,"anchor":anchor(draft,posted["anchor"]),"direction":posted["direction"],
                           "example":posted.get("example",""),"rewrite":posted.get("rewrite",""),"decision":"pending",
                           "target_skill_id":task["target_skill_id"]}
            event.update(support="hint",visible_text=posted["direction"])
        elif op in {"help","example","rewrite","exposure"}:
            if op=="help":
                m["unsubmitted"]=text  # Persist unsent work before releasing help.
                m["switched"]=True
                event.update(support="hint",visible_text=posted.get("help_text") if coach else task["target_description"])
            elif op=="exposure":
                check(posted["support"] in (*SUPPORT[1:],"unknown") and posted["surface"] in ("adjacent","sidebar","chat"),
                      "invalid observed help")
                event.update(support=posted["support"],visible_text=posted["text"],surface=posted["surface"])
            else:
                check(fb and fb[op],"no suggestion is available for this draft")
                event.update(feedback_id=fb["id"],support="example" if op=="example" else "ai_rewrite",visible_text=fb[op])
        elif op=="submit" and guided:
            points.bind_submission(m,posted)
            check(not guided.get("incoming") and isinstance(text,str) and text.strip(),"wait for this submitted message")
            if not native:event["submitted_text"]=text
            guided["incoming"]=dict(id=posted.get("_message_id",eid),text=text,sha256=text_sha256(text),
                                    at=posted.get("_native_at",stamp),session=posted.get("_session",state["entity_key"]),
                                    question_id=posted["question_id"],medium=posted.get("medium","typed"),verification=posted.get("verification","as_submitted"))
            m["unsubmitted"]=""
        elif op in {"submit","accept"}:
            if guided:points.bind_submission(m,posted)
            base=posted.get("base_draft")
            check(base==(draft["id"] if draft else None),"stale base draft")
            if op=="accept":
                check(fb and fb["decision"]=="pending" and draft and fb["anchor"]["draft_id"]==base
                      and any(e.get("feedback_id")==fb["id"] and e["kind"]=="rewrite" for e in m["events"]),
                      "suggestion must be opened on this draft before accepting")
                a=fb["anchor"]; text=draft["text"][:a["offset"]]+fb["rewrite"]+draft["text"][a["offset"]+a["length"]:]
                fb["decision"]="accepted"
            check(isinstance(text,str) and text.strip() and len(text)<=20000,"submit a nonempty draft")
            support=help_level([{"support":s.get("support","unknown")} for s in task["surfaces"].values()]+m["events"])
            normalized=lambda v:" ".join(v.casefold().split())
            copied=any(e.get("visible_text") and e.get("support") in {"example","ai_rewrite"}
                       and normalized(e.get("example_text") or e["visible_text"]) in normalized(text) for e in m["events"])
            known=task.get("observed_scope_complete") is True
            conditions=dict(task_cue=task["task_cue"],answer_support=support if known or support!="none" else "unknown",
                            task_relation=m["relation"],production_origin="ai_or_mixed" if op=="accept" or copied else
                            "human_revision" if draft else "human_initial")
            doc=dict(id=eid,parent=base,text=text,sha256=text_sha256(text),at=stamp,conditions=conditions,
                     feedback_id=fb["id"] if fb else None,help_ids=[e["id"] for e in m["events"] if e.get("support")])
            if guided:points.recorded_submission(m,doc,posted)
            m["drafts"].append(doc);m["unsubmitted"]=""
            if fb and op=="submit" and fb["decision"]=="pending":
                fb["decision"]="self_revised"
        elif op=="decline":
            check(fb and draft and fb["anchor"]["draft_id"]==draft["id"] and fb["decision"]=="pending","feedback is no longer current")
            fb["decision"]="declined"
        elif op=="assess":
            check(draft and posted["draft_id"]==draft["id"],"review must locate the selected current draft")
            result=judge(task,draft,posted["review"])
            result.update(id=eid,at=stamp,feedback_id=draft.get("feedback_id"))
            m["reviews"].append(result)
            labels={"met":"已达到","not_met":"仍需修改","unknown":"待核","not_applicable":"不适用"}
            visible=["本次目标 · "+labels[result["target_result"]],*[row["reason"] for row in result["criteria"].values()],
                     *["语言 · "+row["reason"] for row in result["language_findings"]]]
            event.update(support="hint",visible_text="\n".join(visible))
        elif op=="correct_opportunity":
            value=posted["opportunity"]
            check(value in {"applicable","unknown","not_applicable"} and posted["reason"].strip(),"correction needs an explicit reason")
            m["corrections"].append(dict(at=stamp,value=value,reason=posted["reason"],id=eid))
        if guided and op in {"point_turn","point_decision","point_control"} and event.get("visible_text"):
            event["prepared_feedback"]=event.pop("visible_text")
            event["prepared_support"]=event.pop("support",None)
        m["events"].append(event)
        state=workspace.commit(directory,{**state,"revision":state["revision"]+1})
        return {"status":"saved","id":eid,"revision":state["revision"]}


def summarize(m, *, corrected=True):
    drafts=m["drafts"]
    result=None
    if drafts:
        first=drafts[0]
        review=next((r for r in reversed(m["reviews"]) if r["draft_id"]==first["id"]),None)
        task=m["task"]
        opportunity=m["corrections"][-1]["value"] if corrected and m["corrections"] else None
        result=observation(task,first,review,opportunity=opportunity)
    return {"first":result,"skipped":m["skipped"],"switched":m["switched"]}


def confirmed_transfer(repo):
    from .learning_state import learning_events_path, load_learning_events
    from .memory_store import read_lifecycle
    lifecycle=read_lifecycle(repo/"intake")
    if lifecycle.get("global_pending"):
        return {"status":"pending","reason":"memory_update_pending"}
    selected={}
    invalid=set(lifecycle.get("invalidated_episode_ids",[]))
    ledger=learning_events_path(repo)
    for row in load_learning_events(ledger) if ledger.exists() else []:
        w=row["event"].get("writing_memory") or {}
        obs=w.get("writing_task")
        if (not obs or obs.get("lesson_point") or "parent_draft_id" not in obs or obs["parent_draft_id"] is not None or row["event_id"] in invalid
                or w["point_key"] in lifecycle.get("pending_keys",{})):
            continue
        ident=obs["observation_id"]
        check(ident not in selected or selected[ident]==obs,"conflicting ratings need native adjudication")
        selected[ident]=obs
    result=metrics({"first":v,"skipped":False,"switched":False} for v in selected.values())
    return {k:v for k,v in result.items() if k not in {"offered","skipped","switched_to_training"}} | {
            "status":"ready","scope":"formally confirmed V2 first submissions","confirmed_first_submissions":len(selected)}


def report(root,key):
    ms=[r["micro"] for r in attempts(root,key).values() if not r["micro"].get("guided")]
    return dict(scope="reader observations pending native formal confirmation", formal_write_count=0,
                before=metrics(summarize(m,corrected=False) for m in ms),
                current=metrics(summarize(m) for m in ms),corrections=[c for m in ms for c in m["corrections"]])


def projection(repo, root, state, meta):
    m=state["micro"];task=m["task"];fb=m["feedback"];draft=current_draft(m)
    # Choose fields before serialization. Hidden teacher material never reaches DOM, ARIA or network payloads.
    public={"phase":"skipped" if m["skipped"] else "writing","prompt":task["prompt"],"drafts":[
        {k:d[k] for k in ("id","parent","text","sha256")} for d in m["drafts"]],"feedback":None,
        "unsubmitted":m["unsubmitted"],"selected_draft":m.get("selected_draft"),"exposed":[],"reviews":[]}
    if m.get("guided"):
        from .guided_points import read_scope,state_view
        read_scope(repo,root,task)
        view=state_view(state)
        public["guided"]={k:view[k] for k in ("lesson_point_id","status","feedback","pending_response","outbound_id","feedback_seen")}
        q=view["question"];public["guided"]["question"]={k:q[k] for k in ("id","text","status")}
        public["guided"]["question"]["sha256"]=text_sha256(q["text"])
        from .guided_points import enabled
        public["guided"]["enabled"]=enabled(root)
        public["prompt"]=view["question"]["text"] if view["status"]=="needs_practice" else ""
    if task["mode"]=="training" or m["switched"] or draft:
        public["goal"]=task["target_description"]
        if task.get("source_ref"):
            try:
                public["source"]=source_return(repo,root,task["source_ref"])
            except (ValueError,OSError,KeyError,ValidationError):
                public["source_status"]="原句版本或授权已变化"
    if fb:
        public["feedback"]={k:fb[k] for k in ("id","anchor","direction","decision")}
        public["feedback"].update(has_example=bool(fb["example"]),has_rewrite=bool(fb["rewrite"]))
    for e in m["events"]:
        if e.get("visible_text") and (e["kind"]=="help" or fb and e.get("feedback_id")==fb["id"]):
            public["exposed"].append(dict(kind=e["kind"],text=e["visible_text"]))
    if draft:
        for r in m["reviews"]:
            public["reviews"].append(dict(draft_id=r["draft_id"],result=r["target_result"],
                findings=[x["reason"] for x in r["criteria"].values()],language=[x["reason"] for x in r["language_findings"]]))
    return {**meta,"schema":VERSION,"stage":"micro","stage_label":"短写","session_id":state["entity_key"],
            "lesson_id":state["attempt_id"],"course":"考研英语一","section":"短写","goal":"","cards":[],"micro":public}
