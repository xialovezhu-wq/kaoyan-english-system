---
name: kaoyan-english-daily-intake-curation
description: "Use for explicitly authorized English cutoff intake, real answer/correction/review backflow, local A-advice adjudication, or exact legacy needs_user retirement. Reuse the single typed writer, original evidence/dates and post-commit recovery; never fabricate learning or run background models."
---

# Kaoyan English Daily Intake Curation

## English web-return route

New English webpage project E returns use `kaoyan-english-web-return`, which binds the exact reviewed original/supplement set and reuses this native writer/closeout. Packages with `source.review_route=web` or an existing web-bundle hold remain `waiting_web_review` in ordinary cutoff plans; report their count separately from NOOP and never consume them through a new local-only batch. Existing unheld packages retain the cutoff behavior below.

## Trigger gate

Accept either primary trigger:

开始英语正式入库

开始 YYYY-MM-DD 英语正式入库

The no-date form uses the current Asia/Shanghai calendar date as cutoff. Validate any explicit date. By default both forms include every package with study_date no later than cutoff that lacks a trusted formal terminal. Narrow to cutoff day only or an exact package subset only when the user states that modifier explicitly.

An explicitly authorized update of a real current answer, correction or review, or local processing of an A/B/C/D return, uses `references/learning-backflow-contract.md` for that exact episode. It does not authorize replaying unrelated backlog. Only A advice that the local reviewer accepts or modifies can support corresponding formal changes; rejected advice is recorded as rejected, not mastered.

## Role

The current local reviewer owns the semantic decision; the deterministic writer is the only formal writer. `sol_nightly_reviewer` and `english_sol_actions_v2` are compatibility identifiers, not a model override. Seal the canonical plan, preserve each original study_date and use native read-only leaves only for useful independent evidence work.

## Required reading

Read references/backlog-gate-contract.md for scope and the current plan. For a new apply, read references/nightly-curation-contract.md, references/typed-actions-contract.md and references/sentence-support-contract.md, then every package and formal snapshot named by that daily manifest. Read archive-contract.md only for its eligible post-formal stage/recovery, references/native-luna-read-contract.md only when delegation is useful, and references/output-contract.md for the matching result. A zero-package NOOP does not require unrelated apply/archive evidence or model work. Reuse unchanged contracts within this task.

## Freeze and evidence

Run plan-backlog first. The canonical english_package_backlog_plan_v1 binds cutoff, selection mode, original dates, package states, future exclusions, legacy reachability audit and plan_canonical_sha256. Dashboard or inspection uses --no-persist; the authorized formal turn persists the immutable plan.

The plan status is READY when it selects packages and NOOP when package_count is zero. A zero-package plan, execution receipt and global gate must all remain NOOP while preserving the complete legacy reachability and user_removed audit; it must not freeze, invoke the writer or call archive-nightly.

Process daily_batches in study_date order. Never combine dates in one freeze. Exclude needs_user and damaged packages from freeze/apply while continuing independent pending packages. Resume recovery_required and archive_pending states without another apply.

Each new daily freeze uses english_nightly_manifest_v3, explicitly requires the
sentence-support/display/archive/locator/cleanup post-formal closures, and binds only
that date's selected pending package IDs. Historical v2 manifests are read-only
legacy closeouts; they cannot authorize a new apply or be represented as having
sentence-support.

Reopen each manifest.json, conversation.json, source.json, attachment and receipt.json. Verify exact bytes, file SHA values, package_canonical_sha256, date and subject. missing_fields are evidence gaps, not a reason to lose the package. Put materially ambiguous items in needs_user.

Historical Luna candidate files may be read only as provenance for old records. They never define the new package set, authorize an action or satisfy an evidence ref.

## Local decisions and native read leaves

The current parent keeps the final semantic decisions and writer authority.

If the root task is Ultra, use native proactive orchestration directly and do not invoke the orchestrate Skill.

If the root task is not Ultra, use the unchanged orchestrate Skill only when independent workstreams justify it. Follow a current explicit model requirement; do not impose an old fixed Luna/model/effort/fork/tier on an Ultra root.

When delegated lookup is useful, follow references/native-luna-read-contract.md. The read leaf returns evidence; the same parent rejects scope/hash drift and decides identity, deduplication, error, mastery, relations and typed actions. No background model or consumer processes the learning package.

