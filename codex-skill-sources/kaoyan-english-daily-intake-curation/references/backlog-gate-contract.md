# Through-Cutoff Backlog and Global Gate Contract

Build the canonical plan:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py plan-backlog \
  --repo-root /Users/your-user/Documents/kaoyan-english \
  --state-dir /Users/your-user/Documents/kaoyan-english/intake \
  [--cutoff-date YYYY-MM-DD] \
  [--only-today] \
  [--package-id EN-PKG-...]
```

Omitting cutoff uses the current Asia/Shanghai date. Default selection is through_cutoff: every package with study_date no later than cutoff and no trusted terminal. only-today and package-id narrow only when explicitly supplied. Future packages remain future_excluded.

The immutable plan uses english_package_backlog_plan_v1 and binds plan_id plus plan_canonical_sha256. For Dashboard or inspection, add --no-persist; this is completely read-only.

The plan status is READY when packages are selected and NOOP when packages is empty. The zero-package execution receipt and english_package_backlog_global_gate_v1 must also be NOOP, retain the legacy reachability/user_removed audit, and contain no writer or archive invocation.

run-backlog without --apply is also completely read-only. It does not freeze, recover, archive or write a gate. It only validates the plan and returns would_freeze and would_resume. A real run requires both:

```text
--apply --authorization <exact-plan_id>
```

An authorized run processes original study_date values in ascending order. It never crosses dates in one freeze. needs_user and damaged packages remain residual and do not enter freeze/apply; later independent dates continue. recovery_required resumes only its exact transaction. PARTIAL and committed closeouts are reused; apply is never repeated. archive_pending first resumes support_refresh when that is the named pending component, otherwise it resumes only the named display/archive/locator/cleanup component.

The final english_package_backlog_global_gate_v1 lists every selected package exactly once under completed, already_consumed, needs_user, failed or archive_pending. --no-archive cannot produce completed; a formally committed package remains archive_pending until sentence-support, archive receipt, exact locator reread, cleanup intent, pointer and cleanup receipt all verify. Support or archive failure resumes from the same closeout and never repeats apply.

Legacy events are separate. audit-legacy-events is read-only and returns eligible-for-explicit-migration only when canonical capture receipt plus a hash-bound authoritative complete-conversation sidecar can form a verifiable package. Otherwise it returns needs_user. This workflow always reports migration_performed=false and never migrates a legacy event.

An explicit request to delete exact legacy `needs_user` events uses a two-stage
administrative retirement, not migration or physical deletion:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py retire-legacy-events \
  --state-dir /Users/your-user/Documents/kaoyan-english/intake \
  --event-id EVT-... [--event-id EVT-...]
```

The default preview writes nothing and returns a content-derived retirement ID. A
real mutation requires `--apply --authorization <exact retirement_id>`. The immutable
receipt binds every event and capture receipt SHA with `disposition=user_removed`.
Legacy audit v2 retains those rows with `actionable=false` and excludes them from
`needs_user`; it never labels them completed, consumed, migrated, mastered or formal.

An event without authoritative hash-bound complete-conversation evidence cannot be synthesized, frozen or archived as a package. The nine events in the 2026-08-27 user_removed retirement receipt remain administrative evidence only.
