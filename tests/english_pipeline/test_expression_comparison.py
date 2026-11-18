"""Synthetic teaching output and role evidence, using the existing CLI."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from english_pipeline.cli import main


class ExpressionComparisonTests(unittest.TestCase):
    def test_stages_actors_and_copyable_state(self):
        fixture = json.loads((Path(__file__).resolve().parents[2] / "examples/english-expression-comparison.json").read_text())
        request = fixture["request"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / "rollout.jsonl"
            events = [dict(type="session_meta", payload={"id": fixture["session_id"]})]
            for mid, role, quote in fixture["turns"]:
                events.append(dict(type="response_item", payload=dict(type="message", role=role, id=mid,
                    content=[dict(type="input_text" if role == "user" else "output_text", text=quote)])))
            rollout.write_text("".join(json.dumps(row) + "\n" for row in events))
            def invoke(value):
                path = root / "input.json"
                path.write_text(json.dumps(value, ensure_ascii=False))
                stream = io.StringIO()
                with redirect_stdout(stream), redirect_stderr(stream):
                    code = main(["expression-comparison", "--input-json", str(path), "--session-id", fixture["session_id"],
                                 "--rollout", str(rollout), "--repo-root", str(root), "--state-dir", str(root / "intake")])
                return code, json.loads(stream.getvalue())
            code, before = invoke({**request, "stage": "before_translation"})
            self.assertEqual(code, 0)
            self.assertNotIn("每一种", json.dumps(before, ensure_ascii=False))
            self.assertNotIn("You cannot", str(before))
            self.assertNotIn("copy_states", before)
            code, after = invoke(request)
            self.assertEqual(code, 0)
            self.assertEqual(after["first_attempt"], fixture["turns"][1][2])
            self.assertEqual(after["followup_status"], "not_observed")
            self.assertTrue(all(x["actor"] == "assistant" for x in after["suggestions"]))
            self.assertIn("含义变化", after["chat_text"])
            self.assertIn("| 类型 | 表达 | 与原句的关系 |", after["chat_text"])
            a, b = after["copy_states"]
            self.assertEqual(a["scene_version"], b["scene_version"])
            self.assertNotEqual(a["state_version"], b["state_version"])
            self.assertNotIn(after["first_attempt"], json.dumps(a, ensure_ascii=False))
            self.assertFalse(a["learning_evidence"])
            ack = dict(message_id="ack", quote="懂了", status="acknowledgment_only")
            self.assertEqual(invoke({**request, "followup": ack})[1]["followup_status"], "acknowledgment_only")
            revision = dict(message_id="revision", quote=fixture["turns"][4][2], status="prompted",
                            hint=dict(message_id="hint", quote=fixture["turns"][2][2]))
            code, revised = invoke({**request, "followup": revision})
            self.assertEqual(code, 0)
            self.assertEqual(revised["first_attempt"], after["first_attempt"])
            self.assertEqual(revised["followup_text"], fixture["turns"][4][2])
            direct = dict(request, stage="direct_explanation", direct_request={"message_id": "direct", "quote": fixture["turns"][5][2]})
            del direct["first_attempt"]
            code, explanation = invoke(direct)
            self.assertEqual(code, 0)
            self.assertNotIn("first_attempt", explanation)
            self.assertEqual(explanation["suggestions"], after["suggestions"])
            wrong_actor = {**request, "first_attempt": revision["hint"]}
            self.assertNotEqual(invoke(wrong_actor)[0], 0)
            question = copy.deepcopy(request)
            question["source"]["unit_id"] = "Q21"
            self.assertNotEqual(invoke(question)[0], 0)
            self.assertFalse((root / "intake").exists())
