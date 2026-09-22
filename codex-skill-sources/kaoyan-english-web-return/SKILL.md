---
name: kaoyan-english-web-return
description: "Review a GPT-6 Pro English web-return ZIP locally and, when formal intake is authorized, finish the existing native English writer and post-commit chain. Use for 根据英语网页建议正式入库 or local GPT-6 adjudication of an english_web_return_v1 package."
---

# Kaoyan English Web Return

The local GPT-6 reviewer owns every semantic decision. Web advice is evidence and a proposal, never a formal verdict. Use the existing English typed writer, sentence-support, display, T9 archive and local MCP publication chain; do not create a second writer or change math A/B/C or 408 D.

## Authorization and scope

Preparing and reviewing an uploaded return does not by itself authorize formal apply. A request such as `根据该包正式入库`, `把网页返回正式入库`, or earlier authorization in the same session is sufficient; do not ask again. Without that authorization, finish the read-only preparation and local review artifact, then stop before `--apply`.

The return's exact package set is the scope. Preserve every original `study_date` and process dates in ascending order. A package with a trusted completed decision or formal terminal is not redone.

## Prepare and read everything

Run:

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/english_web_workflow.py prepare-return --zip RETURN.zip
```

Require `english_web_return_v1`, `contract_version=english-web-review-v1`, the actual input `study.zip` SHA-256, exact `bundle_id`, package set and pinned `source_commit`. Reopen every original package member and attachment, the complete `advice.json`, optional exact `web_conversation.json`, file inventory and all returned raw artifacts. An unavailable web transcript must be an explicit gap; never synthesize one.

Read the returned `review.template.json` and bundled native contracts. For each package, independently check the first attempt, later self-correction, hints, explanation exposure, assistant errors, identity, duplicates, existing formal state, proposed native actions and complete support record. Do not infer mastery from a web suggestion, a correct final answer after help, or the mere presence of an item in the bank.

Fill every template item with:

- `local_decision`: exactly `accepted`, `modified`, `rejected`, `needs_user` or `already_formal`;
- a concrete `reason` grounded in original or returned evidence;
- final native typed actions with all fields required by `contracts/typed-actions.md` and `contracts/formal-fields.json`;
- a complete native `support_record` that validates against the included sentence-support record schema, without extra fields;
- unresolved evidence or semantic conflicts in `uncertainties`.

Do not copy the web decision into `local_decision`. For `modified`, supply the complete corrected values. For `rejected`, preserve why no action is accepted. Accepted, modified and rejected items all require a valid support record; `needs_user` and a natively verified `already_formal` terminal may remain outside the new support/action set. Use `needs_user` only when the missing fact changes the decision; ask one minimal question for that item and continue independent items.

Set `reviewed_formal_version` to the actual current local formal version read during this review. If it differs from the bundle's pinned version, re-evaluate affected identity, deduplication and final values against current formal state; never relabel stale advice as current.

## Seal the local review

After the review JSON is complete, run:

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/english_web_workflow.py approve-return --review-json REVIEW.json
```

Require the returned `review_id` and `daily_dates`. This preserves the immutable original packages, local decisions and entire raw web return as native supplementary captures. It performs no formal writes. Repeating the same return must resolve idempotently and must not create a second learning episode.

## Prepare and run each day

For every returned date in ascending order:

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/english_web_workflow.py prepare-day --review-id REVIEW_ID --date YYYY-MM-DD
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/english_web_workflow.py run-day --review-id REVIEW_ID --date YYYY-MM-DD
```

`prepare-day` freezes the exact originals and supplements for that day and compiles the reviewed native actions/support. The first `run-day` is the required dry run and preflight. Resolve only real validation or current-state conflicts; do not rewrite already accepted independent items merely for style.

When formal apply is authorized and the dry run passes, run exactly:

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/english_web_workflow.py run-day \
  --review-id REVIEW_ID --date YYYY-MM-DD --apply --authorization REVIEW_ID
```

This route must finish the existing typed writer, sentence-support refresh, display publication, T9 archive/locator/cleanup and current local MCP publication. If formal apply has committed but a later step fails, retry only the unresolved post-apply component through the same review/date transaction. Never repeat apply.

## Completion

Report each date's accepted, modified, rejected, needs_user and already_formal packages, actual formal IDs, writer receipt, support/display/archive closure and MCP current readback. Generating, preparing or approving a return is not formal completion. Formal completion requires the actual writer and all required post-commit gates; preserve any unresolved package and name its pending component.

## Returns from parallel parts

Each part has its own bundle_id/input ZIP hash and return ZIP. Web processing can be parallel, but local review/apply is sequential. Process one return through its native terminal before applying the next. `prepare-return` rereads the current formal version; if a template was prepared before another part committed, reread current bank/SP state and update reviewed_formal_version plus the affected semantic decisions before approve-return. Do not merely replace the version string. Reuse matching IDs for overlapping vocabulary, merge complementary evidence without duplicating the same learning episode, and preserve earlier parts' corrections/support. Do not concatenate independent action JSON documents or overwrite shared summaries with a stale whole version. Preserve every original date within each part. Native locks, exact reviewed actions and prehash checks remain required.

## Formal-only point personalization

After the actual formal writer commits, update only the affected word/phrase, syntax, option_type and error_type summaries from the fully read, accepted learning evidence. Preserve first attempts, explicit corrections, observed later user revisions and hint dependence; no unobserved mastery. Follow `/Users/your-user/.codex/skills/kaoyan-english-intensive-reading/references/point-personal-summary.md` and use `publish-personal-summaries --input-json POINTS.json --writer-receipt WRITER_RECEIPT.json`. Merge point episodes across parts instead of overwriting earlier contributions. Ordinary teaching and quick Capture never update these summaries. If this post-commit step fails, report it pending and resume only summary publication, never replay formal apply.
