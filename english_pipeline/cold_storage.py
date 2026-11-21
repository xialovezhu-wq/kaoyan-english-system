"""English cold artifacts: verified T9 writes with small local directory aliases.

Existing local artifacts remain readable; necessary writes relocate only that ID.
The shared registry owns volume identity checks and file-level receipt publication.
"""
from __future__ import annotations

import importlib.util
import hashlib
import os
import shutil
import stat
import uuid
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from .errors import ValidationError
from .runtime_paths import REPO_ROOT, storage_map_root
from .util import atomic_write_json, canonical_bytes, exclusive_lock, load_json

CENTRAL_MODULE = storage_map_root() / 'cold_storage.py'
PRODUCTION_COLD_ROOT = Path('/Volumes/T9-Data/02_英语/网页交接与过程资料')
PRODUCTION_STATE_ROOT = REPO_ROOT / 'intake'
COLD_ROOT = PRODUCTION_COLD_ROOT
_active: ContextVar[list[tuple[Path, str, str, bool]] | None] = ContextVar('english_cold_outputs', default=None)


def _central():
    spec = importlib.util.spec_from_file_location('study_storage_cold_runtime', CENTRAL_MODULE)
    if spec is None or spec.loader is None:
        raise ValidationError(f'cold storage runtime unavailable: {CENTRAL_MODULE}')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as exc:
        raise ValidationError(f'cold storage runtime unavailable: {CENTRAL_MODULE}: {exc}') from exc
    return module


def ensure_output(path: Path) -> Path:
    try:
        return Path(_central().ensure_output_path(path, 'english'))
    except Exception as exc:
        raise ValidationError(f'T9 cold output unavailable; no local fallback: {exc}') from exc


def _require_logical_scope(logical: Path, *, explicit: bool = False) -> None:
    """Never route an unconfigured test/scratch intake to real production T9.

    Isolated tests must explicitly inject their cold root and central API fixture.
    This gate runs before directory creation, relocation or registry access.
    """
    if '..' in logical.parts:
        raise ValidationError('cold output logical path cannot contain parent traversal')
    if COLD_ROOT.resolve() != PRODUCTION_COLD_ROOT.resolve():
        return
    if logical.is_relative_to(PRODUCTION_STATE_ROOT):
        return
    if explicit:
        ensure_output(logical)
        return
    raise ValidationError(
        'non-production intake cannot use the production English cold root; '
        'inject an isolated COLD_ROOT and registry fixture, or select an explicit verified T9 output'
    )


def _inventory(root: Path):
    """Hash one exact cold directory without following any internal symlinks."""
    rows, stable = [], []
    def visit(path):
        before = path.lstat()
        rel = path.relative_to(root).as_posix()
        if stat.S_ISLNK(before.st_mode):
            payload = ('symlink', os.readlink(path))
        elif stat.S_ISDIR(before.st_mode):
            payload = ('directory',)
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(before.st_mode):
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for data in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(data)
            payload = ('file', before.st_size, digest.hexdigest())
        else:
            raise ValidationError(f'unsupported file in cold directory: {path}')
        after = path.lstat()
        guard = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if guard(before) != guard(after):
            raise ValidationError(f'cold source changed while hashing: {path}')
        rows.append((rel, *payload, stat.S_IMODE(before.st_mode), before.st_uid, before.st_gid,
                     None if stat.S_ISLNK(before.st_mode) else before.st_mtime_ns))
        stable.append((rel, *guard(before)))
    visit(root)
    return sorted(rows), sorted(stable)


def _verified_registration(root: Path, logical: Path, purpose: str):
    result = _central().register_tree(root, subject='english', purpose=purpose, aliases=[str(logical)])
    proof_path = Path(result['receipt_path'])
    proof_bytes = proof_path.read_bytes()
    if (result.get('status') != 'registered'
            or hashlib.sha256(proof_bytes).hexdigest() != result['receipt_sha256']
            or load_json(proof_path).get('root') != str(root)):
        raise ValidationError('cold relocation registry receipt did not read back')
    return result


