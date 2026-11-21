"""The writing display stores lesson position and UI requests, never learner outcomes."""
from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from .article_workspace import metadata
from .util import atomic_write_bytes, atomic_write_json, exclusive_lock, load_json, object_sha256, file_sha256, utc_now
from .writing_contract import TASKS, VERSION, lesson, source_id
from .writing_materials import COURSE, EDITION, edition_path, inside, lines, require

KEY = re.compile(r"(?:SYN-)?WRITING-[a-f0-9]{24}")
STAGES = {"course":"跟课", "attempt":"独立练习", "guided":"提示与修订", "review":"本节回看"}
ACTIVE = "active"

def folder(root, key):
    require(KEY.fullmatch(key), "invalid writing session")
    return root / "writing" / key

def lesson_file(repo, sha):
    require(re.fullmatch(r"[0-9a-f]{64}", sha), "invalid lesson hash")
    return repo / COURSE / "lessons" / (sha + ".json")

def current(root, key):
    state = load_json(folder(root, key) / "current.json")
    require(state["entity_key"] == key, "wrong writing session")
    return state

def spec_for(repo, state):
    spec = load_json(lesson_file(repo, state["source_version"]))
    require(object_sha256(spec) == state["source_version"], "course lesson changed")
    return spec

def commit(directory, state):
    # Retain the exact tutor projection at each revision for later native Capture.
    state = {**state,"issued_at":utc_now()}
    revision_file = directory / "revisions" / f'{state["revision"]}.json'
    if revision_file.is_file():
        pending = load_json(revision_file)
        require(pending["entity_key"]==state["entity_key"] and pending["revision"]==state["revision"],"revision identity mismatch")
        # Finish a pointer update interrupted after the immutable revision was saved.
        atomic_write_json(directory/"current.json", pending)
        if pending.get("micro") and state.get("micro"):
            previous=pending["micro"]["events"]
            incoming=state["micro"]["events"]
            if (previous and incoming and previous[-1]["request_sha256"]==incoming[-1]["request_sha256"]
                    or not previous and not incoming and pending["source_version"]==state["source_version"]):
                return pending
        require({k:v for k,v in pending.items() if k!="issued_at"}=={k:v for k,v in state.items() if k!="issued_at"},
                "recovered saved revision; retry on its current version")
        return pending
    atomic_write_json(revision_file, state)
    atomic_write_json(directory / "current.json", state)
    return state

def standby(repo, root, key):
    directory = folder(root,key)
    catalog = load_json(edition_path(repo,EDITION))
    from .writing_plan import screen_plan
    schedule=screen_plan(repo)
    with exclusive_lock(directory/"display.lock"):
        old = current(root,key) if (directory/"current.json").exists() else None
        if old and old["status"]=="standby":
            if old.get("schedule")==schedule:return old
            return commit(directory,{**old,"revision":old["revision"]+1,"schedule":schedule})
        require(not old or old["status"] in {"closed","standby_closed"}, "resume the existing course instead")
        return commit(directory, dict(entity_key=key, revision=1 if not old else old["revision"]+1,
            source_version=catalog["source_manifest_sha256"],attempt_id=uuid4().hex,access_epoch=uuid4().hex,
            status="standby",lesson_id="COURSE-STANDBY",stage="standby",schedule=schedule))

def pause(root,key):
    directory=folder(root,key)
    with exclusive_lock(directory/"display.lock"):
        s=current(root,key)
        if s["status"] not in {ACTIVE,"standby"}:return s
        return commit(directory,{**s,"status":"suspended" if s["status"]==ACTIVE else "standby_closed",
                                 "access_epoch":uuid4().hex,"revision":s["revision"]+1})

def resume(root,key):
    directory=folder(root,key)
    with exclusive_lock(directory/"display.lock"):
        s=current(root,key)
        if s["status"] in {ACTIVE,"standby"}:return s
        require(s["status"] in {"suspended","standby_closed"},"course session unavailable")
        return commit(directory,{**s,"status":ACTIVE if s["status"]=="suspended" else "standby",
                                 "access_epoch":uuid4().hex,"revision":s["revision"]+1})

def release_page(repo, root, spec, permission):
    if permission is None:return None
    require(permission in {"lesson_page","worked_example"},"unknown page permission")
    src=spec["source"]
    if src["role"]=="worked_examples_with_answers":
        require(permission=="worked_example","full worked page requires its specific release")
    catalog=load_json(edition_path(repo,src["edition"]))
    bundle=Path(catalog["bundle"]).resolve(strict=True)
    record=next(r for r in lines(inside(bundle,"indexes/pages.jsonl")) if r["source_id"]==src["source_id"] and r["pdf_page"]==src["pdf_page"])
    require(record["source_sha256"]==src["sha256"],"course page edition changed")
    original=inside(bundle,record["page_image_path"])
    require(file_sha256(original)==record["page_image_sha256"],"changed course page image")
    target=root/"writing-pages"/(record["page_image_sha256"]+".webp")
    if not target.exists():atomic_write_bytes(target,original.read_bytes())
    require(file_sha256(target)==record["page_image_sha256"],"hot page hash mismatch")
    return {"sha256":record["page_image_sha256"],"permission":permission}

