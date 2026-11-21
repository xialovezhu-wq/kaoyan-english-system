"""Safe, bounded lesson context and the learner's managed writing home."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import json
import re

from .memory_query import query_memory_context
from .util import LOCAL_ZONE, atomic_write_text, canonical_bytes, exclusive_lock, file_sha256, load_json, object_sha256
from .writing_personal import SKILLS, VERSION
from .writing_sources import valid

HOME=Path("wiki/writing/个人作文学习摘要.md")
DAYS=Path("wiki/writing/每日作文摘要")
OPEN="<!-- english-writing-profile:begin -->"
END="<!-- english-writing-profile:end -->"

def recent_capture_context(state):
    """Offer at most two recent original locators when formal teaching memory is missing or pending."""
    from .writing_sessions import SCHEMA
    today=datetime.now(LOCAL_ZONE).date()
    found={}
    for day in (today,today-timedelta(days=1)):
        folder=state/"packages"/day.isoformat()
        roots=[]
        for root in folder.glob("EN-PKG-*"):
            try:
                roots.append((root.stat().st_mtime_ns,root))
            except FileNotFoundError:
                continue
        for _,root in sorted(roots,reverse=True)[:32]:
            try:
                source_path=root/"source.json"
                src=load_json(source_path)["identity"]
                binding=src.get("writing_session",{})
                if binding.get("schema")!=SCHEMA or binding["attempt_id"] in found:
                    continue
                receipt=load_json(root/"receipt.json")
                manifest=load_json(root/"manifest.json")
                if (receipt["package_id"]!=root.name or receipt["package_sha256"]!=manifest["package_canonical_sha256"]
                        or receipt["manifest_sha256"]!=file_sha256(root/"manifest.json")
                        or manifest["source_identity"]!=src):
                    continue
                found[binding["attempt_id"]]=dict(status="capture_available",study_date=receipt["study_date"],
                    attempt_id=binding["attempt_id"],package_id=root.name,package_sha256=receipt["package_sha256"],
                    stages=sorted({m["stage"] for m in binding.get("observed_stages",{}).values()}))
                if len(found)==2:
                    return list(found.values())
            except (FileNotFoundError,ValueError,KeyError):
                continue
    return list(found.values())

def context(repo,state,*,keys=(),shared=(),source=None,unit=None,phase="before_attempt",budget=6144):
    if phase=="open_assessment":
        return dict(status="current_prompt_only", phase=phase, preferences=[], writing_points=[], shared_points=[], formal_write_count=0)
    valid(phase in {"before_attempt","after_attempt"} and 2048<=budget<=16384,"作文课前读取参数无效")
    from .personal_summary import key_hash
    path=state/"personal-summary/current.json"
    routes=load_json(path)["routes"] if path.is_file() else {}
    available=["writing_skill:"+k for k in SKILLS if key_hash("writing_skill:"+k) in routes]
    requested=list(dict.fromkeys([*keys,*available]))[:8]
    valid(all(k.startswith("writing_skill:") and k.split(":",1)[1] in SKILLS for k in requested),"未知作文点")
    pref=query_memory_context(repo,state,ids=["teaching-preferences"],query="作文 写作 讲解 偏好",max_bytes=3072)
    prefs=[{k:p[k] for k in ("content","source") if k in p} for p in pref["matched"] if p.get("role")=="explicit_preference"]
    out=dict(status="ready",phase=phase,preferences=prefs,writing_points=[],shared_points=[],pending=[],unavailable=[],
             omitted=0,formal_write_count=0)
    if pref.get("generation"):
        out["generation"]=pref["generation"]
    if pref.get("pending"):
        out["pending"].append("英语教学偏好正在收尾")
    if pref.get("unavailable") and not prefs:
        out["unavailable"].append("英语教学偏好暂时无法核实")
    from .memory_query import _state_identity
    identity=_state_identity(state)
    linked=list(shared)
    def add(result,target):
        if result.get("pending"):out["pending"].append("相关摘要或索引待完成")
        if result.get("unavailable") or result.get("conflicts"):out["unavailable"].append("部分来源或权限尚未核实")
        for p in result["matched"]:
            if p.get("role")=="explicit_preference" or p.get("audit",{}).get("status")!="verified":continue
            if target=="shared_points" and phase=="before_attempt":
                # No article prose, reference expression, correction or solved answer before the draft.
                projection={"key":p["key"],"summary_sha256":p["summary_sha256"],"status":"valid_reading_reference"}
            else:
                e=p.get("latest_observation",{})
                projection={"key":p["key"],"summary_sha256":p["summary_sha256"],
                    "observation":e.get("first_attempt"),"study_date":e.get("study_date"),"episode_id":e.get("episode_id"),
                    "next_check":p.get("teaching_hint"),"hint_dependence":e.get("hint_dependence")}
                if target=="writing_points":
                    if not p["key"].startswith("writing_skill:"):continue
                    projection.update({k:e[k] for k in ("writing_stage","attempt_id","writing_task") if k in e})
                    if e.get("correction") and e.get("writing_stage") in {"preference","revision"}:
                        projection["recorded_feedback"]=e["correction"]
                    linked.extend(e.get("linked_keys",[]))
            out[target].append(projection)
        if result.get("generation"):out["generation"]=result["generation"]
    # Small exact lookups avoid returning a partial episode or exposing an unrelated protected article.
    for k in requested:
        result=query_memory_context(repo,state,keys=[k],max_bytes=min(budget,6144))
        add(result,"writing_points")
    valid(all(re.fullmatch(r"(?:word|phrase|syntax):.{1,150}",k) for k in shared),"读写关联必须绑定具体词句")
    if linked:
        add(query_memory_context(repo,state,keys=list(dict.fromkeys(linked))[:8],source_id=source,unit_id=unit,max_bytes=4096),"shared_points")
    if _state_identity(state)!=identity:
        out.update(writing_points=[],shared_points=[],pending=["本次读取期间正式版本更新，请重读"])
    out["pending"]=list(dict.fromkeys(out["pending"]));out["unavailable"]=list(dict.fromkeys(out["unavailable"]))
    from .memory_lifecycle import read_lifecycle
    if read_lifecycle(state).get("global_pending") and not out["pending"]:out["pending"].append("正式摘要正在收尾")
    if out["pending"]:out["status"]="pending"
    elif not out["writing_points"]:out["status"]="no_verified_writing_points" if not out["unavailable"] else "partial"
    if out["pending"] or not out["writing_points"]:
        out["recent_captures"]=recent_capture_context(state)
    while len(canonical_bytes(out))>budget:
        if out.get("recent_captures"):out["recent_captures"].pop()
        elif out["shared_points"]:out["shared_points"].pop()
        elif out["writing_points"]:out["writing_points"].pop()
        elif out["preferences"]:out["preferences"].pop()
        else:break
        out["omitted"]+=1
    return out

def managed(path,body):
    path.parent.mkdir(parents=True,exist_ok=True)
    valid(not path.is_symlink(),"作文摘要页是符号链接，待核")
    old=path.read_text() if path.exists() else ""
    block=OPEN+"\n"+body.rstrip()+"\n"+END
    if old:
        valid(old.count(OPEN)==old.count(END)==1 and old.index(OPEN)<old.index(END),"作文摘要页含未托管正文，保留原页待合并")
        text=old[:old.index(OPEN)]+block+old[old.index(END)+len(END):]
    else:text=block+"\n"
    if text!=old:atomic_write_text(path,text)

def publish_home(repo,state,db,manifest,lifecycle):
    """Caller holds the native projection snapshot. Use only its verified, active functional facets."""
    dates={datetime.now(LOCAL_ZONE).date().isoformat():[]}
    from .writing_plan import BOOKMARKS, PLAN, duration
    bookmarks={}
    if (repo/BOOKMARKS).is_file() and (repo/PLAN).is_file():
        catalogue=load_json(repo/PLAN)["lessons"]
        for row in sorted(load_json(repo/BOOKMARKS)["observations"],key=lambda x:x["observed_at"]):
            lesson=catalogue.get(row["unit"])
            if not lesson or not row.get("message_id") or not row.get("quote"):
                continue
            day=datetime.fromisoformat(row["observed_at"].replace("Z","+00:00")).astimezone(LOCAL_ZONE).date().isoformat()
            label=f'{lesson["playlist"]}第 {lesson["number"]} 节《{lesson["title"]}》'
            if row["state"]=="paused" and row.get("position") is not None:
                bookmarks[day]=f'本人报告的续学位置为 **{label} {duration(row["position"])}**。'
            elif row["state"]=="watched":
                bookmarks[day]=f'本人报告已看完 **{label}**。'
            if day in bookmarks:dates.setdefault(day,[])
    directory=repo/DAYS
    for f in directory.glob("????-??-??.md"):
        if re.fullmatch(r"\d{4}-\d\d-\d\d",f.stem):dates.setdefault(f.stem,[])
    pending=bool(lifecycle.get("global_pending"));invalid=set(lifecycle.get("invalidated_episode_ids",[]))
    for point,sha,raw,audit in db.execute("SELECT point_key,summary_sha256,record_json,audit_json FROM points WHERE point_key LIKE 'writing_skill:%' AND answer_safe=1"):
        if point in lifecycle.get("pending_keys",{}):pending=True;continue
        if lifecycle.get("global_pending") or json.loads(audit).get("status")!="verified":continue
        if point in lifecycle.get("invalidated_summary_keys",[]) and lifecycle.get("accepted_summary_hashes",{}).get(point)!=sha:continue
        record=json.loads(raw)
        if object_sha256(record)!=sha:continue
        for e in record["episodes"]:
            if e["episode_id"] in invalid or not e.get("attempt_id") or e.get("writing_stage") not in {"first_draft","revision","transfer","question","acknowledgment","preference","adoption"}:continue
            dates.setdefault(e["study_date"],[]).append((e,record["key"]))
    evidence={}
    if any(dates.values()):
        from .learning_state import load_learning_events,learning_events_path
        evidence={r["event_id"]:r for r in load_learning_events(learning_events_path(repo)) if r["event"].get("writing_memory")}
    feedback={}
    from .writing_feedback import OUTCOMES
    if not lifecycle.get("global_pending"):
        for path in (state/"writing-sessions/feedback-days").glob("????-??-??/*.json"):
            row=load_json(path);item=row["comparison"]
            valid(row["sha256"]==object_sha256(item) and item["comparison_id"]==path.stem,"作文反馈索引校验失败")
            if (invalid.intersection(row["event_ids"]) or item["point_key"] in lifecycle.get("pending_keys",{})
                    or item["point_key"] in lifecycle.get("invalidated_summary_keys",[])):
                continue
            feedback.setdefault(item["study_date"],[]).append(item)
            dates.setdefault(item["study_date"],[])
    def plain(text):
        # Reviewed local prose is data, including when it contains pasted Markdown.
        return re.sub(r"([\\\[\]<>#|*_!`])",r"\\\1"," ".join(text.split()))
    with exclusive_lock(state/"writing-sessions/.home.lock"):
        home=["# 个人作文学习摘要","","说 **今天学作文** 从有效摘要和实际进度开始。学完把会话链接交给 **英语科代表**，也可以在原聊天说 **今天作文结束并入库**。",""]
        if pending:home+=["部分正式摘要正在收尾，恢复后再读这些观察。",""]
        total=set()
        for day,items in sorted(dates.items(),reverse=True):
            items.sort(key=lambda pair:(pair[0].get("observed_at",""),pair[0]["episode_id"]))
            items=[(e,k) for e,k in items if e["episode_id"] in evidence and
                   evidence[e["episode_id"]]["record_sha256"]==e.get("formal_event_sha256")]
            ids={e["attempt_id"] for e,_ in items if e["writing_stage"] in {"first_draft","revision","transfer"}}
            total.update(ids);home.append(f"- [[{DAYS}/{day}|{day}]]　{len(ids)} 个有作品证据的作答单元")
            body=[f"# {day} 作文摘要",""]
            if day in bookmarks:
                body.extend([bookmarks[day],""])
            if not items:
                body.append("这一天目前没有可读的正式作文观察。科代表收尾后，可在这里查看原句、实际改句和核对结果。")
            else:
                for e,point in items:
                    event=evidence[e["episode_id"]]["event"]
                    observed=event["writing_memory"]
                    body.append("- "+e["first_attempt"]+" "+plain(observed["finding"])+" 下次检查："+plain(observed["next_check"]))
                    if observed["shared_keys"]:
                        body.append("  关联到已审的英语点："+", ".join(plain(k) for k in observed["shared_keys"]))
                    if observed["feedback_ids"]:
                        body.append("  已绑定本轮助手反馈与作答原件。")
                    packages=dict.fromkeys(ref["package_id"] for ref in e.get("evidence",[]) if ref.get("package_id"))
                    if packages:
                        body.append("  "+" · ".join(f"[[wiki/raw_archives/{pid}|原件 {i}]]" for i,pid in enumerate(packages,1)))
                body+=["",f"本日有作品证据的不同作答单元 **{len(ids)} 个**。原稿、讲评与修订按原消息保存。"]
            if pending:body+=["","部分摘要尚在收尾。"]
            for item in sorted(feedback.get(day,[]),key=lambda x:(x["feedback"]["at"],x["comparison_id"])):
                body+=["",f"<!-- writing-feedback:{item['comparison_id']} -->","#### 本次反馈与实际修改","",
                       "- 原句　"+plain(item["original"]["quote"]),"- 讲评　"+plain(item["feedback"]["quote"])]
                if item["revision"]:
                    body.append("- 实际改句　"+plain(item["revision"]["quote"]))
                body+=["- 核对结果　"+OUTCOMES[item["result"]]+"。"+plain(item["reason"]),
                       f"- [[wiki/raw_archives/{item['feedback']['package_id']}|查看讲评原件]]"]
            managed(repo/DAYS/(day+".md"),"\n".join(body))
        home+=["",f"跨日去重后共 **{len(total)} 个**有作品证据的作答单元。"]
        managed(repo/HOME,"\n".join(home))
        from .writing_history import publish_locators
        publish_locators(repo,state,manifest,dates,evidence)
    return {"home":str(repo/HOME),"days":len(dates),"attempts":len(total),"pending":pending,"generation":manifest["generation"]}
