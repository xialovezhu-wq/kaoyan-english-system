import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from english_pipeline.errors import ValidationError
from english_pipeline.memory_store import (build_memory_projection, export_safe_mirror,
                                           open_memory_projection, publish_managed_pages)
from english_pipeline.personal_summary import SCHEMA, key_hash, publish_personal_summaries
from english_pipeline.util import canonical_bytes, file_sha256, object_sha256


class MemoryFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.state = self.repo / "intake"
        self.source = self.repo / "articles/record.md"
        self.source.parent.mkdir()
        self.source.write_text("真实首答。\n提示后的观察。\n", encoding="utf-8")
        self.record = {"key": "syntax:apposition", "aliases": ["syntax:同位语"],
                       "visibility": "answer_free", "teaching_hint": "同位语说明前面的名词；保留已经读对的主干。",
                       "episodes": [{"episode_id": "E-OLD", "study_date": "2026-09-01", "source_id": "ARTICLE-A",
                                     "unit_id": "S01", "first_attempt": "把名词补充说明当作主句。", "correction": "识别同位关系。",
                                     "later_observation": "提示后能识别，未独立复测。", "hint_dependence": "explanation",
                                     "evidence": [{"path": "articles/record.md", "sha256": file_sha256(self.source),
                                                   "locator": "lines/1-2"}]}]}

    def publish(self, record=None):
        return publish_personal_summaries(self.repo, self.state,
            {"schema_version": SCHEMA, "records": [record or self.record]}, bootstrap_existing_formal=True)

    def raw_publish(self, record):
        record = {**record, "schema_version": SCHEMA}
        digest = object_sha256(record)
        root = self.state / "personal-summary"
        (root / "objects").mkdir(parents=True, exist_ok=True)
        (root / "objects" / (digest + ".json")).write_bytes(canonical_bytes(record))
        path = root / "current.json"
        idx = json.loads(path.read_text()) if path.exists() else {"schema_version": "english_point_personal_summary_index_v1", "routes": {}}
        for key in [record["key"], *record.get("aliases", [])]:
            idx["routes"][key_hash(key)] = {"key": record["key"], "sha256": digest}
        path.write_bytes(canonical_bytes(idx))
        return digest


