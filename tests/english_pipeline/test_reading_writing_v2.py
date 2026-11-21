"""Synthetic exercises only. All reader revisions, formal evidence and snapshots live in fixtures."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from english_pipeline import writing_practice as practice
from english_pipeline.errors import ValidationError
from english_pipeline.reading_writing_tasks import anchor, judge, metrics, observation, task_contract
from english_pipeline.util import load_json, object_sha256, text_sha256

ROOT=Path(__file__).resolve().parents[2]


def exercise(kind="genre", **values):
    prompt="Write a note to a new student about the event."
    obj=dict(task_id="SYN-short-note",task_version="1",rubric_version="1",task_type=kind,
             task_genre="letter" if kind=="genre" else "sentence",target_skill_id="writing_skill:reader_action",
             target_description="Give the reader a practical next action.",prompt=prompt,mode="training",
             context_id="event",context_basis="A student is deciding how to attend.",opportunity="applicable",
             opportunity_reason="The student has to respond.",task_relation="same_task",observed_scope_complete=True,
             surfaces={k:{"text":prompt if k=="prompt" else "","cue":"none","support":"none"} for k in ("prompt","sidebar","adjacent")})
    obj.update(values)
    return obj


def binding(draft,quote=None):
    return dict(draft_id=draft["id"],draft_sha256=text_sha256(draft["text"]),quote=quote or draft["text"])


def review(task,draft,findings,language=()):
    return dict(reviewer="tutor",rubric_version=task["rubric_version"],target_skill_id=task["target_skill_id"],
                criteria={k:{"status":status,"reason":reason,"anchor":binding(draft,quote)} for k,(quote,status,reason) in findings.items()},
                language_findings=[dict(anchor=binding(draft,q),reason=r,blocks_meaning=b) for q,r,b in language])


class ShortWritingTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        self.root=Path(td.name); self.repo=self.root/"repo"; self.runtime=self.repo/".writing-reader"
        self.key="WRITING-"+object_sha256("fixture")[:24]

    def start(self,spec):
        old=practice.workspace.folder(self.runtime,self.key)/"current.json"
        result=practice.start(self.repo,self.runtime,self.key,dict(task=spec,expected_revision=load_json(old)["revision"] if old.exists() else None))
        self.assertEqual(result["status"],"offered")
        return result["attempt_id"]

    def state(self):
        return practice.workspace.current(self.runtime,self.key)

    def act(self,kind,coach=False,**values):
        s=self.state()
        return practice.operate(self.repo,self.runtime,self.key,dict(action_id=str(uuid4()),attempt_id=s["attempt_id"],
                                revision=s["revision"],kind=kind,**values),coach=coach)

    def submit(self,text):
        old=practice.current_draft(self.state()["micro"])
        self.act("submit",text=text,base_draft=old["id"] if old else None)
        return practice.current_draft(self.state()["micro"])

    def test_semantic_variants_direction_evidence_genre_and_language(self):
        # A tutor reads each synthetic sentence against a source with a staffing condition.
        # Different tokens and clause order implement the same qualified relationship.
        task=task_contract(exercise("relation"))
        cases=[
          ("The lab can stay open if a supervisor is available.","if a supervisor is available","met","Staffing limits the recommendation."),
          ("Keep a supervisor on duty. Only then can the lab open late.","Only then","met","The second sentence depends on the first."),
          ("The lab will open late even if no one can supervise.","even if no one can supervise","not_met","The response explicitly removes the required staffing condition.")]
        for text,span,status,reason in cases:
            draft=dict(id=str(uuid4()),text=text)
            finding=review(task,draft,dict(meaning=(text,status,reason),relation=(span,status,reason)))
            actual=judge(task,draft,finding)
            self.assertEqual(actual["target_result"],status)
            self.assertEqual(actual["criteria"]["relation"]["anchor"]["quote"],span)
        evidence=task_contract(exercise("evidence",source_ref={"source_id":"SYN-source"}))
        d=dict(id="SYN-draft",text="A tutor helped two visitors. This proves all grades improved.")
        r=review(evidence,d,dict(traceable=("two visitors","met","The source counts two visitors."),
            qualified=("all grades improved","not_met","Attendance gives no grades or population estimate."),
            reason=("This proves","not_met","The asserted change is outside the measured outcome.")))
        self.assertEqual(judge(evidence,d,r)["target_result"],"not_met")
        genre=task_contract(exercise());d=dict(id="SYN-note",text="Please emails your choice to the club.")
        r=review(genre,d,dict(purpose=("your choice","met","The requested decision is explicit."),
            audience=("Please","met","A courteous request suits a new member.")),
            [("emails","The imperative takes the base verb.",False)])
        self.assertEqual((judge(genre,d,r)["target_result"],len(r["language_findings"])),("met",1))
        r["language_findings"][0]["blocks_meaning"]=True
        self.assertEqual(judge(genre,d,r)["target_result"],"unknown")

    def test_material_and_letter_accept_different_orders(self):
        evidence=task_contract(exercise("evidence",source_ref={"source_id":"SYN-desk"}))
        # The source reports two assisted visitors. Both responses connect that observation to modest support.
        for text,limited,support in [
            ("The desk helped two visitors, so staffing it may serve a few arrivals.","may","two visitors"),
            ("Some arrivals may benefit from a staffed desk. Two visitors received help.","Some","Two visitors")]:
            d=dict(id="SYN-evidence",text=text)
            r=review(evidence,d,dict(traceable=(support,"met","Two recipients correspond to the reported observation."),
                    qualified=(limited,"met","The conclusion is limited to some arrivals."),
                    reason=(text,"met","Observed use supports a limited benefit.")))
            self.assertEqual(judge(evidence,d,r)["target_result"],"met")
        genre=task_contract(exercise())
        for text,action in [("Please email the desk by noon to secure your seat.","email the desk"),
                            ("There is a place waiting for you. Could you confirm by noon?","confirm by noon")]:
            d=dict(id="SYN-letter",text=text)
            r=review(genre,d,dict(purpose=(action,"met","The member can act on the request."),
                                  audience=(text,"met","The note addresses a peer courteously.")))
            self.assertEqual(judge(genre,d,r)["target_result"],"met")

    def test_cue_scope_unsent_help_and_open_payload(self):
        for cue_text,expected in [("Use although.","form"),("表达让步","function"),("","none")]:
            s=exercise(mode="open",target_skill_id="writing_skill:concession")
            s["surfaces"]["adjacent"]["text"]=cue_text
            self.assertEqual(task_contract(s)["task_cue"],expected)
        s=exercise(mode="open");s["target_description"]="PRIVATE-TARGET"
        s["surfaces"]["sidebar"]["text"]="PRIVATE-SIDEBAR"
        self.start(s)
        public=practice.workspace.projection(self.repo,self.runtime,self.key)
        self.assertNotIn("PRIVATE-",json.dumps(public));self.assertNotIn(s["target_skill_id"],json.dumps(public))
        self.act("help",text="Unsent original.")
        self.assertEqual(self.state()["micro"]["drafts"],[])
        self.assertEqual(self.state()["micro"]["unsubmitted"],"Unsent original.")
        d=self.submit("Please join us.")
        self.assertEqual(d["conditions"]["answer_support"],"hint")
        self.assertEqual(len(self.state()["micro"]["drafts"]),1)

    def test_version_anchor_exposure_decisions_and_reopen(self):
        self.start(exercise())
        text="🙂e\u0301 中文. Reply now.\nReply now."
        d=self.submit(text); spot=binding(d,"Reply now.")
        with self.assertRaises(ValidationError):anchor(d,spot)
        spot["offset"]=text.rindex("Reply now.")
        self.act("feedback",True,anchor=spot,target_skill_id="writing_skill:reader_action",direction="Specify a time.",
                 example="Tell us by Thursday.",rewrite="Please confirm by Friday.")
        s=self.state()
        self.assertNotIn("Thursday",json.dumps(practice.workspace.projection(self.repo,self.runtime,self.key)))
        self.act("example");self.act("rewrite")
        stale=dict(kind="accept",attempt_id=s["attempt_id"],revision=s["revision"],action_id=str(uuid4()),base_draft=d["id"])
        with self.assertRaises(ValidationError):practice.operate(self.repo,self.runtime,self.key,stale)
        req={**stale,"revision":self.state()["revision"]}
        for _ in range(2):practice.operate(self.repo,self.runtime,self.key,req)
        reopened=json.loads(subprocess.check_output([sys.executable,str(ROOT/"scripts/english_writing.py"),"micro","read",
              "--session","fixture","--repo",str(self.repo),"--runtime",str(self.runtime)]))
        m=reopened["micro"];self.assertEqual([d["text"] for d in m["drafts"]],[text,text[:spot["offset"]]+"Please confirm by Friday."])
        self.assertEqual(m["feedback"]["decision"],"accepted")
        self.assertEqual(self.state()["micro"]["drafts"][-1]["conditions"]["production_origin"],"ai_or_mixed")
        self.assertEqual(sum(e["kind"]=="accept" for e in self.state()["micro"]["events"]),1)
        for changed in ("Reply.","Reply. Now.","Now reply after lunch.","Reply now.\n🙂e\u0301 中文."):
            with self.assertRaises(ValidationError):anchor(dict(id="next",text=changed),spot)

    def test_opportunity_registration_unknown_and_correction(self):
        first=self.start(exercise());self.submit("Please send a reply.")
        for n in range(4):
            s=exercise(mode="open",task_id="SYN-"+str(n),context_id="case-"+str(n),context_basis="A new case "+str(n),
                       previous_attempt=first,task_relation="new_context")
            s["prompt"]=s["surfaces"]["prompt"]["text"]="Write to visitor "+str(n)+" about a campus visit."
            self.start(s);d=self.submit("Please confirm a date." if n<2 else "Visits are worthwhile.")
            if n<3:
                finding="met" if n<2 else "not_met"
                r=review(task_contract(s),d,dict(purpose=(d["text"],finding,"Request present." if n<2 else "No requested action."),
                         audience=(d["text"],"met","The tone suits visitors.")))
                self.act("assess",True,draft_id=d["id"],review=r)
        a=practice.report(self.runtime,self.key)["current"]
        self.assertEqual([a[k] for k in ("eligible","met","not_met","result_unknown","observed_success_over_all_eligible")],[4,2,1,1,.5])
        self.act("correct_opportunity",True,opportunity="not_applicable",reason="This task did not require a decision.")
        history=practice.report(self.runtime,self.key)
        self.assertEqual((history["before"]["eligible"],history["current"]["eligible"],history["current"]["not_applicable"]),(4,3,1))
        s=exercise(mode="open",task_id="SYN-pending",task_relation="new_context")
        self.start(s);self.submit("Please send an email.")
        self.assertEqual(practice.report(self.runtime,self.key)["current"]["eligibility_pending"],1)
        self.assertIsNone(metrics([])["observed_success_over_all_eligible"])

    def test_relabelled_context_retyped_example_and_unresolved_target(self):
        initial=self.start(exercise());d=self.submit("There will be a meeting.")
        s=exercise(mode="open",task_id="SYN-renumbered",context_id="new-number",context_basis="Changed nominal number",
                   previous_attempt=initial,task_relation="new_context")
        self.start(s);d=self.submit("There will be a gathering.")
        self.assertEqual(d["conditions"]["task_relation"],"same_task")
        self.act("feedback",True,anchor=binding(d),direction="Specify the next action.",
                 target_skill_id=s["target_skill_id"],example="Please send your name now.",rewrite="Please confirm.")
        changed=self.submit("There will be an extraordinary gathering.")
        r=review(task_contract(s),changed,dict(purpose=("extraordinary","not_met","The adjective adds no next action."),
                                                      audience=("gathering","met","The term is appropriate.")))
        self.act("assess",True,draft_id=changed["id"],review=r)
        self.assertEqual(self.state()["micro"]["reviews"][-1]["target_result"],"not_met")
        self.act("feedback",True,anchor=binding(changed),direction="Tell the reader what to do.",
                 target_skill_id=s["target_skill_id"],example="Please send your name now.")
        self.act("example")
        copied=self.submit("Please send your name now.")
        self.assertEqual((copied["conditions"]["production_origin"],copied["conditions"]["answer_support"]),("ai_or_mixed","example"))
        opened=exercise(mode="open",task_id="SYN-different",previous_attempt=initial,task_relation="new_context",
                        opportunity="not_applicable",context_id="opinion",context_basis="Giving an opinion")
        opened["prompt"]=opened["surfaces"]["prompt"]["text"]="Describe one preference."
        self.start(opened);self.submit("I like the campus.")
        a=practice.report(self.runtime,self.key)["current"]
        self.assertEqual(a["not_applicable"],1);self.assertEqual(a["eligible"],0)

    def test_no_offer_until_reading_done_and_skip(self):
        self.assertEqual(practice.start(self.repo,self.runtime,self.key,{"task":exercise("relation",source_ref={"source_id":"SYN-reading"})})["status"],
                         "reading_in_progress")
        self.start(exercise());self.act("skip")
        self.assertEqual(practice.report(self.runtime,self.key)["current"]["skipped"],1)
        self.assertFalse((self.repo/"bank/learning_events.jsonl").exists())
