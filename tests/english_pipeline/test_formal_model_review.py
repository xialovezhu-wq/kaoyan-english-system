import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_pipeline.errors import FormalModelReviewPending
from english_pipeline.formal_model_review import (
    DIMENSIONS, _call, _candidate_pool, _coverage, _guard_duplicate, _guard_relation, _shortlists, _validated_vectors,
)
from english_pipeline.formal_review_evidence import message_ref, select_evidence
from english_pipeline.memory_store import search_tokens
from english_pipeline.util import load_json


class FormalComparisonBoundaryTests(unittest.TestCase):
    def unit(self, item, source, *, target=None, kind="skip_duplicate", bounded=True):
        return {"id": "unit-1", "kind": kind, "item": item, "formal_target": target,
                "excerpts": [{"text": source}], "original_sentences": {source} if bounded else set(),
                "verified_article_refs": {"fixture-article"} if bounded else set()}

    def corpus(self, *items):
        rows = [{"id": f"bank:{i:03}", "formal_id": f"{i:03}", "kind": "formal_bank",
                 "item": item, "text": text} for i, (item, text) in enumerate(items, 1)]
        return rows, [(row, set(search_tokens(row["text"]))) for row in rows]

    def test_explicit_id_survives_both_ranks_and_context_breaks_the_lexical_tie(self):
        phrases = ("charge a fee", "charge a battery", "charge at an enemy", "charge someone with an offence")
        rows, corpus = self.corpus(*[(p, p) for p in phrases])
        utterance = "They charged him with an offence."
        unit = self.unit("charge", utterance, target=" BANK:004 ")
        plan = _candidate_pool([unit], corpus)
        uid = unit["id"]
        lexical = plan[uid]["lexical_candidate_ids"]
        _shortlists([unit], plan, [uid, *lexical], [[1, 0]] * (1 + len(lexical)), {})
        report = _coverage(plan[uid], {}, {row["id"]: row for row in rows})
        self.assertEqual(report["formal_id"], "004")
        self.assertTrue(report["explicit_target_in_pool"])
        self.assertTrue(report["explicit_target_shortlisted"])
        self.assertEqual(report["shortlist_ids"][0], "bank:004")
        self.assertIn("no_verified_vector_cache", report["limits"])

        no_target = _candidate_pool([{**unit, "formal_target": None}], corpus)[uid]
        self.assertEqual(no_target["lexical_candidate_ids"][0], "bank:004")
        missing = _candidate_pool([{**unit, "formal_target": "bank:999"}], corpus)[uid]
        self.assertEqual(missing["target_status"], "missing")
        self.assertEqual(missing["shortlist_ids"], [])

    def test_cache_can_supply_a_nonlexical_candidate_and_new_respects_comparison_scope(self):
        rows, corpus = self.corpus(("forbid", "block access conditional on authorization"))
        source = "The policy prohibits entry without a permit."
        unit = self.unit("prohibit", source, kind="master_bank_insert", bounded=False)
        uid = unit["id"]
        decision = {"source_unit": uid, "candidate_id": None, "relation": "new"}
        plan = _candidate_pool([unit], corpus)
        self.assertEqual(plan[uid]["lexical_candidate_ids"], [])
        held = _guard_relation(decision, unit, None, plan[uid])
        self.assertEqual((held["relation"], held["reason"]), ("unresolved", "required_history_not_compared"))

        cache = {"bank:001": [1.0, 0.0]}
        _shortlists([unit], plan, [uid], [[1.0, 0.0]], cache)
        report = _coverage(plan[uid], cache, {row["id"]: row for row in rows})
        self.assertEqual(report["semantic_candidate_ids"], ["bank:001"])
        self.assertEqual(report["shortlist_ids"], ["bank:001"])
        self.assertEqual(report["compared_formal_count"], 0)

        bounded = self.unit("prohibit", source, kind="master_bank_insert")
        bounded_plan = _candidate_pool([bounded], corpus)[uid]
        self.assertEqual(bounded_plan["history_scope"], "exact_item_and_bound_source")
        self.assertEqual(_guard_relation(decision, bounded, None, bounded_plan)["relation"], "new")

    def test_short_correction_keeps_full_negation_condition_and_explicit_unread_bytes(self):
        source = "They charged him with an offence."
        correction = ("更正。 " + source + " Charge means accuse, not request payment. "
                      "Only when a court issues the charge does this qualification apply.")
        long_text = "General discussion. " * 102 + "\n\n" + correction + "\n\n" + "Additional notes. " * 55
        package = {"package_id": "fixture", "package_sha256": "a" * 64}
        original = {"role": "user", "sequence": 1, "created_at": "2026-11-16T10:00:00+08:00",
                    "content": "My first answer was request payment."}
        assistant = {"role": "assistant", "sequence": 2, "content": long_text}
        refs = [message_ref(package, 0, original), message_ref(package, 1, assistant)]
        excerpts, audit, problems = select_evidence(refs, ("charge", source.casefold()), 2200)
        self.assertFalse(problems)
        self.assertEqual(excerpts[0]["text"], original["content"])
        self.assertEqual([ex["sequence"] for ex in excerpts], [1, 2])
        self.assertIn(correction, excerpts[1]["text"])
        self.assertNotIn("General discussion.", excerpts[1]["text"])
        self.assertEqual(excerpts[1]["source_sha256"], refs[1]["binding"]["resolved_node_sha256"])
        left, right = excerpts[1]["utf8_range"]
        self.assertEqual(long_text.encode()[left:right].decode(), excerpts[1]["text"])
        self.assertEqual([part["utf8_range"] for part in audit[1]["unread"]],
                         [[0, left], [right, len(long_text.encode())]])

        excerpts, audit, problems = select_evidence(refs, ("charge",), 80)
        self.assertEqual(excerpts, [])
        self.assertIn("required_evidence_exceeds_byte_budget", problems)
        self.assertEqual(audit[1]["read_utf8_ranges"], [])


