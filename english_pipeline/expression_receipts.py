"""Thread-owned, derived display receipts for the English copy/paste route."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import re
import sqlite3
import sys
from uuid import uuid4

from .constants import DEFAULT_STATE_DIR, REPO_ROOT
from .errors import ValidationError
from .util import atomic_write_json, canonical_bytes, exclusive_lock, load_json, object_sha256, text_sha256


DISPLAY_ROOT = DEFAULT_STATE_DIR / "display-assets" / "expression-comparisons"
RECEIPT_SCHEMA = "english_expression_display_v1"
_SCENE_REF = re.compile(r"EN-EXPR-[a-f0-9]{32}")


class Refusal(ValidationError):
    def __init__(self, status, reason_code):
        super().__init__(reason_code)
        self.status, self.reason_code = status, reason_code


def live_session(asserted_id):
    session = os.environ.get("CODEX_THREAD_ID", "")
    if not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", session):
        raise Refusal("unavailable", "CURRENT_SESSION_UNAVAILABLE")
    if asserted_id is not None and asserted_id != session:
        raise Refusal("mismatch", "SESSION_MISMATCH")
    return session


def event_ref(row):
    return {"message_id": row["message_id"], "turn_id": row["metadata"]["turn_id"],
            "rollout_line": row["metadata"]["rollout_line"]}


class Conversation:
    """Use the native visible-message reader and its actual turn/line boundaries."""
    def __init__(self, session):
        from .current_unit import locate_rollout, read_messages

        self.turn_id = None
        self.closed_turn = None
        self.last_line = 0
        self.final_lines = set()
        try:
            self.messages = read_messages(locate_rollout(session), session, tool_trace=self)
        except (OSError, sqlite3.Error, ValidationError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise Refusal("unavailable", "CURRENT_CONVERSATION_UNAVAILABLE") from exc
        self.users = [row for row in self.messages if row["role"] == "user"]
        if (not self.users or not self.turn_id or self.closed_turn == self.turn_id
                or self.users[-1]["metadata"]["turn_id"] != self.turn_id):
            raise Refusal("unavailable", "CURRENT_TURN_UNAVAILABLE")

    def observe(self, kind, payload, number, turn):
        self.turn_id, self.last_line = turn, number
        if kind == "event_msg" and payload.get("type") in ("task_complete", "turn_aborted"):
            self.closed_turn = payload.get("turn_id") or turn
        if (kind == "response_item" and payload.get("type") == "message"
                and payload.get("role") == "assistant" and payload.get("recipient") in (None, "all")
                and payload.get("phase") in (None, "final", "final_answer")
                and payload.get("channel") in (None, "final")):
            self.final_lines.add(number)

    def current_user(self, expected_id=None):
        row = self.users[-1]
        if expected_id is not None and expected_id != row["message_id"]:
            raise Refusal("mismatch", "CURRENT_MESSAGE_MISMATCH")
        if sum(item["message_id"] == row["message_id"] for item in self.messages) != 1:
            raise Refusal("unavailable", "CURRENT_MESSAGE_AMBIGUOUS")
        return row

    def preceding_display(self, receipt):
        if len(self.users) < 2:
            raise Refusal("unavailable", "DISPLAY_MESSAGE_UNAVAILABLE")
        origin = self.users[-2]
        if (event_ref(origin) != receipt["originating_message"]
                or text_sha256(origin["content"]) != receipt["originating_message_sha256"]
                or origin["metadata"]["turn_id"] == self.turn_id):
            raise Refusal("stale", "DISPLAY_TURN_CHANGED")
        selection_line = self.users[-1]["metadata"]["rollout_line"]
        for row in reversed(self.messages):
            line = row["metadata"]["rollout_line"]
            if line <= receipt["observed_through_line"]:
                break
            if line < selection_line and row["role"] == "assistant":
                if line in self.final_lines and row["metadata"]["turn_id"] == receipt["originating_turn_id"]:
                    return row["content"]
                break
        raise Refusal("unavailable", "DISPLAY_MESSAGE_UNAVAILABLE")


def _controlled(path):
    # The root comes from the installed module. Neither route accepts a storage path.
    if not path.is_relative_to(DISPLAY_ROOT) or path.resolve() != path:
        raise Refusal("unavailable", "TRUSTED_RECEIPT_UNAVAILABLE")
    return path


def _load_receipt(folder, session, expected_id=None):
    try:
        pointer = load_json(_controlled(folder / "current.json"))
        receipt_id = pointer["receipt_id"]
        if not isinstance(receipt_id, str) or not _SCENE_REF.fullmatch(receipt_id):
            raise ValueError("invalid current receipt ID")
        receipt = load_json(_controlled(folder / f"{receipt_id}.json"))
        digest = receipt.pop("receipt_content_sha256")
        comparison = receipt["comparison"]
        if (digest != pointer["receipt_content_sha256"] or digest != object_sha256(receipt)
                or receipt["schema_version"] != RECEIPT_SCHEMA or receipt["session_id"] != session
                or receipt["receipt_id"] != receipt_id or receipt["stage"] != comparison["stage"]
                or receipt["source_binding"] != comparison["source_binding"]
                or receipt["originating_turn_id"] != receipt["originating_message"]["turn_id"]):
            raise ValueError("inconsistent current display receipt")
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise Refusal("unavailable", "TRUSTED_RECEIPT_UNAVAILABLE") from exc
    if expected_id is not None and expected_id != receipt_id:
        raise Refusal("stale", "SCENE_REPLACED")
    return receipt


@contextmanager
def current_display(asserted_session_id, scene_ref):
    session = live_session(asserted_session_id)
    if scene_ref is not None and (not isinstance(scene_ref, str) or not _SCENE_REF.fullmatch(scene_ref)):
        raise Refusal("mismatch", "INVALID_SCENE_REF")
    folder = _controlled(DISPLAY_ROOT / session)
    try:
        handle = _controlled(folder / "scene.lock").open("rb")
    except OSError as exc:
        raise Refusal("unavailable", "TRUSTED_RECEIPT_UNAVAILABLE") from exc
    with handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        yield _load_receipt(folder, session, scene_ref)


def save_display(args):
    from .expression_comparison import render_comparison
    from .packages import _write_immutable

    session = live_session(args.session_id)
    if (args.rollout is not None or args.input_json != "-" or args.repo_root.resolve() != REPO_ROOT
            or args.state_dir.resolve() != DEFAULT_STATE_DIR):
        raise Refusal("mismatch", "DISPLAY_PATH_OVERRIDE_FORBIDDEN")
    data = json.load(sys.stdin)
    folder = _controlled(DISPLAY_ROOT / session)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with exclusive_lock(_controlled(folder / "scene.lock")):
        conversation = Conversation(session)
        origin = conversation.current_user(args.origin_message_id)
        anchor = event_ref(origin)
        pointer = _controlled(folder / "current.json")
        if pointer.exists():
            previous = _load_receipt(folder, session, args.replaces_scene_ref)
            previous_line = previous["originating_message"]["rollout_line"]
            if previous_line > anchor["rollout_line"]:
                raise Refusal("stale", "SCENE_REPLACED")
            if previous_line == anchor["rollout_line"] and not args.replaces_scene_ref:
                return {"status": "stale", "reason_code": "SCENE_REPLACED",
                        "current_scene_ref": previous["receipt_id"], "invalidate_prior_selection": True,
                        "formal_write_count": 0, "learning_event_write_count": 0}
        elif args.replaces_scene_ref is not None:
            raise Refusal("unavailable", "TRUSTED_RECEIPT_UNAVAILABLE")

        receipt_id = "EN-EXPR-" + uuid4().hex
        result = render_comparison(REPO_ROOT, data, lambda: conversation.messages, display_id=receipt_id)
        if args.origin_message_id is None:
            witnesses = [result.get("first_attempt_message_id"), result.get("followup_message_id")]
            if result["stage"] == "direct_explanation":
                witnesses.append(data["direct_request"]["message_id"])
            if result["source_binding"]["source_kind"] == "user_provided":
                witnesses.append(data["source"]["original_message"]["message_id"])
            if origin["message_id"] not in witnesses:
                raise Refusal("unavailable", "ORIGIN_MESSAGE_REQUIRED")
        states = result.get("copy_states", [])
        scene = states[0]["scene_version"] if states else object_sha256([receipt_id, result])
        receipt = {"schema_version": RECEIPT_SCHEMA, "receipt_id": receipt_id, "session_id": session,
                   "originating_turn_id": anchor["turn_id"], "originating_message": anchor,
                   "originating_message_sha256": text_sha256(origin["content"]),
                   "observed_through_line": conversation.last_line, "source_binding": result["source_binding"],
                   "stage": result["stage"], "scene_version": scene, "comparison": result}
        if result["source_binding"]["source_kind"] == "user_provided":
            original = data["source"]["original_message"]
            receipt["original_message"] = {key: original[key] for key in ("message_id", "quote")}
        witness_key = {"after_translation": "first_attempt", "direct_explanation": "direct_request"}.get(result["stage"])
        if witness_key:
            receipt["stage_message"] = {key: data[witness_key][key] for key in ("message_id", "quote")}
        digest = object_sha256(receipt)
        _write_immutable(_controlled(folder / f"{receipt_id}.json"),
                         canonical_bytes({**receipt, "receipt_content_sha256": digest}))
        ref = {"receipt_id": receipt_id, "receipt_content_sha256": digest}
        atomic_write_json(pointer, ref)
        return {**result, "scene_ref": receipt_id, "scene_version": scene, "display_receipt": ref}
