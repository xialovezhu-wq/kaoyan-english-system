"""Read only public, explicitly supplied ChatGPT share data with a complete underlying branch."""
from html.parser import HTMLParser
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler,Request,build_opener

from .errors import ValidationError
from .writing_sources import external_export,valid,uuid

class Scripts(HTMLParser):
    def __init__(self):
        super().__init__();self.inside=False;self.items=[]
    def handle_starttag(self,tag,attrs):
        d=dict(attrs)
        self.inside=(tag=="script" and (d.get("type")=="application/json" or d.get("id")=="__NEXT_DATA__"))
    def handle_endtag(self,tag):
        if tag=="script":self.inside=False
    def handle_data(self,data):
        if self.inside:
            try:self.items.append(json.loads(data))
            except ValueError:pass

def graph(value,share_id):
    found=[]
    def walk(node):
        if isinstance(node,dict):
            if isinstance(node.get("mapping"),dict) and node.get("current_node") and (node.get("conversation_id") or node.get("id")):
                found.append(node)
            else:
                for v in node.values():walk(v)
        elif isinstance(node,list):
            for v in node:walk(v)
    walk(value)
    valid(len(found)==1,"共享页未给出唯一完整的原始消息链，需要该会话的原始导出")
    obj=dict(found[0]);uuid(obj.get("conversation_id") or obj.get("id"))
    valid(obj.get("share_id",share_id)==share_id and obj.get("shared_conversation_id",share_id)==share_id,"分享身份与原件不符")
    return obj

class ShareRedirects(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        p=urlsplit(newurl)
        if p.scheme!="https" or p.hostname!="chatgpt.com" or p.path.rstrip("/")!=urlsplit(req.full_url).path.rstrip("/"):
            raise ValidationError("共享链接没有公开完整原件，需要该对话的导出")
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def public_share(share_id):
    url="https://chatgpt.com/share/"+uuid(share_id)
    try:
        with build_opener(ShareRedirects).open(Request(url,headers={"Accept":"text/html,application/json"}),timeout=8) as r:
            raw=r.read(32*1024*1024+1)
            valid(len(raw)<=32*1024*1024,"共享页过大，需要原始导出以保留完整内容")
            if r.headers.get_content_type()=="application/json":obj=graph(json.loads(raw),share_id)
            else:
                p=Scripts();p.feed(raw.decode("utf-8"));obj=graph(p.items,share_id)
    except (OSError,ValueError,URLError,RecursionError) as exc:
        raise ValidationError("共享链接未取得完整原文，请提供该会话的原始导出及附件") from exc
    with TemporaryDirectory(prefix="writing-share-") as tmp:
        path=Path(tmp)/"conversation.json";path.write_text(json.dumps(obj,ensure_ascii=False))
        tr=external_export(path,uuid(obj.get("conversation_id") or obj.get("id")))
    tr.origin=url
    return tr
