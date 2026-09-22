# English Source Identity Handoff

The canonical source_id is reused for the same passage. source_hash is the SHA-256 of the verified practice-safe source payload. Preserve stable paragraph_id, sentence_id, knowledge_point_id and question_id values; do not derive them from a chat turn.

A source may omit some unit identities. Record absent fields in the conversation package missing_fields. Never invent year, Text number, answer identity or source metadata.

When source_article is available, use `resolve-source --source-article articles/FILE.md` through the existing English CLI. It verifies the old pipeline_handoff when present, otherwise binds the existing practice-safe corpus or the original source-only article. Preserve its returned source_id, source_hash and source_binding; do not insert a second handoff into existing articles. Protected answers and later tutoring dialogue never enter the practice-safe payload hash.
