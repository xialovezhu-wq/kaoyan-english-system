# Exact Raw Archive Fallback

Use this only when the current article, formal English library and existing Obsidian information are insufficient, and wiki/raw_archives/PACKAGE_ID.md already exists.

Run the exact package entry:

```text
python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py reopen-archive \
  --repo-root /Users/your-user/Documents/kaoyan-english \
  --package-id EN-PKG-... \
  [--message-sequence N] \
  [--attachment-role question_image]
```

The command verifies the fixed T9 sentinel, volume UUID, locator type/subject/status, retrieval_keys, safe T9-relative path, English archive-root containment, archived manifest SHA and package SHA. Locate the note from an exact formal/source/question/package key, then read only the named package and selected message sequences or attachment roles. It never scans the disk or enumerates unrelated packages.

Request only the exact evidence needed. Explanation and solution images remain protected: the reopen receipt returns their verified locator and hashes with content_returned=false. Open protected content only through the existing answer-protection contract and only within the authorized question scope.

A missing, unsafe, drifted or hash-mismatched locator fails closed. Do not guess a neighboring package or data-disk path.
