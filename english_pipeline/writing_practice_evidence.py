"""Bind visible reader actions to the original after-class package and its sole formal writer."""
from datetime import datetime
from pathlib import Path

from .reading_writing_tasks import VERSION, SUPPORT, check, cue_level, observation
from .util import object_sha256, load_json, file_sha256
from .writing_sources import digest_message


def runtime_for(repo):
    from .article_workspace import RUNTIME
    from .constants import REPO_ROOT
    return RUNTIME if repo.resolve()==REPO_ROOT.resolve() else repo/".writing-reader"


def display_attempts(repo, tr):
    from .writing_practice import attempts
    if not tr.native:
        return {}
    from .guided_point_cli import selected
    root=runtime_for(repo)
    key="WRITING-"+object_sha256(tr.native)[:24]
    related=selected(root,tr.native)
    original=attempts(root,key)
    if related!=key:
        current_id=load_json(root/"writing"/related/"current.json")["attempt_id"]
        original.update({a:s for a,s in attempts(root,related).items() if a==current_id})
    return original


def visible_rows(repo,tr):
    """Only original UI submissions and actually displayed instructions are visible transcript turns."""
    rows=[]
    def append(state, key, role, text, at):
        mid="WRUI-"+key
        rows.append(dict(message_id=mid,role=role,content=text,created_at=at,attachment_refs=[],
            metadata={"original_content_blocks":[{"type":"text","text":text}],"source_system":"english_writing_reader",
                      "display_attempt_id":state["attempt_id"],"native_action_id":key}))
    for s in display_attempts(repo,tr).values():
        m=s["micro"];task=m["task"]
        if not m.get("guided"):append(s,s["attempt_id"],"assistant",task["prompt"],m["opened_at"])
        drafts={d["id"]:d for d in m["drafts"]}
        for e in m["events"]:
            if e["id"] in drafts:
                d=drafts[e["id"]]
                if not d.get("native_message_id") or d.get("native_session")==s["entity_key"]:
                    append(s,d["id"],"user",d["text"],d["at"])
            elif m.get("guided") and e.get("submitted_text"):
                append(s,e["id"],"user",e["submitted_text"],e["at"])
            elif m.get("guided") and e.get("seen_at") and not e.get("outbound_native_message_id"):
                append(s,e["id"],"assistant",e["visible_text"],e["seen_at"])
            elif e.get("visible_text") and (not m.get("guided") or e["kind"]=="question_seen" and not e.get("native_message_id")):
                append(s,e["id"],"assistant",e["visible_text"],e["at"])
    if rows:
        tr.messages=sorted([*tr.messages,*rows],key=lambda r:datetime.fromisoformat(r["created_at"].replace("Z","+00:00")))
        tr.checked()
    return tr


def material(repo,tr,state):
    from .writing_workspace import folder,lesson_file
    k=state["entity_key"]; sha=state["source_version"]
    t=state["micro"]["task"]
    return dict(task_id=t["task_id"],prompt=t["prompt"],
        origin=dict(kind="writing_reader_v2",lesson_sha256=sha,display_attempt_id=state["attempt_id"]),
        attachments=[str(lesson_file(repo,sha)),str(folder(runtime_for(repo),k)/"revisions"/(str(state["revision"])+".json"))])


