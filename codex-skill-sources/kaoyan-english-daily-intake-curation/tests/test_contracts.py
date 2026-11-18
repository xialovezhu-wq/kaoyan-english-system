from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ContractTests(unittest.TestCase):
    def test_direct_package_contract_has_no_old_runtime_route(self):
        root = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("开始英语正式入库", root)
        for name, fields in {
            "backlog-gate-contract": ("english_package_backlog_plan_v1", "plan_canonical_sha256", "NOOP",
                "completed", "already_consumed", "needs_user", "failed", "archive_pending", "exact locator reread", "user_removed"),
            "nightly-curation-contract": ("english_nightly_manifest_v3", "Historical v2", "english_sol_actions_v2",
                "english_sentence_support_proposal_v1", "english_sentence_support_preflight_receipt_v1"),
            "sentence-support-contract": ("english_sentence_support_refresh_receipt_v1", "FORMAL_COMMITTED_SUPPORT_PENDING"),
            "archive-contract": ("archive-pointer",),
            "output-contract": ("FORMAL_COMMITTED_ARCHIVE_PENDING",),
        }.items():
            relative = f"references/{name}.md"
            self.assertIn(relative, root)
            contract = (ROOT / relative).read_text(encoding="utf-8")
            for field in fields:
                self.assertIn(field, contract)

    def test_backlog_reference_requires_explicit_apply_and_legacy_audit_only(self):
        text = (ROOT / "references" / "backlog-gate-contract.md").read_text(encoding="utf-8")
        self.assertIn("--apply --authorization <exact-plan_id>", text)
        self.assertIn("completely read-only", text)
        self.assertIn("migration_performed", text)
        self.assertIn("never repeats apply", text)
        self.assertIn("cannot be synthesized", text)
        self.assertIn("support_refresh", text)

    def test_sentence_support_contract_is_post_writer_and_pre_archive(self):
        text = (ROOT / "references" / "sentence-support-contract.md").read_text(encoding="utf-8")
        self.assertIn("english_sentence_support_proposal_v1", text)
        self.assertIn("english_sentence_support_record_v1", text)
        self.assertIn("english_sentence_support_index_v1", text)
        self.assertIn("english_sentence_support_refresh_receipt_v1", text)
        self.assertIn("FORMAL_COMMITTED_SUPPORT_PENDING", text)
        self.assertIn("never regenerates formal actions or repeats dry-run/apply", text)
        self.assertIn("current sentence and the user's current first translation", text)
        self.assertIn("formal and SP identities as locators only", text)
        self.assertIn("coverage_status=partial", text)
        self.assertIn("--support-preflight", (ROOT / "references/nightly-curation-contract.md").read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
