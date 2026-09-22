#!/usr/bin/env python3
"""Aggregate the selected final GPT-5.6 prompt A/B runs for review."""

from __future__ import annotations

import json
import math
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "tmp" / "prompt-evals" / "gpt56-migration"
FINAL_REVIEW = MIGRATION / "final-review"
MODEL = "gpt-5.6-sol"
EFFORT = "ultra"

SELECTIONS = [
    ("E01", "answer-safe-local-question", "iteration-1", "E01-answer-safe-local-question"),
    ("E02", "explicit-answer-unlock", "iteration-1", "E02-explicit-answer-unlock"),
    ("E03", "learning-first-breakpoint", "iteration-3", "E03-learning-first-breakpoint"),
    ("E04", "grounded-example-fail-closed", "iteration-1", "E04-grounded-example-fail-closed"),
]

CONFIGS = [
    ("with_skill", "with_skill"),
    ("without_skill", "old_skill"),
]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def stats(values: list[float]) -> dict[str, float]:
    return {
        "mean": round(mean(values), 4),
        "stddev": round(stdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def chat_format_violations(text: str) -> list[str]:
    violations = []
    patterns = {
        "markdown_heading": r"(?m)^#{1,6}\s+",
        "markdown_bold": r"\*\*[^*\n]+\*\*|__[^_\n]+__",
        "markdown_strikethrough": r"~~[^~\n]+~~",
        "markdown_italic": r"(?<!\*)\*[^*\n]+\*(?!\*)|(?<![\w_])_[^_\n]+_(?![\w_])",
    }
    for name, pattern in patterns.items():
        if re.search(pattern, text):
            violations.append(name)
    return violations


def completed_tool_calls(transcript_path: Path) -> int:
    count = 0
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") not in {None, "agent_message"}:
                count += 1
    return count


def build() -> dict[str, Any]:
    eval_payload = read_json(MIGRATION / "evals" / "evals.json")
    cases = {str(case["id"]): case for case in eval_payload["evals"]}
    if FINAL_REVIEW.exists():
        shutil.rmtree(FINAL_REVIEW)
    FINAL_REVIEW.mkdir(parents=True)

    runs = []
    format_summary = {"with_skill": {"passed": 0, "total": 0}, "without_skill": {"passed": 0, "total": 0}}
    config_metrics: dict[str, dict[str, list[float]]] = {
        config: {"pass_rate": [], "time_seconds": [], "tokens": []}
        for config, _ in CONFIGS
    }

    for eval_id, eval_name, iteration, directory in SELECTIONS:
        case = cases[eval_id]
        source_eval_dir = MIGRATION / iteration / directory
        target_eval_dir = FINAL_REVIEW / f"eval-{eval_id}-{eval_name}"
        for public_config, source_config in CONFIGS:
            source = source_eval_dir / source_config
            grading = read_json(source / "grading.json")
            metrics = read_json(source / "outputs" / "metrics.json")
            timing = read_json(source / "timing.json")
            final_text = (source / "outputs" / "final.txt").read_text(encoding="utf-8")
            violations = chat_format_violations(final_text)
            format_summary[public_config]["total"] += 1
            if not violations:
                format_summary[public_config]["passed"] += 1

            destination = target_eval_dir / public_config
            destination.mkdir(parents=True)
            shutil.copytree(source / "outputs", destination / "outputs")
            for name in ("grading.json", "transcript.jsonl", "transcript.md", "timing.json"):
                shutil.copy2(source / name, destination / name)
            (destination / "eval_metadata.json").write_text(
                json.dumps(
                    {
                        "eval_id": eval_id,
                        "eval_name": eval_name,
                        "prompt": case["prompt"],
                        "expected_output": case["expected_output"],
                        "assertions": case["assertions"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            summary = grading["summary"]
            duration = float(
                timing.get("total_duration_seconds", timing.get("executor_duration_seconds", 0.0))
            )
            input_tokens = int(metrics.get("usage", {}).get("input_tokens", 0))
            run = {
                "eval_id": eval_id,
                "eval_name": eval_name,
                "configuration": public_config,
                "run_number": 1,
                "result": {
                    "pass_rate": summary["pass_rate"],
                    "passed": summary["passed"],
                    "failed": summary["failed"],
                    "total": summary["total"],
                    "time_seconds": duration,
                    "tokens": input_tokens,
                    "tool_calls": completed_tool_calls(source / "transcript.jsonl"),
                    "errors": 0 if metrics.get("exit_code") == 0 else 1,
                },
                "expectations": grading["expectations"],
                "notes": [
                    f"chat_format={'PASS' if not violations else 'FAIL'}",
                    f"instruction_bytes={metrics.get('instruction_bytes')}",
                    f"output_tokens={metrics.get('usage', {}).get('output_tokens')}",
                    *[f"format_violation={item}" for item in violations],
                ],
            }
            runs.append(run)
            config_metrics[public_config]["pass_rate"].append(float(summary["pass_rate"]))
            config_metrics[public_config]["time_seconds"].append(duration)
            config_metrics[public_config]["tokens"].append(float(input_tokens))

    run_summary = {
        config: {metric: stats(values) for metric, values in metrics.items()}
        for config, metrics in config_metrics.items()
    }
    new = run_summary["with_skill"]
    old = run_summary["without_skill"]
    run_summary["delta"] = {
        "pass_rate": f"{new['pass_rate']['mean'] - old['pass_rate']['mean']:+.4f}",
        "time_seconds": f"{new['time_seconds']['mean'] - old['time_seconds']['mean']:+.3f}",
        "tokens": f"{new['tokens']['mean'] - old['tokens']['mean']:+.1f}",
    }
    total_tokens = {
        config: int(sum(config_metrics[config]["tokens"])) for config, _ in CONFIGS
    }
    token_reduction = (
        1 - total_tokens["with_skill"] / total_tokens["without_skill"]
        if total_tokens["without_skill"]
        else 0.0
    )
    benchmark = {
        "metadata": {
            "skill_name": "kaoyan-english-prompt-stack",
            "skill_path": str(ROOT / "prompts" / "codex_prompt.md"),
            "executor_model": MODEL,
            "reasoning_effort": EFFORT,
            "analyzer_model": "independent grader agents",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "evals_run": [item[0] for item in SELECTIONS],
            "runs_per_configuration": 1,
        },
        "runs": sorted(runs, key=lambda row: (row["eval_id"], 0 if row["configuration"] == "with_skill" else 1)),
        "run_summary": run_summary,
        "format_summary": format_summary,
        "input_token_totals": total_tokens,
        "input_token_reduction_ratio": round(token_reduction, 4),
        "notes": [
            "Both configurations used gpt-5.6-sol with ultra reasoning in the same isolated read-only environment.",
            "without_skill is the pre-migration snapshot; with_skill is the final migrated prompt stack.",
            "E03 uses iteration-3 after a surgical fix for verbatim first-translation preservation and exactly one diagnostic question.",
            "Each case was executed once per configuration; results are representative directional evidence, not a variance estimate across repeated stochastic runs.",
            "Token statistics use total input_tokens reported by Codex, including cached input; prompt byte counts are retained in per-run notes.",
        ],
    }
    (FINAL_REVIEW / "benchmark.json").write_text(
        json.dumps(benchmark, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# GPT-5.6 English prompt migration benchmark",
        "",
        f"- Model: `{MODEL}`",
        f"- Reasoning effort: `{EFFORT}`",
        f"- Selected evals: {', '.join(item[0] for item in SELECTIONS)}",
        "",
        "| Eval | New pass | Old pass | New input tokens | Old input tokens | New seconds | Old seconds |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    by_key = {(run["eval_id"], run["configuration"]): run for run in runs}
    for eval_id, eval_name, _iteration, _directory in SELECTIONS:
        new_run = by_key[(eval_id, "with_skill")]
        old_run = by_key[(eval_id, "without_skill")]
        lines.append(
            f"| {eval_id} {eval_name} | {new_run['result']['passed']}/{new_run['result']['total']} | "
            f"{old_run['result']['passed']}/{old_run['result']['total']} | "
            f"{new_run['result']['tokens']} | {old_run['result']['tokens']} | "
            f"{new_run['result']['time_seconds']:.3f} | {old_run['result']['time_seconds']:.3f} |"
        )
    lines.extend(
        [
            "",
            f"New total input tokens: {total_tokens['with_skill']}",
            f"Old total input tokens: {total_tokens['without_skill']}",
            f"Input-token reduction: {token_reduction:.1%}",
            f"New chat-format pass: {format_summary['with_skill']['passed']}/{format_summary['with_skill']['total']}",
            f"Old chat-format pass: {format_summary['without_skill']['passed']}/{format_summary['without_skill']['total']}",
            "",
        ]
    )
    (FINAL_REVIEW / "benchmark.md").write_text("\n".join(lines), encoding="utf-8")
    return benchmark


def main() -> None:
    benchmark = build()
    print(json.dumps(benchmark, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
