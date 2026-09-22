---
name: kaoyan-english-web-bundle
description: "Partition all unfinished English captures across dates, articles and tasks into independently reviewable ZIPs and paired prompts for parallel GPT-6 Pro sessions. Use for 将所有未处理的 capture 打包, 英语全部快速资料打包, or the earlier 今天全部打包 phrasing; this never performs formal intake."
---

# Kaoyan English Web Bundle

Create several immutable English ZIP parts and one corresponding prompt per part for independent parallel GPT-6 Pro web sessions. This is logical project E and does not change the existing math A/B/C or 408 D routes.

This is the local GPT-6 packaging entry after DeepSeek study. The deterministic script calls no additional model; preserve the actual selected provider and never claim that Skill text switched the runtime model.

## Scope

The default scope is **all English captures without a verified completed native formal/support/archive/cleanup terminal**, across every original study date, article and task. “今天打包” means perform the packaging now, not limit the capture dates. A previously exported or uploaded package is still unfinished and must remain included. Preserve original study dates; do not replace them with the bundle creation date.

Exclude only verified completed packages. Formal-committed but support/archive pending packages remain visible in the bundle as already_formal so local recovery does not repeat formal writes. Missing or damaged evidence must be reported, never silently omitted or fabricated.

Use `--date YYYY-MM-DD` only when the user explicitly says to limit the captures to that date (such as “只打包今天产生的”). Add `--source-id ID` only for an explicit source restriction. Repeated `--package-id ID` values define an explicitly selected set, including an intentional re-export of completed evidence.

## Command

Run the deterministic entrypoint:

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/english_web_workflow.py bundle \
  [--date YYYY-MM-DD] \
  [--source-id ID] \
  [--package-id ID ...]
```

For the default all-unfinished request, omit all optional selectors so the command collects every pending date. Do not enumerate tasks, articles or package directories yourself to reconstruct the set.

## Partition rules and acceptance

The CLI now returns `english_web_parts_v1`, `collection_id`, `parts`, a collection manifest and exact coverage. Do not first build a giant ZIP, dump all conversations into the parent context or send the same whole set to multiple web sessions.

Group by canonical article/source first; retain chronological order within an article. Split larger articles only between complete captures. Default limits per part are 12 captures, 65536 raw UTF-8 text bytes, 8 attachments and 32 MiB raw total bytes. These are configurable planning limits, not a measured model-token guarantee; reserve context for instructions, relevant MCP history, images/PDFs and final output. If the user supplies different limits, pass the corresponding --max-packages, --max-text-bytes, --max-attachments, --max-bytes values.

Every original selected package must appear in exactly one part. Each part uses the same frozen snapshot, its own native bundle_id and exact ZIP hash, the original dates, full source/conversation/attachments, return template, contracts, and PART_PROMPT.txt. The separately returned prompt_path must match the embedded prompt bytes. Check the collection manifest and each output file.

If one capture exceeds a budget on its own, keep it intact in a dedicated ZIP and report oversized_single_capture. Do not silently trim evidence or claim this part meets the budget; explain that it needs a separate handling decision. Preserve the other valid parts. Missing/corrupt evidence stays an explicit failure, never silently excluded.

The command performs zero formal writes. It must not mark a package reviewed, accepted, mastered or consumed, and it must not start the web reviewer automatically. If it returns `NOOP`, report that no unfinished complete package exists in the requested scope, preserving any separately reported legacy evidence gaps.

## Output

Give a numbered list or table with one ZIP download link and one matching prompt-file link for every part. Include article/source, capture count and original date range when useful. Prompts must restrict each web session to its own part, use the shared pinned MCP snapshot, request an independently named return ZIP, and prohibit cross-part learning-count duplication or formal ID allocation. Provide the project link once. Never send only a collection manifest, one giant aggregate ZIP or one generic prompt for all parts.

Web sessions may run in parallel. Local returns must be applied sequentially through kaoyan-english-web-return. After one return changes the bank, reread current formal values before reviewing the next return. Match existing words/senses/SP IDs, merge evidence-supported additions, and preserve previous parts' updates; do not blindly concatenate action files or overwrite shared learning summaries. Pending archive/support stages resume their original transaction.
