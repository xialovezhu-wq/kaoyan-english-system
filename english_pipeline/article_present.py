"""English consumer of the owner-published study-display/v1 interface."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from .article_service import HEALTH, descriptor, health_fields, origin, read_http
from .article_source import checked_key
from .article_workspace import PROTOCOL, scope
from .errors import ValidationError
from .util import atomic_write_json, exclusive_lock, load_json

CHECKS = ("saved","served","rendered","placed")
REQUEST_FIELDS = ("protocol","subject","entity_key","relative_path","focus_anchor","operation_id",
                  "expected_revision","expected_scope","present_mode")
RECEIPT_FIELDS = ("protocol","subject","entity_key","operation_id","canonical_url","requested_revision",
                  "observed_revision","checks","errors")
# Structurally validate both ends; pin the owner's canonical schema bytes when published.
SCHEMA = {"protocol":PROTOCOL, "subjects":["math","english","cs408"],
          "request_fields":list(REQUEST_FIELDS), "receipt_fields":list(RECEIPT_FIELDS),
          "present_modes":["update_only","present"],"checks":list(CHECKS),"check_values":["pass","fail","not_checked"]}


def display_home():
    return Path(os.environ.get("STUDY_DISPLAY_HOME", Path.home() / "Library/Application Support/KaoyanStudyDisplay")).expanduser()


def manifest(home):
    path = home / "ready.json"
    if not path.is_file():
        return None
    data = load_json(path)
    if data.get("status") != "ready":
        return None
    if data.get("protocol") != PROTOCOL or data.get("owner") != "global-browser-project":
        raise ValidationError("incompatible_contract")
    digest = data.get("contract_hash")
    argv = (data.get("invocation") or {}).get("argv")
    if (not isinstance(digest,str) or not re.fullmatch("[a-f0-9]{64}", digest)
            or not isinstance(argv,list) or not argv or not all(isinstance(arg,str) and arg and "\x00" not in arg for arg in argv)
            or not Path(argv[0]).is_absolute() or not os.access(argv[0], os.X_OK)
            or not Path(data["endpoint_dir"]).is_absolute() or not Path(data["display_config_path"]).is_absolute()):
        raise ValidationError("incompatible_contract")
    schema = SCHEMA
    candidate = data.get("contract_schema_path") or data.get("schema_path") or data.get("contract_path")
    if candidate:
        schema = load_json(Path(candidate))
    else:
        for name in ("contract-schema.json","contract.schema.json","contract.json","schema.json"):
            if (home / name).is_file():
                schema = load_json(home / name)
                break
    canonical = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",",":")).encode()
    if digest != hashlib.sha256(canonical).hexdigest():
        raise ValidationError("incompatible_contract_hash")
    if schema != SCHEMA:
        definitions = schema.get("$defs", {})
        pairs = (("request", REQUEST_FIELDS), ("receipt", RECEIPT_FIELDS),
                 ("scope", ("source_version","attempt_id","access_epoch")))
        for name, fields in pairs:
            spec = definitions.get(name, {})
            if not set(fields) <= set(spec.get("properties", {})) or not set(spec.get("required", [])) <= set(fields):
                raise ValidationError("incompatible_contract_schema")
    return data


def register(rec, home, ready):
    if read_http(rec, HEALTH) != health_fields(rec["instance_id"]):
        raise ValidationError("service_identity_mismatch")
    folder = Path(ready["endpoint_dir"]) if ready else home / "endpoints"
    path = folder / "english.json"
    desc = descriptor(rec)
    if not path.exists() or load_json(path) != desc:
        atomic_write_json(path, desc)
    return path


def chat_link(root, key, ready):
    # Read the current owner launcher as argv. Its link command never selects a tab.
    checked_key(key)
    argv = (ready or {}).get("link_argv")
    if not isinstance(argv,list) or not argv or not all(isinstance(a,str) and a and "\x00" not in a for a in argv):
        raise ValidationError("chat_link_pending")
    if not Path(argv[0]).is_absolute() or not os.access(argv[0],os.X_OK):
        raise ValidationError("invalid_chat_link_launcher")
    result = subprocess.run([*argv,"link","english",key],text=True,capture_output=True,timeout=8)
    url = result.stdout.strip()
    parsed = urlsplit(url)
    if (result.returncode or parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not parsed.port or parsed.username or parsed.password or not parsed.path.startswith("/")
            or any(c.isspace() for c in url)):
        raise ValidationError("invalid_chat_link_receipt")
    directory = (root / "synthetic" if key.startswith("SYN-") else root) / "links"
    with exclusive_lock(directory / (key+".lock")):
        path = directory / (key+".json")
        links = load_json(path)["chat_links"] if path.exists() else []
        if url not in links:
            atomic_write_json(path,{"entity_key":key,"chat_links":[*links,url]})
    return url


def present(rec, root, state, *, mode="update_only", operation_id, anchor=None, home=None):
    key = checked_key(state["entity_key"])
    checked_key(operation_id)
    route = descriptor(rec)["entity_route_template"].format(entity_key=key)
    req = {"protocol":PROTOCOL,"subject":"english","entity_key":key,"relative_path":route,
           "focus_anchor":anchor,"operation_id":operation_id,"expected_revision":state["revision"],
           "expected_scope":scope(state),"present_mode":mode}
    reply = {k:req[k] for k in ("protocol","subject","entity_key","operation_id")}
    reply.update(canonical_url=origin(rec)+route,requested_revision=state["revision"],observed_revision=None,
                 checks={k:"not_checked" for k in CHECKS},errors=[])
    def fail(code):
        return {**reply,"errors":[code]}
    if mode not in ("update_only","present"):
        return fail("invalid_present_mode")
    try:
        actual = read_http(rec,route + "/revision")
        if scope(actual) != req["expected_scope"]:
            return fail("scope_expired")
        if actual["revision"] != state["revision"] or actual["content_hash"] != state["content_hash"]:
            return fail("revision_conflict")
        reply.update(observed_revision=actual["revision"])
        reply["checks"].update(saved="pass",served="pass")
        if anchor:
            safe = read_http(rec,route + "/projection")
            if anchor not in [s["id"] for s in safe["sections"] if "text" in s or "tabs" in s]:
                return fail("focus_outside_scope")
        if (root / "external-disabled").exists():
            return fail("external_display_disabled")
        home = home or display_home()
        ready = manifest(home)
        register(rec, home, ready)
        if ready is None:
            return fail("integration_pending")
        result = subprocess.run(ready["invocation"]["argv"],input=json.dumps(req),text=True,capture_output=True,timeout=35)
        observed = json.loads(result.stdout)
        for k in ("protocol","subject","entity_key","operation_id","requested_revision","canonical_url"):
            if observed.get(k) != reply[k]:
                return fail("invalid_opener_receipt")
        checks = observed.get("checks")
        if (not isinstance(checks,dict) or any(checks.get(k) not in {"pass","fail","not_checked"} for k in CHECKS)
                or not isinstance(observed.get("errors"),list)):
            return fail("invalid_opener_receipt")
        if not observed["errors"] and (result.returncode != 0 or observed.get("observed_revision") != actual["revision"]):
            return fail("invalid_opener_receipt")
        if ready.get("link_argv"):
            try:
                observed["chat_link"] = chat_link(root,key,ready)
            except (ValidationError,OSError,subprocess.SubprocessError):
                observed["chat_link_status"] = "unavailable"
        return observed
    except ValidationError as exc:
        return fail(str(exc))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        return fail("service_unavailable")
