"""Course-led writing. Validate the sealed lesson and observations inside the native writer."""
from __future__ import annotations

import re
from pathlib import Path

from .util import file_sha256, load_json, object_sha256, text_sha256
from .writing_materials import COURSE, EDITION, locate, require

VERSION = "english-read-write/1"
SOURCE_PREFIX = "WRCOURSE-"
DIRECTIONS = {"reading_recognition", "writing_production", "paraphrase_or_summary",
              "translation_en_zh", "translation_zh_en", "speaking_production", "listening_recognition"}
TASKS = {
    "course_overview": ("跟随课程", "按老师当前讲解理解本节目标，保留想回看的位置。"),
    "kaoyan_i_chart": ("图表描述", "核对类别、分母、数值与时间；静态份额只支持当期比较。"),
    "kaoyan_i_argument": ("图画或文字论述", "回应题目要求，区分自己的立场、论据与适用范围。"),
    "kaoyan_i_practical": ("应用文", "核对读者身份、目的、必含信息及相应语域。"),
}
PHASES = {"first_draft", "revision", "transfer", "recognition"}
ASSET_FIELDS = ("lemma", "sense", "collocation", "function", "register")

def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value), "invalid unit identifier")
    return value

def strings(row, names):
    require(isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k].strip() for k in names),
            "missing or empty course/asset fields")

def reading_source(repo, link):
    from .reading_preparation import DEFAULT_OUTPUT, query
    from .learning_state import learning_events_path, load_learning_events
    record = query(repo / DEFAULT_OUTPUT, source_id=identifier(link["source_id"]), repo_root=repo)
    unit = next((u for u in record["units"] if u["unit_id"] == link["unit_id"]), None)
    require(unit and unit["kind"] == "article" and unit["answer_exposure"] == "answer_free"
            and record["source_hash"] == link["source_hash"] and unit["text_sha256"] == link["sentence_sha256"],
            "reading sentence, visibility or version mismatch")
    contact = any(r["event"]["source_id"] == record["source_id"] and r["event"]["unit_id"] == unit["unit_id"]
                  and r["event"]["kind"] != "adjudication" for r in load_learning_events(learning_events_path(repo)))
    require(contact, "no recorded learning contact for this reading unit")
    return dict(source_id=record["source_id"], source_hash=record["source_hash"], reference_id=record["reference_id"],
                unit_id=unit["unit_id"], paragraph_id=unit["paragraph_id"], sentence=unit["text"],
                sentence_sha256=unit["text_sha256"], visibility="article_language_only")

def lesson(repo, request):
    """The coach supplies only material for the named course segment. No automatic lesson selection."""
    strings(request, ("course", "section", "goal", "task_profile"))
    require(request["task_profile"] in TASKS, "this exam task is not enabled")
    if "video_material" in request:
        from pathlib import Path
        material=request["video_material"]
        path=Path(material["path"]).expanduser().resolve(strict=True)
        require(path.is_file() and path.suffix.lower() in {".mp4",".m4v",".mkv",".webm",".mov"} and
                file_sha256(path)==material["sha256"],"original course video hash mismatch")
        item=dict(source_id=identifier(request["source_id"]),sha256=material["sha256"],filename=path.name,
                  pdf_page=None,source_kind="video",original_path=str(path))
    else:
        item = locate(repo, request.get("edition", EDITION), request["source_id"], request["pdf_page"])
        require(request["source_sha256"] == item["sha256"], "course page hash mismatch")
        item.pop("reference")
    assets = {}
    selected = list(request.get("assets", []))
    if request.get("asset_ids"):
        catalog = {row["asset_id"]:row for row in load_json(repo/COURSE/"shared-language-assets.json")["assets"]}
        selected.extend(catalog[key] for key in request["asset_ids"])
    for supplied in selected:
        strings(supplied, ASSET_FIELDS)
        identity = {k:supplied[k].strip() for k in ASSET_FIELDS}
        key = "RW-" + object_sha256(identity)[:24]
        review = {k:supplied[k] for k in ("usage", "restriction") if isinstance(supplied.get(k), str)}
        readings = [reading_source(repo, link) for link in supplied.get("readings", [])]
        location = item
        if "pdf_page" in supplied:
            location=locate(repo,supplied.get("edition",item["edition"]),supplied["source_id"],supplied["pdf_page"])
            require(location["sha256"]==supplied["source_sha256"],"asset citation mismatch")
            location.pop("reference")
        forms=supplied.get("production_forms",[identity["lemma"]])
        require(isinstance(forms,list) and 0<len(forms)<=32 and all(isinstance(t,str) and t.strip() for t in forms),"invalid accepted expressions")
        assets[key] = {**identity, **review, "production_forms":forms,"reading_sources": readings, "source": location}
        if supplied.get("source_quote"):
            # Optional published text keeps its original representation and physical-page identity.
            from .writing_materials import edition_path, lines, inside
            catalog=load_json(edition_path(repo,location["edition"]))
            bundle=Path(catalog["bundle"]).resolve(strict=True)
            row=next(p for p in lines(inside(bundle,"indexes/pages.jsonl")) if p["source_id"]==location["source_id"] and p["pdf_page"]==location["pdf_page"])
            text=inside(bundle,row["native_text_path"])
            require(row["source_sha256"]==location["sha256"] and file_sha256(text)==row["native_text_sha256"]
                    and supplied["source_quote"] in text.read_text(),"quotation does not match the original page text")
            assets[key].update(source_quote=supplied["source_quote"],source_channel="publisher_native_text")
    video = request.get("video")
    if video is not None:
        strings(video, ("reference",))
        require(set(video)<= {"reference","start_seconds","end_seconds"}, "video contains unscoped fields")
        for k in ("start_seconds", "end_seconds"):
            require(video.get(k) is None or type(video[k]) in (int, float) and video[k] >= 0, "invalid observed video time")
        if video.get("end_seconds") is not None:
            require(video.get("start_seconds") is not None and video["end_seconds"] >= video["start_seconds"], "invalid time interval")
    require(isinstance(request.get("prompt", ""),str),"invalid course prompt")
    extra={}
    if "points" in request:
        rows=request["points"]
        require(isinstance(rows,list) and rows and len(rows)<=128,"invalid lesson points")
        ids=[identifier(r["lesson_point_id"]) for r in rows]
        require(len(ids)==len(set(ids)) and all("criteria" in r and "goal" in r and "question" in r for r in rows),"duplicate or incomplete lesson point")
        extra["points"]=rows
    return dict(contract=VERSION, **extra, course=request["course"], section=request["section"],
                lesson_id=identifier(request["lesson_id"]), goal=request["goal"], source=item,
                task_profile=request["task_profile"], task_constraint=TASKS[request["task_profile"]][1],
                assets=assets, prompt=request.get("prompt", ""), video=video)

