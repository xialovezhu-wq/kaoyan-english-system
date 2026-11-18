# Per-Date Package Freeze and Writer Contract

Read backlog-gate-contract.md first. A through-cutoff plan may contain several dates, but each freeze remains one original study_date. Pass exact package IDs when a plan subset applies; never freeze needs_user, damaged, already-consumed or future packages.

In v7, a compiled native action set exposes `formal_model_review` for its validated source units. Embeddings ranks bounded English formal and same-batch candidates. Decisions compares those candidates against the evidence within reach of the review budget. Read the bound cold receipt and reconcile the proposed actions against their original evidence before authorizing apply. The frozen evidence and final typed actions remain the writer's authority.

The formal review receipt lists the exact snapshot count, protected label vectors reused, candidate pool, shortlist and comparisons delivered for each action. The explicit formal ID stays in the pool and shortlist. A missing target is `unresolved_target`. A `new` relation applies only to the stated comparison scope after its required records have been compared. Read the coverage before drawing conclusions about the rest of the snapshot.

Check `source_coverage` for each locator, source hash, speaker, message sequence, UTF-8 ranges delivered to Decisions and unread ranges. Follow those locators back to the complete original for any missing qualification, antecedent, prompt or correction. If the original still leaves a material gap, keep that relationship unresolved in the typed draft. The 2,200-byte evidence budget is for formal review. The teaching view keeps its existing budget and scope.

The writer uses the same receipt for the exact manifest and actions or prepares it before taking the formal write lock, including on the approved web-return route. Its compact binding is recorded in the transaction and apply receipt. A nonempty duplicate-only batch still enters review. Exact retries reuse successful stages. Provider failure or request-budget exhaustion leaves the batch unstarted and can resume from its saved receipt. Reconcile ambiguous network outcomes before repeating a request. Open transactions follow the existing recovery entry. An empty batch, validation-only run or post-commit recovery makes zero provider calls.

Freeze:

```text
python3 scripts/english_learning_pipeline.py freeze-nightly \
  --state-dir intake \
  --date YYYY-MM-DD \
  --package-id EN-PKG-... --local-review-id <本次工作清单ID>
```

Require english_freeze_receipt_v1 and english_nightly_manifest_v3. The v3 manifest
contains only locally staged, unprocessed immutable package records, current formal
prehashes, `postformal_contract_version=sentence-support-v1`, exact
`required_postformal_closures`, and its sentence-support history scope. Historical v2
manifests remain readable only for legacy recovery/archive and cannot authorize a new
apply. The local reviewer who prepares `english_sol_actions_v2` also prepares one
`english_sentence_support_proposal_v1` bound to this manifest and exact package
evidence; read sentence-support-contract.md.

Dry-run:

```text
python3 scripts/english_learning_pipeline.py apply-nightly \
  --state-dir intake \
  --manifest <manifest.json> --actions <actions.json> --dry-run
```

After dry-run and before apply, require a canonical
`english_sentence_support_preflight_receipt_v1` for the companion support proposal.
The authorized apply passes it as `--support-preflight <receipt.json>`; v3 apply
without the exact receipt fails closed.

Apply after a valid dry-run:

```text
python3 scripts/english_learning_pipeline.py apply-nightly \
  --state-dir intake \
  --manifest <manifest.json> --actions <actions.json> \
  --support-preflight <preflight-receipt.json> \
  --apply --authorization <batch_id>
```

The deterministic writer keeps lock, formal prehash compare-and-swap, atomic replacement, journal, receipt and recovery behavior. It binds package_ids, package_sha256s, per-package terminal dispositions and resolved evidence-node hashes in its receipt. A PARTIAL receipt processes only completed packages. needs_user, failed and incomplete packages remain local, but are not refrozen within the same backlog plan. A later explicit plan may reconsider them only after the missing decision or evidence changes. After the canonical writer closeout, refresh sentence-support from the companion proposal before any display or archive operation. Also complete the affected personal-summary and memory-index stages bound to the same writer receipt; they may use original staged or verified archived evidence under the existing source contract. A support or memory failure never changes or replays the writer result. Attach source-backed `display_review` annotations to the sentence proposal. Keep
different senses scoped separately and leave incomplete comparisons unresolved.
Use `source_coverage` and the original locators to verify which evidence reached
review. After sentence support, inspect the visual and concordance receipts.
Recover any missing derived view with `rebuild-sentence-teaching --writer-receipt
<canonical receipt>`. For a verified writer receipt with pending `memory_index`, use `recover-memory-closeout --writer-receipt` and verify the current audit and scope. Keep the live lifecycle and source vetoes when recovering. An old generation is only a historical evidence source.

