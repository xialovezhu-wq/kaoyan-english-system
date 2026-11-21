"""Writing branch of the existing local-day author and native memory closeout."""
from collections import defaultdict
from pathlib import Path

from .util import atomic_write_json, file_sha256, load_json, object_sha256
from .writing_personal import VERSION, combine_summary
from .writing_sessions import SCHEMA
from .writing_sources import digest_message,valid

def is_writing_package(root):
    return load_json(root/"source.json")["identity"].get("writing_session",{}).get("schema")==SCHEMA

def candidate_rows(state,root):
    source=load_json(root/"source.json")["identity"]["writing_session"]
    folder=state/"writing-sessions"/object_sha256(source["host"]+":"+source["session_id"])
    p=folder/"session.json"
    if not p.exists():return []
    receipt=load_json(root/"receipt.json")
    found=[]
    for c in load_json(p)["candidates"]:
        if c["package_id"]!=receipt["package_id"]:continue
        valid(c["package_sha256"]==receipt["package_sha256"] and load_json(folder/"candidates"/(c["candidate_id"]+".json"))==c,
              "待审候选与封存包或不可变旁证不符")
        found.append(c)
    return found

def review_rows(state,root,item):
    rows=load_json(root/"conversation.json")["messages"]
    review=item["writing_review"]
    valid(isinstance(review,dict) and review.get("version")=="writing-coverage/1"
          and isinstance(review.get("messages"),list),"科代表须核完整作文单元")
    decisions={r["message_id"]:r for r in review["messages"]}
    valid(len(decisions)==len(rows)==len(review["messages"]) and set(decisions)=={m["message_id"] for m in rows},"作文覆盖表未覆盖全部原消息")
    for m in rows:
        d=decisions[m["message_id"]]
        valid(d.get("original_sha256")==digest_message(m) and d.get("disposition") in {"observed","feedback","reference","no_new_point","unresolved"}
              and isinstance(d.get("reason"),str) and d["reason"].strip(),"作文消息未核原话、范围或处置原因")
        if d["disposition"]=="observed":
            valid(m["role"]=="user" and any(m["message_id"]==a.get("event",{}).get("writing_memory",{}).get("message_id")
                  for a in item["actions"]), "个人作文观察缺少对应的 typed action")
        if d["disposition"]=="unresolved":valid(item["unresolved"],"未决作文消息须保留原生未决处置")
    observed={a["event"]["writing_memory"]["message_id"] for a in item["actions"] if a.get("event",{}).get("writing_memory")}
    valid(all(mid not in decisions or decisions[mid]["disposition"]=="observed" for mid in observed),"作文事件与覆盖分类不一致")
    candidate_ids={c["candidate_id"] for c in review.get("candidate_decisions",[]) if isinstance(c,dict) and c.get("disposition") in {"accepted","declined","held"} and c.get("reason")}
    valid(len(candidate_ids)==len(review.get("candidate_decisions",[])), "候选审核须有逐项结果及依据")
    candidates={c["candidate_id"]:c for c in candidate_rows(state,root)}
    valid(candidate_ids==set(candidates),"尚有候选未逐项核读")
    for selection in review.get("candidate_decisions",[]):
        if selection["disposition"]=="accepted":
            c=candidates[selection["candidate_id"]]
            valid(any(a.get("event",{}).get("writing_memory",{}).get("message_id")==c["message_id"] and
                      a["event"]["writing_memory"]["point_key"]==c["point_key"] for a in item["actions"]),
                  "采用候选须有对应正式观察")
    from .writing_feedback import comparison
    comparisons=review.get("feedback_reviews",[])
    verified=[comparison(state.parent,state,root,pair) for pair in comparisons]
    valid(verified==comparisons and len({p["comparison_id"] for p in verified})==len(verified),"反馈对照需要先逐句核原件")
    pid=item["package_id"]
    for mid,d in decisions.items():
        if d["disposition"]=="feedback":
            valid(any(p["feedback"]["package_id"]==pid and p["feedback"]["message_id"]==mid for p in verified),"有价值的讲评未对到原句和改稿状态")
    for action in item["actions"]:
        event=action.get("event",{})
        w=event.get("writing_memory",{})
        if w.get("stage")=="revision" and event.get("hint_dependence") in {"hint","explanation"}:
            valid(any(p["revision"] and p["revision"]["package_id"]==pid and p["revision"]["message_id"]==w["message_id"]
                      and p["feedback"]["message_id"] in w["feedback_ids"] and p["point_key"]==w["point_key"] for p in verified),
                  "提示后修订须对到原句、原反馈、本人改句及实际结果")
    return review