class MemoryStoreTests(MemoryFixture):
    def test_legacy_question_heading_locator_is_readable_without_changing_protected_evidence(self):
        source = self.repo / "articles/protected-reference.md"
        source.write_text("## Q35\n\nSynthetic reference material.\n")
        record = copy.deepcopy(self.record)
        record["visibility"] = "protected"
        record["episodes"][0]["evidence"] = [{"path": "articles/protected-reference.md",
            "sha256": file_sha256(source), "locator": "Q35；来源PDF p37；标准答案与逐题解析"}]
        self.publish(record)
        original_source = source.read_bytes()
        original_index = (self.state / "personal-summary/current.json").read_bytes()
        result = build_memory_projection(self.repo, self.state)
        self.assertNotIn("locator_unresolved", result["counts"]["issues"])
        self.assertEqual(result["counts"]["safe_point_count"], 0)
        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual((self.state / "personal-summary/current.json").read_bytes(), original_index)

    def test_question_heading_locator_rejects_missing_duplicate_and_ambiguous_sections(self):
        from english_pipeline.memory_store import _SourceAudit
        source = self.repo / "articles/reference.md"
        for text in ("The body mentions Q35.\n", "## Q35\n\n## Q35\n", "## Q34 and Q35\n", "## Q36\n"):
            with self.subTest(text=text):
                source.write_text(text)
                audit, _ = _SourceAudit(self.repo, self.state, True).audit({"path": "articles/reference.md",
                    "sha256": file_sha256(source), "locator": "Q35；来源PDF p37；标准答案与逐题解析"})
                self.assertEqual(audit["locator_status"], "unresolved")

    def test_managed_page_links_real_sources_without_invented_source_notes(self):
        self.publish()
        build_memory_projection(self.repo, self.state)
        result = publish_managed_pages(self.repo, self.state)
        page = next(p for p in Path(result["index_path"]).parent.glob("syntax-*.md"))
        content = page.read_text()
        self.assertIn("[[articles/record|record.md]]", content)
        self.assertIn("lines/1-2", content)
        self.assertNotIn("[[ARTICLE-A]]", content)

    def test_multi_package_zip_binds_the_package_not_bundle_root(self):
        archive = self.repo / "bundle.zip"
        pid = "EN-PKG-20260901-1234567890ABCDEF"
        package_sha = "1" * 64
        prefix = f"packages/{pid}/"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("manifest.json", json.dumps({"attachments": []}))
            z.writestr(prefix + "manifest.json", json.dumps({"package_id": pid,
                "package_canonical_sha256": package_sha, "attachments": [{"role": "user_work"}]}))
            z.writestr(prefix + "conversation.json", json.dumps({"messages": [{"role": "user", "content": "原始作答"}]}))
            z.writestr(prefix + "source.json", json.dumps({"identity": {"source_id": "REAL-ARTICLE", "unit_id": "S03"}}))
        record = copy.deepcopy(self.record)
        record["episodes"][0].pop("source_id")
        record["episodes"][0].pop("unit_id")
        record["episodes"][0]["evidence"] = [{"path": "bundle.zip", "sha256": file_sha256(archive),
            "package_id": pid, "package_sha256": package_sha,
            "pointers": ["conversation.json#/messages/0", "manifest.json#/attachments/0"]}]
        self.raw_publish(record)
        made = build_memory_projection(self.repo, self.state)
        self.assertEqual(made["counts"]["safe_point_count"], 1)
        self.assertFalse(made["counts"]["issues"])
        db, _ = open_memory_projection(self.state)
        self.addCleanup(db.close)
        row = db.execute("SELECT source_ids_json,unit_ids_json FROM points").fetchone()
        self.assertIn("REAL-ARTICLE", json.loads(row[0]))
        self.assertIn("S03", json.loads(row[1]))
        record["episodes"][0]["evidence"][0]["package_sha256"] = "2" * 64
        self.raw_publish(record)
        wrong = build_memory_projection(self.repo, self.state)
        self.assertEqual(wrong["counts"]["safe_point_count"], 0)
        self.assertIn("source_hash_conflict", wrong["counts"]["issues"])

    def test_legacy_mixed_pointer_and_exact_root_field(self):
        source = self.repo / "review.json"
        source.write_text(json.dumps({"items": [{"reason": "原理由"}], "reference_audit": {"status": "checked"}}))
        record = copy.deepcopy(self.record)
        record["episodes"][0]["evidence"] = [{"path": "review.json", "sha256": file_sha256(source),
            "locator": "items/0/reason；reference_audit"}]
        self.raw_publish(record)
        self.assertFalse(build_memory_projection(self.repo, self.state)["counts"]["issues"])

    def test_preview_is_zero_write_and_build_preserves_sources(self):
        self.publish()
        before = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        receipt = build_memory_projection(self.repo, self.state, publish=False)
        self.assertEqual(receipt["status"], "preview")
        self.assertFalse((self.state / "memory").exists())
        self.assertEqual(before, {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()})
        made = build_memory_projection(self.repo, self.state)
        self.assertEqual(made["counts"]["point_count"], 1)
        for path, body in before.items():
            self.assertEqual((self.repo / path).read_bytes(), body)

    def test_incremental_build_retains_prior_generation_and_untouched_point(self):
        self.publish()
        other = copy.deepcopy(self.record)
        other.update(key="word:advertiser", aliases=[], teaching_hint="advertiser 是广告商。")
        other["episodes"][0]["episode_id"] = "WORD-1"
        self.publish(other)
        first = build_memory_projection(self.repo, self.state)
        old_db = Path(first["database_path"]).read_bytes()
        changed = copy.deepcopy(self.record)
        changed["teaching_hint"] = "同位语补充名词；当前已读对主干，保留。"
        self.publish(changed)
        second = build_memory_projection(self.repo, self.state, keys=[changed["key"]])
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(Path(first["database_path"]).read_bytes(), old_db)
        db, _ = open_memory_projection(self.state)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT count(*) FROM points").fetchone()[0], 2)
        self.assertEqual(second["updated_keys"], [changed["key"]])

    def test_idempotent_build_does_not_rewrite_generation_manifest(self):
        self.publish()
        first = build_memory_projection(self.repo, self.state)
        path = Path(first["database_path"]).parent / "manifest.json"
        before = path.read_bytes()
        second = build_memory_projection(self.repo, self.state)
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(before, path.read_bytes())

    def test_reverse_dependencies_include_ancestor_evidence_and_protection(self):
        parent = copy.deepcopy(self.record)
        parent["visibility"] = "protected"
        digest = self.raw_publish(parent)
        child = copy.deepcopy(self.record)
        child.update(key="syntax:child", aliases=[])
        child["episodes"][0].update(episode_id="CHILD", source_episode_id="E-OLD",
            evidence=[{"path": f"intake/personal-summary/objects/{digest}.json", "sha256": digest, "locator": "episodes/0"}])
        child["episodes"][0].pop("source_id")
        self.raw_publish(child)
        made = build_memory_projection(self.repo, self.state)
        db, _ = open_memory_projection(self.state)
        self.addCleanup(db.close)
        row = db.execute("SELECT answer_safe,source_ids_json FROM points WHERE point_key=?", (child["key"],)).fetchone()
        self.assertEqual(row[0], 0)
        self.assertIn("ARTICLE-A", json.loads(row[1]))
        # Ancestor source identity and exact article path remain retrievable.
        sources = [json.loads(r[0])["reference"].get("path") for r in db.execute(
            "SELECT s.audit_json FROM sources s JOIN dependencies d USING(source_key) WHERE d.point_key=?", (child["key"],))]
        self.assertIn("articles/record.md", sources)
        self.assertFalse(list(Path(made["mirror_path"]).glob("*.md")))

    def test_zip_member_locators_and_protected_package_safe_projection(self):
        archive = self.repo / "evidence/study.zip"
        archive.parent.mkdir()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("conversation.json", json.dumps({"messages": [{"role": "user", "content": "PRIVATE_ANSWER_TEXT"}]}))
            z.writestr("source.json", json.dumps({"source_id": "ARTICLE-Z", "unit_id": "S03", "answer_exposure": "protected"}))
        record = copy.deepcopy(self.record)
        record["episodes"][0]["evidence"] = [{"path": "evidence/study.zip", "sha256": file_sha256(archive),
            "pointers": ["conversation.json#/messages/0", "source.json#/source_id"]}]
        self.raw_publish(record)
        made = build_memory_projection(self.repo, self.state)
        self.assertEqual(made["counts"]["safe_point_count"], 1)
        body = "\n".join(p.read_text() for p in Path(made["mirror_path"]).glob("*.md"))
        self.assertNotIn("PRIVATE_ANSWER_TEXT", body)
        self.assertIn("ARTICLE-Z", body)
        db, _ = open_memory_projection(self.state)
        self.addCleanup(db.close)
        source = json.loads(db.execute("SELECT audit_json FROM sources").fetchone()[0])
        self.assertEqual(source["status"], "verified")
        self.assertEqual(source["locator_status"], "verified")

    def test_mutable_revision_drift_is_coverage_not_learning_error(self):
        self.publish()
        self.source.write_text(self.source.read_text() + "后续合法追加。\n")
        made = build_memory_projection(self.repo, self.state)
        self.assertIn("mutable_revision_drift", made["counts"]["issues"])
        db, _ = open_memory_projection(self.state)
        self.addCleanup(db.close)
        audit = json.loads(db.execute("SELECT audit_json FROM points").fetchone()[0])
        self.assertEqual(audit["status"], "coverage_limited")
        self.assertNotIn("learning_error", json.dumps(audit))

    def test_immutable_hash_conflict_excluded_from_safe_mirror(self):
        source = self.repo / "proof.json"
        source.write_text('{"value":"first"}')
        record = copy.deepcopy(self.record)
        record["episodes"][0]["evidence"] = [{"path": "proof.json", "sha256": file_sha256(source), "locator": "/value"}]
        self.raw_publish(record)
        source.write_text('{"value":"changed"}')
        made = build_memory_projection(self.repo, self.state)
        self.assertEqual(made["counts"]["safe_point_count"], 0)
        self.assertIn("source_hash_conflict", made["counts"]["issues"])

    def test_missing_source_and_old_locator_are_reported_without_fabricating_loss(self):
        self.publish()
        self.source.unlink()
        made = build_memory_projection(self.repo, self.state)
        self.assertEqual(made["counts"]["issues"]["unavailable"], 1)
        self.assertEqual(made["counts"]["safe_point_count"], 1)

    def test_corrupt_summary_is_unavailable_without_poisoning_other_points(self):
        self.publish()
        index = json.loads((self.state / "personal-summary/current.json").read_text())
        digest = next(iter(index["routes"].values()))["sha256"]
        (self.state / "personal-summary/objects" / f"{digest}.json").write_text("{}")
        made = build_memory_projection(self.repo, self.state)
        self.assertEqual(made["counts"]["safe_point_count"], 0)
        self.assertEqual(made["counts"]["issues"]["invalid_or_missing_summary"], 1)

    def test_managed_pages_preserve_user_text_and_remove_withdrawn_body(self):
        self.publish()
        build_memory_projection(self.repo, self.state)
        first = publish_managed_pages(self.repo, self.state)
        page = next(p for p in (self.repo / "wiki/memory").glob("*.md") if p.name != "index.md")
        page.write_text("我的笔记开头\n" + page.read_text() + "\n我的笔记结尾")
        lifecycle = {"pending_keys": {self.record["key"]: {"reason": "corrected"}}}
        (self.state / "memory/lifecycle.json").write_text(json.dumps(lifecycle))
        publish_managed_pages(self.repo, self.state)
        body = page.read_text()
        self.assertTrue(body.startswith("我的笔记开头\n"))
        self.assertTrue(body.endswith("我的笔记结尾"))
        self.assertNotIn(self.record["teaching_hint"], body)
        self.assertIn("待更新", body)
        exported = export_safe_mirror(self.repo, self.state)
        self.assertEqual(exported["entry_count"], 0)
        self.assertEqual(first["page_count"], 1)

    def test_explicit_mirror_destination_cannot_overwrite_other_generation(self):
        self.publish()
        build_memory_projection(self.repo, self.state)
        path = self.state / "memory/test-export"
        export_safe_mirror(self.repo, self.state, path)
        (self.state / "memory/lifecycle.json").write_text(json.dumps({"global_pending": True}))
        with self.assertRaises(ValidationError):
            export_safe_mirror(self.repo, self.state, path)

    def test_semicolon_json_locators_are_individually_verified(self):
        source = self.repo / "conversation.json"
        source.write_text(json.dumps({"messages": [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}]}))
        record = copy.deepcopy(self.record)
        record["episodes"][0]["evidence"] = [{"path": "conversation.json", "sha256": file_sha256(source),
                                               "locator": "messages/0;messages/1"}]
        self.raw_publish(record)
        made = build_memory_projection(self.repo, self.state)
        self.assertNotIn("locator_invalid", made["counts"]["issues"])
        db, _ = open_memory_projection(self.state)
        self.addCleanup(db.close)
        self.assertEqual(json.loads(db.execute("SELECT audit_json FROM sources").fetchone()[0])["locator_status"], "verified")

    def test_symlink_to_protected_parent_inherits_visibility(self):
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
        made = build_memory_projection(self.repo, self.state)
        self.assertEqual(made["counts"]["safe_point_count"], 0)

    def test_stale_projection_cannot_export_or_publish_old_safe_text(self):
        self.publish()
        build_memory_projection(self.repo, self.state)
        changed = copy.deepcopy(self.record)
        changed["visibility"] = "protected"
        self.raw_publish(changed)
        for action in (export_safe_mirror, publish_managed_pages):
            with self.assertRaisesRegex(ValidationError, "stale"):
                action(self.repo, self.state)
        self.assertFalse((self.repo / "wiki/memory").exists())
        self.assertFalse((self.state / "memory/backend-mirror.json").exists())

    def test_backend_pointer_binds_post_lifecycle_state_without_revision_counter(self):
        from english_pipeline.memory_store import lifecycle_fingerprint
        self.publish()
        made = build_memory_projection(self.repo, self.state)
        pending = {"pending_keys": {self.record["key"]: {}}, "revision": 1}
        (self.state / "memory/lifecycle.json").write_text(json.dumps(pending))
        post = {"pending_keys": {}, "revision": 1}
        exported = export_safe_mirror(self.repo, self.state, lifecycle_override=post)
        pointer = json.loads((self.state / "memory/backend-mirror.json").read_text())
        self.assertEqual(pointer["generation"], made["generation"])
        self.assertEqual(pointer["lifecycle_sha256"], lifecycle_fingerprint({**post, "revision": 2}))
        self.assertNotEqual(pointer["lifecycle_sha256"], lifecycle_fingerprint(pending))
        self.assertEqual(exported["entry_count"], 1)
        self.assertEqual(json.loads((self.state / "memory/lifecycle.json").read_text()), pending)

    def test_mirror_observation_dates_do_not_date_unknown_followup(self):
        self.publish()
        made = build_memory_projection(self.repo, self.state)
        body = next(Path(made["mirror_path"]).glob("*.md")).read_text()
        self.assertIn("## Observations", body)
        self.assertIn("[learner_first_attempt] @occurred:2026-09-01", body)
        self.assertIn("[learner_followup] 后续观察", body)
        self.assertNotIn("[learner_followup] @occurred:2026-09-01", body)
        self.assertIn("## Relations", body)
        self.assertNotIn("\ngeneration:", body)

    def test_unrelated_update_keeps_unchanged_note_bytes(self):
        self.publish()
        first = build_memory_projection(self.repo, self.state)
        name = key_hash(self.record["key"]) + ".md"
        before = (Path(first["mirror_path"]) / name).read_bytes()
        other = copy.deepcopy(self.record)
        other.update(key="word:advertiser", aliases=[], teaching_hint="advertiser 是广告商。")
        other["episodes"][0]["episode_id"] = "OTHER"
        self.publish(other)
        second = build_memory_projection(self.repo, self.state, keys=[other["key"]])
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(before, (Path(second["mirror_path"]) / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