## Typed writer workflow

1. Persist and reopen one canonical through-cutoff backlog plan.
2. For each original date, resume active/committed or archive-pending work before considering a new freeze; never replay apply for a trusted closeout.
3. Freeze only that date's pending package IDs.
4. Reopen all frozen raw evidence and complete bounded processing-library reads when useful.
5. Sol emits english_sol_actions_v2 and one companion
   english_sentence_support_proposal_v1 with package evidence_refs only. The support
   proposal has no formal write authority and adds no second model call.
6. Run apply-nightly --dry-run against the same manifest and action file.
7. Against the dry-run receipt, simulate same-key support merges and persist one
   `english_sentence_support_preflight_receipt_v1` binding the exact proposal,
   actions, manifest, current generation, completed subset and record/query budgets.
8. Stop that daily batch on formal prehash compare-and-swap or support-preflight conflict, record failed, and continue only independent later dates that remain safe.
9. Apply safe actions once with --authorization equal to the frozen batch_id and
   `--support-preflight` equal to the canonical preflight receipt. The writer
   transaction and receipt must bind its ID/path/SHA and proposal SHA. The backlog CLI itself also requires explicit --apply and --authorization equal to plan_id; its default run is fully read-only.
10. Verify writer receipt, formal posthashes, journal, package and support-preflight bindings.
    The writer invalidates current local learning context and requests one deterministic
    publication after releasing the formal lock. Cloud failure remains pending and
    never rolls back local facts or authorizes another apply.
11. Incrementally refresh and audit sentence-support from the same manifest,
    proposal and canonical writer closeout exactly as defined in
    references/sentence-support-contract.md. Every completed package disposition must
    have a verified support closure, even when no formal row changed.
12. Publish and revalidate package-external display assets and their managed Obsidian embeds exactly as defined in references/archive-contract.md.
13. Follow the T9 locator, pointer and cleanup gates in references/archive-contract.md, then write the final global gate with completed, already_consumed, needs_user, failed and archive_pending.

Do not directly edit bank, learning events or review files. Keep safe actions independent from needs_user items. Current formal values and event trajectories are read by `query-learning-context` on every relevant teaching turn, including already-open conversations; no Skill rewrite or extra model is required after each learning update.

## Formal and archive completion

Local high-speed staging is the only foreground package destination. Never write a fast package directly to the data disk.

After formal writer success, first require
`english_sentence_support_refresh_receipt_v1`. A missing or failed refresh is
`FORMAL_COMMITTED_SUPPORT_PENDING`: retain the local package, resume only the exact
support refresh and never replay apply. Only after the support closure passes may the
workflow publish stable display assets. Practice-safe images go only under `raw/articles/_display_assets/SOURCE_ID/PACKAGE_ID/`; solution and explanation images go only under `raw/protected/conversation-assets/SOURCE_ID/PACKAGE_ID/`; other attachments default to archive-only. Modify only the package-bound article's machine-managed display block and the deterministic protected page. Require `english_display_asset_closure_receipt_v1`, valid embeds, stable/source SHA equality and zero temporary/T9 absolute references before T9 archive or cleanup.

Then run archive-nightly. It verifies the fixed T9-Data sentinel and UUID, stages and verifies the complete package, atomically finalizes it under the fixed English raw-conversation root, writes and reopens wiki/raw_archives/PACKAGE_ID.md with retrieval_keys, then writes archive and cleanup receipts. Only after display closure, T9 archive, locator, pointer and cleanup checks may it remove the local package root.

A selected completed package enters completed or already_consumed only after the canonical writer closeout, sentence-support closure, archive receipt, exact locator reread, cleanup intent, archive-pointer and cleanup receipt all reopen with matching package and hash bindings. Missing or drifted completion evidence remains archive_pending with its exact pending_component. Resume only the named post-formal component from the same closeout; never replay apply.

Archive only packages whose writer disposition is completed, including created, updated, already-current, skip, no-op and duplicate outcomes. formal_ids may be empty or contain the actual assigned IDs. A needs_user, failed or otherwise incomplete package remains complete in local staging and is never cleaned.

