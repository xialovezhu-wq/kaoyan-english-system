"""Read one identified authored conversation. No browser scraping or third-party account credentials."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import unquote, urlsplit
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from .current_unit import locate_rollout, read_messages
from .errors import ValidationError
from .util import object_sha256, text_sha256

T3_OWNER = Path(os.environ.get("T3_STUDY_UI_ROOT",str(Path.home()/".local/share/study-ui")))
T3_DATA = Path.home() / ".t3/userdata"
CLOSE = re.compile(r"^\s*(?:今天(?:的)?|本次|这段)?(?:英语)?作文(?:课|学习)?(?:结束|下课|学完)(?:了)?(?:并(?:正式)?入库)?[。！!，,\s]*$")
META = ("# AGENTS.md instructions", "<environment_context>", "<permissions instructions>", "<INSTRUCTIONS>")

def valid(test, message):
    if not test:
        raise ValidationError(message)

def uuid(value):
    try:
        return str(UUID(str(value)))
    except (AttributeError, ValueError):
        raise ValidationError("会话链接缺少可核验的会话 ID") from None

def digest_message(message):
    # Transport line, assistant/final flags and attachment extraction progress are not authored content.
    return object_sha256([message["message_id"],message["role"],message["created_at"],message["content"],
                          message["metadata"].get("original_content_blocks",[])])

@dataclass
class Transcript:
    host: str
    session: str
    messages: list
    origin: str
    gaps: list
    native: str | None = None
    live_tail: bool = False

    @property
    def key(self):
        return self.host + ":" + self.session

    def checked(self):
        ids=set()
        for row in self.messages:
            ident=row["message_id"]
            valid(isinstance(ident,str) and ident and ident not in ids,"来源内有重复或缺失的原消息 ID")
            ids.add(ident)
            try:
                stamp=datetime.fromisoformat(str(row["created_at"]).replace("Z","+00:00"))
                valid(stamp.tzinfo is not None,"原消息缺少时区")
            except ValueError:
                raise ValidationError("原消息日期尚未核实") from None
        valid(self.messages or self.gaps,"尚未取得真实会话正文")
        return self

class TurnTrace:
    def __init__(self):
        self.phase={}
        self.open_turn=None
        self.final_turns=set()
        self.final_text={}
        self.ends={}
        self.partials=[]
    def observe(self,kind,payload,line,turn):
        if kind=="response_item" and payload.get("type")=="message" and payload.get("role")=="assistant":
            phase=payload.get("phase") or payload.get("channel")
            self.phase[line]=phase
            if phase in {"final_answer","final"}:
                self.final_turns.add(turn)
                self.final_text[turn]="".join(b.get("text","") for b in payload.get("content",[]) if isinstance(b,dict))
        if kind=="event_msg":
            t=payload.get("type")
            turn=payload.get("turn_id") or turn
            if t in {"task_started","turn_started"}:self.open_turn=turn
            if t in {"task_complete","turn_complete","turn_aborted"}:
                self.open_turn=None
                if payload.get("last_agent_message"):self.ends[turn]=payload["last_agent_message"]

def codex(session):
    path=locate_rollout(session)
    trace=TurnTrace()
    rows=read_messages(path,session,tool_trace=trace)
    for r in rows:
        r["metadata"]["phase"]=trace.phase.get(r["metadata"]["rollout_line"])
    # A completed-turn witness is a check, not a substitute for the actual assistant message.
    assistant={(r["metadata"]["turn_id"],r["content"]) for r in rows if r["role"]=="assistant"}
    gaps=["已完成回合缺少对应助手原文："+str(t) for t,text in trace.ends.items() if (t,text) not in assistant]
    return Transcript("codex",session,rows,str(path),gaps,session, bool(trace.open_turn and trace.open_turn not in trace.final_turns)).checked()

def t3_binding(token, *, lookup_native=False):
    path=T3_DATA/"statev2.sqlite"
    if not path.is_file():return None
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        cond="json_extract(p.payload_json,'$.nativeThreadRef.nativeId')=?" if lookup_native else "p.thread_id=?"
        rows=db.execute("""select distinct p.thread_id,json_extract(p.payload_json,'$.nativeThreadRef.nativeId')
                  from orchestration_v2_projection_provider_threads p join orchestration_v2_projection_threads t
                  on t.thread_id=p.thread_id where p.driver='codex' and t.deleted_at is null and """+cond,(token,)).fetchall()
    if lookup_native:rows=[r for r in rows if not r[0].startswith("import:")]
    if not rows:return None
    valid(len(rows)==1 and rows[0][1],"T3 会话与本地 Codex 的对应尚未唯一核实")
    return rows[0][0],uuid(rows[0][1])

def t3(thread,native):
    native_path=locate_rollout(native)
    with native_path.open(encoding="utf8") as fp:
        head=json.loads(fp.readline())
    valid(head.get("type")=="session_meta" and head["payload"].get("id")==native,"T3 绑定的原生日志身份失配")
    # The owner's database and attachment conventions are read-only. Preserve all rows including those without runs.
    with contextlib.closing(sqlite3.connect((T3_DATA/"statev2.sqlite").as_uri()+"?mode=ro",uri=True)) as db:
        items=db.execute("""select m.payload_json from orchestration_v2_projection_messages m
            where m.thread_id=? and m.role in ('user','assistant') order by m.created_at,m.rowid""",(thread,)).fetchall()
    rows=[];gaps=[];streaming=False
    for (raw,) in items:
        m=json.loads(raw)
        if m.get("streaming"):
            streaming=True
            continue
        blocks=[{"type":"text","text":m["text"]}]
        originals=[]
        for a in m.get("attachments",[]):
            aid=a.get("id","")
            valid(bool(re.fullmatch(r"[-\w.]+",aid)),"T3 附件身份异常")
            parts=[p for p in (T3_DATA/"attachments").glob(aid+".*") if p.is_file() and not p.is_symlink()]
            blocks.append({"type":"host_attachment","id":aid,"name":a.get("name"),"bytes":a.get("sizeBytes")})
            p=parts[0] if len(parts)==1 and parts[0].stat().st_size==a.get("sizeBytes") else None
            originals.append((len(blocks)-1,p,a.get("mimeType") or "application/octet-stream"))
        rows.append(dict(role=m["role"],content=m["text"],message_id=m["id"],created_at=m["createdAt"],
             metadata={"original_content_blocks":blocks,"host_presentation_context":m.get("context"),"host_updated_at":m.get("updatedAt")},
             original_attachments=originals))
    return Transcript("t3",thread,rows,"t3-authored-v2",gaps,native,streaming).checked()

def external_export(path, session, shared=False):
    """Read the named conversation from an original ChatGPT JSON/ZIP export. Inactive branches stay in its original."""
    try:
        archive=ZipFile(path) if path.suffix.lower()==".zip" else None
    except BadZipFile as exc:
        raise ValidationError("导出压缩包无法完整读取，请补原始导出") from exc
    try:
        if archive:
            paths=[n for n in archive.namelist() if re.fullmatch(r"(?:[^/]+/)?conversations(?:-\d+)?\.json",n)]
            valid(paths,"导出包中未找到原始 conversations JSON")
            docs=[]
            for name in paths:
                raw=json.loads(archive.read(name))
                docs.extend(raw if isinstance(raw,list) else [raw])
        else:
            raw=json.loads(path.read_bytes());docs=raw if isinstance(raw,list) else [raw]
        def matches(d):
            allowed=[d.get("shared_conversation_id"),d.get("share_id")] if shared else [d.get("id"),d.get("conversation_id")]
            return session in allowed
        chosen=[d for d in docs if isinstance(d,dict) and matches(d)]
        valid(len(chosen)==1,"导出未唯一绑定所交链接，请提供该对话的原始导出或本机链接")
        d=chosen[0];mapping=d.get("mapping",{});node=d.get("current_node");chain=[];seen=set()
        valid(node and node in mapping,"导出没有可验证的当前分支")
        gaps=[]
        while node:
            if node not in mapping or node in seen:
                gaps.append("当前分支的前序原消息缺失或循环，请补全导出")
                break
            seen.add(node);entry=mapping[node]
            if entry.get("message"):chain.append((node,entry["message"]))
            node=entry.get("parent")
        rows=[];visible_originals={}
        members=archive.namelist() if archive else []
        for node_id,original in reversed(chain):
            m=original if original.get("id") else {**original,"id":"export-position-"+object_sha256([session,node_id])}
            role=(m.get("author") or {}).get("role");meta=m.get("metadata") or {}
            if role not in {"user","assistant"} or m.get("recipient") not in (None,"all") or meta.get("is_visually_hidden_from_conversation") or m.get("channel") in {"analysis","reasoning"}:
                continue
            visible_originals[m["id"]]=original
            content=m.get("content") or {};parts=content.get("parts",[])
            if content.get("content_type") not in {"text","multimodal_text"}:
                gaps.append(m["id"]+":未经适配的可见内容类型");continue
            ts=m.get("create_time")
            if not isinstance(ts,(float,int)):
                gaps.append(m["id"]+":缺原始时间");continue
            txt="".join(p for p in parts if isinstance(p,str));blocks=[]; originals=[]
            for piece in parts:
                if isinstance(piece,str):blocks.append({"type":"text","text":piece})
                else:blocks.append({"type":"export_part","original":piece})
            assets=list(meta.get("attachments") or [])
            known=[str(a.get("id","")) for a in assets]
            for piece in (p for p in parts if isinstance(p,dict)):
                aid=str(piece.get("asset_pointer","")).rsplit("/",1)[-1]
                if aid and aid not in known:assets.append({"id":aid,"name":aid})
                elif not aid:gaps.append(m["id"]+":缺多媒体原件定位")
            for a in assets:
                aid=str(a.get("id",""));clean=bool(re.fullmatch(r"[A-Za-z0-9_-]{4,160}",aid))
                candidates=[n for n in members if clean and not n.endswith("/") and
                    (Path(n).name==aid or Path(n).name.startswith((aid+".",aid+"-","file-"+aid+"-")))]
                blocks.append({"type":"host_attachment","id":aid,"name":a.get("name"),"bytes":a.get("size")})
                data=archive.read(candidates[0]) if len(candidates)==1 else None
                if data is None and clean and not archive:
                    base=path.resolve().parent
                    names=[s for s in base.glob(aid+"*") if s.is_file() and not s.is_symlink() and s.parent==base
                           and (s.name==aid or s.name.startswith((aid+"-",aid+".")))]
                    if len(names)==1:data=names[0].read_bytes()
                if data is not None and isinstance(a.get("size"),int) and len(data)!=a["size"]:
                    data=None
                originals.append((len(blocks)-1,data,a.get("mime_type") or "application/octet-stream"))
            updated=m.get("update_time")
            metadata={"original_content_blocks":blocks}
            if not original.get("id"):
                metadata.update(identity_basis="export_conversation_and_mapping_node",export_node=node_id,native_message_id=None)
            if isinstance(updated,(float,int)) and updated>=ts:
                metadata["host_updated_at"]=datetime.fromtimestamp(updated,timezone.utc).isoformat()
            rows.append(dict(role=role,content=txt,message_id=m["id"],
                 created_at=datetime.fromtimestamp(ts,timezone.utc).isoformat(),
                 metadata=metadata,original_attachments=originals))
        result=Transcript("chatgpt",uuid(d.get("conversation_id") or d.get("id")),rows,
                          str(path.resolve()),gaps).checked()
        result.export_nodes=visible_originals
        result.export_version=object_sha256(d)
        return result
    except BadZipFile as exc:
        raise ValidationError("导出压缩包成员损坏，请补该会话的完整原件") from exc
    finally:
        if archive:archive.close()

def read_source(link, *, export=None):
    raw=str(link or os.environ.get("CODEX_THREAD_ID") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F-]{36}",raw):
        session=uuid(raw);binding=t3_binding(session,lookup_native=True)
        return t3(*binding) if binding else codex(session)
    p=urlsplit(raw)
    valid(not p.username and not p.password,"无效的会话链接")
    parts=[unquote(x) for x in p.path.split("/") if x]
    if p.scheme=="codex" and p.netloc in {"threads","thread","chat"} and len(parts)==1:return read_source(uuid(parts[0]))
    if p.scheme in {"https","http"} and p.hostname in {"chatgpt.com","chat.openai.com"}:
        valid(len(parts)>=2 and parts[-2] in {"c","share"},"需要完整的对话或共享链接")
        session=uuid(parts[-1])
        if export is None and parts[-2]=="share":
            from .writing_share import public_share
            return public_share(session)
        valid(export is not None,"此 ChatGPT 私有链接尚未取得完整原件，请提供可公开读取的分享链接或原始对话导出（含附件）")
        return external_export(Path(export),session,parts[-2]=="share")
    if ((p.scheme in {"t3","t3code","t3code-beta"} and p.netloc in {"thread","threads","chat"}) or p.scheme in {"http","https"} and p.hostname in {"localhost","127.0.0.1","::1"}) and parts:
        # T3 web routes bind /environmentId/threadId. Do not interpret arbitrary loopback links as threads.
        env=T3_DATA/"environment-id"
        if p.scheme in {"http","https"}:
            valid(len(parts)==2 and env.is_file() and parts[0]==env.read_text().strip(),"T3 环境标识与本机不符")
        token=parts[-1];valid(len(token)<=180,"无效的 T3 会话标识")
        binding=t3_binding(token)
        valid(binding,"没有找到该 T3 会话的原生绑定")
        return read_source(binding[1]) if token.startswith("import:") else t3(*binding)
    raise ValidationError("未核实的会话链接，请提供本机 Codex/T3 会话链接或可核对的原始导出")
