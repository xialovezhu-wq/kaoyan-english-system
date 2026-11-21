"""Read the dated exam plan and resume only from actual course bookmarks."""
from datetime import date, datetime
from pathlib import Path

from .util import LOCAL_ZONE, atomic_write_json, exclusive_lock, load_json, object_sha256
from .writing_materials import COURSE, require

PLAN = COURSE / "考前逐日计划.json"
BOOKMARKS = Path("intake/views/course-playback/2026-english-writing.json")

def duration(seconds):
    return f"{seconds//3600}:{seconds%3600//60:02d}:{seconds%60:02d}" if seconds >= 3600 else f"{seconds//60}:{seconds%60:02d}"

def brief(writing, lessons):
    if writing.get("course_label"):
        return writing["course_label"]
    if writing["mode"] != "course":
        if "tasks" in writing:
            labels = []
            for item in writing["tasks"]:
                part = "大作文" if item["part"] == "B" else "小作文"
                labels.append(f'{item["year"]} {part}（{item["genre"]}，{item["activity"]}）')
            return " / ".join(labels)
        tag = "闭卷重写" if writing["mode"] == "rewrite" else "真题实写"
        return f'{writing["year"]} 英一大作文 1 篇、小作文 1 篇 · {tag}'
    groups=[]
    for name in ("大作文","小作文"):
        nums=[lessons[k]["number"] for k in writing["lessons"] if lessons[k]["playlist"]==name]
        if nums:
            groups.append(f"{name}第{nums[0]}–{nums[-1]}节" if len(nums)>1 else f"{name}第{nums[0]}节")
    return " → ".join(groups)

def current_plan(repo, day=None):
    plan=load_json(repo/PLAN)
    today=day or datetime.now(LOCAL_ZONE).date()
    if isinstance(today,str):today=date.fromisoformat(today)
    row=next((d for d in plan["days"] if d["date"]==today.isoformat()),None)
    if row is None:
        return {"status":"outside_plan","start":plan["start"],"end":plan["end"],"date":today.isoformat()}
    lessons=plan["lessons"]
    order=list(dict.fromkeys(k for d in plan["days"] if d["writing"]["mode"]=="course" for k in d["writing"]["lessons"]))
    path=repo/BOOKMARKS
    observations=load_json(path)["observations"] if path.exists() else []
    latest=max(observations,key=lambda x:(x["observed_at"],x["message_id"])) if observations else None
    pos=order.index(latest["unit"]) + (latest["state"]=="watched") if latest else 0
    target=order[pos] if pos<len(order) else None
    cursor=None if not target else {"unit":target,**lessons[target],
          "position":latest["position"] if latest and latest["state"]=="paused" else None}
    writing=row["writing"]
    play=[dict(unit=k,**lessons[k]) for k in writing.get("lessons",[])]
    for entry in play:
        if entry["unit"] in writing.get("segments",{}):
            start,end=writing["segments"][entry["unit"]]
            entry.update(start_seconds=start,end_seconds=end,planned_duration_seconds=end-start)
    block=row.get("method_video")
    video=None
    if block:
        v=plan["method_videos"][block["key"]]
        url=v["url"]
        if block["start_seconds"]:url+=("&" if "?" in url else "?")+"t="+str(block["start_seconds"])
        video=dict(label=v["label"],uploader=v["up"],url=url,start_seconds=block["start_seconds"],
                   end_seconds=block["end_seconds"],full_duration_seconds=v["duration_seconds"],
                   sequence=block.get("sequence","before"))
    readings = row.get("readings")
    if readings is None:
        readings = [row["reading"]] if row.get("reading") else []
    day_row = {**row, "readings": readings, "reading": readings[0] if readings else None,
               "reading_count": len(readings), "reading_question_count": sum(r["questions"] for r in readings),
               "first_task": "reading" if readings else "writing"}
    selection={}
    if row["minor"].get("year") and row["minor"]["section"] in {"C","T","N"}:
        year=row["minor"]["year"]
        part={"C":"section1","T":"section2-part-c","N":"section2-part-b"}[row["minor"]["section"]]
        original=plan.get("original_papers",{}).get(str(year),{})
        selection=dict(paper_id=f"{year}-I",section=part,questions=row["minor"]["questions"],
                       pdf=original.get("pdf"),sha256=original.get("sha256"),
                       physical_page=original.get("verified_heading_pages",{}).get(part))
        if original.get("native_html"):
            selection.update(native_html=original["native_html"], native_html_sha256=original["native_html_sha256"],
                             question_url=original["section_urls"][part])
    if video:
        video["published_date"] = v.get("date_published")
        video["lesson_paper_year"] = v.get("lesson_paper_year")
    return dict(status="planned",version=plan["version"],**day_row,today_method=video,exercise_source=selection,writing_label=brief(writing,lessons),
                planned_lectures=play,resume_course=cursor,last_bookmark=latest,
                practice_starts="2026-11-24",practice_parts=["B","A"] if writing["mode"]!="course" else [])

