"""Native video-point commands used by the installed course coach and existing reader."""
import argparse
import json
import os
from pathlib import Path
import sys
from uuid import UUID, uuid5, NAMESPACE_URL

from . import guided_points as gp, writing_workspace as ws, writing_practice as practice
from .constants import REPO_ROOT
from .errors import PipelineError
from .article_workspace import RUNTIME
from .util import atomic_write_json, file_sha256, load_json, object_sha256, text_sha256


def workspace_key(session):
    return "WRITING-"+object_sha256(session)[:24]


def continuation(root,session):
    return root/"writing"/"continuations"/(object_sha256(session)+".json")


def selected(root,session):
    path=continuation(root,session)
    if path.is_file():
        ref=load_json(path);s=ws.current(root,ref["workspace"])
        if s["attempt_id"]==ref["attempt_id"]:return ref["workspace"]
    return workspace_key(session)


def coach_contract():
    folder=Path(os.environ.get("KAOYAN_ENGLISH_ACTIVE_SKILL",Path.home()/".codex/skills/kaoyan-english-intensive-reading"))
    ref=folder/"references/guided-course-points.md"
    return dict(instructions=ref.read_text(),instructions_sha256=file_sha256(ref))


def latest_request(repo,root,key,session):
    from .writing_sources import read_source,META
    tr=read_source(session);s=ws.current(root,key)
    msg=next((m for m in reversed(tr.messages) if m["role"]=="user" and not m["content"].lstrip().startswith(META)),None)
    gp.check(msg is not None,"no original learner message")
    latest=next((ev for ev in reversed(s["micro"]["events"]) if ev.get("original_message_id")==msg["message_id"] and
                 ev.get("original_session") in {session,tr.session}),None)
    if latest:
        gp.check(latest["original_sha256"]==text_sha256(msg["content"]),"source message edited; preserve new version through Capture")
        return {"already_reviewed":True,"point":gp.state_view(s)}
    q=gp.current_question(s["micro"]["guided"])
    idx=next(i for i,row in enumerate(tr.messages) if row["message_id"]==msg["message_id"])
    sender=next((row for row in reversed(tr.messages[:idx]) if row["role"]=="assistant"),None)
    if sender:
        view=gp.state_view(s)
        show=view["outbound_id"] and not view["feedback_seen"] and view["feedback"] in sender["content"]
        from datetime import datetime
        parse=lambda v:datetime.fromisoformat(v.replace("Z","+00:00"))
        ask=q["status"]=="planned" and q["text"] in sender["content"] and parse(sender["created_at"])>=parse(q.get("created_at",s["micro"]["opened_at"]))
        if show or ask:
            posted=dict(kind="question_seen" if ask else "feedback_seen",attempt_id=s["attempt_id"],revision=s["revision"],
                        action_id=str(uuid5(NAMESPACE_URL,object_sha256([s["attempt_id"],q["id"],sender["message_id"],"seen"]))))
            if ask:posted.update(question_id=q["id"],question_sha256=text_sha256(q["text"]))
            if show:posted["feedback_id"]=view["outbound_id"]
            practice.operate(repo,root,key,posted,coach=True,native={"_message_id":sender["message_id"],"_native_at":sender["created_at"],"_channel":"chat"})
            s=ws.current(root,key)
    return dict(attempt_id=s["attempt_id"],revision=s["revision"],question_id=q["id"],
                action_id=str(uuid5(NAMESPACE_URL,object_sha256([s["attempt_id"],tr.key,msg["message_id"],text_sha256(msg["content"])]))),
                message=dict(link=session,message_id=msg["message_id"],sha256=text_sha256(msg["content"])))


