"""Scope and supplement authored writing chats through the existing English Capture."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from itertools import groupby
from pathlib import Path
import mimetypes
import os
import re
from tempfile import TemporaryDirectory

from .current_unit import collect_attachments
from .packages import create_conversation_package, unit_binding_path
from .constants import REPO_ROOT
from .util import (LOCAL_ZONE, atomic_write_json, bytes_sha256, canonical_bytes, exclusive_lock,
                   file_sha256, load_json, object_sha256, text_sha256)
from .web_review import package_root
from .writing_sources import CLOSE, META, digest_message, valid

SCHEMA = "english-writing-session/2"
STAGES = {"first_draft","question","feedback","revision","transfer","switch","end","resume","reference","acknowledgment","preference","adoption"}
LEARNER_STAGES = {"first_draft","question","revision","transfer","acknowledgment","preference","adoption"}
SOURCE = "WRUNIT-"

def directory(state, transcript):
    return state/"writing-sessions"/object_sha256(transcript.key)

def read_book(folder, tr):
    path=folder/"session.json"
    if not path.exists():
        return dict(schema=SCHEMA,source_locator=tr.origin,host=tr.host,session_id=tr.session,units=[],captures=[],annotations={},candidates=[],intent=None)
    book=load_json(path)
    valid(book.get("schema")==SCHEMA and (book["host"],book["session_id"])==(tr.host,tr.session),"会话身份不符")
    return book

def persist(folder, book):
    atomic_write_json(folder/"session.json",book)

def row_by_id(tr):
    return {m["message_id"]:m for m in tr.messages}

def study_day(m):
    return datetime.fromisoformat(m["created_at"].replace("Z","+00:00")).astimezone(LOCAL_ZONE).date().isoformat()

def durable_unit(tr, first, task, prompt, origin, attachments=(), transfer_from=None):
    valid(isinstance(task,str) and re.fullmatch(r"[\w.:-]{1,160}",task) and isinstance(prompt,str) and prompt.strip(),
          "课题身份和已核原题或课程目标需要齐全")
    inputs=[]
    for name in attachments:
        p=Path(name).resolve(strict=True)
        valid(p.is_file(),"课件原件缺失")
        inputs.append({"path":str(p),"sha256":file_sha256(p)})
    identity={"schema":"english_writing_task_v2","task_id":task,"prompt":prompt,"origin":origin,
              "material_hashes":[i["sha256"] for i in inputs]}
    if transfer_from:identity["transfer_from"]=transfer_from
    sha=object_sha256(identity)
    return dict(attempt_id="WR-ATT-"+object_sha256([tr.key,first,sha])[:24],start=first,stop=None,
                task=identity,sha256=sha,inputs=inputs)

def transfer_origin(repo, state, book, previous, task, prompt, start):
    """Resolve an earlier piece of learner work, also when the new lesson is in a different chat."""
    if previous.startswith("EN-PKG-"):
        packages=[previous]
    else:
        packages=[c["package_id"] for c in reversed(book["captures"]) if c["attempt_id"]==previous]
    for pid in packages:
        root=package_root(state,repo,pid)
        source=load_json(root/"source.json")["identity"].get("writing_session",{})
        valid(source.get("schema")==SCHEMA,"迁移来源必须是已封存的作文作答")
        old=source["task"]
        valid(old["task_id"]!=task and old["prompt"].strip()!=prompt.strip(),"独立迁移须有不同的课题和真实题面")
        journal=state/"writing-sessions"/object_sha256(source["host"]+":"+source["session_id"])/"session.json"
        marks=dict(source.get("observed_stages",{}))
        if journal.is_file():
            marks.update(load_json(journal)["annotations"])
        for m in reversed(load_json(root/"conversation.json")["messages"]):
            mark=marks.get(m["message_id"],{})
            if m["role"]!="user" or mark.get("stage") not in {"first_draft","revision","transfer"}:
                continue
            if mark["original_sha256"]!=digest_message(m) or m["message_id"]==start["message_id"] and book["session_id"]==source["session_id"]:
                continue
            valid(datetime.fromisoformat(m["created_at"].replace("Z","+00:00"))<=
                  datetime.fromisoformat(start["created_at"].replace("Z","+00:00")),"迁移来源晚于新题")
            return dict(package_id=pid,package_sha256=load_json(root/"receipt.json")["package_sha256"],
                        attempt_id=source["attempt_id"],message_id=m["message_id"],original_sha256=digest_message(m))
    raise ValueError("迁移须能定位上一次实际作品的会话、原包和消息")

def register_unit(repo,state,tr,*,task,prompt,origin,first=None,stop=None,attachments=(),transfer_from=None,include_from=None):
    if repo.resolve()==REPO_ROOT.resolve() and (str(origin).lower().find("isolated_exam_fixture")>=0 or task.startswith("SYN-")):
        raise ValueError("合成课不能进入个人 Capture")
    folder=directory(state,tr)
    rows=row_by_id(tr)
    if first is None:
        first=next((m["message_id"] for m in reversed(tr.messages) if m["role"]=="user"),None)
    valid(first in rows and (not stop or stop in rows),"课节起止必须来自这个会话")
    order={mid:n for n,mid in enumerate(rows)}
    with exclusive_lock(folder/"capture.lock"):
        b=read_book(folder,tr)
        parent=transfer_origin(repo,state,b,transfer_from,task,prompt,rows[first]) if transfer_from else None
        unit=durable_unit(tr,first,task,prompt,origin,attachments,parent["attempt_id"] if parent else None)
        if parent:
            unit["transfer_evidence"]=parent
        prior=next((u for u in b["units"] if u["start"]==first),None)
        if prior:
            valid(prior["attempt_id"]==unit["attempt_id"],"该起点已绑定另一课题")
            if include_from:
                current_start=prior.get("evidence_start",prior["start"])
                valid(include_from in order and order[include_from]<=order[current_start],"补入的起点应在既有课节之前")
                earlier=[x for x in b["units"] if order[x["start"]]<order[first]]
                valid(all(x["stop"] and order[x["stop"]]<order[include_from] for x in earlier),"补入的消息已属于另一课节")
                prior["evidence_start"]=include_from
            if stop:
                valid(order[first]<=order[stop],"课节起止颠倒")
                valid(not prior["stop"] or order[stop]>=order[prior["stop"]],"历史已核范围不能缩短")
                valid(all(order[other.get("evidence_start",other["start"])]>order[stop] for other in b["units"] if order[other["start"]]>order[first]),
                      "补充范围与下一课节重叠")
                prior["stop"]=stop
            persist(folder,b)
            return prior
        valid(not stop or order[first]<=order[stop],"课节起止颠倒")
        later=[old for old in b["units"] if order[old["start"]]>order[first]]
        if later:
            valid(stop and order[stop]<min(order[x.get("evidence_start",x["start"])] for x in later),
                  "补入历史课节须提供与已登记课节互不重叠的结束消息")
        earlier=[old for old in b["units"] if order[old["start"]]<order[first]]
        if earlier:
            prev=earlier[-1]
            if prev["stop"] is None:
                previous_id=tr.messages[order[first]-1]["message_id"]
                already={mid for c in b["captures"] if c["attempt_id"]==prev["attempt_id"] for mid in c["messages"]}
                valid(all(order[mid]<order[first] for mid in already),"新课节与已封存证据重叠")
                prev["stop"]=previous_id
            else:
                valid(order[prev["stop"]]<order[first],"课节范围重叠")
        unit["stop"]=stop
        b["units"].append(unit)
        b["units"].sort(key=lambda x:order[x["start"]])
        persist(folder,b)
        return unit

def scope_rows(book,tr):
    rows=row_by_id(tr); order=list(rows); positions={mid:i for i,mid in enumerate(order)}
    units={}
    for u in book["units"]:
        first=u.get("evidence_start",u["start"])
        valid(first in positions and u["start"] in positions and (not u["stop"] or u["stop"] in positions),"原始会话缺少已绑定边界")
        end=positions[u["stop"]]+1 if u["stop"] else len(order)
        if end>positions[first]:
            # Keep a flushed last assistant even if it arrived after the close command.
            # A later, unrelated user turn never silently extends this lesson.
            close=next((i for i in range(positions[first],end) if rows[order[i]]["role"]=="user"
                        and CLOSE.fullmatch(rows[order[i]]["content"])),None)
            if close is not None:
                end=next((i for i in range(close+1,end) if rows[order[i]]["role"]=="user"),end)
        for mid in order[positions[first]:end]:
            valid(mid not in units,"课节范围相互覆盖")
            units[mid]=u
    return units

def review_source(tr):
    return dict(host=tr.host,session_id=tr.session,total=len(tr.messages),source_gaps=tr.gaps,
                messages=[{"id":m["message_id"],"role":m["role"],"at":m["created_at"],"characters":len(m["content"]),
                           "sha256":digest_message(m)} for m in tr.messages])

def staged(scope,rows,stage,mid):
    if not stage or not mid:return None
    valid(stage in STAGES and mid in scope,"观察没有绑定本课的真实消息")
    m=rows[mid]
    if stage in LEARNER_STAGES:
        valid(m["role"]=="user" and not m["content"].lstrip().startswith(META) and not CLOSE.fullmatch(m["content"]),"所选消息不是学习者本人的作答")
    if stage=="feedback":valid(m["role"]=="assistant","讲评需要助手的原消息")
    return {"stage":stage,"original_sha256":digest_message(m),"role":m["role"]}

def version_history(book):
    history=defaultdict(list)
    for cap in book["captures"]:
        for mid,digest in cap["messages"].items():
            version=cap.get("versions",{}).get(mid,{})
            versions=history[mid]
            number=version.get("number",1)
            ref=dict(package_id=cap["package_id"],sha256=cap["sha256"],digest=digest,index=cap["positions"][mid],
                     attempt_id=cap["attempt_id"],gaps=cap["gaps"].get(mid,[]),assets=cap.get("assets",{}).get(mid),version=version)
            old=next((r for r in versions if r["version"].get("number",1)==number),None)
            if old:
                valid(old["digest"]==digest and old["attempt_id"]==ref["attempt_id"],"已封存的消息版本发生冲突")
                if old["gaps"] and not ref["gaps"]:
                    versions[versions.index(old)]=ref
                continue
            if versions:
                parent=versions[-1]
                valid(number==parent["version"].get("number",1)+1 and version.get("previous_package_id")==parent["package_id"]
                      and version.get("previous_package_sha256")==parent["sha256"],"新版本没有绑定前一份已封存原件")
            else:
                valid(number==1,"缺少原消息第一版")
            versions.append(ref)
    return history

def evidence_index(book):
    return {mid:versions[-1] for mid,versions in version_history(book).items()}

def captured_entry(root,receipt,unit,messages,gaps):
    doc=load_json(root/"conversation.json")["messages"];source=load_json(root/"source.json")["identity"]
    expected={m["message_id"]:digest_message(m) for m in messages}
    positions={m.get("message_id"):i for i,m in enumerate(doc) if m.get("message_id") in expected}
    valid(set(expected)==set(positions) and all(digest_message(doc[i])==expected[mid] for mid,i in positions.items()),
          "快存回读与原消息不一致")
    parts={a["attachment_id"]:a["sha256"] for a in load_json(root/"manifest.json")["attachments"]}
    assets={mid:[parts[r] for r in doc[i]["attachment_refs"]] for mid,i in positions.items()}
    versions=source.get("writing_session",{}).get("message_versions",{})
    return dict(package_id=receipt["package_id"],sha256=receipt["package_sha256"],study_date=receipt["study_date"],
                attempt_id=unit["attempt_id"],messages=expected,positions=positions,gaps=gaps,assets=assets,versions=versions,
                source_id=source.get("source_id"),source_unit=source.get("unit_id") or source.get("sentence_id"))

def verify_originals(repo,state,book,tr,rows):
    for cap in book["captures"]:
        root=package_root(state,repo,cap["package_id"])
        manifest=load_json(root/"manifest.json")
        valid(manifest["package_canonical_sha256"]==cap["sha256"],"已封存原件与回执版本不同")
        contents=load_json(root/"conversation.json")["messages"]
        parts={a["attachment_id"]:a["sha256"] for a in manifest["attachments"]}
        for mid,n in cap["positions"].items():
            old=contents[n]
            valid(mid in rows and old["message_id"]==mid and digest_message(old)==cap["messages"][mid],"原件覆盖校验失败")
            if mid in cap.get("assets",{}):
                valid(cap["assets"][mid]==[parts[r] for r in old["attachment_refs"]],"封存附件与该消息版本不同")

def compare_versions(repo,state,tr,book,scope,rows):
    """Match a source snapshot against every sealed version, including a replay of an older export."""
    matches={};changes={};cache={}
    def member(ref):
        pid=ref["package_id"]
        if pid not in cache:
            root=package_root(state,repo,pid)
            manifest=load_json(root/"manifest.json")
            valid(manifest["package_canonical_sha256"]==ref["sha256"],"已存消息版本校验失败")
            cache[pid]=(load_json(root/"conversation.json")["messages"],{a["attachment_id"]:a["sha256"] for a in manifest["attachments"]})
        contents,parts=cache[pid];old=contents[ref["index"]]
        return old,[parts[a] for a in old["attachment_refs"]]
    with TemporaryDirectory(prefix="writing-versions-") as temporary:
        for mid,versions in version_history(book).items():
            valid(mid in rows,"当前来源缺少已封存的原消息，请补全该分支")
            if mid not in scope:continue
            row=rows[mid];current=digest_message(row)
            local={**row,"metadata":dict(row["metadata"]),"attachment_refs":[]}
            attachments,gaps=attachments_for(tr,[local],Path(temporary))
            by_ref={f"ATT-{i:03d}":file_sha256(Path(a["path"])) for i,a in enumerate(attachments,1)}
            assets=[by_ref[a] for a in local["attachment_refs"]]
            for ref in reversed(versions):
                if ref["digest"]!=current:continue
                prior_assets=ref["assets"] if ref["assets"] is not None else member(ref)[1]
                # Missing temporary originals can be recovered from the sealed copy. A newly available file repairs a gap.
                if assets==prior_assets or (gaps and set(assets)<=set(prior_assets)) or ref["gaps"]:
                    matches[mid]=ref
                    break
            if mid in matches:continue
            parent=versions[-1];original,_=member(parent)
            valid(parent["attempt_id"]==scope[mid]["attempt_id"] and (original["role"],original["created_at"])==(row["role"],row["created_at"]),
                  "同一消息的作者、原始时间或作答归属不一致，保留全部已封存版本待核")
            updated=row["metadata"].get("host_updated_at")
            if updated:
                instant=datetime.fromisoformat(updated.replace("Z","+00:00"))
                valid(instant.tzinfo and instant>=datetime.fromisoformat(row["created_at"].replace("Z","+00:00")),"原消息的修改时间不符")
            changes[mid]=dict(number=parent["version"].get("number",1)+1,previous_package_id=parent["package_id"],
                previous_package_sha256=parent["sha256"],fingerprint=object_sha256([current,assets]),
                source_updated_at=updated)
    return matches,changes

def adopt_legacy(repo,state,tr,book,scope,rows):
    if tr.host!="codex":return
    known=evidence_index(book)
    ids={c["package_id"] for c in book["captures"]}
    for mid,unit in scope.items():
        m=rows[mid];line=m["metadata"].get("rollout_line")
        if mid in known or line is None:continue
        native="EN-UNIT-"+object_sha256([tr.session,line])[:24].upper()
        pointer=unit_binding_path(state,text_sha256("codex-thread:"+tr.session),native)
        if not pointer.exists():continue
        pid=load_json(pointer).get("package_id")
        if pid in ids:continue
        root=package_root(state,repo,pid);manifest=load_json(root/"manifest.json");identity=manifest["source_identity"]
        if not str(identity.get("source_id","")).startswith(("WRCOURSE-",SOURCE)):continue
        actual=load_json(root/"conversation.json")["messages"]
        if not all(scope.get(r.get("message_id"))==unit for r in actual):continue
        gaps={r["message_id"]:manifest.get("missing_fields",[]) for r in actual} if any(
            str(f).startswith("attachments.") for f in manifest.get("missing_fields",[])) else {}
        cap=captured_entry(root,load_json(root/"receipt.json"),unit,[rows[r["message_id"]] for r in actual],gaps)
        book["captures"].append(cap);ids.add(pid);known.update(cap["messages"])

def attachments_for(tr,messages,tmp):
    if tr.host=="codex":
        # Attribute a missing original to its message without guessing from unrelated files.
        attachments=[];lookup={};gaps={}
        for m in messages:
            if m["metadata"].get("source_system")=="english_writing_reader":
                continue  # Typed UI events have their native action ID; they have no rollout line or uploaded image.
            found,missing=collect_attachments([m],[],tmp)
            refs=[]
            for a in found:
                key=(str(a["path"]),a["role"])
                if key not in lookup:
                    lookup[key]=len(attachments)+1
                    attachments.append(a)
                refs.append(f"ATT-{lookup[key]:03d}")
            m["attachment_refs"]=refs
            if missing:gaps[m["message_id"]]=missing
        return attachments,gaps
    attachments=[];known={};gaps={}
    for m in messages:
        refs=[]
        for index,raw,mime in m.pop("original_attachments",[]):
            if raw is None:
                gaps.setdefault(m["message_id"],[]).append("缺原附件 "+str(index));continue
            data=raw if isinstance(raw,bytes) else Path(raw).read_bytes()
            sha=bytes_sha256(data)
            if sha not in known:
                name=Path(str(m["metadata"]["original_content_blocks"][index].get("name") or "original")).name
                p=tmp/(sha[:16]+"-"+name)
                p.write_bytes(data)
                attachments.append({"path":str(p),"role":"other_attachment","mime_type":mime,"expected_sha256":sha})
                known[sha]=len(attachments)
            refs.append(f"ATT-{known[sha]:03d}")
        m["attachment_refs"]=refs
    return attachments,gaps

def seal(repo,state,tr,folder,b,intent,rows):
    u=next(u for u in b["units"] if u["attempt_id"]==intent["attempt"])
    ref=("codex-thread:" if tr.host=="codex" else tr.host+"-thread:")+tr.session
    pointer=unit_binding_path(state,text_sha256(ref),intent["key"])
    if pointer.exists():
        root=package_root(state,repo,load_json(pointer)["package_id"])
        receipt=load_json(root/"receipt.json")
        gaps=load_json(root/"source.json")["identity"]["writing_session"]["missing_attachments"]
        original=load_json(root/"conversation.json")["messages"]
        valid({m["message_id"]:digest_message(m) for m in original}==intent["digests"],"原操作回执与版本意图不同")
    else:
        original=[rows[mid] for mid in intent["ids"]]
        valid(all(digest_message(m)==intent["digests"][m["message_id"]] for m in original),"待恢复快存的原消息已变化")
        messages=[{**m,"metadata":dict(m["metadata"]),"attachment_refs":[]} for m in original]
        with TemporaryDirectory(prefix="writing-attachments-") as temporary:
            tmp=Path(temporary)
            attachments,gaps=attachments_for(tr,messages,tmp)
            if intent.get("versions") and not intent["key"].startswith("EN-WRREPAIR-"):
                for a in attachments:
                    a["expected_sha256"]=file_sha256(Path(a["path"]))
                for msg in messages:
                    v=intent["versions"].get(msg["message_id"])
                    if v:
                        hashes=[attachments[int(ref[4:])-1]["expected_sha256"] for ref in msg["attachment_refs"]]
                        valid(v["fingerprint"]==object_sha256([digest_message(msg),hashes]),"封存期间原件再次变化，待核该版本")
            extras=list(u["inputs"])
            offered=[]
            origin=u["task"]["origin"]
            if repo.resolve()==REPO_ROOT.resolve() and isinstance(origin,dict) and origin.get("display_attempt_id") and tr.native:
                from .article_workspace import RUNTIME
                from .writing_workspace import folder as display_folder
                key="WRITING-"+object_sha256(tr.native)[:24]
                start=datetime.fromisoformat(original[0]["created_at"].replace("Z","+00:00"))
                end=datetime.fromisoformat(original[-1]["created_at"].replace("Z","+00:00"))
                revisions=[]
                for path in (display_folder(RUNTIME,key)/"revisions").glob("*.json"):
                    revision=load_json(path)
                    if revision.get("attempt_id")!=origin["display_attempt_id"] or revision.get("source_version")!=origin.get("lesson_sha256") or not revision.get("issued_at"):continue
                    stamp=datetime.fromisoformat(revision["issued_at"].replace("Z","+00:00"))
                    if stamp<=end:revisions.append((stamp,revision["revision"],path,revision))
                earlier=[r for r in revisions if r[0]<start]
                selected=([max(earlier,key=lambda x:(x[0],x[1]))] if earlier else [])+[r for r in revisions if r[0]>=start]
                # Re-locking a page cannot undo an earlier disclosure on this attempt.
                exposures=[r for r in earlier if r[3].get("status")=="active" and
                           (r[3].get("page_release") or r[3].get("stage") in {"guided","review"})]
                if exposures:
                    first_exposure=min(exposures,key=lambda r:(r[0],r[1]))
                    if first_exposure not in selected:
                        selected.append(first_exposure)
                for _,_,path,revision in sorted(selected,key=lambda x:(x[0],x[1])):
                    extras.append({"path":str(path),"sha256":file_sha256(path)})
                    image=(revision.get("page_release") or {}).get("sha256")
                    if image:extras.append({"path":str(RUNTIME/"writing-pages"/(image+".webp")),"sha256":image})
                    offered.append({"revision":revision["revision"],"issued_at":revision["issued_at"],"stage":revision.get("stage"),
                                    "page_released":bool(image),"released":revision.get("status")=="active"})

            if getattr(tr,"export_nodes",None):
                native=tmp/"original-messages.json"
                native.write_bytes(canonical_bytes({"source_object_sha256":tr.export_version,
                    "messages":[tr.export_nodes[m["message_id"]] for m in original]}))
                extras.append({"path":str(native),"sha256":file_sha256(native)})
            for extra in extras:
                p=Path(extra["path"])
                if not p.is_file() or file_sha256(p)!=extra["sha256"]:
                    gaps.setdefault(messages[0]["message_id"],[]).append("课件原件缺失或改变："+p.name)
                else:attachments.append({"role":"other_attachment","path":str(p),"expected_sha256":extra["sha256"]})
            history={c["package_id"]:c["sha256"] for c in b["captures"]}
            parents=intent["parents"]
            binding=dict(schema=SCHEMA,host=tr.host,session_id=tr.session,attempt_id=u["attempt_id"],task=u["task"],
                         missing_attachments=gaps, display_exposure=offered, observed_stages={mid:b["annotations"][mid] for mid in intent["ids"] if b["annotations"].get(mid,{}).get("original_sha256")==intent["digests"][mid]})
            if intent.get("versions"):
                binding["message_versions"]=intent["versions"]
            if u.get("transfer_evidence"):
                binding["transfer_evidence"]=u["transfer_evidence"]
            source=dict(source_id=SOURCE+u["sha256"][:32],source_hash=u["sha256"],unit_id=u["task"]["task_id"],
                        knowledge_point_id=u["task"]["task_id"],source_sentence=u["task"]["prompt"],
                        source_kind="explanation",answer_exposure="protected",review_route="local",writing_session=binding)
            if parents:
                source.update(prior_package_ids=parents,prior_package_sha256s={p:history[p] for p in parents})
            last=original[-1]
            at=intent.get("versions",{}).get(last["message_id"],{}).get("source_updated_at") or last["created_at"]
            req=dict(capture_mode="independent_unit",idempotency_key=intent["key"],segment_key=intent["key"],
                     thread_ref=ref,study_date=intent["day"],occurred_at=at,
                     source=source,conversation=messages,attachments=attachments,
                     missing_fields=["attachments."+mid+"."+str(i) for mid,issues in gaps.items() for i in range(len(issues))])
            root,receipt=create_conversation_package(state,req)
    entry=captured_entry(root,receipt,u,original,gaps)
    if not any(c["package_id"]==entry["package_id"] for c in b["captures"]):b["captures"].append(entry)
    b["intent"]=None;persist(folder,b)
    return entry

def checkpoint(repo,state,tr,*,stage=None,message_id=None,candidates=(),closing=False,repair=False,admin_tail=False,only_day=None):
    folder=directory(state,tr);rows=row_by_id(tr)
    with exclusive_lock(folder/"capture.lock"):
        b=read_book(folder,tr);scope=scope_rows(b,tr)
        if only_day:
            scope={mid:u for mid,u in scope.items() if study_day(rows[mid])==only_day or
                   (rows[mid]["metadata"].get("host_updated_at") and study_day({"created_at":rows[mid]["metadata"]["host_updated_at"]})==only_day)}
            if not scope and b["units"]:return {"status":"no_writing_units","packages":[],"formal_write_count":0}
        if not scope:
            return {"status":"scope_review_required","source":review_source(tr),"formal_write_count":0}
        if b.get("paused") and not closing:
            return {"status":"automatic_capture_paused","formal_write_count":0}
        mark=staged(scope,rows,stage,message_id)
        if mark:
            prior=b["annotations"].get(message_id)
            if prior and prior["original_sha256"]!=mark["original_sha256"]:
                b.setdefault("annotation_history",{}).setdefault(message_id,{})[prior["original_sha256"]]=prior
            else:
                valid(not prior or prior==mark,"同一消息的同一版本已绑定其他观察阶段")
            b["annotations"][message_id]=mark
        if b["intent"]:seal(repo,state,tr,folder,b,b["intent"],rows)
        adopt_legacy(repo,state,tr,b,scope,rows)
        matched,changes=compare_versions(repo,state,tr,b,scope,rows)
        for mid,version in changes.items():
            u=scope[mid];row=rows[mid]
            parents=[c["package_id"] for c in b["captures"] if c["attempt_id"]==u["attempt_id"]][-1:]
            parents=list(dict.fromkeys([*parents,version["previous_package_id"]]))
            changed_day=study_day({"created_at":version["source_updated_at"] or row["created_at"]})
            intent=dict(attempt=u["attempt_id"],day=changed_day,ids=[mid],digests={mid:digest_message(row)},parents=parents,versions={mid:version})
            intent["key"]="EN-WRVERSION-"+object_sha256([tr.key,intent])[:30].upper()
            b["intent"]=intent;persist(folder,b);seal(repo,state,tr,folder,b,intent,rows)
        saved=evidence_index(b)
        # Freeze only uncovered bytes. Each permanent boundary survives a timeout or later appended turns.
        pending=[m for m in tr.messages if m["message_id"] in scope and m["message_id"] not in saved]
        for (aid,day),group in groupby(pending,key=lambda m:(scope[m["message_id"]]["attempt_id"],study_day(m))):
            group=list(group);ids=[m["message_id"] for m in group]
            prev=[c["package_id"] for c in b["captures"] if c["attempt_id"]==aid]
            if not prev and scope[ids[0]]["task"].get("transfer_from"):
                prev=[c["package_id"] for c in b["captures"] if c["attempt_id"]==scope[ids[0]]["task"]["transfer_from"]]
            intent=dict(attempt=aid,day=day,ids=ids,digests={m["message_id"]:digest_message(m) for m in group},parents=prev[-1:])
            intent["key"]="EN-WRNODE-"+object_sha256([tr.key,intent])[:30].upper()
            b["intent"]=intent;persist(folder,b);seal(repo,state,tr,folder,b,intent,rows)
        saved=evidence_index(b)
        if repair:
            for mid,ref in list(saved.items()):
                if mid not in scope or not ref["gaps"] or ref["digest"]!=digest_message(rows[mid]):continue
                row=rows[mid]
                with TemporaryDirectory(prefix="writing-repair-") as td:
                    copy={**row,"metadata":dict(row["metadata"]),"attachment_refs":[]}
                    _,remaining=attachments_for(tr,[copy],Path(td))
                u=scope[mid]
                if remaining or any(not Path(x["path"]).is_file() or file_sha256(Path(x["path"]))!=x["sha256"] for x in u["inputs"]):continue
                intent=dict(attempt=u["attempt_id"],day=study_day(row),ids=[mid],digests={mid:digest_message(row)},parents=[ref["package_id"]])
                if ref.get("version"):intent["versions"]={mid:ref["version"]}
                intent["key"]="EN-WRREPAIR-"+object_sha256([tr.key,intent])[:30].upper()
                b["intent"]=intent;persist(folder,b);seal(repo,state,tr,folder,b,intent,rows)
            saved=evidence_index(b)
        for mid,ref in matched.items():
            if not ref["gaps"]:saved[mid]=ref
        for c in candidates:
            mid=c["message_id"];valid(mid in saved and mid in scope,"待审候选必须绑定已封存消息")
            valid(isinstance(c.get("observation"),str) and c["observation"].strip(),"候选缺少具体观察")
            from .writing_personal import skill_key
            k=skill_key(c["point_key"])
            entry=dict(schema="writing-candidate/1",status="candidate",point_key=k,observation=c["observation"],
                    message_id=mid,original_sha256=saved[mid]["digest"],package_id=saved[mid]["package_id"],
                    package_sha256=saved[mid]["sha256"],role=rows[mid]["role"],attempt_id=scope[mid]["attempt_id"])
            entry["candidate_id"]=object_sha256(entry)
            sidecar=folder/"candidates"/(entry["candidate_id"]+".json")
            if sidecar.is_file():valid(load_json(sidecar)==entry,"同一候选身份内容发生变化")
            else:atomic_write_json(sidecar,entry)
            if not any(x["candidate_id"]==entry["candidate_id"] for x in b["candidates"]):b["candidates"].append(entry)
        gaps=tr.gaps+[mid+":"+g for mid,v in saved.items() if mid in scope for g in v["gaps"]]
        last=next((m for m in reversed(tr.messages) if m["message_id"] in scope),None)
        latest_user=next((m for m in reversed(tr.messages) if m["role"]=="user"),None)
        close_turn=bool(latest_user and CLOSE.fullmatch(latest_user["content"]))
        waiting=bool(tr.live_tail and latest_user and latest_user["message_id"] in scope and not (close_turn and admin_tail and tr.native==os.getenv("CODEX_THREAD_ID")) and last and last["metadata"].get("phase") not in {"final","final_answer"})
        if closing and waiting:gaps.append("本回合的助手末段尚未落盘")
        if closing:verify_originals(repo,state,b,tr,rows)
        persist(folder,b)
        return dict(status="capture_pending" if gaps else "saved",host=tr.host,session_id=tr.session,
             source_messages_total=len(rows),scoped_messages=len(scope),outside_scope=len(rows)-len(scope),
             saved_messages=len(set(saved)&set(scope)),last_message_id=last["message_id"],last_role=last["role"],
             last_assistant_id=next((m["message_id"] for m in reversed(tr.messages) if m["message_id"] in scope and m["role"]=="assistant"),None),
             waiting_for_feedback=waiting,packages=[{"package_id":c["package_id"],"study_date":c["study_date"],"sha256":c["sha256"]} for c in b["captures"] if not only_day or c["study_date"]==only_day],
             candidate_count=len(b["candidates"]),gaps=gaps,formal_write_count=0)
