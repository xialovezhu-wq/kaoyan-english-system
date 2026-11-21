"""Coach controls for the existing writing reader and its protected native Capture path."""
import argparse
import json
import os
import sys
import subprocess
from pathlib import Path

from . import writing_practice as practice
from .article_workspace import RUNTIME
from .constants import REPO_ROOT
from .errors import PipelineError
from .util import object_sha256


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("start","feedback","assess","exposure","correct_opportunity","report","read"))
    parser.add_argument("--repo",type=Path,default=REPO_ROOT)
    parser.add_argument("--runtime",type=Path,default=RUNTIME)
    parser.add_argument("--session",default=os.environ.get("CODEX_THREAD_ID"))
    args=parser.parse_args(argv)
    try:
        practice.check(args.session,"actual writing session required")
        key="WRITING-"+object_sha256(args.session)[:24]
        repo=args.repo.resolve(); root=args.runtime.resolve()
        practice.check((repo==REPO_ROOT.resolve())==(root==RUNTIME.resolve()),"production and fixture roots must stay separate")
        if args.action=="report":
            result=practice.report(root,key)
            result["formal_transfer"]=practice.confirmed_transfer(repo)
        elif args.action=="read":
            result=practice.workspace.projection(repo,root,key)
        else:
            request=json.load(sys.stdin)
            if args.action=="start":
                result=practice.start(repo,root,key,request)
            else:
                result=practice.operate(repo,root,key,{**request,"kind":args.action},coach=True)
            if repo==REPO_ROOT.resolve() and result.get("status") in {"offered","already_offered","saved"}:
                from .article_service import ensure
                from .article_present import display_home,manifest,register
                rec=ensure(repo,root)
                register(rec,display_home(),manifest(display_home()))
                state=practice.workspace.current(root,key)
                if args.action=="start" and state["attempt_id"]==result.get("attempt_id"):
                    from scripts.english_writing import call_owner,deliver
                    try:
                        result["layout"]=call_owner("enter",{"entity_key":key})
                        ready=manifest(display_home())
                        mirror=subprocess.run(ready["html_argv"],input=json.dumps({"subject":"english","entity_key":key+"-DRAFT"}),
                                              text=True,capture_output=True,timeout=40)
                        result["draft_reference"]=json.loads(mirror.stdout)
                        result["display"]=deliver(root,key,state,True)
                    except (OSError,ValueError,PipelineError,subprocess.SubprocessError) as exc:
                        result["display"]={"status":"pending","reason":str(exc),"saved_revision":state["revision"]}
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except (KeyError,TypeError,OSError,ValueError,PipelineError) as exc:
        print(json.dumps({"status":"unavailable","reason":str(exc)},ensure_ascii=False))
        return 1