def run(args):
    repo=args.repo.resolve();root=(args.runtime or (RUNTIME if repo==REPO_ROOT.resolve() else repo/".writing-reader")).resolve()
    gp.check((repo==REPO_ROOT.resolve())==(root==RUNTIME.resolve()),"isolated and installed roots cannot be mixed")
    if args.op=="feature":
        if args.setting:atomic_write_json(root/"guided-points-feature.json",{"enabled":args.setting=="on"})
        return {"enabled":gp.enabled(root),"formal_write_count":0}
    if args.op=="binding":
        from . import writing_publication
        skill=Path(os.environ.get("KAOYAN_ENGLISH_ACTIVE_SKILL",Path.home()/".codex/skills/kaoyan-english-intensive-reading"))
        paths=[Path(gp.__file__),Path(practice.__file__),Path(__file__),Path(writing_publication.__file__),
               skill/"SKILL.md",skill/"scripts/english_skill.py",skill/"references/guided-course-points.md"]
        return dict(enabled=gp.enabled(root),schema=gp.SCHEMA,interpreter=sys.executable,
                    loaded=[dict(path=str(p.resolve()),sha256=file_sha256(p)) for p in paths],formal_write_count=0)
    if args.op=="query":
        from .lesson_point_memory import query_points
        return query_points(repo/"intake",args.point_id,args.max_bytes)
    gp.check(args.session,"missing current conversation")
    key=selected(root,args.session)
    if args.op not in {"start","query"}:
        chosen=args.workspace if args.op=="resume" and args.workspace else key
        current=ws.current(root,chosen)
        gp.read_scope(repo,root,current.get("micro",{}).get("task",{}))
    if args.op=="resume":
        if args.workspace:
            s=ws.current(root,args.workspace)
            gp.check(s.get("micro",{}).get("guided") and s["attempt_id"]==args.attempt_id,"resume requires the exact prior practice")
            check_current=ws.folder(root,workspace_key(args.session))/"current.json"
            if check_current.exists():
                own=load_json(check_current)
                gp.check(own.get("micro",{}).get("task",{}).get("mode")!="open" and
                         (own.get("practice") or {}).get("phase")!="transfer","protected assessment active")
            atomic_write_json(continuation(root,args.session),dict(workspace=args.workspace,attempt_id=args.attempt_id))
            key=args.workspace
        return gp.state_view(ws.current(root,key))
    if args.op=="read":
        if args.workspace and args.attempt_id:
            snap=practice.attempts(root,args.workspace).get(args.attempt_id)
            gp.check(snap and snap["micro"].get("guided"),"point attempt not found")
            gp.read_scope(repo,root,snap["micro"]["task"])
            return gp.state_view(snap)
        return gp.state_view(ws.current(root,key))
    if args.op=="packet":
        gp.check(gp.enabled(root),"guided_points_disabled")
        state=ws.current(root,key)
        raw=state["micro"]["guided"].get("incoming")
        package=gp.unclassified(state,raw) if raw else gp.packet(state)
        return {**package,**coach_contract()}
    gp.check(gp.enabled(root),"guided_points_disabled")
    req=latest_request(repo,root,key,args.session) if args.op=="turn" and args.latest else json.load(sys.stdin)
    if req.get("already_reviewed"):return req
    if args.op=="next":
        s=ws.current(root,key);m=s["micro"];t=m["task"];p=t["course_point"]
        if m["guided"]["practice_id"]==object_sha256([req["attempt_id"],p["lesson_point_id"]])[:32]:
            parent=practice.attempts(root,key).get(req["attempt_id"])
            gp.check(parent and parent["revision"]==req["revision"],"stale predecessor")
            return {"status":"already_advanced","point":gp.state_view(s)}
        gp.check(req["attempt_id"]==s["attempt_id"] and req["revision"]==s["revision"] and
                 m["guided"]["status"] in {"ready_for_next","skipped_by_user"},"current point is not complete or skipped")
        if not p["next_point"]:return {"status":"next_point_unknown","message":"请继续视频，把下一个知识点带过来。"}
        lesson=load_json(ws.lesson_file(repo,p["source"]["lesson_sha256"]))
        entry=next(r for r in lesson["points"] if r["lesson_point_id"]==p["next_point"])
        req={**entry,"lesson_id":p["lesson_id"],"source":{"kind":"registered_lesson","lesson_sha256":p["source"]["lesson_sha256"]},
             "practice_id":object_sha256([s["attempt_id"],p["next_point"]])[:32],"revision":s["revision"]}
    if args.op in {"start","next"}:
        if args.workspace:gp.check(args.workspace==key,"start in current course workspace")
        result=gp.begin(repo,root,key,req,args.session)
        s=ws.current(root,key)
        if result["attempt_id"]==s["attempt_id"]:
            answer={**result,"point":gp.state_view(s)}
            if args.present and repo==REPO_ROOT.resolve():
                from scripts.english_writing import call_owner,deliver
                try:
                    answer["layout"]=call_owner("enter",{"entity_key":key})
                    answer["display"]=deliver(root,key,s,True)
                except (ValueError,OSError,PipelineError) as exc:
                    answer["display"]={"status":"pending","detail":str(exc)}
            return answer
        return result
    s=ws.current(root,key)
    request_sha=object_sha256([args.op,req])
    previous=next((ev for ev in s.get("micro",{}).get("events",[]) if ev["id"]==req.get("action_id") and ev.get("client_request_sha256")),None)
    if previous and args.op!="turn":
        gp.check(previous["client_request_sha256"]==request_sha,"point action ID conflict")
        return {"status":"saved","id":previous["id"],"revision":s["revision"],"point":gp.state_view(s)}
    gp.check(s.get("micro",{}).get("guided"),"no active guided point")
    if args.op in {"turn","apply"} or args.op=="decide" and s["micro"]["guided"].get("incoming"):
        gp.check(req["attempt_id"]==s["attempt_id"],"wrong practice")
        q=gp.current_question(s["micro"]["guided"]);ref=req.get("message")
        if ref:
            msg=gp.native_message(ref,"user",args.session)
            from .writing_sources import read_source
            original_tr=read_source(ref["link"])
            raw=dict(id=msg["message_id"],session=original_tr.session,text=msg["content"],sha256=text_sha256(msg["content"]),
                     medium=req.get("medium","submitted_text"),verification=req.get("verification","as_submitted"),at=msg["created_at"],question_id=req["question_id"])
            if raw["verification"]=="checked_original":
                from .writing_sources import read_source
                from .writing_sessions import attachments_for
                from tempfile import TemporaryDirectory
                gp.check(raw["medium"] in {"ocr","mixed","handwritten"} and req.get("checked_by")=="current_tutor",
                         "verified original requires an explicit original-image review")
                tr=read_source(ref["link"])
                with TemporaryDirectory() as td:
                    files,missing=attachments_for(tr,[{**msg,"metadata":dict(msg["metadata"]),"attachment_refs":[]}],Path(td))
                    gp.check(not missing and any(file_sha256(Path(f["path"]))==req.get("attachment_sha256") for f in files),
                             "the checked original must belong to this learner message")
                raw.update(original_verified=True,attachment_sha256=req["attachment_sha256"])
            else:
                gp.check(raw["verification"] in {"as_submitted","unverified"},"invalid source verification")
            if q.get("channel")=="chat" and not (args.op=="apply" and s["revision"]!=req["revision"]):
                from .writing_sources import read_source
                rows=read_source(ref["link"]).messages;pos={r["message_id"]:i for i,r in enumerate(rows)}
                sent=pos.get(q.get("message_id"));idx=pos[msg["message_id"]]
                known={ev["visible_text"] for ev in s["micro"]["events"] if ev.get("visible_text") and ev.get("kind") in {"point_turn","point_control","point_decision"}}
                raw["answer_follows_question"]=sent is not None and sent<idx and all(
                    r["role"]!="assistant" or r["content"] in known for r in rows[sent+1:idx])
        else:
            raw=s["micro"]["guided"].get("incoming")
            gp.check(raw,"missing original message or reader submission")
        if args.op=="turn":
            gp.check(s["revision"]==req["revision"],"stale turn")
            result={**gp.unclassified(s,raw),**coach_contract()}
            if args.latest:result["apply_request"]=req
            return result
        if req["decision"]["kind"]=="answer":
            gp.check(raw.get("answer_follows_question",True),"answer is not bound to the latest delivered question")
        posted={k:req[k] for k in ("attempt_id","revision","action_id","packet_sha256","decision")}
        result=practice.operate(repo,root,key,{**posted,"kind":"point_turn"},coach=True,native={"_turn":raw,"_client_request_sha256":request_sha})
        return {**result,"point":gp.state_view(ws.current(root,key))}
    base={"attempt_id":req["attempt_id"],"revision":req["revision"],"action_id":req["action_id"]}
    native=None
    if args.op in {"delivered","feedback-seen","answer","control"}:
        message=gp.native_message(req["message"],"assistant" if args.op in {"delivered","feedback-seen"} else "user",args.session)
        native={"_message_id":message["message_id"],"_native_at":message["created_at"],"_session":args.session,"_channel":"chat","_original_text":message["content"]}
        q=gp.current_question(s["micro"]["guided"])
        if args.op in {"delivered","feedback-seen"} and req.get("feedback_id"):
            ev=next((v for v in s["micro"]["events"] if v["id"]==req["feedback_id"]),{})
            gp.check(ev.get("prepared_feedback") and ev["prepared_feedback"] in message["content"],"feedback has not actually been sent")
            base["feedback_id"]=req["feedback_id"]
        if args.op=="feedback-seen":
            base["kind"]="feedback_seen"
        elif args.op=="delivered":
            gp.check(q["text"] in message["content"] and req["question_id"]==q["id"],"the actual sent message must contain the current question")
            from datetime import datetime
            earliest=q.get("created_at",s["micro"]["opened_at"])
            gp.check(datetime.fromisoformat(message["created_at"].replace("Z","+00:00"))>=datetime.fromisoformat(earliest.replace("Z","+00:00")),
                     "message predates the planned question")
            base.update(kind="question_seen",question_id=q["id"],question_sha256=text_sha256(q["text"]))
        elif args.op=="answer":
            if q.get("channel")=="chat":
                from .writing_sources import read_source
                messages=read_source(req["message"]["link"]).messages
                positions={m["message_id"]:i for i,m in enumerate(messages)}
                start=positions.get(q.get("message_id"));end=positions[message["message_id"]]
                gp.check(start is not None and start<end and not any(m["role"]=="assistant" for m in messages[start+1:end]),
                         "this answer does not follow the delivered question")
            previous=next((d for d in s["micro"]["drafts"] if d.get("native_message_id")==message["message_id"] and d.get("native_session")==args.session),None)
            if previous:
                gp.check(previous["sha256"]==text_sha256(message["content"]) and previous["question_id"]==req["question_id"],
                         "changed or differently bound original; retain original through versioned Capture")
                return {"status":"already_submitted","response_id":previous["id"]}
            base.update(kind="submit",text=message["content"],base_draft=req.get("base_draft"),question_id=req["question_id"],
                        medium=req.get("medium","submitted_text"),verification=req.get("verification","as_submitted"))
        else:
            base.update(kind="point_control",control=req["control"])
            for k in ("explanation","support"):
                if k in req:base[k]=req[k]
    elif args.op=="decide":
        base.update(kind="point_decision",packet_sha256=req["packet_sha256"],decision=req["decision"])
    result=practice.operate(repo,root,key,base,coach=args.op!="answer",native={**(native or {}),"_client_request_sha256":request_sha})
    return {**result,"point":gp.state_view(ws.current(root,key))}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("op",choices=("binding","feature","start","next","read","resume","packet","turn","apply","answer","decide","delivered","feedback-seen","control","query"))
    p.add_argument("--repo",type=Path,default=REPO_ROOT);p.add_argument("--runtime",type=Path)
    p.add_argument("--set",dest="setting",choices=("on","off"))
    p.add_argument("--present",action="store_true");p.add_argument("--latest",action="store_true")
    p.add_argument("--session",default=os.environ.get("CODEX_THREAD_ID"));p.add_argument("--workspace");p.add_argument("--attempt-id")
    p.add_argument("--point-id");p.add_argument("--max-bytes",type=int,default=6144)
    try:
        print(json.dumps(run(p.parse_args(argv)),ensure_ascii=False));return 0
    except (OSError,KeyError,TypeError,ValueError,PipelineError) as exc:
        print(json.dumps(dict(status="unavailable",detail=str(exc)),ensure_ascii=False));return 1