def page_bytes(root,key,epoch,sha):
    s=current(root,key)
    require(s["status"]==ACTIVE and s["stage"]!="attempt" and s["access_epoch"]==epoch and
            (s.get("page_release") or {}).get("sha256")==sha and re.fullmatch(r"[0-9a-f]{64}",sha),"scope_expired")
    path=root/"writing-pages"/(sha+".webp")
    require(file_sha256(path)==sha,"course image integrity failure")
    return path.read_bytes()

def enter(repo, root, key, request, *, expected=None):
    spec = lesson(repo, request)
    sha = object_sha256(spec)
    directory = folder(root, key)
    with exclusive_lock(directory / "display.lock"):
        prev = current(root, key) if (directory / "current.json").exists() else None
        if prev and prev["source_version"] == sha and prev["status"] == ACTIVE:
            return prev
        if prev and prev["status"] not in {"standby","standby_closed"}:
            require(expected == prev["revision"], "revision_conflict")
        path = lesson_file(repo, sha)
        if path.exists():
            require(load_json(path) == spec, "immutable lesson mismatch")
        else:
            atomic_write_json(path, spec)
        return commit(directory, dict(entity_key=key, revision=1 if not prev else prev["revision"]+1,
            source_version=sha, attempt_id=uuid4().hex, access_epoch=uuid4().hex, status=ACTIVE,
            lesson_id=spec["lesson_id"], stage="course", notes=[], templates=[], started_at=utc_now(),
            page_release=release_page(repo,root,spec,request.get("page_permission"))))

def update(repo, root, key, expected, stage, notes, *, page_permission=None, practice=None):
    directory = folder(root, key)
    with exclusive_lock(directory / "display.lock"):
        s = current(root,key)
        require(s["revision"] == expected and s["status"] == ACTIVE, "revision_conflict")
        require(not s.get("micro"),"use version-bound short writing feedback for this task")
        require(stage in STAGES and isinstance(notes, list) and len(notes) <= 8, "invalid writing stage or note")
        require(not notes or stage in {"guided","review"}, "independent view cannot include feedback")
        for n in notes:
            require(isinstance(n, dict) and set(n)-{"corrections"} in ({"title","body"},{"title","body","diagram"},{"title","body","diagram","figure"})
                    and all(isinstance(n[k],str) and 0<len(n[k])<=12000 for k in ("title","body")), "invalid note")
            if "corrections" in n:
                require(isinstance(n["corrections"],list) and all(isinstance(row,dict)
                        and set(row)=={"label","original","replacement","reason"}
                        and all(isinstance(v,str) and v.strip() for v in row.values())
                        for row in n["corrections"]), "invalid corrections")
            if "diagram" in n:
                from .article_figures import native_renderer, store_asset
                renderer = native_renderer(repo)
                _, fragment = renderer.sentence_fragment(n["diagram"], directory / "sentence.html")
                n["figure"] = store_asset(root / "figures", fragment.encode())
        spec = spec_for(repo, s)
        require(stage!="attempt" or page_permission is None,"practice cannot include a whole teaching page")
        task=s.get("practice") or {"phase":"first_draft","prompt":spec["prompt"]}
        if practice is not None:
            require(stage=="attempt" and isinstance(practice,dict) and set(practice)=={"phase","prompt"}
                    and practice["phase"] in {"first_draft","transfer"} and isinstance(practice["prompt"],str)
                    and practice["prompt"].strip(),"invalid practice task")
            require(practice["phase"]!="transfer" or practice["prompt"]!=spec["prompt"],"transfer requires a new context")
            task=practice
        page=release_page(repo,root,spec,page_permission)
        templates = template_cards(repo, spec) if stage == "review" else []
        return commit(directory, {**s, "access_epoch":uuid4().hex if practice is not None or stage!=s["stage"] or page!=s.get("page_release") else s["access_epoch"],
            "revision":expected+1, "stage":stage, "notes":notes, "templates":templates, "practice":task,"page_release":page, "status":ACTIVE})

def template_cards(repo, spec):
    path = repo / "intake" / "writing-view" / "current.json"
    if not path.exists():
        return []
    view = load_json(path)
    return [r for r in view["modules"] if r["asset_id"] in spec["assets"] and r["task_profile"] == spec["task_profile"]
            and r["source_id"] == source_id(spec)]

