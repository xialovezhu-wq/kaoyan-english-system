# Nightly Sentence-Support Contract

`sentence-support` is a deterministic, rebuildable tutoring projection. Complete
conversation packages, formal files, writer receipts and verified archives remain the
truth. The projection exists only to let `kaoyan-english-intensive-reading` obtain one
small, evidence-bound record without scanning Obsidian, formal tables or T9.

## Same-Sol proposal

For every frozen daily manifest, the same Sol that emits `english_sol_actions_v2`
also emits one package-bound `english_sentence_support_proposal_v1`. It adds no model
call and carries no write authority. Every proposed correct-part, confirmed breakpoint,
outcome state, vocabulary link or structure link must use the same exact package
`evidence_refs` contract as formal actions. Current-turn tutoring evidence is never
reconstructed from a summary.

The proposal may contain source identity, paragraph context, sentence spans,
translation invariants, directly evidenced historical episodes, candidate lexical and
sentence-pattern links, and optional question-sidecar locators. It must not contain a
future tutoring reply, a unique mandatory Chinese translation, a predicted future
error, an unsupported learner profile, a mastery claim, or protected answers and
publisher analysis.

## Durable preflight and writer binding

After formal dry-run and before apply, the deterministic support preflight simulates
same-key merges against the current generation, validates answer-safety and projects
the 12 KiB record and 16 KiB hot-query budgets. Persist one canonical
`english_sentence_support_preflight_receipt_v1` binding the manifest, actions SHA,
dry-run action set and completed subset, current generation, content-addressed
proposal path/SHA and projected records.

The v3 writer requires this receipt through `--support-preflight`. Its transaction,
apply receipt and recovery closeout bind the receipt ID/path/SHA and proposal SHA.
Missing, drifted, replaced or oversized proposal evidence stops before formal apply;
post-commit refresh may use only the exact writer-bound persisted proposal.

## Post-writer refresh

After the canonical writer closeout verifies, and before display publication or
archive, run the deterministic sentence-support refresh against the same manifest,
proposal and writer receipt. Require:

- `english_sentence_support_record_v1` content-addressed objects;
- one atomically replaced `english_sentence_support_index_v1` current projection;
- `english_sentence_support_refresh_receipt_v1` binding the previous and new index
  SHA, package IDs and SHAs, proposal SHA, writer receipt SHA, completed subset,
  per-record old/new SHAs and resolved formal/SP ID locators;
- answer-safety validation, `formal_write_count=0`, and
  `formal_apply_invocation_count=0`.

The refresh covers every completed writer disposition, including created, updated,
already-current, duplicate, skip and no-formal-change outcomes. A new learning episode
is valuable even when no formal row changes. A PARTIAL batch refreshes only its
completed package subset; needs-user packages remain local and cannot contribute an
unverified ready episode.

Support records store formal and SP identities as locators only; they do not cache
formal row meanings, mastery state or row hashes. A tutoring or vocabulary consumer
uses `query-learning-context` to resolve related stable bank/SP/concept IDs against
current formal sources and recorded events, with a fresh formal_version every turn.
Therefore an unrelated formal-row update does not require rewriting old sentence
objects and cannot leave a cached semantic snapshot stale.

## Completion and recovery

Archive, cleanup and trusted-consumed evaluation must bind and revalidate the support
receipt, support key, record SHA and index generation SHA for each completed package.
If proposal validation fails before formal apply, stop the daily batch before the
writer. If the writer has committed but refresh is missing, stale or failed, report
`FORMAL_COMMITTED_SUPPORT_PENDING` with `pending_component=support_refresh`, retain
the local package, and do not call archive.

Recovery reuses the exact manifest, proposal and canonical writer closeout. It runs
only support refresh and its audit. After PASS it continues display/archive/cleanup.
It never regenerates formal actions or repeats dry-run/apply. Missing support closure
prevents COMPLETE and already-consumed even when other post-formal evidence exists.

With zero selected packages and no prior support debt, report
`support_status=NOOP_NO_PACKAGES`. A prior support-pending closeout must still be
resumed; today's empty package set is not permission to ignore it.

`history_complete_through` is present only when the refresh covered the complete local
date and excluded no package. Package-subset or needs-user refreshes use
`coverage_status=partial`, retain `latest_refresh_study_date`, and keep
`history_complete_through=null`; never present a partial slice as complete history.

## Tutoring read boundary

The current sentence and the user's current first translation are always evaluated
before history. The actual teaching entry uses `query-learning-context` once for
related current formal values, source-bound support and recorded trajectories.
Its formal_version is obtained again on the next relevant turn, including an already
open conversation. Bind a current article locator or both hashes for external text;
missing/deleted/changed evidence cannot be proved fresh by the cache's old hash.
Use the exact paragraph window and one or two relevant episodes in the explanation,
retaining reported history coverage. History changes teaching, not the question's answer.

Missing, stale, ambiguous, tampered or no-match support fails soft immediately to the
current sentence and current translation. Do not rebuild while the learner waits. An
exact archived package may be reopened only through an already named locator and only
when the bounded record plus current evidence are insufficient.
