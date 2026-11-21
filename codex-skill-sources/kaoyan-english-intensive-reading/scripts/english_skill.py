#!/usr/bin/env python3
"""Locate the current English checkout and use its existing native entrypoints."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


MARKERS = ("scripts/english_learning_pipeline.py", "english_pipeline/constants.py", "bank/master_bank.csv")


def valid_root(path: Path) -> bool:
    return all((path / name).is_file() for name in MARKERS)


def discover_repo(explicit=None, *, cwd=None, home=None, anchor=None) -> Path:
    supplied = explicit or os.environ.get("KAOYAN_ENGLISH_PROJECT_ROOT") or os.environ.get("KAOYAN_ENGLISH_ROOT")
    if supplied:
        root = Path(supplied).expanduser().resolve()
        if not valid_root(root):
            raise ValueError("configured_english_repository_unavailable: " + str(root))
        return root
    for start in (Path(cwd or Path.cwd()).resolve(), Path(anchor or __file__).resolve().parent):
        for candidate in (start, *start.parents):
            if valid_root(candidate):
                return candidate
    home = Path(home or Path.home())
    registry = home / "Library/Application Support/obsidian/obsidian.json"
    candidates = set()
    if registry.is_file():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            for vault in data.get("vaults", {}).values():
                value = vault.get("path") if isinstance(vault, dict) else None
                if isinstance(value, str) and Path(value).is_absolute():
                    path = Path(value).resolve()
                    if valid_root(path):
                        candidates.add(path)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    if len(candidates) == 1:
        return candidates.pop()
    if candidates:
        raise ValueError("multiple_english_vaults; use the selected workspace or --repo")
    default = home / "Documents/kaoyan-english"
    if valid_root(default):
        return default.resolve()
    raise ValueError("english_repository_not_found; use the English workspace or --repo")


def native_command(root: Path, name: str, arguments: list[str]) -> list[str]:
    relative = Path(name)
    if len(relative.parts) == 1:
        relative = Path("scripts") / relative
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "scripts":
        raise ValueError("English entry must be a repository-relative script")
    script = (root / relative).resolve()
    if not script.is_relative_to(root) or not script.is_file() or script.suffix != ".py":
        raise ValueError("English native script is missing or outside the checkout")
    return [sys.executable, str(script), *arguments]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("root")
    commands.add_parser("paths")
    commands.add_parser("check")
    run = commands.add_parser("run")
    run.add_argument("script")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    bridge = commands.add_parser("bridge")
    bridge.add_argument("script")
    bridge.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        root = discover_repo(args.repo)
        if args.command == "root":
            print(root)
            return 0
        if args.command == "bridge":
            sys.path.insert(0, str(root))
            from english_pipeline.runtime_paths import bridge_root
            base = bridge_root()
            relative = Path(args.script)
            script = (base / relative).resolve()
            if (relative.is_absolute() or ".." in relative.parts or not script.is_relative_to(base)
                    or not script.is_file() or script.suffix != ".py"):
                raise ValueError("Bridge script is missing or outside the resolved companion project")
            python = base / ".venv-study-mcp/bin/python"
            interpreter = str(python) if python.is_file() and os.access(python, os.X_OK) else sys.executable
            command = [interpreter, str(script), *args.arguments]
        elif args.command == "run":
            command = native_command(root, args.script, args.arguments)
        else:
            command = native_command(root, "english_runtime_paths.py", [args.command])
        env = dict(os.environ, KAOYAN_ENGLISH_PROJECT_ROOT=str(root), KAOYAN_ENGLISH_ROOT=str(root), KAOYAN_ENGLISH_ACTIVE_SKILL=str(Path(__file__).resolve().parents[1]))
        return subprocess.run(command, cwd=root, env=env).returncode
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "unavailable", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
