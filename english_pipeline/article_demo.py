"""Original synthetic acceptance material. Never open a production source for this route."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from . import reading_preparation as reading
from .article_workspace import apply, scope
from .current_unit import read_messages
from .util import atomic_write_json, atomic_write_text, exclusive_lock, load_json, text_sha256

FIXTURE = Path(__file__).resolve().parents[1] / "examples/english-article-display.json"
SESSION = "synthetic-article-display"
KEY = "SYN-OUTDOOR-NOTES"
OTHER = "SYN-GARDEN-CLOCK"


def build_material(repo, sentences=None):
    if repo.name != "repo" or repo.parent.name != "synthetic":
        raise ValueError("synthetic_directory_required")
    fixture = load_json(FIXTURE)
    records, sources = [], []
    for n, key in enumerate((KEY, OTHER), 1):
        ref, year = f"EXAM-READING-2098-T{n}", 2098
        article = f"articles/synthetic-{n}.md"
        protected = f"raw/protected/exam-reading-analysis/text-{n}.json"
        passages = [" ".join(sentences or fixture["sentences"])] if n == 1 else ["A clock stood on the shelf."]
        doc = dict(schema="exam_reading_practice_safe_v1", visibility="practice_safe", source_id=key,
                   reference_id=ref, article_path=article, passage_paragraphs=passages,
                   questions=[dict(number=21, prompt="Which detail is mentioned?",
                                   options=dict(A="The chair.",B="The sea.",C="The train.",D="The moon."))],
                   protected_analysis_path=protected)
        atomic_write_text(repo / article, "# " + ref)
        atomic_write_json(repo / f"raw/articles/exam-reading-corpus/{year}/text-{n}.json", doc)
        answer = "A" if n == 1 else "C"
        publisher = text_sha256("synthetic-publisher")
        analysis = dict(reference_id=ref,analysis_id="SYN-ANALYSIS-"+str(n),year=year,source_sha256=publisher,
                        visibility="hidden_until_review", extraction_status="preprocessed_complete",
                        answer_key={"21":answer},questions=[dict(question_number=21,correct_answer=answer,
                        analysis_text="SYN-PRIVATE-EXPLANATION-"+str(n),analysis_source_pages=[1],
                        verification_status="source_verified_synthetic",answer_evidence_page=1)])
        records.append(dict(reference_id=ref,analysis_id=analysis["analysis_id"],status="preprocessed_complete",json_path=protected))
        atomic_write_json(repo / protected, analysis)
    sources.append(dict(year=year,role="answer_analysis_hidden_source",sha256=publisher))
    atomic_write_json(repo / "raw/reference_sources/exam_pdf_manifest.json", dict(sources=sources))
    atomic_write_json(repo / "raw/protected/exam-reading-analysis/index.json", dict(records=records))
    reading.build(repo,repo / reading.DEFAULT_OUTPUT)


def conversation(root, number, extra=()):
    fixture = load_json(FIXTURE)
    turns = [("user","u0","开始学习这篇合成文章，显示整篇原文。")]
    for i,r in enumerate(fixture["rounds"][:number],1):
        turns += [("user","u"+str(i),r["user"]), ("assistant","a"+str(i),r["explanation"])]
    if number and not extra:
        turns.pop()
    turns += list(extra)
    # Read the original message format through the native Capture *reader*.
    events = [dict(type="session_meta",payload=dict(id=SESSION))]
    turn = ""
    for role, mid, value in turns:
        if role == "user":
            turn = "turn-" + mid
            events.append(dict(type="turn_context",payload=dict(turn_id=turn)))
        events.append(dict(type="response_item",payload=dict(type="message",role=role,id=mid,
                      content=[dict(type="input_text" if role=="user" else "output_text",text=value)])))
    path = root / "transcripts" / (text_sha256(json.dumps(events))+".jsonl")
    if not path.is_file():
        atomic_write_text(path, "".join(json.dumps(row,ensure_ascii=False)+"\n" for row in events))
    messages = read_messages(path,SESSION)
    def current(expected=None):
        row = next(m for m in reversed(messages) if m["role"]=="user")
        if expected is not None and expected != row["message_id"]:
            raise ValueError("current_message_mismatch")
        return row
    return SimpleNamespace(messages=messages, current_user=current, session=SESSION, turn_id=turn,
                           root=root, repo=root / "repo")


def round_changes(number):
    r = load_json(FIXTURE)["rounds"][number-1]
    uid, msg = r["unit"], "u"+str(number)
    def user(unit,role,quote):
        return dict(unit=unit, role=role, evidence=dict(message_id=msg,quote=quote))
    changes = []
    if number == 2:
        changes.append(user("S01","transcript_note",r["user"].split("第二句")[0]))
    if number == 3:
        changes.append(user("S02","acknowledgment","第二句懂了。"))
    changes += [user(uid,"first_attempt",r["first"]), dict(unit=uid,role="explanation",text=r["explanation"]),
                dict(unit=uid,role="translation",text=r["translation"])]
    if number == 1:
        changes.append(dict(unit=uid,role="comparison",suggestions=[
            dict(style="简洁",text="Mira kept a notebook by the window to record the birds she saw.",
                 meaning_relation="changed",basis="to record 保留目的；kept 表示放在那里，弱化了 placed 的放置动作。")]))
    return changes


def run_round(root, number):
    root = root / "synthetic"
    repo = root / "repo"
    with exclusive_lock(root / "fixture.lock"):
        if not (repo / reading.DEFAULT_OUTPUT / "index.json").is_file():
            build_material(repo)
        ctx = conversation(root,number)
        state = apply(repo,root,conversation(root,0),{"entity_key":KEY,"operation":"open","operation_id":"demo-open",
                                                   "originals":"article"})
        if number:
            # Replaying the same round uses the original expected revision.
            req = dict(entity_key=KEY, **scope(state), operation="patch", operation_id=f"demo-r{number}",
                       expected_revision=number, active_unit_id=f"S{number:02d}", changes=round_changes(number))
            state = apply(repo,root,ctx,req)
        return {**state, "synthetic":True,"learning_evidence":False}
