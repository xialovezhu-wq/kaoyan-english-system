# Post-Formal T9 Archive Contract

Foreground packages exist only under the local high-speed staging root.

In a through-cutoff backlog, archive is still per original study_date and per committed manifest. An existing trusted closeout with archive_pending resumes this command only; never regenerate actions or rerun apply. Archive failure remains residual while later independent dates continue.

After an apply receipt reports APPLIED, PARTIAL or NO_ACTION and binds the same package SHA list, first require the exact completed subset to have a verified
`english_sentence_support_refresh_receipt_v1`. Then run:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py archive-nightly \
  --repo-root /Users/your-user/Documents/kaoyan-english \
  --state-dir /Users/your-user/Documents/kaoyan-english/intake \
  --manifest <manifest.json> \
  --writer-receipt <english_apply_receipt_v1.json>
```

The command never calls apply or rebuilds sentence-support. It rejects a completed
package whose support closure is missing, stale, answer-unsafe or does not bind the
same manifest, package SHA and writer receipt.

## Stable Obsidian display gate

Before copying to T9, `archive-nightly` deterministically runs the equivalent of:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py publish-display-assets \
  --repo-root /Users/your-user/Documents/kaoyan-english \
  --state-dir /Users/your-user/Documents/kaoyan-english/intake \
  --package-root <exact-local-package-root>
```

The immutable package is unchanged. `english_display_asset_plan_v1` is a sidecar under `intake/display-assets/plans/`. The writer, not Sol, derives every destination:

- only answer-free question, source-article and user-work images from an answer-free package with a verified target article go to `raw/articles/_display_assets/SOURCE_ID/PACKAGE_ID/`;
- solution/explanation images and all display images in an explanation or protected/unknown-exposure package go to `raw/protected/conversation-assets/SOURCE_ID/PACKAGE_ID/`; a handwritten user answer is not made practice-safe by its attachment role;
- other attachments, and practice-safe images without a verified target article, are archive_only.

Only the exact package-bound article's `english-display-assets` managed block and the deterministic protected page may change. Existing stable bytes with the same SHA are reused; a different byte at the deterministic target fails closed and is never overwritten or renamed. Require `english_display_asset_closure_receipt_v1`, resolved vault-relative embeds, stable/source byte and SHA equality, and a PASS scan proving no package, attachment, local absolute or `/Volumes/T9-Data` reference remains on the managed display surface.

Before archive, require:

- archive_root is /Volumes/T9-Data
- /Volumes/T9-Data 1 does not exist
- sentinel is /Volumes/T9-Data/00_迁移管理/状态/volume-sentinel.json
- sentinel SHA-256 is f086b32b29b2b38f1a28dffb8fde4fa850078332d8a23ae269cc8e857a6bd2ca
- volume_name is T9-Data
- volume_uuid is 00000000-0000-0000-0000-000000000000
- English subject-relative root is 02_英语/资料库/原始会话资料

For each package, stage-copy every file, compare the complete tree and package manifest, then atomically finalize to:

02_英语/资料库/原始会话资料/YYYY-MM-DD/PACKAGE_ID

Only a completed per-package writer disposition is eligible: created, updated, already-current, skip, no-op and duplicate outcomes all qualify. The archive command independently derives terminal outcomes and formal_ids from the canonical writer receipt, its action results and receipt-closed journal binding; callers cannot supply or omit them. formal_ids may validly be empty. needs_user, failed and incomplete packages remain in local staging and receive no cleanup.

Write wiki/raw_archives/PACKAGE_ID.md with exactly these frontmatter fields:

- type: raw_archive_locator
- subject: english
- package_id
- formal_ids as a list
- retrieval_keys containing package ID, original study_date, formal IDs and available source/article/question/unit IDs
- archive_volume: T9-Data
- raw_archive_relpath relative only to the T9 root
- raw_archive_manifest_sha256
- raw_archive_package_sha256
- archive_verified_at
- archive_status: verified

Reopen and verify the locator note. Do not edit any .base file.

Persist a deterministic archive intent before copying. Only after the display closure, archive receipt and reopened locator binding all pass may cleanup proceed. Persist both cleanup intent and archive-pointer before deleting the local package root, then write the cleanup receipt. Those three cleanup documents bind display closure receipt ID/path/SHA, formal-reference scan SHA, stable-asset count, no-display proof and pending_component. Any injected or real failure resumes from those durable intents without rerunning apply; an absent local package without both intent and pointer fails closed.

The archive intent, archive receipt, package result, cleanup intent, pointer and cleanup receipt all bind the exact archive_root, package SHA, relative archive path, writer receipt, sentence-support receipt ID/path/SHA, support key, support record SHA, support index generation SHA, locator SHA and display closure. Completion evaluation rereads the support closure, stable assets and embeds, retrieval_keys, locator fields and archived package, then verifies the cleanup evidence and confirms the local package is absent. Any missing, malformed or drifted member keeps the selected package out of completed and already_consumed.

If a failure occurs after the formal closeout, classify the package as archive_pending with pending_component=`display_assets|archive|locator|cleanup`. Resume only the named post-apply stage from the same writer closeout. If the local package was removed before the cleanup receipt became durable, require the exact pointer and cleanup intent before finishing the receipt. Never regenerate actions or invoke apply again.

After all requested archive/cleanup work passes, the lock-free post-closeout hook
uses the approved shared publisher with `WRITER_EVENT:closeout:HASH_PREFIX`.
The hash binds existing manifest, package, display and support content/dependency
hashes plus the completed cleanup set. It excludes timestamps, PIDs and retry copy
status. This is a publication version only; it never appends a learning event.
The archive result includes the publication status. Cloud failure preserves every
local formal/archive receipt and remains publication-pending without another apply.
