"""Versioned, per-article display store. Only the local tutor CLI can mutate it."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .article_sections import LABELS, new_section, prepare_block
from .article_source import checked_key, originals, read_article
from .errors import PipelineError, ValidationError
from .util import object_sha256

PROTOCOL = "study-display/v1"
SERVICE_VERSION = "english-article/1.9.0"
RUNTIME = Path.home() / "Library/Application Support/KaoyanEnglishArticle"
SCOPE = ("source_version", "attempt_id", "access_epoch")
SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions(
    revision INTEGER PRIMARY KEY, attempt TEXT NOT NULL, epoch TEXT NOT NULL,
    op TEXT NOT NULL, request_hash TEXT NOT NULL, manifest TEXT NOT NULL,
    session TEXT NOT NULL, turn TEXT NOT NULL, written_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS operations ON revisions(attempt, epoch, op);
CREATE INDEX IF NOT EXISTS capture_turn ON revisions(session, turn);
CREATE TABLE IF NOT EXISTS sections(
    unit TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(unit, revision)
);
"""


def scope(state):
    return {k: state[k] for k in SCOPE}


def metadata(state):
    return dict(entity_key=state["entity_key"], **scope(state), revision=state["revision"],
                content_hash=object_sha256(state))


def path_for(root, key):
    return root / "articles" / (checked_key(key) + ".sqlite3")


@contextmanager
def database(root, key, *, write=False, create=False):
    path = path_for(root, key)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not path.is_file():
        raise ValidationError("article_unavailable")
    mode = "rwc" if create else ("rw" if write else "ro")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True, timeout=6)
    try:
        if create:
            db.executescript(SCHEMA)
        if write:
            db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    finally:
        db.close()


def head(db):
    row = db.execute("SELECT manifest FROM revisions ORDER BY revision DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def saved_section(db, state, uid):
    rev = state["sections"].get(uid)
    if rev is None:
        return new_section()
    row = db.execute("SELECT body FROM sections WHERE unit=? AND revision=?", (uid, rev)).fetchone()
    body = json.loads(row[0])
    if object_sha256(body) != state["section_hashes"].get(uid):
        raise ValidationError("section_integrity_failure")
    return body


def verified(repo, root, state):
    try:
        source = read_article(repo, root, state["entity_key"])
    except (OSError, PipelineError, ValueError, KeyError) as exc:
        raise ValidationError("source_changed") from exc
    if source["source_version"] != state["source_version"]:
        raise ValidationError("source_changed")
    return source


def require_scope(state, req):
    if any(req.get(k) != state[k] for k in SCOPE):
        raise ValidationError("scope_expired")


def require_revision(state, req):
    if type(req.get("expected_revision")) is not int or req["expected_revision"] != state["revision"]:
        raise ValidationError("revision_conflict")


def initialize(article, ctx, data, previous=None):
    uid = data.get("active_unit_id", next(iter(article["sections"])))
    allowed = originals(article, data.get("originals", []), uid)
    return {"entity_key":article["binding"]["source_id"], "binding":article["binding"],
            "source_version":article["source_version"], "attempt_id":uuid4().hex,
            "access_epoch":uuid4().hex, "session":ctx.session, "start_line":ctx.current_user()["metadata"]["rollout_line"],
            "active_unit_id":uid, "originals":allowed, "sections":{}, "section_hashes":{},
            "revision":previous["revision"] + 1 if previous else 1}


def apply(repo, root, ctx, data):
    from .article_source import supplied_material
    action = data.get("operation")
    key = data.get("entity_key")
    if action == "open" and "original_message" in data:
        if key:
            raise ValidationError("temporary_identity_is_derived")
        key = supplied_material(root, ctx.messages, ctx.session, data["original_message"])
    checked_key(key)
    if action not in ("open", "reset", "patch", "map_catalog") or data.get("subject", "english") != "english" or (
            data.get("protocol", PROTOCOL) != PROTOCOL):
        raise ValidationError("invalid_display_operation")
    if action == "open":
        article = read_article(repo, root, key)
        key = article["binding"]["source_id"]
    op = checked_key(data.get("operation_id"))
    signature = object_sha256(data)
    with database(root, key, write=True, create=action == "open") as db:
        state = head(db)
        if state and action == "open":
            verified(repo, root, state)
            if state["session"] != ctx.session:
                raise ValidationError("new_attempt_required")
            previous = db.execute("SELECT attempt,epoch,request_hash FROM revisions WHERE op=? ORDER BY revision DESC LIMIT 1", (op,)).fetchone()
            if previous and tuple(previous[:2]) != (state["attempt_id"],state["access_epoch"]):
                raise ValidationError("scope_expired")
            if previous and previous[2] != signature:
                raise ValidationError("operation_id_conflict")
            return {**metadata(state), "status":"resumed"}
        changed = {}
        if action == "open":
            state = initialize(article, ctx, data)
        else:
            if not state:
                raise ValidationError("article_unavailable")
            require_scope(state, data)
            if action == "reset":
                require_revision(state, data)
                article = read_article(repo, root, key)
                # A reset is an explicit rebinding, including a deliberate resegmentation.
                if data.get("new_source_version") != article["source_version"]:
                    raise ValidationError("current_source_version_required")
                state = initialize(article, ctx, data, state)
            else:
                if state["session"] != ctx.session:
                    raise ValidationError("scope_expired")
                article = verified(repo, root, state)
                prior = db.execute("SELECT request_hash, manifest FROM revisions WHERE attempt=? AND epoch=? AND op=?",
                                   (state["attempt_id"], state["access_epoch"], op)).fetchone()
                if prior:
                    if prior[0] != signature:
                        raise ValidationError("operation_id_conflict")
                    return {**metadata(json.loads(prior[1])), "status":"idempotent"}
                require_revision(state, data)
                if action == "map_catalog":
                    if not key.startswith("TEMP-"):
                        raise ValidationError("temporary_material_required")
                    target = read_article(repo,root,checked_key(data["catalog_source_id"]))
                    mapped = data.get("unit_mapping")
                    if (target["binding"]["source_id"].startswith(("TEMP-","SYN-"))
                            or target["source_version"] != data.get("catalog_source_version")
                            or not isinstance(mapped,dict) or set(mapped) != set(article["sections"])
                            or len(set(mapped.values())) != len(mapped)
                            or any(target["sections"].get(other,{}).get("text_sha256") != article["sections"][uid]["text_sha256"]
                                   for uid,other in mapped.items())):
                        raise ValidationError("explicit_verified_sentence_mapping_required")
                    state["catalog_mapping"] = {"binding":target["binding"], "units":mapped}
                    state["revision"] += 1
                else:
                    changes = data.get("changes")
                    if not isinstance(changes, list) or not 1 <= len(changes) <= 64:
                        raise ValidationError("bounded_section_patch_required")
                    next_unit = data.get("active_unit_id", state["active_unit_id"])
                    allowed = originals(article, data.get("originals", []), next_unit)
                    state["originals"] = list(dict.fromkeys([*state["originals"], *allowed]))
                    state["revision"] += 1
                    for change in changes:
                        uid = change["unit"]
                        if uid not in state["originals"]:
                            raise ValidationError("original_outside_scope")
                        if uid not in changed:
                            changed[uid] = saved_section(db, state, uid)
                        prepare_block(ctx, state, changed[uid], change, article)
                    tabs = [c["tab"] for c in changes if c["unit"] == next_unit and c["role"] == "question_language"]
                    if tabs:
                        tab = data.get("active_tab", "STEM" if "STEM" in tabs else tabs[0])
                        if tab not in tabs:
                            raise ValidationError("active_language_tab_outside_patch")
                        changed[next_unit].update(active_tab=tab, tab_revision=state["revision"])
                    state["active_unit_id"] = next_unit
        for uid, body in changed.items():
            body["bound_original"] = article["sections"][uid]
            db.execute("INSERT INTO sections VALUES (?,?,?)", (uid, state["revision"], json.dumps(body, ensure_ascii=False)))
            state["sections"][uid] = state["revision"]
            state["section_hashes"][uid] = object_sha256(body)
        # The single transaction publishes scope, changed sections and replay receipt together.
        db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)",
                   (state["revision"], state["attempt_id"], state["access_epoch"], op, signature,
                    json.dumps(state, ensure_ascii=False), ctx.session, ctx.turn_id, time.time()))
        result = metadata(state)
    return {**result, "status":"saved", "committed_ns":time.time_ns()}


