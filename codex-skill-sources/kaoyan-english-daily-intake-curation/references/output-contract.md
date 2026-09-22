# Daily Curation Output Contract

Use plain-text labels.

```text
本次结果

cutoff_date：
selection：through_cutoff / only_today / package_subset
plan_id：
plan_canonical_sha256：
状态：COMPLETE / PARTIAL / FORMAL_COMMITTED_SUPPORT_PENDING / FORMAL_COMMITTED_ARCHIVE_PENDING / NOOP / FAILED
daily batches：按原 study_date 升序
package_count：
package_sha256s：

正式处理

safe actions：
needs_user：
writer status：
writer receipt：
formal prehash compare-and-swap：
formal posthashes：

Sentence support

proposal：PASS / PENDING / FAILED / NOOP_NO_PACKAGES
preflight receipt：
refresh receipt：
support audit：PASS / PENDING / FAILED
history_complete_through：
latest_refresh_study_date：
coverage_status：complete / partial / legacy_support_not_required
updated / unchanged / excluded：

归档

archive root：configured / missing
archive verify：PASS / PENDING / FAILED
Obsidian path receipt：PASS / PENDING / FAILED
display asset closure：PASS / PENDING / FAILED
pending_component：support_refresh / display_assets / archive / locator / cleanup / none
local packages：retained / cleaned

Global gate

completed：
already_consumed：
needs_user：
failed：
archive_pending：

Legacy reachability

eligible-for-explicit-migration：
needs_user：
migration_performed：false

Administrative retirement

user_removed：
actionable_event_count：
retirement receipt：
source events/receipts preserved：PASS / FAILED

待你判断

只列会改变未决动作的最小问题。
```

Formal writer success with incomplete sentence-support refresh is
FORMAL_COMMITTED_SUPPORT_PENDING, not COMPLETE. Resume support only and never rerun
apply. Formal writer and support success with incomplete archive or locator receipt is FORMAL_COMMITTED_ARCHIVE_PENDING. Never claim local cleanup without support, archive, locator and cleanup receipts.

When package_count is zero, report NOOP for the plan, execution and global gate. Still report the full legacy reachability and Administrative retirement sections, including user_removed; do not call an empty package set COMPLETE.

completed and already_consumed require verified writer closeout, sentence-support closure, display asset closure, archive receipt, exact locator/retrieval_keys reread, cleanup intent, archive-pointer and cleanup receipt. If any member is absent or drifts, report archive_pending with its pending_component and preserve the no-reapply boundary.

Default dry-run reports would_freeze and would_resume and must leave the state tree byte-identical. --no-archive cannot place a package in completed.

`user_removed` is a separate administrative legacy terminal. Report it separately;
never include it under completed, already_consumed, package_count or migration.
