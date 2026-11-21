"""Advisory evidence flags for an already validated English web return.

This module never approves advice, changes a review template, or calls a writer.
It reuses native immutable-package and evidence-pointer validation before the
shared Decisions pre-screen sees text. Only exact input packages are reopened.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any, Callable

from .packages import validate_conversation_package, validate_evidence_refs
from .util import bytes_sha256, load_json, object_sha256
from .runtime_paths import REPO_ROOT, bridge_root

BRIDGE = bridge_root()
PRODUCTION_REPO = REPO_ROOT
ADAPTER_VERSION = 'english-web-evidence-prescreen-v1'
MAX_CLAIMS = 200
MAX_SOURCE_BYTES = 256 * 1024
LEARNER_LANGUAGE = re.compile(
    r'独立|掌握|提示|理解|误译|纠正|修正|不会|未能|学会|已会|自报|偏好|喜欢|'
    r'首次|首答|复发|断点|迁移|independen|master|hint|understand|prefer|self.report', re.I)


class _EvidenceUnavailable(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _unavailable(reason: str, *, count: int = 0, error_type: str | None = None) -> dict[str, Any]:
    result = {'status': 'not_screened', 'advisory_only': True,
              'not_flagged_is_not_approved': True, 'report_path': None,
              'checked_count': 0, 'needs_review_count': 0, 'not_screened_count': count,
              'api_requests': 0, 'cache_hits': 0, 'reason': reason,
              'formal_write_count': 0, 'learner_evidence_write_count': 0}
    if error_type:
        result['error_type'] = error_type
    return result


def _runner() -> Callable[..., dict[str, Any]]:
    if str(BRIDGE) not in sys.path:
        sys.path.insert(0, str(BRIDGE))
    from study_evidence_prescreen import run_prescreen
    return run_prescreen


def _indexed_refs(record: dict[str, Any], refs: Any) -> tuple[Any, str | None]:
    indexes = record.get('evidence_ref_indexes')
    if (not isinstance(refs, list) or not isinstance(indexes, list) or not indexes
            or any(type(i) is not int or i < 0 or i >= len(refs) for i in indexes)):
        return [], 'missing_or_invalid_evidence_indexes'
    return [refs[i] for i in indexes], None


def _claims(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []

    def add(pid, path, value, kind, refs=None, problem=None):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        if not text.strip():
            return
        claims.append({'claim_id': 'EN-CLAIM-' + object_sha256([pid, path, value])[:24],
                       'field_path': path, 'claim': text, 'claim_kind': kind,
                       '_package_id': pid, '_refs': refs, '_problem': problem})

    for ordinal, item in enumerate(items):
        if item.get('formal_status') == 'already_formal':
            continue
        pid = item['package_id']; prefix = f'/items/{ordinal}'
        reason = item.get('reason')
        if isinstance(reason, str) and LEARNER_LANGUAGE.search(reason):
            add(pid, prefix + '/reason', reason, 'learning_state')
        support = item.get('support_record')
        if isinstance(support, dict):
            refs = support.get('evidence_refs')
            for i, episode in enumerate(support.get('history_episodes') or []):
                if isinstance(episode, dict):
                    selected, problem = _indexed_refs(episode, refs)
                    add(pid, prefix + f'/support_record/history_episodes/{i}',
                        {k: episode.get(k) for k in ('state', 'summary')},
                        'learning_state', selected, problem)
            cues = support.get('teaching_cues')
            if isinstance(cues, dict):
                for name, values in cues.items():
                    if not isinstance(values, list):
                        continue
                    for i, cue in enumerate(values):
                        if isinstance(cue, dict):
                            selected, problem = _indexed_refs(cue, refs)
                            add(pid, prefix + f'/support_record/teaching_cues/{name}/{i}',
                                {k: cue.get(k) for k in ('text', 'evidence_state')},
                                'teaching_assertion', selected, problem)
        for i, action in enumerate(item.get('actions') or []):
            if not isinstance(action, dict):
                continue
            path = prefix + f'/actions/{i}'; refs = action.get('evidence_refs')
            kind = action.get('action_type')
            if kind == 'learning_event_append' and isinstance(action.get('event'), dict):
                event = action['event']
                add(pid, path + '/event', {k: event[k] for k in (
                    'outcome', 'hint_dependence', 'reasoning_status', 'user_response',
                    'reasoning', 'correction') if k in event}, 'learning_state', refs or [])
            elif kind == 'mastered_insert':
                add(pid, path, {'requested_action': 'mastered_insert',
                    'mastery_evidence_kind': action.get('mastery_evidence_kind'),
                    'reason': action.get('reason'), 'item': (action.get('row') or {}).get('item')},
                    'learning_state', refs or [])
            for container in ('row', 'updates'):
                data = action.get(container)
                note = data.get('review_note') if isinstance(data, dict) else None
                if isinstance(note, str) and LEARNER_LANGUAGE.search(note):
                    add(pid, path + f'/{container}/review_note', note, 'learning_state', refs or [])
            action_reason = action.get('reason')
            if isinstance(action_reason, str) and LEARNER_LANGUAGE.search(action_reason):
                add(pid, path + '/reason', action_reason, 'learning_state', refs or [])
    return claims


def prescreen_return(*, repo: Path, state: Path, original: dict[str, Any],
                     manifest: dict[str, Any], items: list[dict[str, Any]],
                     return_id: str, return_sha256: str, output_dir: Path,
                     runner: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Best-effort sidecar; every failure preserves the native prepare result."""
    claim_count = 0
    runner_started = False
    try:
        claims = _claims(items)
        claim_count = len(claims)
        if len(claims) > MAX_CLAIMS:
            return _unavailable('claim_budget_exceeded', count=len(claims))
        # Existing native tests and scratch checkouts must never reach the user's
        # real API credential. Tests opt in with an explicitly injected fake.
        if Path(repo).resolve() != PRODUCTION_REPO.resolve() and runner is None:
            return _unavailable('isolated_repository_requires_injected_runner', count=len(claims))
        bindings = {r['package_id']: r for r in original['packages']}
        packages: dict[str, tuple[Path, dict[str, Any]]] = {}

        def package(pid: str):
            if pid not in bindings:
                raise _EvidenceUnavailable('evidence_outside_return_scope')
            if pid not in packages:
                # Import lazily to avoid a module cycle; this is the original
                # hot/archive locator, not a path supplied by return JSON.
                from .web_review import package_root
                root = package_root(state, repo, pid)
                m = validate_conversation_package(root)
                if m['package_canonical_sha256'] != bindings[pid]['package_sha256']:
                    raise _EvidenceUnavailable('original_package_hash_changed')
                packages[pid] = root, m
            return packages[pid]

        def member(pid: str, name: str, *, locator: str | None = None):
            root, m = package(pid)
            descriptor = m['files'][name]
            if descriptor['bytes'] > MAX_SOURCE_BYTES:
                raise _EvidenceUnavailable('evidence_exceeds_adapter_budget')
            raw = (root / name).read_bytes()
            if bytes_sha256(raw) != descriptor['sha256']:
                raise _EvidenceUnavailable('original_member_hash_changed')
            return {'source_id': pid + ':' + name, 'sha256': descriptor['sha256'],
                    'text': raw.decode('utf-8'), 'role': 'conversation' if name == 'conversation.json' else 'source',
                    'locator': locator or name}

        normalized = []
        for record in claims:
            item = {k: v for k, v in record.items() if not k.startswith('_')}
            item.update(evidence=[], evidence_complete=False)
            if record['_problem']:
                item['evidence_problem'] = record['_problem']; normalized.append(item); continue
            try:
                pid = record['_package_id']; package(pid)
                refs = record['_refs']
                selected = {pid}; source_members: dict[tuple[str, str], str] = {}
                if refs is not None:
                    if not isinstance(refs, list) or not refs:
                        raise _EvidenceUnavailable('missing_evidence_refs')
                    for ref in refs:
                        if not isinstance(ref, dict) or ref.get('package_id') not in bindings:
                            raise _EvidenceUnavailable('evidence_outside_return_scope')
                        package(ref['package_id'])
                    documents = [{'package_id': p, 'package_sha256': bindings[p]['package_sha256'],
                                  'path': str(packages[p][0])} for p in packages]
                    resolved = validate_evidence_refs(refs, documents)
                    for row in resolved:
                        binding = row['binding']; p = binding['package_id']; selected.add(p)
                        if binding['resolved_type'] == 'attachment':
                            raise _EvidenceUnavailable('attachment_evidence_not_text_screened')
                        if binding['resolved_type'] == 'source_value':
                            source_members[(p, 'source.json')] = binding['pointer']
                for p in sorted(selected):
                    _, m = package(p)
                    if any(a.get('role') == 'user_work_image' for a in m.get('attachments', [])):
                        raise _EvidenceUnavailable('image_work_not_text_screened')
                    item['evidence'].append(member(p, 'conversation.json'))
                for (p, name), locator in sorted(source_members.items()):
                    item['evidence'].append(member(p, name, locator=locator))
                item['evidence_complete'] = True
            except Exception as exc:
                item['evidence'] = []
                item['evidence_problem'] = (exc.code if isinstance(exc, _EvidenceUnavailable)
                    else 'invalid_or_unavailable_original_evidence:' + type(exc).__name__)
            normalized.append(item)
        execute = runner or _runner()
        runner_started = True
        result = execute(subject='english', artifact_id=return_id,
            source_version=manifest['source_commit'], items=normalized, output_dir=output_dir,
            context={'adapter_version': ADAPTER_VERSION, 'bundle_id': manifest['bundle_id'],
                     'return_zip_sha256': return_sha256, 'input_zip_sha256': manifest['input_zip_sha256'],
                     'scope': 'exact_return_packages_only', 'no_answer_unlock': True})
        if not isinstance(result, dict) or result.get('advisory_only') is not True:
            return _unavailable('invalid_prescreen_result', count=len(claims))
        return result
    except Exception as exc:
        result = _unavailable('prescreen_unavailable', count=claim_count, error_type=type(exc).__name__)
        result['api_requests'] = None if runner_started else 0
        return result