def binding_path(state, batch, actions_sha):
    return state/"writing-sessions"/"reviewed"/batch/(actions_sha+".json")

def store_coverage(state,manifest_path,actions_path,decisions):
    manifest=load_json(manifest_path)
    selected={r["package_id"]:{"package_sha256":r["package_sha256"],"review":r["writing_review"]}
              for r in decisions if "writing_review" in r}
    if not selected:return
    sha=file_sha256(actions_path);p=binding_path(state,manifest["batch_id"],sha)
    obj=dict(version="writing-author/1",manifest_sha256=file_sha256(manifest_path),actions_sha256=sha,packages=selected)
    if p.is_file():valid(load_json(p)==obj,"相同动作已绑定另一份作文审阅")
    else:atomic_write_json(p,obj)

def reviewed(state,writer):
    path=binding_path(state,writer["batch_id"],writer["actions_sha256"])
    if not path.is_file():return {}
    r=load_json(path)
    valid(r["manifest_sha256"]==writer["manifest_sha256"] and r["actions_sha256"]==writer["actions_sha256"],"作文审阅与 writer 回执失配")
    return r["packages"]

def applies(state,writer):
    return bool(any(r.get("writing_contract")==VERSION for r in writer.get("action_results",[]))
                or (writer.get("batch_id") and writer.get("actions_sha256") and
                    binding_path(state,writer["batch_id"],writer["actions_sha256"]).is_file()))

def require_reviewed_capture(manifest,actions_path,state):
    packages=[p for p in manifest.get("package_documents",[]) if is_writing_package(Path(p["path"]))]
    if not packages:return
    sha=file_sha256(actions_path);p=binding_path(state,manifest["batch_id"],sha)
    valid(p.is_file(),"需由英语科代表核对指定作文包的完整覆盖")
    bound=load_json(p)
    valid(bound.get("actions_sha256")==sha and all(bound["packages"].get(r["package_id"],{}).get("package_sha256")==r["package_sha256"]
          for r in packages),"作文覆盖与这批正式动作不一致")

def profile_document(repo,state,verified,writer,base=None):
    from .personal_summary import SCHEMA as PS
    review=reviewed(state,writer)
    completed={k for k,v in writer["package_dispositions"].items() if v=="completed"}
    basis=base or {"schema_version":PS,"records":[],"coverage":[]}
    if base is None:
        shared=[v["review"].get("shared_memory") for v in review.values() if v["review"].get("shared_memory")]
        for field in ("records","coverage","memory_claims","memory_corrections"):
            if shared:basis[field]=[item for doc in shared for item in doc.get(field,[])]
        knowledge_pids={ref["package_id"] for a in writer["action_results"] if a.get("result")=="applied"
                        and a.get("writing_contract")!=VERSION and a["action_type"]!="skip_duplicate" for ref in a["evidence_refs"]}
        covered={c["package_id"] for c in basis.get("coverage",[])}
        if knowledge_pids-covered or completed-set(review):return None
    supplied={}
    for record in basis.get("records",[]):
        key=record["key"]
        if key not in supplied:
            supplied[key]=dict(record,episodes=list(record["episodes"]))
            continue
        previous=supplied[key]
        valid(previous["visibility"]==record["visibility"],"共享摘要权限冲突")
        merged={e["episode_id"]:e for e in previous["episodes"]}
        for e in record["episodes"]:
            valid(e["episode_id"] not in merged or merged[e["episode_id"]]==e,"共享摘要证据身份冲突")
            merged[e["episode_id"]]=e
        newest=lambda r:max(e.get("observed_at") or e["study_date"] for e in r["episodes"])
        head=record if newest(record)>newest(previous) else previous
        supplied[key]={**head,"episodes":list(merged.values()),"aliases":sorted(set(previous.get("aliases",[]))|set(record.get("aliases",[])))}
    # Normalize hot references from the shared language author to stable native
    # locators, including after archive cleanup. Keep other kinds of evidence.
    from .web_review import package_root
    for record in supplied.values():
        for ep in record["episodes"]:
            for ref in ep.get("evidence",[]):
                if ref.get("member") or not ref.get("path"):continue
                target=(repo/ref["path"]).resolve()
                if target.name!="conversation.json" or target.parent.name not in review:continue
                pid=target.parent.name
                pk=package_root(state,repo,pid);rc=load_json(pk/"receipt.json")
                logical=state/"packages"/rc["study_date"]/pid/"conversation.json"
                valid(target==logical.resolve() and file_sha256(pk/"conversation.json")==ref["sha256"],"共享作文原件哈希失配")
                ref.update(kind="writing_original_v2",path=logical.relative_to(repo).as_posix(),member="conversation.json",
                           package_id=pid,package_sha256=rc["package_sha256"])
    basis={**basis,"records":list(supplied.values())}
    doc=combine_summary(repo,state,verified,writer,basis)
    covered={c["package_id"] for c in doc["coverage"]}
    for pid in sorted(completed-covered):
        if pid not in review:return None
        items=review[pid]["review"]["messages"]
        if any(r["disposition"] in {"observed","unresolved"} for r in items):return None
        doc["coverage"].append(dict(package_id=pid,disposition="no_relevant_points",keys=[],
                                    reason="逐消息核读后仅有资料、助手附言或无新观察；详见原作文覆盖表。"))
    return doc

