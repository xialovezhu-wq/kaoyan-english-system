"""Conservative content checks for English teaching projections.

These signals are a backstop for the authoring review, not a way to certify
that arbitrary prose cannot reveal a solution.
"""
import re
import unicodedata
from typing import Any


ANSWER_PATTERN = re.compile(
    r"(?:correct|standard|best|official)\s+(?:answer|option)\s*(?:is|[:=：])?\s*(?:option\s*)?[a-d]\b"
    r"|answer(?:\s+key)?\s*(?:is|was|[:=：])\s*(?:option\s*)?[a-d](?=\s*(?:[.,;:!?)]|$))"
    r"|(?:choose|select|eliminate|rule out)\s*(?:option\s+[a-d]\b|[a-d](?=\s*(?:[.,;:!?)]|$)))"
    r"|\b(?:option\s*)?[a-d]\s*(?:\)|\.)?\s+(?:is|was)\s+(?:correct|incorrect|wrong|right|the\s+answer)\b"
    r"|(?:正确答案|正确选项|标准答案|应选|应该选|故选|所以选|因此选|排除|答案\s*[:：是为])"
    r"\s*[:：是为]?\s*[（(]?\s*(?:第?[一二三四1234]\s*项|[a-d](?![a-z]))"
    r"|选项\s*[a-d](?![a-z])\s*(?:成立|正确|错误|不正确|不成立|应选|可排除)"
    r"|(?<![a-z])(?:[a-d]\s*(?:项|选项)|第?[一二三四1234]\s*项)\s*(?:正确|错误|应选|可排除|不成立|是答案)"
    r"|\bQ\d{1,3}\s*[:：=]\s*[a-d]\b",
    re.I,
)


def contains_answer_signal(value: Any) -> bool:
    if isinstance(value, str):
        return bool(ANSWER_PATTERN.search(unicodedata.normalize("NFKC", value)))
    if isinstance(value, dict):
        return any(contains_answer_signal(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_answer_signal(item) for item in value)
    return False


def cites_answer_analysis(ref: dict[str, Any]) -> bool:
    """Known answer/analysis artifacts, including a protected bundle member."""
    for field in ("path", "member", "locator", "pointer", "locators", "pointers"):
        value = ref.get(field, [])
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, str):
                continue
            raw = item.replace("\\", "/").casefold()
            if ("/raw/protected/" in "/" + raw or re.search(r"(?:^|/)(?:answer[-_]?keys?|exam-reading-analysis)(?:[./#_-]|$)", raw)
                    or re.search(r"\bQ\d+\b.*(?:标准答案|逐题解析|正确选项)", item, re.I)):
                return True
    return False


def point_answer_risk(record: dict[str, Any], *, question_bound: bool = False) -> bool:
    # A concrete option analysis can disclose the answer without a choice
    # letter. Keep those trajectories behind the scoped question reader.
    if str(record.get("key", "")).startswith("option_type:") and (
            question_bound or record.get("question_id")
            or any(ep.get("question_id") for ep in record.get("episodes", []))):
        return True
    return contains_answer_signal(record) or any(
        cites_answer_analysis(ref)
        for episode in record.get("episodes", []) for ref in episode.get("evidence", [])
        if isinstance(ref, dict))
