"""Recover only this saved unit's offered display blocks at their original revisions."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .article_source import KEY, offered_article
from .article_workspace import database, path_for, public_blocks
from .errors import ValidationError
from .util import bytes_sha256, file_sha256


def article_attachments(root, source, selected, session, temporary):
    """Called only inside native authorized Capture. Never snapshot the latest URL."""
    key = source.get("source_id","")
    if not isinstance(key,str) or not KEY.fullmatch(key) or key.startswith("SYN-") or not path_for(root,key).exists():
        return [], []
    by_turn = {}
    for message in selected:
        if message["role"] == "assistant" and offered_article(message["content"],root,key):
            by_turn[message["metadata"]["turn_id"]] = message
    uid = source["unit_id"]
    attached, missing = [], []
    with database(root,key) as db:
        for turn, message in by_turn.items():
            for revision, raw in db.execute(
                    "SELECT revision,manifest FROM revisions WHERE session=? AND turn=? ORDER BY revision",(session,turn)):
                state = json.loads(raw)
                binding = state["binding"]
                if (source.get("source_hash") != binding["source_hash"] or
                        (source.get("record_sha256") and source["record_sha256"] != binding["record_sha256"])):
                    missing.append("attachments.article_projection.source_binding_changed")
                    continue
                for unit, body in db.execute("SELECT unit,body FROM sections WHERE revision=? ORDER BY unit",(revision,)):
                    section = json.loads(body)
                    original = section.get("bound_original",{})
                    tab = uid.split("-",1)[1] if uid.startswith("Q") and "-" in uid else None
                    if not (uid == unit or uid == original.get("paragraph_id") or
                            (tab and uid.split("-",1)[0] == unit) or (uid == source.get("reference_id"))):
                        continue
                    blocks = public_blocks(section,state["binding"],unit)
                    parts = []
                    for b in blocks:
                        stored = next(v for v in section["blocks"] if v["id"] == b["id"])
                        if stored.get("turn") != turn or (tab and b.get("tab") != tab):
                            continue
                        if b["role"] in {"answer","review_figure"} and source.get("answer_exposure") != "protected":
                            raise ValidationError("saved_question_feedback_requires_protected_exposure")
                        parts.append("<h3>"+html.escape(b["label"])+"</h3>")
                        if "text" in b:
                            parts.append("<p>"+html.escape(b["text"])+"</p>")
                        for suggestion in b.get("suggestions",[]):
                            parts.append("<p>"+html.escape(" · ".join(suggestion.values()))+"</p>")
                        if "figure" in b:
                            figure = root / "figures" / (b["figure"]+".html")
                            if not figure.exists() or file_sha256(figure) != b["figure"]:
                                missing.append("attachments.article_figure.original_unavailable")
                            else:
                                attached.append((message,figure))
                    if parts:
                        heading = html.escape(unit + " · v" + str(revision))
                        original_text = original.get("text") or original.get("tabs",{}).get(tab or "STEM","")
                        data = ("<!doctype html><meta charset=utf-8><h2>" + heading + "</h2><p>"
                                +html.escape(original_text)+"</p>"+"".join(parts)).encode()
                        file = temporary / (bytes_sha256(data)+".html")
                        file.write_bytes(data)
                        attached.append((message,file))
    return attached, missing
