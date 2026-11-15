import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_pipeline.errors import FormalModelReviewPending
from english_pipeline.formal_model_review import DIMENSIONS, _call, _guard_duplicate, _validated_vectors
from english_pipeline.util import load_json


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
