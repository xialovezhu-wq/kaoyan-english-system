"""Exact confirmed course-point observations on the existing English memory generation."""
import json
import re
from .reading_writing_tasks import checked_id, check
from .guided_points import SCHEMA, STATUSES, FOLLOW, RATINGS, MEDIA
from .util import canonical_bytes, file_sha256, load_json, object_sha256, text_sha256
from .memory_store import open_memory_projection, read_lifecycle
from .personal_summary import key_hash

CONTROLS=("explain","pause","resume","skip","acknowledge","watched","time_elapsed","change_target","clarify_intent")
FIELDS={"schema","lesson_id","lesson_point_id","point_version","rubric_version","practice_ref","question_id","decision_ref",
        "response_sha256","source_ref","action","classroom_status","criteria","remaining","medium","verification","response_kind","shareable","updated_at"}


def validate(proof):
    check(isinstance(proof,dict) and set(proof)==FIELDS and proof["schema"]==SCHEMA,"invalid formal lesson point contract")
    for key in ("lesson_id","lesson_point_id","point_version","rubric_version","question_id"):checked_id(proof[key])
    for key in ("practice_ref","decision_ref","response_sha256","source_ref"):
        check(re.fullmatch("[a-f0-9]{64}",proof[key]),"invalid point evidence digest")
    check(proof["classroom_status"] in STATUSES and proof["action"] in FOLLOW+("advance",)+CONTROLS and
          proof["medium"] in MEDIA and proof["response_kind"] in {"specified_form","function","reading_judgment"} and
          proof["verification"] in {"as_submitted","unverified","checked_original"} and type(proof["shareable"]) is bool,"invalid formal point state")
    check(isinstance(proof["criteria"],dict) and (proof["criteria"] or proof["action"] in CONTROLS) and len(proof["criteria"])<=8,"invalid criterion coverage")
    for key,value in proof["criteria"].items():
        checked_id(key);check(value in RATINGS,"invalid point finding")
    check(isinstance(proof["remaining"],list) and set(proof["remaining"])<=set(proof["criteria"]),"invalid outstanding conditions")
    from datetime import datetime
    check(datetime.fromisoformat(proof["updated_at"].replace("Z","+00:00")).tzinfo is not None,"missing decision timestamp")
    if proof["classroom_status"]=="ready_for_next":check(proof["action"] in ("advance",)+CONTROLS and not proof["remaining"],"unsupported classroom pass")
    return proof


def frozen_proof(m,draft,review):
    decision=review.get("lesson_decision") if review else None
    if not decision:return None
    task=m["task"];point=task["course_point"];b=decision["binding"]
    check(b["response_id"]==draft["id"] and b["response_sha256"]==draft["sha256"] and b["task_sha256"]==object_sha256(task)
          and b["question_id"]==draft["question_id"] and b["lesson_point_id"]==point["lesson_point_id"],"frozen point, answer or question mismatch")
    status=decision["status"];at=decision["at"];basis=decision
    return validate(dict(schema=SCHEMA,lesson_id=point["lesson_id"],lesson_point_id=point["lesson_point_id"],
           point_version=point["version"],rubric_version=task["rubric_version"],practice_ref=object_sha256(b["attempt_id"]),
           question_id=b["question_id"],decision_ref=object_sha256(basis),response_sha256=draft["sha256"],source_ref=object_sha256(point["source"]),
           action=decision["action"],classroom_status=status,criteria={k:r["status"] for k,r in decision["criteria"].items()},
           remaining=decision["unresolved"],medium=draft["medium"],verification=draft["verification"],response_kind=point["target_kind"],shareable=not bool((task.get("source_ref") or {}).get("question_id")),updated_at=at))


def control_proof(task,m,event,message,display_attempt_id):
    """A control is original evidence of intent, never evidence of a correct or failed answer."""
    p=task["course_point"]
    ctrl=next((r for r in m["guided"]["control_events"] if r["id"]==event["id"]),None)
    check(ctrl and text_sha256(message["content"])==event["original_sha256"],"control did not match original")
    earlier=[d for d in m["guided"]["decisions"] if d["at"]<ctrl["at"]]
    last=earlier[-1] if earlier else {}
    return validate(dict(schema=SCHEMA,lesson_id=p["lesson_id"],lesson_point_id=p["lesson_point_id"],point_version=p["version"],
                rubric_version=task["rubric_version"],practice_ref=object_sha256(display_attempt_id),question_id=event["question_id"],
                decision_ref=object_sha256(ctrl),response_sha256=event["original_sha256"],source_ref=object_sha256(p["source"]),
                action=ctrl["kind"],classroom_status=ctrl["status"],criteria={k:v["status"] for k,v in last.get("criteria",{}).items()},
                remaining=last.get("unresolved",[]),medium=event["original_medium"],verification="as_submitted",
                response_kind=p["target_kind"],shareable=not bool((task.get("source_ref") or {}).get("question_id")),updated_at=ctrl["at"]))


