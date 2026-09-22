# Package-Backed Article Completion

For the new web-review workflow, use the stronger formal-ready gate:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_vocab_handoff.py --source-id <source_id>
```

It revalidates every required `review_route=web` package through the native writer,
sentence-support, display and verified archive-cleanup chain before creating one
idempotent article package snapshot and one handoff receipt. Use the returned
`handoff_path` to start a new task with the user's selected DeepSeek V4.1 Flash
provider. A pending or local-only package is not formal-ready.

The older output-only snapshot route remains available when explicitly requested:

Run:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py complete-article \
  --state-dir /Users/your-user/Documents/kaoyan-english/intake \
  --source-id <source_id> \
  --idempotency-key <stable-completion-key> \
  [--date YYYY-MM-DD]
```

Require english_article_completion_receipt_v2, status created or idempotent_noop, exact package_ids/package_sha256s, formal_write_count=0 and background_processing=none.

This older receipt proves the package snapshot only. It does not prove that a web
review was formally applied or that support/archive closure completed.

The JSON and Markdown snapshots list immutable conversation packages and do not authorize formal entry. Completion starts no external or background processing.

The snapshot includes all matching local packages and exact pointer/locator-verified archived packages through the optional completion date. The date is a cutoff, not permission to omit earlier episodes. A missing/drifted named archive fails completeness; no T9 enumeration or synthetic replacement is allowed. Replaying a completion may repair locator-only snapshot paths without changing any immutable package or learning record.
