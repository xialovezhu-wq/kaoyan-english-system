# Real learning and A-advice backflow

This branch applies to an explicitly authorized daily correction/answer/review or a
downloaded A/B/C/D result that the local subject reviewer is processing. A generated
plan or explanation is not evidence that the learner attempted or mastered an item.

Preserve the complete original package, user response and external result first.
`capture --input-json` uses the existing segment/token contract. For a web result,
`source.web_result` binds exactly `origin` (`project-A` through `project-D`), `task_id`
and the actually read Git `source_commit`. The external response and supplied files
remain original evidence, not authority to edit formal data. Use explicit
`source_kind` and `answer_exposure`; answers/analysis remain protected.

For A advice received after the original study day, preserve both dates. The new
capture's `occurred_at` and `captured_at` are the actual processing time; optional
top-level `study_date` retains the original study day only when `source.supplement_of`
names the exact original local package ID and SHA-256 from that day and the same
source. Use a separate processing thread, the project-A binding and original
downloaded result bytes. Never change the original package or move a thread frontier
backwards to manufacture the earlier date.

The same local reviewer accepts, modifies or rejects A advice. Its typed actions
use the current frozen manifest, normal evidence refs and the existing writer.
No model is called by the importer, capture, writer, refresher or publisher.

Add a `learning_event_append` action when preserving a real result or A decision:

```json
{
  "action_id": "local-decision-identity",
  "action_type": "learning_event_append",
  "reason": "Local evidence-backed decision, not a web model instruction",
  "evidence_refs": [],
  "event": {
    "event_key": "stable-original-task-result-or-daily-event-key",
    "kind": "review",
    "origin": "project-B",
    "task_id": "original-task-id",
    "source_commit": "actually-read-40-or-64-hex-commit",
    "source_id": "existing-English-source-id",
    "unit_id": "existing-sentence-question-or-knowledge-id",
    "bank_ids": [],
    "sentence_pattern_ids": [],
    "concept_ids": [],
    "observed_at": "original-observation-ISO-timestamp",
    "outcome": "incorrect",
    "hint_dependence": "not_observed",
    "reasoning_status": "not_observed",
    "user_response": "verbatim actual user response",
    "reasoning": "",
    "correction": ""
  }
}
```

The sample is a field map, never a valid real event or permission to fabricate one.
Fill nonempty `evidence_refs` with exact frozen package nodes. Payload schema is
`schema/english_pipeline/learning-event-v1.schema.json`. Valid kinds are answer,
correction, review and adjudication. Daily origin uses null task_id/source_commit.
A adjudication uses project-A and accepted/modified/rejected; it never updates
learner mastery. Other kinds use correct/incorrect/unresolved and require the
verbatim user response from a bound user message. Reasoning/correction text must
occur in the bound evidence. Independent success additionally requires an exact
user node tagged `independent_correct_use`, valid reasoning and no hint dependence.
For answer/review events, correct endpoints with invalid reasoning are retained as
recurrence, not mastery. Correction and adjudication events preserve the decision
history without changing review eligibility or learner mastery.

Before Bridge completion, run `validate-formal-receipt` on the canonical writer
receipt. This command is read-only and returns exact decisions, parallel
`package_ids` / `package_sha256s`, the original `publication_event_id`, and
`verified_attachments` bound to actual decision evidence nodes. Bridge compares the
downloaded result SHA to those verified attachment hashes; a claimed filename or
unreferenced attachment does not prove that result was processed.

Use existing bank IDs, SP IDs, and `bank:ID` / `sp:SP-ID` as stable concept identities;
names and optional English alias mappings locate these identities but do not replace
them. An ambiguous or deleted target stops that proposed action. Same event_key and
same result is an idempotent no-op; changed result under that key conflicts. A later
real correction is a new event with its own original date, never a history rewrite.

`master_bank_update` also accepts evidence-bound corrected meaning/source_sentence;
its other editable fields remain usage/tags/review_note/appear_count/last_seen.
Preserve the previous record through transaction preimages and the correction event;
do not directly edit CSV to bypass the writer.

Freeze and apply through the normal v3 actions/support preflight contract. Formal
events are atomically committed to `bank/learning_events.jsonl` in the same writer
transaction. Current formal values are reread by `query-learning-context` every turn;
the formal closeout invalidates related derived state immediately. It then requests
one deterministic asynchronous publication after the formal lock is released.
After support, display, archive and requested cleanup finish, the archive closeout
requests a separate stable publication version from the same writer event and the
existing content/dependency hashes. The version identity contains no new learning
event, timestamp or PID; an already-published early snapshot cannot swallow the
final archive snapshot, and an unchanged closeout retry is idempotent.
Cloud failure cannot roll back local facts or authorize another apply. Resume only
the named support/display/archive/publication component using the original closeout.

For a failed final publication, explicitly use `archive-nightly --manifest PATH
--writer-receipt PATH --retry-publication` or `recover-nightly --transaction PATH
--retry-publication`. Recovery reuses the existing final closeout event ID and
archive content basis, reports its publication status, and never repeats formal
apply, learning-event append, support refresh or completed cleanup. A pending
publication makes recover-nightly PARTIAL; a successful retry remains idempotent.
Publication still runs after releasing the formal lock.

## English project E

The English-only `project-E` route is owned by kaoyan-english-web-return. It preserves exact downloaded advice and local decisions as explicitly labelled model artifacts, not fabricated learner messages. Original study_date stays with the original package; the generated E adjudication observed_at must equal the exact supplementary package actual capture timestamp. No mastery or review eligibility is inferred from accepted/modified/rejected. Only this route seals web_review_id and reviewed action hashes for native writer admission.
