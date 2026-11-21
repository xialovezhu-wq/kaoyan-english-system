"""Hash-addressed writing editions. The 26 corpus retains its own validation contract."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import ValidationError
from .util import atomic_write_json, exclusive_lock, file_sha256, load_json

COURSE = Path("learning/writing-course-2027")
LEGACY_FAMILY = {
    "large_essay_template_candidates": ["WRITING-SRC-BIG-TEMPLATE"],
    "practical_writing_template_candidates": ["WRITING-SRC-SMALL-TEMPLATE"],
    "topic_vocabulary_candidates": ["WRITING-SRC-TOPIC-VOCAB"],
    "prompt_collection_image_heavy": ["WRITING-SRC-HISTORICAL-BIG-PROMPTS", "WRITING-SRC-HISTORICAL-SMALL-PROMPTS"],
}
EDITION = "english-writing-preprocessed-20261120"

def require(ok, message):
    if not ok:
        raise ValidationError(message)

def edition_path(repo, edition):
    require(isinstance(edition, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", edition), "invalid edition")
    return repo / COURSE / "editions" / (edition + ".json")

def inside(root, relative):
    path = (root / relative).resolve(strict=True)
    require(path.is_relative_to(root) and path.is_file(), "missing or escaped material file")
    return path

def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def import_bundle(repo, bundle, verify_only=False):
    bundle = bundle.resolve(strict=True)
    manifest = load_json(bundle / "source_manifest.json")
    edition = manifest["bundle_id"]
    target = edition_path(repo, edition)
    old = load_json(repo / "raw/writing_reference/manifest.json")["sources"]
    previous = load_json(repo / COURSE / "sources.json")["sources"]
    legacy = {row["source_id"]: row for row in old}
    exact = {}
    for rows, key, pathkey in [(old, "source_id", "external_source_path"), (previous, "id", "path")]:
        for row in rows:
            original = Path(row[pathkey])
            if original.is_file() and file_sha256(original) == row["sha256"]:
                exact.setdefault(row["sha256"], []).append(row[key])
    for catalog in sorted(target.parent.glob("*.json")):
        if catalog==target:continue
        earlier=load_json(catalog)
        for sid,r in earlier["sources"].items():
            exact.setdefault(r["sha256"],[]).append(earlier["edition"]+"/"+sid)
    pages = lines(bundle / "indexes/pages.jsonl")
    issues = lines(bundle / "quality/known_errata.jsonl")
    checked = {}
    for source in manifest["sources"]:
        sid, sha = source["source_id"], source["sha256"]
        pdf = inside(bundle, source["original_pdf_path"])
        require(sid not in checked and file_sha256(pdf) == sha, "source identity mismatch")
        locators = [p for p in pages if p["source_id"] == sid]
        require(sorted(p["pdf_page"] for p in locators) == list(range(1, source["pages"] + 1)), "incomplete page coverage")
        require(all(p["source_sha256"] == sha and inside(bundle, p["page_markdown_path"]) for p in locators), "wrong page edition")
        family = [dict(source_id=i, sha256=legacy[i]["sha256"]) for i in LEGACY_FAMILY.get(source["role"], []) if i in legacy]
        checked[sid] = {
            "content_id": "sha256:" + sha, "sha256": sha, "aliases": exact.get(sha, []),
            "filename": source["uploaded_filename"], "pages": source["pages"], "role": source["role"],
            "edition_label": source["material_edition_label"], "version_label": source["version_label"],
            "related_older_editions": family, "original": source["original_pdf_path"],
            "page_index": {str(p["pdf_page"]): p["page_markdown_path"] for p in locators},
            "issues": [i for i in issues if i["source_id"] == sid],
        }
    require(len(checked) == manifest["source_count"] and len(pages) == manifest["total_pdf_pages"], "manifest count mismatch")
    catalog = dict(schema="english_writing_reference_edition_v1", edition=edition, bundle=str(bundle),
                   source_manifest_sha256=file_sha256(bundle / "source_manifest.json"), sources=checked,
                   missing_materials=manifest["missing_materials"], learner_evidence=False)
    with exclusive_lock(target.with_suffix(".lock")):
        if target.exists():
            prior=load_json(target)
            for sid,row in checked.items():
                if sid in prior["sources"]:
                    row["aliases"]=prior["sources"][sid]["aliases"]
            require(prior == catalog, "edition already registered with different identity or location")
            status = "verified_existing"
        else:
            require(not verify_only, "edition has not been registered")
            atomic_write_json(target, catalog)
            status = "registered"
    return dict(status=status, edition=edition, sources=len(checked), pages=len(pages),
                matched_content={k:r["aliases"] for k,r in checked.items() if r["aliases"]},
                formal_write_count=0)

def locate(repo, edition, source, page):
    catalog = load_json(edition_path(repo, edition))
    row = catalog["sources"][source]
    require(type(page) is int and str(page) in row["page_index"], "unknown physical page")
    return dict(edition=edition, source_id=source, sha256=row["sha256"], pdf_page=page,
                role=row["role"], filename=row["filename"],
                reference=str(Path(catalog["bundle"]) / row["page_index"][str(page)]),
                issues=[i["id"] for i in row["issues"] if page in i["affected_pages"] or page in i["evidence_pages"]])
