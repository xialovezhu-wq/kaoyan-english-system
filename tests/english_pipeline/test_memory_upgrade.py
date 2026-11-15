"""Synthetic boundary cases for the 2026-11-15 English memory upgrade."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

from english_pipeline.answer_safety import contains_answer_signal
from english_pipeline.errors import IdempotencyConflict, ValidationError
from english_pipeline.memory_query import query_memory_context
from english_pipeline.memory_store import build_memory_projection, source_dependents
from english_pipeline.personal_summary import key_hash, query_personal_summary, SCHEMA, publish_personal_summaries
from english_pipeline.publication import build_source_descriptors
from english_pipeline.util import canonical_bytes, file_sha256, object_sha256
from english_pipeline.web_review import web_reference_files
from test_memory_store import MemoryFixture


class MemoryUpgradeTests(MemoryFixture):
    def query(self, **options):
        return query_memory_context(self.repo, self.state, **options)

    def test_long_history_publishes_and_teaching_does_not_read_older_observation_bodies(self):
        episodes = [{**self.record["episodes"][0], "episode_id": f"fixture-history-{n}",
                     "first_attempt": "机制造成误判；这只是一条测试文本。" * 50}
                    for n in range(42)]
        latest = {**self.record["episodes"][0], "episode_id": "fixture-correction", "study_date": "2026-11-01",
                  "first_attempt": "已经读对主干；修饰范围仍有误。",
                  "correction": "关系从句仅修饰前面的名词。"}
        self.record.update(episodes=episodes)
        self.publish()
        self.publish({**self.record, "episodes": [latest]})
        index = json.loads((self.state / "personal-summary/current.json").read_bytes())
        pointer = index["routes"][key_hash(self.record["key"])]
        path = self.state / "personal-summary/objects" / (pointer["sha256"] + ".json")
        self.assertGreater(path.stat().st_size, 90_000)
        self.assertEqual(json.loads(path.read_bytes())["episodes"], [*episodes, latest])
        build_memory_projection(self.repo, self.state)
        original = json.loads

        def without_older_text(data, *args, **kwargs):
            if "机制造成误判" in data:
                raise AssertionError("a teaching read fetched an older observation body")
            return original(data, *args, **kwargs)

        with patch("english_pipeline.memory_query.json.loads", side_effect=without_older_text):
            result = self.query(keys=[self.record["key"]], source_id="ARTICLE-A")
        self.assertEqual(result["matched"][0]["latest_observation"]["correction"], latest["correction"])
        self.assertEqual(result["matched"][0]["episodes_total"], 43)
        self.assertLessEqual(len(canonical_bytes(result)), 6144)
        history = self.query(keys=[self.record["key"]], view="history", max_bytes=4000)
        self.assertEqual(history["status"], "partial")
        self.assertEqual(history["matched"][0]["episodes"][0]["episode_id"], "fixture-correction")
        self.assertGreater(history["matched"][0]["remaining_episode_ids_omitted"], 0)
        legacy = query_personal_summary(self.state, keys=[self.record["key"]], view="teaching")
        self.assertEqual(legacy["unavailable"][0]["reason"], "use_bounded_memory_query")
        conflict = {**latest, "correction": "与先前同 ID 的证据不一致"}
        with self.assertRaises(IdempotencyConflict):
            self.publish({**self.record, "episodes": [conflict]})
        self.assertEqual(json.loads((self.state / "personal-summary/current.json").read_bytes()), index)

    def test_late_backfill_keeps_latest_correction_in_teaching(self):
        self.record.update(teaching_hint="最近核实的解释", latest_episode_id="E-OLD")
        self.publish()
        self.publish({**self.record, "latest_episode_id": "E-OLD"})
        prior = {**self.record["episodes"][0], "episode_id": "earlier", "study_date": "2026-08-01"}
        self.publish({k: v for k, v in {**self.record, "episodes": [prior],
                     "teaching_hint": "旧材料的解释"}.items() if k != "latest_episode_id"})
        build_memory_projection(self.repo, self.state)
        point = self.query(keys=[self.record["key"]])["matched"][0]
        self.assertEqual(point["teaching_hint"], "最近核实的解释")
        self.assertEqual(point["latest_observation"]["episode_id"], "E-OLD")
        self.assertEqual(point["episodes_total"], 2)

    def test_t1_acknowledgment_t2_separate_and_prompted_variant(self):
        transcript = self.repo / "articles/synthetic.json"
        transcript.write_bytes(canonical_bytes({"messages": [
            {"role": "user", "content": "委员会昨天发表报告这件事还没有完成。"},
            {"role": "assistant", "content": "主句是 The report is incomplete。"},
            {"role": "user", "content": "懂了。"},
            {"role": "user", "content": "proposal 是主语，was rejected 是主句谓语。"},
            {"role": "assistant", "content": "先标修饰提案的从句。"},
            {"role": "user", "content": "被拒的是那份提案，从句修饰 proposal。"}]}))
        refs = [{"path": "articles/synthetic.json", "sha256": file_sha256(transcript),
                 "locator": f"/messages/{i}"} for i in range(6)]
        t1 = {**self.record["episodes"][0], "episode_id": "SYNTH-EN-01-T1", "study_date": "2026-09-01",
              "first_attempt": "委员会昨天发表报告这件事还没有完成。", "first_attempt_hint_dependence": "none",
              "correction": "主句是 The report is incomplete。", "correction_observed_at": "2026-09-01",
              "later_observation": "用户只回应懂了。", "last_observed_at": "2026-09-01",
              "hint_dependence": "not_observed", "followup_status": "acknowledgment_only",
              "followup_evidence_index": 2, "evidence": refs[:3]}
        for i, reason in ((1, "exact original user"), (2, "acknowledgment")):
            fictitious = {**t1, "hint_dependence": "none", "followup_status": "independent", "followup_evidence_index": i}
            with self.assertRaisesRegex(ValidationError, reason):
                self.publish({**self.record, "episodes": [fictitious]})
        self.publish({**self.record, "episodes": [t1]})
        t2 = {**t1, "episode_id": "SYNTH-EN-01-T2", "study_date": "2026-09-02",
              "first_attempt": "proposal 是主语，was rejected 是主句谓语。",
              "correction": "此句主干判断正确。", "correction_observed_at": "2026-09-02",
              "later_observation": "未观察本句后续作答。", "followup_status": "not_observed",
              "followup_evidence_index": None, "last_observed_at": None, "evidence": refs[3:4]}
        self.publish({**self.record, "episodes": [t2]})
        variant = {**t2, "episode_id": "SYNTH-EN-01-T2-PROMPTED", "first_attempt_hint_dependence": "explanation",
                   "first_attempt": "被拒的是那份提案，从句修饰 proposal。", "evidence": refs[4:]}
        alternate_key = "syntax:synthetic-prompted"
        self.publish({**self.record, "key": alternate_key, "aliases": [], "episodes": [variant]})
        build_memory_projection(self.repo, self.state)
        old = self.query(keys=[self.record["key"]], view="history", as_of="2026-09-01")
        self.assertEqual([e["episode_id"] for e in old["matched"][0]["episodes"]], ["SYNTH-EN-01-T1"])
        self.assertEqual(old["matched"][0]["episodes"][0]["followup_status"], "acknowledgment_only")
        self.assertNotIn("proposal", json.dumps(old, ensure_ascii=False))
        result = self.query(keys=[self.record["key"], alternate_key])
        self.assertEqual([p["latest_observation"]["first_attempt_hint_dependence"] for p in result["matched"]],
                         ["none", "explanation"])

    def test_known_changed_source_invalidates_all_indexed_dependents(self):
        source = self.repo / "immutable.json"
        source.write_text('{"initial":"old evidence"}')
        self.record["episodes"][0]["evidence"] = [{"path": "immutable.json", "sha256": file_sha256(source),
                                                   "locator": "/initial"}]
        self.raw_publish(self.record)
        related = {**self.record, "key": "syntax:dependent", "aliases": []}
        self.raw_publish(related)
        build_memory_projection(self.repo, self.state)
        source.write_text('{"initial":"different evidence"}')
        keys = source_dependents(self.repo, self.state, [Path("immutable.json")])
        self.assertEqual(set(keys), {self.record["key"], related["key"]})
        result = build_memory_projection(self.repo, self.state, keys=keys)
        self.assertEqual(result["counts"]["issues"]["source_hash_conflict"], 2)
        query = self.query(keys=keys)
        self.assertEqual(query["status"], "conflict")
        self.assertFalse(query["matched"])

    def test_actual_answer_hidden_across_native_legacy_and_web_export(self):
        self.publish()
        unsafe = copy.deepcopy(self.record)
        unsafe.update(key="syntax:fixture-advisory", aliases=["syntax:plain-alias"])
        unsafe["episodes"][0].update(episode_id="LEAK", question_id="Q21", hint_dependence="排除 Ｂ；答案是D。")
        with self.assertRaisesRegex(ValidationError, "answer-free summary"):
            self.publish(unsafe)
        self.raw_publish(unsafe)  # Pretend a prior version wrote the wrong label.
        indirect = {**copy.deepcopy(unsafe), "key": "option_type:fixture-evidence", "aliases": [],
                    "teaching_hint": "原文最后一段给出了选项理由。"}
        indirect["episodes"][0].update(episode_id="INDIRECT", hint_dependence="explanation")
        self.raw_publish(indirect)
        build_memory_projection(self.repo, self.state)
        for kwargs in ({"keys": ["syntax:plain-alias", indirect["key"]]},
                       {"query": "排除 B 最后一段选项理由"}, {"ids": ["LEAK"]}):
            result = self.query(**kwargs)
            self.assertNotIn("答案是D", json.dumps(result, ensure_ascii=False))
            self.assertNotIn("fixture-evidence", json.dumps(result.get("matched"), ensure_ascii=False))
        result = self.query(keys=[unsafe["key"]], source_id="ARTICLE-A", question_id="Q21",
                            unlocked_question_ids=["Q21"])
        self.assertIn("答案是D", result["matched"][0]["latest_observation"]["hint_dependence"])
        legacy = query_personal_summary(self.state, keys=[unsafe["key"]], question_review=True)
        self.assertFalse(legacy["matched"])
        files = web_reference_files(self.repo, self.state)
        prefix = "context/personal-summary/"
        index = json.loads(files[prefix + "current.json"])
        self.assertEqual(index["coverage"]["included_points"], 1)
        pointer = index["routes"][key_hash(self.record["key"])]
        exported = json.loads(files[prefix + "objects/" + pointer["sha256"] + ".json"])
        self.assertEqual(object_sha256(exported), pointer["sha256"])
        self.assertNotIn("evidence", exported["episodes"][0])
        self.assertFalse(any(contains_answer_signal(json.loads(data)) for name, data in files.items()
                             if name.startswith(prefix + "objects/")))

    def test_exact_miss_does_not_run_candidate_search_even_with_query_parameter(self):
        self.publish()
        build_memory_projection(self.repo, self.state)
        with patch("english_pipeline.memory_query._natural_candidates", side_effect=AssertionError("broadened lookup")):
            result = self.query(keys=["word:unrecorded"], query="同位语")
        self.assertEqual(result["status"], "not_found")

    def test_generic_shared_snapshot_omits_local_point_memory_but_keeps_source_evidence(self):
        self.publish()
        generated = self.repo / "wiki/memory/fixture.md"
        generated.parent.mkdir(parents=True)
        generated.write_text("derived point projection")
        descriptors = build_source_descriptors(self.repo)
        paths = [row["relative_path"] for row in descriptors["files"]]
        self.assertIn("articles/record.md", paths)
        self.assertFalse(any(path.startswith(("intake/personal-summary/", "wiki/memory/")) for path in paths))
