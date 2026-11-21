#!/usr/bin/env python3
"""Native course-led writing entry. UI actions and display state never submit learning events."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from english_pipeline import writing_workspace as ws
from english_pipeline.article_present import display_home, manifest, register, present, chat_link
from english_pipeline.article_service import context, ensure
from english_pipeline.article_workspace import RUNTIME, metadata
from english_pipeline.errors import PipelineError, ValidationError
from english_pipeline.util import load_json, object_sha256, atomic_write_json, exclusive_lock
from english_pipeline.writing_contract import source_id
from english_pipeline.writing_materials import locate, EDITION

def call_owner(action, payload):
    ready = manifest(display_home()) or {}
    argv = ready.get("writing_argv")
    if not argv:
        raise ValidationError("writing_layout_not_installed")
    run = subprocess.run([*argv,action], input=json.dumps(payload), text=True,capture_output=True,timeout=35)
    try:
        result = json.loads(run.stdout)
    except ValueError as exc:
        raise ValidationError("writing layout did not return a receipt") from exc
    if run.returncode or result.get("errors"):
        raise ValidationError("writing layout: "+str(result))
    return result

def deliver(root, key, state, explicit):
    rec = ensure(REPO, RUNTIME)
    ready = manifest(display_home())
    register(rec, display_home(), ready)
    if explicit:
        if not ready:
            raise ValidationError("display_not_registered")
        result = subprocess.run(ready["html_argv"],input=json.dumps({"subject":"english","entity_key":key}),
            text=True,capture_output=True,timeout=40)
        return json.loads(result.stdout)
    return present(rec,RUNTIME,metadata(state),mode="update_only",operation_id="writing-"+uuid4().hex)

def close_unit(repo, root, key, args):
    """Capture once, then hand the exact package to existing scoped formal authoring."""
    s = ws.current(root,key)
    spec = ws.spec_for(repo,s)
    if s["status"] != "active" or args.synthetic:
        raise ValidationError("capture requires an active real course unit")
    frozen = ws.lesson_file(repo, s["source_version"])
    cmd = [sys.executable,str(repo/"scripts/capture_current_english.py"),"--source-id",source_id(spec),
           "--source-hash",s["source_version"],"--unit-id",spec["lesson_id"],"--source-text",spec["prompt"] or spec["goal"],
           "--source-kind","explanation","--answer-exposure","protected","--attachment","other_attachment:"+str(frozen),
           "--start-line",str(args.start_line),"--end-line",str(args.end_line)]
    for turn in args.turn_id:
        cmd.extend(["--turn-id",turn])
    for path in sorted((ws.folder(root,key)/"revisions").glob("*.json"), key=lambda p:int(p.stem)):
        version = load_json(path)
        if version["attempt_id"]==s["attempt_id"] and version["source_version"]==s["source_version"]:
            cmd.extend(["--attachment","other_attachment:"+str(path)])
            if version.get("page_release"):
                image=root/"writing-pages"/(version["page_release"]["sha256"]+".webp")
                if str(image) not in cmd:cmd.extend(["--attachment","explanation_image:"+str(image)])
    binding=ws.folder(root,key)/"captures"/(s["attempt_id"]+".json")
    with exclusive_lock(binding.with_suffix(".lock")):
        if binding.is_file():
            saved=load_json(binding)
            if saved["bounds"] != [args.start_line,args.end_line,args.turn_id]:
                raise ValidationError("unit already has a Capture intent; use the original bounds or a native supplement")
            cmd=saved["command"]
        else:
            saved={"bounds":[args.start_line,args.end_line,args.turn_id],"command":cmd}
            atomic_write_json(binding,saved)
        if saved.get("receipt"):
            r=saved["receipt"]
        else:
            cap=subprocess.run(cmd,capture_output=True,text=True)
            if cap.returncode:raise ValidationError("capture pending: "+(cap.stdout+cap.stderr)[-1100:])
            r=json.loads(cap.stdout)
            atomic_write_json(binding,{**saved,"receipt":r})
    pid = r.get("package_id") or r.get("receipt",{}).get("package_id")
    if not pid:raise ValidationError("read the saved native Capture receipt before continuing")
    planning = subprocess.run([sys.executable,str(repo/"scripts/english_learning_pipeline.py"),"plan-local-intake",
                      "--package-id",pid,"--no-legacy-audit"],capture_output=True,text=True)
    plan=json.loads(planning.stdout) if not planning.returncode else {}
    formal_status="check_original_closeout" if plan.get("status")=="NOOP" else "awaiting_native_authorship"
    return {"capture":r, "package_id":pid,"formal_status":formal_status,
            "plan":json.loads(planning.stdout) if not planning.returncode else None,
            "planning_error":(planning.stderr or planning.stdout)[-400:] if planning.returncode else None}

def readback(repo,root,key,path):
    from english_pipeline.packages import validate_canonical_writer_closeout, frozen_package_overrides
    from english_pipeline.publication import complete_formal_closeout
    state=repo/"intake"
    receipt=load_json(path)
    manifest=state/"nightly"/path.parent.name/(receipt["batch_id"]+".manifest.json")
    overrides=frozen_package_overrides(state,repo,load_json(manifest))
    receipt, _=validate_canonical_writer_closeout(state,path,expected_manifest_path=manifest,package_root_overrides=overrides)
    display=ws.current(root,key)
    bindings=[]
    for intent in (ws.folder(root,key)/"captures").glob("*.json"):
        capture=load_json(intent).get("receipt",{})
        if capture.get("package_id") in receipt["package_ids"]:
            bindings.append((intent.stem,capture))
    if len(bindings)!=1:
        raise ValidationError("receipt must identify exactly one saved unit in this writing session")
    attempt,capture=bindings[0]
    quality_path=state/"receipts/local-quality"/(receipt["receipt_id"]+".json")
    quality=load_json(quality_path) if quality_path.is_file() else {}
    if quality.get("status")!="VERIFIED_CURRENT":
        quality=complete_formal_closeout(repo,state,receipt)["local_quality"]
    result={"contract":ws.VERSION,"capture_id":capture["package_id"],
            "writer_id":receipt["receipt_id"],"writer_status":receipt["status"],"native_status":quality.get("status"),
            "memory":quality.get("memory"),"writing_view":quality.get("writing_view"),
            "pending_component":quality.get("pending_component"),"formal_apply_invocation_count":0}
    if display["attempt_id"]==attempt and display["status"]=="active" and display["stage"]=="review":
        spec=ws.spec_for(repo,display)
        if ws.template_cards(repo,spec)!=display["templates"]:
            display=ws.update(repo,root,key,display["revision"],"review",display["notes"],
                              page_permission=(display.get("page_release") or {}).get("permission"))
        result["display"]=deliver(root,key,display,False)
    return result

def run(args):
    if args.action == "today":
        from english_pipeline.writing_plan import current_plan
        return current_plan(REPO,args.date)
    if args.action == "bookmark":
        from english_pipeline.writing_plan import record_bookmark
        return record_bookmark(REPO,args.unit,args.state,args.quote,args.position)
    if args.action == "links":
        return ws.links(REPO,asset_id=args.asset_id,source=args.source_id,unit=args.unit)
    if args.action == "page":
        return locate(REPO,args.edition,args.source,args.page)
    thread = os.getenv("CODEX_THREAD_ID")
    if not thread:raise ValidationError("missing actual Codex session")
    key = ("SYN-" if args.synthetic else "")+"WRITING-"+object_sha256(thread)[:24]
    repo,root = context(REPO,RUNTIME,key)
    if args.action in {"status","template"}:
        path = repo/"intake/writing-view/current.json"
        template = load_json(path) if path.exists() else {"template_revision":0,"modules":[],"status":"no_writing_observation"}
        if args.action=="template":
            return {"contract":ws.VERSION,"template_revision":len(template.get("history",[])),"modules":template["modules"]}
        if not (ws.folder(root,key)/"current.json").exists():
            return {"status":"course_not_started","session_id":key,"template_revision":len(template.get("history",[]))}
        s = ws.current(root,key)
        return {**metadata(s),"status":s["status"],"stage":s["stage"],"lesson_id":s["lesson_id"],
                "template_revision":len(template.get("history",[]))}
    coaching={}
    native_lesson=args.action in {"open","start-today","resume"} and not args.synthetic
    if native_lesson:
        from english_pipeline.writing_workflow import active_context
        current = ws.current(root,key) if (ws.folder(root,key)/"current.json").is_file() else {}
        protected = ((current.get("micro") or {}).get("task",{}).get("mode")=="open"
                     or (current.get("practice") or {}).get("phase")=="transfer")
        coaching["lesson_context"]=active_context(repo,repo/"intake",phase="open_assessment" if protected else "before_attempt")
        coaching["capture_scope"]={"status":"after_class_import","formal_write_count":0}
    if args.action in {"start-today","resume"}:
        from english_pipeline.guided_point_cli import selected
        from english_pipeline.guided_points import enabled,state_view
        guided_key=selected(root,thread) if not args.synthetic else key
        current_path=ws.folder(root,guided_key)/"current.json"
        saved=load_json(current_path) if current_path.is_file() else {}
        if args.action=="start-today" and saved.get("status")=="active" and (
                saved.get("micro",{}).get("task",{}).get("mode")=="open" or (saved.get("practice") or {}).get("phase")=="transfer"):
            return {"status":"open_assessment_active","entity_key":guided_key,"answer_view":"current_prompt_only","formal_write_count":0}
        if saved.get("micro",{}).get("guided") and not enabled(root) and args.action=="resume":
            args.action="start-today"
        if enabled(root) and saved.get("micro",{}).get("guided"):
            from english_pipeline.guided_points import read_scope
            read_scope(repo,root,saved["micro"]["task"])
            return {**coaching,"status":"resume_guided_course_point","point":state_view(saved)}
    if args.action=="start-today":
        from english_pipeline.writing_plan import current_plan
        plan=current_plan(repo)
        if plan["status"]!="planned":return {**coaching,**plan}
        if plan["writing"]["mode"]!="course":
            writing=plan["writing"]
            tasks=writing.get("tasks") or [{"part":part,"year":writing["year"]} for part in ("B","A")]
            first=getattr(args,"part","B")
            if native_lesson:
                from english_pipeline.evening_feedback import writing_prompt
                task=next(t for t in tasks if t["part"]==first)
                coaching["current_prompt"]=writing_prompt(repo,task["year"],first,"I")
            return {**coaching,"status":"daily_essay_practice","writing":writing,"first_part":first,
                    "prompt_requests":[{"paper":"I","year":t["year"],"part":t["part"]} for t in tasks],
                    "prompt_entry":"query-writing-prompt","answer_view":"question_only"}
        cursor=plan["resume_course"]
        if not cursor:return {**coaching,"status":"course_watch_complete","today":plan["writing_label"]}
        from english_pipeline.guided_points import enabled
        if enabled(root):
            coaching["point_entry"]={"teaching_mode":"guided_course_point","command":"english_writing.py point start",
                                    "when_unknown":"继续视频，把当前老师讲到的一个知识点告诉我。"}
        coaching["resume_course"]={k:cursor[k] for k in ("unit","playlist","number","title","position")}
        goal="跟随老师学习"+cursor["title"]+"。"
        if cursor["position"] is not None:
            from english_pipeline.writing_plan import duration
            goal=f'从 {duration(cursor["position"])} 续看《{cursor["title"]}》。'
        request=dict(course="27 考研英语一"+cursor["playlist"],section=cursor["title"],lesson_id="WC27-"+cursor["unit"],
                     goal=goal,task_profile=cursor["task_profile"],source_id=cursor["source_id"],
                     source_sha256=cursor["source_sha256"],pdf_page=cursor["reference_pages"][0],prompt="")
        # Full teaching pages are released separately as the learner reaches them.
        prev=ws.current(root,key) if (ws.folder(root,key)/"current.json").is_file() else None
        if prev and prev["lesson_id"]==request["lesson_id"] and prev["status"] in ("active","suspended"):
            state=ws.resume(root,key)
        else:
            state=ws.enter(repo,root,key,request,expected=prev["revision"] if prev else None)
        if enabled(root):
            frozen=ws.spec_for(repo,state)
            if frozen.get("points"):
                from english_pipeline.guided_points import begin,state_view
                entry=frozen["points"][0]
                result=begin(repo,root,key,{**entry,"lesson_id":frozen["lesson_id"],"revision":state["revision"],
                             "practice_id":object_sha256([state["attempt_id"],entry["lesson_point_id"]])[:32],
                             "source":{"kind":"registered_lesson","lesson_sha256":state["source_version"]}},thread)
                return {**coaching,**result,"point":state_view(ws.current(root,key))}
    elif args.action=="preview":
        state=ws.standby(repo,root,key)
    elif args.action=="open":
        request = json.load(sys.stdin)
        state = ws.enter(repo,root,key,request,expected=args.revision)
    elif args.action=="resume":
        state = ws.resume(root,key)
    elif args.action=="update":
        req=json.load(sys.stdin)
        state=ws.update(repo,root,key,args.revision,req["stage"],req.get("notes",[]),page_permission=req.get("page_permission"),practice=req.get("practice"))
        return {**coaching,"saved":metadata(state),"display":deliver(root,key,state,False)}
    elif args.action in {"events","ack"}:
        return {**coaching,**ws.actions(root,key,ack=getattr(args,"action_id",None))}
    elif args.action=="readback":
        return readback(repo,root,key,args.writer_receipt)
    elif args.action=="capture":
        return close_unit(repo,root,key,args)
    elif args.action=="exit":
        ws.pause(root,key)
        return {**coaching,**call_owner("exit",{"entity_key":key})}
    else:
        raise ValidationError("unknown writing action")
    rec=ensure(REPO,RUNTIME)
    ready=manifest(display_home())
    register(rec,display_home(),ready)
    layout_req = {"entity_key":key}
    if args.chat_id:layout_req["chat_id"]=args.chat_id
    if args.chat_title:layout_req["chat_title"]=args.chat_title
    if args.video_id:
        if not ws.spec_for(repo,state)["video"]:raise ValidationError("video is not bound to this course unit")
        layout_req["video_id"]=args.video_id
    try:
        layout=call_owner("enter",layout_req)
        return {**coaching,"saved":metadata(state),"layout":layout,"display":deliver(root,key,state,True)}
    except (OSError,ValueError,PipelineError,subprocess.SubprocessError) as exc:
        return {**coaching,"saved":metadata(state),"display":{"status":"pending","reason":str(exc),
                "resume":"resume this same unit","chat_url":chat_link(RUNTIME,key,ready)}}

def main():
    if sys.argv[1:2]==["point"]:
        from english_pipeline.guided_point_cli import main as point_main
        return point_main(sys.argv[2:])
    if sys.argv[1:2]==["micro"]:
        from english_pipeline.writing_practice_cli import main as micro_main
        return micro_main(sys.argv[2:])
    if sys.argv[1:2]==["memory"]:
        from english_pipeline.writing_workflow import main as memory_main
        return memory_main(sys.argv[2:])
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--synthetic",action="store_true",help="isolated UI QA, native Capture disabled")
    sub=p.add_subparsers(dest="action",required=True)
    for name in ("open","resume","preview","start-today"):
        a=sub.add_parser(name);a.add_argument("--chat-id",type=int);a.add_argument("--chat-title")
        a.add_argument("--video-id",type=int);a.add_argument("--revision",type=int)
    sub.choices["start-today"].add_argument("--part",choices=["A","B"],default="B")
    u=sub.add_parser("update");u.add_argument("--revision",type=int,required=True)
    for n in ("exit","status","events","template"):sub.add_parser(n)
    sub.add_parser("ack").add_argument("action_id")
    sub.add_parser("readback").add_argument("--writer-receipt",type=Path,required=True)
    c=sub.add_parser("capture");c.add_argument("--start-line",type=int,required=True);c.add_argument("--end-line",type=int,required=True)
    c.add_argument("--turn-id",action="append",default=[])
    sub.add_parser("today").add_argument("--date",help="Preview an ISO calendar date without advancing progress")
    mark=sub.add_parser("bookmark");mark.add_argument("unit");mark.add_argument("state",choices=["paused","watched"])
    mark.add_argument("--quote",required=True);mark.add_argument("--position",type=int,help="actual paused second")
    links=sub.add_parser("links");links.add_argument("--asset-id");links.add_argument("--source-id");links.add_argument("--unit")
    page=sub.add_parser("page");page.add_argument("source");page.add_argument("page",type=int);page.add_argument("--edition",default=EDITION)
    args=p.parse_args()
    try:
        value=run(args);print(json.dumps(value,ensure_ascii=False,indent=2))
        return int(value.get("display",{}).get("status") in ("unavailable","pending"))
    except (OSError,KeyError,TypeError,ValueError,PipelineError,subprocess.SubprocessError) as exc:
        print(json.dumps({"status":"pending","detail":str(exc)},ensure_ascii=False));return 1
if __name__=="__main__":
    raise SystemExit(main())