def records(state,ident=None):
    from .writing_publication import summary_identity
    identity=summary_identity(state)
    check(identity.get("status")=="available","personal summary identity unavailable")
    db,manifest=open_memory_projection(state)
    try:
        index=load_json(state/"personal-summary/current.json")["routes"];life=read_lifecycle(state)
        bad=set(life.get("invalidated_episode_ids",[]));matches=[]
        sql="""SELECT e.point_key,e.episode_id,e.episode_json,p.audit_json,p.summary_sha256,p.question_ids_json
          FROM episodes e JOIN points p ON p.point_key=e.point_key
          WHERE e.point_key GLOB 'writing_skill:*' AND p.answer_safe=1 AND json_type(e.episode_json,'$.writing_task.lesson_point')='object'"""
        params=()
        if ident:
            checked_id(ident)
            sql+=" AND json_extract(e.episode_json,'$.writing_task.lesson_point.lesson_point_id')=?";params=(ident,)
        for key,eid,raw,audit,digest,questions in db.execute(sql,params):
            if (index.get(key_hash(key),{}).get("sha256")!=digest or life.get("global_pending") or key in life["pending_keys"] or eid in bad
                or key in life.get("invalidated_summary_keys",[]) and life.get("accepted_summary_hashes",{}).get(key)!=digest
                or json.loads(questions) or json.loads(audit).get("status")!="verified"):continue
            episode=json.loads(raw);w=episode["writing_task"];p=validate(w["lesson_point"])
            if not p["shareable"]:continue
            matches.append({**p,"broad_skill":key,"episode_id":eid,"source_summary_sha256":digest,
                            "answer_support":w["answer_support"],"task_cue":w["task_cue"],"production_origin":w["production_origin"],
                            "response_result":w["target_result"],"assisted_success":w["target_result"]=="met" and w["production_origin"]=="ai_or_mixed",
                            "scope":"formal_classroom_observation","open_task_transfer_observed":False})
        check(summary_identity(state)==identity,"point index changed during read")
        return sorted(matches,key=lambda r:(r["updated_at"],r["episode_id"]),reverse=True)
    finally:db.close()


def original_access(audit,state):
    from .memory_query import _public_ref
    from .errors import PipelineError
    from .archive import resolve_archived_package_from_pointer
    result=_public_ref(audit,state.parent)["current_access"]
    if result=="available_not_reopened":return result
    ref=audit.get("reference",{});pid=ref.get("package_id","")
    match=re.fullmatch(r"EN-PKG-(\d{4})(\d{2})(\d{2})-[A-F0-9]{16}",pid)
    if match is None:return result
    day="-".join(match.groups())
    if not (state/"archive-pointers"/day/(pid+".json")).is_file():return result
    try:
        from .constants import REPO_ROOT
        if state.parent.resolve()==REPO_ROOT.resolve():
            from .archive import verify_volume_contract,VolumeContract
            verify_volume_contract(VolumeContract())
        package=resolve_archived_package_from_pointer(state,state.parent,study_date=day,package_id=pid,
                                                     expected_package_sha256=ref.get("package_sha256"))
        member=ref.get("member","conversation.json")
        target=(package/member).resolve(strict=True)
        return "verified_archived_original" if target.is_relative_to(package) and file_sha256(target)==ref.get("sha256") else "original_hash_mismatch"
    except (OSError,ValueError,PipelineError):
        return "archive_unavailable_or_changed"


def query_points(state,ident,budget):
    checked_id(ident);check(1024<=budget<=16384,"point query budget outside native limits")
    rows=records(state,ident)
    result=dict(status="ready" if rows else "not_found",lesson_point_id=ident,matched_total=len(rows),included=0,
                omitted=len(rows),records=[],formal_write_count=0)
    if rows:
        from .memory_query import _episode_audits
        db,_=open_memory_projection(state)
        try:
            for row in rows:
                statuses=sorted({original_access(json.loads(a),state) for a in _episode_audits(db,row["broad_skill"],row["episode_id"])})
                item=dict(row,original_access=statuses or ["not_reopened"])
                trial={**result,"records":[*result["records"],item],"included":result["included"]+1,"omitted":result["omitted"]-1}
                if len(canonical_bytes(trial))>budget:break
                result=trial
        finally:db.close()
    return result


def publication_rows(state):
    rows=records(state);groups={}
    for r in rows:groups.setdefault((r["lesson_id"],r["lesson_point_id"]),[]).append(r)
    return dict(schema="english-course-point-projection/1",scope="latest safe confirmed classroom observation per lesson and point",
                confirmed_point_count=len(groups),eligible_observation_count=len(rows),older_observations_omitted=len(rows)-len(groups),
                excluded=["unconfirmed_workspace","protected","invalid_or_pending","raw_answers","legacy_without_point_id"],
                records=[{**group[0],"older_observations_omitted":len(group)-1} for _,group in sorted(groups.items())])