def source_id(spec):
    return SOURCE_PREFIX + object_sha256(spec)[:32]

OBS_FIELDS = {"contract", "lesson_sha256", "asset_id", "direction", "phase", "package_id", "message_index",
              "response_sha256", "matched_text", "issue", "hint", "model_suggestion", "template_change", "task_prompt"}

def observation_fields(event):
    w = event.get("writing")
    require(isinstance(w, dict) and set(w) == OBS_FIELDS and w["contract"] == VERSION, "writing observation contract mismatch")
    require(event["origin"] == "daily" and w["phase"] in PHASES and w["direction"] in DIRECTIONS, "unbound writing task direction")
    require((w["phase"] == "recognition") == (w["direction"] == "reading_recognition"), "phase and direction mismatch")
    require(type(w["message_index"]) is int and w["message_index"] >= 0 and re.fullmatch(r"EN-PKG-[0-9]{8}-[0-9A-F]{16}", w["package_id"]),
            "writing observation must bind its exact capture message")
    require(re.fullmatch(r"[0-9a-f]{64}", w["lesson_sha256"]) and re.fullmatch(r"RW-[0-9a-f]{24}", w["asset_id"]), "invalid lesson/asset identity")
    require(event["source_id"] == SOURCE_PREFIX + w["lesson_sha256"][:32]
            and event["event_key"] == f'{w["contract"]}:{w["package_id"]}:{w["message_index"]}:{w["direction"]}:{w["asset_id"]}',
            "unstable writing observation key")
    strings(w, ("task_prompt",))
    response = event["user_response"]
    require(w["response_sha256"] == text_sha256(response) and isinstance(w["matched_text"], str) and w["matched_text"] in response,
            "writing observation differs from actual response")
    require(isinstance(w["hint"], str) and isinstance(w["model_suggestion"], str), "invalid coaching fields")
    issue = w["issue"]
    if issue is not None:
        require(isinstance(issue, dict) and set(issue) == {"offset", "length", "quote", "category", "message"}
                and type(issue["offset"]) is int and type(issue["length"]) is int and issue["offset"] >= 0 and issue["length"] >= 0
                and issue["offset"] + issue["length"] <= len(response)
                and response[issue["offset"]:issue["offset"] + issue["length"]] == issue["quote"], "stale writing error span")
        strings(issue, ("category", "message"))
    if w["template_change"] is not None:
        change = w["template_change"]
        strings(change, ("expression", "slots", "applies_when", "avoid_when", "reason"))
        require(set(change) == {"expression","slots","applies_when","avoid_when","reason"}
                and w["direction"] == "writing_production" and event["outcome"] == "correct"
                and bool(w["matched_text"]) and change["expression"] in response, "template requires attested writing use")
    return w

def json_document(root, attachment):
    import json
    root=root.resolve(strict=True)
    path=(root/attachment["path"]).resolve(strict=True)
    require(path.is_relative_to(root) and file_sha256(path)==attachment["sha256"],"changed Capture attachment")
    return json.loads(path.read_bytes())

