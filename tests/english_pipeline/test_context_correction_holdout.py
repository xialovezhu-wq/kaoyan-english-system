"""Synthetic source-separated acceptance of evidence available to the English tutor."""
import contextlib
import io
import json
import unittest

from english_pipeline.memory_cli import main
from english_pipeline.memory_store import build_memory_projection
from english_pipeline.util import atomic_write_json, file_sha256
from tests.english_pipeline.test_memory_store import MemoryFixture


# Each headword is a separate synthetic article family. Within a family the
# evidence for both applicable contexts remains available for final explanation.
COEXIST = (
    ("charge", "Charge the battery before leaving.", "给电池充电", "They charge a fee.", "收取费用"),
    ("file", "She filed the report.", "提交报告", "The file is archived.", "档案"),
    ("draft", "They drafted a bill.", "起草法案", "A cold draft came through.", "穿堂风"),
    ("fine", "We had a fine meal.", "美味的一餐", "Pay the fine.", "缴纳罚款"),
    ("plant", "They planted a tree.", "种树", "The chemical plant closed.", "工厂"),
    ("subject", "Subject the samples to heat.", "对样品加热", "The subject of the essay is trade.", "主题"),
)
REVISED = (
    ("screen", "The hedge screened the garden.", "树篱遮挡花园", "把树篱装上屏幕"),
    ("address", "The report addresses the shortage.", "报告讨论短缺问题", "报告列出短缺的地址"),
    ("discount", "Officials discounted the rumor.", "官员认为传闻不可信", "官员给传闻打折"),
    ("secure", "They secured the contract.", "他们拿到了合同", "他们绑紧合同"),
    ("account", "Weather may account for the delay.", "天气可能是延误的原因", "天气可能为延误开账户"),
    ("observe", "Operators must observe the ban.", "操作人员必须遵守禁令", "操作人员必须观察禁令"),
)


