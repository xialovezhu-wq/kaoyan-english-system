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
    return attempts(runtime_for(repo),"WRITING-"+object_sha256(tr.native)[:24])


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
        append(s,s["attempt_id"],"assistant",task["prompt"],m["opened_at"])
        drafts={d["id"]:d for d in m["drafts"]}
        for e in m["events"]:
            if e["id"] in drafts:
                d=drafts[e["id"]]
                append(s,d["id"],"user",d["text"],d["at"])
            elif e.get("visible_text"):
                append(s,e["id"],"assistant",e["visible_text"],e["at"])
    if rows:
        tr.messages=sorted([*tr.messages,*rows],key=lambda r:datetime.fromisoformat(r["created_at"].replace("Z","+00:00")))
        tr.checked()
    return tr


def material(repo,tr,state):
    from .writing_workspace import folder,lesson_file
    k="WRITING-"+object_sha256(tr.native)[:24]; sha=state["source_version"]
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
    draft=next((d for d in m["drafts"] if "WRUI-"+d["id"]==mid),None)
    if draft is None:
        return None
    check(message["role"]=="user" and message["content"]==draft["text"] and message["created_at"]==draft["at"]
          and message["metadata"].get("display_attempt_id")==origin["display_attempt_id"],"submitted draft mismatch")
    task=m["task"]
    result=next((r for r in reversed(m["reviews"]) if r["draft_id"]==draft["id"]),None)
    conditions=dict(draft["conditions"])
    if any(e.get("support") not in (None,"none") for e in m["events"] if e["id"] in draft["help_ids"]):
        check(conditions["answer_support"] != "none", "prior recorded help cannot be removed")
    # A target-free classification requires all adjacent chat instructions in the captured scope to be reviewed.
    # The opening conversation turn must be present; an excerpt without its beginning stays unknown.
    rows=load_json(root/"conversation.json")["messages"]
    if identity.get("missing_attachments") or not any(r["message_id"]=="WRUI-"+origin["display_attempt_id"] for r in rows):
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
    return observation(task,{**draft,"conditions":conditions},result,opportunity=opportunity)
