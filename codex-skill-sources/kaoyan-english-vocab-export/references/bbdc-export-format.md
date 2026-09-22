# BBDC Output Format

For every A or B item report item, article meaning, exact source sentence, Chinese meaning, usage, one review note, source package_id and evidence pointer.

For C items report item, article meaning, exact source sentence and why it is low priority.

When a sentence-support record is used, retain its support key and evidence pointer in
the internal provenance, but do not expose support implementation metadata in the
ordinary learning-facing list. Reopen the exact source/package evidence before
claiming a user mistranslation or historical recurrence.

Never invent a source sentence, user error or package identity. A generated example is separate from the source sentence and never updates mastery, appear_count or last_seen.

For every final A/B generated example, run the existing read-only
`scripts/select_bbdc_foundation.py` route and obey
`schema/reference_grounded_examples.md`: ground it in the user's current wording or
explicit error evidence, one approved/corrected writing pattern, at least one
approved/corrected writing phrase, and one `verified_*` syllabus occurrence. An
`SP-*` relation is optional and does not replace the approved writing pattern. Keep
the sentence natural and within the existing 12–28 word gate; do not mechanically
pack sources into it.

When a useful due old word can fit naturally, include the existing old-word review
cue and its formal ID. Never force an old word, treat it as new, or let it replace the
target word's article sense. Preserve applicable existing SP IDs and constraints.