If the process crashes after the apply receipt is durable but before receipt_closed, run recover-nightly for the canonical transaction. Recovery verifies transaction, receipt, manifest, staged/preimage hashes, formal posthashes, resolved evidence and the full journal chain, then appends exactly one receipt_closed. Repeating recovery must not append a second close record or replay apply.

Default run-backlog is validation-only and must not call freeze-nightly, recover-nightly, apply-nightly or archive-nightly. Only an authorized apply-mode run may invoke this contract.


## Memory adoption evidence

The same formal author can use the per-package `memory_use` reference returned by
`author-local-day`. Open the original scoped query receipt and the visible
explanation at the recorded locators. Review at most three relevant observations.
If there is no preserved result or the original explanation cannot be verified,
keep the disposition unknown. A subsequent query cannot supply historical proof.

Run `review-memory-use --manifest <exact v3 freeze> --package-id <id> --reviewer <current identity> --input-json <decisions>`
with `{"use_observations":[...]}`. Bind each item to the supplied `call_id`, `episode_id`
and `summary_sha256`. Give a concise `reason` and a `status` of `applied`,
`inapplicable`, `contradicted_by_current_answer` or `unknown`. For applied, include
the public `assistant_message_id` and original UTF-8 byte `span` as `[start,end)`.
Check the meaning, conditions and instructional action before declaring adoption.
Record later independent performance only through its current authorized learning
flow. These local-only diagnostics use the frozen evidence and preserve
`learning_gain=not_assessed`.

`inspect-memory-use --package-root <sealed package>` reads existing evidence. If
the exact Codex session and original visible lines are known, `--session-id`
and optional `--rollout` can replay that unit read-only. The audit does not run
the query again. Private diagnostics are excluded from the shared source set.

## 本机逐包作者与闭合读回

用本轮 `local_review_id` 和选定学习日的精确包清单冻结。已存在的 hold 和事务按原身份继续。对每个包核对 manifest、conversation、source、附件、receipt 的实际字节、日期、学科及 canonical SHA。missing_fields 表示证据缺口。历史候选仅作来源，关键条件不足的单元留作 typed unresolved。

运行 `author-local-day --manifest <当天 manifest> --reviewer <当前会话实际身份>`。在返回的 decisions_dir 按包写 `<package_id>.json`，字段为 package_id、package_sha256、batch_manifest_sha256、actions、unresolved 和 support_record。依 [动作合同](typed-actions-contract.md) 绑定出处。单一事实跨包只提交一次动作并列出证据。已完成项要有经 [句子支撑合同](sentence-support-contract.md) 验证的 support_record。相同入口恢复同一份成稿。`sol_nightly_reviewer` 和 `english_sol_actions_v2` 是兼容标识，沿用当前模型。T9 冷目录不可用时按原批次待恢复。

收到 LOCAL_ACTIONS_READY_FOR_NATIVE_PREFLIGHT 后沿上面的 dry-run、支撑预检和唯一 apply 办理。与正式 writer 同收据完成 support、display、个人摘要、memory_index、本机教学读回和归档。原生验回逐点核对摘要身份、索引代际及题号权限，然后核对 sentence_visual、concordance 和 VERIFIED_CURRENT 质量回执。恢复 native_readback 只补原事务的欠项。历史 v2 依原合同验收。用户明确要求远程重试时才使用原命令的 --retry-publication。

正式后的增量点摘要用 `publish-personal-summaries --input-json POINTS.json --writer-receipt WRITER_RECEIPT.json`。跨批合并保留旧 episode。撤回精确绑定旧 claim、episode 与新证据，成功正式提交和先前回执一直保留。日页面和可选副本分别读回。归档定位、清理意图及五类终态由 [归档合同](archive-contract.md) 和 [清单合同](backlog-gate-contract.md) 管理。