class FormalModelReviewTests(unittest.TestCase):
    def test_retry_reuses_an_embedding_request_that_already_completed(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            path = Path(root)
            source = {"units": ["synthetic-audio-free-language-source"]}
            budget = [5]
            def transport(endpoint, payload, trace):
                calls.append(endpoint)
                if endpoint == "decisions" and calls.count("decisions")==1:
                    error = RuntimeError("synthetic rate limit")
                    error.code = "STUDY_API_HTTP_429"
                    raise error
                return {"model": payload["model"], "value": 1}
            consume = lambda response: response["value"]
            with patch("english_pipeline.formal_model_review._request", side_effect=transport):
                self.assertEqual(_call(path, "embeddings", {"model": "fixture"},source,"test",budget,consume)["mode"],"live")
                with self.assertRaises(FormalModelReviewPending):
                    _call(path, "decisions", {"model": "fixture"},source,"test",budget,consume)
                self.assertEqual(_call(path, "embeddings", {"model": "fixture"},source,"test",budget,consume)["mode"],"cache")
                self.assertEqual(_call(path, "decisions", {"model": "fixture"},source,"test",budget,consume)["mode"],"live")
            ledger,=path.glob("decisions-*.json")
            self.assertEqual([r["status"] for r in load_json(ledger)["attempts"]],["blocked","complete"])
            self.assertEqual(calls, ["embeddings","decisions","decisions"])

    def test_duplicate_requires_exact_source_and_identity(self):
        unit = {"item": "dispute", "formal_target": "A", "verified_article_refs": {"synthetic-article"},
                "excerpts": [{"text":"The finding remains open to dispute."}]}
        decision = {"relation":"duplicate","candidate_id":"bank:A","source_unit":"unit-1"}
        row = {"item":"dispute","formal_id":"A","kind":"formal_bank","article":"synthetic-article",
               "_source_sentence":"The finding remains open to dispute."}
        self.assertEqual(_guard_duplicate(decision,unit,row),decision)
        other = {**row,"article":"second-article"}
        rejected = _guard_duplicate(decision,unit,other)
        self.assertEqual(rejected["relation"],"unresolved")
        self.assertEqual(rejected["model_relation"],"duplicate")

    def test_vector_dimensions_and_identity_are_checked(self):
        vector = [0.01]*DIMENSIONS
        self.assertEqual(_validated_vectors({"data":[{"index":0,"embedding":vector}]},1),[vector])
        with self.assertRaises(ValueError):
            _validated_vectors({"data":[{"index":2,"embedding":vector}]},1)
