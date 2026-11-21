"""User-triggered operations for the installed writing tutor and English daily representative."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

from . import writing_sessions as ws
from .article_workspace import RUNTIME
from .constants import REPO_ROOT
from .errors import PipelineError, ValidationError
from .util import LOCAL_ZONE, atomic_write_json, exclusive_lock, file_sha256, load_json, object_sha256
from .writing_sources import read_source, digest_message, valid
from .writing_coach import HOME, DAYS, context
from .writing_personal import SKILLS

def active_context(repo,state,*,phase="before_attempt",keys=(),shared=(),source=None,unit=None):
    return context(repo,state,phase=phase,keys=keys,shared=shared,source=source,unit=unit)

def classroom_sources(repo,tr):
    """Recover frozen course material at intake without creating records during teaching."""
    from .writing_practice_evidence import display_attempts, material
    practices={aid:material(repo,tr,s) for aid,s in display_attempts(repo,tr).items()}
    if not tr.native or repo.resolve()!=REPO_ROOT.resolve():
        return practices
    from .writing_workspace import folder,lesson_file,spec_for
    revisions=folder(RUNTIME,"WRITING-"+object_sha256(tr.native)[:24])/"revisions"
    sources=dict(practices)
    for path in revisions.glob("*.json"):
        revision=load_json(path)
        attempt=revision.get("attempt_id")
        if not attempt or attempt in sources or not revision.get("source_version") or not revision.get("lesson_id"):
            continue
        spec=spec_for(repo,revision)
        sources[attempt]=dict(task_id=spec["lesson_id"],prompt=spec["prompt"] or spec["goal"],
            origin=dict(kind="course",lesson_sha256=revision["source_version"],display_attempt_id=attempt,source=spec["source"]),
            attachments=[str(lesson_file(repo,revision["source_version"]))])
    return sources

def begin(repo,state,tr,*,task,prompt,origin,first=None,inputs=(),transfer_from=None,new_attempt=False):
    path=ws.directory(state,tr)
    with exclusive_lock(path/"capture.lock"):
        b=ws.read_book(path,tr)
        saved=next((u for u in reversed(b["units"]) if not u["stop"]),None)
    if b.get("paused"):return {"status":"automatic_capture_paused"}
    current=saved and saved["task"]["task_id"]==task and saved["task"]["prompt"]==prompt and tr.messages[-1]["message_id"] in ws.scope_rows(b,tr)
    if current and not new_attempt and not transfer_from:
        u=saved
    else:
        u=ws.register_unit(repo,state,tr,task=task,prompt=prompt,origin=origin,first=first,
                           attachments=inputs,transfer_from=transfer_from)
    cap=ws.checkpoint(repo,state,tr)
    return {"status":cap["status"],"attempt_id":u["attempt_id"],"source_id":ws.SOURCE+u["sha256"][:32],
            "last_message_id":cap.get("last_message_id"),"candidate_count":cap.get("candidate_count",0)}

def projection_home(repo,state):
    from .memory_lifecycle import read_lifecycle
    from .writing_coach import publish_home
    from .util import exclusive_lock
    import sqlite3,contextlib
    with exclusive_lock(state/"memory/.projection.lock"):
        m=load_json(state/"memory/current.json")
        db_path=(state/"memory"/m["database"]).resolve(strict=True)
        valid(db_path.is_relative_to((state/"memory/generations").resolve()) and file_sha256(db_path)==m["database_sha256"],
              "当前英语记忆索引未通过版本核对")
        valid(file_sha256(state/"personal-summary/current.json")==m["summary_index_sha256"],"点摘要与当前记忆索引版本不同")
        with contextlib.closing(sqlite3.connect(db_path.as_uri()+"?mode=ro",uri=True)) as db:
            return publish_home(repo,state,db,m,read_lifecycle(state))

def coverage_state(repo,state,tr):
    folder=ws.directory(state,tr)
    with exclusive_lock(folder/"capture.lock"):
        b=ws.read_book(folder,tr);rows=ws.row_by_id(tr);scope=ws.scope_rows(b,tr);index=ws.evidence_index(b)
        return [{"message_id":mid,"original_sha256":digest_message(m),"role":m["role"],"created_at":m["created_at"],
                 "in_scope":mid in scope,"package":index.get(mid,{}).get("package_id"),"stage":b["annotations"].get(mid,{}).get("stage")}
                for mid,m in rows.items()]

def native_handoff(repo,state,captures,reviewer,*,commit=False):
    from .backlog import build_backlog_plan,execute_backlog_plan
    from .nightly import freeze_nightly
    from .local_review import author_local_day
    days=defaultdict(set)
    for c in captures:days[c["study_date"]].add(c["package_id"])
    if not days:return {"status":"no_writing_units","formal_apply_invocations":[]}
    results=[];complete=True
    for day,ids in sorted(days.items()):
        path,plan=build_backlog_plan(state,repo,cutoff_date=day,package_ids=sorted(ids),
                                     include_legacy_audit=False,local_authoring=True)
        valid(set(plan["selection"]["package_ids"])==ids,"科代表清单超出本次作文范围")
        pending={r["package_id"] for r in plan["packages"] if r["status"]=="pending"}
        if pending:
            manifest,doc=freeze_nightly(state,repo,study_date=day,package_ids=pending,local_review_id=plan["local_review_id"])
            author=author_local_day(state,manifest,reviewer,repo=repo)
            if author["status"]!="LOCAL_ACTIONS_READY_FOR_NATIVE_PREFLIGHT" or not commit:
                results.append(dict(day=day,status="author_required" if author.get("pending_package_ids") else "ready_for_native_commit",
                                    plan=str(path),manifest=str(manifest),author=author))
                complete=False
                break
            compiled=Path(author["compiled_dir"])
        else:compiled=None
        if not commit:
            results.append(dict(day=day,status="readback_required",plan=str(path)));complete=False;continue
        run=execute_backlog_plan(state,repo,plan_path=path,actions_dir=compiled,support_dir=compiled,apply=True,authorization=plan["plan_id"])
        _,after=build_backlog_plan(state,repo,cutoff_date=day,package_ids=sorted(ids),include_legacy_audit=False,persist=False)
        statuses={r["package_id"]:r["status"] for r in after["packages"]}
        done=set(statuses)==ids and all(v=="already_consumed" for v in statuses.values())
        results.append(dict(day=day,status="verified_native" if done else "native_closeout_pending",plan=str(path),
                            package_statuses=statuses,run=run))
        if not done:complete=False;break
    return dict(status="verify_fresh_reader" if complete else "formal_processing_pending",days=results)

def verify_feedback_pages(repo,state,ids):
    from .writing_author import reviewed,verified_writer
    from .writing_feedback import publish_reviewed
    from .memory_lifecycle import read_lifecycle
    lifecycle=read_lifecycle(state)
    dates={p[7:11]+"-"+p[11:13]+"-"+p[13:15] for p in ids}
    expected=set()
    for day in dates:
        for path in (state/"receipts/nightly"/day).glob("*.json"):
            receipt=load_json(path)
            if receipt.get("mode")!="apply" or not ids.intersection(receipt.get("package_ids",[])):
                continue
            decisions={pid:entry for pid,entry in reviewed(state,receipt).items()
                       if pid in ids and entry["review"].get("feedback_reviews")}
            if not decisions:continue
            verified_writer(repo,state,path)
            publish_reviewed(state,receipt,decisions)
            for entry in decisions.values():
                for item in entry["review"]["feedback_reviews"]:
                    if (item["point_key"] in lifecycle.get("pending_keys",{}) or item["point_key"] in lifecycle.get("invalidated_summary_keys",[])
                            or lifecycle.get("global_pending")):continue
                    record=load_json(state/"writing-sessions/feedback-days"/item["study_date"]/(item["comparison_id"]+".json"))
                    if set(record["event_ids"]).intersection(lifecycle.get("invalidated_episode_ids",[])):continue
                    expected.add((item["study_date"],item["comparison_id"]))
    def visible():
        for day,key in expected:
            note=repo/DAYS/(day+".md")
            if not note.is_file() or f"<!-- writing-feedback:{key} -->" not in note.read_text():
                return False
        return True
    if not visible():
        projection_home(repo,state)
        valid(visible(),"正式反馈已提交，日期页的反馈与实际改稿对照尚待完成")
    return len(expected)

def fresh_reader(repo,state,ids):
    """Independent interpreter, same lesson entry. No prior chat context or test-only memory."""
    from .learning_state import load_learning_events,learning_events_path
    rows=[r for r in load_learning_events(learning_events_path(repo)) if r["event"].get("writing_memory") and
          any(ref["package_id"] in ids for ref in r["evidence_refs"])]
    keys=sorted({r["event"]["writing_memory"]["point_key"] for r in rows})
    cmd=[sys.executable,str(REPO_ROOT/"scripts/english_writing.py"),"memory","--repo",str(repo),"--state",str(state),"context"]
    matches=set();generation=None
    feedback_count=verify_feedback_pages(repo,state,ids)
    if not keys:
        p=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        valid(p.returncode==0 and json.loads(p.stdout).get("status") in {"ready","no_verified_writing_points"}
              and not json.loads(p.stdout).get("pending"),"新会话摘要待完成")
        return {"status":"no_new_writing_point","observations":0,"feedback_comparisons":feedback_count}
    for key in keys:
        p=subprocess.run([*cmd,"--key",key],text=True,capture_output=True,timeout=30)
        valid(p.returncode==0,"新会话作文读取不可用")
        doc=json.loads(p.stdout);valid(doc.get("status")=="ready" and not doc["pending"],"正式提交成功，作文摘要或索引仍待完成")
        point=next((r for r in doc["writing_points"] if r["key"]==key),None)
        valid(point,"新会话尚未读回所提交的作文点")
        sha=point["summary_sha256"]
        record=load_json(state/"personal-summary/objects"/(sha+".json"))
        valid(object_sha256(record)==sha,"新会话返回的作文摘要版本失配")
        expected={r["event_id"]:r["record_sha256"] for r in rows if r["event"]["writing_memory"]["point_key"]==key}
        episodes={e["episode_id"]:e.get("formal_event_sha256") for e in record["episodes"]}
        valid(all(episodes.get(i)==h for i,h in expected.items()),"新会话读回的摘要尚未覆盖这次的真实作答")
        matches.add(key)
        generation=doc["generation"]
    valid(set(keys)<=matches,"新会话未读回所提交的作文点")
    valid((repo/HOME).exists() and all((repo/DAYS/(ws.study_day({"created_at":r["event"]["observed_at"]})+".md")).exists() for r in rows),
          "作文日摘要尚未完成")
    return dict(status="verified_fresh_reader",generation=generation,keys=keys,observations=len(rows),feedback_comparisons=feedback_count,
                attempts=len({r["event"]["writing_memory"]["attempt_id"] for r in rows if r["event"]["writing_memory"]["stage"] in {"first_draft","revision","transfer"}}))

def receive(repo,state,tr,args):
    if getattr(tr,"export_nodes",None) and tr.gaps:
        folder=ws.directory(state,tr)/"incomplete-originals"
        doc={"schema":"writing-source-hold/1","source_sha256":tr.export_version,"session_id":tr.session,
             "authored_nodes":tr.export_nodes,"gaps":tr.gaps}
        path=folder/(object_sha256(doc)+".json")
        if not path.exists():atomic_write_json(path,doc)
    folder=ws.directory(state,tr)
    book=ws.read_book(folder,tr)
    snapshot=object_sha256([[m["message_id"],digest_message(m)] for m in tr.messages])
    if not args.units and book.get("inbox_scope")!=snapshot:
        return dict(status="scope_review_required",source=ws.review_source(tr),existing_units=book["units"],
                    formal_write_count=0,detail="科代表核对全文并提交实际课节范围")
    if args.units:
        specs=read_input(args.units)["units"]
        valid(isinstance(specs,list) and bool(specs),"科代表须核实本次实际作文范围")
        for u in specs:
            old=next((x for x in book["units"] if x["attempt_id"]==u.get("attempt_id")),None)
            if old:
                spec=old["task"]
                ws.register_unit(repo,state,tr,task=spec["task_id"],prompt=spec["prompt"],origin=spec["origin"],
                                 first=old["start"],stop=u["last"],include_from=u.get("include_from"),attachments=[i["path"] for i in old["inputs"]],
                                 transfer_from=(old.get("transfer_evidence") or {}).get("package_id") or spec.get("transfer_from"))
            else:
                if u.get("display_attempt_id"):
                    sources=classroom_sources(repo,tr)
                    valid(u["display_attempt_id"] in sources,"本会话没有该课程的已核资料屏记录")
                    u={**u,**sources[u["display_attempt_id"]]}
                prior=u.get("transfer_from")
                if u.get("transfer_from_first"):
                    matched=[x for x in ws.read_book(folder,tr)["units"] if x["start"]==u["transfer_from_first"]]
                    valid(len(matched)==1,"本课前一份作品的起点尚未核实")
                    prior=matched[0]["attempt_id"]
                ws.register_unit(repo,state,tr,task=u["task_id"],prompt=u["prompt"],origin=u["origin"],
                                 first=u["first"],stop=u["last"],attachments=u.get("attachments",[]),transfer_from=prior)
            for mid,stage in u.get("stages",{}).items():
                ws.checkpoint(repo,state,tr,stage=stage,message_id=mid,closing=True,admin_tail=not args.link,only_day=args.day)
            if u.get("candidates"):
                ws.checkpoint(repo,state,tr,candidates=u["candidates"],closing=True,admin_tail=not args.link,only_day=args.day)
        with exclusive_lock(folder/"capture.lock"):
            current=ws.read_book(folder,tr);current["inbox_scope"]=snapshot;ws.persist(folder,current)
    cap=ws.checkpoint(repo,state,tr,closing=True,repair=True,admin_tail=not args.link,only_day=args.day)
    if cap["status"]!="saved":return cap
    if getattr(args,"capture_only",False):
        return {"status":"saved","capture":cap,"formal_write_count":0}
    # A single session link never selects reading or older unrelated unprocessed packages.
    selected=[c for c in cap["packages"] if not args.day or c["study_date"]==args.day]
    listing=native_handoff(repo,state,selected,args.reviewer,commit=args.commit)
    if listing["status"]=="verify_fresh_reader":
        from .writing_practice_evidence import visible_rows
        latest=visible_rows(repo,read_source(args.link,export=args.export))
        basis={m["message_id"]:digest_message(m) for m in tr.messages}
        new_evidence=any(basis.get(m["message_id"])!=digest_message(m) for m in latest.messages if
                         args.link or m["role"]=="user" or m["message_id"] in basis)
        again=ws.checkpoint(repo,state,latest,closing=True,repair=True,admin_tail=not args.link,only_day=args.day)
        live=[c for c in again.get("packages",[]) if not args.day or c["study_date"]==args.day]
        if new_evidence or again["status"]!="saved" or live!=selected:
            listing.update(status="new_evidence_pending",latest=again)
        else:
            readback=fresh_reader(repo,state,{c["package_id"] for c in selected})
            listing.update(status="completed",memory_readback=readback)
    return {"capture":cap,**listing}

def read_input(path):
    return json.load(sys.stdin) if path=="-" else load_json(Path(path))

def _source_options(p):
    p.add_argument("--link");p.add_argument("--export",type=Path)

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo",type=Path,default=REPO_ROOT);p.add_argument("--state",type=Path)
    commands=p.add_subparsers(dest="op",required=True)
    c=commands.add_parser("context");c.add_argument("--key",action="append",default=[]);c.add_argument("--shared",action="append",default=[])
    c.add_argument("--source");c.add_argument("--unit");c.add_argument("--phase",choices=["before_attempt","after_attempt","open_assessment"],default="before_attempt")
    commands.add_parser("home")
    commands.add_parser("regression").add_argument("--cohort",type=Path,required=True)
    i=commands.add_parser("inspect");_source_options(i);i.add_argument("--full",action="store_true")
    i.add_argument("--offset",type=int,default=0);i.add_argument("--limit",type=int)
    b=commands.add_parser("begin");_source_options(b);b.add_argument("--input",required=True);b.add_argument("--save-now",action="store_true")
    f=commands.add_parser("checkpoint");_source_options(f);f.add_argument("--stage",choices=sorted(ws.STAGES));f.add_argument("--message-id")
    f.add_argument("--candidates");f.add_argument("--closing",action="store_true");f.add_argument("--save-now",action="store_true")
    r=commands.add_parser("receive");_source_options(r);r.add_argument("--day");r.add_argument("--units");r.add_argument("--commit",action="store_true")
    r.add_argument("--reviewer",default="当前英语科代表")
    r.add_argument("--capture-only",action="store_true")
    t=commands.add_parser("autosave");_source_options(t);t.add_argument("enabled",choices=["on","off"])
    h=commands.add_parser("action");h.add_argument("--input",required=True)
    feedback=commands.add_parser("feedback");feedback.add_argument("--package-id",required=True);feedback.add_argument("--input",required=True)
    day=commands.add_parser("receive-day");day.add_argument("--day",required=True);day.add_argument("--commit",action="store_true");day.add_argument("--reviewer",default="当前英语科代表")
    q=commands.add_parser("coverage");q.add_argument("--package-id",required=True)
    f=commands.add_parser("readback");f.add_argument("--package-id",action="append",required=True)
    return p

def run(args):
    repo=args.repo.resolve();state=(args.state or repo/"intake").resolve()
    if args.op in {"begin","checkpoint"} and not args.save_now and not getattr(args,"closing",False):
        result={"status":"after_class_import","formal_write_count":0}
        if args.op=="begin":
            return {"context":active_context(repo,state),"scope":result}
        return result
    if args.op=="autosave" and args.enabled=="on":
        return {"status":"after_class_import","formal_write_count":0}
    if args.op=="context":return active_context(repo,state,phase=args.phase,keys=args.key,shared=args.shared,source=args.source,unit=args.unit)
    if args.op=="home":return projection_home(repo,state)
    if args.op=="regression":
        from .writing_history import evaluate
        return evaluate(repo,state,load_json(args.cohort))
    if args.op=="action":
        from .writing_personal import make_action
        return make_action(repo,state,read_input(args.input))
    if args.op=="feedback":
        from .web_review import package_root
        from .writing_feedback import comparison
        return comparison(repo,state,package_root(state,repo,args.package_id),read_input(args.input))
    if args.op=="coverage":
        from .writing_author import coverage_skeleton
        return coverage_skeleton(repo,state,args.package_id)
    if args.op=="readback":return fresh_reader(repo,state,set(args.package_id))
    if args.op=="receive-day":
        from datetime import date
        valid(date.fromisoformat(args.day).isoformat()==args.day,"学习日格式无效")
        results=[]
        for path in sorted((state/"writing-sessions").glob("*/session.json")):
            book=load_json(path)
            if not book.get("units"):continue
            link={"codex":"codex://threads/","t3":"t3://threads/","chatgpt":"https://chatgpt.com/c/"}[book["host"]]+book["session_id"]
            exp=None
            if book["host"]=="chatgpt":
                origin=book["source_locator"]
                if origin.startswith("https://chatgpt.com/share/"):link=origin
                else:exp=Path(origin)
            try:
                from .writing_practice_evidence import visible_rows
                tr=visible_rows(repo,read_source(link,export=exp));scoped=ws.scope_rows(book,tr)
                if not any(ws.study_day(m)==args.day and m["message_id"] in scoped for m in tr.messages):continue
                single=argparse.Namespace(**{**vars(args),"units":None,"link":link,"export":exp})
                results.append(receive(repo,state,tr,single))
            except (OSError,ValueError,PipelineError) as exc:
                results.append({"status":"scope_pending","reason":str(exc),"session_id":book["session_id"]})
        return {"status":"completed" if results and all(r["status"]=="completed" for r in results) else
                ("no_writing_units" if not results else "pending"),"date":args.day,"sessions":results}
    from .writing_practice_evidence import visible_rows
    tr=visible_rows(repo,read_source(args.link,export=args.export))
    if args.op=="inspect":
        out=ws.review_source(tr)
        valid(args.offset>=0 and (args.limit is None or args.limit>0),"消息分页范围无效")
        end=len(tr.messages) if args.limit is None else min(len(tr.messages),args.offset+args.limit)
        out["messages"]=out["messages"][args.offset:end]
        out.update(offset=args.offset,next_offset=end if end<len(tr.messages) else None,eof=end==len(tr.messages))
        if args.full:
            out["available_course_attempts"]=classroom_sources(repo,tr)
            out["messages"]=[{"id":m["message_id"],"role":m["role"],"at":m["created_at"],"text":m["content"],
                    "original_sha256":digest_message(m),"blocks":len(m["metadata"]["original_content_blocks"])} for m in tr.messages[args.offset:end]]
        return out
    if args.op=="begin":
        u=read_input(args.input)
        return {"context":active_context(repo,state,shared=u.get("shared_keys",[])),
                "scope":begin(repo,state,tr,task=u["task_id"],prompt=u["prompt"],origin=u["origin"],first=u.get("first"),
                              inputs=u.get("attachments",[]),transfer_from=u.get("transfer_from"),new_attempt=bool(u.get("new_attempt")))}
    if args.op=="autosave":
        folder=ws.directory(state,tr)
        with exclusive_lock(folder/"capture.lock"):
            book=ws.read_book(folder,tr);book["paused"]=args.enabled=="off"
            if book["paused"] and book["units"] and book["units"][-1]["stop"] is None:
                book["units"][-1]["stop"]=tr.messages[-1]["message_id"]
            ws.persist(folder,book)
        return {"status":"automatic_capture_paused" if book["paused"] else "automatic_capture_enabled"}
    if args.op=="checkpoint":
        mid=args.message_id
        if args.stage and not mid:
            role="assistant" if args.stage=="feedback" else "user"
            mid=next((m["message_id"] for m in reversed(tr.messages) if m["role"]==role),None)
        candidates=read_input(args.candidates)["candidates"] if args.candidates else []
        return ws.checkpoint(repo,state,tr,stage=args.stage,message_id=mid,candidates=candidates,closing=args.closing,repair=args.closing,admin_tail=not args.link)
    return receive(repo,state,tr,args)

def main(argv=None):
    try:
        result=run(parser().parse_args(argv));print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (OSError,ValueError,KeyError,TypeError,PipelineError,subprocess.SubprocessError) as exc:
        print(json.dumps({"status":"pending","reason":str(exc)},ensure_ascii=False));return 2
