---
name: kaoyan-english-vocab-export
description: "Produce a source-backed A/B/C or full BBDC vocabulary export. For the new web workflow, start only from a formal-ready handoff after local GPT-6 has applied reviewed advice; preserve the user's selected DeepSeek V4.1 Flash provider and current mastery state."
---

# Kaoyan English Vocab Export

## Role

Produce a source-backed output-only A/B/C or full BBDC list from the verified article and exact packages. In the new web workflow, local GPT-6 owns review and formal application; a new user-selected DeepSeek V4.1 Flash task owns the BBDC output only after the formal-ready handoff passes.

## Required reading

Read these output contracts on first use; reuse unchanged references. The current article and exact package snapshot must be read for this export:

1. references/article-completion-contract.md
2. references/abc-priority-rules.md
3. references/bbdc-export-format.md
4. references/output-template.md
5. the verified article and its package snapshot

## Workflow

1. Resolve the canonical source_id.
2. For a new web-review article, run `/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/english_vocab_handoff.py --source-id ID` only after local GPT-6 has formally applied the reviewed advice. Require its formal-ready handoff and open a new task with the user's selected `DeepSeek V4.1 Flash` provider identity. Do not override, merge or fall back between the official and OpenCode providers.
3. Read the handoff's verified english_article_completion_receipt_v2 with exact package_ids/package_sha256s and its freshly read formal_version. An older explicit output-only export may still be produced through references/article-completion-contract.md, but it must not be called formal-ready.
4. Reopen the article and the exact package snapshot, including packages reached by
   existing exact local archive pointers. Preserve their complete conversations and
   attachments. Missing archives remain explicit gaps; do not scan T9 or replace raw
   evidence with a support summary.
5. If relevant, use `query-learning-context` for exact bank/SP IDs or an unambiguous
   English alias. It rereads current formal values and corrections, including a later
   reactivation that supersedes older mastery. Support is a bounded locator/context
   aid, not an article-completeness authority.
6. Exclude currently mastered and review-excluded items using the effective ledger,
   then compare master_bank and deduplicate. Keep historical mastery proof when a
   later real failure reactivates an item. A same-sense bank match remains in this
   article's output with its existing ID; bank presence alone is not an exclusion.
7. Produce the A/B/C list from source-backed words, phrases and structures. Compute
   final A/B/C at article completion rather than freezing it in sentence-support.
8. Run any grounded-example or SP selector only for final selected A/B items, not in
   every sentence turn and not for C/background items.
9. Produce the requested complete BBDC output in the current task; use the four-layer
   grounding and old-word/SP rules in references/bbdc-export-format.md. Report
   formal_write_count=0 and background_processing=none.

Article completion does not append a background event, start a worker, create a candidate or wait for another model. It is a deterministic package snapshot only.

## Formal boundary

Do not write formal bank, learning events, review files or article data. Explicit cutoff curation accepts `开始英语正式入库` or `开始 YYYY-MM-DD 英语正式入库`; a separately authorized real correction/review follows the daily-curation learning-backflow contract. Article completion is not a real review result or mastery evidence.

## Output

Use references/output-template.md. Preserve source sentence, package evidence, mastered/duplicate status and grounding gaps. Do not claim article completion without a real package-backed receipt.

## Stop rules

- Active source_id missing: ask only for the smallest identifier.
- New web packages lack trusted formal, support, display or archive closure: stop at `VOCAB_HANDOFF_PENDING`; do not create or claim a formal-ready handoff.
- No verified conversation packages: report completion unsaved.
- Source sentence missing: mark needs_context.
- Validation fails: do not fabricate a list or receipt.
