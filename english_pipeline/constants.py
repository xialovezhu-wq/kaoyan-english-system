from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = REPO_ROOT / "intake"
SCHEMA_DIR = REPO_ROOT / "schema" / "english_pipeline"

MASTER_HEADER = [
    "id",
    "date",
    "type",
    "item",
    "source_article",
    "source_sentence",
    "meaning",
    "usage",
    "writing_value",
    "tags",
    "review_note",
    "appear_count",
    "last_seen",
]
MASTERED_HEADER = [
    "item",
    "matched_id",
    "matched_type",
    "mastered_date",
    "evidence_sentence",
    "evidence_context",
    "proof_note",
]
SP_FIELDS = [
    "title",
    "骨架",
    "难度等级",
    "基本句型",
    "从句类型",
    "场景标签",
    "可复用程度",
    "中文解释",
    "结构拆解",
    "生成模板",
    "相关词汇/搭配",
    "常用变体",
    "来源与示例",
    "use_count",
    "last_used",
]

MASTER_TYPES = {"单词", "词组", "熟词僻义", "句型", "长难句", "写作表达"}
WRITING_VALUES = {"适合", "一般", "不建议"}
USER_EVIDENCE = {
    "unknown",
    "mistranslated",
    "missed",
    "familiar_new_meaning",
    "structure_trap",
    "question_logic",
    "direct_explanation",
    "other",
}
EVIDENCE_STATES = {
    "unknown_observed",
    "guided_understood",
    "independent_correct_use",
}
STRONG_A_EVIDENCE = {"unknown", "mistranslated", "missed", "familiar_new_meaning"}

FORMAL_FILES = {
    "master_bank": Path("bank/master_bank.csv"),
    "mastered_items": Path("bank/mastered_items.csv"),
    "sentence_patterns": Path("bank/sentence_patterns.md"),
}