def projection(repo, root, key, *, meta_only=False):
    s = current(root, key)
    require(s["status"] in {ACTIVE,"standby"}, "scope_expired")
    meta = {**metadata(s), "presentation_profile":"english_writing", "focus_anchors":[]}
    if s.get("micro"):
        if meta_only:
            return meta
        from .writing_practice import projection as practice_projection
        return practice_projection(repo,root,s,meta)
    if s["status"]=="standby":
        meta["writing_standby"]=True
        if meta_only:return meta
        today=s.get("schedule") or {}
        catalog=load_json(edition_path(repo,EDITION))
        return {**meta, "schema":VERSION,"session_id":key,"lesson_id":s["lesson_id"],"stage":"standby","stage_label":"待开课",
                "course":"考研英语一 · 写作","section":today.get("section","作文随课资料"),"goal":today.get("goal","开课后，这里跟随老师正在讲的小节。"),
                "source":None,"video":None,"cards":today.get("cards",[]),"inventory":[{"id":k,"name":v["filename"],"pages":v["pages"]}
                    for k,v in catalog["sources"].items()]}
    spec = spec_for(repo,s)
    if meta_only:
        return meta
    src = spec["source"]
    if s["stage"]=="attempt" and (s.get("practice") or {}).get("phase")=="transfer":
        return {**meta, "schema":VERSION,"session_id":key,"lesson_id":s["attempt_id"],
                "course":"考研英语一","section":"写作练习","goal":"","source":None,"video":None,
                "stage":"attempt","stage_label":"独立练习","cards":[{"id":"task","title":"题目","body":s["practice"]["prompt"]}]}
    result = {**meta, "schema":VERSION, "session_id":key, "lesson_id":spec["lesson_id"], "course":spec["course"],
              "section":spec["section"], "goal":spec["goal"], "stage":s["stage"], "stage_label":STAGES[s["stage"]],
              "source":{k:src[k] for k in ("filename","sha256","pdf_page","source_id")},
              "video":spec["video"], "cards":[]}
    if s.get("page_release") and s["stage"]!="attempt":
        result["page_image"]=s["page_release"]["sha256"]
    cards = result["cards"]
    if s["stage"]=="guided" and s["notes"] and "corrections" in s["notes"][0]:
        note=s["notes"][0]
        result["cards"]=[{"id":"current-feedback",**{k:v for k,v in note.items() if k!="diagram"}}]
        return result
    def card(key, title, body):
        if body:
            cards.append(dict(id=key,title=title,body=body))
    card("task", TASKS[spec["task_profile"]][0], (s.get("practice") or {"prompt":spec["prompt"]})["prompt"] if s["stage"]=="attempt" else spec["task_constraint"])
    if s["stage"] in {"guided","review"}:
        for key,a in spec["assets"].items():
            card(key,a["lemma"]+" · "+a["sense"],"\n".join([a["collocation"],a["function"],a.get("usage",""),a.get("restriction","")]).strip())
            for link in a["reading_sources"]:
                card(key+"-"+link["unit_id"],link["reference_id"]+" · "+link["unit_id"],link["sentence"])
        for i,n in enumerate(s["notes"]):
            card("note-"+str(i),n["title"],n["body"])
            if n.get("figure"):
                cards[-1]["figure"] = n["figure"]
        for t in s["templates"]:
            card("template-"+t["asset_id"],"个人模板 · 第 "+str(t["version"])+" 版",
                 t["expression"]+"\n"+t["applies_when"]+"\n"+t["avoid_when"])
    return result

def actions(root, key, *, posted=None, ack=None):
    directory = folder(root,key)
    with exclusive_lock(directory / "display.lock"):
        s = current(root,key)
        require(s["status"] == ACTIVE,"scope_expired")
        path = directory / "actions.json"
        rows = load_json(path)["actions"] if path.exists() else []
        scope = dict(session_id=key, lesson_id=s["lesson_id"], payload_revision=s["revision"], access_epoch=s["access_epoch"])
        eligible = [r for r in rows if all(r.get(k)==v for k,v in scope.items())]
        if posted is not None:
            require(isinstance(posted,dict) and set(posted)==set(scope)|{"action_id","kind"}
                    and isinstance(posted["action_id"],str) and re.fullmatch(r"[0-9a-f-]{36}",posted["action_id"])
                    and posted["kind"]=="request_explanation" and all(posted[k]==v for k,v in scope.items()), "stale_or_invalid_action")
            existing = next((r for r in rows if r["action_id"]==posted["action_id"]),None)
            if existing:
                require(existing in eligible,"stale_or_invalid_action")
                return existing
            row = {**posted,"status":"queued"}
            atomic_write_json(path,{"actions":[*rows,row]})
            return row
        if ack:
            row = next((r for r in eligible if r["action_id"]==ack),None)
            require(row is not None,"stale_or_invalid_action")
            row["status"]="coach_read"
            atomic_write_json(path,{"actions":rows})
            return row
        return {"actions":eligible, **scope}

