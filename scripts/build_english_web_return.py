#!/usr/bin/env python3
"""Portable return ZIP builder: run beside study.zip and completed advice.json."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def build(input_zip, advice_path, output, conversation=None, personal_summaries=None, vocab_draft=None):
    raw = input_zip.read_bytes()
    with zipfile.ZipFile(input_zip) as archive:
        source = json.loads(archive.read("manifest.json"))
    advice = json.loads(advice_path.read_text(encoding="utf-8"))
    if advice.get("bundle_id") != source["bundle_id"] or advice.get("source_commit") != source["snapshot"]["source_commit"]:
        raise ValueError("advice is not bound to this study.zip and pinned snapshot")
    files = {"advice.json": advice_path.read_bytes()}
    if conversation:
        files["web_conversation.json"] = conversation.read_bytes()
    expected = {row["package_id"] for row in source["packages"]}
    for name, path, schema in (
        ("personal-summary-draft.json", personal_summaries, "english_personal_summary_draft_v1"),
        ("vocab-export-draft.json", vocab_draft, "english_vocab_export_draft_v1"),
    ):
        if path is None:
            if name in source.get("required_return_files", []):
                raise ValueError("required enhancement draft missing: " + name)
            continue
        data = path.read_bytes()
        draft = json.loads(data)
        ids = draft.get("package_ids", [])
        coverage = draft.get("package_coverage", [])
        if (draft.get("schema_version") != schema or draft.get("bundle_id") != source["bundle_id"]
                or draft.get("source_commit") != source["snapshot"]["source_commit"]
                or len(ids) != len(expected) or set(ids) != expected
                or len(coverage) != len(expected) or {row.get("package_id") for row in coverage} != expected
                or any(row.get("status") not in {"drafted", "no_change", "needs_context"}
                       or not str(row.get("reason", "")).strip() for row in coverage)):
            raise ValueError("enhancement draft identity or package coverage differs: " + name)
        files[name] = data
    manifest = {"schema_version": "english_web_return_v1", "contract_version": "english-web-review-v1",
                "subject": "english", "bundle_id": source["bundle_id"],
                "source_commit": source["snapshot"]["source_commit"],
                "input_zip_sha256": hashlib.sha256(raw).hexdigest(),
                "files": [{"path": k, "size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for k,v in sorted(files.items())]}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980,1,1,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--advice", type=Path, required=True)
    parser.add_argument("--conversation", type=Path)
    parser.add_argument("--personal-summaries", type=Path)
    parser.add_argument("--vocab-draft", type=Path)
    parser.add_argument("--output", type=Path, default=Path("advice.zip"))
    args = parser.parse_args()
    print(build(args.input, args.advice, args.output, args.conversation, args.personal_summaries, args.vocab_draft))
