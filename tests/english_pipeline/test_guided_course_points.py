"""Contract tests use labeled decisions only to exercise integrity. They are not model-performance tests."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from english_pipeline import guided_points as gp, writing_workspace as ws, writing_practice as practice
from english_pipeline.errors import ValidationError
from english_pipeline.reading_writing_tasks import task_contract
from english_pipeline.util import atomic_write_json, file_sha256, load_json, object_sha256, text_sha256
from english_pipeline.writing_contract import lesson
from tests.english_pipeline.test_reading_writing_v2 import exercise


def point(pid):
    return dict(lesson_point_id=pid,point_version="1",rubric_version="rubric-1",goal="State a condition for access.",
                target_kind="specified_form",target_skill_id="writing_skill:language_control",question="Write a condition for entering a workshop.",
                criteria={"relation":{"description":"Entry depends on permission.","required":True,"evidence":"meaning"},
                          "syntax":{"description":"Use the designated conditional construction.","required":True,"evidence":"written_form"}},
                qualified_variants=["Use a main clause first.","Put the restriction first."],counterexamples=["Reverse the direction of permission."])


class PointContractTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.repo=Path(t.name)/"repo";self.repo.mkdir()
        self.root=self.repo/".writing-reader";self.session=str(uuid4());self.key="WRITING-"+object_sha256(self.session)[:24]
        clip=self.repo/"original-fixture.mp4";clip.write_bytes(b"isolated course-media fixture")
        self.points=[point("SYN-rule-A"),point("SYN-rule-B")]
        request=dict(course="Synthetic equipment workshop",section="Access",goal="Read a permission rule.",task_profile="kaoyan_i_argument",
                     source_id="VIDEO-WORKSHOP",lesson_id="SYN-video-lesson",video_material=dict(path=str(clip),sha256=file_sha256(clip)),
                     video={"reference":"Synthetic clip","start_seconds":2,"end_seconds":7},points=self.points)
        self.lesson=lesson(self.repo,request);self.sha=object_sha256(self.lesson)
        atomic_write_json(ws.lesson_file(self.repo,self.sha),self.lesson)

    def start(self,n=0,**overrides):
        s=self.current() if (ws.folder(self.root,self.key)/"current.json").exists() else {}
        req=dict(**self.points[n],lesson_id=self.lesson["lesson_id"],practice_id=str(uuid4()),revision=s.get("revision"),
                 source={"kind":"registered_lesson","lesson_sha256":self.sha})
        return gp.begin(self.repo,self.root,self.key,{**req,**overrides},self.session)

    def current(self):return ws.current(self.root,self.key)
    def post(self,kind,**body):
        s=self.current();p=dict(kind=kind,action_id=str(uuid4()),attempt_id=s["attempt_id"],revision=s["revision"],**body)
        return practice.operate(self.repo,self.root,self.key,p,coach=kind in {"point_decision","exposure"})
    def question(self):return gp.current_question(self.current()["micro"]["guided"])
    def sent(self):
        q=self.question();self.post("question_seen",question_id=q["id"],question_sha256=text_sha256(q["text"]))
    def submit(self,medium="typed"):
        q=self.question();self.post("submit",question_id=q["id"],text="Unless you have permission, you cannot enter.",medium=medium,base_draft=None)
        s=self.current();raw=s["micro"]["guided"]["incoming"]
        pkt=gp.unclassified(s,raw)
        dec=dict(kind="answer",criteria={k:dict(status="met",quote=raw["text"],reason="Fixture criterion is visibly demonstrated.") for k in self.points[0]["criteria"]},
                 feedback="Both conditions are satisfied.",action="advance",reason="Required evidence exists.",strategy="verify_source",next=None)
        posted=dict(action_id=str(uuid4()),attempt_id=s["attempt_id"],revision=s["revision"],kind="point_turn",packet_sha256=pkt["packet_sha256"],decision=dec)
        return posted,raw

    def test_custom_rubric_legacy_and_task_freeze(self):
        legacy=exercise();self.assertEqual(task_contract(legacy)["mode"],"training")
        task=task_contract({**legacy,"success_criteria":{"specific":"Keep the exception."},"counterexamples":["No exception."],
                            "genre_constraint":"To a visitor.","hint_ladder":["Ask about scope."]})
        self.assertEqual(task["success_criteria"],{"specific":"Keep the exception."});self.assertEqual(task["hint_ladder"],["Ask about scope."])
        self.start()
        self.assertEqual(self.current()["micro"]["task"]["success_criteria"]["relation"],self.points[0]["criteria"]["relation"]["description"])
        with self.assertRaises(ValidationError):self.start(goal="Different goal")
        self.assertFalse((self.root/"writing/reading-offers").exists())
        with self.assertRaises(ValidationError):
            practice.start(self.repo,self.root,self.key,{"task":self.current()["micro"]["task"]})

    def test_actual_delivery_stale_receipt_and_fresh_attempt(self):
        self.start()
        with self.assertRaises(ValidationError):self.submit()
        self.sent()
        with self.assertRaises(ValidationError):
            self.post("submit",question_id=str(uuid4()),base_draft=None,text="This belongs to a different question.")
        op,raw=self.submit()
        with self.assertRaises(ValidationError):practice.operate(self.repo,self.root,self.key,{**op,"packet_sha256":"0"*64},coach=True,native={"_turn":raw})
        run=lambda:practice.operate(self.repo,self.root,self.key,op,coach=True,native={"_turn":raw})
        self.assertEqual(run()["id"],run()["id"])
        old=self.current();self.assertEqual(len(old["micro"]["drafts"]),1);self.assertEqual(gp.state_view(old)["status"],"ready_for_next")
        command=[sys.executable,str(Path(__file__).resolve().parents[2]/"scripts/english_writing.py"),"point","next","--repo",str(self.repo),"--session",self.session]
        request=json.dumps({"attempt_id":old["attempt_id"],"revision":old["revision"]})
        new=json.loads(subprocess.check_output(command,input=request,text=True))
        replay=json.loads(subprocess.check_output(command,input=request,text=True))
        self.assertEqual(new["point"]["lesson_point_id"],"SYN-rule-B")
        self.assertEqual(replay["status"],"already_advanced")
        self.assertEqual(new["point"]["attempt_id"],replay["point"]["attempt_id"])
        self.assertEqual(gp.state_view(self.current())["status"],"needs_practice")
        self.assertEqual(self.current()["micro"]["drafts"],[])
        with self.assertRaises(ValidationError):run()
        self.assertFalse((self.repo/"bank/learning_events.jsonl").exists())

    def test_resume_same_point_skip_controls_and_flag(self):
        self.start()
        for k in ["watched","acknowledge","time_elapsed","pause","resume","skip","resume"]:
            self.post("point_control",control=k)
        s=self.current();self.assertEqual(gp.state_view(s)["status"],"needs_practice");self.assertFalse(s["micro"]["drafts"])
        from english_pipeline.guided_point_cli import main
        cli=[sys.executable,str(Path(__file__).resolve().parents[2]/"scripts/english_writing.py"),"point"]
        new_session=str(uuid4());opt=["--repo",str(self.repo),"--session",new_session]
        reopened=json.loads(subprocess.check_output(cli+["resume",*opt,"--workspace",self.key,"--attempt-id",s["attempt_id"]]))
        same=json.loads(subprocess.check_output(cli+["read",*opt]))
        self.assertEqual(reopened,same);self.assertEqual(same["question"]["id"],self.question()["id"])
        self.assertFalse((self.repo/"intake/views/course-playback").exists())
        with patch.dict(os.environ,KAOYAN_ENGLISH_GUIDED_POINTS="off"):
            with self.assertRaises(ValidationError):self.start()
            self.assertEqual(gp.state_view(self.current())["practice_id"],same["practice_id"])
            v2=practice.start(self.repo,self.root,"WRITING-"+"a"*24,{"task":exercise()})
            self.assertEqual(v2["status"],"offered")

    def test_unverified_media_cannot_prove_written_form(self):
        for kind in ("speech_to_text","ocr","copied"):
            self.start();self.sent();op,raw=self.submit(kind)
            with self.assertRaises(ValidationError):practice.operate(self.repo,self.root,self.key,op,coach=True,native={"_turn":raw})
            self.assertFalse(self.current()["micro"]["drafts"])
            op["decision"].update(action="check_variant",strategy="typed_variant",feedback="Please verify original evidence.",
                         next=dict(criteria=["syntax"],question="Type a fresh access rule.",expected="A verified written condition."))
            practice.operate(self.repo,self.root,self.key,op,coach=True,native={"_turn":raw})
            self.assertEqual(self.current()["micro"]["guided"]["status"],"needs_practice")
            self.assertEqual(self.current()["micro"]["drafts"][0]["medium"],kind)
            self.post("point_control",control="pause")

    def test_open_assessment_and_question_scope_cannot_be_relabelled(self):
        spec=exercise(mode="open");practice.start(self.repo,self.root,self.key,{"task":spec})
        with self.assertRaises(ValidationError):self.start()
        public=ws.projection(self.repo,self.root,self.key)
        self.assertNotIn("course_point",json.dumps(public))
        self.assertNotIn("reader_action",json.dumps(public))
        with self.assertRaises((ValidationError,OSError,KeyError)):
            gp.read_scope(self.repo,self.root,{"source_ref":{"source_id":"SYN-missing","unit_id":"S01","question_id":"Q22","source_hash":"0"*64,"sentence_sha256":"1"*64}})
