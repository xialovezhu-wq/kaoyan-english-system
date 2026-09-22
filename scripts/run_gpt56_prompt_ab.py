#!/usr/bin/env python3
"""Run isolated old-vs-new GPT-5.6 Sol Ultra prompt evaluations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "tmp" / "prompt-evals" / "gpt56-migration"
ITERATION_ROOT = EVAL_ROOT / "iteration-1"
SNAPSHOT = EVAL_ROOT / "skill-snapshot"
ISOLATED_HOME = EVAL_ROOT / "isolated-codex-home"
SKILLS = Path("/Users/your-user/.codex/skills")
MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "ultra"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def stack_paths(eval_id: str, configuration: str) -> list[Path]:
    old = configuration == "old_skill"
    if eval_id in {"E01", "E02", "E03"}:
        if old:
            skill = SNAPSHOT / "skills" / "kaoyan-english-intensive-reading"
            return [
                SNAPSHOT / "repo" / "prompts" / "codex_prompt.md",
                skill / "SKILL.md",
                SNAPSHOT / "repo" / "schema" / "protected_exam_analysis.md",
                skill / "references" / "sentence-session-workflow.md",
                skill / "references" / "sentence-output-template.md",
                skill / "references" / "candidate-tracking.md",
            ]
        skill = SKILLS / "kaoyan-english-intensive-reading"
        return [
            ROOT / "prompts" / "codex_prompt.md",
            skill / "SKILL.md",
            ROOT / "schema" / "protected_exam_analysis.md",
            skill / "references" / "sentence-session-workflow.md",
            skill / "references" / "sentence-output-template.md",
            skill / "references" / "candidate-tracking.md",
        ]

    if old:
        skill = SNAPSHOT / "skills" / "kaoyan-english-vocab-export"
        return [
            SNAPSHOT / "repo" / "prompts" / "codex_prompt.md",
            skill / "SKILL.md",
            SNAPSHOT / "repo" / "schema" / "reference_grounded_examples.md",
            skill / "references" / "abc-priority-rules.md",
            skill / "references" / "bbdc-export-format.md",
            skill / "references" / "memory-curve-old-word-selection.md",
            skill / "references" / "output-template.md",
        ]
    skill = SKILLS / "kaoyan-english-vocab-export"
    return [
        ROOT / "prompts" / "codex_prompt.md",
        skill / "SKILL.md",
        ROOT / "schema" / "reference_grounded_examples.md",
        skill / "references" / "abc-priority-rules.md",
        skill / "references" / "bbdc-export-format.md",
        skill / "references" / "memory-curve-old-word-selection.md",
        skill / "references" / "output-template.md",
    ]


def render_stack(paths: list[Path]) -> str:
    blocks = []
    for path in paths:
        blocks.append(
            f'<instruction_file path="{path}">\n{read(path)}\n</instruction_file>'
        )
    return "\n\n".join(blocks)


def build_prompt(case: dict[str, Any], configuration: str, paths: list[Path]) -> str:
    stack = render_stack(paths)
    return f"""You are executing one read-only prompt migration evaluation.

Instruction isolation:
- Treat the embedded instruction files below as the complete English behavior stack for this run.
- Do not open or consult current prompt, schema, workflow, template, or installed skill files. References to such instruction files resolve to the embedded copies.
- You may read task evidence and data files inside the current English workspace when the embedded stack permits it.
- Do not modify files. Respond directly as the English assistant, not as an evaluator. Do not mention the A/B test, configuration name, or these isolation rules.

Configuration: {configuration}
Model target: {MODEL}
Reasoning effort target: {REASONING_EFFORT}

<instruction_stack>
{stack}
</instruction_stack>

