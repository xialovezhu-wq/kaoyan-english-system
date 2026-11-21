"""Select complete, locator-bound evidence for formal review only."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import FormalModelReviewPending
from .util import bytes_sha256, canonical_bytes, file_sha256, load_json, normalize_sentence, object_sha256


SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|(?:api[_ -]?key|authorization)\s*[:=]\s*\S+", re.I)
_PARAGRAPH_BREAK = re.compile(r"\n[ \t\r]*\n+")
_SENTENCE_END = re.compile(r"""[。！？]+[”’"')\]）」』】]*|[.!?]+[”’"')\]）]*(?=\s|$)""")
_ABBREVIATION = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof|St|No|Fig|vs|etc|e\.g|i\.e|[A-Za-z])\.$", re.I)
_CONTEXT_CUE = re.compile(
    r"\b(?:correction|instead|not|never|without|unless|if|except|exception|only|otherwise|however|provided that|"
    r"as long as|depends on|refers to)\b|不是|而是|更正|订正|除非|如果|否则|仅当|只有|前提|指代", re.I)
_CROSS_REFERENCE = re.compile(
    r"\b(?:former|latter|the preceding|see above|see below|as (?:stated|noted) above|as follows)\b"
    r"|前者|后者|上述|上文|下文|如下", re.I)


def frozen_messages(record: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(record["path"])
    manifest_path = root / "manifest.json"
    if file_sha256(manifest_path) != record["manifest_sha256"]:
        raise FormalModelReviewPending("frozen conversation manifest changed before formal review")
    manifest = load_json(manifest_path)
    raw = (root / "conversation.json").read_bytes()
    if (manifest["package_canonical_sha256"] != record["package_sha256"]
            or bytes_sha256(raw) != manifest["files"]["conversation.json"]["sha256"]
            or file_sha256(manifest_path) != record["manifest_sha256"]):
        raise FormalModelReviewPending("frozen conversation bytes changed before formal review")
    return json.loads(raw)["messages"]


def message_ref(package: dict[str, Any], index: int, message: dict[str, Any]) -> dict[str, Any]:
    role = message["role"]
    return {"binding": {"package_id": package["package_id"], "package_sha256": package["package_sha256"],
                        "pointer": f"conversation.json#/messages/{index}", "kind": f"{role}_message",
                        "resolved_type": "conversation_message", "resolved_role": role,
                        "resolved_node_sha256": object_sha256(message)}, "value": message}


def _sentence_ranges(text: str, start: int, end: int) -> list[tuple[int, int]]:
    ranges = []
    for match in _SENTENCE_END.finditer(text, start, end):
        finish = match.end()
        if match.group() == "." and _ABBREVIATION.search(text[max(start, finish - 8):finish]):
            continue
        ranges.append((start, finish))
        start = finish
    if start < end:
        ranges.append((start, end))
    return ranges


def _message_excerpt(text: str, anchors: tuple[str, ...]) -> list[tuple[int, int]]:
    """Return character spans for whole anchored sentences and their governing context."""
    sections = []
    start = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        sections.append((start, match.start()))
        start = match.end()
    sections.append((start, len(text)))
    spans = []
    found_anchor = False
    for start, end in sections:
        sentences = _sentence_ranges(text, start, end)
        keep = set()
        for index, (left, right) in enumerate(sentences):
            sentence = text[left:right]
            if any(anchor in sentence.casefold() for anchor in anchors):
                found_anchor = True
                # The preceding sentence can define a referent and the next can
                # qualify the answer, even without repeating the action's item.
                keep.update(range(max(0, index - 1), min(len(sentences), index + 2)))
            elif _CONTEXT_CUE.search(sentence):
                keep.add(index)
        for index in sorted(keep):
            left, right = sentences[index]
            if spans and spans[-1][1] == left:
                spans[-1] = (spans[-1][0], right)
            else:
                spans.append((left, right))
    # An explicitly bound turn without a lexical anchor may still be the
    # learner's answer or a prompt. Require the full turn in that case.
    if not found_anchor or any(_CROSS_REFERENCE.search(text[left:right])
                               or text[left:right].rstrip().endswith((":", "：")) for left, right in spans):
        return [(0, len(text))]
    return spans


def _unread(length: int, ranges: list[list[int]], reason: str) -> list[dict[str, Any]]:
    unread, start = [], 0
    for left, right in [*ranges, [length, length]]:
        if start < left:
            unread.append({"utf8_range": [start, left], "reason": reason})
        start = right
    return unread


def select_evidence(refs: list[dict[str, Any]], anchors: tuple[str, ...], limit: int, *,
                    required_quotes: tuple[str, ...] = ()):
    """Fit every required turn or span together; record exact UTF-8 coverage."""
    sources, problems = [], set()
    for ref in refs:
        binding, node = ref["binding"], ref["value"]
        message = binding["resolved_type"] == "conversation_message"
        content = node["content"] if message else node
        text = content if isinstance(content, str) else (
            canonical_bytes(content).decode() if type(content) in {int, float, bool} else "")
        length = len(text.encode())
        source = {"binding": binding, "origin": ref.get("origin", "typed_evidence_ref"),
                  "length_bytes": length, "read_utf8_ranges": [], "unread": []}
        if message:
            source.update(sequence=node.get("sequence"), created_at=node.get("created_at"))
        reason = None
        if binding["resolved_type"] == "attachment":
            source["length_bytes"] = node.get("bytes")
            reason = "attachment_not_read_by_text_review"
        elif SECRET.search(text):
            reason = "restricted_text"
        elif not text.strip():
            reason = "no_readable_text"
        if reason:
            problems.add(reason)
        sources.append((source, text, reason))

    slice_messages = sum(len(text.encode()) for _, text, reason in sources if not reason) > limit
    excerpts, used = [], 0
    for source, text, reason in sources:
        length = source["length_bytes"]
        if reason:
            source["unread"] = [{"utf8_range": [0, length] if length else None, "reason": reason}]
            continue
        binding = source["binding"]
        spans = _message_excerpt(text, anchors) if slice_messages else [(0, len(text))]
        previous, position = 0, 0
        for start, end in spans:
            excerpt = text[start:end]
            left = position + len(text[previous:start].encode())
            size = len(excerpt.encode())
            previous, position = end, left + size
            ranges = source["read_utf8_ranges"]
            ranges.append([left, position])
            excerpts.append({"package_id": binding["package_id"], "pointer": binding["pointer"],
                             "source_sha256": binding["resolved_node_sha256"], "role": binding["resolved_role"],
                             "sequence": source.get("sequence"), "created_at": source.get("created_at"),
                             "utf8_range": ranges[-1], "text": excerpt})
            used += size
        source["unread"] = _unread(length, source["read_utf8_ranges"], "outside_anchored_context")

    if used > limit:
        problems.add("required_evidence_exceeds_byte_budget")
    elif required_quotes:
        delivered = [normalize_sentence(excerpt["text"]).casefold() for excerpt in excerpts]
        if any(not any(normalize_sentence(quote).casefold() in text for text in delivered) for quote in required_quotes):
            problems.add("required_action_quote_not_delivered")
    if not excerpts:
        problems.add("unresolved_without_text")
    if problems:
        # No excerpt is sent as a complete account of this evidence.
        for source, _, reason in sources:
            if not reason:
                source["read_utf8_ranges"] = []
                reason = ("required_evidence_exceeds_byte_budget" if used > limit else "required_evidence_incomplete")
                source["unread"] = _unread(source["length_bytes"], [], reason)
        excerpts = []
    return excerpts, [source for source, _, _ in sources], sorted(problems)