def record_bookmark(repo, unit, state, quote, position=None):
    from .expression_receipts import Conversation, live_session, event_ref
    session=live_session(None)
    message=Conversation(session).current_user()
    require(isinstance(quote,str) and quote.strip() and quote in message["content"],"course bookmark requires the learner's exact report")
    plan=load_json(repo/PLAN)
    require(unit in plan["lessons"] and state in {"paused","watched"},"invalid course bookmark")
    if position is not None:
        require(type(position) is int and 0<=position<=plan["lessons"][unit]["duration_seconds"],"invalid observed position")
    ref=event_ref(message)
    row=dict(unit=unit,state=state,position=position,quote=quote,observed_at=message["created_at"],session_id=session,**ref)
    path=repo/BOOKMARKS
    with exclusive_lock(path.with_suffix(".lock")):
        saved=load_json(path) if path.exists() else {"role":"course_position_self_report","observations":[]}
        same=[x for x in saved["observations"] if (x["session_id"],x["message_id"],x["unit"])==(session,ref["message_id"],unit)]
        if same:require(same==[row],"bookmark conflicts with the same original report")
        else:
            saved["observations"].append(row)
            atomic_write_json(path,saved)
    return {"cursor":row,"formal_write_count":0}

def screen_plan(repo):
    if not (repo/PLAN).exists():return None
    state=current_plan(repo)
    if state["status"]!="planned":return None
    w=state["writing"]
    section=f'第 {state["day"]} 天 · {state["date"][5:]}'
    if w["mode"]=="course":
        c=state["resume_course"]
        goal=f'从{c["playlist"]}第 {c["number"]} 节《{c["title"]}》开始。' if c else "课程已走到最后一节，按当天实际进度安排练习。"
        if c and c["position"] is not None:goal=f'续看{c["playlist"]}第 {c["number"]} 节，从 {duration(c["position"])} 接起。'
        def time_label(x):
            if "start_seconds" in x:
                return f'{duration(x["start_seconds"])}–{duration(x["end_seconds"])}'
            return duration(x["duration_seconds"])
        body="\n".join(f'{x["playlist"]} {x["number"]}　{x["title"]}　{time_label(x)}' for x in state["planned_lectures"])
        cards=[dict(id="plan-course",title="今天的课 · "+duration(w["duration_seconds"]),body=body)]
    else:
        goal=state["writing_label"]
        cards=[dict(id="plan-essays",title="先写大作文，再写小作文",body="大作文 30–35 分钟，小作文 15–20 分钟。两篇写完留 10 分钟核任务、改写一处。")]
    reading_rows = [f'英一 {r["year"]} Text {r["text"]}（{r["questions"]} 题）' for r in state["readings"]]
    reading_line = "、".join(reading_rows) if reading_rows else "今天阅读休息"
    method=state.get("today_method")
    description=state["minor"]["label"]+"。"
    if method:
        span = duration(method["start_seconds"])+"–"+duration(method["end_seconds"])
        issued = method.get("published_date")
        clip = method["label"]+"（"+(issued+" 发布，" if issued else "")+span+"）"
        if method["sequence"] == "after":
            description += "\n交卷后看"+clip+"。"
        else:
            description = "先看"+clip+"。\n配套练习　"+description
    card=dict(id="plan-english",title="今天的阅读和专项",body=reading_line+"。\n"+description)
    if method:card.update(href=method["url"],link_label="打开今天的 B 站方法课")
    cards.append(card)
    return dict(section=section,goal=goal,cards=cards,date=state["date"],fingerprint=object_sha256([state["date"],goal,cards]))