def validate_observation(event, resolved, frozen, identities):
    w = observation_fields(event)
    pid = w["package_id"]
    require(pid in frozen and identities[pid].get("source_hash") == w["lesson_sha256"] and
            identities[pid].get("source_id") == event["source_id"] and identities[pid].get("unit_id") == event["unit_id"],
            "writing observation does not bind its source Capture")
    root = Path(frozen[pid]["path"]).resolve()
    rows = [r for r in resolved if r["binding"]["package_id"] == pid]
    pointer = f'conversation.json#/messages/{w["message_index"]}'
    bound = next((r["value"] for r in rows if r["binding"]["pointer"] == pointer and r["binding"]["resolved_role"] == "user"), {})
    from .util import learner_message_spans
    require(bound.get("created_at") == event["observed_at"] and any(event["user_response"] in part for part in
            learner_message_spans(bound, identities[pid].get("source_sentence", ""))), "missing verbatim writing response at bound time")
    spec = None
    for r in rows:
        a = r["value"]
        if r["binding"]["kind"] != "other_attachment" or a.get("mime_type") != "application/json":
            continue
        path = (root / a["path"]).resolve(strict=True)
        require(path.is_relative_to(root) and file_sha256(path) == a["sha256"], "writing lesson attachment changed")
        candidate = json_document(root,a)
        if isinstance(candidate,dict) and candidate.get("contract") == VERSION and object_sha256(candidate) == w["lesson_sha256"]:
            spec = candidate
            break
    require(spec and spec["lesson_id"] == event["unit_id"] and w["asset_id"] in spec["assets"] and spec["task_profile"] in TASKS,
            "sealed course/asset/lesson required")
    require(w["direction"] in {"reading_recognition","writing_production","paraphrase_or_summary"}, "future task is not enabled")
    asset=spec["assets"][w["asset_id"]]
    require("RW-"+object_sha256({k:asset[k] for k in ASSET_FIELDS})[:24]==w["asset_id"], "changed semantic asset identity")
    if w["direction"]=="writing_production" and event["outcome"]=="correct":
        matched=w["matched_text"].casefold()
        require(bool(re.search(r"[a-z]",matched)) and any(re.search(r"(?<![a-z])"+re.escape(form.casefold())+r"(?![a-z])",matched)
            for form in asset.get("production_forms",[asset["lemma"]])), "claimed correct expression is absent from the original writing")
    assistant = [r for r in rows if r["binding"].get("resolved_role") == "assistant"]
    for key in ("hint", "model_suggestion"):
        require(not w[key] or any(w[key] in r["value"]["content"] for r in assistant), "coaching text lacks its assistant message")
    transcript = load_json(root / "conversation.json")["messages"]
    earlier = [m["content"] for m in transcript[:w["message_index"]] if m["role"] == "assistant"]
    if w["task_prompt"] != spec["prompt"]:
        require(any(w["task_prompt"] in m for m in earlier), "exercise prompt needs an earlier tutor message")
    if w["phase"] == "transfer":
        require(w["task_prompt"] != spec["prompt"], "transfer needs a different, evidenced task")
    if w["phase"] == "revision" and w["hint"]:
        require(any(w["hint"] in m for m in earlier), "revision must follow the cited hint")
    if event["hint_dependence"] == "none" and w["direction"] == "writing_production":
        from datetime import datetime
        observed = datetime.fromisoformat(event["observed_at"].replace("Z","+00:00"))
        for a in load_json(root/"manifest.json")["attachments"]:
            if a.get("role")!="other_attachment" or a.get("mime_type")!="application/json":continue
            data = json_document(root,a)
            if not isinstance(data,dict) or data.get("source_version")!=w["lesson_sha256"] or not data.get("issued_at"):continue
            practice = data.get("practice") or {"prompt":spec["prompt"]}
            if practice["prompt"]!=w["task_prompt"] or datetime.fromisoformat(data["issued_at"].replace("Z","+00:00"))>observed:continue
            require(not data.get("page_release") and data.get("stage") not in {"guided","review"},
                    "reference or guided material was released for this task before the response")
    if event["hint_dependence"] == "none":
        require(not w["hint"] and w["phase"] != "revision", "guided revision cannot be independent")
        from .util import normalize_sentence
        sentence = normalize_sentence(event["user_response"]).casefold()
        known_models = earlier + [a.get("source_quote","") for a in spec["assets"].values()] + [s["sentence"] for a in spec["assets"].values() for s in a["reading_sources"]]
        require(not any(sentence in normalize_sentence(s).casefold() for s in known_models), "copied reference cannot establish independent production")
    if event["kind"] == "correction" or w["phase"] == "revision":
        require(any(m["role"] == "user" for m in transcript[:w["message_index"]]), "revision has no earlier learner message")
    return spec