class ContextCorrectionHoldout(MemoryFixture):
    def add_observation(self, label, english, correction, *, date="2026-10-12",
                        source=None, unit="S01", **fields):
        source = source or "SYN-HOLD-" + label
        relative = f"articles/{label}.txt"
        path = self.repo / relative
        path.write_text(english + "\n", encoding="utf-8")
        return {
            "episode_id": label, "study_date": date, "source_id": source, "unit_id": unit,
            "correction": correction, "correction_observed_at": date,
            "evidence": [{"path": relative, "locator": "lines/1-1", "sha256": file_sha256(path)}],
            **fields,
        }

    def index(self, cases):
        for key, episodes, fields in cases:
            self.raw_publish({"key": key, "visibility": "answer_free", "teaching_hint": "核对当前语言条件",
                              "episodes": episodes, **fields})
        build_memory_projection(self.repo, self.state)

    def read(self, *flags):
        output = io.StringIO()
        before = {p: file_sha256(p) for p in self.repo.rglob("*") if p.is_file()}
        with contextlib.redirect_stdout(output):
            code = main(["query-memory-context", "--repo-root", str(self.repo), "--state-dir", str(self.state),
                         "--backend", "native", "--view", "teaching", "--max-bytes", "6144", *flags])
        raw = output.getvalue().encode("utf-8")
        self.assertEqual(code, 0)
        self.assertLessEqual(len(raw), 6144)
        body = json.loads(raw)
        self.assertEqual(body["backend"]["api_requests"], 0)
        self.assertEqual(body["formal_write_count"], 0)
        self.assertEqual(body["learning_event_write_count"], 0)
        self.assertEqual(before, {p: file_sha256(p) for p in self.repo.rglob("*") if p.is_file()})
        return body

    @staticmethod
    def claim(cid, key, episode, content, status="confirmed"):
        return {"claim_id": cid, "point_key": key, "evidence_episode_ids": [episode["episode_id"]],
                "content": content, "basis": "observed" if status == "confirmed" else "inference",
                "status": status, "valid_from": episode["study_date"]}

    def test_six_context_or_part_of_speech_conflicts_keep_both_scoped_claims(self):
        cases, claims = [], {}
        for number, (word, current, correction, other, other_sense) in enumerate(COEXIST):
            key = "word:" + word
            episode = self.add_observation(word, " ".join((current, other)), correction)
            cases.append((key, [episode], {}))
            first, second = (("A", "Z") if number % 2 else ("Z", "A"))
            for suffix, text in ((first, f"{current} {correction}"), (second, f"{other} {other_sense}")):
                cid = f"{word}-{suffix}"
                claims[cid] = self.claim(cid, key, episode, text)
        self.index(cases)
        atomic_write_json(self.state / "memory/claims.json", {"claims": claims})
        for word, current, correction, other, other_sense in COEXIST:
            with self.subTest(family=word):
                result = self.read("--key", "word:" + word, "--source-id", "SYN-HOLD-" + word, "--unit-id", "S01")
                point, = result["matched"]
                self.assertEqual(result["status"], "ready")
                self.assertEqual(point["latest_observation"]["correction"], correction)
                self.assertEqual({c["content"] for c in point["claims"]},
                                 {f"{current} {correction}", f"{other} {other_sense}"})
                self.assertTrue(all(c["status"] == "confirmed" for c in point["claims"]))
                self.assertEqual(point["sources"][0]["status_at_build"], "verified")

    def test_six_revisions_exclude_later_import_of_old_mistakes_and_keep_candidates_unconfirmed(self):
        cases, claims, invalidated, expected = [], {}, [], {}
        for word, english, correction, obsolete in REVISED:
            key = "word:" + word
            current = self.add_observation(word, english, correction)
            old = self.add_observation(word + "-old", english, obsolete, date="2026-10-01",
                                       source=current["source_id"])
            cases.append((key, [current, old], {}))
            for suffix, observation, text, status in (
                ("A", old, obsolete, "confirmed"),
                ("B", current, correction, "confirmed"),
                ("C", current, "可能需要口头再练", "candidate"),
            ):
                cid = f"{word}-{suffix}"
                claims[cid] = self.claim(cid, key, observation, text, status)
            invalidated.append(f"{word}-A")
            expected[word] = current
        self.index(cases)
        atomic_write_json(self.state / "memory/claims.json", {"claims": claims})
        atomic_write_json(self.state / "memory/lifecycle.json", {"invalidated_claim_ids": invalidated})
        for word, _, correction, obsolete in REVISED:
            with self.subTest(family=word):
                result = self.read("--key", "word:" + word, "--source-id", expected[word]["source_id"])
                point, = result["matched"]
                self.assertEqual(point["latest_observation"]["correction"], correction)
                self.assertEqual([(c["claim_id"], c["status"]) for c in point["claims"]],
                                 [(f"{word}-B", "confirmed"), (f"{word}-C", "candidate")])
                self.assertNotIn(obsolete, json.dumps(point, ensure_ascii=False))

    def test_four_independent_families_preserve_prompt_dependence_and_current_scope(self):
        specs = (
            ("infer", "The jury inferred intent.", "推断", "懂了", "acknowledgment_only", "not_observed"),
            ("coerce", "They coerced him into signing.", "迫使", "提示后能复述", "prompted", "hint"),
            ("tender", "US dollars are legal tender.", "法定货币", "没有再次作答", "not_observed", "not_observed"),
            ("yield", "Bonds can yield income.", "产生收益", "在新句无提示认出", "independent", "none"),
        )
        cases = []
        for word, english, meaning, observed, status, hint in specs:
            attempt = meaning if word == "tender" else "需要再核对这个词"
            episode = self.add_observation(word, english, meaning, first_attempt=attempt, later_observation=observed,
                                           followup_status=status, hint_dependence=hint)
            cases.append(("word:" + word, [episode], {}))
        self.index(cases)
        for word, _, meaning, observed, status, hint in specs:
            with self.subTest(family=word):
                result = self.read("--key", "word:" + word, "--source-id", "SYN-HOLD-"+word)
                episode = result["matched"][0]["latest_observation"]
                self.assertEqual((episode["correction"], episode["later_observation"]), (meaning, observed))
                self.assertEqual((episode["followup_status"], episode["hint_dependence"]), (status, hint))
                if word == "tender":
                    self.assertEqual(episode["first_attempt"], episode["correction"])
                self.assertNotIn("teaching_hint", result["matched"][0])

    def test_four_test_articles_keep_exact_question_and_answer_scope(self):
        cases = []
        for art, qid, choice in (("rubric-a", "Q21", "A"), ("rubric-b", "Q21", "B"),
                                 ("rubric-c", "Q22", "C"), ("rubric-d", "Q23", "D")):
            scope = f"SYN-HOLD-{art}"
            episode = self.add_observation(art, "A separate synthetic question", "Correct answer: " + choice,
                                           source=scope, unit=qid, question_id=qid)
            cases.append((f"option_type:{art}", [episode], {"visibility": "protected", "aliases": [f"syntax:{art}"]}))
        control = self.add_observation("related-language", "Only the wording is open.", "只核对语言")
        cases.append(("word:wording", [control], {"related_points": ["option_type:rubric-a"]}))
        self.index(cases)
        public = json.dumps(self.read("--key", "word:wording"), ensure_ascii=False)
        self.assertNotIn("rubric-a", public)
        self.assertNotIn("Correct answer", public)
        for art, qid, choice in (("rubric-a", "Q21", "A"), ("rubric-b", "Q21", "B"),
                                 ("rubric-c", "Q22", "C"), ("rubric-d", "Q23", "D")):
            with self.subTest(family=art):
                key = f"syntax:{art}"
                scope = f"SYN-HOLD-{art}"
                self.assertFalse(self.read("--key", key, "--source-id", scope, "--question-id", qid)["matched"])
                self.assertFalse(self.read("--key", key, "--source-id", scope, "--question-id", qid,
                                           "--unlocked-question-id", "Q30")["matched"])
                self.assertFalse(self.read("--key", key, "--source-id", "SYN-HOLD-rubric-x", "--question-id", qid,
                                           "--unlocked-question-id", qid)["matched"])
                self.assertFalse(self.read("--query", art)["matched"])
                result = self.read("--key", key, "--source-id", scope, "--question-id", qid, "--unlocked-question-id", qid)
                self.assertEqual(result["matched"][0]["latest_observation"]["correction"], "Correct answer: " + choice)

    def test_two_unavailable_families_never_supply_a_future_or_retracted_correction(self):
        future = self.add_observation("cast", "The lamps cast shadows.", "投下影子")
        withdrawn = self.add_observation("suspend", "The sign hangs by a hook.", "旧纠正留在原件")
        self.index([("word:cast", [future], {}), ("word:suspend", [withdrawn], {})])
        atomic_write_json(self.state / "memory/lifecycle.json",
                          {"invalidated_episode_ids": [withdrawn["episode_id"]]})
        with self.subTest(family="cast"):
            result = self.read("--key", "word:cast", "--source-id", future["source_id"], "--as-of", "2026-10-10")
            self.assertEqual(result["status"], "no_evidence")
            self.assertFalse(result["matched"])
        with self.subTest(family="suspend"):
            result = self.read("--key", "word:suspend", "--source-id", withdrawn["source_id"])
            self.assertFalse(result["matched"])

    def test_two_ordinary_families_can_continue_with_or_without_memory(self):
        ordinary = self.add_observation("lend", "Can you lend me your pen?", "把笔借给我")
        self.index([("word:lend", [ordinary], {})])
        with self.subTest(family="lend"):
            result = self.read("--key", "word:lend", "--source-id", ordinary["source_id"])
            self.assertEqual(result["matched"][0]["latest_observation"]["correction"], "把笔借给我")
        with self.subTest(family="lighten"):
            result = self.read("--key", "word:lighten", "--source-id", "SYN-HOLD-lighten")
            self.assertEqual(result["status"], "not_found")
            self.assertEqual(result["matched"], [])


if __name__ == "__main__":
    unittest.main()
