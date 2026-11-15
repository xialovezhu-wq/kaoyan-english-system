"""Machine-local locations for the existing English entrypoints.

The checkout owns its hot data. Companion projects normally live beside it;
explicit overrides are honored without silently switching to another copy.
No resolver creates a directory, scans a volume or opens an application.
"""
from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def configured_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    path = Path(os.path.expandvars(value)).expanduser() if value else default.expanduser()
    if not path.is_absolute():
        raise ValueError(name + " must be an absolute path")
    return path.resolve()


def companion_root(name: str, variable: str, *, repo_root: Path = REPO_ROOT) -> Path:
    if os.environ.get(variable):
        return configured_path(variable, repo_root.parent / name)
    candidates = (repo_root.resolve().parent / name, Path.home() / "Documents" / name)
    return next((path.resolve() for path in candidates if path.is_dir()), candidates[0])


def bridge_root() -> Path:
    return companion_root("Study-Pro-Bridge", "STUDY_BRIDGE_ROOT")


def storage_map_root() -> Path:
    return companion_root("Study-Storage-Map", "STUDY_STORAGE_MAP_ROOT")
