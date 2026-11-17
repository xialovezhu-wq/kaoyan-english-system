"""Explicit raw-source review for teaching summaries, separate from formal intake."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import captured_reference_text, file_sha256, learner_message_spans, object_sha256

REVIEW_SCHEMA = "english_summary_source_review_v1"
SOURCE_KINDS = {"learner_dialogue", "learner_record", "assistant_explanation", "formal_advice", "source_text"}
_MESSAGE = re.compile(r"/messages/\d+(?:/content)?")


class SummarySourceReview:
    def __init__(self, repo: Path, manifest: dict[str, Any], document: dict[str, Any]):
        if (manifest.get("schema_version") != REVIEW_SCHEMA
                or manifest.get("purpose") != "explicit_summary_maintenance"
                or manifest.get("summary_input_sha256") != object_sha256(document)):
            raise ValidationError("raw summary review must bind the exact reviewed input")
        self.repo = repo
        self.review_id = object_sha256(manifest)
        self.sources = {}
        self.contents = {}
        self.reference_text = {}
        for source in manifest.get("sources", []):
            if (source.get("kind") not in SOURCE_KINDS or not isinstance(source.get("locators"), list)
                    or not source["locators"] or any(not isinstance(x, str) for x in source["locators"])):
                raise ValidationError("review source requires a kind and reviewed locators")
            path = (repo / source["path"]).resolve(strict=True)
            if path in self.sources:
                raise ValidationError("duplicate review source")
            if file_sha256(path) != source.get("sha256"):
                raise ValidationError("reviewed original source changed")
            self.sources[path] = source
        if not self.sources:
            raise ValidationError("raw summary review requires explicitly read original sources")

    def read_locator(self, path: Path, locator: str) -> Any:
        if path not in self.contents:
            self.contents[path] = path.read_text(encoding="utf-8")
        content = self.contents[path]
        match = re.fullmatch(r"lines/(\d+)-(\d+)", locator)
        if match:
            lines = content.splitlines()
            start, end = map(int, match.groups())
            if not 1 <= start <= end <= len(lines):
                raise ValidationError("review source line range is outside the original")
            return "\n".join(lines[start - 1:end])
        if not locator.startswith("/"):
            raise ValidationError("review source requires an exact JSON pointer or line range")
        try:
            value = json.loads(content)
            for part in locator[1:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                value = value[int(part)] if isinstance(value, list) and part.isdigit() else value[part]
            return value
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ValidationError("review source locator does not resolve") from exc

    def validate_episode(self, episode: dict[str, Any], *, assertion: str | None = None) -> None:
        has_learner_evidence = False
        reference_only = False
        quoted_assertion = authored_assertion = False
        reviewed_dates = set()
        for ref in episode["evidence"]:
            path = (self.repo / ref["path"]).resolve(strict=True)
            source = self.sources.get(path)
            if not source or ref.get("sha256") != source["sha256"]:
                raise ValidationError("summary cites an original outside its reviewed source manifest")
            reviewed_dates.update(source.get("study_dates", []))
            locators = ref.get("locators", [ref.get("locator")])
            if not isinstance(locators, list) or not locators:
                raise ValidationError("summary evidence requires explicit locators")
            for locator in locators:
                if locator not in source["locators"]:
                    raise ValidationError("summary locator was not included in the explicit source review")
                value = self.read_locator(path, locator)
                text = ""
                if ref.get("quote") or assertion:
                    text = (value if isinstance(value, str) else value["content"]
                            if isinstance(value, dict) and isinstance(value.get("content"), str)
                            else json.dumps(value, ensure_ascii=False))
                if ref.get("quote") and ref["quote"] not in text:
                    raise ValidationError("summary quote does not occur at its original locator")
                if source["kind"] in {"learner_dialogue", "learner_record"} and _MESSAGE.fullmatch(locator):
                    message = self.read_locator(path, locator.removesuffix("/content"))
                    if not isinstance(message, dict) or message.get("role") != "user":
                        continue
                    if path not in self.reference_text:
                        self.reference_text[path] = captured_reference_text(path)
                    spans = learner_message_spans(message, self.reference_text[path])
                    if assertion:
                        owned = any(assertion in span for span in spans)
                        authored_assertion |= owned
                        quoted_assertion |= assertion in text and not owned
                    if spans == (message.get("content"),):
                        has_learner_evidence = True
                    elif spans and any(episode["first_attempt"] in part for part in spans):
                        has_learner_evidence |= not ref.get("quote") or any(ref["quote"] in part for part in spans)
                    else:
                        reference_only = True
                elif source["kind"] == "learner_record":
                    relative = path.relative_to(self.repo.resolve()) if path.is_relative_to(self.repo.resolve()) else path
                    is_reference = (path.name in {"source.json", "conversation.json", "manifest.json"}
                                    or relative.parts[:2] in {("raw", "articles"), ("raw", "protected"), ("raw", "reference_sources")})
                    reference_only |= is_reference
                    has_learner_evidence |= not is_reference
                    if assertion and assertion in text:
                        quoted_assertion |= is_reference
                        authored_assertion |= not is_reference
                elif source["kind"] == "source_text":
                    reference_only = True
                    quoted_assertion |= bool(assertion and assertion in text)
            if ref.get("study_date") and source.get("study_dates") and ref["study_date"] not in source["study_dates"]:
                raise ValidationError("summary must retain a reviewed original study date")
        if reviewed_dates and episode.get("study_date") not in reviewed_dates:
            raise ValidationError("summary must retain a reviewed original study date")
        if not has_learner_evidence:
            if reference_only:
                raise ValidationError("an external reference alone cannot establish a learner observation")
            raise ValidationError("formal advice or assistant explanation alone cannot establish a learner observation")
        if quoted_assertion and not authored_assertion:
            raise ValidationError("an external reference cannot confirm a claim without the learner's own assertion")
