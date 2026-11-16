# Per-Date Package Freeze and Writer Contract

Read backlog-gate-contract.md first. A through-cutoff plan may contain several dates, but each freeze remains one original study_date. Pass exact package IDs when a plan subset applies; never freeze needs_user, damaged, already-consumed or future packages.

In v7, an authorized writer apply binds formal model review for the exact frozen sources and typed actions when its policy is enabled. Embeddings ranks bounded English formal and same-batch candidates. Decisions labels the comparison evidence for review. Read the bound cold receipt through the writer result. The frozen evidence and final reviewed typed actions remain the writer's authority.

The formal review receipt lists the exact snapshot count, protected label vectors reused, candidate pool, shortlist and comparisons delivered for each action. The explicit formal ID stays in the pool and shortlist. A missing target is `unresolved_target`. A `new` relation applies only to the stated comparison scope after its required records have been compared. Read the coverage before drawing conclusions about the rest of the snapshot.

Check `source_coverage` for each locator, source hash, speaker, message sequence, UTF-8 ranges delivered to Decisions and unread ranges. Follow those locators back to the complete original for any missing qualification, antecedent, prompt or correction. If the original still leaves a material gap, keep that relationship unresolved in the typed draft. The 2,200-byte evidence budget is for formal review. The teaching view keeps its existing budget and scope.

The writer uses the same receipt for the exact manifest and actions or prepares it before taking the formal write lock, including on the approved web-return route. Its compact binding is recorded in the transaction and apply receipt. A nonempty duplicate-only batch still enters review. Exact retries reuse successful stages. Provider failure or request-budget exhaustion leaves the batch unstarted and can resume from its saved receipt. Reconcile ambiguous network outcomes before repeating a request. Open transactions follow the existing recovery entry. An empty batch, validation-only run or post-commit recovery makes zero provider calls.

Freeze:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py freeze-nightly \
  --state-dir /Users/your-user/Documents/kaoyan-english/intake \
  --date YYYY-MM-DD \
  [--package-id EN-PKG-...]
```

Require english_freeze_receipt_v1 and english_nightly_manifest_v3. The v3 manifest
contains only locally staged, unprocessed immutable package records, current formal
prehashes, `postformal_contract_version=sentence-support-v1`, exact
`required_postformal_closures`, and its sentence-support history scope. Historical v2
manifests remain readable only for legacy recovery/archive and cannot authorize a new
apply. The same Sol that prepares `english_sol_actions_v2` also prepares one
`english_sentence_support_proposal_v1` bound to this manifest and exact package
evidence; read sentence-support-contract.md.

Dry-run:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py apply-nightly \
  --state-dir /Users/your-user/Documents/kaoyan-english/intake \
  --manifest <manifest.json> --actions <actions.json> --dry-run
```

After dry-run and before apply, require a canonical
`english_sentence_support_preflight_receipt_v1` for the companion support proposal.
The authorized apply passes it as `--support-preflight <receipt.json>`; v3 apply
without the exact receipt fails closed.

Apply after a valid dry-run:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py apply-nightly \
  --state-dir /Users/your-user/Documents/kaoyan-english/intake \
  --manifest <manifest.json> --actions <actions.json> \
  --support-preflight <preflight-receipt.json> \
  --apply --authorization <batch_id>
```

The deterministic writer keeps lock, formal prehash compare-and-swap, atomic replacement, journal, receipt and recovery behavior. It binds package_ids, package_sha256s, per-package terminal dispositions and resolved evidence-node hashes in its receipt. A PARTIAL receipt processes only completed packages. needs_user, failed and incomplete packages remain local, but are not refrozen within the same backlog plan. A later explicit plan may reconsider them only after the missing decision or evidence changes. After the canonical writer closeout, refresh sentence-support from the companion proposal before any display or archive operation. Also verify the affected personal summary and memory index with the original writer receipt. A support or memory failure never changes or replays the writer result. For a verified writer receipt with pending `memory_index`, use `recover-memory-closeout --writer-receipt` and verify the current audit and scope. Keep the live lifecycle and source vetoes when recovering. An old generation is only a historical evidence source.

If the process crashes after the apply receipt is durable but before receipt_closed, run recover-nightly for the canonical transaction. Recovery verifies transaction, receipt, manifest, staged/preimage hashes, formal posthashes, resolved evidence and the full journal chain, then appends exactly one receipt_closed. Repeating recovery must not append a second close record or replay apply.

Default run-backlog is validation-only and must not call freeze-nightly, recover-nightly, apply-nightly or archive-nightly. Only an authorized apply-mode run may invoke this contract.