<user_scenario>
{case['prompt']}
</user_scenario>
"""


def parse_events(stdout: str) -> dict[str, Any]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    usage: dict[str, int] = {}
    item_types: dict[str, int] = {}
    for event in events:
        if event.get("type") == "turn.completed":
            usage = event.get("usage", {})
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type", "unknown"))
            item_types[item_type] = item_types.get(item_type, 0) + 1
    return {"events": len(events), "usage": usage, "item_types": item_types}


def run_one(case: dict[str, Any], configuration: str) -> dict[str, Any]:
    eval_id = str(case["id"])
    case_dirs = sorted(ITERATION_ROOT.glob(f"{eval_id}-*"))
    if len(case_dirs) != 1:
        raise RuntimeError(f"expected one directory for {eval_id}, found {case_dirs}")
    run_dir = case_dirs[0] / configuration
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    final_path = outputs_dir / "final.txt"
    transcript_jsonl = run_dir / "transcript.jsonl"
    transcript_md = run_dir / "transcript.md"
    metrics_path = outputs_dir / "metrics.json"
    timing_path = run_dir / "timing.json"

    paths = stack_paths(eval_id, configuration)
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing stack files: {missing}")
    prompt = build_prompt(case, configuration, paths)

    cmd = [
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--model",
        MODEL,
        "-c",
        f'model_reasoning_effort="{REASONING_EFFORT}"',
        "--json",
        "-o",
        str(final_path),
        "-",
    ]
    env = os.environ.copy()
    env["CODEX_HOME"] = str(ISOLATED_HOME)
    start = time.monotonic()
    completed = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        cwd=ROOT,
        env=env,
        capture_output=True,
        timeout=900,
        check=False,
    )
    duration = round(time.monotonic() - start, 3)
    transcript_jsonl.write_text(completed.stdout, encoding="utf-8")
    parsed = parse_events(completed.stdout)
    final_text = read(final_path) if final_path.is_file() else ""
    metrics = {
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "configuration": configuration,
        "exit_code": completed.returncode,
        "prompt_chars": len(prompt),
        "instruction_file_count": len(paths),
        "instruction_bytes": sum(path.stat().st_size for path in paths),
        "output_chars": len(final_text),
        "stderr_chars": len(completed.stderr),
        **parsed,
    }
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    timing_path.write_text(
        json.dumps(
            {
                "executor_duration_seconds": duration,
                "total_duration_seconds": duration,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    transcript_md.write_text(
        "\n".join(
            [
                f"# {eval_id} {configuration}",
                "",
                "## User scenario",
                "",
                str(case["prompt"]),
                "",
                "## Instruction files",
                "",
                *[f"- {path}" for path in paths],
                "",
                "## Final response",
                "",
                final_text,
                "",
                "## Executor stderr",
                "",
                "```text",
                completed.stderr,
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if completed.returncode != 0 or not final_text.strip():
        raise RuntimeError(
            f"{eval_id}/{configuration} failed exit={completed.returncode}: "
            f"{completed.stderr[-500:]}"
        )
    return {
        "eval_id": eval_id,
        "configuration": configuration,
        "duration_seconds": duration,
        "input_tokens": parsed.get("usage", {}).get("input_tokens"),
        "output_tokens": parsed.get("usage", {}).get("output_tokens"),
        "reasoning_output_tokens": parsed.get("usage", {}).get("reasoning_output_tokens"),
        "output_chars": len(final_text),
    }


def main() -> None:
    global ITERATION_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iteration", default="iteration-1")
    parser.add_argument("--eval-id", action="append", dest="eval_ids")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--allow-historical-model-run", action="store_true",
                        help="Only for a separately requested historical GPT-5.6 experiment, never current validation.")
    args = parser.parse_args()
    if not args.allow_historical_model_run:
        parser.error("historical model A/B is retired from current validation; use verify_gpt56_prompt_stack.py and isolated unit tests")
    ITERATION_ROOT = EVAL_ROOT / args.iteration
    payload = json.loads(read(EVAL_ROOT / "evals" / "evals.json"))
    cases = [
        case
        for case in payload["evals"]
        if not args.eval_ids or str(case["id"]) in set(args.eval_ids)
    ]
    if not cases:
        parser.error("no matching eval cases")
    tasks = [(case, configuration) for case in cases for configuration in ("old_skill", "with_skill")]
    results = []
    errors = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_map = {
            pool.submit(run_one, case, configuration): (case["id"], configuration)
            for case, configuration in tasks
        }
        for future in as_completed(future_map):
            eval_id, configuration = future_map[future]
            try:
                result = future.result()
                results.append(result)
                print(json.dumps({"status": "completed", **result}, ensure_ascii=False), flush=True)
            except Exception as exc:  # noqa: BLE001 - collect all paired-run failures
                errors.append({"eval_id": eval_id, "configuration": configuration, "error": str(exc)})
                print(
                    json.dumps(
                        {"status": "failed", "eval_id": eval_id, "configuration": configuration, "error": str(exc)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    manifest = {
        "schema": "gpt56_prompt_ab_run_v1",
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "isolated_codex_home": str(ISOLATED_HOME),
        "duration_seconds": round(time.monotonic() - started, 3),
        "runs": sorted(results, key=lambda row: (row["eval_id"], row["configuration"])),
        "errors": errors,
    }
    (ITERATION_ROOT / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