def rebuild(repo, state_root, receipt):
    """A derived and reproducible view; an empty or unrelated receipt touches nothing."""
    applied = [r for r in receipt["action_results"] if r.get("writing_contract") == VERSION]
    if not applied:
        return None
    from .learning_state import load_learning_events, learning_events_path
    path = state_root / "writing-view/current.json"
    with exclusive_lock(path.with_suffix(".lock")):
        events = load_learning_events(learning_events_path(repo))
        by_id = {r["event_id"]:r for r in events}
        require(all(by_id[r["assigned_id"]]["record_sha256"]==r["learning_event_sha256"] for r in applied), "writer receipt mismatch")
        history, directions, modules, assets = [], {}, {}, {}
        for row in events:
            ev = row["event"]
            w = ev.get("writing")
            if not w:
                continue
            spec = spec_for(repo,{"source_version":w["lesson_sha256"]})
            key = w["asset_id"]
            asset=spec["assets"][key]
            a=assets.setdefault(key,{"identity":{k:asset[k] for k in ("lemma","sense","collocation","function","register")},
                                     "sources":[],"reading_sources":[]})
            for field,items in (("sources",[asset["source"]]),("reading_sources",asset["reading_sources"])):
                for item in items:
                    if item not in a[field]:a[field].append(item)
            evidence = dict(event_id=row["event_id"], outcome=ev["outcome"], hint_dependence=ev["hint_dependence"],
                            phase=w["phase"], matched=bool(w["matched_text"]), source=spec["source"],
                            source_id=ev["source_id"], lesson_id=spec["lesson_id"], task_profile=spec["task_profile"])
            directions.setdefault(key,{}).setdefault(w["direction"],[]).append(evidence)
            if w["template_change"]:
                module_key = (key,spec["task_profile"])
                previous = modules.get(module_key)
                change = w["template_change"]
                if previous and all(previous[k]==v for k,v in change.items()) and previous["hint_dependence"]==ev["hint_dependence"]:
                    continue
                m = {**change,**evidence,"asset_id":key,"version":previous["version"]+1 if previous else 1}
                history.append(m)
                modules[module_key]=m
        result = dict(contract=VERSION, formal_high_water=len(events), assets=assets, directions=directions, modules=list(modules.values()), history=history)
        atomic_write_json(path,result)
        require(load_json(path)==result,"writing view readback failed")
    return dict(contract=VERSION, status="ready", formal_high_water=len(events), view_sha256=object_sha256(result),
                template_revision=len(history), writer_receipt_id=receipt["receipt_id"],
                observations=[{"event_id":r["assigned_id"], **r["writing_binding"]} for r in applied])


def links(repo, *, asset_id=None, source=None, unit=None):
    """Look up either a precise reading location or one sense from writing."""
    require(bool(asset_id) != bool(source and unit),"select an asset or a reading sentence")
    catalog=load_json(repo/COURSE/"shared-language-assets.json")["assets"]
    index=repo/"intake/writing-view/current.json"
    view=load_json(index) if index.exists() else {}
    assets={r["asset_id"]:{"identity":{k:r[k] for k in ("lemma","sense","collocation","function","register")},
            "sources":[{k:r[k] for k in ("edition","source_id","source_sha256","pdf_page","source_quote","lines")}],
            "reading_sources":list(r["readings"])} for r in catalog}
    for key,stored in view.get("assets",{}).items():
        a=assets.setdefault(key,{"identity":stored["identity"],"sources":[],"reading_sources":[]})
        for field in ("sources","reading_sources"):
            for row in stored[field]:
                if row not in a[field]:a[field].append(row)
    result=[]
    for key,a in assets.items():
        if asset_id and key!=asset_id:continue
        if source and not any(r["source_id"]==source and r["unit_id"]==unit for r in a["reading_sources"]):continue
        directions=view.get("directions",{}).get(key,{})
        relevant={d:[{k:row[k] for k in ("event_id","outcome","hint_dependence","phase","matched","task_profile")}
                     for row in directions.get(d,[])] for d in ("reading_recognition","writing_production")}
        # The ordinary reading route receives locators only. Retrieve published body text through its own scope.
        refs=[{k:v for k,v in r.items() if k!="sentence"} for r in a["reading_sources"]]
        result.append(dict(asset_id=key,**a["identity"],writing_sources=a["sources"],reading_sources=refs,directions=relevant))
    return {"contract":VERSION,"assets":result,"empty_direction":"not_observed_in_contract"}
