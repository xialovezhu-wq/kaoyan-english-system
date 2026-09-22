"""Read-only tutoring queries against isolated source and formal fixtures."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from english_pipeline.constants import MASTER_HEADER, MASTERED_HEADER
from english_pipeline.formal import serialize_csv
from english_pipeline.learning_state import query_learning_context
from english_pipeline.sources import resolve_article_source


class LearningStateSourceBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.state = self.repo / "intake"
        (self.repo / "bank").mkdir()
        self.row = dict.fromkeys(MASTER_HEADER, "")
        self.row.update(id="20260901-001", item="dispute", meaning="争执")
        self.write_bank()
        (self.repo / "bank/mastered_items.csv").write_bytes(serialize_csv([], MASTERED_HEADER))
        self.pattern = self.repo / "bank/sentence_patterns.md"
        self.pattern.write_text("## SP-001｜open to\n原解释\n", encoding="utf-8")
        self.article = self.repo / "articles/2026-04-24-article.md"
        self.article.parent.mkdir()
        self.article.write_text(
            "# Article\ndate: 2026-04-24\nsource: fixture\n\nThe finding is open to dispute.\n"
            "### 2026-09-01｜S01\n首次翻译：这个发现仍然开放。\n", encoding="utf-8")
        self.binding = resolve_article_source(self.repo, self.article.relative_to(self.repo).as_posix())

    def write_bank(self):
        (self.repo / "bank/master_bank.csv").write_bytes(serialize_csv([self.row], MASTER_HEADER))

    def query_article(self, **kwargs):
        return query_learning_context(self.repo, self.state, source_id=self.binding["source_id"],
                                      sentence_id="S01", source_article=self.binding["source_article"], **kwargs)

    def test_stale_explicit_hash_stops_before_article_support_or_implicit_refs(self):
        # The current identity demonstrably has readable history and a formal row.
        current = self.query_article(source_hash=self.binding["source_hash"], aliases=["dispute"])
        self.assertEqual(current["status"], "ready")
        self.assertIn("首次翻译", current["recorded_article_episode"]["records"][0]["text"])
        self.assertEqual(len(current["formal_records"]), 1)
        for protected in (False, True):
            with self.subTest(allow_protected=protected), patch(
                "english_pipeline.learning_state._article_episode", side_effect=AssertionError("stale article read")
            ), patch(
                "english_pipeline.sentence_support.query_sentence_support", side_effect=AssertionError("stale support read")
            ):
                result = self.query_article(source_hash="0" * 64, aliases=["dispute"], allow_protected=protected)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["reason"], "source_changed")
            self.assertEqual(result["source_status"], "source_changed")
            self.assertEqual(result["fallback"], "current_input_only")
            for key in ("recorded_article_episode", "sentence_support", "formal_records",
                        "sentence_patterns", "learning_history", "formal_version"):
                self.assertNotIn(key, result)
            self.assertEqual(result["formal_write_count"], 0)

    def test_alias_and_patterns_reload_in_same_process(self):
        first = query_learning_context(self.repo, self.state, aliases=["dispute", "open to"])
        self.row["meaning"] = "争议"
        self.write_bank()
        self.pattern.write_text("## SP-001｜open to\n更新解释\n", encoding="utf-8")
        second = query_learning_context(self.repo, self.state, aliases=["dispute", "open to"])
        self.assertEqual(second["status"], "ready")
        self.assertNotEqual(first["formal_version"], second["formal_version"])
        self.assertEqual(second["formal_records"][0]["record"]["meaning"], "争议")
        self.assertIn("更新解释", second["sentence_patterns"][0]["text"])

    def test_missing_history_and_answer_protection_remain_available(self):
        result = query_learning_context(self.repo, self.state, aliases=["no-such-word"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source_status"], "current_input_only")
        self.assertEqual(result["learning_history"], [])
        self.assertEqual(result["formal_records"], [])
        self.assertEqual(result["missing_aliases"], ["no-such-word"])
        self.row["review_note"] = "正确答案是 A"
        self.write_bank()
        protected = query_learning_context(self.repo, self.state, aliases=["dispute"])
        self.assertEqual(protected["reason"], "formal_record_protected")
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