def public_blocks(section, binding, uid):
    """Whitelist the public projection. No client-supplied stage/grant is consulted."""
    question = uid.startswith("Q")
    if not question and section["stage"] == "before_translation":
        return []
    blocks = []
    for block in section["blocks"]:
        role = block["role"]
        if role not in LABELS or question != (role in {"answer", "question_attempt", "question_language", "review_figure"}):
            continue
        if role in {"answer","review_figure"}:
            grant = section["grant"]
            if not grant or grant["source_binding"] != binding or grant["unit"] != uid or block["grant"] != object_sha256(grant):
                continue
        safe = {k:block[k] for k in ("id","role","actor","text","tab","hint_dependence","transcribed","reason","figure")
                if k in block}
        safe["label"] = LABELS[role]
        if role == "comparison":
            safe["suggestions"] = [{k:s[k] for k in ("style","text","meaning_relation","basis")} for s in block["suggestions"]]
        blocks.append(safe)
    return blocks


def project(repo, root, key, *, since=None, epoch=None, meta_only=False):
    with database(root, key) as db:
        state = head(db)
        if not state:
            raise ValidationError("article_unavailable")
        article = verified(repo, root, state)
        meta = metadata(state)
        if meta_only:
            return {**meta, "focus_anchors":state["originals"]}
        # Only a manifest in the current scope qualifies for a delta.
        old = None
        if epoch == state["access_epoch"] and since is not None and 0 <= since <= state["revision"]:
            row = db.execute("SELECT manifest FROM revisions WHERE revision=? AND epoch=?", (since, epoch)).fetchone()
            old = json.loads(row[0]) if row else None
        changed = []
        for uid, src in article["sections"].items():
            visible = uid in state["originals"]
            if old and visible == (uid in old["originals"]) and state["sections"].get(uid) == old["sections"].get(uid):
                continue
            # Future questions are completely absent. Unreleased sentence text has only a position.
            if uid.startswith("Q") and not visible:
                continue
            sec = saved_section(db, state, uid) if visible else new_section()
            item = dict(id=uid, paragraph_id=src.get("paragraph_id"), stage=sec["stage"],
                        blocks=public_blocks(sec, article["binding"], uid))
            if visible:
                item.update({k:src[k] for k in ("text","tabs") if k in src})
                if sec.get("active_tab") in src.get("tabs", {}):
                    item.update(active_tab=sec["active_tab"], tab_revision=sec["tab_revision"])
                if sec.get("words") and ("tabs" in src or sec["stage"] != "before_translation"):
                    item["words"] = [{k: w[k] for k in ("tab", "source_form", "context_meaning",
                                                          "user_translation", "issue")} for w in sec["words"]]
            changed.append(item)
        return {**meta, "service_version":SERVICE_VERSION, "active_unit_id":state["active_unit_id"],
                "synthetic":key.startswith("SYN-"), "learning_evidence":False,
                "full":old is None, "sections":changed}
