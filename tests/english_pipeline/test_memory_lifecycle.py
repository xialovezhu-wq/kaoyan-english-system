import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_pipeline.errors import IdempotencyConflict, ValidationError
from english_pipeline.memory_lifecycle import (mark_formal_memory_pending, memory_closeout_status,
    read_lifecycle, recover_memory_closeout)
from english_pipeline.personal_summary import SCHEMA, publish_personal_summaries, query_personal_summary
from english_pipeline.util import atomic_write_json, file_sha256, object_sha256


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.state = self.repo / "intake"
        self.source = self.repo / "articles/session.json"
        atomic_write_json(self.source, {"messages": [
            {"role": "user", "content": "我第一次把 advertise 和 advise 混淆了。"},
            {"role": "user", "content": "这是转写错误，我实际说的是 advertise。"}]})
        self.record = {"key": "word:advertise", "aliases": [], "visibility": "answer_free",
            "teaching_hint": "当时读成 advise，需要先辨认词形。", "episodes": [self.episode("e1", 0)]}
        publish_personal_summaries(self.repo, self.state, {"schema_version": SCHEMA, "records": [self.record]}, bootstrap_existing_formal=True)
        atomic_write_json(self.state / "memory/enabled.json", {"enabled": True})
        self.builders = patch("english_pipeline.memory_store.build_memory_projection", create=True,
            return_value={"status": "ready", "generation": "generation-1"})
        self.exporter = patch("english_pipeline.memory_store.export_safe_mirror", create=True,
            return_value={"status": "ready", "path": "fixture-only"})
        self.pages = patch("english_pipeline.memory_store.publish_managed_pages", create=True,
            return_value={"status": "published"})
        # During parallel implementation these modules may not yet be present;
        # tests run once the core module exists.
        self.builder = self.builders.start()
        self.exporter.start()
        self.pages.start()
        self.addCleanup(self.builders.stop)
        self.addCleanup(self.exporter.stop)
        self.addCleanup(self.pages.stop)

    def episode(self, eid, message, date="2026-11-01"):
        return {"episode_id": eid, "study_date": date,
            "first_attempt": "用户原始表述", "correction": "按原话核实", "later_observation": "未观察独立复测",
            "hint_dependence": "not_observed", "evidence": [{"path": "articles/session.json",
                "sha256": file_sha256(self.source), "locator": f"/messages/{message}"}]}

    def review(self, document):
        return {"schema_version": "english_summary_source_review_v1", "purpose": "explicit_summary_maintenance",
            "summary_input_sha256": object_sha256(document), "sources": [{"path": "articles/session.json",
                "sha256": file_sha256(self.source), "kind": "learner_dialogue",
                "locators": ["/messages/0", "/messages/1"], "study_dates": ["2026-11-01", "2026-11-02"]}]}

    def publish_review(self, document):
        return publish_personal_summaries(self.repo, self.state, document, source_review=self.review(document))

    def writer(self, *, partial=False, date="2026-11-06T00:00:00Z"):
        pid = "EN-PKG-20261106-AAAAAAAAAAAAAAAA"
        second = "EN-PKG-20261106-BBBBBBBBBBBBBBBB"
        receipt = {"schema_version": "english_apply_receipt_v1", "receipt_id": "EN-RECEIPT-AAAAAAAAAAAAAAAA",
            "batch_id": "EN-BATCH-20261106-AAAAAAAAAAAA", "mode": "apply", "status": "PARTIAL" if partial else "APPLIED",
            "completed_at": date, "package_ids": [pid, second] if partial else [pid],
            "package_sha256s": ["a" * 64, "b" * 64] if partial else ["a" * 64],
            "package_dispositions": {pid: "completed", **({second: "needs_user"} if partial else {})},
            "formal_posthashes": {}, "formal_files_changed": False, "action_results": []}
        path = self.state / "receipts/nightly/2026-11-06" / f"{receipt['receipt_id']}.json"
        atomic_write_json(path, receipt)
        verified = {"schema_version": "english_formal_receipt_validation_v1", "status": "PASS", "subject": "english",
            "receipt_id": receipt["receipt_id"], "writer_receipt_sha256": file_sha256(path),
            "writer_receipt_path": str(path), "package_ids": receipt["package_ids"],
            "package_sha256s": receipt["package_sha256s"], "package_dispositions": receipt["package_dispositions"]}
        return receipt, verified

    def noop(self, verified):
        return {"schema_version": SCHEMA, "records": [], "coverage": [{
            "package_id": verified["package_ids"][0], "disposition": "no_relevant_points", "keys": [],
            "reason": "完整核读后未发现需要新增或修订的个人教学观察。"}]}

    def test_new_formal_commit_masks_old_summary_until_explicit_coverage(self):
        raw, verified = self.writer()
        mark_formal_memory_pending(self.repo, self.state, raw)
        read = query_personal_summary(self.state, keys=["word:advertise"], view="teaching")
        self.assertEqual(read["matched"], [])
        self.assertEqual(read["unavailable"][0]["reason"], "memory_update_pending")
        result = publish_personal_summaries(self.repo, self.state, self.noop(verified), formal_receipt=verified)
        self.assertEqual(result["status"], "published")
        self.assertEqual(memory_closeout_status(self.state, raw["receipt_id"])["status"], "ready")
        self.assertEqual(len(query_personal_summary(self.state, keys=["word:advertise"])["matched"]), 1)

    def test_legacy_receipt_is_not_reopened_when_layer_activates(self):
        atomic_write_json(self.state / "memory/enabled.json", {"enabled": True, "activated_at": "2026-11-06T01:00:00Z"})
        raw, _ = self.writer()
        self.assertEqual(mark_formal_memory_pending(self.repo, self.state, raw)["status"], "LEGACY_NOT_REQUIRED")
        self.assertFalse((self.state / "memory/lifecycle.json").exists())

    def test_coverage_cannot_close_a_needs_user_package(self):
        raw, verified = self.writer(partial=True)
        mark_formal_memory_pending(self.repo, self.state, raw)
        document = self.noop(verified)
        document["coverage"][0]["package_id"] = verified["package_ids"][1]
        before = (self.state / "personal-summary/current.json").read_bytes()
        with self.assertRaisesRegex(ValidationError, "completed writer package"):
            publish_personal_summaries(self.repo, self.state, document, formal_receipt=verified)
        self.assertEqual(before, (self.state / "personal-summary/current.json").read_bytes())
        publish_personal_summaries(self.repo, self.state, self.noop(verified), formal_receipt=verified)
        self.assertEqual(memory_closeout_status(self.state, raw["receipt_id"])["covered_packages"], [verified["package_ids"][0]])

    def test_projection_failure_recovers_without_replaying_summary_or_writer(self):
        raw, verified = self.writer()
        mark_formal_memory_pending(self.repo, self.state, raw)
        self.builder.side_effect = OSError("index unavailable")
        result = publish_personal_summaries(self.repo, self.state, self.noop(verified), formal_receipt=verified)
        self.assertEqual(result["status"], "FORMAL_COMMITTED_MEMORY_PENDING")
        self.assertEqual(result["pending_component"], "memory_index")
        before = (self.state / "personal-summary/current.json").read_bytes()
        self.builder.side_effect = None
        with patch("english_pipeline.personal_summary.publish_personal_summaries", side_effect=AssertionError("summary replay")), \
                patch("english_pipeline.writer.apply_nightly", create=True, side_effect=AssertionError("formal replay")):
            recovered = recover_memory_closeout(self.repo, self.state, raw["receipt_id"])
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(recovered["formal_apply_invocation_count"], 0)
        self.assertEqual(before, (self.state / "personal-summary/current.json").read_bytes())

    def test_claims_require_real_learner_evidence_and_inference_stays_candidate(self):
        claim = {"claim_id": "claim:advertise", "point_key": "word:advertise", "content": "可能需要核对词形。",
            "basis": "inference", "status": "confirmed", "evidence_episode_ids": ["e1"]}
        doc = {"schema_version": SCHEMA, "records": [self.record], "memory_claims": [claim]}
        with self.assertRaisesRegex(ValidationError, "inference"):
            self.publish_review(doc)
        claim["status"] = "candidate"
        self.assertEqual(self.publish_review(doc)["status"], "published")
        saved = json.loads((self.state / "memory/claims.json").read_text())["claims"]["claim:advertise"]
        self.assertEqual(saved["status"], "candidate")
        self.assertEqual(saved["basis"], "inference")

    def test_bootstrap_cannot_retract_an_episode(self):
        doc = {"schema_version": SCHEMA, "records": [self.record], "memory_corrections": [{
            "correction_id": "fix:1", "action": "retract", "target_episode_ids": ["e1"],
            "reason": "旧归因不成立", "evidence_episode_ids": ["e1"]}]}
        before = (self.state / "personal-summary/current.json").read_bytes()
        with self.assertRaisesRegex(ValidationError, "authority"):
            publish_personal_summaries(self.repo, self.state, doc, bootstrap_existing_formal=True)
        self.assertEqual(before, (self.state / "personal-summary/current.json").read_bytes())

    def test_shared_episode_retraction_invalidates_other_summary_hints(self):
        other = {**copy.deepcopy(self.record), "key": "error_type:word-shape", "teaching_hint": "旧归因。"}
        self.publish_review({"schema_version": SCHEMA, "records": [other]})
        revised = copy.deepcopy(self.record)
        revised["teaching_hint"] = "旧词形归因已撤回，尚无独立复测。"
        revised["episodes"].append(self.episode("e2", 1, "2026-11-02"))
        doc = {"schema_version": SCHEMA, "records": [revised], "memory_corrections": [{
            "correction_id": "fix:transcription", "action": "retract", "target_episode_ids": ["e1"],
            "reason": "原话证实是转写问题，撤回对这次词形误认的解释。", "evidence_episode_ids": ["e2"]}]}
        self.publish_review(doc)
        result = query_personal_summary(self.state, keys=["word:advertise", "error_type:word-shape"], view="teaching")
        self.assertEqual([x["key"] for x in result["matched"]], ["word:advertise"])
        self.assertEqual(result["unavailable"][0]["reason"], "summary_invalidated")
        self.assertEqual(read_lifecycle(self.state)["invalidated_episode_ids"], ["e1"])
        full = json.loads((self.state / "personal-summary/objects" / (read_lifecycle(self.state)["accepted_summary_hashes"]["word:advertise"] + ".json")).read_text())
        self.assertEqual([x["episode_id"] for x in full["episodes"]], ["e1", "e2"])

    def test_unknown_target_rejected_before_current_pointer_changes(self):
        doc = {"schema_version": SCHEMA, "records": [self.record], "memory_corrections": [{
            "correction_id": "fix:bad", "action": "retract", "target_claim_ids": ["claim:missing"],
            "reason": "invalid fixture", "evidence_episode_ids": ["e1"]}]}
        before = (self.state / "personal-summary/current.json").read_bytes()
        with self.assertRaisesRegex(ValidationError, "unknown"):
            self.publish_review(doc)
        self.assertEqual(before, (self.state / "personal-summary/current.json").read_bytes())

    def test_snapshot_published_does_not_claim_memory_ready(self):
        from english_pipeline.publication import complete_formal_closeout
        raw, _ = self.writer()
        result = complete_formal_closeout(self.repo, self.state, raw,
            runner=lambda **kwargs: {"status": "PUBLISHED", "publication_target": "local"})
        self.assertEqual(result["snapshot_status"], "PUBLISHED")
        self.assertEqual(result["status"], "FORMAL_COMMITTED_MEMORY_PENDING")
        self.assertEqual(result["pending_component"], "personal_summary")

    def test_pending_read_does_not_modify_lifecycle(self):
        raw, _ = self.writer()
        mark_formal_memory_pending(self.repo, self.state, raw)
        before = (self.state / "memory/lifecycle.json").read_bytes()
        query_personal_summary(self.state, keys=["word:advertise"])
        memory_closeout_status(self.state)
        self.assertEqual(before, (self.state / "memory/lifecycle.json").read_bytes())

    def test_pending_page_failure_keeps_real_state_gated_until_recovery(self):
        raw, verified = self.writer()
        mark_formal_memory_pending(self.repo, self.state, raw)
        with patch("english_pipeline.memory_store.publish_managed_pages", return_value={"status": "partial", "conflicts": ["user-edited-block"]}):
            result = publish_personal_summaries(self.repo, self.state, self.noop(verified), formal_receipt=verified)
        self.assertEqual(result["pending_component"], "memory_index")
        self.assertEqual(memory_closeout_status(self.state, raw["receipt_id"])["status"], "pending")
        self.assertEqual(recover_memory_closeout(self.repo, self.state, raw["receipt_id"])["status"], "ready")

    def test_conflicting_revision_waits_for_existing_publication_recovery(self):
        self.builder.side_effect = OSError("index unavailable")
        first = {"schema_version": SCHEMA, "records": [self.record]}
        self.assertEqual(self.publish_review(first)["pending_component"], "memory_index")
        changed = copy.deepcopy(self.record)
        changed["teaching_hint"] = "新的受审阅表述。"
        with self.assertRaisesRegex(ValidationError, "recover the pending"):
            self.publish_review({"schema_version": SCHEMA, "records": [changed]})
        self.builder.side_effect = None
        self.assertEqual(recover_memory_closeout(self.repo, self.state)["status"], "ready")

    def test_cyclic_claim_replacement_is_rejected_atomically(self):
        claims = [{"claim_id": cid, "point_key": "word:advertise", "content": content,
            "basis": "observed", "status": "confirmed", "evidence_episode_ids": ["e1"]}
            for cid, content in (("claim:a", "观察A"), ("claim:b", "观察B"))]
        self.publish_review({"schema_version": SCHEMA, "records": [self.record], "memory_claims": claims})
        before = (self.state / "personal-summary/current.json").read_bytes()
        corrections = [{"correction_id": "fix:" + first, "action": "supersede", "target_claim_ids": ["claim:" + first],
            "replacement_claim_id": "claim:" + second, "reason": "测试替代关系", "evidence_episode_ids": ["e1"]}
            for first, second in (("a", "b"), ("b", "a"))]
        with self.assertRaisesRegex(ValidationError, "cyclic"):
            self.publish_review({"schema_version": SCHEMA, "records": [self.record], "memory_corrections": corrections})
        self.assertEqual(before, (self.state / "personal-summary/current.json").read_bytes())


    def test_candidate_cannot_supersede_confirmed_claim_or_its_source_episode(self):
        from english_pipeline.memory_query import _claims

        first = copy.deepcopy(self.record)
        first["episodes"].append({**self.episode("child", 0), "source_episode_id": "e1"})
        old = {"claim_id": "claim:old", "point_key": first["key"], "content": "Confirmed observation",
               "basis": "observed", "status": "confirmed", "evidence_episode_ids": ["child"]}
        self.publish_review({"schema_version": SCHEMA, "records": [first], "memory_claims": [old]})
        revised = copy.deepcopy(first)
        revised["episodes"].append(self.episode("e2", 1, "2026-11-02"))
        candidate = {**old, "claim_id": "claim:pending", "content": "Candidate interpretation",
                     "basis": "inference", "status": "candidate", "evidence_episode_ids": ["e2"]}
        candidate_doc = {"schema_version": SCHEMA, "records": [revised], "memory_claims": [candidate]}
        claims_path = self.state / "memory/claims.json"
        watched = [claims_path, self.state / "memory/lifecycle.json", self.state / "personal-summary/current.json"]
        before = [p.read_bytes() for p in watched]
        withdrawal = {"correction_id": "fix:withdraw", "action": "retract", "target_episode_ids": ["e1"],
                      "reason": "Verified independent grounds for withdrawal", "evidence_episode_ids": ["e2"]}

        for target in ({"target_claim_ids": ["claim:old"]}, {"target_episode_ids": ["e1"]}):
            correction = {"correction_id": "fix:replace", "action": "supersede", **target,
                          "replacement_claim_id": "claim:pending", "reason": "Synthetic boundary",
                          "evidence_episode_ids": ["e2"]}
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValidationError, "confirmed non-inference replacement"):
                    self.publish_review({**candidate_doc, "memory_corrections": [correction]})
                self.assertEqual([p.read_bytes() for p in watched], before)

        self.publish_review(candidate_doc)

        def visible():
            rows = json.loads(claims_path.read_bytes())["claims"]
            life = read_lifecycle(self.state)
            return {r["claim_id"]: r["status"] for r in _claims(
                rows, first["key"], {"e1", "child", "e2"}, life, None, True)}

        self.assertEqual(visible(), {"claim:old": "confirmed", "claim:pending": "candidate"})
        retract_doc = {"schema_version": SCHEMA, "records": [revised], "memory_corrections": [withdrawal]}
        self.publish_review(retract_doc)
        self.assertEqual(read_lifecycle(self.state)["invalidated_episode_ids"], ["child", "e1"])
        self.assertEqual(visible(), {"claim:pending": "candidate"})
        self.publish_review(retract_doc)
        self.publish_review(candidate_doc)
        self.assertEqual(visible(), {"claim:pending": "candidate"})

    def test_claim_retraction_also_blocks_the_old_unstructured_hint(self):
        claim = {"claim_id": "claim:old-attribution", "point_key": "word:advertise", "content": "旧词形归因",
            "basis": "observed", "status": "confirmed", "evidence_episode_ids": ["e1"]}
        self.publish_review({"schema_version": SCHEMA, "records": [self.record], "memory_claims": [claim]})
        other = {**copy.deepcopy(self.record), "key": "error_type:transcription", "teaching_hint": "转写原因已有原话说明。",
                 "episodes": [self.episode("e2", 1, "2026-11-02")]}
        self.publish_review({"schema_version": SCHEMA, "records": [other], "memory_corrections": [{
            "correction_id": "fix:claim-only", "action": "retract", "target_claim_ids": ["claim:old-attribution"],
            "reason": "撤回解释，不篡改原来的作答事件。", "evidence_episode_ids": ["e2"]}]})
        result = query_personal_summary(self.state, keys=["word:advertise"], view="teaching")
        self.assertEqual(result["matched"], [])
        self.assertEqual(result["unavailable"][0]["reason"], "summary_invalidated")
        self.assertEqual(read_lifecycle(self.state)["invalidated_episode_ids"], [])

    def test_replaying_completed_old_input_never_rewinds_a_newer_summary(self):
        first = {"schema_version": SCHEMA, "records": [copy.deepcopy(self.record)]}
        self.publish_review(first)
        newer = copy.deepcopy(self.record)
        newer["teaching_hint"] = "后续原话已纠正早先的归因。"
        newer["episodes"].append(self.episode("e2", 1, "2026-11-02"))
        self.publish_review({"schema_version": SCHEMA, "records": [newer]})
        before_index = (self.state / "personal-summary/current.json").read_bytes()
        before_lifecycle = (self.state / "memory/lifecycle.json").read_bytes()
        replay = self.publish_review(first)
        self.assertTrue(replay["idempotent_replay"])
        self.assertFalse(replay["current_pointer_rewritten"])
        self.assertEqual(before_index, (self.state / "personal-summary/current.json").read_bytes())
        self.assertEqual(before_lifecycle, (self.state / "memory/lifecycle.json").read_bytes())
        current = query_personal_summary(self.state, keys=["word:advertise"], view="teaching")
        self.assertEqual(current["matched"][0]["teaching_hint"], newer["teaching_hint"])

    def test_crash_persisting_transaction_leaves_a_durable_read_gate(self):
        from english_pipeline import memory_lifecycle
        document = {"schema_version": SCHEMA, "records": [self.record]}
        original = memory_lifecycle.atomic_write_json
        def crash(path, value):
            if path.parent.name == "publications":
                raise OSError("crash between gate and transaction")
            return original(path, value)
        before = (self.state / "personal-summary/current.json").read_bytes()
        with patch("english_pipeline.memory_lifecycle.atomic_write_json", side_effect=crash):
            with self.assertRaises(OSError):
                self.publish_review(document)
        self.assertEqual(before, (self.state / "personal-summary/current.json").read_bytes())
        self.assertEqual(query_personal_summary(self.state, keys=["word:advertise"])["matched"], [])
        self.assertEqual(recover_memory_closeout(self.repo, self.state)["status"], "pending")
        self.assertEqual(self.publish_review(document)["status"], "published")

    def test_old_episode_cannot_be_claimed_as_coverage_for_new_package(self):
        raw, verified = self.writer()
        mark_formal_memory_pending(self.repo, self.state, raw)
        doc = {"schema_version": SCHEMA, "records": [self.record], "coverage": [{
            "package_id": verified["package_ids"][0], "disposition": "covered", "keys": ["word:advertise"]}]}
        before = (self.state / "personal-summary/current.json").read_bytes()
        with self.assertRaisesRegex(ValidationError, "exact completed package"):
            publish_personal_summaries(self.repo, self.state, doc, formal_receipt=verified)
        self.assertEqual(before, (self.state / "personal-summary/current.json").read_bytes())
        self.assertTrue(read_lifecycle(self.state)["global_pending"])

    def test_recovery_restores_missing_gate_and_reports_incomplete_child(self):
        from english_pipeline.memory_lifecycle import preflight_summary_memory
        from english_pipeline.summary_source_review import SummarySourceReview
        document = {"schema_version": SCHEMA, "records": [self.record]}
        reviewed = SummarySourceReview(self.repo, self.review(document), document)
        plan = preflight_summary_memory(self.repo, self.state, document, {self.record["key"]: self.record}, review=reviewed)
        # Simulate the historical bad write order: intent exists but no gate.
        plan.update(status="summary_pending", pending_component="personal_summary", recorded_at="2026-11-06T00:00:00Z")
        atomic_write_json(self.state / "memory/publications" / (plan["publication_id"] + ".json"), plan)
        recovered = recover_memory_closeout(self.repo, self.state)
        self.assertEqual(recovered["status"], "pending")
        self.assertEqual(recovered["pending_component"], "personal_summary")
        self.assertEqual(query_personal_summary(self.state, keys=["word:advertise"])["matched"], [])
