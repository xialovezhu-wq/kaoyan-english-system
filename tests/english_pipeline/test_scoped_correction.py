"""Run both correction paths against synthetic records and the real teaching CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from scoped_correction_fixture import ScopedCorrectionFixture

from english_pipeline.archive import archive_committed_batch, resolve_archived_package_from_pointer
from english_pipeline.constants import MASTER_HEADER
from english_pipeline.errors import IdempotencyConflict, ValidationError
from english_pipeline.formal import read_csv, serialize_csv
from english_pipeline.learning_state import load_learning_events
from english_pipeline.memory_lifecycle import read_lifecycle
from english_pipeline.memory_store import source_dependents
from english_pipeline.personal_summary import SCHEMA, publish_personal_summaries
from english_pipeline.reading_preparation import DEFAULT_OUTPUT, build as prepare_reading
from english_pipeline.sentence_support import refresh_sentence_support
from english_pipeline.sources import resolve_article_source
from english_pipeline.util import atomic_write_json, file_sha256, load_json, object_sha256
from english_pipeline.writer import apply_nightly, recover_nightly


CLI = Path(__file__).resolve().parents[2] / "scripts/english_learning_pipeline.py"
DAY = "2026-09-01"
EXTERNAL_EXAMPLE = "The merchants dispute the additional fee."
PROTECTED_E = "E_FIXTURE_PROTECTED: Correct answer is B."
KEYS = {"A": "word:dispute", "B": "word:retain", "C": "word:steady",
        "D": "syntax:predicate interpretation", "E": "word:dispute (verb)",
        "A-question": "option_type:fixture question"}
SOURCES = {"A": "RAW-R11", "B": "FIXTURE-B", "C": "FIXTURE-C", "D": "RAW-R11",
           "E": "FIXTURE-E", "A-question": "RAW-R11"}
BANK_IDS = {"A": "20260901-001", "B": "20260901-002", "C": "20260901-003", "E": "20260901-004"}
BANK_FILES = ("master_bank.csv", "mastered_items.csv", "sentence_patterns.md",
              "learning_events.jsonl", "review_exclusion_ledger.jsonl")


def emit_receipt(operation, **result):
    if os.environ.get("ENGLISH_SCOPED_CORRECTION_QA") == "1":
        print("QA_SCOPED_CORRECTION " + json.dumps({"operation": operation, **result}, sort_keys=True))


class ScopedCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ScopedCorrectionFixture(self)
        self.repo, self.state = self.fixture.repo, self.fixture.state
        self.bank = self.repo / "bank/master_bank.csv"
        self.events = self.bank.with_name("learning_events.jsonl")
        self.witnesses = {}
        model = patch("english_pipeline.formal_model_review._request", side_effect=AssertionError("model transport"))
        self.addCleanup(model.stop)
        model.start()

    def cli(self, command, *args):
        run = subprocess.run(
            [sys.executable, "-B", str(CLI), command, "--repo-root", str(self.repo), "--state-dir", str(self.state),
             *map(str, args)], cwd=self.repo, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def teaching(self, label, *, source=None, unlock=False):
        identity = ("--id", BANK_IDS[label]) if label in {"B", "C", "E"} else ("--key", KEYS[label])
        args = ["--backend", "native", "--view", "teaching", *identity, "--source-id", source or SOURCES[label]]
        args += ["--unit-id", "S01"] if label not in {"E", "A-question"} else ["--unit-id", "Q21", "--question-id", "Q21"]
        if unlock:
            args += ["--unlocked-question-id", "Q21"]
        result = self.cli("query-memory-context", *args)
        self.assertEqual(result["backend"]["name"], "native")
        self.assertEqual(result["backend"]["api_requests"], 0)
        self.assertEqual(result["formal_write_count"], 0)
        return result

    def source(self, label, *messages):
        path = self.repo / f"articles/{label}.json"
        scope = {"question_id": "Q21", "unit_id": "Q21"} if label in {"E", "A-question"} else {"unit_id": "S01"}
        atomic_write_json(path, {"source_id": SOURCES[label], **scope, "messages": list(messages)})
        self.witnesses[label] = path
        return {"path": path.relative_to(self.repo).as_posix(), "sha256": file_sha256(path), "locator": "/messages/0"}

    def episode(self, label, identity, first_attempt, evidence, **extra):
        return {"episode_id": identity, "study_date": DAY, "source_id": SOURCES[label],
                "unit_id": "S01", "first_attempt": first_attempt, "correction": "按本句核对。",
                "later_observation": "未复测。", "hint_dependence": "not_observed", "evidence": [evidence], **extra}

    def routes(self):
        return {route["key"]: route["sha256"] for route in
                load_json(self.state / "personal-summary/current.json")["routes"].values()}

    def bank_hashes(self):
        return {name: file_sha256(path) if (path := self.bank.with_name(name)).is_file() else None
                for name in BANK_FILES}

    def rows(self):
        rows = read_csv(self.bank, MASTER_HEADER)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(len(rows), len(by_id))
        return by_id

    def source_review(self, document):
        refs = {name: {"path": path.relative_to(self.repo).as_posix(), "sha256": file_sha256(path),
                       "kind": "learner_dialogue", "locators": ["/messages/0"], "study_dates": [DAY]}
                for name, path in self.witnesses.items()}
        refs["A"]["locators"] += ["/messages/1", "/messages/2"]
        refs["B"]["locators"] += ["/messages/1"]
        parent = self.records["D"]["episodes"][0]["evidence"][1]
        return {"schema_version": "english_summary_source_review_v1", "purpose": "explicit_summary_maintenance",
                "summary_input_sha256": object_sha256(document),
                "sources": [*refs.values(), {**parent, "kind": "assistant_explanation",
                                             "locators": [parent["locator"]], "study_dates": [DAY]}]}

    def exam_source(self, label, year, sentence):
        reference = f"EXAM-READING-{year}-T1"
        article = f"articles/{year}-english-i-text-1.md"
        canonical = f"raw/articles/exam-reading-corpus/{year}/text-1.json"
        protected = f"raw/protected/fixture-{year}.json"
        (self.repo / article).write_text(f"# Synthetic fixture\n\n{reference}\n", encoding="utf-8")
        atomic_write_json(self.repo / protected, {"fixture": "protected"})
        atomic_write_json(self.repo / canonical, {
            "schema": "exam_reading_practice_safe_v1", "visibility": "practice_safe",
            "reference_id": reference, "source_id": SOURCES[label], "article_path": article,
            "passage_paragraphs": [sentence], "protected_analysis_path": protected,
            "questions": [{"number": 21, "prompt": "What did the writer say?",
                           "options": dict(zip("ABCD", ("First.", "Second.", "Third.", "Fourth.")))}],
        })
        self.official_paths.extend(self.repo / path for path in (article, canonical, protected))
        return resolve_article_source(self.repo, article)

    def seed(self):
        def user(text):
            return {"role": "user", "content": text}

        self.a_original = "我把 dispute 译成了争执。"
        a_ref = self.source("A", user(self.a_original), user("旧的归因应撤回，只保留原来的首译。"),
                            {"role": "assistant", "content": EXTERNAL_EXAMPLE})
        b_ref = self.source("B", user("我漏译了 retain。"), user("按提示补上保留。"))
        c_ref = self.source("C", user("这里 steady 指稳定的。"))
        e_ref = self.source("E", user("这里 dispute 作动词。"))
        a_question_ref = self.source("A-question", user("我在尝试另一篇的同号题。"))
        self.records = {}
        original = self.episode("A", "A-original", self.a_original, a_ref, formal_ids=[BANK_IDS["A"]])
        interpretation = {**original, "episode_id": "A-interpretation",
                          "correction": "原先暂定的词义归因。"}
        self.a_fix = self.episode("A", "A-withdrawal-evidence", "旧的归因应撤回，只保留原来的首译。",
                                  {**a_ref, "locator": "/messages/1"})
        episodes = {
            "A": [original, interpretation],
            "B": [self.episode("B", "B-original", "我漏译了 retain。", b_ref, formal_ids=[BANK_IDS["B"]],
                               later_observation="按提示补上保留。", hint_dependence="explanation",
                               followup_status="prompted", followup_evidence_index=1)],
            "C": [self.episode("C", "C-original", "这里 steady 指稳定的。", c_ref, formal_ids=[BANK_IDS["C"]])],
            "E": [self.episode("E", "E-Q21", "这里 dispute 作动词。", e_ref, formal_ids=[BANK_IDS["E"]],
                               unit_id="Q21", question_id="Q21", correction=PROTECTED_E)],
            "A-question": [self.episode("A-question", "A-Q21", "我在尝试另一篇的同号题。", a_question_ref,
                                       unit_id="Q21", question_id="Q21", correction="Correct answer is D.")],
        }
        episodes["B"][0]["evidence"].append({**b_ref, "locator": "/messages/1"})
        for label, observations in episodes.items():
            self.records[label] = {"key": KEYS[label], "aliases": [], "teaching_hint": "读当前上下文。",
                                   "visibility": "protected" if label in {"E", "A-question"} else "answer_free",
                                   "episodes": observations}
        self.records["A"].update(latest_episode_id="A-interpretation", teaching_hint="旧归因待核。")
        publish_personal_summaries(self.repo, self.state, {"schema_version": SCHEMA, "records": [self.records["A"]]},
                                   bootstrap_existing_formal=True)
        digest = self.routes()[KEYS["A"]]
        parent = {"path": f"intake/personal-summary/objects/{digest}.json", "sha256": digest, "locator": "/episodes/1"}
        dependent = self.episode("D", "D-interpretation", self.a_original, a_ref, source_episode_id="A-interpretation",
                                 correction="从 A 的旧释义推得的派生判断。")
        dependent["evidence"].append(parent)
        self.records["D"] = {"key": KEYS["D"], "aliases": [], "visibility": "answer_free",
                             "teaching_hint": "依赖 A 的旧释义。", "episodes": [dependent]}
        self.claims = [{"claim_id": f"claim:{label}", "point_key": KEYS[label],
                        "basis": "user_explicit" if label == "C" else "inference",
                        "status": "confirmed" if label == "C" else "candidate",
                        "content": "这里 steady 指稳定的。" if label == "C" else "旧解释待核实。",
                        "evidence_episode_ids": [self.records[label]["episodes"][-1]["episode_id"]]}
                       for label in ("A", "C", "D")]
        rows = [self.fixture.row] + [
            {**self.fixture.row, "id": BANK_IDS[label], "item": item, "meaning": meaning,
             "source_article": f"articles/{label}.json", "source_sentence": sentence}
            for label, item, meaning, sentence in (
                ("B", "retain", "保留", "Retain the original format."),
                ("C", "steady", "稳定的", "They recorded a steady pace."),
                ("E", "dispute", "质疑", "They dispute the proposal."),
            )
        ]
        self.official_paths = []
        self.article_a = self.exam_source("A", 2097, rows[0]["source_sentence"])
        article_e = self.exam_source("E", 2098, rows[-1]["source_sentence"])
        rows[0]["source_article"] = self.article_a["source_article"]
        rows[-1]["source_article"] = article_e["source_article"]
        prepare_reading(self.repo, self.repo / DEFAULT_OUTPUT)
        self.bank.write_bytes(serialize_csv(rows, MASTER_HEADER))
        atomic_write_json(self.state / "memory/enabled.json", {"enabled": True})
        self.baseline = {"schema_version": SCHEMA, "records": list(self.records.values()), "memory_claims": self.claims}
        self.baseline_review = self.source_review(self.baseline)
        result = publish_personal_summaries(self.repo, self.state, self.baseline, source_review=self.baseline_review)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual([claim["claim_id"] for claim in self.teaching("D")["matched"][0]["claims"]], ["claim:D"])
        self.assertEqual(source_dependents(self.repo, self.state, [self.witnesses["A"]]), sorted([KEYS["A"], KEYS["D"]]))
        self.old_routes = self.routes()
        self.assertEqual(set(self.old_routes), set(KEYS.values()))
        self.old_rows = self.rows()
        self.old_bank = self.bank_hashes()
        self.old_generation = result["memory"]["memory_generation"]
        self.old_claims_sha256 = file_sha256(self.state / "memory/claims.json")
        self.old_witness_hashes = self.witness_hashes()
        self.controls = self.control_reads()

    def witness_hashes(self):
        return {path.relative_to(self.repo).as_posix(): file_sha256(path)
                for path in (*self.witnesses.values(), *self.official_paths)}

    def control_reads(self):
        results = {}
        for label, hint in (("B", "explanation"), ("C", "not_observed"), ("E", "not_observed")):
            native = self.teaching(label, unlock=label == "E")
            self.assertEqual(native["status"], "ready", native)
            self.assertEqual([p["key"] for p in native["matched"]], [KEYS[label]])
            point = native["matched"][0]
            self.assertEqual(point["latest_observation"]["formal_ids"], [BANK_IDS[label]])
            self.assertEqual(point["latest_observation"]["hint_dependence"], hint)
            self.assertTrue(point["sources"])
            for source_ref in point["sources"]:
                self.assertEqual(source_ref["verification"], "verified_at_build")
                for ref in source_ref.get("references", [source_ref]):
                    self.assertEqual(ref["locator_status"], "verified")
                    self.assertIn(ref["locator"], {"/messages/0", "/messages/1"})
            context = self.cli("query-learning-context", "--bank-id", BANK_IDS[label])
            formal = context["formal_records"][0]
            self.assertEqual(formal["record"]["id"], BANK_IDS[label])
            self.assertEqual(formal["effective_state"], "active")
            results[label] = {"id": BANK_IDS[label], "formal_sha256": object_sha256(formal),
                              "teaching_sha256": object_sha256(point), "visibility": point["visibility"],
                              "hint_dependence": hint, "source_id": SOURCES[label], "locator_check": "verified"}
        for label, source, unlock in (("E", None, False), ("E", SOURCES["A"], True), ("A-question", None, False)):
            denial = self.teaching(label, source=source, unlock=unlock)
            self.assertFalse(denial["matched"], denial)
            self.assertNotIn(PROTECTED_E, json.dumps(denial))
        self.assertEqual(self.teaching("A-question", unlock=True)["status"], "ready")
        ambiguity = self.cli("query-learning-context", "--alias", "dispute")
        self.assertEqual(ambiguity["reason"], "ambiguous_alias")
        self.assertEqual(ambiguity["aliases"]["dispute"], ["bank:" + BANK_IDS[label] for label in ("A", "E")])
        return results

    def assert_delta(self, expected_bank_ids, expected_latest, *, managed_article=None):
        rows, routes = self.rows(), self.routes()
        self.assertEqual(set(rows), set(self.old_rows))
        self.assertEqual(set(routes), set(self.old_routes))
        changed = sorted(key for key in rows if rows[key] != self.old_rows[key])
        self.assertEqual(changed, expected_bank_ids)
        self.assertEqual(sorted(key for key in routes if routes[key] != self.old_routes[key]), [KEYS["A"]])
        preserved = self.control_reads()
        self.assertEqual(preserved, self.controls)
        witness_hashes = dict(self.old_witness_hashes)
        if managed_article is not None:
            path, digest = managed_article
            witness_hashes[path] = digest
        self.assertEqual(self.witness_hashes(), witness_hashes)
        objects = self.state / "personal-summary/objects"
        for digest in self.old_routes.values():
            self.assertEqual(file_sha256(objects / f"{digest}.json"), digest)
        a = self.teaching("A")
        if expected_latest is None:
            self.assertFalse(a["matched"])
            self.assertEqual(a["unavailable"][0]["reason"], "observation_order_ambiguous")
        else:
            self.assertEqual(a["status"], "ready", a)
            self.assertEqual(a["matched"][0]["latest_observation"]["episode_id"], expected_latest)
            self.assertEqual(a["matched"][0]["claims"], [])
        self.assertNotIn(EXTERNAL_EXAMPLE, json.dumps(a))
        d = self.teaching("D")
        self.assertFalse(d["matched"], d)
        self.assertEqual(d["pending"], [{"key": KEYS["D"], "reason": "invalidated_summary_requires_refresh"}])
        lifecycle = read_lifecycle(self.state)
        self.assertEqual(lifecycle["invalidated_episode_ids"], ["A-interpretation", "D-interpretation"])
        self.assertEqual(lifecycle["invalidated_claim_ids"], ["claim:A", "claim:D"])
        self.assertEqual(lifecycle["invalidated_summary_keys"], sorted([KEYS["A"], KEYS["D"]]))
        self.assertNotIn("A-original", lifecycle["invalidated_episode_ids"])
        self.assertEqual(file_sha256(self.state / "memory/claims.json"), self.old_claims_sha256)
        old_a = load_json(objects / f"{self.old_routes[KEYS['A']]}.json")["episodes"][0]
        new_a = load_json(objects / f"{routes[KEYS['A']]}.json")["episodes"][0]
        self.assertEqual(new_a, old_a)
        self.assertEqual(new_a["first_attempt"], self.a_original)
        self.assertEqual(self.old_rows[BANK_IDS["A"]]["source_sentence"], rows[BANK_IDS["A"]]["source_sentence"])
        self.assertNotIn(EXTERNAL_EXAMPLE, self.bank.read_text())
        return {"changed_formal_ids": changed, "changed_summary_keys": [KEYS["A"]],
                "invalidated_claim_ids": lifecycle["invalidated_claim_ids"],
                "invalidated_episode_ids": lifecycle["invalidated_episode_ids"],
                "independent_records": preserved, "non_target_modified_count": 0, "non_target_deleted_count": 0,
                "original_evidence": {"status": "preserved", "fixture_sha256": object_sha256(self.old_witness_hashes)},
                "managed_article_locator_changes": [managed_article[0]] if managed_article else [],
                "same_word_and_q21_isolation": "pass",
                "global_versions": {"bank_before": self.old_bank["master_bank.csv"], "bank_after": self.bank_hashes()["master_bank.csv"],
                                    "generation_before": self.old_generation, "generation_after": a["generation"]},
                "dependent_readback": d["pending"][0]["reason"]}

    def correction(self, record, evidence):
        return {"schema_version": SCHEMA, "records": [record], "memory_corrections": [{
            "correction_id": "fix:A", "action": "retract", "target_claim_ids": ["claim:A"],
            "target_episode_ids": ["A-interpretation"], "evidence_episode_ids": [evidence],
            "reason": "原始用户更正只撤回解释。"}]}

    def test_formal_correction_closes_after_index_failure(self):
        self.seed()
        fixture = self.fixture
        request = fixture._request("A-correction", independent=True)
        request["source"].update(source_hash=self.article_a["source_hash"], source_article=self.article_a["source_article"])
        package = fixture._capture(request)
        refs = [fixture._ref(package), fixture._ref(package, 1, "assistant_message")]
        update = {"action_id": "fix:bank:A", "action_type": "master_bank_update", "record_id": BANK_IDS["A"],
                  "updates": {"meaning": "争议"}, "reason": "按原话更正当前义项。", "evidence_refs": refs}
        old_date = "2026-09-06"
        stale_package = fixture._capture(fixture._request("stale", day=old_date, independent=True))
        stale_args, _, _, _ = fixture._prepare_apply([stale_package], [{**update, "evidence_refs": [fixture._ref(stale_package)]}], day=old_date)
        event = fixture._event("fixture-correction", request, kind="correction", outcome="correct")
        args, _, proposal, dry = fixture._prepare_apply([package], [update, fixture._action(event, refs)])
        writer_path, writer = apply_nightly(self.state, self.repo, **args)
        self.assertEqual(writer["status"], "APPLIED")
        self.assertEqual(dry["proposed_posthashes"], writer["formal_posthashes"])
        writer_delta = self.bank_hashes()
        self.assertEqual({name for name in writer_delta if writer_delta[name] != self.old_bank[name]},
                         {"master_bank.csv", "learning_events.jsonl"})
        _, stale = apply_nightly(self.state, self.repo, **stale_args)
        self.assertEqual((stale["status"], stale["formal_write_count"]), ("CAS_CONFLICT", 0))
        self.assertEqual(self.bank_hashes(), writer_delta)
        refresh = refresh_sentence_support(self.state, self.repo, manifest_path=args["manifest_path"],
                                           writer_receipt_path=writer_path, proposal_path=proposal)
        journal = Path(writer["journal_path"])
        writer_hashes = (file_sha256(writer_path), file_sha256(journal))
        with self.subTest("reapply identical committed action set after support refresh"):
            self.assertEqual(apply_nightly(self.state, self.repo, **args), (writer_path, writer))
        with self.subTest("same action set ID cannot bind different bytes"):
            path = args["actions_path"]
            original = path.read_bytes()
            modified = load_json(path)
            modified["actions"][0]["reason"] = "Changed under the same action_set_id"
            atomic_write_json(path, modified)
            try:
                with self.assertRaisesRegex(ValidationError, "committed action set has different bytes"):
                    apply_nightly(self.state, self.repo, **args)
            finally:
                path.write_bytes(original)
        self.assertEqual((file_sha256(writer_path), file_sha256(journal)), writer_hashes)

        proof = self.cli("validate-formal-receipt", "--writer-receipt", writer_path)
        package_root = Path(package["package_path"])
        source_hashes = {item.name: file_sha256(item) for item in package_root.iterdir() if item.is_file()}
        article_path = self.repo / self.article_a["source_article"]
        article_before = article_path.read_bytes()
        contract = fixture._volume()
        archive = archive_committed_batch(self.state, self.repo, manifest_path=args["manifest_path"],
                                          writer_receipt_path=writer_path, contract=contract)
        self.assertEqual(archive["status"], "COMPLETE")
        pid = package["package_id"]
        header = f"<!-- english-display-assets:begin {pid} -->\n".encode()
        footer = f"<!-- english-display-assets:end {pid} -->\n".encode()
        article_after = article_path.read_bytes()
        self.assertTrue(article_after.startswith(article_before.rstrip() + b"\n\n" + header))
        self.assertTrue(article_after.endswith(footer))
        self.assertEqual(article_after.count(header), 1)
        self.assertEqual(resolve_article_source(self.repo, self.article_a["source_article"]), self.article_a)
        managed_article = (self.article_a["source_article"], file_sha256(article_path))
        located = resolve_archived_package_from_pointer(self.state, self.repo, study_date="2026-09-07",
                                                       package_id=package["package_id"], expected_package_sha256=package["package_sha256"])
        self.assertEqual({name: file_sha256(located / name) for name in source_hashes}, source_hashes)
        ref = {"package_id": package["package_id"], "package_sha256": package["package_sha256"],
               "member": "conversation.json", "sha256": source_hashes["conversation.json"], "locator": "/messages/0"}
        revised_episode = self.episode("A", "A-formal-correction", request["conversation"][0]["content"], ref,
                                       study_date="2026-09-07", formal_ids=[BANK_IDS["A"]], correction="此处取争议义。")
        updated = {**self.records["A"], "episodes": [*self.records["A"]["episodes"], revised_episode],
                   "latest_episode_id": "A-formal-correction", "teaching_hint": "用本次更正核对词义。"}
        document = self.correction(updated, "A-formal-correction")
        document["coverage"] = [{"package_id": package["package_id"], "disposition": "covered", "keys": [KEYS["A"]]}]
        with patch("english_pipeline.memory_store.build_memory_projection", side_effect=OSError("fixture projection failure")):
            published = publish_personal_summaries(self.repo, self.state, document, formal_receipt=proof)
        self.assertEqual(published["pending_component"], "memory_index")
        self.assertFalse(self.teaching("A")["matched"])
        self.assertEqual(self.control_reads(), self.controls)
        transaction = next(self.state.glob(f"nightly/**/transactions/{writer['receipt_id']}/transaction.json"))
        immutable = {path: file_sha256(path) for path in (writer_path, transaction, journal, self.events)}
        recovered = self.cli("recover-memory-closeout")
        self.assertEqual((recovered["status"], recovered["formal_apply_invocation_count"]), ("ready", 0))
        again = recover_nightly(self.state, self.repo, transaction_path=transaction, archive_contract=contract)
        self.assertEqual(again["status"], "PASS", again["publication"])
        replay = recover_nightly(self.state, self.repo, transaction_path=transaction, archive_contract=contract)
        self.assertEqual(replay["status"], "PASS")
        self.assertEqual(replay["formal_write_count"], 0)
        self.assertEqual({path: file_sha256(path) for path in immutable}, immutable)
        self.assertEqual(self.bank_hashes(), writer_delta)
        self.assertEqual(self.rows()[BANK_IDS["A"]], {**self.old_rows[BANK_IDS["A"]], "meaning": "争议"})
        events = load_learning_events(self.events)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"]["kind"], "correction")
        self.assertEqual(events[0]["event"]["user_response"], request["conversation"][0]["content"])
        quality = again["publication"][0]["local_quality"]
        self.assertEqual(quality["status"], "VERIFIED_CURRENT")
        readback = quality["native_teaching_readback"]["points"]
        self.assertEqual({key: item["teaching_visibility"] for key, item in readback.items()},
                         {KEYS["A"]: "answer_free", KEYS["D"]: "invalidated"})
        projection = load_json(Path(recovered["recovered"][0]["receipt_path"]))["projection_receipt"]
        self.assertEqual(projection["updated_keys"], sorted([KEYS["A"], KEYS["D"]]))
        emit_receipt("master_bank_update", request_target_ids=[BANK_IDS["A"], "claim:A", "A-interpretation"],
                     scope=[KEYS["A"], KEYS["D"]],
                     **self.assert_delta([BANK_IDS["A"]], "A-formal-correction", managed_article=managed_article),
                     formal_write_count=writer["formal_write_count"], learning_event_write_count=len(events), model_requests=0,
                     added_learning_event_ids=[events[0]["event_id"]], committed_action_replay="idempotent",
                     writer_status=writer["status"], native_closeout=quality["status"],
                     verified_writer_sha256=proof["writer_receipt_sha256"], archive_receipt_sha256=archive["archive_receipt_sha256"],
                     support_status=refresh[1]["status"], original_package_sha256=package["package_sha256"],
                     errors={"stale_prehashes": stale["validation"]["reason"], "index": published["memory"]["error"]},
                     recovery={"status": recovered["status"], "nightly_status": again["status"], "repeat_nightly_status": replay["status"],
                               "formal_apply_invocation_count": 0, "writer_artifacts_preserved": True,
                               "B_C_readable_during_index_pending": True})

    def test_retract_derived_claim_preserves_confirmed_facts_and_raw_attempt(self):
        self.seed()
        invalid = {"schema_version": SCHEMA, "records": [self.records["A"]],
                   "memory_claims": [{**self.claims[0], "claim_id": "claim:unsupported", "status": "confirmed"}]}
        with self.assertRaisesRegex(ValidationError, "inference cannot become"):
            publish_personal_summaries(self.repo, self.state, invalid, source_review=self.source_review(invalid))
        invalid["memory_claims"] = [{**self.claims[0], "content": "A stable ID cannot be reused for this."}]
        with self.assertRaisesRegex(IdempotencyConflict, "stable claim"):
            publish_personal_summaries(self.repo, self.state, invalid, source_review=self.source_review(invalid))
        assistant_ref = {**self.a_fix["evidence"][0], "locator": "/messages/2"}
        assistant_episode = self.episode("A", "A-assistant-only", EXTERNAL_EXAMPLE, assistant_ref)
        invalid = {"schema_version": SCHEMA, "records": [
            {**self.records["A"], "episodes": [*self.records["A"]["episodes"], assistant_episode]}]}
        with self.assertRaisesRegex(ValidationError, "assistant explanation alone"):
            publish_personal_summaries(self.repo, self.state, invalid, source_review=self.source_review(invalid))
        self.assertEqual(self.routes(), self.old_routes)
        revised = {**self.records["A"], "episodes": [*self.records["A"]["episodes"], self.a_fix],
                   "latest_episode_id": "A-original", "teaching_hint": "保留首译，暂不沿用旧归因。"}
        doc = self.correction(revised, "A-withdrawal-evidence")
        review = self.source_review(doc)
        published = publish_personal_summaries(self.repo, self.state, doc, source_review=review)
        self.assertEqual(published["status"], "published", published)
        corrected = self.routes()
        for request, binding in ((doc, review), (self.baseline, self.baseline_review)):
            replay = publish_personal_summaries(self.repo, self.state, request, source_review=binding)
            self.assertTrue(replay["idempotent_replay"])
            self.assertFalse(replay["current_pointer_rewritten"])
        self.assertEqual(self.routes(), corrected)
        self.assertEqual(self.bank_hashes(), self.old_bank)
        unscoped = self.cli("query-memory-context", "--backend", "native", "--view", "teaching", "--key", KEYS["A"])
        self.assertEqual(unscoped["unavailable"][0]["reason"], "observation_order_ambiguous")
        source_read = self.cli("query-memory-context", "--backend", "native", "--view", "teaching",
                               "--source-id", SOURCES["A"], "--unit-id", "S01", "--id", "A-original")
        self.assertEqual(source_read["matched"][0]["latest_observation"]["first_attempt"], self.a_original)
        self.assertEqual(self.teaching("C")["matched"][0]["claims"][0]["status"], "confirmed")
        projection = load_json(Path(published["memory"]["receipt_path"]))["projection_receipt"]
        self.assertEqual(projection["updated_keys"], sorted([KEYS["A"], KEYS["D"]]))
        emit_receipt("retract", request_target_ids=["claim:A", "A-interpretation"], scope=[KEYS["A"], KEYS["D"]],
                     **self.assert_delta([], None), formal_write_count=0, learning_event_write_count=0,
                     model_requests=0, memory_publication="ready", exact_and_old_replay="idempotent",
                     original_attempt_teaching_readback="A-original", confirmed_control_preserved=True,
                     errors={"inference_as_confirmed": "ValidationError", "stable_claim_conflict": "IdempotencyConflict",
                             "assistant_only_learner_observation": "ValidationError"})

    def test_external_reference_cannot_replace_exam_source(self):
        request = self.fixture._request("synthetic-external")
        request["conversation"][1]["content"] = (
            f"Synthetic external example. {EXTERNAL_EXAMPLE} A similar word is challenge.")
        package = self.fixture._capture(request)
        ref = self.fixture._ref(package, 1, "assistant_message")
        source_ref = {**ref, "kind": "source_text", "pointer": "source.json#/identity/source_sentence"}
        self.fixture.row["source_sentence"] = "The finding remains open to dispute!"
        self.fixture._write_bank()
        before = self.bank_hashes()
        action = {"action_id": "fix:source:A", "action_type": "master_bank_update", "record_id": BANK_IDS["A"],
                  "reason": "Fixture for source-role validation.", "evidence_refs": [source_ref, ref],
                  "updates": {"source_sentence": request["source"]["source_sentence"]}}
        manifest, _, path = self.fixture._freeze_actions([package], [action], day="2026-09-07")
        _, dry = apply_nightly(self.state, self.repo, manifest_path=manifest, actions_path=path)
        self.assertEqual(dry["status"], "DRY_RUN_VALID")
        document = load_json(path)
        for field, value, reason in (
            ("source_article", "https://example.invalid/external-lexicon", "unsupported fields"),
            ("source_sentence", EXTERNAL_EXAMPLE, "corrected source sentence"),
        ):
            document["actions"][0]["updates"] = {field: value}
            atomic_write_json(path, document)
            with self.assertRaisesRegex(ValidationError, reason):
                apply_nightly(self.state, self.repo, manifest_path=manifest, actions_path=path)
        self.assertEqual(self.bank_hashes(), before)
        self.assertFalse((self.repo / "bank/concept_aliases.json").exists())
        self.assertEqual(load_learning_events(self.events), [])
        emit_receipt("verify_external_reference", request_target_ids=[BANK_IDS["A"]], changed_formal_ids=[],
                     source_sentence="verified original accepted; assistant reference rejected",
                     source_article="external overwrite rejected", synonyms="no formal aliases created",
                     appear_count_last_seen_and_mastery="unchanged",
                     errors={"external_reference": "ValidationError: corrected source sentence",
                             "external_article": "ValidationError: unsupported fields"},
                     formal_write_count=0, learning_event_write_count=0, model_requests=0)
