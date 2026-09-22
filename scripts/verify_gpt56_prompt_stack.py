#!/usr/bin/env python3
"""Check the current English instruction routes without model calls or writes.

The historical filename remains callable. Old A/B fixtures, deleted templates,
fixed headings and word-count reductions are not current acceptance criteria.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = (
    "kaoyan-english-intensive-reading", "kaoyan-english-reading-intake",
    "kaoyan-english-vocab-export", "kaoyan-english-daily-intake-curation",
)


def verify(skills_root: Path, installed_root: Path | None = None) -> dict:
    failures = []
    references = set()
    checked = []
    for name in NAMES:
        folder = skills_root / name
        path = folder / "SKILL.md"
        if not path.is_file():
            failures.append(f"missing skill: {name}")
            continue
        text = path.read_text(encoding="utf-8")
        front = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        if front is None or re.search(r"(?m)^name:\s*" + re.escape(name) + r"\s*$", front.group(1)) is None:
            failures.append(f"invalid skill identity: {name}")
        if front is None or not re.search(r"(?m)^description:\s*\S.+$", front.group(1)):
            failures.append(f"missing trigger description: {name}")
        yaml = folder / "agents/openai.yaml"
        if not yaml.is_file() or "$" + name not in yaml.read_text(encoding="utf-8"):
            failures.append(f"missing matching UI invocation: {name}")
        for source in folder.rglob("*.md"):
            body = source.read_text(encoding="utf-8")
            for reference in re.findall(r"\breferences/[A-Za-z0-9_.-]+\.md", body):
                target = folder / reference
                references.add(str(target))
                if not target.is_file():
                    failures.append(f"broken reference: {name}/{reference}")
            for reference in re.findall(r"\b(?:schema|prompts)/[A-Za-z0-9_./-]+\.(?:md|json)", body):
                target = ROOT / reference
                references.add(str(target))
                if not target.is_file():
                    failures.append(f"broken repo reference: {reference}")
        if installed_root is not None:
            source_files = {p.relative_to(folder): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
            installed = installed_root / name
            installed_files = {p.relative_to(installed): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in installed.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
            if source_files != installed_files:
                failures.append(f"installed copy differs: {name}")
        checked.append(name)
    tree = ast.parse((ROOT / "english_pipeline/cli.py").read_text(encoding="utf-8"))
    commands = {node.args[0].value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_parser" and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}
    for command in ("capture", "recover-segment-gate", "resolve-source", "query-learning-context",
                    "validate-package", "validate-formal-receipt", "complete-article", "apply-nightly",
                    "export-publication-sources"):
        if command not in commands:
            failures.append(f"required actual command missing: {command}")
    for required in ("prompts/codex_prompt.md", "schema/schema.md", "schema/protected_exam_analysis.md",
                     "schema/english_pipeline/learning-event-v1.schema.json"):
        if not (ROOT / required).is_file():
            failures.append(f"required contract missing: {required}")
    return {"schema_version": "english_instruction_stack_validation_v2", "status": "FAIL" if failures else "PASS",
            "skills": checked, "reference_count": len(references), "registered_commands": sorted(commands),
            "failures": sorted(set(failures)), "model_call_count": 0, "formal_write_count": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-root", type=Path, default=ROOT / "codex-skill-sources")
    parser.add_argument("--installed-root", type=Path)
    args = parser.parse_args()
    result = verify(args.skills_root, args.installed_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
