"""Reviewed writing observations on the existing English event and point-summary contracts."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
import re

from .errors import ValidationError
from .util import (canonical_bytes, file_sha256, learner_message_spans, load_json,
                   normalize_sentence, object_sha256, parse_iso_date)
from .writing_sources import CLOSE, META, digest_message, valid
from .writing_sessions import LEARNER_STAGES, SCHEMA, SOURCE

VERSION = "english-writing-observation/2"
# The before-attempt projection may contain general review cues, never current answers or essay sentences.
SKILLS = {
 "task_coverage": ("任务覆盖","题干要求是否逐项落实","逐项核对必答动作"),
 "reader_action": ("读者与行动","是否交代读者下一步要做什么","检查行动人、时间和请求"),
 "argument_support": ("理由与论点","理由能否支撑结论","再说明一句理由与结论的关系"),
 "concession": ("让步回应","让步后是否回到自己的立场","核对转折两边的论证方向"),
 "cohesion": ("段落衔接","句子和段落之间能否接上","核对指代和上下句的话题"),
 "register": ("语域","语气是否适合收信人与目的","按受众核称呼和请求语气"),
 "data_description": ("图表表述","类别、数值和总体是否对应","对照原图核分母、数值和比较对象"),
 "language_control": ("语言准确性","词义、搭配和句法能否准确表达原意","按原意检查一处影响表达的词句"),
 "feedback_method": ("讲解偏好","哪种反馈后确实发生了修订或明确表达偏好","按有证据的反馈方式接续")
}
METHODS={"none":"未记录具体教法","minimal_edit":"保留原意做最小修改","chinese_reason":"用中文解释修改原因",
         "compare_versions":"并排比较两版原句","ask_for_reason":"先让用户解释理由","outline_actions":"先列题干动作"}
STAGE_NAMES={"first_draft":"原样首稿","question":"本人追问","revision":"实际修订","transfer":"新任务产出",
             "acknowledgment":"仅确认理解","preference":"明确教学反馈","adoption":"采用示范"}

def skill_key(raw):
    valid(isinstance(raw,str) and raw.startswith("writing_skill:") and raw.split(":",1)[1] in SKILLS,"未知的作文技能点")
    return raw

def stable_key(host,session,message,point,version=None):
    identity=[host,session,message,point]
    if version:
        identity.append(version["fingerprint"])
    return "WR-OBS-"+object_sha256(identity)[:32].upper()

FIELDS={"version","host","session_id","message_id","original_sha256","attempt_id","task_id","point_key",
        "stage","method","shared_keys","feedback_ids","finding","next_check"}

def fields(event):
    w=event.get("writing_memory")
    valid(isinstance(w,dict) and set(w)-{"submission","message_version","writing_task","surface_reviews"}==FIELDS and w["version"]==VERSION,"作文事件版本或字段不符")
    version=w.get("message_version")
    if version:
        valid(type(version.get("number")) is int and version["number"]>=2 and re.fullmatch(r"[a-f0-9]{64}",version["fingerprint"]),
              "变更的原消息须关联已封存版本")
        valid(version.get("source_updated_at"),"已保留新版原件，未取得修改发生时间，暂存候选或用后续已定时的本人修订取证")
        valid(w["stage"] not in {"first_draft","transfer"},"同一消息的后续版本不能另记首稿或独立迁移")
    submission=w.get("submission",{})
    if "submission" in w:
        valid(isinstance(submission,dict) and {"medium","verification"}<=set(submission)<=
              {"medium","verification","attachment_sha256"} and submission["medium"] in
              {"submitted_text","typed","handwritten","speech_to_text","ocr","copied","mixed","unknown"}
              and submission["verification"] in {"as_submitted","checked_original","unverified"},"作文提交媒介或核实状态不符")
        if "attachment_sha256" in submission:
            valid(re.fullmatch(r"[a-f0-9]{64}",submission["attachment_sha256"]),"原稿附件哈希无效")
        if submission["medium"]=="copied":
            valid(w["stage"]=="adoption","照录示范应按采用记录")
        if submission["medium"] in {"ocr","handwritten"} and event["outcome"]!="unresolved":
            valid(submission["verification"]=="checked_original" and submission.get("attachment_sha256"),"手写或 OCR 判定需已核原稿附件")
        if submission["medium"]=="speech_to_text" and w["point_key"]=="writing_skill:language_control":
            valid(event["outcome"]=="unresolved","语音转写文本不能证明原始拼写或书面语法")
    point=skill_key(w["point_key"])
    valid(w["stage"] in LEARNER_STAGES and w["method"] in METHODS and
          event["origin"]=="daily" and event["event_key"]==stable_key(w["host"],w["session_id"],w["message_id"],point,version),
          "作文事件缺少稳定来源或分类")
    valid(w["task_id"]==event["unit_id"] and re.fullmatch(r"WR-ATT-[a-f0-9]{24}",w["attempt_id"])
          and re.fullmatch(r"[a-f0-9]{64}",w["original_sha256"]), "作文事件没有绑定实际课题和作答")
    valid(isinstance(w["shared_keys"],list) and len(w["shared_keys"])<=8 and
          all(isinstance(k,str) and re.fullmatch(r"(word|phrase|syntax):.{1,150}",k) for k in w["shared_keys"]),
          "词句关联需要规范键")
    valid(isinstance(w["feedback_ids"],list) and len(w["feedback_ids"])<=16 and
          all(isinstance(m,str) and m for m in w["feedback_ids"]), "反馈须绑定实际助手消息")
    valid(all(isinstance(w[k],str) and len(w[k].strip()) in range(1,2001) for k in ("finding","next_check")),
          "需要审核所得的具体观察与下次检查")
    if w["stage"] in {"question","acknowledgment","preference","adoption"}:
        valid(event["outcome"]=="unresolved","求助、确认和偏好不能计为独立成功")
    if w["stage"] in {"first_draft","revision","transfer"}:
        from .personal_summary import _ACK
        parts=[s.strip() for s in re.split(r"[，。！!、,.；;？?]+",event["user_response"]) if s.strip()]
        valid(submission.get("attachment_sha256") or not parts or not all(_ACK.fullmatch(s) for s in parts),"单纯确认理解不能算一份作品")
    return w

def prior_context(root, identity):
    """Traverse only earlier packages actually named and hashed by this frozen supplement."""
    from .web_review import package_root
    state=root.parents[2]; repo=state.parent
    todo=list(identity.get("prior_package_sha256s",{}).items())
    result={};visited=set()
    while todo:
        pid,sha=todo.pop()
        if pid in visited:continue
        visited.add(pid)
        old=package_root(state,repo,pid);manifest=load_json(old/"manifest.json")
        valid(manifest["package_canonical_sha256"]==sha,"补充链中的原件哈希不符")
        src=manifest["source_identity"]
        parent=src.get("writing_session",{})
        if any(parent.get(k)!=identity["writing_session"].get(k) for k in ("host","session_id","attempt_id")):
            continue
        for m in load_json(old/"conversation.json")["messages"]:
            result[m["message_id"]]=m
        todo.extend(src.get("prior_package_sha256s",{}).items())
    return result

def validate(event,resolved,frozen,identities):
    w=fields(event);messages={};user=None;binding=None;original_image=False
    for r in resolved:
        pid=r["binding"]["package_id"];s=identities[pid];u=s.get("writing_session",{})
        if u.get("schema")!=SCHEMA:continue
        if (u.get("host"),u.get("session_id"),u.get("attempt_id"))!=(w["host"],w["session_id"],w["attempt_id"]):continue
        valid(s["source_hash"]==object_sha256(u["task"]) and s["source_id"]==SOURCE+s["source_hash"][:32],"作文课题来源漂移")
        m=r["value"]
        if not isinstance(m,dict) or "message_id" not in m:continue
        messages[m["message_id"]]=m
        if (m["message_id"]==w["message_id"] and m["role"]=="user" and s["source_id"]==event["source_id"]
                and u["attempt_id"]==w["attempt_id"] and u["task"]["task_id"]==w["task_id"]):
            native_version=u.get("message_versions",{}).get(m["message_id"])
            valid(w.get("message_version")==native_version,"观察与封存的原消息版本不同")
            valid(event["user_response"]==m["content"] and event["observed_at"]==((native_version or {}).get("source_updated_at") or m["created_at"]) and
                  digest_message(m)==w["original_sha256"],"作文事件必须保留绑定消息的原话和发生时间")
            valid(not m["content"].lstrip().startswith(META) and not CLOSE.fullmatch(m["content"]),"操作消息不能作为作文表现")
            pkg=Path(frozen[pid]["path"]).resolve()
            submission=w.get("submission",{})
            if submission.get("attachment_sha256"):
                attachments=load_json(pkg/"manifest.json")["attachments"]
                valid(any(a["sha256"]==submission["attachment_sha256"] and a["attachment_id"] in m["attachment_refs"]
                          for a in attachments),"已核原稿必须属于这条学习者消息")
                original_image=submission["verification"]=="checked_original" and submission["medium"] in {"handwritten","ocr","mixed"}
            valid(original_image or learner_message_spans(m,s.get("source_sentence","")),"材料引用不能作为本人首稿")
            mark=u.get("observed_stages",{}).get(m["message_id"])
            valid(not mark or mark["stage"]==w["stage"],"复核分类与快存时的原始节点不同")
            user=m;binding=u
            messages.update(prior_context(pkg,s))
            for other in load_json(pkg/"conversation.json")["messages"]:
                if other["role"]=="assistant":messages[other["message_id"]]=other
    valid(user is not None,"作文观察缺少同一任务的用户原消息")
    for mid in w["feedback_ids"]:
        valid(mid in messages and messages[mid]["role"]=="assistant","助手反馈没有同源原件")
    from .writing_practice_evidence import from_capture
    proof=from_capture(pkg,user,w.get("surface_reviews"))
    valid(proof==w.get("writing_task"),"作文任务归因和原稿、曝光或评价版本不符")
    if proof:
        valid(proof["target_skill_id"]==w["point_key"],"评估目标与正式观察不同")
        required={"met":"correct","not_met":"incorrect","unknown":"unresolved","not_applicable":"unresolved"}
        valid(event["outcome"]==("unresolved" if w["stage"]=="adoption" else required[proof["target_result"]]),"局部目标结果与正式观察不符")
        valid(proof["production_origin"]!="human_revision" or w["stage"]=="revision","修订不能作为首稿")
        valid(proof["production_origin"]!="ai_or_mixed" or w["stage"]=="adoption","示范或混合产出需归因")
        valid(proof["answer_support"]=="none" or event["hint_dependence"]!="none","不能抹除提示或未知状态")
    if w["stage"]=="revision" and event["hint_dependence"] in {"hint","explanation"}:
        observed=datetime.fromisoformat(event["observed_at"].replace("Z","+00:00"))
        valid(any(instant(messages[i])<=observed for i in w["feedback_ids"]),"提示后修订需要较早的实际反馈")
    if w["stage"]=="transfer":
        valid(binding["task"].get("transfer_from") and binding["task"]["transfer_from"]!=w["attempt_id"],
              "迁移需要换题和另一实际作答身份")
        origin=binding.get("transfer_evidence")
        if origin:
            from .web_review import package_root
            state=pkg.parents[2]
            prior=package_root(state,state.parent,origin["package_id"])
            prior_binding=load_json(prior/"source.json")["identity"]["writing_session"]
            prior_rows=load_json(prior/"conversation.json")["messages"]
            valid(load_json(prior/"receipt.json")["package_sha256"]==origin["package_sha256"]
                  and prior_binding["attempt_id"]==origin["attempt_id"]==binding["task"]["transfer_from"]
                  and prior_binding["task"]["task_id"]!=w["task_id"] and prior_binding["task"]["prompt"]!=binding["task"]["prompt"]
                  and any(m["role"]=="user" and m["message_id"]==origin["message_id"] and digest_message(m)==origin["original_sha256"]
                          and instant(m)<=instant(user) for m in prior_rows),"新题未核实上次真实作品")
    if w["stage"] in {"first_draft","transfer"} and event["hint_dependence"]=="none":
        valid(not any(instant(messages[i])<=instant(user) for i in w["feedback_ids"]),
              "本题已绑定作答前的提示，应据实标记提示依赖")
        authored=normalize_sentence(event["user_response"]).casefold()
        valid(original_image or bool(re.search("[a-z]",authored)),"独立写作需要实际英文产出")
        if any(r["released"] and (r["page_released"] or r["stage"] in {"guided","review"}) and
               datetime.fromisoformat(r["issued_at"].replace("Z","+00:00"))<=instant(user) for r in binding.get("display_exposure",[])):
            raise ValidationError("本题参考或提示已经开放，独立性需据实标注")
        if original_image:
            return w
        earlier=[m for m in messages.values() if m["role"]=="assistant" and instant(m)<=instant(user)]
        valid(bool(learner_message_spans(user,binding["task"]["prompt"])) and
              normalize_sentence(binding["task"]["prompt"]).casefold() != authored,
              "题面不能算作独立写作")
        supplied=[normalize_sentence(m["content"]).casefold() for m in earlier]
        spans=[normalize_sentence(x).casefold() for section in learner_message_spans(user,binding["task"]["prompt"])
               for x in re.findall(r"[a-zA-Z][a-zA-Z '’.,!?-]*",section) if x.strip()]
        valid(spans and not any(authored in t for t in supplied) and
              not all(any(span in t for t in supplied) for span in spans),"采用或照录助手示范不能算独立使用")
    return w

def instant(message):
    return datetime.fromisoformat(message["created_at"].replace("Z","+00:00"))

def make_action(repo,state,decision):
    """Materialize a human/model-reviewed observation; never infer grading from raw text."""
    from .web_review import package_root
    pid=decision["package_id"];root=package_root(state,repo,pid);s=load_json(root/"source.json")["identity"]
    u=s["writing_session"];msgs=load_json(root/"conversation.json")["messages"]
    hits=[(i,m) for i,m in enumerate(msgs) if m["message_id"]==decision["message_id"] and m["role"]=="user"]
    valid(len(hits)==1,"原包缺少指定用户消息")
    idx,m=hits[0];c=load_json(root/"receipt.json")
    def ref(package,sha,index,kind):
        return dict(package_id=package,package_sha256=sha,pointer=f"conversation.json#/messages/{index}",kind=kind)
    refs=[ref(pid,c["package_sha256"],idx,"user_message")];feedback_ids=[]
    for f in decision.get("feedback",[]):
        fr=package_root(state,repo,f["package_id"]);fm=load_json(fr/"conversation.json")["messages"]
        matches=[(i,x) for i,x in enumerate(fm) if x["message_id"]==f["message_id"] and x["role"]=="assistant"]
        valid(len(matches)==1,"指定助手反馈没有取到原件")
        if f["package_id"]!=pid and f["message_id"] in prior_context(root,s):
            parent=next(iter(s["prior_package_sha256s"]))
            bound=dict(package_id=pid,package_sha256=c["package_sha256"],kind="source_text",
                       pointer="source.json#/identity/prior_package_sha256s/"+parent)
            if bound not in refs:refs.append(bound)
        else:
            refs.append(ref(f["package_id"],load_json(fr/"receipt.json")["package_sha256"],matches[0][0],"assistant_message"))
        feedback_ids.append(f["message_id"])
    w=dict(version=VERSION,host=u["host"],session_id=u["session_id"],message_id=m["message_id"],original_sha256=digest_message(m),
           attempt_id=u["attempt_id"],task_id=s["unit_id"],point_key=skill_key(decision["point_key"]),stage=decision["stage"],
           method=decision.get("method","none"),shared_keys=decision.get("shared_keys",[]),feedback_ids=feedback_ids,
           finding=decision["finding"],next_check=decision["next_check"])
    from .writing_practice_evidence import from_capture
    observed=from_capture(root,m,decision.get("surface_reviews"))
    if observed is not None:
        w.update(writing_task=observed,surface_reviews=decision.get("surface_reviews",{}))
    if "submission" in decision:
        w["submission"]=decision["submission"]
    version=u.get("message_versions",{}).get(m["message_id"])
    if version:
        w["message_version"]=version
    stable=stable_key(w["host"],w["session_id"],m["message_id"],w["point_key"],version)
    event=dict(event_key=stable,kind="correction" if w["stage"]=="revision" else "answer" if w["stage"] in {"first_draft","transfer"} else "review",
               origin="daily",task_id=None,source_commit=None,source_id=s["source_id"],unit_id=s["unit_id"],
               bank_ids=decision.get("bank_ids",[]),sentence_pattern_ids=decision.get("sentence_pattern_ids",[]),concept_ids=decision.get("concept_ids",[]),
               observed_at=(version or {}).get("source_updated_at") or m["created_at"],outcome=decision["outcome"],hint_dependence=decision["hint_dependence"],reasoning_status="not_observed",
               user_response=m["content"],reasoning="",correction="",writing_memory=w)
    fields(event)
    return dict(action_id=stable,action_type="learning_event_append",event=event,reason=w["finding"],evidence_refs=refs)

def safe_observation(event):
    w=event["writing_memory"];label,focus,next_check=SKILLS[w["point_key"].split(":",1)[1]]
    stage=w["stage"];level=event["hint_dependence"]
    if stage=="preference":
        observed="用户明确评价了本次讲解方式"+("，涉及"+METHODS[w["method"]] if w["method"]!="none" else "")
    elif stage=="adoption":observed="用户采用了示范，独立使用待观察"
    elif stage=="acknowledgment":observed="用户确认理解，实际改写待观察"
    elif stage=="question":observed="这次主动追问，尚待在自己的作品里检查"
    else:
        names={"correct":"满足这一项要求","incorrect":"这一项仍需修改","unresolved":"是否满足这一项仍待核对"}
        conditions={"none":"无提示","hint":"有提示","explanation":"看过讲解","not_observed":"提示情况未知"}
        observed=f'{STAGE_NAMES[stage]}（{conditions[level]}）{names[event["outcome"]]}'
    if w.get("writing_task") and stage in {"first_draft","revision","transfer"}:
        task=w["writing_task"]
        cues={"form":"指定形式","function":"提示了功能","none":"已核无目标提示","unknown":"任务线索待核"}
        supports={"none":"已核无答案帮助","hint":"看过提示","example":"看过示例","ai_rewrite":"看过改写","unknown":"帮助情况待核"}
        observed=f'{STAGE_NAMES[stage]}（{cues[task["task_cue"]]}，{supports[task["answer_support"]]}）{names[event["outcome"]]}'
    instruction=""
    if stage=="revision" and w["feedback_ids"] and w["method"]!="none":
        instruction=f"本次修订前的反馈方式为{METHODS[w['method']]}。"
    if w.get("writing_task",{}).get("open_task_transfer_observed"):
        instruction+="本次记录一次开放新情境的自然迁移观察。"
    return f"{label} · {observed}。{instruction}核对点是{focus}。",next_check+"。"

def summary_records(repo,state,verified,writer):
    """Build answer-safe functional summaries; detailed drafts stay in the protected originals."""
    from .learning_state import load_learning_events,learning_events_path
    from .web_review import package_root
    matched={a["assigned_id"]:a["learning_event_sha256"] for a in writer["action_results"]
             if a.get("writing_contract")==VERSION and a.get("result") in {"applied","skipped"}}
    events=[r for r in load_learning_events(learning_events_path(repo)) if r["event_id"] in matched]
    valid(len(events)==len(matched),"原生 writer 的观察未在正式账本验回")
    grouped=defaultdict(list);by_package=defaultdict(set)
    for r in events:
        ev=r["event"];w=fields(ev);valid(r["record_sha256"]==matched[r["event_id"]],"正式事件摘要失配")
        refs=[];point=w["point_key"];observed,check=safe_observation(ev)
        for b in r["evidence_refs"]:
            if not b["pointer"].startswith("conversation.json#/messages/"):continue
            pkg=package_root(state,repo,b["package_id"]);member=pkg/"conversation.json";loc=b["pointer"].split("#/",1)[1]
            # Scoped archive-aware references keep working after the hot package is moved.
            day=load_json(pkg/"receipt.json")["study_date"]
            logical=(state/"packages"/day/b["package_id"]/"conversation.json").relative_to(repo).as_posix()
            refs.append(dict(kind="writing_original_v2",path=logical,package_id=b["package_id"],package_sha256=b["package_sha256"],
                             member="conversation.json",sha256=file_sha256(member),locator=loc))
            by_package[b["package_id"]].add(point)
        ep=dict(episode_id=r["event_id"],source_id=ev["source_id"],unit_id=ev["unit_id"],study_date=parse_iso_date(ev["observed_at"]),
                observed_at=ev["observed_at"],first_attempt=observed,correction=METHODS[w["method"]] if w["feedback_ids"] else "详见封存的本轮讲解",
                later_observation=observed if w["stage"] in {"revision","transfer","acknowledgment"} else "本条未观察后续修订",
                hint_dependence=ev["hint_dependence"],formal_ids=[r["event_id"],*ev["bank_ids"],*ev["sentence_pattern_ids"]],
                formal_event_sha256=r["record_sha256"],attempt_id=w["attempt_id"],writing_stage=w["stage"],linked_keys=w["shared_keys"],next_action=check,evidence=refs)
        if w.get("writing_task"):
            ep["writing_task"]=w["writing_task"]
        grouped[point].append((ep,check))
    records=[]
    for point,pairs in sorted(grouped.items()):
        pairs.sort(key=lambda pair:pair[0]["observed_at"])
        records.append(dict(writing_summary_version="writing-point/1",key=point,aliases=[],visibility="answer_free",teaching_hint=pairs[-1][1],
                            episodes=[e for e,_ in pairs],latest_episode_id=pairs[-1][0]["episode_id"]))
    return records,by_package

def validate_point_publication(repo,record,authority):
    """Only exact canonical writer outcomes may feed the safe writing-skill projection."""
    valid(authority and authority.get("schema_version")=="english_formal_receipt_validation_v1"
          and authority.get("status")=="PASS" and authority.get("writer_receipt_path"),"作文点摘要需要原生 writer 验证")
    path=Path(authority["writer_receipt_path"])
    valid(path.is_file() and file_sha256(path)==authority["writer_receipt_sha256"],"个人摘要 writer 凭据失配")
    receipt=load_json(path)
    valid(receipt["receipt_id"]==authority["receipt_id"] and record.get("writing_summary_version")=="writing-point/1","作文点摘要版本不符")
    point=skill_key(record["key"])
    valid(record["visibility"]=="answer_free" and not record.get("aliases") and not record.get("contrasts"),"作文课前面只允许核过的技能摘要")
    from .learning_state import load_learning_events,learning_events_path
    canonical={r["event_id"]:r for r in load_learning_events(learning_events_path(repo))}
    accepted={x["assigned_id"]:x["learning_event_sha256"] for x in receipt["action_results"] if x.get("writing_contract")==VERSION}
    for ep in record.get("episodes",[]):
        ident=ep["episode_id"];row=canonical.get(ident)
        valid(row and row["record_sha256"]==accepted.get(ident)==ep.get("formal_event_sha256"),"作文摘要不能来自未提交的候选")
        e=row["event"];w=e["writing_memory"];observation,cue=safe_observation(e)
        valid(ep.get("writing_task")==w.get("writing_task") and w["point_key"]==point and w["attempt_id"]==ep["attempt_id"] and w["stage"]==ep["writing_stage"] and
              ep["first_attempt"]==observation and ep["next_action"]==cue and record["teaching_hint"]==cue,
              "作文摘要与已复核的观察不同")

def combine_summary(repo,state,verified,writer,document):
    from .personal_summary import SCHEMA as POINT_SCHEMA
    records,by_package=summary_records(repo,state,verified,writer)
    result={**document,"schema_version":POINT_SCHEMA,"records":list(document.get("records",[])),"coverage":list(document.get("coverage",[]))}
    overlap={r["key"] for r in result["records"]}&{r["key"] for r in records}
    valid(not overlap,"作文点由同一收尾从正式观察生成；共享词句由本科作者提交")
    result["records"].extend(records)
    coverage={c["package_id"]:dict(c) for c in result["coverage"]}
    for pid,keys in by_package.items():
        if verified["package_dispositions"].get(pid)!="completed":continue
        entry=coverage.setdefault(pid,{"package_id":pid})
        entry.update(disposition="covered",keys=sorted(set(entry.get("keys",[]))|keys))
    result["coverage"]=list(coverage.values())
    return result