def verified_writer(repo,state,path):
    from .cli import build_parser,run
    return run(build_parser().parse_args(["validate-formal-receipt","--repo-root",str(repo),"--state-dir",str(state),"--writer-receipt",str(path)]))

def automatic_profile(repo,state,writer):
    from .memory_lifecycle import memory_enabled, memory_closeout_status
    from .personal_summary import publish_personal_summaries
    if not memory_enabled(state):return {"status":"pending","pending_component":"memory_index_not_enabled"}
    current=memory_closeout_status(state,writer["receipt_id"])
    if current.get("status")=="ready":return current
    paths=list((state/"receipts/nightly").glob("*/"+writer["receipt_id"]+".json"))
    valid(len(paths)==1,"待恢复的作文 writer 回执不可用")
    v=verified_writer(repo,state,paths[0])
    from .writing_feedback import publish_reviewed
    publish_reviewed(state,writer,reviewed(state,writer))
    proposal=profile_document(repo,state,v,writer)
    if proposal is None:return {"status":"pending","pending_component":"personal_summary"}
    publish_personal_summaries(repo,state,proposal,formal_receipt=v)
    return memory_closeout_status(state,writer["receipt_id"])

def protected_support(root):
    from .sentence_support import _source_identity_from_package
    verified=_source_identity_from_package(root)
    fields=("source_id","source_hash","segment_type","segment_id","sentence_id","paragraph_id","question_id",
            "knowledge_point_id","source_kind","answer_exposure","source_sentence","sentence_sha256")
    receipt=load_json(root/"receipt.json");first=load_json(root/"conversation.json")["messages"][0]
    ref=dict(package_id=receipt["package_id"],package_sha256=receipt["package_sha256"],
             pointer="conversation.json#/messages/0",kind=first["role"]+"_message")
    return dict(package_id=receipt["package_id"],package_sha256=receipt["package_sha256"],record_kind="protected_sidecar",
                source_identity={k:verified[k] for k in fields},evidence_refs=[ref],
                paragraph_context={"text":None,"locator":None,"evidence_ref_indexes":[]},history_episodes=[],
                teaching_cues={k:[] for k in ("correct_observations","first_breaks","structure_cues","meaning_constraints","effective_explanations","next_read_actions")},
                vocabulary_candidates=[])

def coverage_skeleton(repo,state,pid):
    from .web_review import package_root
    root=package_root(state,repo,pid)
    return dict(version="writing-coverage/1",candidates=candidate_rows(state,root),feedback_reviews=[],
                candidate_decisions=[],messages=[dict(message_id=m["message_id"],original_sha256=digest_message(m),
                role=m["role"],disposition=None,reason=None) for m in load_json(root/"conversation.json")["messages"]])