def _relocate_legacy(logical: Path, category: str, identity: str) -> None:
    """Copy/verify/register one legacy ID before replacing it with an alias.

    Every failure preserves the local source or its explicitly recorded holding
    directory. Existing T9 files are never overwritten or deleted.
    """
    _require_logical_scope(logical)
    target = ensure_output(COLD_ROOT / category / identity)
    record_path = target.parent / '.relocations' / (identity + '.json')
    lock_path = logical.parent / ('.' + logical.name + '.cold-storage.lock')
    with exclusive_lock(lock_path):
        if record_path.is_file():
            record = load_json(record_path)
            holding = Path(record['holding'])
            if record['logical'] != str(logical) or record['target'] != str(target):
                raise ValidationError('cold relocation identity differs from preserved recovery record')
            if holding.exists():
                wanted = record['content_sha256']
                if (hashlib.sha256(canonical_bytes(_inventory(holding)[0])).hexdigest() != wanted
                        or hashlib.sha256(canonical_bytes(_inventory(target)[0])).hexdigest() != wanted):
                    raise ValidationError(f'cold relocation recovery needs inspection; retained source: {holding}')
                _verified_registration(target, logical, 'legacy-cold-relocation-recovery')
                if logical.is_symlink() and logical.resolve() == target:
                    pass
                elif logical.exists() or logical.is_symlink():
                    raise ValidationError(f'cold relocation has both source and holding; preserved: {holding}')
                else:
                    logical.symlink_to(target, target_is_directory=True)
                if logical.resolve() != target:
                    raise ValidationError('cold relocation alias failed to read back')
                shutil.rmtree(holding)
                atomic_write_json(record_path, {**record, 'status': 'complete'})
                _central().register_tree(record_path, subject='english', purpose='legacy-cold-relocation-receipt')
                return
        if logical.is_symlink():
            ensure_output(logical)
            return
        if not logical.is_dir():
            raise ValidationError(f'legacy cold source is unavailable: {logical}')
        before, guards = _inventory(logical)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            stage = target.parent / ('.' + identity + '.copy-' + uuid.uuid4().hex)
            try:
                shutil.copytree(logical, stage, symlinks=True)
                if _inventory(stage)[0] != before or _inventory(logical) != (before, guards):
                    raise ValidationError(f'cold copy verification failed; source and copy retained: {stage}')
            except BaseException as exc:
                if stage.exists():
                    try:
                        _central().register_tree(stage, subject='english', purpose='legacy-cold-copy:partial')
                    except Exception as register_error:
                        exc.add_note(f'partial cold copy retained at {stage}; registration failed: {register_error}')
                raise
            stage.rename(target)
        if _inventory(target)[0] != before or _inventory(logical) != (before, guards):
            raise ValidationError('legacy cold target differs; source and existing T9 contents preserved')
        registration = _verified_registration(target, logical, 'legacy-cold-directory-verified-copy')
        if _inventory(logical) != (before, guards):
            raise ValidationError('cold source changed after registration; local source retained')
        holding = logical.with_name('.' + logical.name + '.cold-holding-' + uuid.uuid4().hex)
        record = {'status': 'verified_copy', 'logical': str(logical), 'target': str(target),
                  'holding': str(holding), 'content_sha256': hashlib.sha256(canonical_bytes(before)).hexdigest(),
                  'registration': registration}
        atomic_write_json(record_path, record)
        logical.rename(holding)
        try:
            if _inventory(holding)[0] != before:
                raise ValidationError('cold source changed at relocation; holding retained')
            logical.symlink_to(target, target_is_directory=True)
            if logical.resolve() != target or _inventory(holding)[0] != before or _inventory(target)[0] != before:
                raise ValidationError('cold relocation readback failed; holding retained')
        except BaseException:
            if logical.is_symlink() and logical.resolve() == target:
                logical.unlink()
            if not logical.exists():
                holding.rename(logical)
            raise
        shutil.rmtree(holding)
        atomic_write_json(record_path, {**record, 'status': 'complete'})
        _central().register_tree(record_path, subject='english', purpose='legacy-cold-relocation-receipt')