Any support-refresh failure after formal commit is
FORMAL_COMMITTED_SUPPORT_PENDING. Any later display, archive, locator or cleanup
failure is FORMAL_COMMITTED_ARCHIVE_PENDING. Preserve the local package unless a
durable pointer and cleanup intent prove deletion already occurred; report
pending_component as support_refresh, display_assets, archive, locator or cleanup,
and resume only that same post-apply chain. Never rerun apply. This Skill does not edit any Obsidian .base file or user-authored article body.

## Explicit legacy residual retirement

Only an explicit user request to delete/remove exact current legacy `needs_user`
events authorizes retirement. Run `retire-legacy-events` first without `--apply` for
a zero-write preview over repeated explicit `--event-id` values. Apply only with
`--authorization` exactly equal to the preview-derived retirement ID.

The command writes one immutable, content-addressed
`english_legacy_event_retirement_v1` receipt with disposition `user_removed`. It
binds every source event and canonical capture receipt by path and SHA. It never
deletes or rewrites them, creates no package/evidence sidecar, performs no migration,
writer, archive or formal write, and does not mean completed/consumed/mastered.
Conflicts or hash drift fail closed. A fresh v2 legacy audit preserves each record as
`status=user_removed`, `actionable=false`, with zero `needs_user` contribution.

Legacy events without authoritative hash-bound complete-conversation evidence can never be synthesized or archived as conversation packages. The nine events bound by the 2026-08-27 user_removed retirement receipt remain preserved administrative evidence only: they are not packages, formal terminals, completed items or archive candidates.

## Output

Use references/output-contract.md. Report cutoff, plan/SHA, each original date,
typed-action/writer result, sentence-support refresh/audit, unresolved gaps, archive
verification and the five-list global gate. Ask one minimal question only for a
decision that changes unresolved actions.

## Stop rules

- No recognized start trigger: do not plan, freeze or write.
- No pending packages through cutoff: NOOP while still reporting trusted consumed and legacy audit state when requested.
- Package drift: FAILED, no writer.
- Missing evidence that changes semantics: needs_user; do not refreeze it in the same plan, preserve it and continue independent safe packages.
- Formal prehash conflict: stop that daily batch and require a fresh authorized backlog plan.
- Formal writer success but archive receipt, locator reread, cleanup intent, pointer or cleanup receipt is incomplete: FORMAL_COMMITTED_ARCHIVE_PENDING and keep or recover the package under archive_pending.
- Formal writer success but sentence-support receipt or audit is incomplete:
  FORMAL_COMMITTED_SUPPORT_PENDING, keep the package local, refresh support only and
  never repeat formal apply.
- Legacy capture event eligibility is audit-only. Never synthesize old dialogue, create a package or claim consumption without a later explicit migration task.
- Never fall back to any retired external processing route or old evidence authority.

## Automatic local MCP snapshot closeout

An authorized formal intake includes the existing local-context refresh and final MCP snapshot publication; do not ask the user to separately request synchronization. English retains its native learning context and sentence-support contracts, not the new math/408 knowledge-point table. After the final archive/locator/cleanup closure, follow the existing `complete_formal_closeout` with its archive basis and reopen its publication receipt. `STARTED`, an old `PUBLISHED` receipt or local support refresh alone is not proof that the final source version is available to MCP.

Run `/usr/local/bin/python3 /Users/your-user/Documents/Study-Pro-Bridge/study_publication.py verify-current --subject english` after the deterministic publisher finishes. Require `PUBLISHED_CURRENT` before claiming the MCP copy is current. Preserve pending state and report the exact remaining synchronization component if it cannot complete; recover only the existing publication/closeout, using `recover-nightly --retry-publication` for its existing transaction when applicable. Never repeat formal apply, create a new learning event or require GitHub push to repair a local snapshot. A zero-package NOOP still does not start an unrelated publication.

## Formal-only point personalization

After the actual formal writer commits, update only the affected word/phrase, syntax, option_type and error_type summaries from the fully read, accepted learning evidence. Preserve first attempts, explicit corrections, observed later user revisions and hint dependence; no unobserved mastery. Follow `/Users/your-user/.codex/skills/kaoyan-english-intensive-reading/references/point-personal-summary.md` and use `publish-personal-summaries --input-json POINTS.json --writer-receipt WRITER_RECEIPT.json`. Merge point episodes across parts instead of overwriting earlier contributions. Ordinary teaching and quick Capture never update these summaries. If this post-commit step fails, report it pending and resume only summary publication, never replay formal apply.
