#!/usr/bin/env python3
"""Installed English skill entry for an article, its live updates and display receipt."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from english_pipeline.article_present import chat_link, display_home, manifest, present, register
from english_pipeline.article_service import context, ensure, origin, serve, stop_worker
from english_pipeline.article_source import checked_key, read_article
from english_pipeline.article_workspace import RUNTIME, apply, database, head, metadata, project
from english_pipeline.errors import PipelineError, ValidationError
from english_pipeline.util import atomic_write_text, exclusive_lock, load_json


def input_object():
    raw = sys.stdin.buffer.read(500_001)
    if len(raw) > 500_000:
        raise ValidationError("display_input_too_large")
    result = json.loads(raw)
    if not isinstance(result,dict):
        raise ValidationError("display_object_required")
    return result


def run(args):
    root,repo = args.runtime.resolve(),args.repo.resolve()
    action = args.action
    if action == "witness":
        from english_pipeline.expression_receipts import Conversation, live_session
        c = Conversation(live_session(None))
        return {"message_id":c.current_user()["message_id"], "turn_id":c.turn_id}
    if action == "serve":
        serve(repo,root)
        return None
    if action == "stop":
        with exclusive_lock(root / "service.lock"):
            config = root / "service.json"
            return stop_worker(load_json(config),repo,root) if config.is_file() else {"status":"already_stopped"}
    if action in ("disable","enable"):
        flag = root / "external-disabled"
        if action == "disable":
            atomic_write_text(flag,"Text and stable links only.\n")
        else:
            flag.unlink(missing_ok=True)
        return {"external_adapter":action+"d"}
    if action == "link":
        key = checked_key(args.entity)
        rec = ensure(repo,root)
        ready = manifest(display_home())
        register(rec,display_home(),ready)
        return {"entity_key":key, "chat_link":chat_link(root,key,ready)}
    if action == "service":
        rec = ensure(repo,root)
        ready = manifest(display_home())
        path = register(rec,display_home(),ready)
        return {**rec,"origin":origin(rec),"endpoint":str(path),"shared_status":"ready" if ready else "integration_pending"}
    if action == "demo":
        from english_pipeline.article_demo import run_round
        state = run_round(root,args.round)
        op = "demo-present-" + str(args.round)
    elif action == "apply":
        from english_pipeline.expression_receipts import Conversation, live_session
        req = input_object()
        if req.get("entity_key","").startswith("SYN-"):
            raise ValidationError("reserved_synthetic_identity")
        ctx = Conversation(live_session(None))
        ctx.session = live_session(None)
        ctx.repo, ctx.root = repo, root
        state = apply(repo,root,ctx,req)
        op = req["operation_id"]
    elif action == "present":
        state = input_object()
        op = state["operation_id"]
    elif action in ("status","source"):
        key = checked_key(args.entity)
        actual_repo,actual_root = context(repo,root,key)
        if action == "source":
            material = read_article(actual_repo,actual_root,key)
            return {**material["binding"],"source_version":material["source_version"],
                    "units":list(material["sections"])}
        # Private CLI-only manifest identity lets a caller CAS-reset even after drift.
        with database(actual_root,key) as db:
            state = head(db)
        if not state:
            raise ValidationError("article_unavailable")
        current = metadata(state)
        if "catalog_mapping" in state:
            current["catalog_mapping"] = state["catalog_mapping"]
        try:
            project(actual_repo,actual_root,key,meta_only=True)
            current["source_status"] = "current"
        except ValidationError as exc:
            current["source_status"] = str(exc)
        return current
    display = {}
    delivery_error = None
    try:
        rec = ensure(repo,root)
        display = present(rec,root,state,mode=args.mode,operation_id=op,anchor=args.anchor)
    except (OSError,ValueError,PipelineError) as exc:
        delivery_error = exc
    inline = {}
    if getattr(args, "inline_root", None):
        from english_pipeline.article_figures import article_inline
        try:
            inline = article_inline(repo, root, state, args.inline_root, unit=args.anchor,
                                    tab=args.tab, block_id=args.block_id, article_url=display.get("chat_link"))
        except (OSError, ValueError, PipelineError) as exc:
            inline = {"inline_error": str(exc)}
    if delivery_error is not None:
        if action == "present" and not inline:
            raise delivery_error
        return {**state, **inline, "delivery_error":str(delivery_error),"display_commit_retained":True}
    return {**state, **inline, "display":display, "formal_write_count":0,"capture_write_count":0,"model_requests":0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime",type=Path,default=RUNTIME)
    parser.add_argument("--repo",type=Path,default=ROOT)
    sub = parser.add_subparsers(dest="action",required=True)
    for action in ("serve","service","link","witness","enable","disable","stop","source","status","apply","demo","present"):
        command = sub.add_parser(action)
        if action in ("source","status","link"):
            command.add_argument("entity")
        if action in ("apply","demo","present"):
            command.add_argument("--mode",choices=("present","update_only"),default="update_only")
            command.add_argument("--anchor")
        if action in ("apply","present"):
            command.add_argument("--inline-root", type=Path, help="This chat's visualization directory")
            command.add_argument("--tab", help="Initially selected question language tab in chat")
            command.add_argument("--block-id", help="Specific current unit figure for the chat")
        if action == "demo":
            command.add_argument("--round",type=int,choices=range(4),required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = run(args)
        if result is not None:
            print(json.dumps(result,ensure_ascii=False))
        return 2 if result and (result.get("inline_error") or result.get("delivery_error") or result.get("display",{}).get("errors")) else 0
    except (OSError,ValueError,TypeError,KeyError,PipelineError) as exc:
        print(json.dumps({"status":"error","error":str(exc),"formal_write_count":0},ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