def from_capture(root, message, review_surfaces=None):
    from .writing_contract import json_document
    identity=load_json(root/"source.json")["identity"]["writing_session"]
    origin=identity["task"]["origin"]
    if not isinstance(origin,dict) or origin.get("kind")!="writing_reader_v2":
        return None
    snapshots=[]
    for asset in load_json(root/"manifest.json")["attachments"]:
        if asset.get("role")!="other_attachment" or not asset["path"].endswith(".json"):
            continue
        obj=json_document(root,asset)
        if not isinstance(obj,dict) or obj.get("attempt_id")!=origin["display_attempt_id"] or not obj.get("micro"):
            continue
        check(obj["source_version"]==origin["lesson_sha256"]==object_sha256(obj["micro"]["task"]),
              "frozen teaching task changed")
        snapshots.append(obj)
    check(snapshots,"frozen writing proof missing")
    m=max(snapshots,key=lambda row:row["revision"])["micro"]
    mid=message["message_id"]
    draft=next((d for d in m["drafts"] if "WRUI-"+d["id"]==mid or
                (d.get("native_message_id")==mid and d.get("native_session")==identity["session_id"])),None)
    if draft is None:
        if not m.get("guided"):return None
        bound=next((ev for ev in m["events"] if ev.get("original_message_id") in (mid,mid.removeprefix("WRUI-"))
                    and ev.get("question_id") and ev.get("original_session") in (identity["session_id"],"WRITING-"+object_sha256(identity["session_id"])[:24])),None)
        if not bound:return None
        from .lesson_point_memory import control_proof
        t=m["task"];point=control_proof(t,m,bound,message,origin["display_attempt_id"])
        return dict(activity="writing",task_type=t["task_type"],task_id=t["task_id"],task_version=t["task_version"],rubric_version=t["rubric_version"],
                    target_skill_id=t["target_skill_id"],task_cue=t["task_cue"],answer_support="unknown",task_relation="same_task",production_origin="unknown",
                    target_result="not_applicable",eligibility="ineligible",open_task_transfer_observed=False,task_sha256=object_sha256(t),
                    observation_id=object_sha256([bound["id"],point["decision_ref"]]),lesson_point=point)
    check(message["role"]=="user" and message["content"]==draft["text"] and message["created_at"]==draft["at"]
          and (message["metadata"].get("display_attempt_id")==origin["display_attempt_id"] or draft.get("native_message_id")==mid),"submitted draft mismatch")
    task=m["task"]
    result=next((r for r in reversed(m["reviews"]) if r["draft_id"]==draft["id"]),None)
    conditions=dict(draft["conditions"])
    if any(e.get("support") not in (None,"none") for e in m["events"] if e["id"] in draft["help_ids"]):
        check(conditions["answer_support"] != "none", "prior recorded help cannot be removed")
    # A target-free classification requires all adjacent chat instructions in the captured scope to be reviewed.
    # The opening conversation turn must be present; an excerpt without its beginning stays unknown.
    rows=load_json(root/"conversation.json")["messages"]
    opening_ids={"WRUI-"+origin["display_attempt_id"]}
    if m.get("guided"):
        opening_ids.update(q.get("message_id") or ("WRUI-"+q.get("delivery_id","")) for q in m["guided"]["questions"] if q["id"]==draft["question_id"])
    if identity.get("missing_attachments") or not any(r["message_id"] in opening_ids for r in rows):
        for k in ("task_cue","answer_support"):
            if conditions[k]=="none": conditions[k]="unknown"
    audits=review_surfaces or {}
    earlier=[]
    for row in rows:
        if row["message_id"]==mid:break
        if row["metadata"].get("source_system")!="english_writing_reader":
            earlier.append(row)
    for row in earlier:
        audit=audits.get(row["message_id"],{})
        known=audit.get("source_sha256")==digest_message(row) and bool(audit.get("reason"))
        cue=cue_level(row["content"],audit.get("cue","unknown") if known else "unknown",
                      task.get("forms",[]),task.get("functions",[]))
        level=next((c for c in ("form","function","unknown") if c in {cue,conditions["task_cue"]}),"none")
        conditions["task_cue"]=level
        support=audit.get("support","unknown") if known else "unknown"
        check(support in (*SUPPORT,"unknown"),"invalid exposure review")
        conditions["answer_support"]=next((c for c in (*reversed(SUPPORT[1:]),"unknown") if c in
                                           (support,conditions["answer_support"])),"none")
        if row["role"]=="assistant" and " ".join(draft["text"].lower().split()) in " ".join(row["content"].lower().split()):
            conditions["production_origin"]="ai_or_mixed"
    opportunity=m["corrections"][-1]["value"] if m["corrections"] else None
    value=observation(task,{**draft,"conditions":conditions},result,opportunity=opportunity)
    if m.get("guided") and result:
        from .lesson_point_memory import frozen_proof
        value["lesson_point"]=frozen_proof(m,draft,result)
        if draft.get("attachment_sha256"):value["original_attachment_sha256"]=draft["attachment_sha256"]
        value["target_result"]=result["target_result"]
    return value
