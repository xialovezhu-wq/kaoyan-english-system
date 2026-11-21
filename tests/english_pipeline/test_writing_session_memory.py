"""End-to-end regression for the installed tutor/representative contracts, with isolated learner data."""
import copy
from contextlib import closing
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from tests.english_pipeline import test_direct_packages as native
from english_pipeline import writing_sessions as sessions
from english_pipeline.writing_sources import read_source, digest_message
from english_pipeline.writing_personal import make_action
from english_pipeline.writing_workflow import fresh_reader,native_handoff
from english_pipeline.writing_author import coverage_skeleton,store_coverage
from english_pipeline.writing_coach import context, HOME,DAYS
from english_pipeline.errors import ValidationError
from english_pipeline.util import atomic_write_json, file_sha256, load_json, object_sha256
from english_pipeline.packages import validate_conversation_package
from english_pipeline.web_review import package_root
from english_pipeline.publication import complete_formal_closeout
from english_pipeline.local_review import author_local_day
from english_pipeline.writer import apply_nightly

class WritingSessionTests(unittest.TestCase):
    def setUp(self):
        self.f=native.DirectConversationPackageTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups);self.addCleanup(self.f.tearDown)
        self.repo,self.state=self.f.repo.resolve(),self.f.state.resolve()
        self.session=str(uuid4());self.home=self.f.root/"codex";self.home.mkdir()
        self.log=self.home/"sessions"/"original.jsonl";self.log.parent.mkdir()
        with closing(sqlite3.connect(self.home/"state_5.sqlite")) as c, c:
            c.execute("CREATE TABLE threads(id text primary key,rollout_path text)");c.execute("INSERT INTO threads VALUES(?,?)",(self.session,str(self.log)))
        self.patch=patch.dict(os.environ,{"CODEX_HOME":str(self.home),"CODEX_THREAD_ID":self.session})
        self.patch.start();self.addCleanup(self.patch.stop)
        self.link="codex://threads/"+self.session
        self.t=datetime(2026,11,20,1,0,tzinfo=timezone.utc)
        self.clock=0
        self.append("session_meta",{"id":self.session})
        self.append("turn_context",{"turn_id":"t1"})
    def append(self,t,p):
        with self.log.open("a") as f:
            f.write(json.dumps({"type":t,"payload":p,"timestamp":(self.t+timedelta(seconds=self.clock)).isoformat()})+"\n")
        self.clock+=1
    def say(self,role,id,text,phase=None,attachment=None):
        blocks=[{"type":"input_text" if role=="user" else "output_text","text":text}]
        if attachment:blocks.append({"type":"input_image","path":str(attachment)})
        p={"type":"message","role":role,"id":id,"content":blocks}
        if phase:p["phase"]=phase
        self.append("response_item",p)
    def source(self):return read_source(self.link)
    def begin(self,task="TEST-LETTER"):
        sessions.register_unit(self.repo,self.state,self.source(),task=task,prompt="Invite a visitor to a reading session.",
                               origin={"kind":"isolated_exam_fixture"},first="begin")
    def checkpoint(self,**kw):
        return sessions.checkpoint(self.repo,self.state,self.source(),**kw)
    def test_last_feedback_link_replay_midnight_and_long_chat(self):
        self.say("user","outside","A separate topic.")
        self.say("user","begin","今天学作文");self.begin()
        image=self.f.root/"learner.png";image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.say("user","draft","Please come to our event.",attachment=image)
        a=self.checkpoint(stage="first_draft",message_id="draft",candidates=[{"message_id":"draft","point_key":"writing_skill:reader_action","observation":"课后核对任务动作"}])
        original=package_root(self.state,self.repo,a["packages"][0]["package_id"]);before=file_sha256(original/"conversation.json")
        self.t=datetime(2026,11,20,16,1,tzinfo=timezone.utc)
        for i in range(43):
            self.say("user",f"q{i}",f"Question {i}");self.say("assistant",f"a{i}",f"Scoped feedback {i}","final_answer")
        self.say("user","revision","Would you let me know by Friday?")
        b=self.checkpoint(stage="revision",message_id="revision")
        self.say("assistant","last","The response deadline is now explicit.","final_answer",attachment=image)
        self.append("event_msg",{"type":"task_complete","turn_id":"t1","last_agent_message":"The response deadline is now explicit."})
        c=self.checkpoint(closing=True,repair=True);d=self.checkpoint(closing=True,repair=True)
        self.assertEqual(c["status"],"saved");self.assertEqual(c["last_assistant_id"],"last");self.assertEqual(c["last_role"],"assistant")
        self.assertEqual(c["packages"],d["packages"]);self.assertGreater(c["saved_messages"],80);self.assertEqual(c["outside_scope"],1)
        self.assertEqual(set(x["study_date"] for x in c["packages"]),{"2026-11-20","2026-11-21"})
        self.assertEqual(before,file_sha256(original/"conversation.json"))
        final=package_root(self.state,self.repo,c["packages"][-1]["package_id"])
        self.assertEqual([m["content"] for m in load_json(final/"conversation.json")["messages"]],["The response deadline is now explicit."])
        self.assertEqual(load_json(final/"source.json")["identity"]["prior_package_ids"],[b["packages"][-1]["package_id"]])
        self.assertEqual(load_json(final/"manifest.json")["attachments"][0]["sha256"],file_sha256(image))
        self.assertFalse((self.repo/"bank/learning_events.jsonl").exists())
    def test_pending_attachment_repair_and_unfinished_tail(self):
        img=self.f.root/"missing.png";self.say("user","begin","今天学作文");self.begin()
        self.append("event_msg",{"type":"task_started","turn_id":"work"});self.say("user","draft","Please review my draft.",attachment=img)
        first=self.checkpoint(closing=True);self.assertEqual(first["status"],"capture_pending")
        self.assertIn("本回合的助手末段尚未落盘",first["gaps"])
        img.write_bytes(b"original picture");self.say("assistant","last","Check the subject.","final_answer")
        r=self.checkpoint(closing=True,repair=True);self.assertEqual(r["status"],"saved")
        self.assertEqual(r["saved_messages"],3);self.assertEqual(len(r["packages"]),3)
        self.assertEqual(self.checkpoint(closing=True,repair=True)["packages"],r["packages"])
        self.say("user","close","今天作文结束并入库");self.say("assistant","closed","Saved.","final_answer")
        self.say("user","other","An unrelated math question")
        end=self.checkpoint(closing=True);self.assertEqual(end["last_message_id"],"closed")
        with self.assertRaises(ValidationError):read_source("https://example.org/thread/"+self.session)

    def test_t3_native_alias_and_export_attachment_repair(self):
        t3=self.f.root/"t3";t3.mkdir()
        (t3/"environment-id").write_text("this-host")
        attachments=t3/"attachments";attachments.mkdir()
        (attachments/"asset1.png").write_bytes(b"fixture")
        thread=str(uuid4())
        with closing(sqlite3.connect(t3/"statev2.sqlite")) as db, db:
            db.executescript("""
                CREATE TABLE orchestration_v2_projection_provider_threads(thread_id,driver,payload_json);
                CREATE TABLE orchestration_v2_projection_threads(thread_id,deleted_at);
                CREATE TABLE orchestration_v2_projection_messages(thread_id,role,created_at,payload_json);
            """)
            db.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES(?,?,?)",
                       (thread,"codex",json.dumps({"nativeThreadRef":{"nativeId":self.session}})))
            db.execute("INSERT INTO orchestration_v2_projection_threads VALUES(?,NULL)",(thread,))
            for i,(role,mid) in enumerate([("user","draft"),("assistant","final")]):
                msg=dict(role=role,id=mid,text="Original "+mid,createdAt=(self.t+timedelta(seconds=i)).isoformat(),
                         attachments=[dict(id="asset1",name="note.png",sizeBytes=7,mimeType="image/png")] if i else [])
                db.execute("INSERT INTO orchestration_v2_projection_messages VALUES(?,?,?,?)",(thread,role,msg["createdAt"],json.dumps(msg)))
        with patch("english_pipeline.writing_sources.T3_DATA",t3):
            tr=read_source("http://localhost:3773/this-host/"+thread)
            self.assertEqual(tr.key,read_source(self.link).key)
            self.assertEqual(tr.key,read_source("t3://threads/"+thread).key)
            sessions.register_unit(self.repo,self.state,tr,task="T3-Q",prompt="Write an invitation.",origin="fixture",first="draft")
            cap=sessions.checkpoint(self.repo,self.state,tr,closing=True)
            self.assertEqual((cap["saved_messages"],cap["last_assistant_id"]),(2,"final"))
            manifest=load_json(package_root(self.state,self.repo,cap["packages"][0]["package_id"])/"manifest.json")
            self.assertEqual(manifest["attachments"][0]["sha256"],file_sha256(attachments/"asset1.png"))
            with self.assertRaises(ValidationError):read_source("http://localhost:3773/wrong-host/"+thread)

        exported=str(uuid4());url="https://chatgpt.com/c/"+exported
        body={"id":exported,"current_node":"last","mapping":{"root":{"parent":None}}}
        for i,(mid,role,parent) in enumerate([("first","user","root"),("last","assistant","first")]):
            body["mapping"][mid]={"parent":parent,"message":dict(id=mid,author={"role":role},create_time=self.t.timestamp()+i,
                content={"content_type":"text","parts":["Original "+mid]},metadata={"attachments":[{"id":"file-example","name":"note.png","size":7}]} if i else {})}
        path=self.f.root/"export.zip"
        with self.assertRaises(ValidationError):read_source(url)
        def export(data):
            with ZipFile(path,"w") as archive:
                archive.writestr("conversations.json",json.dumps([body]))
                archive.writestr("file-example-note.png",data)
            return read_source(url,export=path)
        tr=export(b"short")
        sessions.register_unit(self.repo,self.state,tr,task="WEB-Q",prompt="Write an invitation.",origin="fixture",first="first")
        self.assertEqual(sessions.checkpoint(self.repo,self.state,tr,closing=True)["status"],"capture_pending")
        fixed=sessions.checkpoint(self.repo,self.state,export(b"fixture"),closing=True,repair=True)
        self.assertEqual((fixed["status"],fixed["saved_messages"],fixed["last_assistant_id"]),("saved",2,"last"))
        self.assertEqual(sessions.checkpoint(self.repo,self.state,read_source(url,export=path),closing=True)["packages"],fixed["packages"])
        body["mapping"]["first"]["parent"]="missing"
        partial=export(b"fixture")
        self.assertTrue(partial.gaps)
        self.assertEqual([m["message_id"] for m in partial.messages],["first","last"])

    def test_transfer_requires_real_new_task_and_rejects_copy(self):
        self.say("user","begin","今天学作文");self.begin()
        self.say("user","draft","Please join us at the library on Friday.")
        old=self.checkpoint(stage="first_draft",message_id="draft")
        parent=old["packages"][0]["package_id"]
        tr=self.source()
        other=copy.deepcopy(tr);other.session=str(uuid4())
        other.messages[0]["content"]="换题写校园活动通知"
        other.messages[0]["created_at"]=(self.t+timedelta(minutes=1)).isoformat()
        def register(task,prompt):
            return sessions.register_unit(self.repo,self.state,other,task=task,prompt=prompt,
                                          origin="isolated_exam_fixture",first="begin",transfer_from=parent)
        with self.assertRaises(ValidationError):register("TEST-LETTER","Invite a visitor to a reading session.")
        registered=register("NOTICE","Announce the evening discussion.")
        self.assertNotEqual(registered["attempt_id"],registered["task"]["transfer_from"])
        other.messages[1].update(content="The discussion starts at seven in Room Five.",created_at=(self.t+timedelta(minutes=2)).isoformat())
        other.messages[1]["metadata"]["original_content_blocks"][0]["text"]=other.messages[1]["content"]
        cap=sessions.checkpoint(self.repo,self.state,other,stage="transfer",message_id="draft",closing=True)
        pid=cap["packages"][0]["package_id"]
        action_args=dict(package_id=pid,message_id="draft",point_key="writing_skill:task_coverage",stage="transfer",
                         outcome="correct",hint_dependence="none",finding="Independently stated a time.",next_check="Check the audience.")
        def validate(pid,mid,args):
            from english_pipeline.writing_personal import validate as verify
            root=package_root(self.state,self.repo,pid)
            event=make_action(self.repo,self.state,args)["event"]
            msg=next(m for m in load_json(root/"conversation.json")["messages"] if m["message_id"]==mid)
            return verify(event,[dict(binding={"package_id":pid},value=msg)],{pid:{"path":str(root)}},
                          {pid:load_json(root/"source.json")["identity"]})
        self.assertEqual(validate(pid,"draft",action_args)["stage"],"transfer")
        self.say("assistant","suggest","Please reply to the secretary.","final_answer")
        self.say("user","echo","Please reply to the secretary.")
        self.say("user","ack","Got it!")
        dup=self.checkpoint(closing=True)["packages"][-1]["package_id"]
        with self.assertRaises(ValidationError):validate(dup,"echo",dict(action_args,package_id=dup,message_id="echo",stage="first_draft"))
        with self.assertRaises(ValidationError):validate(dup,"ack",dict(action_args,package_id=dup,message_id="ack",stage="first_draft"))
        with self.assertRaises(ValidationError):
            make_action(self.repo,self.state,dict(action_args,submission={"medium":"ocr","verification":"unverified"}))

    def test_handwritten_draft_binds_same_message_attachment(self):
        from english_pipeline.learning_state import validate_learning_event
        self.say("user","begin","今天学作文");self.begin()
        image=self.f.root/"handwritten.png";image.write_bytes(b"fixture-original")
        self.say("user","paper","",attachment=image)
        cap=self.checkpoint(stage="first_draft",message_id="paper")
        pid=cap["packages"][0]["package_id"];root=package_root(self.state,self.repo,pid)
        args=dict(package_id=pid,message_id="paper",point_key="writing_skill:task_coverage",stage="first_draft",
                  outcome="correct",hint_dependence="none",finding="Fixture task addressed.",next_check="Check the reader.",
                  submission=dict(medium="handwritten",verification="checked_original",attachment_sha256=file_sha256(image)))
        action=make_action(self.repo,self.state,args)
        message=load_json(root/"conversation.json")["messages"][-1]
        rows=[dict(value=message,binding={**action["evidence_refs"][0],"resolved_role":"user"})]
        def check(event):
            return validate_learning_event(event,rows,study_date="2026-11-20",package_documents=[dict(path=str(root),package_id=pid)])
        check(action["event"])
        with self.assertRaises(ValidationError):
            check(make_action(self.repo,self.state,{**args,"submission":{**args["submission"],"attachment_sha256":"0"*64}})["event"])

    def test_changed_message_and_attachment_keep_all_versions(self):
        image=self.f.root/"paper.png";image.write_bytes(b"first attachment")
        self.say("user","begin","今天学作文");self.begin()
        self.say("user","draft","First wording.",attachment=image)
        first=self.source();sealed=sessions.checkpoint(self.repo,self.state,first,stage="first_draft",message_id="draft")
        pid=sealed["packages"][0]["package_id"];root=package_root(self.state,self.repo,pid)
        first_hash=file_sha256(root/"conversation.json")
        updated=copy.deepcopy(first);m=updated.messages[-1]
        m["content"]="Changed wording.";m["metadata"]["original_content_blocks"][0]["text"]=m["content"]
        m["metadata"]["host_updated_at"]=(self.t+timedelta(minutes=2)).isoformat()
        v2=sessions.checkpoint(self.repo,self.state,updated,stage="revision",message_id="draft",closing=True)
        self.assertEqual(len(v2["packages"]),2)
        r2=package_root(self.state,self.repo,v2["packages"][1]["package_id"])
        binding=load_json(r2/"source.json")["identity"]["writing_session"]
        self.assertEqual(binding["message_versions"]["draft"]["previous_package_id"],pid)
        self.assertEqual(binding["message_versions"]["draft"]["number"],2)
        decision=dict(package_id=r2.name,message_id="draft",point_key="writing_skill:reader_action",stage="revision",
                      outcome="correct",hint_dependence="none",finding="A wording change.",next_check="Check next time.")
        v2_action=make_action(self.repo,self.state,decision)["event"]
        self.assertEqual(v2_action["observed_at"],m["metadata"]["host_updated_at"])
        self.assertNotEqual(v2_action["event_key"],make_action(self.repo,self.state,
            dict(decision,package_id=pid,stage="first_draft"))["event"]["event_key"])
        self.assertEqual(sessions.checkpoint(self.repo,self.state,first,closing=True)["packages"],v2["packages"])
        image.write_bytes(b"second attachment")
        v3=sessions.checkpoint(self.repo,self.state,updated,closing=True)
        self.assertEqual(len(v3["packages"]),3)
        newest=package_root(self.state,self.repo,v3["packages"][-1]["package_id"])
        self.assertEqual(load_json(newest/"source.json")["identity"]["writing_session"]["message_versions"]["draft"]["number"],3)
        self.assertEqual(file_sha256(root/"conversation.json"),first_hash)
        self.assertEqual(sessions.checkpoint(self.repo,self.state,updated,closing=True)["packages"],v3["packages"])
        self.assertEqual(len({c["attempt_id"] for c in sessions.read_book(sessions.directory(self.state,updated),updated)["captures"]}),1)
        m["metadata"].pop("host_updated_at")
        m["content"]="Another edit without a timestamp.";m["metadata"]["original_content_blocks"][0]["text"]=m["content"]
        untimed=sessions.checkpoint(self.repo,self.state,updated,closing=True)["packages"][-1]["package_id"]
        with self.assertRaises(ValidationError):
            make_action(self.repo,self.state,{**decision,"package_id":untimed})

    def test_whole_class_first_submitted_by_link(self):
        from argparse import Namespace
        from functools import partial
        from english_pipeline import archive, backlog
        from english_pipeline.writing_workflow import receive, run, parser
        from english_pipeline.writing_feedback import comparison
        from english_pipeline.writing_author import protected_support
        self.say("user","open","今天学作文")
        self.say("user","first","I hope to see you there.")
        args=parser().parse_args(["--repo",str(self.repo),"checkpoint","--link",self.link,"--stage","first_draft"])
        self.assertEqual(run(args)["status"],"after_class_import")
        self.say("assistant","hint","Tell the reader how to reply.","final_answer")
        self.say("user","rev","Please reply by email.")
        self.say("assistant","end","Consider giving a deadline as well.","final_answer")
        tr=self.source()
        folder=sessions.directory(self.state,tr)
        self.assertFalse((folder/"session.json").exists())
        self.assertFalse(list((self.state/"packages").glob("*/*/receipt.json")))
        atomic_write_json(self.state/"memory/enabled.json",{"enabled":True})
        args=Namespace(link=self.link,units=None,export=None,day=None,commit=True,reviewer="fixture-author")
        self.assertEqual(receive(self.repo,self.state,tr,args)["status"],"scope_review_required")
        scope=self.f.root/"scope.json"
        atomic_write_json(scope,{"units":[dict(first="open",last="end",task_id="FIXTURE-INVITATION",
                         prompt="Write a short invitation.",origin={"kind":"isolated_exam_fixture"})]})
        args.units=str(scope)
        initial=receive(self.repo,self.state,tr,args)
        self.assertEqual(initial["capture"]["last_assistant_id"],"end")
        self.assertEqual(initial["capture"]["saved_messages"],5)
        cap=initial["capture"]["packages"][0];pid=cap["package_id"];src=package_root(self.state,self.repo,pid)
        day=initial["days"][0];manifest=Path(day["manifest"])
        review=coverage_skeleton(self.repo,self.state,pid)
        for row in review["messages"]:
            mid=row["message_id"]
            row.update(disposition="feedback" if mid in {"hint","end"} else "observed" if mid in {"rev","first"} else "reference",reason="Reviewed the original.")
        def quote(mid,text):return dict(package_id=pid,message_id=mid,quote=text)
        pair=dict(point_key="writing_skill:reader_action",original=quote("first","I hope to see you there."),
                  feedback=quote("hint","Tell the reader how to reply."),revision=quote("rev","Please reply by email."),
                  result="addressed",reason="The revision names an actual reply channel.")
        with self.assertRaises(ValidationError):
            comparison(self.repo,self.state,src,{**pair,"revision":quote("hint","Tell the reader how to reply.")})
        review["feedback_reviews"]=[comparison(self.repo,self.state,src,pair),comparison(self.repo,self.state,src,
            dict(pair,original=pair["revision"],feedback=quote("end","Consider giving a deadline as well."),
                 revision=None,result="no_revision",reason="There is no subsequent learner draft."))]
        def action(mid,stage,outcome,hints,feedback):
            return make_action(self.repo,self.state,dict(package_id=pid,message_id=mid,point_key=pair["point_key"],
                stage=stage,outcome=outcome,hint_dependence=hints,finding="Review of the request.",next_check="Check the response channel.",feedback=feedback))
        actions=[action("first","first_draft","incorrect","none",[]),action("rev","revision","correct","hint",
                 [dict(package_id=pid,message_id="hint")])]
        atomic_write_json(Path(day["author"]["decisions_dir"])/(pid+".json"),dict(package_id=pid,package_sha256=cap["sha256"],
            batch_manifest_sha256=file_sha256(manifest),actions=actions,unresolved=[],support_record=protected_support(src),writing_review=review))
        vol=self.f.root.resolve()/"archive-drive";vol.mkdir();(vol/"english").mkdir()
        sentinel=vol/"sentinel.json";atomic_write_json(sentinel,dict(volume_name="fixture",volume_uuid="QA",expected_mount_point=str(vol)))
        contract=archive.VolumeContract(archive_root=vol,subject_relative_root=Path("english"),sentinel_relative_path=Path("sentinel.json"),
                  sentinel_sha256=file_sha256(sentinel),volume_name="fixture",volume_uuid="QA",rejected_mount_path=self.f.root/"absent-drive")
        args.units=None
        with patch.object(backlog,"execute_backlog_plan",partial(backlog.execute_backlog_plan,archive_contract=contract)),patch.object(archive,"VolumeContract",return_value=contract):
            closed=receive(self.repo,self.state,self.source(),args)
            self.assertEqual(closed["status"],"completed",[(d["status"],d.get("run",{}).get("errors")) for d in closed.get("days",[])])
            ledger=self.repo/"bank/learning_events.jsonl";digest=file_sha256(ledger)
            again=receive(self.repo,self.state,self.source(),args)
            self.assertEqual((again["memory_readback"]["observations"],again["memory_readback"]["attempts"]),(2,1))
            self.assertEqual(file_sha256(ledger),digest)
        note=(self.repo/DAYS/"2026-11-20.md").read_text()
        for phrase in ("原句　I hope to see you there.","实际改句　Please reply by email.","改到了反馈指出的问题","还没有实际改稿"):
            self.assertIn(phrase,note)
        self.assertEqual(note.count("#### 本次反馈与实际修改"),2)
    def test_writer_recovery_and_independent_new_reader(self):
        self.say("user","begin","今天学作文");self.begin()
        self.say("user","draft","I hope to see you at the event.")
        cap1=self.checkpoint(stage="first_draft",message_id="draft")
        self.say("assistant","hint","Please specify what the reader should do.","final_answer")
        self.say("user","revision","Please email me your decision by Friday.")
        self.say("assistant","final","Your revised request gives a deadline.","final_answer")
        cap=self.checkpoint(stage="revision",message_id="revision",closing=True)
        pid1=cap1["packages"][0]["package_id"];pid2=cap["packages"][-1]["package_id"]
        # One existing English authoring plan with precise package membership.
        ready=native_handoff(self.repo,self.state,cap["packages"],"synthetic-author")
        entry=ready["days"][0];mp=Path(entry["manifest"]);mf=load_json(mp);dest=Path(entry["author"]["decisions_dir"])
        atomic_write_json(self.state/"memory/enabled.json",{"enabled":True})
        pref=self.state/"personal-summary/teaching-preferences.md";pref.parent.mkdir(parents=True,exist_ok=True)
        pref.write_text("# 教学偏好\n\n## 作文\n先解释原因。\n")
        from english_pipeline.sentence_support import _source_identity_from_package
        for k,pid in enumerate((pid1,pid2)):
            root=package_root(self.state,self.repo,pid);mid="revision" if k else "draft";sha=load_json(root/"receipt.json")["package_sha256"]
            action=make_action(self.repo,self.state,dict(package_id=pid,message_id=mid,point_key="writing_skill:reader_action",
                stage="revision" if k else "first_draft",method="outline_actions" if k else "none",outcome="correct" if k else "incorrect",
                hint_dependence="hint" if k else "none",finding="Isolated, reviewed reader action.",next_check="Check the recipient's next step.",
                shared_keys=["word:decision"] if k else [],
                feedback=[{"package_id":pid2,"message_id":"hint"}] if k else []))
            review=coverage_skeleton(self.repo,self.state,pid)
            for item in review["messages"]:
                item.update(disposition="observed" if item["message_id"]==mid else "reference",reason="reviewed this original message")
            from english_pipeline.writing_author import protected_support
            support=protected_support(root)
            actions=[action]
            if k:
                from english_pipeline.writing_feedback import comparison
                pair=comparison(self.repo,self.state,root,dict(point_key="writing_skill:reader_action",
                    original=dict(package_id=pid1,message_id="draft",quote="I hope to see you at the event."),
                    feedback=dict(package_id=pid,message_id="hint",quote="Please specify what the reader should do."),
                    revision=dict(package_id=pid,message_id="revision",quote="Please email me your decision by Friday."),
                    result="addressed",reason="The revised request specifies both the action and the deadline."))
                review["feedback_reviews"]=[pair]
                next(r for r in review["messages"] if r["message_id"]=="hint")["disposition"]="feedback"
                from english_pipeline.constants import MASTER_HEADER
                source=action["event"]["user_response"];stamp=action["event"]["observed_at"]
                row=dict.fromkeys(MASTER_HEADER,"")
                row.update(type="单词",item="decision",source_article="synthetic writer",source_sentence=source,meaning="决定",
                           writing_value="适合",appear_count=1,date=mf["study_date"],last_seen=mf["study_date"])
                actions.append(dict(action_id="QA-DECISION",action_type="master_bank_insert",reason="reviewed attested fixture",
                                    evidence_refs=action["evidence_refs"][:1],row=row))
                msgfile=root/"conversation.json"
                i=next(i for i,m in enumerate(load_json(msgfile)["messages"]) if m["message_id"]==mid)
                shared_episode=dict(episode_id="QA-SHARED-DECISION",study_date=mf["study_date"],observed_at=stamp,
                          first_attempt="用户在修订中使用这个词",correction="见原反馈",later_observation="尚未独立复测",
                          hint_dependence="hint",evidence=[dict(path=str(msgfile.relative_to(self.repo)),sha256=file_sha256(msgfile),
                                                               locator=f"messages/{i}")])
                review["shared_memory"]={"records":[dict(key="word:decision",aliases=[],visibility="answer_free",
                          teaching_hint="回看搭配。",episodes=[shared_episode])],"coverage":[dict(package_id=pid,
                          disposition="covered",keys=["word:decision"])]}
            atomic_write_json(dest/(pid+".json"),dict(package_id=pid,package_sha256=sha,batch_manifest_sha256=file_sha256(mp),
                              actions=actions,unresolved=[],support_record=support,writing_review=review))
        authored=author_local_day(self.state,mp,"synthetic-author",repo=self.repo)
        self.assertEqual(authored["status"],"LOCAL_ACTIONS_READY_FOR_NATIVE_PREFLIGHT")
        ap=Path(authored["actions_path"]);sp=Path(authored["support_path"])
        from english_pipeline.sentence_support import preflight_sentence_support
        dry,_=apply_nightly(self.state,self.repo,manifest_path=mp,actions_path=ap)
        pre,_=preflight_sentence_support(self.state,manifest_path=mp,proposal_path=sp,dry_run_receipt_path=dry,expected_completed_ids={pid1,pid2})
        with patch("english_pipeline.writing_author.automatic_profile",side_effect=OSError("synthetic post-commit outage")):
            receipt_path,receipt=apply_nightly(self.state,self.repo,manifest_path=mp,actions_path=ap,apply=True,
                                                authorization=mf["batch_id"],support_preflight_path=pre)
        self.assertEqual(receipt["status"],"APPLIED")
        ledger=self.repo/"bank/learning_events.jsonl";high=file_sha256(ledger)
        from english_pipeline.writing_author import automatic_profile
        baseline=automatic_profile(self.repo,self.state,receipt)
        if baseline.get("status")!="ready":
            raise AssertionError((baseline,[(x.name,load_json(x).get("last_error")) for x in (self.state/"memory/publications").glob("*.json")]))
        quality=complete_formal_closeout(self.repo,self.state,receipt)["local_quality"]
        self.assertEqual(quality["status"],"VERIFIED_CURRENT",quality)
        fresh=fresh_reader(self.repo,self.state,{pid1,pid2})
        self.assertEqual((fresh["observations"],fresh["attempts"]),(2,1))
        another_env=dict(os.environ,CODEX_THREAD_ID=str(uuid4()))
        command=[sys.executable,str(Path(__file__).resolve().parents[2]/"scripts/english_writing.py"),"memory",
                 "--repo",str(self.repo),"context"]
        result=json.loads(subprocess.check_output(command,env=another_env,text=True))
        self.assertEqual(result["shared_points"][0]["key"],"word:decision")
        # Exercise the ordinary lesson-opening CLI with a new session, isolated real storage and a question-only fixture.
        from english_pipeline.writing_plan import PLAN
        from english_pipeline.util import LOCAL_ZONE
        today=datetime.now(LOCAL_ZONE).date().isoformat()
        atomic_write_json(self.repo/PLAN,dict(version="isolated",lessons={},days=[dict(date=today,writing=dict(mode="practice",year=2025),
                              minor={"year":None})]))
        course=self.repo/"learning/writing-course-2027"
        img=course/"questions/fixture.png";img.parent.mkdir(parents=True,exist_ok=True);img.write_bytes(b"fixture-prompt")
        atomic_write_json(course/"questions.json",[dict(year=2025,exam="I",part="B",image="questions/fixture.png",
                                  source_pdf_page=1,id="QA-EXAM-TASK")])
        new_id=another_env["CODEX_THREAD_ID"]
        log=self.home/"sessions/new.jsonl"
        msg=dict(type="message",role="user",id="hello",content=[dict(type="input_text",text="今天学作文")])
        log.write_text("".join(json.dumps(dict(type=kind,payload=payload,timestamp=self.t.isoformat()))+"\n" for kind,payload in
                              [("session_meta",{"id":new_id}),("response_item",msg)]))
        with closing(sqlite3.connect(self.home/"state_5.sqlite")) as db, db:
            db.execute("INSERT INTO threads VALUES(?,?)",(new_id,str(log)))
        program=("import sys; from pathlib import Path; from scripts import english_writing as entry; "
                 "entry.REPO=Path(sys.argv[1]); sys.argv=['english_writing.py','start-today']; sys.exit(entry.main())")
        lesson=json.loads(subprocess.check_output([sys.executable,"-c",program,str(self.repo)],env=another_env,text=True))
        self.assertEqual(lesson["capture_scope"]["status"],"after_class_import")
        self.assertFalse((self.state/"writing-sessions"/object_sha256("codex:"+new_id)/"session.json").exists())
        self.assertEqual(lesson["lesson_context"]["writing_points"],result["writing_points"])
        self.assertEqual(lesson["lesson_context"]["shared_points"],result["shared_points"])
        self.assertIn("decision",(self.repo/"bank/master_bank.csv").read_text())
        self.assertIn("Isolated, reviewed reader action.",(self.repo/DAYS/"2026-11-20.md").read_text())
        self.assertIn("提示",result["writing_points"][0]["observation"]);self.assertNotIn("by Friday",json.dumps(result))
        same,_=apply_nightly(self.state,self.repo,manifest_path=mp,actions_path=ap,apply=True,authorization=mf["batch_id"])
        self.assertEqual(same,receipt_path);self.assertEqual(file_sha256(ledger),high)
        self.assertEqual(self.checkpoint(closing=True)["packages"],cap["packages"])
        self.assertIn("**1 个**",(self.repo/HOME).read_text())
        self.assertTrue((self.repo/DAYS/"2026-11-20.md").is_file())
        # Use the actual link inbox twice, including the native archive/readback path.
        from english_pipeline import archive,backlog
        from english_pipeline.writing_workflow import receive
        from argparse import Namespace
        from functools import partial
        volume=self.f.root.resolve()/"fixture-volume";volume.mkdir()
        volfile=volume/"sentinel.json"
        atomic_write_json(volfile,dict(volume_name="fixture",volume_uuid="QA",expected_mount_point=str(volume)))
        contract=archive.VolumeContract(archive_root=volume,subject_relative_root=Path("english"),
                 sentinel_relative_path=Path("sentinel.json"),sentinel_sha256=file_sha256(volfile),volume_name="fixture",
                 volume_uuid="QA",rejected_mount_path=self.f.root/"absent-volume")
        (volume/"english").mkdir()
        scope=self.f.root/"accepted-units.json"
        book=sessions.read_book(sessions.directory(self.state,self.source()),self.source())
        atomic_write_json(scope,{"units":[dict(attempt_id=book["units"][0]["attempt_id"],last="final")]})
        args=Namespace(units=str(scope),link=self.link,export=None,day=None,commit=True,reviewer="synthetic-author")
        with patch.object(backlog,"execute_backlog_plan",partial(backlog.execute_backlog_plan,archive_contract=contract)), patch.object(archive,"VolumeContract",return_value=contract):
            received=receive(self.repo,self.state,self.source(),args)
            self.assertEqual(received["status"],"completed",[(d["status"],d.get("package_statuses"),[(x.get("errors"),x.get("workflow_status")) for x in d.get("run",{}).get("days",[])]) for d in received.get("days",[])])
            args.units=None
            replay=receive(self.repo,self.state,self.source(),args)
            self.assertEqual(replay["status"],"completed",replay.get("days",[])[:1])
            self.assertEqual(received["memory_readback"]["attempts"],replay["memory_readback"]["attempts"])
            self.assertEqual(len(replay["capture"]["packages"]),2)
            self.assertEqual(file_sha256(ledger),high)
