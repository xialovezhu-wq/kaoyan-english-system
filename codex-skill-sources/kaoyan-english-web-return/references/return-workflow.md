# 英语已有回包的审阅与逐日执行

## Prepare and read everything

Run:

```text
python3 scripts/english_web_workflow.py prepare-return --zip RETURN.zip
```

Require `english_web_return_v1`, `contract_version=english-web-review-v1`, the actual input `study.zip` SHA-256, exact `bundle_id`, package set and pinned `source_commit`. Reopen every original package member and attachment, the complete `advice.json`, optional exact `web_conversation.json`, file inventory and all returned raw artifacts. An unavailable web transcript must be an explicit gap; never synthesize one.

Read the returned `review.template.json` and bundled native contracts. For each package, independently check the first attempt, later self-correction, hints, explanation exposure, assistant errors, identity, duplicates, existing formal state, proposed native actions and complete support record. A bundled point snippet or a web draft does not prove an observation; compare its role, chronology and actual answer content with exact original messages. A bare acknowledgment and a reply after explanation are not independent success.

`prepare-return` also returns `evidence_prescreen`, an advisory sidecar from the existing Decisions service. Read its flagged and not-screened items alongside the original evidence; it may identify unsupported learning claims, hint dependence or evidence gaps. An unflagged item is not approved. API errors, unsupported evidence or budget limits leave the native preparation and manual review unchanged; preserve the reported gap instead of treating it as a pass. This pre-screen never fills `local_decision`, changes scores/mastery, unlocks answers or invokes a formal writer. Do not rerun successful formal work to obtain a pre-screen.

Record the actual local reviewer model when known. If its identity is unavailable, retain unknown. Fill every template item with:

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
python3 scripts/english_web_workflow.py approve-return --review-json REVIEW.json
```

Require the returned `review_id` and `daily_dates`. This preserves the immutable original packages, local decisions and entire raw web return as native supplementary captures. It performs no formal writes. Repeating the same return must resolve idempotently and must not create a second learning episode.

## Prepare and run each day

For every returned date in ascending order:

```text
python3 scripts/english_web_workflow.py prepare-day --review-id REVIEW_ID --date YYYY-MM-DD
python3 scripts/english_web_workflow.py run-day --review-id REVIEW_ID --date YYYY-MM-DD
```

`prepare-day` freezes the exact originals and supplements for that day and compiles the reviewed native actions/support. The first `run-day` is the required dry run and preflight. Resolve only real validation or current-state conflicts; do not rewrite already accepted independent items merely for style.

The v7 apply route binds English Embeddings and Decisions review for this exact native batch before starting the writer transaction. Existing web pre-screen output stays advisory. A pending model step resumes the same batch and reuses completed requests, while an already committed batch resumes only its outstanding post-formal stages.

When formal apply is authorized and the dry run passes, run exactly:

```text
python3 scripts/english_web_workflow.py run-day \
  --review-id REVIEW_ID --date YYYY-MM-DD --apply --authorization REVIEW_ID
```

This route must finish the existing typed writer, sentence-support refresh, display publication, T9 archive/locator/cleanup and native teaching readback. If formal apply has committed but a later step fails, retry only the unresolved post-apply component through the same review/date transaction. Never repeat apply.

## Completion

Report each date's accepted, modified, rejected, needs_user and already_formal packages, actual formal IDs, writer receipt, support/display/archive closure and the local quality receipt. Generating, preparing or approving a return is not formal completion. Formal completion requires the actual writer and all required post-commit gates; preserve any unresolved package and name its pending component.

## Returns from parallel parts

Each part has its own bundle_id/input ZIP hash and return ZIP. Web processing can be parallel, but local review/apply is sequential. Process one return through its native terminal before applying the next. `prepare-return` rereads the current formal version; if a template was prepared before another part committed, reread current bank/SP state and update reviewed_formal_version plus the affected semantic decisions before approve-return. Do not merely replace the version string. Reuse matching IDs for overlapping vocabulary, merge complementary evidence without duplicating the same learning episode, and preserve earlier parts' corrections/support. Do not concatenate independent action JSON documents or overwrite shared summaries with a stale whole version. Preserve every original date within each part. Native locks, exact reviewed actions and prehash checks remain required.

## 冷产物与本地热资料（2026-10-11）

新交接 ZIP、网页返回原件、审阅过程、文章完成快照和词表交接默认写入 `<configured English cold root>/` 下各自的产物 ID 目录。原生入口会校验 T9 挂载与 UUID、建立必要的本地目录定位链接，并在完成或保全部分结果后调用 `storage_map_root/cold_storage.py` 登记文件路径与 SHA-256；登记失败保留已经生成的原件并明确报告，不能当作学习完成。自定义 `output_dir` 同样必须通过 T9 校验，外盘不可用时不得回落本地或建立假的挂载目录。

手工产生的交接/词表/过程成品也按独立 ID 目录保存到上述英语 T9 根路径；写入前调用中央 `ensure_output_path(path, 'english')`，关闭文件后调用 `register_tree(path, subject='english', purpose='实际用途', aliases=())` 并读回登记结果。已有 `exports` 兼容链接可继续定位，但新产物仍须校验与登记；不要把大型中间文件改写到本地临时目录。

个人摘要、题卡/题图、当前学习上下文、原生查询缓存、运行依赖，以及 `web-review/holds`、locks、archive-pointers、completion 小回执与其余热 views 继续留本地。不得迁移整个 `intake`、`web-review` 或 `views`。旧完整产物保持原地址可读和幂等；确需继续写旧冷 ID 时，仅在精确目录完成复制、逐文件校验、T9 登记回执读回后改建兼容链接，保留原身份和全部内容。中断按同 ID 恢复，不新建学习身份、不重复正式 apply。
