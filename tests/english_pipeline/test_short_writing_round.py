"""Replay a complete short-writing lesson via the regular reader and after-class CLI dispatch."""
import contextlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest import TestCase, mock

from english_pipeline import article_workspace, writing_practice, writing_workflow
from english_pipeline.article_source import read_article
from english_pipeline.util import atomic_write_json,file_sha256,load_json,object_sha256
from english_pipeline.web_review import package_root
from english_pipeline.writing_author import protected_support
from english_pipeline.writing_publication import freeze_projection
from tests.english_pipeline import test_writing_session_memory as fixtures
from tests.english_pipeline.test_reading_writing_v2 import exercise, binding, review, ROOT
import subprocess
import sys


class ReadingWritingRound(TestCase):
    def test_completed_reading_to_fresh_writer_and_projection(self):
        f=fixtures.WritingSessionTests();f.setUp()
        self.addCleanup(f.doCleanups)
        repo,state=f.repo,f.state
        runtime=repo/".writing-reader"
        f.t=datetime.now(timezone.utc)-timedelta(seconds=50)
        source="Two volunteers staffed the welcome desk."
        f.say("user","material",source)
        article_ctx=SimpleNamespace(session=f.session, turn_id="turn-one",messages=f.source().messages,
                                   current_user=lambda:f.source().messages[-1])
        a=article_workspace.apply(repo,runtime,article_ctx,dict(operation="open",operation_id="open-reading",
                original_message=dict(message_id="material",quote=source),originals="article"))
        article=read_article(repo,runtime,a["entity_key"])
        date=f.t.date().isoformat()
        receipt=state/"receipts/completion"/date/"SYN-reading-done.json"
        atomic_write_json(receipt,dict(schema_version="english_article_completion_receipt_v2",completion_id="SYN-reading-done",
                                       source_id=a["entity_key"],package_ids=["SYN-isolated-reading"]))
        ref=dict(source_id=a["entity_key"],source_hash=article["binding"]["source_hash"],unit_id="S01",
                 sentence_sha256=article["sections"]["S01"]["text_sha256"],question_id=None,completion_id=receipt.stem,
                 completion_day=date,completion_sha256=file_sha256(receipt))
        task=exercise(source_ref=ref)
        key="WRITING-"+object_sha256(f.session)[:24]
        def micro(op,body=None):
            cmd=[sys.executable,str(ROOT/"scripts/english_writing.py"),"micro",op,"--session",f.session,
                 "--repo",str(repo),"--runtime",str(runtime)]
            return json.loads(subprocess.check_output(cmd,input=json.dumps(body),text=True))
        offered=micro("start",dict(task=task,reading_completed=True))
        self.assertEqual(offered["status"],"offered")
        self.assertEqual(micro("read")["micro"]["source"]["text"],source)
        from english_pipeline.errors import ValidationError
        from english_pipeline.reading_writing_tasks import source_return
        for invalid in (dict(question_id="Q21"),dict(sentence_sha256="0"*64)):
            with self.assertRaises(ValidationError):source_return(repo,runtime,{**ref,**invalid})
        def post(kind,**values):
            s=micro("read")
            body=dict(attempt_id=s["attempt_id"],revision=s["revision"],kind=kind,action_id=str(uuid4()),**values)
            if kind in {"feedback","assess"}:return micro(kind,body)
            return writing_practice.operate(repo,runtime,key,body)
        one=post("submit",text="The event will be interesting.",base_draft=None)["id"]
        draft=micro("read")["micro"]["drafts"][0]
        post("assess",draft_id=one,review=review(task,draft,{"purpose":(draft["text"],"not_met","No action for the reader."),
            "audience":("interesting","met","Appropriate tone for a new student.")}))
        feedback=post("feedback",anchor=binding(draft),direction="Say how the student can register.",
                  target_skill_id=task["target_skill_id"],example="Register at the desk.",rewrite="Visit the desk to register.")["id"]
        two=post("submit",text="Please email the desk to register.",base_draft=one)["id"]
        revision=micro("read")["micro"]["drafts"][-1]
        post("assess",draft_id=two,review=review(task,revision,{"purpose":("email the desk","met","A concrete registration action."),
              "audience":("Please","met","Polite request to a student.")}))
        f.t=datetime.now(timezone.utc)+timedelta(seconds=2)
        f.say("user","done","今天作文结束并入库")
        f.say("assistant","last","下课。","final_answer")
        def receive(*args):
            return writing_workflow.run(writing_workflow.parser().parse_args(["--repo",str(repo),"receive","--link",f.link,*args]))
        pre=receive("--capture-only");self.assertEqual(pre["status"],"scope_review_required")
        pid_for=lambda i:"WRUI-"+i
        ids=[pid_for(one),pid_for(feedback),pid_for(two)]
        units=f.f.root/"units.json"
        atomic_write_json(units,{"units":[dict(first=pid_for(offered["attempt_id"]),last="last",display_attempt_id=offered["attempt_id"],
                         stages={ids[0]:"first_draft",ids[1]:"feedback",ids[2]:"revision"})]})
        cap=receive("--units",str(units),"--capture-only")["capture"]["packages"]
        self.assertFalse((repo/"bank/learning_events.jsonl").exists())
        self.assertEqual(cap,receive("--capture-only")["capture"]["packages"])
        ready=receive("--commit");entry=ready["days"][0];manifest=Path(entry["manifest"])
        dest=Path(entry["author"]["decisions_dir"])
        locations={}
        for c in cap:
            root=package_root(state,repo,c["package_id"])
            for m in load_json(root/"conversation.json")["messages"]:locations[m["message_id"]]=(c["package_id"],m)
        from english_pipeline.writing_author import coverage_skeleton
        from english_pipeline.writing_feedback import comparison
        from english_pipeline.writing_personal import make_action
        def loc(mid):
            p,m=locations[mid];return dict(package_id=p,message_id=mid,quote=m["content"])
        for c in cap:
            pid=c["package_id"];root=package_root(state,repo,pid);coverage=coverage_skeleton(repo,state,pid);actions=[]
            for m in coverage["messages"]:
                mid=m["message_id"]
                m.update(disposition="observed" if mid in (ids[0],ids[2]) else "feedback" if mid==ids[1] else "reference",
                         reason="Reviewed this original learner or tutor turn.")
                if mid==ids[1]:
                    coverage["feedback_reviews"]=[comparison(repo,state,root,dict(original=loc(ids[0]),feedback=loc(ids[1]),
                          revision=loc(ids[2]),point_key=task["target_skill_id"],result="addressed",reason="The revision supplies a registration action."))]
                if mid in (ids[0],ids[2]):
                    changed=mid==ids[2]
                    actions.append(make_action(repo,state,dict(package_id=pid,message_id=mid,point_key=task["target_skill_id"],
                        stage="revision" if changed else "first_draft",outcome="correct" if changed else "incorrect",
                        hint_dependence="hint" if changed else "none",method="minimal_edit" if changed else "none",
                        finding="Reviewed reader action.",next_check="Check the next independent task.",
                        feedback=[{k:v for k,v in loc(ids[1]).items() if k!="quote"}] if changed else [])))
            atomic_write_json(dest/(pid+".json"),dict(package_id=pid,package_sha256=c["sha256"],batch_manifest_sha256=file_sha256(manifest),
                                 actions=actions,unresolved=[],support_record=protected_support(root),writing_review=coverage))
        atomic_write_json(state/"memory/enabled.json",{"enabled":True})
        from functools import partial
        from english_pipeline import backlog
        from tests.english_pipeline.test_backlog_gate import BacklogGateTests
        contract=BacklogGateTests._volume_contract(f.f)
        # Replace only the destination volume, retaining the normal author, writer, archive and reader.
        with mock.patch("english_pipeline.archive.VolumeContract",return_value=contract), mock.patch.object(
                backlog,"execute_backlog_plan",partial(backlog.execute_backlog_plan,archive_contract=contract)):
            closed=receive("--commit")
            self.assertEqual(closed["status"],"completed",[(r["status"],r.get("run",{}).get("status")) for r in closed["days"]])
            self.assertEqual(closed["memory_readback"]["attempts"],1)
        ctx=json.loads(subprocess.check_output([sys.executable,str(ROOT/"scripts/english_writing.py"),"memory","--repo",str(repo),
                 "context","--key",task["target_skill_id"]]))
        w=ctx["writing_points"][0]["writing_task"]
        self.assertEqual([w[k] for k in ("task_cue","answer_support","production_origin","target_result")],
                         ["function","hint","human_revision","met"])
        idx,members=freeze_projection(state)
        exported=load_json(members["context/personal-summary/writing.json"])
        self.assertEqual(exported["records"][0]["episodes"][0]["writing_task"],w)
        self.assertNotIn(revision["text"],json.dumps(exported))
        self.assertNotIn(str(repo),json.dumps(exported))
        self.assertEqual(idx["coverage"]["included_points"],1)