def cold_directory(logical: Path, category: str, identity: str, *, explicit: bool = False) -> Path:
    """Allocate only the requested output directory; retain an old local directory.

    An explicit output override still has to resolve onto the verified T9 volume.
    New default outputs get a directory alias so fixed-path native readers work.
    """
    logical = Path(logical).absolute()
    _require_logical_scope(logical, explicit=explicit)
    if Path(identity).name != identity or Path(category).name != category:
        raise ValidationError('cold output identity/category must be a single path component')
    pending = COLD_ROOT / category / '.relocations' / (identity + '.json')
    if pending.is_file():
        record = load_json(pending)
        if record.get('status') != 'complete' and Path(record['holding']).exists():
            _relocate_legacy(logical, category, identity)
    inherited = logical.resolve().is_relative_to(COLD_ROOT.resolve())
    managed = explicit or inherited or logical.is_symlink() or not logical.exists()
    if explicit or inherited or logical.is_symlink():
        ensure_output(logical)
        logical.mkdir(parents=True, exist_ok=True)
    elif logical.exists():
        if not logical.is_dir():
            raise ValidationError(f'cold output directory is occupied: {logical}')
        # Legacy reads stay local. Only a necessary write triggers verified
        # relocation of this exact cold ID, never its hot parent directories.
    else:
        target = ensure_output(COLD_ROOT / category / identity)
        target.mkdir(parents=True, exist_ok=True)
        logical.parent.mkdir(parents=True, exist_ok=True)
        try:
            logical.symlink_to(target, target_is_directory=True)
        except FileExistsError:
            if not logical.is_symlink() or logical.resolve() != target:
                raise ValidationError(f'cold output alias raced with another path: {logical}')
        ensure_output(logical)
    if explicit:
        logical.mkdir(parents=True, exist_ok=True)
    current = _active.get()
    if current is None:
        raise RuntimeError('cold_directory requires a cold_artifacts entrypoint')
    if all(root != logical for root, _, _, _ in current):
        current.append((logical, category, identity, managed))
    return logical


def guard_write(path: Path, data: bytes | None = None) -> None:
    """Check managed cold writes, leaving holds, receipts and all hot writes alone."""
    path = Path(path).absolute()
    for root, category, identity, managed in reversed(_active.get() or []):
        if path == root or root in path.parents:
            if managed:
                ensure_output(path)
            elif not (path.is_file() and data is not None and path.read_bytes() == data):
                _relocate_legacy(root, category, identity)
                current = _active.get()
                current[:] = [(r, c, i, True if r == root else m) for r, c, i, m in current]
                ensure_output(path)
            return


def cold_artifacts(purpose: str):
    """Register complete or partial cold files at every direct-call boundary."""
    def decorate(fn):
        @wraps(fn)
        def invoke(*args, **kwargs):
            token = _active.set([])
            error = None
            try:
                return fn(*args, **kwargs)
            except BaseException as exc:
                error = exc
                raise
            finally:
                roots = _active.get() or []
                _active.reset(token)
                for root, category, identity, managed in roots:
                    if not managed:
                        continue
                    try:
                        resolved = ensure_output(root)
                        _central().register_tree(resolved, subject='english',
                            purpose=f'{purpose}:{category}' + (':partial' if error else ''),
                            aliases=[str(root)] if root != resolved else [])
                    except Exception as exc:
                        message = f'cold files retained but storage registration failed at {root}: {exc}'
                        if error is not None:
                            error.add_note(message)
                        else:
                            raise ValidationError(message) from exc
        return invoke
    return decorate
