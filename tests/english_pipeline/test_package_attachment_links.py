from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_pipeline.cli import main as cli_main
from english_pipeline.errors import ValidationError
from english_pipeline.packages import create_conversation_package, validate_conversation_package
from english_pipeline.util import file_sha256


class PackageAttachmentLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.image = self.root / "question.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    def request(self, key: str) -> dict:
        return {
            "idempotency_key": key,
            "segment_key": key,
            "thread_ref": "attachment-links:" + key,
            "occurred_at": "2026-09-08T09:00:00+08:00",
            "source": {"source_id": "fixture-source", "source_kind": "question", "answer_exposure": "protected"},
            "conversation": [
                {"role": "user", "content": " 原始题图\r\n ", "attachment_refs": ["ATT-001"]},
                {"role": "assistant", "content": "保留原题，尚未作答。"},
            ],
            "attachments": [{"path": str(self.image), "role": "question_image", "message_sequence": 1}],
            "expected_attachment_roles": ["question_image", "explanation_image"],
        }

    def capture(self, request: dict, state: Path) -> tuple[int, dict]:
        path = self.root / "request.json"
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(["capture", "--repo-root", str(self.repo), "--state-dir", str(state), "--input-json", str(path)])
        return code, json.loads(stdout.getvalue() or stderr.getvalue())

    def invalid_requests(self):
        request = self.request("dangling")
        request["conversation"][0]["attachment_refs"] = ["ATT-999"]
        yield "dangling", request, "unknown attachment"
        request = self.request("missing-declaration")
        request.pop("attachments")
        yield "missing-declaration", request, "unknown attachment"
        for sequence in (0, -1, 3, 999, True, False, 1.0, "1", [], {}):
            request = self.request("bad-sequence")
            request["attachments"][0]["message_sequence"] = sequence
            yield repr(sequence), request, "message_sequence must be an integer"
        request = self.request("conflict")
        request["attachments"][0]["message_sequence"] = 2
        yield "conflict", request, "conflicts with attachment_refs"

    def test_invalid_links_are_rejected_by_cli_before_any_state_write(self) -> None:
        for name, request, error in self.invalid_requests():
            with self.subTest(name=name):
                state = self.repo / "intake"
                code, result = self.capture(request, state)
                self.assertEqual(code, 2)
                self.assertIn(error, json.dumps(result))
                self.assertFalse(state.exists(), "invalid input must not create package, receipt or frontier state")

    def test_hash_bound_historical_bad_links_fail_readback_without_rewrite(self) -> None:
        # Disable only the new link check to reproduce old-producer packages with
        # valid component hashes, receipt bindings and continuation tokens.
        for index, (name, request, error) in enumerate(self.invalid_requests()):
            with self.subTest(name=name):
                with patch("english_pipeline.packages._validate_attachment_links"):
                    root, _ = create_conversation_package(self.repo / f"legacy-{index}", request)
                before = {str(p.relative_to(root)): file_sha256(p) for p in root.rglob("*") if p.is_file()}
                with self.assertRaisesRegex(ValidationError, error):
                    validate_conversation_package(root)
                self.assertEqual(before, {str(p.relative_to(root)): file_sha256(p) for p in root.rglob("*") if p.is_file()})

    def test_ambiguous_attachment_ids_are_rejected_on_readback(self) -> None:
        from english_pipeline import packages

        original_check = packages._validate_attachment_links

        def old_duplicate_ids(messages, attachments):
            if len(attachments) == 2:
                attachments[1]["attachment_id"] = attachments[0]["attachment_id"]

        request = self.request("duplicate-id")
        request["attachments"].append({"path": str(self.image), "role": "other_attachment"})
        with patch.object(packages, "_validate_attachment_links", side_effect=old_duplicate_ids):
            root, _ = create_conversation_package(self.repo / "duplicate", request)
        with self.assertRaisesRegex(ValidationError, "duplicate attachment_id"):
            validate_conversation_package(root)
        self.assertIs(packages._validate_attachment_links, original_check)

    def test_valid_optional_links_missing_fields_replay_and_continuation(self) -> None:
        variants = ("both", "refs-only", "sequence-only", "package-level", "null-sequence", "reused-reference", "missing-image")
        for variant in variants:
            with self.subTest(variant=variant):
                request = self.request(variant)
                if variant in {"refs-only", "package-level"}:
                    request["attachments"][0].pop("message_sequence")
                if variant in {"sequence-only", "package-level", "missing-image"}:
                    request["conversation"][0].pop("attachment_refs")
                if variant == "null-sequence":
                    request["attachments"][0]["message_sequence"] = None
                if variant == "reused-reference":
                    request["conversation"][1]["attachment_refs"] = ["ATT-001"]
                if variant == "missing-image":
                    request.pop("attachments")
                state = self.repo / variant
                code, first = self.capture(request, state)
                self.assertEqual(code, 0, first)
                self.assertEqual(first["segment_gate_status"], "ready")
                root = Path(first["package_path"])
                manifest = validate_conversation_package(root)
                self.assertIn("attachments.explanation_image", manifest["missing_fields"])
                saved = json.loads((root / "conversation.json").read_text())
                self.assertEqual([m["content"] for m in saved["messages"]], [m["content"] for m in request["conversation"]])
                for attachment in manifest["attachments"]:
                    self.assertEqual((root / attachment["path"]).read_bytes(), self.image.read_bytes())
                before = {str(p.relative_to(root)): file_sha256(p) for p in root.rglob("*") if p.is_file()}
                code, replay = self.capture(request, state)
                self.assertEqual(code, 0, replay)
                self.assertEqual(replay["status"], "idempotent_noop")
                continuation = copy.deepcopy(request)
                continuation.update(idempotency_key=variant + "-next", segment_key=variant + "-next", previous_token=first["continuation_token"])
                continuation["conversation"][0]["content"] = "补充同题原始证据。"
                code, second = self.capture(continuation, state)
                self.assertEqual(code, 0, second)
                self.assertEqual(second["segment_gate_status"], "ready")
                self.assertNotEqual(first["package_id"], second["package_id"])
                validate_conversation_package(Path(second["package_path"]))
                self.assertEqual(before, {str(p.relative_to(root)): file_sha256(p) for p in root.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
