import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from english_pipeline import memory_query
from english_pipeline.errors import ValidationError
from english_pipeline.memory_query import query_memory_context
from english_pipeline.memory_store import build_memory_projection, source_dependents
from english_pipeline.personal_summary import query_personal_summary
from english_pipeline.util import canonical_bytes, file_sha256
from tests.english_pipeline.test_memory_store import MemoryFixture


class MemoryQueryTests(MemoryFixture):
    def build(self):
        self.publish()
        return build_memory_projection(self.repo, self.state)

    def query(self, **kwargs):
        return query_memory_context(self.repo, self.state, **kwargs)

    def lifecycle(self, **values):
        root = self.state / "memory"
        root.mkdir(parents=True, exist_ok=True)
        (root / "lifecycle.json").write_text(json.dumps(values))

    def test_history_deduplicates_sources_without_losing_episode_provenance(self):
        self.record["episodes"] = [{**copy.deepcopy(self.record["episodes"][0]), "episode_id": f"E-{i}"}
                                   for i in range(6)]
        self.build()
        result = self.query(keys=[self.record["key"]], view="history", max_bytes=8192)
        point = result["matched"][0]
        self.assertEqual(len(point["episodes"]), 6)
        self.assertEqual(len(point["sources"]), 1)
        self.assertTrue(all(e["source_keys"] == [point["sources"][0]["source_key"]] for e in point["episodes"]))
        self.assertLessEqual(len(canonical_bytes(result)), 8192)

    def test_budget_continuation_retrieves_the_exact_omitted_episode(self):
        self.record["episodes"] = [{**copy.deepcopy(self.record["episodes"][0]), "episode_id": f"E-{i}",
                                   "first_attempt": "原始观察" * 40} for i in range(6)]
        self.build()
        first = self.query(keys=[self.record["key"]], view="history", max_bytes=3000)
        point = first["matched"][0]
        self.assertTrue(point["remaining_episode_ids"])
        missing = point["remaining_episode_ids"][0]
        second = self.query(ids=[missing], view="history", max_bytes=3000)
        self.assertEqual([e["episode_id"] for e in second["matched"][0]["episodes"]], [missing])
        self.assertNotIn("teaching_hint", second["matched"][0])
        self.assertLessEqual(len(canonical_bytes(first)), 3000)
        self.assertLessEqual(len(canonical_bytes(second)), 3000)

    def test_distinct_query_terms_beat_repeating_one_rare_term(self):
        self.record["teaching_hint"] = "alpha beta gamma 的不同证据。"
        self.publish()
        noisy = copy.deepcopy(self.record)
        noisy.update(key="syntax:noisy", aliases=[], teaching_hint="gamma " * 30)
        noisy["episodes"][0].update(episode_id="NOISY", first_attempt="gamma", correction="gamma", later_observation="gamma")
        self.publish(noisy)
        build_memory_projection(self.repo, self.state)
        result = self.query(query="alpha beta gamma")
        self.assertEqual(result["matched"][0]["key"], self.record["key"])

    def test_exact_alias_and_cjk_candidate_keep_hint_dependence(self):
        self.build()
        for kwargs in ({"keys": ["syntax:同位语"]}, {"query": "名词补充说明"}):
            result = self.query(**kwargs)
            self.assertEqual(result["matched"][0]["key"], self.record["key"])
            self.assertEqual(result["matched"][0]["latest_observation"]["hint_dependence"], "explanation")
            self.assertTrue(result["coverage"]["not_a_mastery_assessment"])
            self.assertEqual(result["matched"][0]["teaching_hint_status"], "legacy_unclassified")

    def test_word_inflection_is_candidate_not_identity_rewrite(self):
        self.record.update(key="word:advertiser", aliases=[], teaching_hint="advertiser 是广告商。")
        self.build()
        self.assertEqual(self.query(keys=["word:advertisers"])["status"], "not_found")
        result = self.query(query="advertisers")
        self.assertEqual(result["matched"][0]["key"], "word:advertiser")
        self.assertEqual(result["matched"][0]["retrieval_method"], "inflection_candidate")

    def test_backfilled_old_episode_does_not_replace_latest_and_as_of_omits_current_hint(self):
        newer = copy.deepcopy(self.record["episodes"][0])
        newer.update(episode_id="E-NEW", study_date="2026-10-20", later_observation="本次独立识别，但仅限这句。", hint_dependence="none")
        self.record["episodes"] = [newer, self.record["episodes"][0]]
        self.build()
        current = self.query(keys=[self.record["key"]])
        self.assertEqual(current["matched"][0]["latest_observation"]["episode_id"], "E-NEW")
        old = self.query(keys=[self.record["key"]], view="history", as_of="2026-09-15")
        self.assertEqual([e["episode_id"] for e in old["matched"][0]["episodes"]], ["E-OLD"])
        self.assertNotIn("teaching_hint", old["matched"][0])
        self.assertNotIn("本次独立识别", json.dumps(old, ensure_ascii=False))
        self.assertEqual(self.query(keys=[self.record["key"]], as_of="2026-08-01")["status"], "no_evidence")

    def test_cross_article_scope_returns_only_bound_observations(self):
        later = copy.deepcopy(self.record["episodes"][0])
        later.update(episode_id="E-B", study_date="2026-10-01", source_id="ARTICLE-B", unit_id="S09", first_attempt="另一篇的真实首答")
        self.record["episodes"].append(later)
        self.build()
        result = self.query(keys=[self.record["key"]], source_id="ARTICLE-B", unit_id="S09", view="history")
        self.assertEqual([e["episode_id"] for e in result["matched"][0]["episodes"]], ["E-B"])
        self.assertNotIn("teaching_hint", result["matched"][0])
        self.assertTrue(result["matched"][0]["summary_scope_incomplete"])
        digest = result["matched"][0]["summary_sha256"]
        self.lifecycle(accepted_summary_hashes={self.record["key"]: digest})
        repeated = self.query(keys=[self.record["key"]], source_id="ARTICLE-B", unit_id="S09", view="history")
        self.assertNotIn("teaching_hint", repeated["matched"][0])
        self.assertNotIn("contrasts", repeated["matched"][0])
        self.assertEqual(self.query(query="同位语", source_id="OTHER")["status"], "not_found")
        self.assertEqual(self.query(keys=[self.record["key"]], source_id="OTHER")["status"], "no_evidence")

    def test_answer_free_question_observations_still_require_the_requested_question(self):
        first = self.record["episodes"][0]
        first["question_id"] = "Q21"
        sibling = {**first, "episode_id": "SIBLING", "question_id": "Q22", "study_date": "2026-10-01"}
        other = {**first, "episode_id": "OTHER-ARTICLE", "source_id": "ARTICLE-B", "study_date": "2026-10-02"}
        self.record["episodes"].extend([sibling, other])
        self.build()
        request = {"keys": [self.record["key"]], "question_id": "Q21"}
        result = self.query(**request, source_id="ARTICLE-A")
        observation = result["matched"][0]
        self.assertEqual(observation["latest_observation"]["episode_id"], "E-OLD")
        self.assertNotIn("teaching_hint", observation)
        self.assertFalse(self.query(**request)["matched"])
        self.assertFalse(self.query(keys=request["keys"], source_id="ARTICLE-A", question_id="Q23")["matched"])

    def test_alias_requires_the_captured_article_version_and_never_unlocks_a_range(self):
        from english_pipeline.sources import resolve_article_source
        article = self.source.relative_to(self.repo).as_posix()
        self.source.write_text("# Synthetic passage\nreference_id: EXAM-READING-2014-T2\n\nOnly when the condition holds.\n")
        raw = self.repo / "raw/articles/exam-reading-corpus/2014/text-2.json"
        raw.parent.mkdir(parents=True)
        document = {"schema": "exam_reading_practice_safe_v1", "visibility": "practice_safe",
                    "article_path": article, "reference_id": "EXAM-READING-2014-T2",
                    "source_id": "CANONICAL-T2", "passage_paragraphs": ["Only when the condition holds."],
                    "questions": []}
        raw.write_text(json.dumps(document))
        binding = resolve_article_source(self.repo, article)
        episode = self.record["episodes"][0]
        episode.update(source_id=binding["reference_id"], source_hash=binding["source_hash"],
                       question_id="Q26", unit_id="Q26-Q30-FULL-REVIEW")
        from english_pipeline.util import file_sha256
        episode["evidence"][0]["sha256"] = file_sha256(self.source)
        self.build()
        request = {"keys": [self.record["key"]], "source_id": binding["source_id"]}
        self.assertEqual(self.query(**request)["matched"][0]["latest_observation"]["episode_id"], "E-OLD")
        self.assertFalse(self.query(**request, question_id="Q26")["matched"])

        document["passage_paragraphs"] = ["Only when the revised condition holds."]
        raw.write_text(json.dumps(document))
        rebuilt = build_memory_projection(self.repo, self.state, keys=[])
        self.assertEqual(rebuilt["updated_keys"], [self.record["key"]])
        self.assertFalse(self.query(**request)["matched"])
        self.assertTrue(self.query(keys=request["keys"], source_id=binding["reference_id"])["matched"])

    def test_claims_require_all_observations_in_the_returned_context(self):
        second = {**self.record["episodes"][0], "episode_id": "NEW", "study_date": "2026-10-01"}
        second["first_attempt"] = "A fully retained observation. " * 20
        self.record["episodes"].append(second)
        self.build()
        claim = {"claim_id": "two-observations", "point_key": self.record["key"],
                 "content": "本判断需要两次真实观察。", "basis": "inference", "status": "candidate",
                 "evidence_episode_ids": ["E-OLD", "NEW"]}
        (self.state / "memory/claims.json").write_text(json.dumps({"claims": {"two-observations": claim}}))
        result = self.query(keys=[self.record["key"]])
        point = result["matched"][0]
        self.assertEqual(point["latest_observation"]["episode_id"], "NEW")
        self.assertEqual(point["claims"], [])
        self.assertEqual(point["dependent_items_omitted"], 1)
        result = self.query(keys=[self.record["key"]], view="history", max_bytes=4096)
        self.assertEqual(result["matched"][0]["claims"][0]["claim_id"], claim["claim_id"])
        result = self.query(keys=[self.record["key"]], view="history", max_bytes=2800)
        self.assertEqual(len(result["matched"][0]["episodes"]), 1)
        self.assertEqual(result["matched"][0]["claims"], [])

    def test_replay_checks_both_occurrence_and_ready_time_without_falling_back(self):
        publication = "EN-MEMORY-" + "A" * 24
        with patch("english_pipeline.memory_store.utc_now", return_value="2026-09-02T10:00:00Z"):
            snapshot = self.build()
        receipts = self.state / "memory/publications"
        receipts.mkdir()
        path = receipts / f"{publication}.json"
        receipt = {"publication_id": publication, "status": "ready", "pending_component": None,
                   "ready_at": "2026-09-03T10:00:00Z", "memory_generation": snapshot["generation"],
                   "summary_index_sha256": snapshot["summary_index_sha256"]}
        path.write_text(json.dumps(receipt))
        late_import = {**self.record["episodes"][0], "episode_id": "LATE-IMPORT", "study_date": "2026-08-30"}
        self.record["episodes"].append(late_import)
        self.build()
        kwargs = {"keys": [self.record["key"]], "view": "history", "as_of": "2026-09-10"}

        retrospective = self.query(**kwargs)
        self.assertIn("LATE-IMPORT", [e["episode_id"] for e in retrospective["matched"][0]["episodes"]])
        pinned = self.query(**kwargs, known_publication=publication)
        self.assertEqual(pinned["generation"], snapshot["generation"])
        self.assertEqual(pinned["coverage"]["publication_ready_at"], receipt["ready_at"])
        self.assertEqual([e["episode_id"] for e in pinned["matched"][0]["episodes"]], ["E-OLD"])
        kwargs["as_of"] = "2026-09-02"
        early = self.query(**kwargs, known_publication=publication)
        self.assertEqual(early["status"], "unavailable")
        self.assertFalse(early["matched"])
        kwargs["as_of"] = "2026-09-10"
        receipt.pop("ready_at")
        path.write_text(json.dumps(receipt))
        self.assertFalse(self.query(**kwargs, known_publication=publication)["matched"])
        receipt["ready_at"] = "2026-09-03T10:00:00Z"
        path.write_text(json.dumps(receipt))
        self.lifecycle(invalidated_episode_ids=["E-OLD"])
        self.assertFalse(self.query(**kwargs, known_publication=publication)["matched"])
        self.lifecycle()
        database = Path(snapshot["database_path"])
        with database.open("ab") as stream:
            stream.write(b"unexpected data")
        damaged = self.query(**kwargs, known_publication=publication)
        self.assertEqual(damaged["unavailable"], [{"reason": "historical_generation_unavailable"}])
        self.assertFalse(damaged["matched"])

    def test_protected_never_retrieved_by_natural_or_external_candidates(self):
        self.record["visibility"] = "protected"
        self.record["teaching_hint"] = "正确答案是B。PRIVATE_ONLY"
        self.record["episodes"][0]["first_attempt"] = "PRIVATE_ONLY 原始作答"
        self.record["episodes"][0]["question_id"] = "Q21"
        self.build()
        for kwargs in ({"query": "PRIVATE_ONLY"}, {"query": "同位语", "candidate_keys": [self.record["key"]]},
                       {"keys": [self.record["key"]]}, {"keys": [self.record["key"]], "question_id": "Q22", "unlocked_question_ids": ["Q22"]}):
            result = self.query(**kwargs)
            self.assertNotIn("PRIVATE_ONLY", json.dumps(result, ensure_ascii=False))
        missing_source = self.query(keys=[self.record["key"]], question_id="Q21", unlocked_question_ids=["Q21"])
        self.assertNotIn("PRIVATE_ONLY", json.dumps(missing_source, ensure_ascii=False))
        allowed = self.query(keys=[self.record["key"]], source_id="ARTICLE-A", question_id="Q21", unlocked_question_ids=["Q21"])
        self.assertIn("PRIVATE_ONLY", allowed["matched"][0]["latest_observation"]["first_attempt"])

    def test_legacy_boolean_unlock_cannot_bypass_scoped_reader_after_activation(self):
        self.record["visibility"] = "protected"
        self.record["teaching_hint"] = "PRIVATE_LEGACY"
        self.build()
        (self.state / "memory/enabled.json").write_text(json.dumps({"enabled": True}))
        legacy = query_personal_summary(self.state, keys=[self.record["key"]], question_review=True)
        self.assertFalse(legacy["matched"])
        self.assertNotIn("PRIVATE_LEGACY", json.dumps(legacy))

    def test_same_question_number_in_two_articles_requires_exact_source(self):
        self.record["visibility"] = "protected"
        self.record["episodes"][0].update(question_id="Q21", first_attempt="ARTICLE_A_PRIVATE")
        sibling = copy.deepcopy(self.record["episodes"][0])
        sibling.update(episode_id="OTHER-ARTICLE-Q21", source_id="ARTICLE-B", first_attempt="ARTICLE_B_PRIVATE")
        self.record["episodes"].append(sibling)
        self.build()
        missing = self.query(keys=[self.record["key"]], question_id="Q21", unlocked_question_ids=["Q21"], view="history")
        self.assertFalse(missing["matched"])
        precise = self.query(keys=[self.record["key"]], source_id="ARTICLE-A", question_id="Q21",
                             unlocked_question_ids=["Q21"], view="history")
        self.assertIn("ARTICLE_A_PRIVATE", json.dumps(precise))
        self.assertNotIn("ARTICLE_B_PRIVATE", json.dumps(precise))

    def test_denied_alias_or_episode_id_does_not_disclose_protected_point_name(self):
        self.record.update(key="option_type:correct-choice-private", aliases=["syntax:neutral"], visibility="protected")
        self.build()
        for request in ({"keys": ["syntax:neutral"]}, {"ids": ["E-OLD"]}):
            result = self.query(**request)
            self.assertFalse(result["matched"])
            self.assertNotIn("correct-choice-private", json.dumps(result))

    def test_unlock_does_not_expand_to_sibling_question(self):
        self.record["visibility"] = "protected"
        self.record["episodes"][0]["question_id"] = "Q21"
        sibling = copy.deepcopy(self.record["episodes"][0])
        sibling.update(episode_id="Q22-EVENT", question_id="Q22", first_attempt="SIBLING_PRIVATE")
        self.record["episodes"].append(sibling)
        self.record["teaching_hint"] = "该混合摘要也涵盖SIBLING_PRIVATE。"
        self.build()
        result = self.query(keys=[self.record["key"]], source_id="ARTICLE-A", question_id="Q21", unlocked_question_ids=["Q21"], view="history")
        self.assertNotIn("SIBLING_PRIVATE", json.dumps(result, ensure_ascii=False))
        self.assertEqual(len(result["matched"][0]["episodes"]), 1)

    def test_pending_global_and_invalidated_evidence_suppress_old_hint(self):
        made = self.build()
        self.lifecycle(pending_keys={self.record["key"]: {"reason": "correction"}})
        result = self.query(query="同位语")
        self.assertEqual(result["status"], "pending")
        self.assertNotIn(self.record["teaching_hint"], json.dumps(result, ensure_ascii=False))
        self.lifecycle(global_pending=True)
        self.assertEqual(self.query(keys=[self.record["key"]])["status"], "pending")
        self.lifecycle(invalidated_episode_ids=["E-OLD"])
        result = self.query(keys=[self.record["key"]])
        self.assertNotIn("首答", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["status"], "no_evidence")

    def test_accepted_new_hint_keeps_invalidated_old_episode_out(self):
        later = copy.deepcopy(self.record["episodes"][0])
        later.update(episode_id="E-NEW", study_date="2026-10-02", first_attempt="新观察")
        self.record["episodes"].append(later)
        self.build()
        digest = self.query(keys=[self.record["key"]])["matched"][0]["summary_sha256"]
        self.lifecycle(invalidated_episode_ids=["E-OLD"], accepted_summary_hashes={self.record["key"]: digest})
        result = self.query(keys=[self.record["key"]], view="history")
        self.assertEqual([e["episode_id"] for e in result["matched"][0]["episodes"]], ["E-NEW"])
        self.assertEqual(result["matched"][0]["teaching_hint"], self.record["teaching_hint"])

    def test_summary_changed_without_projection_reports_stale(self):
        self.build()
        newer = copy.deepcopy(self.record)
        newer["teaching_hint"] = "新版摘要尚未建立投影。"
        self.publish(newer)
        result = self.query(keys=[self.record["key"]])
        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["matched"])

    def test_claims_require_active_source_and_current_status(self):
        self.build()
        claims = {"good": {"claim_id": "good", "point_key": self.record["key"], "content": "有来源的暂定教学判断。", "basis": "inference",
                           "status": "candidate", "evidence_episode_ids": ["E-OLD"], "valid_from": "2026-09-01"}}
        for name, override in (("wrong-evidence", {"evidence_episode_ids": ["UNKNOWN"]}),
                               ("retracted", {"status": "retracted"}), ("expired", {"valid_to": "2026-09-02"})):
            claims[name] = {**claims["good"], "claim_id": name, **override}
        (self.state / "memory/claims.json").write_text(json.dumps({"claims": claims}))
        result = self.query(keys=[self.record["key"]])
        self.assertEqual([c["claim_id"] for c in result["matched"][0]["claims"]], ["good"])
        self.assertEqual(result["matched"][0]["claims"][0]["status"], "candidate")

    def test_claims_load_once_per_request_only_for_eligible_points(self):
        records = []
        for i in range(8):
            key = f"word:w{i}"
            self.raw_publish({
                "key": key, "visibility": "answer_free", "teaching_hint": "句义",
                "episodes": [{"episode_id": f"E{i}", "study_date": "2026-09-01",
                              "correction": "辨义", "evidence": [{"path": "articles/record.md",
                              "sha256": self.record["episodes"][0]["evidence"][0]["sha256"], "locator": "lines/1-2"}]}],
            })
            records.append(key)
        build_memory_projection(self.repo, self.state)
        # The reader resolves the state root before opening claims.json.
        path = (self.state / "memory/claims.json").resolve()

        def save(text):
            claims = {
                str(i): {"claim_id": str(i), "point_key": key, "status": "confirmed", "content": text,
                         "evidence_episode_ids": [f"E{i}"]}
                for i, key in enumerate(records)
            }
            path.write_bytes(canonical_bytes({"claims": claims}))

        save("old")
        for size in (1, 3, 8):
            with self.subTest(size=size), patch.object(memory_query, "load_json", wraps=memory_query.load_json) as read:
                result = self.query(keys=records[:size], max_bytes=6144)
                reads = [call for call in read.call_args_list if call.args == (path,)]
                self.assertEqual(len(reads), 1)
                self.assertEqual(result["coverage"]["points_in_scope"], size)
                returned = len(result["matched"])
                if size == 8:
                    self.assertEqual(result["status"], "partial")
                    self.assertTrue(result["coverage"]["budget_limited"])
                else:
                    self.assertEqual(returned, size)
                self.assertGreater(returned, 0)
                self.assertEqual([p["claims"][0]["claim_id"] for p in result["matched"]], [str(i) for i in range(returned)])
                self.assertLessEqual(len(canonical_bytes(result)), 6144)

        with patch.object(memory_query, "load_json", wraps=memory_query.load_json) as read:
            self.assertFalse(self.query(keys=["word:unknown"])["matched"])
            self.assertFalse(self.query(keys=records[:1], source_id="OTHER-ARTICLE")["matched"])
            self.assertFalse(any(call.args == (path,) for call in read.call_args_list))

        original = memory_query._claims

        def replace_after_read(*args, **kwargs):
            if args[1] == records[0]:
                save("new")
            return original(*args, **kwargs)

        with patch.object(memory_query, "_claims", side_effect=replace_after_read):
            raced = self.query(keys=records[:3])
        self.assertEqual(raced["matched"], [])
        self.assertEqual(raced["pending"], [{"reason": "memory_state_changed_during_query"}])
        self.assertEqual(self.query(keys=records[:1])["matched"][0]["claims"][0]["content"], "new")

    def test_whole_envelope_budget_and_complete_observations(self):
        self.record["episodes"][0]["later_observation"] = "提示后复述；尚未独立验证。" * 80
        self.build()
        for budget in (512, 1024, 2048, 6144, 16384):
            result = self.query(keys=[self.record["key"]], view="history", max_bytes=budget)
            self.assertLessEqual(len(canonical_bytes(result)), budget)
            for point in result["matched"]:
                for episode in point.get("episodes", []):
                    self.assertEqual(episode["later_observation"], self.record["episodes"][0]["later_observation"])

    def test_missing_projection_falls_back_without_writes(self):
        self.publish()
        before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        result = self.query(keys=[self.record["key"]])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["matched"][0]["key"], self.record["key"])
        self.assertFalse((self.state / "memory").exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()})

    def test_legacy_fallback_discards_observations_if_retracted_during_read(self):
        from english_pipeline import memory_query

        self.publish()
        key = self.record["key"]
        original = memory_query._read_record

        def retract(*args, **kwargs):
            self.lifecycle(invalidated_episode_ids=["E-OLD"])
            return original(*args, **kwargs)

        with patch.object(memory_query, "_read_record", side_effect=retract):
            result = self.query(keys=[key])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["matched"], [])
        self.assertIn({"reason": "memory_state_changed_during_query"}, result["pending"])
        self.assertFalse(self.query(keys=[key])["matched"])

    def test_pinned_history_and_corrupt_projection_respect_current_source_audit(self):
        other_source = self.source.with_name("other.md")
        other_source.write_text("Independent evidence.\n", encoding="utf-8")
        other = copy.deepcopy(self.record)
        other.update(key="syntax:contrast", aliases=[])
        other["episodes"][0]["evidence"] = [{"path": "articles/other.md", "sha256": file_sha256(other_source),
                                             "locator": "lines/1-1"}]
        self.raw_publish(other)
        with patch("english_pipeline.memory_store.utc_now", return_value="2026-09-02T10:00:00Z"):
            snapshot = self.build()
        publication = "EN-MEMORY-" + "B" * 24
        receipts = self.state / "memory/publications"
        receipts.mkdir()
        (receipts / f"{publication}.json").write_bytes(canonical_bytes({
            "publication_id": publication, "status": "ready", "pending_component": None,
            "ready_at": "2026-09-03T10:00:00Z", "memory_generation": snapshot["generation"],
            "summary_index_sha256": snapshot["summary_index_sha256"],
        }))
        keys = [self.record["key"], other["key"]]
        request = {"keys": keys, "view": "history", "as_of": "2026-09-10", "known_publication": publication}
        self.assertEqual([point["key"] for point in self.query(**request)["matched"]], keys)

        self.source.write_text("A different version.\n", encoding="utf-8")
        protected_hash = file_sha256(self.source)
        impacted = source_dependents(self.repo, self.state, [self.source])
        self.assertEqual(impacted, keys[:1])
        rebuilt = build_memory_projection(self.repo, self.state, keys=impacted)
        self.assertEqual(rebuilt["updated_keys"], keys[:1])
        self.assertEqual(file_sha256(self.source), protected_hash)
        self.assertEqual(file_sha256(other_source), other["episodes"][0]["evidence"][0]["sha256"])

        history = self.query(**request)
        self.assertEqual([point["key"] for point in history["matched"]], keys[1:])
        self.assertIn({"key": keys[0], "reason": "historical_evidence_no_longer_verified"}, history["unavailable"])
        self.assertEqual(self.query(keys=keys[:1])["status"], "conflict")

        Path(rebuilt["database_path"]).write_bytes(b"corrupt index fixture")
        for request in ({"keys": keys[:1]}, request):
            result = self.query(**request)
            self.assertEqual(result["unavailable"], [{"reason": "published_source_audit_unavailable"}])
            self.assertFalse(result["matched"])
        (self.state / "memory/current.json").unlink()
        self.assertFalse(self.query(keys=keys[:1])["matched"])
        (self.state / "memory/enabled.json").write_bytes(canonical_bytes({"enabled": True}))
        self.assertFalse(query_personal_summary(self.state, keys=keys[:1], view="teaching")["matched"])

    def test_pinned_history_applies_current_answer_protection(self):
        with patch("english_pipeline.memory_store.utc_now", return_value="2026-09-02T10:00:00Z"):
            snapshot = self.build()
        publication = "EN-MEMORY-" + "C" * 24
        receipts = self.state / "memory/publications"
        receipts.mkdir()
        (receipts / f"{publication}.json").write_bytes(canonical_bytes({
            "publication_id": publication, "status": "ready", "pending_component": None,
            "ready_at": "2026-09-03T10:00:00Z", "memory_generation": snapshot["generation"],
            "summary_index_sha256": snapshot["summary_index_sha256"],
        }))
        request = {"keys": [self.record["key"]], "view": "history", "as_of": "2026-09-10",
                   "known_publication": publication}
        self.assertTrue(self.query(**request)["matched"])

        self.publish({**self.record, "visibility": "protected"})
        build_memory_projection(self.repo, self.state)
        self.assertFalse(self.query(**request)["matched"])
        self.assertFalse(self.query(**request, question_id="Q21", unlocked_question_ids=["Q21"])["matched"])

    def test_query_does_not_read_original_sources(self):
        self.build()
        self.source.unlink()
        with patch("english_pipeline.memory_store._SourceAudit._file", side_effect=AssertionError("query reopened source")):
            result = self.query(keys=[self.record["key"]])
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["coverage"]["original_sources_reopened"])
        self.assertEqual(result["matched"][0]["sources"][0]["verification"], "verified_at_build")
        expected = "not_reopened" if self.source.resolve().as_posix().startswith("/Volumes/") else "source_unavailable_now"
        self.assertEqual(result["matched"][0]["sources"][0]["current_access"], expected)

    def test_preference_id_is_not_mastery_and_fits_whole_sections(self):
        path = self.state / "personal-summary/teaching-preferences.md"
        path.parent.mkdir(parents=True)
        path.write_text("# 偏好\n\n## 图解\n\n使用双语图。\n\n## 答案公开\n\n逐题明确解锁。\n")
        result = self.query(ids=["teaching-preferences"])
        self.assertEqual(result["matched"][0]["role"], "explicit_preference")
        self.assertTrue(result["matched"][0]["not_mastery_evidence"])
        self.assertIn("逐题明确解锁", result["matched"][0]["content"])

    def test_invalid_unlock_shape_and_unknown_as_of_fail(self):
        self.build()
        with self.assertRaises(ValidationError):
            self.query(keys=[self.record["key"]], unlocked_question_ids="Q21")
        with self.assertRaises(ValueError):
            self.query(keys=[self.record["key"]], as_of="not-a-date")

    def test_concurrent_invalidation_does_not_return_old_claims(self):
        self.build()
        from english_pipeline import memory_query
        original = memory_query._claims
        def changed(*args, **kwargs):
            self.lifecycle(global_pending=True)
            return original(*args, **kwargs)
        with patch.object(memory_query, "_claims", side_effect=changed):
            result = self.query(keys=[self.record["key"]])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["matched"], [])
        self.assertEqual(result["pending"][-1]["reason"], "memory_state_changed_during_query")

    def test_no_source_audit_does_not_claim_verification(self):
        self.publish()
        build_memory_projection(self.repo, self.state, audit_sources=False)
        result = self.query(keys=[self.record["key"]])
        self.assertFalse(result["coverage"]["source_integrity_checked_at_build"])
        self.assertEqual(result["matched"][0]["sources"][0]["verification"], "audit_at_build")

    def test_claim_only_retraction_invalidates_old_hint_until_revised_hash(self):
        self.build()
        digest = self.query(keys=[self.record["key"]])["matched"][0]["summary_sha256"]
        self.lifecycle(invalidated_summary_keys=[self.record["key"]], invalidated_claim_ids=["CLAIM-1"])
        result = self.query(keys=[self.record["key"]])
        self.assertEqual(result["status"], "pending")
        self.assertNotIn(self.record["teaching_hint"], json.dumps(result, ensure_ascii=False))
        self.lifecycle(invalidated_summary_keys=[self.record["key"]], accepted_summary_hashes={self.record["key"]: digest})
        self.assertEqual(self.query(keys=[self.record["key"]])["status"], "ready")

    def test_historical_followup_does_not_travel_back_to_first_attempt(self):
        self.record["episodes"][0].update(observed_at="2026-09-01T10:00:00+08:00", last_observed_at="2026-10-20T10:00:00+08:00",
            later_observation="FUTURE_PROOF", next_action="FUTURE_NEXT")
        self.build()
        result = self.query(keys=[self.record["key"]], as_of="2026-09-15", view="history")
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("FUTURE_PROOF", text)
        self.assertNotIn("FUTURE_NEXT", text)
        self.assertNotIn("hint_dependence", result["matched"][0]["episodes"][0])
        self.assertIn("later_observation_after_cutoff", result["matched"][0]["episodes"][0]["temporal_limitations"])

    def test_unknown_followup_time_is_not_assumed_before_historical_cutoff(self):
        self.build()
        result = self.query(keys=[self.record["key"]], as_of="2026-09-15", view="history")
        event = result["matched"][0]["episodes"][0]
        self.assertNotIn("later_observation", event)
        self.assertIn("later_observation_time_unknown", event["temporal_limitations"])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["coverage"]["temporal_coverage_limited"])
        self.assertEqual(result["coverage"]["temporal_fields_omitted"]["later_observation"], 1)

    def test_missing_projection_fails_closed_for_ancestor_and_symlink(self):
        parent = copy.deepcopy(self.record)
        parent["visibility"] = "protected"
        digest = self.raw_publish(parent)
        alias = self.repo / "articles/parent.json"
        alias.symlink_to(self.state / "personal-summary/objects" / f"{digest}.json")
        child = copy.deepcopy(self.record)
        child.update(key="syntax:child", aliases=[], teaching_hint="PRIVATE_PARENT")
        child["episodes"][0].update(episode_id="CHILD", source_episode_id="E-OLD",
            evidence=[{"path": "articles/parent.json", "sha256": digest, "locator": "episodes/0"}])
        self.raw_publish(child)
        result = self.query(keys=[child["key"]])
        self.assertNotIn("PRIVATE_PARENT", json.dumps(result))
        self.assertEqual(result["unavailable"][0]["reason"], "inherited_visibility_unverified")

    def test_source_unit_union_cannot_create_a_pair_or_split_composite_observation(self):
        from english_pipeline.util import file_sha256
        refs = []
        for name, source_id, unit_id in (("a", "A", "S01"), ("b", "B", "S02")):
            path = self.repo / f"{name}.json"
            path.write_text(json.dumps({"source_id": source_id, "unit_id": unit_id}))
            refs.append({"path": path.name, "sha256": file_sha256(path), "locator": "/source_id"})
        self.record["episodes"][0].pop("source_id")
        self.record["episodes"][0].pop("unit_id")
        self.record["episodes"][0]["evidence"] = refs
        self.raw_publish(self.record)
        build_memory_projection(self.repo, self.state)
        for source_id, unit_id in (("A", "S02"), ("A", "S01")):
            exact = self.query(keys=[self.record["key"]], source_id=source_id, unit_id=unit_id)
            self.assertEqual(exact["status"], "no_evidence")
            natural = self.query(query="同位语", source_id=source_id, unit_id=unit_id)
            self.assertEqual(natural["matched"], [])

    def test_historical_filter_precedes_fts_candidate_limit(self):
        for index in range(49):
            record = copy.deepcopy(self.record)
            record.update(key=f"syntax:future-{index:02d}", aliases=[], teaching_hint="needle 同位语")
            record["episodes"][0].update(episode_id=f"F-{index}", study_date="2027-01-01")
            self.raw_publish(record)
        historical = copy.deepcopy(self.record)
        historical.update(key="syntax:zz-historical", aliases=[], teaching_hint="needle 同位语")
        self.raw_publish(historical)
        build_memory_projection(self.repo, self.state)
        result = self.query(query="needle", as_of="2026-09-15", view="history")
        self.assertEqual([point["key"] for point in result["matched"]], [historical["key"]])

    def test_current_preference_is_not_historical_or_article_scoped_evidence(self):
        path = self.state / "personal-summary/teaching-preferences.md"
        path.parent.mkdir(parents=True)
        path.write_text("# CURRENT_ONLY_PREFERENCE")
        for options, reason in (({"as_of": "2026-01-01"}, "preference_history_unavailable"),
                                ({"source_id": "ARTICLE-A"}, "preference_scope_unavailable")):
            result = self.query(ids=["teaching-preferences"], **options)
            self.assertEqual(result["unavailable"][0]["reason"], reason)
            self.assertNotIn("CURRENT_ONLY_PREFERENCE", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
