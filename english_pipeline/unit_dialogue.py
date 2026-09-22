"""Export actual visible messages for an explicitly selected learning unit."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import IdempotencyConflict, ValidationError
from .util import canonical_bytes, object_sha256


def export_unit_dialogue(rollout: Path, *, turn_ids: list[str], output: Path) -> dict[str, Any]:
    """No model, inferred transcript, package write, or learning-state update.

    Only response_item messages are exported, so duplicated event_msg projections,
    tools, developer instructions and private model reasoning are never included.
    Non-text content is retained verbatim in message metadata for attachment intake.
    """
    if not turn_ids or len(set(turn_ids)) != len(turn_ids):
        raise ValidationError("select each actual turn ID exactly once")
    selected = set(turn_ids)
    found: list[str] = []
    messages = []
    thread_id = None
    current = None
    non_text_count = 0
    with rollout.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            payload = row.get("payload", {})
            kind = row.get("type")
            if kind == "session_meta":
                thread_id = payload.get("id")
            if kind == "event_msg" and payload.get("type") == "task_started":
                current = payload.get("turn_id")
                if current in selected:
                    found.append(current)
            if kind == "turn_context":
                current = payload.get("turn_id", current)
                if current in selected and current not in found:
                    found.append(current)
            if current not in selected or kind != "response_item" or payload.get("type") != "message":
                continue
            if payload.get("role") not in {"user", "assistant"}:
                continue
            blocks = payload.get("content", [])
            if not isinstance(blocks, list):
                raise ValidationError("rollout message content is not a block array")
            texts = [b["text"] for b in blocks if b.get("type") in {"input_text", "output_text", "text"}]
            nontext = [b for b in blocks if b.get("type") not in {"input_text", "output_text", "text"}]
            content = "\n".join(texts)
            if not content and not nontext:
                continue
            non_text_count += len(nontext)
            messages.append({"role": payload["role"], "content": content,
                             "message_id": payload.get("id") or f"{current}:line-{line_number}",
                             "created_at": row["timestamp"],
                             "metadata": {"turn_id": current, "rollout_line": line_number,
                                          "phase": payload.get("phase"),
                                          "original_content_blocks": blocks}})
    if not thread_id or found != turn_ids:
        raise ValidationError("turn selection is missing, duplicated or out of original order")
    if not messages or not any(m["role"] == "user" for m in messages):
        raise ValidationError("selected unit has no actual user messages")
    doc = {"schema_version": "english_unit_dialogue_v1", "thread_ref": "codex-thread:" + thread_id,
           "turn_ids": found, "messages": messages, "non_text_block_count": non_text_count,
           "evidence_sha256": object_sha256(messages)}
    data = canonical_bytes(doc)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.read_bytes() != data:
            raise IdempotencyConflict("unit dialogue output already contains different evidence")
    else:
        with output.open("xb") as handle:
            handle.write(data)
    return {"status": "exported", "path": str(output.resolve()), "thread_ref": doc["thread_ref"],
            "turn_ids": found, "message_count": len(messages), "non_text_block_count": non_text_count,
            "evidence_sha256": doc["evidence_sha256"], "formal_write_count": 0,
            "capture_created": False}
