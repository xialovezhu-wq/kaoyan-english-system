"""Synthetic writer and archive setup for the public scoped-correction suite."""

import tempfile
from pathlib import Path

from cold_fixture import install_cold_fixture

from english_pipeline.archive import VolumeContract
from english_pipeline.constants import MASTER_HEADER, MASTERED_HEADER
from english_pipeline.formal import serialize_csv
from english_pipeline.nightly import freeze_nightly
from english_pipeline.packages import create_conversation_package
from english_pipeline.sentence_support import (
    _CUE_FIELDS, _source_identity_from_package, current_index_sha256, preflight_sentence_support,
)
from english_pipeline.util import atomic_write_json, file_sha256, load_json, utc_now
from english_pipeline.writer import apply_nightly


class ScopedCorrectionFixture:
    def __init__(self, test):
        self.temp = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        install_cold_fixture(test, self.root)
        self.repo = self.root / "repo"
        self.state = self.repo / "intake"
        (self.repo / "bank").mkdir(parents=True)
        self.bank_id = "20260901-001"
        self.row = dict(zip(MASTER_HEADER, [self.bank_id, "2026-09-01", "单词", "dispute", "fixture.md",
            "The finding remains open to dispute.", "争执", "fixture usage", "一般", "fixture", "fixture note", "1", "2026-09-01"]))
        self._write_bank()
        (self.repo / "bank/mastered_items.csv").write_bytes(serialize_csv([], MASTERED_HEADER))
        (self.repo / "bank/sentence_patterns.md").write_text("# Patterns\n")

    def _write_bank(self):
        (self.repo / "bank/master_bank.csv").write_bytes(serialize_csv([self.row], MASTER_HEADER))

    def _request(self, key, *, day="2026-09-07", independent=False, protected=False):
        user = "这个结论仍有争议。\n理由是 dispute 在这里指争议。" if independent else "我先译成这个发现仍然开放。"
        return {"idempotency_key": key, "segment_key": key, "thread_ref": f"fixture:{key}",
                "occurred_at": day + "T09:00:00+08:00",
                "source": {"source_id": "RAW-R11", "source_hash": "a" * 64,
                           "source_kind": "explanation" if protected else "article",
                           "answer_exposure": "protected" if protected else "answer_free",
                           "paragraph_id": "P01", "sentence_id": "S01",
                           "source_sentence": "The finding remains open to dispute."},
                "conversation": [{"role": "user", "content": user,
                                  **({"metadata": {"evidence_tags": ["independent_correct_use"]}} if independent else {})},
                                 {"role": "assistant", "content": "open to dispute 表示仍可能受到质疑；dispute 指争议。"}]}

    def _capture(self, request):
        _, receipt = create_conversation_package(self.state, request)
        return receipt

    def _ref(self, receipt, index=0, kind="user_message"):
        return {"package_id": receipt["package_id"], "package_sha256": receipt["package_sha256"],
                "pointer": f"conversation.json#/messages/{index}", "kind": kind}

    def _event(self, key, request, **changes):
        event = {"event_key": key, "kind": "review", "origin": "daily", "task_id": None,
                 "source_commit": None, "source_id": "RAW-R11", "unit_id": "S01",
                 "bank_ids": [self.bank_id], "sentence_pattern_ids": [], "concept_ids": ["bank:" + self.bank_id],
                 "observed_at": request["occurred_at"], "outcome": "incorrect", "hint_dependence": "not_observed",
                 "reasoning_status": "not_observed", "user_response": request["conversation"][0]["content"],
                 "reasoning": "", "correction": "争议"}
        event.update(changes)
        return event

    def _action(self, event, refs):
        return {"action_id": "EVENT-" + event["event_key"], "action_type": "learning_event_append",
                "reason": "explicit fixture decision", "evidence_refs": refs, "event": event}

    def _freeze_actions(self, packages, actions, *, day):
        manifest_path, manifest = freeze_nightly(self.state, self.repo, study_date=day,
                                               package_ids={p["package_id"] for p in packages})
        document = {"schema_version": "english_sol_actions_v2", "action_set_id": "ACTION-" + manifest["batch_id"],
                    "batch_id": manifest["batch_id"], "batch_manifest_sha256": file_sha256(manifest_path),
                    "created_at": utc_now(), "producer": {"role": "sol_nightly_reviewer", "model": "fixture", "prompt_version": "r11"},
                    "formal_write_count": 0, "formal_writeback": "none", "actions": actions, "unresolved": []}
        actions_path = self.root / (manifest["batch_id"] + ".actions.json")
        atomic_write_json(actions_path, document)
        return manifest_path, manifest, actions_path

    def _prepare_apply(self, packages, actions, *, day="2026-09-07"):
        manifest_path, manifest, actions_path = self._freeze_actions(packages, actions, day=day)
        document = load_json(actions_path)
        dry_path, dry = apply_nightly(self.state, self.repo, manifest_path=manifest_path, actions_path=actions_path)
        records = []
        for package in packages:
            source = _source_identity_from_package(Path(package["package_path"]))
            source = {key: value for key, value in source.items() if key not in {"package_id", "package_sha256"}}
            protected = source["source_kind"] in {"question", "option", "explanation"} or source["answer_exposure"] != "answer_free"
            records.append({"package_id": package["package_id"], "package_sha256": package["package_sha256"],
                            "record_kind": "protected_sidecar" if protected else "sentence", "source_identity": source,
                            "evidence_refs": [self._ref(package), self._ref(package, 1, "assistant_message")],
                            "paragraph_context": {"text": None, "locator": None, "evidence_ref_indexes": []},
                            "history_episodes": [], "teaching_cues": {key: [] for key in _CUE_FIELDS}, "vocabulary_candidates": []})
        proposal = {"schema_version": "english_sentence_support_proposal_v1", "proposal_id": "PROPOSAL-" + manifest["batch_id"],
                    "batch_id": manifest["batch_id"], "batch_manifest_sha256": file_sha256(manifest_path),
                    "action_set_id": document["action_set_id"], "expected_index_sha256": current_index_sha256(self.state),
                    "created_at": utc_now(), "producer": document["producer"], "records": records,
                    "formal_write_count": 0, "background_processing": "none"}
        proposal_path = self.root / (manifest["batch_id"] + ".support.json")
        atomic_write_json(proposal_path, proposal)
        preflight_path, _ = preflight_sentence_support(self.state, manifest_path=manifest_path, proposal_path=proposal_path,
            dry_run_receipt_path=dry_path, expected_completed_ids={p["package_id"] for p in packages})
        apply_args = {"manifest_path": manifest_path, "actions_path": actions_path, "apply": True,
                      "authorization": manifest["batch_id"], "support_preflight_path": preflight_path}
        return apply_args, manifest, proposal_path, dry

    def _volume(self):
        root = self.root / "volume"
        root.mkdir()
        sentinel = root / "sentinel.json"
        atomic_write_json(sentinel, {"volume_name": "fixture", "volume_uuid": "fixture-uuid", "expected_mount_point": str(root)})
        return VolumeContract(archive_root=root, subject_relative_root=Path("english"), sentinel_relative_path=Path("sentinel.json"),
                              sentinel_sha256=file_sha256(sentinel), volume_name="fixture", volume_uuid="fixture-uuid",
                              rejected_mount_path=self.root / "rejected")
