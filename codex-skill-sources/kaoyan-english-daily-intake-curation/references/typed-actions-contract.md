# Package-Bound Sol Typed Actions

Emit english_sol_actions_v2 bound to batch_id and batch_manifest_sha256. actions contain safe writer operations; unresolved contains status needs_user.

The same Sol also emits a separate `english_sentence_support_proposal_v1` bound to
the same batch and manifest. It reuses this evidence-ref contract but is not an
allowed formal operation, cannot allocate a formal ID, and is consumed only after the
canonical writer closeout by the deterministic support refresher. A package with no
formal row change still receives a support proposal so its translation episode is not
lost.

Every action and unresolved item carries one or more evidence_refs:

```json
{
  "package_id": "EN-PKG-...",
  "package_sha256": "<sha256>",
  "pointer": "conversation.json#/messages/0",
  "kind": "user_message"
}
```

Pointers must resolve to a real JSON node. A conversation message pointer binds the complete indexed message and kind must match its user or assistant role. A source pointer must resolve to a scalar source fact with kind=source_text. An attachment pointer uses manifest.json#/attachments/N and kind must exactly equal that attachment role. Missing targets, array overflow, scalar traversal and role/kind mismatch fail closed.

The writer records the resolved node SHA-256, node type and exact role in its action result. It uses only those resolved nodes for evidence checks; the rest of the package is not treated as implicit support.

Allowed formal operations remain master_bank_insert, master_bank_update, mastered_insert, sentence_pattern_append, sentence_pattern_merge, learning_event_append, skip_duplicate and the existing review status operations. Sol never supplies a filesystem path, shell command or formal ID allocation.

mastered_insert requires explicit independent_correct_use evidence. Guided understanding, explanations, generated examples and self-report are insufficient. Ambiguous sense, duplicate, source or mastery decisions go to needs_user without blocking independent safe actions.

For real answer/correction/review results and local A-advice decisions, use learning_event_append under references/learning-backflow-contract.md; vocabulary-only edits do not replace the event history.
