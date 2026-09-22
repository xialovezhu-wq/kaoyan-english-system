# Independent Learning Unit Capture

Only save on the user's explicit request or an already authorized unit close. Normal teaching stays read-only. The target is 10–20 seconds from the user's save request to the complete reply; script timing alone is not end-to-end evidence.

## Normal path

Reuse the current source/unit identity and the known starting message line. Run once:

```sh
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/capture_current_english.py \
  --start-line UNIT_START_LINE \
  --source-id SOURCE_ID --unit-id S05 \
  --source-kind article --answer-exposure answer_free
```

The script reads the real session ID from CODEX_THREAD_ID, locates only that session in the app state database, and checks session_meta. Never substitute a context-window ID or another task's ID. Known exact --rollout and --session-id can be supplied for an explicit original-thread capture.

The command reads the original conversation, copies available attachment bytes, binds the exact prepared source and necessary paragraph/question context, derives the original study day from message timestamps, seals one independent package and returns its ZIP. It does not call a model or update formal learning data, scheduling or personal summaries. No previous_token, frontier recovery, prior-package scan or T9 read precedes a new unit.

If the starting line is unknown, run this once:

```sh
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/capture_current_english.py prepare
```

Select the exact unit start from the returned current-session messages and reuse end_line. Do not search all sessions, run help, inspect source code or hand-copy the conversation into JSON. The recent window contains at most 80 messages; if it does not cover the known start, supply that known start rather than guessing.

## Boundaries and evidence

--start-line and --end-line are inclusive physical message lines. The default end is the latest real user message, excluding save-progress commentary. If a later unrelated sentence has already arrived, pass the unit's known end explicitly. For an interrupted unit only, repeat --turn-id in original order to select every related turn while excluding unrelated maintenance. Preserve all first attempts, corrections, explanations and user reformulations; never select only the final exchange.

--unit-id accepts a prepared sentence, paragraph, full question or option. Known --source-hash is checked, never omitted to bypass a mismatch. External text uses --source-kind user_provided with --source-text or --source-text-file; absent source metadata stays in missing_fields. The same exact source text must remain unchanged.

Pass --image-role LINE:BLOCK:ROLE for known image roles; blocks are zero-based. Available local paths, app image wrappers, embedded image bytes and explicit attached-file headers are collected automatically. Unknown roles stay other_attachment; unavailable originals are explicitly listed in missing_fields. Never omit an available original for speed. Additional known files outside those blocks can be supplied with --attachment ROLE:/absolute/path.

## Answer protection

Always explicitly select source-kind and answer-exposure. article is canonical article text; user_provided is an ordinary external sentence; question/option preserve exam identity. Answers, publisher analysis and answer-revealing dialogue use explanation with protected exposure. Pure language work on a stem or option can be answer_free but remains a protected question/option sidecar. Translation does not authorize opening answers. explanation_image requires protected exposure. Do not restate protected answers in the save reply.

## Receipt and retry

New packages retain intake/packages/YYYY-MM-DD/EN-PKG-... and the existing manifest, conversation, source and attachment format. Receipt v3 marks independent_unit and ready. Each real session/start pair has one immutable binding under intake/unit-captures; same evidence replays idempotently and different evidence conflicts. A new learning attempt uses its real new starting message, not a renamed retry key.

After a package is already saved, capture only new related messages and use --supplement-of EN-PKG-... to link the original. Legacy packages remain unchanged. Do not reconstruct an old chain or duplicate an old episode just to save the continuation.

A successful created/idempotent_noop result with ready and an existing zip_path finishes the task: give one concise success sentence and the returned ZIP link. The writer already validates bytes, attachment hashes and the receipt; do not reopen each file, scan receipts, query the Dashboard or run segment-zip separately.

If ZIP creation fails after sealing, repeat the exact bounds to reuse the same package. On failure preserve the exact input/bounds and report the concrete missing item; ordinary teaching and other new units remain available. Legacy v1/v2 packages and recover-segment-gate remain readable for explicitly requested old-chain recovery only.

Package paths are temporary evidence references, never Obsidian display links. Formal intake later owns published assets, verified personalization updates, archive and cleanup.
