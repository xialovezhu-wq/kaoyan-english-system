#!/usr/bin/env python3
"""Build the private writing-reference corpus from six user-provided PDFs.

The three core writing PDFs have usable text layers.  Their page text and
unreviewed English/topic/heading candidates are retained with page provenance.
The two historical-prompt attachments are image-heavy, so their prompt faces
are saved with English-only OCR and explicit Chinese-visual review warnings.
The answer sheet is registered only as a printable asset.

This script intentionally never reads or writes ``bank/master_bank.csv`` or
``bank/sentence_patterns.md``.  Extracted content is a candidate reference
layer, not an approved writing bank.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT / "raw" / "writing_reference"
WIKI_ROOT = ROOT / "wiki" / "writing"

WRITING_SOURCE_ROOT = Path("/Users/your-user/Desktop/考研英语作文模板（英一）")


@dataclass(frozen=True)
class Source:
    source_id: str
    slug: str
    title: str
    path: Path
    role: str
    extraction: str


@dataclass(frozen=True)
class ApprovedPatternSpec:
    approved_id: str
    source_candidate_ids: tuple[str, ...]
    source_page: int
    verbatim: str
    normalized: str
    template: str
    category: str
    review_note: str
    usage_constraints: tuple[str, ...]
    status: str = "approved"
    correction_note: str | None = None


@dataclass(frozen=True)
class ApprovedVocabularySpec:
    approved_id: str
    source_candidate_id: str
    normalized: str
    theme: str
    form: str
    usage_constraints: tuple[str, ...]
    known_error_id: str | None = None


SOURCES: tuple[Source, ...] = (
    Source(
        "WRITING-SRC-BIG-TEMPLATE",
        "big_essay_template",
        "1.26考研英语大作文模板（英一英二通用）",
        WRITING_SOURCE_ROOT / "1.26考研英语大作文模板（英一英二通用）.pdf",
        "core_text_reference",
        "pdftotext_page_by_page",
    ),
    Source(
        "WRITING-SRC-SMALL-TEMPLATE",
        "small_essay_template",
        "2.26考研英语小作文模板（英一英二通用）",
        WRITING_SOURCE_ROOT / "2.26考研英语小作文模板（英一英二通用）.pdf",
        "core_text_reference",
        "pdftotext_page_by_page",
    ),
    Source(
        "WRITING-SRC-TOPIC-VOCAB",
        "topic_vocabulary",
        "5.英语一历年主题词汇总及拓展",
        WRITING_SOURCE_ROOT / "5.英语一历年主题词汇总及拓展.pdf",
        "core_text_reference",
        "pdftotext_page_by_page",
    ),
    Source(
        "WRITING-SRC-HISTORICAL-BIG-PROMPTS",
        "historical_big_prompts",
        "附件1：历年大作文题目汇总",
        WRITING_SOURCE_ROOT / "附件1：历年大作文题目汇总.pdf",
        "visual_prompt_reference",
        "tesseract_eng_180dpi",
    ),
    Source(
        "WRITING-SRC-HISTORICAL-SMALL-PROMPTS",
        "historical_small_prompts",
        "附件2：历年小作文题目汇总",
        WRITING_SOURCE_ROOT / "附件2：历年小作文题目汇总.pdf",
        "visual_prompt_reference",
        "tesseract_eng_180dpi",
    ),
    Source(
        "WRITING-SRC-ANSWER-SHEET",
        "answer_sheet",
        "附件3：考研英语答题卡",
        WRITING_SOURCE_ROOT / "附件3：考研英语答题卡.pdf",
        "print_asset_only",
        "registered_only_no_text_extraction",
    ),
)

SOURCE_BY_ID = {source.source_id: source for source in SOURCES}
CORE_SOURCES = SOURCES[:3]
OCR_SOURCES = SOURCES[3:5]
PRINT_SOURCE = SOURCES[5]

# Page mappings were verified visually/OCR against the two attachments.  Each
# tuple is (paper, year); every record retains the PDF page and remains an
# unreviewed prompt candidate until a human checks the image.
HISTORICAL_BIG_PAGE_MAP: dict[int, tuple[tuple[str, int], ...]] = {
    1: (("英语一", 2001), ("英语一", 2002)),
    2: (("英语一", 2003), ("英语一", 2004)),
    3: (("英语一", 2005), ("英语一", 2006)),
    4: (("英语一", 2007), ("英语一", 2008)),
    5: (("英语一", 2009), ("英语一", 2010)),
    6: (("英语一", 2011), ("英语一", 2012)),
    7: (("英语一", 2013), ("英语一", 2014)),
    8: (("英语一", 2015), ("英语一", 2016)),
    9: (("英语一", 2017), ("英语一", 2018)),
    10: (("英语一", 2019), ("英语一", 2020)),
    11: (("英语一", 2021), ("英语一", 2022)),
    12: (("英语一", 2023), ("英语一", 2024)),
    13: (("英语一", 2025),),
    14: (("英语二", 2010), ("英语二", 2011)),
    15: (("英语二", 2012), ("英语二", 2013)),
    16: (("英语二", 2014), ("英语二", 2015)),
    17: (("英语二", 2016), ("英语二", 2017)),
    18: (("英语二", 2018), ("英语二", 2019)),
    19: (("英语二", 2020), ("英语二", 2021)),
    20: (("英语二", 2022), ("英语二", 2023)),
    21: (("英语二", 2024), ("英语二", 2025)),
}

HISTORICAL_SMALL_PAGE_MAP: dict[int, tuple[tuple[str, int], ...]] = {
    1: (("英语一", 2025), ("英语一", 2024)),
    2: (("英语一", 2023), ("英语一", 2022), ("英语一", 2021), ("英语一", 2020)),
    3: (("英语一", 2019), ("英语一", 2018), ("英语一", 2017), ("英语一", 2016)),
    4: (("英语一", 2015), ("英语一", 2014), ("英语一", 2013), ("英语一", 2012)),
    5: (("英语一", 2011), ("英语一", 2010), ("英语一", 2009)),
    6: (("英语一", 2008), ("英语一", 2007), ("英语一", 2006), ("英语一", 2005)),
    7: (("英语二", 2025), ("英语二", 2024), ("英语二", 2023)),
    8: (("英语二", 2022), ("英语二", 2021), ("英语二", 2020)),
    9: (("英语二", 2019), ("英语二", 2018), ("英语二", 2017)),
    10: (("英语二", 2016), ("英语二", 2015), ("英语二", 2014)),
    11: (("英语二", 2013), ("英语二", 2012), ("英语二", 2011)),
    12: (("英语二", 2010),),
}

EXPECTED_PAGES = {
    "WRITING-SRC-BIG-TEMPLATE": 72,
    "WRITING-SRC-SMALL-TEMPLATE": 32,
    "WRITING-SRC-TOPIC-VOCAB": 25,
    "WRITING-SRC-HISTORICAL-BIG-PROMPTS": 21,
    "WRITING-SRC-HISTORICAL-SMALL-PROMPTS": 12,
    "WRITING-SRC-ANSWER-SHEET": 4,
}

EXPECTED_SHA256 = {
    "WRITING-SRC-BIG-TEMPLATE": "e6f4c3673a8b0ca72d0feb14e686df8a5b0643cf0986ad37b35a2550ddd08ff8",
    "WRITING-SRC-SMALL-TEMPLATE": "ec5d86a57bb55d68cfd4fed31ce4233386321a9f8ae2840f04d7596e2e4c622a",
    "WRITING-SRC-TOPIC-VOCAB": "90980e2c0f481ba5840b1658c5be83227a401fe0d0428772630f7f2bbca61539",
    "WRITING-SRC-HISTORICAL-BIG-PROMPTS": "9b2eba82bb32fb2a37cb1a0a503bf2a2ffbc5bc31cb8567c19b622cea859769d",
    "WRITING-SRC-HISTORICAL-SMALL-PROMPTS": "afe1cfd161afeaf62d7872c3d27435dacdf44032c051ec0f53b769d755b2391e",
    "WRITING-SRC-ANSWER-SHEET": "09701ef97751569a1c776a6c41baf76eb8ed6ac5e1c01d0c7774a0a0f4cd9871",
}

KNOWN_ERRORS: tuple[dict[str, object], ...] = (
    {
        "error_id": "WRITING-ERR-001",
        "source_id": "WRITING-SRC-SMALL-TEMPLATE",
        "source_pages": [11],
        "issue_type": "bilingual_date_mismatch",
        "source_verbatim": {
            "english": "The seminar will be convened in the conference room at 8:00 on the morning of Sunday, December 20th.",
            "chinese": "研讨会将于 12 月 24 日周日早上 8 点在会议室召开。",
        },
        "candidate_status": "rejected",
        "reason": "英文日期为 December 20th，中文释义写成 12 月 24 日，双语日期不一致。",
        "corrected_candidate": {
            "english": "The seminar will be convened in the conference room at 8:00 on the morning of Sunday, December 20th.",
            "chinese": "研讨会将于 12 月 20 日周日早上 8 点在会议室召开。",
        },
        "corrected_candidate_status": "corrected",
    },
    {
        "error_id": "WRITING-ERR-002",
        "source_id": "WRITING-SRC-SMALL-TEMPLATE",
        "source_pages": [17, 18],
        "issue_type": "grammar_error",
        "source_verbatim": "If there had been no your timely help or response, I'm afraid the consequences would have been truly serious.",
        "candidate_status": "rejected",
        "reason": "no your 不能直接置于 there had been 后；该反事实条件句结构不成立。",
        "corrected_candidate": "If it had not been for your timely help or response, I am afraid the consequences would have been truly serious.",
        "corrected_candidate_status": "corrected",
    },
    {
        "error_id": "WRITING-ERR-003",
        "source_id": "WRITING-SRC-TOPIC-VOCAB",
        "source_pages": [25],
        "issue_type": "collocation_form_error",
        "source_verbatim": "riddle with challenges",
        "candidate_status": "rejected",
        "reason": "表示“困难重重”时应使用被动结构 be riddled with challenges；原短语缺少 be 且词形关系不完整。",
        "corrected_candidate": "be riddled with challenges",
        "corrected_candidate_status": "corrected",
        "alternative_candidate": "face numerous challenges",
    },
)

# This is deliberately small and human-reviewed.  The full candidate layer
# remains available unchanged; only these items may be used directly when
# generating future example sentences.
APPROVED_PATTERN_SPECS: tuple[ApprovedPatternSpec, ...] = (
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-001",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00006",),
        9,
        "As is shown in the picture above, 图画描述.",
        "As is shown in the picture above, [PICTURE_DESCRIPTION].",
        "As is shown in the picture above, [PICTURE_DESCRIPTION].",
        "图画描述",
        "人工回看大作文 PDF p9；保留基础引图结构，删除替换词堆叠。",
        ("只用于图画或漫画作文。", "逗号后必须接完整图画描述分句。", "图表题不要使用 picture。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-002",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00128", "WRITING-ENG-BIG_ESSAY_TEMPLATE-00129"),
        23,
        "The drawing on the left side provides us with a lively scene: several urban residents are exercising in a park.",
        "The drawing on the left side provides us with a lively scene: [PICTURE_DESCRIPTION].",
        "The drawing on the [POSITION] side provides us with a lively scene: [PICTURE_DESCRIPTION].",
        "图画描述",
        "人工回看大作文 PDF p23；冒号后的示例为完整、自然的场景句。",
        ("仅用于双图或可明确左右位置的图画。", "冒号后写完整场景，不接单个名词。", "POSITION 只用 left/right。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-003",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00136",),
        24,
        "Referring to the statistics provided in the chart, 图表描述.",
        "The statistics provided in the [CHART_TYPE] show that [DATA_DESCRIPTION].",
        "The statistics provided in the [CHART_TYPE] show that [DATA_DESCRIPTION].",
        "图表引入",
        "人工回看大作文 PDF p24；原分词结构存在悬垂风险，白名单只保留明确主语的更正模板。",
        ("只用于有数据的图表。", "show that 后必须是主谓完整的数据分句。", "CHART_TYPE 可用 chart/bar chart/line chart/table。"),
        status="corrected",
        correction_note="将悬垂风险结构 Referring to... 改为明确主语 The statistics... show that...；原文只作来源追溯。",
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-004",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00165",),
        26,
        "The bar chart shows the distribution/composition of 主题词 at a glance, revealing a striking disparity in their preferences.",
        "The bar chart shows the distribution of [TOPIC].",
        "The bar chart shows the distribution of [TOPIC].",
        "图表总体描述",
        "人工回看大作文 PDF p26；仅批准简洁主干，未批准斜杠替换和无依据的 disparity 判断。",
        ("只用于柱状图。", "TOPIC 使用名词短语。", "若图中不是分布数据，改用其他图表描述。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-005",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00168", "WRITING-ENG-BIG_ESSAY_TEMPLATE-00169", "WRITING-ENG-BIG_ESSAY_TEMPLATE-00170"),
        26,
        "The pie chart highlights the dominance of 选项 in 整体, which overshadows all other components.",
        "The pie chart highlights the dominance of [CATEGORY] in [WHOLE].",
        "The pie chart highlights the dominance of [CATEGORY] in [WHOLE].",
        "图表总体描述",
        "人工回看大作文 PDF p26；人工缩减掉 which 尾部，避免指代歧义，只保留清晰主句。",
        ("仅在一个类别确实明显最大时使用。", "CATEGORY 必须属于 WHOLE。", "若差距不显著，不使用 dominance。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-006",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00201", "WRITING-ENG-BIG_ESSAY_TEMPLATE-00202"),
        29,
        "The health literacy level of Chinese residents increased gradually from 8.8% in 2012 to 11.58% in 2016.",
        "[METRIC] increased gradually from [START_VALUE] in [START_YEAR] to [END_VALUE] in [END_YEAR].",
        "[METRIC] increased [ADVERB] from [START_VALUE] in [START_YEAR] to [END_VALUE] in [END_YEAR].",
        "动态图细节",
        "人工回看大作文 PDF p29；数值顺序与时态正确，可安全抽象为增长句。",
        ("START_YEAR 必须早于 END_YEAR。", "两个数值必须同单位、同口径。", "ADVERB 要与实际变化幅度相符。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-007",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00194", "WRITING-ENG-BIG_ESSAY_TEMPLATE-00195"),
        29,
        "The percentage of graduates who pursue further study was 34% in 2018, compared with 26.3% in 2013, showing an increase of 7.7%.",
        "[METRIC] was [END_VALUE] in [END_YEAR], compared with [START_VALUE] in [START_YEAR], showing an increase of [CHANGE].",
        "[METRIC] was [END_VALUE] in [END_YEAR], compared with [START_VALUE] in [START_YEAR], showing an increase of [CHANGE].",
        "动态图细节",
        "人工回看大作文 PDF p29；采用前后值方向正确的 2019 英二示例。",
        ("END_YEAR 必须晚于 START_YEAR。", "CHANGE 必须按原数据复算。", "百分比差通常写 percentage points，除非题面明确按百分比增幅。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-008",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00250",),
        34,
        "Recent years have witnessed the rapid development of...",
        "Recent years have witnessed the rapid development of [TOPIC].",
        "Recent years have witnessed the [ADJECTIVE] development of [TOPIC].",
        "趋势概括",
        "人工回看大作文 PDF p34；结构自然，限定为确有时间趋势的主题。",
        ("TOPIC 使用名词短语。", "只有数据或事实支持发展趋势时使用。", "ADJECTIVE 不得夸大图表变化。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-009",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00244",),
        34,
        "It is apparent from the statistics that…",
        "It is apparent from the statistics that [DATA_BASED_CLAIM].",
        "It is apparent from the statistics that [DATA_BASED_CLAIM].",
        "图表结论",
        "人工回看大作文 PDF p34；保留 that 从句骨架，不批准空泛结论。",
        ("that 后必须接完整分句。", "结论必须能由图中数据直接支持。", "没有统计数据时不要使用。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-010",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00264",),
        35,
        "This is not an uncommon scene/phenomenon/ situation in our life.",
        "This is not an uncommon phenomenon in our lives.",
        "This is not an uncommon phenomenon in [CONTEXT].",
        "现象概括",
        "人工回看大作文 PDF p35；只批准 phenomenon 单一版本，并将 our life 规范为 our lives。",
        ("只用于现实中确实常见的现象。", "避免与 common/recurrent 重复堆叠。", "CONTEXT 使用明确范围，如 modern society。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-011",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00345",),
        41,
        "Many factors contribute to the tendency reflected in this chart.",
        "Many factors contribute to the tendency reflected in this chart.",
        "Many factors contribute to [PHENOMENON_OR_TENDENCY].",
        "原因分析",
        "人工回看大作文 PDF p41；搭配 contribute to 自然且因果方向清楚。",
        ("to 后接名词短语，不接动词原形。", "后文应至少展开两个真实因素。", "不要把相关性直接写成因果。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-012",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00409",),
        49,
        "It has become increasingly evident that...",
        "It has become increasingly evident that [CLAIM].",
        "It has become increasingly evident that [CLAIM].",
        "观点引入",
        "人工回看大作文 PDF p49；完整保留形式主语与 that 从句。",
        ("that 后接完整分句。", "CLAIM 需要后文论据支持。", "不与 obviously/apparently 重复叠加。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-013",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00412",),
        49,
        "We must acknowledge the fact that...",
        "We must acknowledge the fact that [CLAIM].",
        "We must acknowledge the fact that [CLAIM].",
        "观点引入",
        "人工回看大作文 PDF p49；结构自然，适合承认不能回避的事实。",
        ("that 后必须是可证实或可论证的事实判断。", "不用于纯个人偏好。", "语气较强，避免连续多次使用 must。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-014",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00557", "WRITING-ENG-BIG_ESSAY_TEMPLATE-00558"),
        57,
        "Not only does participating in labor practice classes improve undergraduates' physical and mental health, but it also promotes social progress.",
        "Not only does [ACTIVITY] improve [BENEFIT_A], but it also promotes [BENEFIT_B].",
        "Not only does [SINGULAR_ACTIVITY] [VERB_PHRASE_A], but it also [VERB_PHRASE_B].",
        "并列递进",
        "人工回看大作文 PDF p57；选用主谓一致、平行结构完整的示例。",
        ("SINGULAR_ACTIVITY 用单数名词或动名词短语。", "前半句使用 does 后，谓语用原形。", "it 必须明确指代同一活动，两个谓语保持平行。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-015",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00633", "WRITING-ENG-BIG_ESSAY_TEMPLATE-00634"),
        63,
        "It is, therefore, imperative for us to adopt a discerning attitude, carefully weighing its merits against its demerits.",
        "It is, therefore, imperative for us to adopt a discerning attitude, carefully weighing its merits against its demerits.",
        "It is, therefore, imperative for [ACTOR] to adopt a discerning attitude toward [ISSUE], carefully weighing its merits against its demerits.",
        "利弊权衡",
        "人工回看大作文 PDF p63；句法自然，保留 merits/demerits 对称结构。",
        ("只用于确有利弊两面的议题。", "its 必须有清晰单数先行词。", "若加入 toward ISSUE，避免重复另一个 issue 指代。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-016",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00715",),
        69,
        "Let us believe that this is the first step toward a brighter future for all of us.",
        "Let us believe that this is the first step toward a brighter future for all of us.",
        "Let us believe that [ACTION] is the first step toward a brighter future for all of us.",
        "措施与展望",
        "人工回看大作文 PDF p69；采用完整、自然的未来展望句，避开同页含错误分支的万能句。",
        ("ACTION 使用名词或动名词短语。", "只在该行动确实是起点时使用 first step。", "不把单一措施夸大为最终解决方案。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-017",
        ("WRITING-ENG-BIG_ESSAY_TEMPLATE-00718", "WRITING-ENG-BIG_ESSAY_TEMPLATE-00719"),
        69,
        "There is no better time than now for us to take collective and decisive action to address this pressing issue.",
        "There is no better time than now for us to take collective and decisive action to address this pressing issue.",
        "There is no better time than now for [ACTOR] to take [ADJECTIVE] action to address [PRESSING_ISSUE].",
        "措施与展望",
        "人工回看大作文 PDF p69；结构完整，可用于结尾行动号召。",
        ("只用于确实紧迫的议题。", "action 通常作不可数名词。", "避免与 immediately/urgent 等近义词过度堆叠。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-018",
        ("WRITING-ENG-SMALL_ESSAY_TEMPLATE-00012",),
        6,
        "I hope this letter/email finds you well and…",
        "I hope this email finds you well.",
        "I hope this [LETTER_OR_EMAIL] finds you well.",
        "小作文开头",
        "人工回看小作文 PDF p6；去除原模板悬空的 and，仅保留完整问候句。",
        ("只用于书信或邮件开头。", "LETTER_OR_EMAIL 二选一。", "正式投诉或紧急通知可直接进入主题，不强制使用。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-019",
        ("WRITING-ENG-SMALL_ESSAY_TEMPLATE-00015",),
        6,
        "It is my honor to inform you that…",
        "It is my honor to inform you that [POSITIVE_INFORMATION].",
        "It is my honor to inform you that [POSITIVE_INFORMATION].",
        "小作文开头",
        "人工回看小作文 PDF p6；结构自然，但限定为正式、积极通知。",
        ("that 后接完整分句。", "只用于积极或中性正式消息。", "道歉、投诉、拒绝等负面消息不要使用 honor。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-020",
        ("WRITING-ENG-SMALL_ESSAY_TEMPLATE-00046",),
        8,
        "The purpose of this letter is to...",
        "The purpose of this letter is to [PURPOSE_VERB].",
        "The purpose of this [LETTER_OR_EMAIL] is to [BASE_VERB_PHRASE].",
        "来信目的",
        "人工回看小作文 PDF p8；保留正式目的句，槽位要求明确。",
        ("to 后接动词原形。", "LETTER_OR_EMAIL 与实际载体一致。", "目的应直接对应题干任务。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-021",
        ("WRITING-ENG-SMALL_ESSAY_TEMPLATE-00047",),
        8,
        "I am writing with the intention of...",
        "I am writing with the intention of [PURPOSE].",
        "I am writing with the intention of [GERUND_OR_NOUN_PHRASE].",
        "来信目的",
        "人工回看小作文 PDF p8；明确 of 后只能接动名词或名词短语。",
        ("of 后不能直接接动词原形。", "优先使用具体功能，如 requesting/clarifying。", "与 The purpose of... 二选一，避免重复。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-022",
        ("WRITING-ENG-SMALL_ESSAY_TEMPLATE-00075",),
        11,
        "The English corner is scheduled to take place in the library on Friday from 7:00 to 8:00 p.m.",
        "[EVENT] is scheduled to take place in [PLACE] on [DATE] from [START_TIME] to [END_TIME].",
        "[EVENT] is scheduled to take place in [PLACE] on [DATE] from [START_TIME] to [END_TIME].",
        "活动信息",
        "人工回看小作文 PDF p11；采用日期、地点、时间顺序一致的第 3 句，避开同页日期翻译错误。",
        ("活动必须已经确定时间地点。", "介词顺序固定为 in PLACE, on DATE, from...to...。", "核对日期与星期是否一致。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-023",
        ("WRITING-ENG-SMALL_ESSAY_TEMPLATE-00093",),
        12,
        "This conference aims to facilitate an in-depth discussion on 某事.",
        "This conference aims to facilitate an in-depth discussion on [TOPIC].",
        "This [CONFERENCE_OR_SEMINAR] aims to facilitate an in-depth discussion on [TOPIC].",
        "活动目的",
        "人工回看小作文 PDF p12；动词搭配和介词 on 自然。",
        ("仅用于会议、研讨会等讨论型活动。", "TOPIC 使用名词短语。", "普通文娱活动不要硬套 in-depth discussion。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-024",
        ("WRITING-ENG-SMALL_ESSAY_TEMPLATE-00168",),
        16,
        "Without your generous help, I would not have made such great progress in...",
        "Without your generous help, I would not have made such great progress in [AREA].",
        "Without [HELP_OR_SUPPORT], [SUBJECT] would not have [PAST_PARTICIPLE_PHRASE].",
        "感谢信",
        "人工回看小作文 PDF p16；反事实条件和完成时结构正确。",
        ("主句必须使用 would not have + 过去分词。", "只用于过去已发生结果的感谢。", "PAST_PARTICIPLE_PHRASE 必须明确说明未获帮助时不会实现的结果。"),
    ),
    ApprovedPatternSpec(
        "WRITING-APPROVED-PATTERN-025",
        ("WRITING-ENG-SMALL_ESSAY_TEMPLATE-00264",),
        25,
        "For more information about the activity, please do not hesitate to contact me.",
        "For more information about [TOPIC], please do not hesitate to contact [CONTACT].",
        "For more information about [TOPIC], please do not hesitate to contact [CONTACT].",
        "小作文结尾",
        "人工回看小作文 PDF p25；礼貌、完整，适合作为信息类信件结尾。",
        ("只用于确实可以继续提供信息的场景。", "CONTACT 与署名身份一致。", "正式公告可把 me 改为具体部门或邮箱。"),
    ),
)

NOUN_PHRASE_CONSTRAINTS = (
    "作为名词短语使用；根据句意处理冠词、单复数和所有格。",
    "必须放入具体论证或语境，避免主题词堆砌。",
)
VERB_PHRASE_CONSTRAINTS = (
    "按主语和时态调整动词形式。",
    "宾语或介词补语必须语义匹配，不能只替换单词硬套。",
)
CLAUSE_PHRASE_CONSTRAINTS = (
    "作为完整分句或句内谓语使用，按上下文补足主语。",
    "不得与同义表达重复堆叠。",
)

APPROVED_VOCABULARY_SPECS: tuple[ApprovedVocabularySpec, ...] = (
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-001", "WRITING-TOPIC-0214", "traditional culture", "文化", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-002", "WRITING-TOPIC-0216", "cultural heritage", "文化", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-003", "WRITING-TOPIC-0218", "intangible cultural heritage", "文化", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-004", "WRITING-TOPIC-0221", "cultural diversity", "文化", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-005", "WRITING-TOPIC-0220", "cultural and historical identity", "文化", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-006", "WRITING-TOPIC-0083", "cultural exchange", "文化", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-009", "WRITING-TOPIC-0248", "cultural integration", "文化", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-010", "WRITING-TOPIC-0257", "mutual learning between civilizations", "文化", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-011", "WRITING-TOPIC-0279", "artificial intelligence", "科技创新", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-012", "WRITING-TOPIC-0276", "cloud computing", "科技创新", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-014", "WRITING-TOPIC-0285", "digital infrastructure", "科技创新", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-015", "WRITING-TOPIC-0288", "digital economy", "科技创新", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-016", "WRITING-TOPIC-0290", "virtual reality", "科技创新", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-017", "WRITING-TOPIC-0292", "core technologies in key fields", "科技创新", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-018", "WRITING-TOPIC-0294", "innovation-driven development", "科技创新", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-019", "WRITING-TOPIC-0297", "environmental protection", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-020", "WRITING-TOPIC-0301", "wildlife protection", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-021", "WRITING-TOPIC-0302", "biodiversity conservation", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-022", "WRITING-TOPIC-0303", "green and low-carbon development", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-023", "WRITING-TOPIC-0304", "green economy", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-024", "WRITING-TOPIC-0306", "sustainable development", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-025", "WRITING-TOPIC-0308", "clean energy", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-026", "WRITING-TOPIC-0311", "ecological restoration", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-027", "WRITING-TOPIC-0318", "global warming", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-028", "WRITING-TOPIC-0319", "climate change", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-029", "WRITING-TOPIC-0320", "environmental degradation", "环境", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-030", "WRITING-TOPIC-0232", "healthy lifestyle", "健康", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-031", "WRITING-TOPIC-0234", "physical exercise", "健康", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-032", "WRITING-TOPIC-0339", "physical and mental well-being", "健康", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-033", "WRITING-TOPIC-0205", "critical thinking", "教育", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-034", "WRITING-TOPIC-0397", "lifelong education", "教育", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-035", "WRITING-TOPIC-0203", "extracurricular activities", "教育", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-036", "WRITING-TOPIC-0199", "cultivate an interest", "教育", "verb_phrase", VERB_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-037", "WRITING-TOPIC-0363", "resilience", "个人成长", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-038", "WRITING-TOPIC-0366", "perseverance", "个人成长", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-039", "WRITING-TOPIC-0367", "self-discipline", "个人成长", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-040", "WRITING-TOPIC-0361", "integrity", "个人成长", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-041", "WRITING-TOPIC-0373", "sense of responsibility", "个人成长", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-042", "WRITING-TOPIC-0420", "team spirit", "社会协作", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-043", "WRITING-TOPIC-0421", "collaborate with others", "社会协作", "verb_phrase", VERB_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-044", "WRITING-TOPIC-0435", "make headway in", "通用搭配", "verb_phrase", VERB_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-045", "WRITING-TOPIC-0447", "much remains to be done", "通用搭配", "clause", CLAUSE_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-046", "WRITING-TOPIC-0448", "share experience and expertise", "通用搭配", "verb_phrase", VERB_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-047", "WRITING-TOPIC-0449", "insightful perspectives", "通用搭配", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-048", "WRITING-TOPIC-0451", "be riddled with challenges", "通用搭配", "verb_phrase", VERB_PHRASE_CONSTRAINTS, "WRITING-ERR-003"),
    ApprovedVocabularySpec("WRITING-APPROVED-VOCAB-049", "WRITING-TOPIC-0452", "sustained and rapid development", "通用搭配", "noun_phrase", NOUN_PHRASE_CONSTRAINTS),
)

HEADER_MARKERS = (
    "全套课程上传",
    "全套视频课程上传",
    "完整版资料及课程请关注",
    "B 站@AI 归来",
    "B 站：AI 归来",
)

FORMAL_DATA_PATHS = (
    "bank/master_bank.csv",
    "bank/sentence_patterns.md",
    "bank/mastered_items.csv",
    "prompts/codex_prompt.md",
    "raw/index.md",
    "wiki/index.md",
    "wiki/log.md",
    "schema/schema.md",
)

UNREVIEWED_CANDIDATE_PATHS = (
    "raw/writing_reference/extracted/english_candidates.jsonl",
    "raw/writing_reference/extracted/headings.jsonl",
    "raw/writing_reference/extracted/topic_candidates.jsonl",
    "raw/writing_reference/extracted/known_error_candidates.jsonl",
    "raw/writing_reference/ocr/prompt_candidates.jsonl",
)


def run(command: Sequence[str], *, stderr_to_stdout: bool = False) -> str:
    result = subprocess.run(
        list(command),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if stderr_to_stdout else subprocess.PIPE,
    )
    return result.stdout.decode("utf-8", errors="replace")


def require_tools() -> None:
    missing = [name for name in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract") if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"Missing required tools: {', '.join(missing)}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_info(path: Path) -> dict[str, object]:
    output = run(["pdfinfo", str(path)])
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    if "Pages" not in values:
        raise RuntimeError(f"Cannot determine page count: {path}")
    return {
        "pages": int(values["Pages"]),
        "page_size": values.get("Page size", "未记录"),
        "pdf_version": values.get("PDF version", "未记录"),
    }


def normalize_same_source(value: str) -> str:
    """Normalize Unicode/whitespace without editorial rewriting."""
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00ad", "-").replace("\ufeff", "")
    return re.sub(r"\s+", " ", value).strip()


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def write_jsonl(path: Path, records: Iterable[dict[str, object]]) -> None:
    lines = [json.dumps(record, ensure_ascii=False, sort_keys=False) for record in records]
    write_text(path, "\n".join(lines))


def protected_file_snapshot() -> dict[str, str]:
    return {
        relative: sha256(ROOT / relative)
        for relative in FORMAL_DATA_PATHS
        if (ROOT / relative).is_file()
    }


def relative_file_snapshot(paths: Iterable[str]) -> dict[str, str]:
    return {relative: sha256(ROOT / relative) for relative in paths if (ROOT / relative).is_file()}


def read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def normalize_for_source_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u200b", "").replace("\ufeff", "").replace("…", "...")
    return re.sub(r"\s+", " ", value).strip()


def build_approved_patterns(
    english_records: list[dict[str, object]],
    core_pages: dict[str, dict[int, str]],
    reviewed_date: date,
) -> list[dict[str, object]]:
    by_id = {str(record["candidate_id"]): record for record in english_records}
    approved: list[dict[str, object]] = []
    for spec in APPROVED_PATTERN_SPECS:
        missing = [candidate_id for candidate_id in spec.source_candidate_ids if candidate_id not in by_id]
        if missing:
            raise RuntimeError(f"Approved pattern {spec.approved_id} missing source candidates: {missing}")
        source_records = [by_id[candidate_id] for candidate_id in spec.source_candidate_ids]
        primary = source_records[0]
        source_id = str(primary["source_id"])
        if any(str(record["source_id"]) != source_id for record in source_records):
            raise RuntimeError(f"Approved pattern {spec.approved_id} crosses source PDFs")
        if any(int(record["source_page"]) != spec.source_page for record in source_records):
            raise RuntimeError(f"Approved pattern {spec.approved_id} has inconsistent source page")
        page_text = core_pages[source_id][spec.source_page]
        if normalize_for_source_match(spec.verbatim) not in normalize_for_source_match(page_text):
            raise RuntimeError(f"Approved pattern {spec.approved_id} verbatim not found on source page")
        if any(str(record["review_status"]) == "rejected" for record in source_records):
            raise RuntimeError(f"Approved pattern {spec.approved_id} references a rejected source candidate")
        approved.append(
            {
                "schema": "writing_approved_pattern_v1",
                "approved_id": spec.approved_id,
                "source_id": source_id,
                "source_page": spec.source_page,
                "source_candidate_id": spec.source_candidate_ids[0],
                "source_candidate_ids": list(spec.source_candidate_ids),
                "verbatim": spec.verbatim,
                "source_verbatim": spec.verbatim,
                "normalized": spec.normalized,
                "template": spec.template,
                "category": spec.category,
                "status": spec.status,
                "language_status": "reviewed",
                "reviewed_date": reviewed_date.isoformat(),
                "approval_basis": "manual_grammar_correction" if spec.status == "corrected" else "manual_source_page_review",
                "review_note": spec.review_note,
                "usage_constraints": list(spec.usage_constraints),
                "promotion_status": "reviewed_reference_only_not_formal_bank",
            }
        )
        if spec.correction_note:
            approved[-1]["correction_note"] = spec.correction_note
    return approved


def build_approved_vocabulary(
    topic_records: list[dict[str, object]],
    reviewed_date: date,
) -> list[dict[str, object]]:
    by_id = {str(record["topic_candidate_id"]): record for record in topic_records}
    errors = {str(error["error_id"]): error for error in KNOWN_ERRORS}
    approved: list[dict[str, object]] = []
    for spec in APPROVED_VOCABULARY_SPECS:
        if spec.source_candidate_id not in by_id:
            raise RuntimeError(
                f"Approved vocabulary {spec.approved_id} missing source candidate {spec.source_candidate_id}"
            )
        source = by_id[spec.source_candidate_id]
        source_status = str(source["review_status"])
        approval_basis = "manual_source_page_review"
        review_note = (
            f"人工回看主题词 PDF p{source['source_page']}；中英对应清楚、搭配自然，批准进入造句参考白名单。"
        )
        if source_status == "rejected":
            if not spec.known_error_id or spec.known_error_id not in errors:
                raise RuntimeError(f"Rejected vocabulary source lacks approved correction: {spec.approved_id}")
            corrected = errors[spec.known_error_id]["corrected_candidate"]
            if not isinstance(corrected, str) or normalize_for_source_match(corrected) != normalize_for_source_match(spec.normalized):
                raise RuntimeError(f"Vocabulary correction mismatch: {spec.approved_id}")
            approval_basis = "known_error_corrected_candidate"
            review_note = (
                f"人工回看主题词 PDF p{source['source_page']}；原候选已 rejected，"
                f"本条只批准 {spec.known_error_id} 的 corrected_candidate。"
            )
        elif spec.known_error_id:
            raise RuntimeError(f"Non-rejected vocabulary unexpectedly carries known_error_id: {spec.approved_id}")
        record: dict[str, object] = {
            "schema": "writing_approved_vocabulary_v1",
            "approved_id": spec.approved_id,
            "source_id": source["source_id"],
            "source_page": source["source_page"],
            "source_candidate_id": spec.source_candidate_id,
            "source_candidate_status": source_status,
            "verbatim": source["english_candidate"],
            "normalized": spec.normalized,
            "template": spec.normalized,
            "theme": spec.theme,
            "form": spec.form,
            "status": "approved",
            "language_status": "reviewed",
            "reviewed_date": reviewed_date.isoformat(),
            "approval_basis": approval_basis,
            "review_note": review_note,
            "usage_constraints": list(spec.usage_constraints),
            "promotion_status": "reviewed_reference_only_not_formal_bank",
        }
        if spec.known_error_id:
            record["known_error_id"] = spec.known_error_id
        approved.append(record)
    return approved


def extract_text_page(source: Source, page: int) -> str:
    return run(
        [
            "pdftotext",
            "-f",
            str(page),
            "-l",
            str(page),
            "-layout",
            "-enc",
            "UTF-8",
            str(source.path),
            "-",
        ]
    ).replace("\r\n", "\n").replace("\r", "\n").rstrip("\f\n")


def ocr_page(source: Source, page: int, *, refresh: bool) -> str:
    destination = RAW_ROOT / "ocr" / source.slug / "pages" / f"page-{page:03d}.txt"
    if destination.exists() and not refresh:
        return destination.read_text(encoding="utf-8").rstrip("\n")
    with tempfile.TemporaryDirectory(prefix="writing_reference_ocr_") as tmpdir:
        prefix = Path(tmpdir) / "page"
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                str(page),
                "-l",
                str(page),
                "-r",
                "180",
                "-png",
                "-singlefile",
                str(source.path),
                str(prefix),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        text = run(
            ["tesseract", str(prefix.with_suffix(".png")), "stdout", "-l", "eng", "--psm", "6"]
        ).replace("\r\n", "\n").replace("\r", "\n").rstrip()
    write_text(destination, text)
    return text


def is_header_or_footer(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if any(marker in stripped for marker in HEADER_MARKERS):
        return True
    if re.fullmatch(r"[-—]?\s*\d{1,3}\s*[-—]?", stripped):
        return True
    if stripped == "——AI 归来":
        return True
    return False


ENGLISH_SPAN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9’'\"“”.,;:!?%()\[\]{}+&/=_<>~\-–—… ]{4,}"
)


def english_line_candidates(source: Source, pages: dict[int, str]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    serial = 0
    seen: set[tuple[int, str]] = set()
    for page, text in pages.items():
        for line_no, line in enumerate(text.splitlines(), start=1):
            if is_header_or_footer(line):
                continue
            for match in ENGLISH_SPAN_RE.finditer(line):
                verbatim = match.group(0).strip()
                normalized = normalize_same_source(verbatim).strip(" -–—")
                letters = sum(character.isascii() and character.isalpha() for character in normalized)
                words = re.findall(r"[A-Za-z]+(?:[’'][A-Za-z]+)?", normalized)
                if letters < 8 or len(words) < 2:
                    continue
                if normalized.lower() in {"ai", "ai return"}:
                    continue
                key = (page, normalized)
                if key in seen:
                    continue
                seen.add(key)
                serial += 1
                review_status = "candidate"
                known_error_id = None
                lowered = normalized.lower()
                if "if there had been no your timely help" in lowered:
                    review_status = "rejected"
                    known_error_id = "WRITING-ERR-002"
                if "riddle with challenges" in lowered:
                    review_status = "rejected"
                    known_error_id = "WRITING-ERR-003"
                kind = "sentence" if len(words) >= 7 or re.search(r"[.!?]$", normalized) else "phrase"
                record: dict[str, object] = {
                    "candidate_id": f"WRITING-ENG-{source.slug.upper()}-{serial:05d}",
                    "source_id": source.source_id,
                    "source_page": page,
                    "source_line": line_no,
                    "candidate_kind": kind,
                    "verbatim": verbatim,
                    "normalized": normalized,
                    "normalization_scope": "same_source_unicode_and_whitespace_only",
                    "extraction_method": "pdftotext_layout_line_span",
                    "language_status": "unreviewed",
                    "review_status": review_status,
                    "promotion_status": "not_promoted",
                }
                if known_error_id:
                    record["known_error_id"] = known_error_id
                records.append(record)
    return records


NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?P<number>(?:\d+(?:\.\d+){0,3}\.?|[一二三四五六七八九十]+、))\s*(?P<title>[^。！？!?]{1,80})\s*$"
)


def heading_candidates(source: Source, pages: dict[int, str]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    serial = 0
    seen: set[tuple[int, str, str]] = set()
    for page, text in pages.items():
        for line_no, line in enumerate(text.splitlines(), start=1):
            if is_header_or_footer(line):
                continue
            match = NUMBERED_HEADING_RE.match(line)
            if not match:
                continue
            number = match.group("number").rstrip(".")
            title = normalize_same_source(match.group("title"))
            if len(re.findall(r"[\u3400-\u9fff]", title)) < 2:
                continue
            if "年" in title and re.search(r"20\d{2}", title):
                continue
            key = (page, number, title)
            if key in seen:
                continue
            seen.add(key)
            serial += 1
            level = number.count(".") + 1 if number[0].isdigit() else 1
            records.append(
                {
                    "heading_id": f"WRITING-HEAD-{source.slug.upper()}-{serial:04d}",
                    "source_id": source.source_id,
                    "source_page": page,
                    "source_line": line_no,
                    "numbering": number,
                    "heading_level_candidate": level,
                    "verbatim": line.strip(),
                    "normalized": f"{number} {title}",
                    "normalization_scope": "same_source_unicode_and_whitespace_only",
                    "language_status": "unreviewed",
                    "review_status": "candidate",
                }
            )
    return records


def topic_candidates(source: Source, pages: dict[int, str]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    serial = 0
    seen: set[tuple[int, str, str]] = set()
    for page, text in pages.items():
        for line_no, line in enumerate(text.splitlines(), start=1):
            if is_header_or_footer(line):
                continue
            first_ascii = re.search(r"[A-Za-z]", line)
            if not first_ascii:
                continue
            chinese_part = normalize_same_source(line[: first_ascii.start()])
            english_part = normalize_same_source(line[first_ascii.start() :])
            chinese_part = re.sub(r"^(?:主题|相关词汇/短语|名词|动词|形容词|其他)\s*", "", chinese_part)
            chinese_count = len(re.findall(r"[\u3400-\u9fff]", chinese_part))
            english_letters = sum(character.isascii() and character.isalpha() for character in english_part)
            if chinese_count < 1 or english_letters < 2:
                continue
            if any(marker in english_part for marker in HEADER_MARKERS) or english_part.startswith("AI "):
                continue
            english_part = english_part.strip(" -–—")
            key = (page, chinese_part, english_part)
            if key in seen:
                continue
            seen.add(key)
            serial += 1
            status = "rejected" if "riddle with challenges" in english_part.lower() else "candidate"
            record: dict[str, object] = {
                "topic_candidate_id": f"WRITING-TOPIC-{serial:04d}",
                "source_id": source.source_id,
                "source_page": page,
                "source_line": line_no,
                "verbatim": line.strip(),
                "normalized": normalize_same_source(line),
                "normalization_scope": "same_source_unicode_and_whitespace_only",
                "chinese_candidate": chinese_part,
                "english_candidate": english_part,
                "language_status": "unreviewed",
                "review_status": status,
                "promotion_status": "not_promoted",
            }
            if status == "rejected":
                record["known_error_id"] = "WRITING-ERR-003"
            records.append(record)
    return records


def find_year_heading(lines: list[str], year: int, start: int) -> int | None:
    pattern = re.compile(rf"^\s*{year}(?:\b|\s|[:：—-])")
    for index in range(start, len(lines)):
        if pattern.search(lines[index]):
            return index
    return None


def infer_big_type(text: str) -> str:
    lowered = text.lower()
    if ("picture" in lowered or "drawing" in lowered) and ("chart" in lowered or "table" in lowered):
        return "图画+图表作文（OCR 候选）"
    if "picture" in lowered or "drawing" in lowered:
        return "图画作文（OCR 候选）"
    if "chart" in lowered or "table" in lowered:
        return "图表作文（OCR 候选）"
    return "大作文题面（具体载体待核验）"


def infer_small_type(text: str) -> str:
    lowered = text.lower()
    if "notice" in lowered:
        return "通知/告示（OCR 候选）"
    if "reply" in lowered or "answer the inquiry" in lowered:
        return "回复信/邮件（OCR 候选）"
    if "invite" in lowered:
        return "邀请信/邮件（OCR 候选）"
    if "recommend" in lowered:
        return "推荐信/邮件（OCR 候选）"
    if "apolog" in lowered or "apology" in lowered:
        return "道歉信（OCR 候选）"
    if "suggest" in lowered or "advice" in lowered or "suggestion" in lowered:
        return "建议信/邮件（OCR 候选）"
    if "thank" in lowered or "gratitude" in lowered:
        return "感谢信/邮件（OCR 候选）"
    if "resign" in lowered or "quit" in lowered:
        return "辞职信（OCR 候选）"
    if "complain" in lowered:
        return "投诉信（OCR 候选）"
    if "letter" in lowered or "email" in lowered or "e-mail" in lowered:
        return "书信/邮件（功能待细分）"
    return "小作文应用文（题型待核验）"


def prompt_candidates(
    source: Source,
    pages: dict[int, str],
    mapping: dict[int, tuple[tuple[str, int], ...]],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    serial = 0
    section_code = "BIG" if source is OCR_SOURCES[0] else "SMALL"
    for page, entries in mapping.items():
        text = pages[page]
        lines = text.splitlines()
        positions: list[int | None] = []
        cursor = 0
        for _paper, year in entries:
            position = find_year_heading(lines, year, cursor)
            positions.append(position)
            if position is not None:
                cursor = position + 1
        for index, ((paper, year), position) in enumerate(zip(entries, positions)):
            next_positions = [candidate for candidate in positions[index + 1 :] if candidate is not None]
            if position is None:
                segment = text
                extraction_status = "year_marker_missing_full_page_fallback"
            else:
                end = next_positions[0] if next_positions else len(lines)
                segment = "\n".join(lines[position:end]).strip()
                extraction_status = "year_segment_extracted"
            serial += 1
            task_type = infer_big_type(segment) if source is OCR_SOURCES[0] else infer_small_type(segment)
            records.append(
                {
                    "prompt_candidate_id": f"WRITING-PROMPT-{section_code}-{serial:04d}",
                    "source_id": source.source_id,
                    "source_page": page,
                    "paper": paper,
                    "year": year,
                    "writing_section": "大作文" if source is OCR_SOURCES[0] else "小作文",
                    "task_type_candidate": task_type,
                    "verbatim": segment,
                    "normalized": normalize_same_source(segment),
                    "normalization_scope": "same_ocr_unicode_and_whitespace_only",
                    "extraction_method": "tesseract_eng_180dpi_psm6",
                    "extraction_status": extraction_status,
                    "language_status": "unreviewed",
                    "review_status": "candidate",
                    "chinese_visual_status": "待核验",
                    "promotion_status": "not_promoted",
                }
            )
    return records


def source_manifest(source: Source, info: dict[str, object], digest: str) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "title": source.title,
        "filename": source.path.name,
        "external_source_path": str(source.path),
        "source_role": source.role,
        "extraction": source.extraction,
        "pages": info["pages"],
        "page_size": info["page_size"],
        "pdf_version": info["pdf_version"],
        "bytes": source.path.stat().st_size,
        "sha256": digest,
        "expected_sha256_match": digest == EXPECTED_SHA256[source.source_id],
        "expected_page_count_match": info["pages"] == EXPECTED_PAGES[source.source_id],
        "source_storage": "external_read_only_not_copied",
        "rights_scope": "private_study_reference_only",
    }


def markdown_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def heading_section(records: list[dict[str, object]], source_id: str) -> list[str]:
    source_records = [record for record in records if record["source_id"] == source_id]
    out = [f"共 {len(source_records)} 条标题层级候选；均为 `unreviewed`。", ""]
    current_page = None
    for record in source_records:
        page = record["source_page"]
        if page != current_page:
            out.extend([f"### PDF p{page}", ""])
            current_page = page
        indent = "  " * max(0, int(record["heading_level_candidate"]) - 1)
        out.append(f"{indent}- `{record['heading_id']}` {markdown_escape(record['normalized'])}")
    out.append("")
    return out


def approved_whitelist_markdown(
    build_date: date,
    patterns: list[dict[str, object]],
    vocabulary: list[dict[str, object]],
) -> str:
    lines = [
        "# 作文造句可用白名单",
        "",
        f"人工复核日期：{build_date.isoformat()}",
        "",
        "> 本页是完整作文候选层之上的小型人工复核白名单。只有 `status=approved|corrected` 的条目可直接用于未来造句；其余 `unreviewed` 候选仍只能用于检索和人工回看。",
        "",
        "## 使用顺序",
        "",
        "1. 先从本页选择语义匹配的批准词汇或搭配。",
        "2. 再选择一个槽位和语域匹配的批准句型。",
        "3. 逐项执行 `usage_constraints`，确保主谓一致、时态、介词、数值口径和指代自然。",
        "4. 同时与大纲词汇 reviewed/reference 层交叉检查；能自然使用大纲词时优先使用。",
        "5. 本白名单不等于正式入库，不自动写入 `master_bank.csv` 或 `sentence_patterns.md`。",
        "",
        "## 数量",
        "",
        f"- 批准句型：{len(patterns)} 条。",
        f"- 批准主题词/搭配：{len(vocabulary)} 条。",
        "- 完整未审核候选层保持不变。",
        "",
        "## 批准句型",
        "",
        "| approved_id | 状态 | 类别 | 来源 | 可直接调用模板 | 关键限制 |",
        "|---|---|---|---|---|---|",
    ]
    for record in patterns:
        constraints = "<br>".join(markdown_escape(item) for item in record["usage_constraints"])
        lines.append(
            f"| {record['approved_id']} | {record['status']} | {record['category']} | {record['source_id']} p{record['source_page']} / {record['source_candidate_id']} | {markdown_escape(record['template'])} | {constraints} |"
        )
    lines.extend(
        [
            "",
            "机器数据：[approved_patterns.jsonl](../../raw/writing_reference/reviewed/approved_patterns.jsonl)",
            "",
            "## 批准主题词与搭配",
            "",
        ]
    )
    themes: list[str] = []
    for record in vocabulary:
        theme = str(record["theme"])
        if theme not in themes:
            themes.append(theme)
    for theme in themes:
        theme_records = [record for record in vocabulary if record["theme"] == theme]
        lines.extend(
            [
                f"### {theme}",
                "",
                "| approved_id | 批准表达 | 来源 | 形式 | 审核说明 |",
                "|---|---|---|---|---|",
            ]
        )
        for record in theme_records:
            lines.append(
                f"| {record['approved_id']} | {markdown_escape(record['normalized'])} | p{record['source_page']} / {record['source_candidate_id']} | {record['form']} | {markdown_escape(record['review_note'])} |"
            )
        lines.append("")
    lines.extend(
        [
            "机器数据：[approved_vocabulary.jsonl](../../raw/writing_reference/reviewed/approved_vocabulary.jsonl)",
            "",
            "## 已知错误处理",
            "",
            "- `WRITING-ERR-001` 日期中英不一致：未进入白名单。",
            "- `WRITING-ERR-002` `If there had been no your...`：未进入白名单。",
            "- `WRITING-ERR-003` 原 `riddle with challenges`：原候选继续 rejected；白名单只批准更正后的 `be riddled with challenges`。",
            "",
            "## 回退规则",
            "",
            "如果本白名单没有语义和槽位都自然的组合，宁可自造一个简洁自然句，也不要从完整未审核候选层直接拼接或硬套。",
            "",
        ]
    )
    return "\n".join(lines)


def build_markdown(
    build_date: date,
    manifest: list[dict[str, object]],
    english_records: list[dict[str, object]],
    heading_records: list[dict[str, object]],
    topic_records: list[dict[str, object]],
    prompt_records: list[dict[str, object]],
    approved_patterns: list[dict[str, object]],
    approved_vocabulary: list[dict[str, object]],
    checks: list[dict[str, str]],
) -> dict[Path, str]:
    manifest_rows = []
    for record in manifest:
        manifest_rows.append(
            f"| {record['source_id']} | {record['title']} | {record['source_role']} | {record['pages']} | `{record['sha256']}` |"
        )

    known_error_rows = []
    for error in KNOWN_ERRORS:
        corrected = error["corrected_candidate"]
        if isinstance(corrected, dict):
            corrected_text = f"{corrected['english']} / {corrected['chinese']}"
        else:
            corrected_text = str(corrected)
        known_error_rows.append(
            "| {error_id} | {source_id} p{pages} | {issue} | rejected | {corrected} | corrected |".format(
                error_id=error["error_id"],
                source_id=error["source_id"],
                pages=",".join(str(page) for page in error["source_pages"]),
                issue=markdown_escape(error["reason"]),
                corrected=markdown_escape(corrected_text),
            )
        )

    overall = "\n".join(
        [
            "# 作文资料总索引",
            "",
            f"生成日期：{build_date.isoformat()}",
            "",
            "> 本目录是作文 PDF 的独立候选参考层。所有自动抽取内容默认 `language_status=unreviewed`、`promotion_status=not_promoted`；不得直接当作正式句式卡或长期词条。",
            "",
            "## 使用边界",
            "",
            "- 核心三册按 PDF 页保存原始文本层，并建立英文句/短语、标题层级、主题词候选。",
            "- 附件 1/2 只使用 `eng` OCR 保存英文题面候选；图画、图表及中文图注统一标记 `待核验`。",
            "- 附件 3 仅登记为打印答题卡资产，不做 OCR，不进入语言候选。",
            "- 未来造句时，作文候选只能作为待审核参考；正式使用还应与大纲词汇参考层交叉检查。",
            "- 本次未写入 `bank/master_bank.csv`、`bank/sentence_patterns.md`、全局 prompt/index/log/schema。",
            "",
            "## 来源清单",
            "",
            "| source_id | 资料 | 角色 | 页数 | SHA-256 |",
            "|---|---|---|---:|---|",
            *manifest_rows,
            "",
            "完整机器清单：[manifest.json](../../raw/writing_reference/manifest.json)",
            "",
            "## 导航",
            "",
            "- [作文造句可用白名单](作文造句可用白名单.md)",
            "- [大作文候选索引](大作文候选索引.md)",
            "- [小作文候选索引](小作文候选索引.md)",
            "- [主题词候选索引](主题词候选索引.md)",
            "- [历年题目索引](历年题目索引.md)",
            "- [验证报告](验证报告.md)",
            "- [英文句/短语候选 JSONL](../../raw/writing_reference/extracted/english_candidates.jsonl)",
            "- [标题层级候选 JSONL](../../raw/writing_reference/extracted/headings.jsonl)",
            "- [主题词候选 JSONL](../../raw/writing_reference/extracted/topic_candidates.jsonl)",
            "- [历年题面 OCR 候选 JSONL](../../raw/writing_reference/ocr/prompt_candidates.jsonl)",
            "- [已知错误与更正 JSONL](../../raw/writing_reference/extracted/known_error_candidates.jsonl)",
            "",
            "## 已知错误隔离",
            "",
            "| error_id | 来源 | 问题 | 原候选状态 | 更正候选 | 更正状态 |",
            "|---|---|---|---|---|---|",
            *known_error_rows,
            "",
            "> rejected 原文仅用于追溯，不得进入未来造句；应使用 corrected 候选，并在正式使用前再做人工语言审核。",
            "",
        ]
    )

    big_english = [record for record in english_records if record["source_id"] == "WRITING-SRC-BIG-TEMPLATE"]
    big_prompts = [record for record in prompt_records if record["writing_section"] == "大作文"]
    big_lines = [
        "# 大作文候选索引",
        "",
        f"生成日期：{build_date.isoformat()}",
        "",
        f"- 核心模板英文句/短语候选：{len(big_english)} 条。",
        f"- 历年大作文 OCR 题面候选：{len(big_prompts)} 条（英语一 25 条；英语二 16 条）。",
        "- 候选正文见 [english_candidates.jsonl](../../raw/writing_reference/extracted/english_candidates.jsonl)，按 `source_id=WRITING-SRC-BIG-TEMPLATE` 查询。",
        "- 历年题面见 [历年题目索引](历年题目索引.md)。",
        "",
        "## 标题层级候选",
        "",
        *heading_section(heading_records, "WRITING-SRC-BIG-TEMPLATE"),
        "## 审核边界",
        "",
        "- 自动抽取按物理行保留，跨行句子可能被拆分；引用前必须回看对应页原始文本。",
        "- 图画/图表题面中的视觉信息未由英文 OCR 完整恢复，中文图注仍为 `待核验`。",
        "",
    ]

    small_english = [record for record in english_records if record["source_id"] == "WRITING-SRC-SMALL-TEMPLATE"]
    small_prompts = [record for record in prompt_records if record["writing_section"] == "小作文"]
    small_lines = [
        "# 小作文候选索引",
        "",
        f"生成日期：{build_date.isoformat()}",
        "",
        f"- 核心模板英文句/短语候选：{len(small_english)} 条。",
        f"- 历年小作文 OCR 题面候选：{len(small_prompts)} 条（英语一 21 条；英语二 16 条）。",
        "- 候选正文见 [english_candidates.jsonl](../../raw/writing_reference/extracted/english_candidates.jsonl)，按 `source_id=WRITING-SRC-SMALL-TEMPLATE` 查询。",
        "- 已知错误 `WRITING-ERR-001/002` 已隔离为 rejected；不得复用原句。",
        "",
        "## 标题层级候选",
        "",
        *heading_section(heading_records, "WRITING-SRC-SMALL-TEMPLATE"),
        "## 审核边界",
        "",
        "- 书信/通知等功能标签来自 OCR 关键词，仅是候选，不等于人工确认。",
        "- 自动抽取按物理行保留；引用前须回看对应页文本与错误登记。",
        "",
    ]

    topic_lines = [
        "# 主题词候选索引",
        "",
        f"生成日期：{build_date.isoformat()}",
        "",
        f"共 {len(topic_records)} 条中英主题词/短语候选，全部保留 PDF 页码；默认 `unreviewed`。",
        "",
        "| candidate_id | PDF 页 | 中文候选 | 英文候选 | 状态 |",
        "|---|---:|---|---|---|",
    ]
    for record in topic_records:
        topic_lines.append(
            f"| {record['topic_candidate_id']} | {record['source_page']} | {markdown_escape(record['chinese_candidate'])} | {markdown_escape(record['english_candidate'])} | {record['review_status']} |"
        )
    topic_lines.extend(
        [
            "",
            "> `WRITING-ERR-003`（`riddle with challenges`）已标记 rejected；更正候选为 `be riddled with challenges`。",
            "",
            "机器数据：[topic_candidates.jsonl](../../raw/writing_reference/extracted/topic_candidates.jsonl)",
            "",
        ]
    )

    prompt_lines = [
        "# 历年题目索引",
        "",
        f"生成日期：{build_date.isoformat()}",
        "",
        "> 附件 1/2 为图像型题面。本索引来自 `tesseract -l eng --psm 6`；英文题面候选未审核，中文图注/图画/图表统一 `待核验`。",
        "",
        "| candidate_id | 试卷 | 年份 | 板块 | 题型候选 | PDF 页 | OCR 状态 | 中文视觉 |",
        "|---|---|---:|---|---|---:|---|---|",
    ]
    for record in sorted(prompt_records, key=lambda item: (str(item["paper"]), int(item["year"]), str(item["writing_section"]))):
        prompt_lines.append(
            f"| {record['prompt_candidate_id']} | {record['paper']} | {record['year']} | {record['writing_section']} | {record['task_type_candidate']} | {record['source_page']} | {record['review_status']} | {record['chinese_visual_status']} |"
        )
    prompt_lines.extend(
        [
            "",
            "完整 OCR 候选：[prompt_candidates.jsonl](../../raw/writing_reference/ocr/prompt_candidates.jsonl)",
            "",
        ]
    )

    check_rows = [f"| {record['check']} | {record['status']} | {record['evidence']} |" for record in checks]
    validation_lines = [
        "# 作文参考层验证报告",
        "",
        f"验证日期：{build_date.isoformat()}",
        "",
        "| 检查 | 状态 | 证据 |",
        "|---|---|---|",
        *check_rows,
        "",
        "## 明确未执行",
        "",
        "- 未将任何候选写入 `bank/master_bank.csv`。",
        "- 未将任何候选写入 `bank/sentence_patterns.md`。",
        "- 未修改 `prompts/codex_prompt.md`、`raw/index.md`、`wiki/index.md`、`wiki/log.md` 或全局 schema。",
        "- 未对附件 3 做语言抽取；它只作为打印答题卡资产登记。",
        "",
        "## 人工复核队列",
        "",
        "1. 对未来实际采用的英文句型逐条回看来源页并做语言审核。",
        "2. 对附件 1/2 的图画、图表和中文图注进行视觉核验。",
        "3. 正式造句前同时交叉检查大纲词汇参考层与本作文参考层。",
        "",
    ]

    return {
        WIKI_ROOT / "作文资料总索引.md": overall,
        WIKI_ROOT / "大作文候选索引.md": "\n".join(big_lines),
        WIKI_ROOT / "小作文候选索引.md": "\n".join(small_lines),
        WIKI_ROOT / "主题词候选索引.md": "\n".join(topic_lines),
        WIKI_ROOT / "历年题目索引.md": "\n".join(prompt_lines),
        WIKI_ROOT / "验证报告.md": "\n".join(validation_lines),
        WIKI_ROOT / "作文造句可用白名单.md": approved_whitelist_markdown(
            build_date, approved_patterns, approved_vocabulary
        ),
    }


def checks_for_build(
    manifest: list[dict[str, object]],
    core_pages: dict[str, dict[int, str]],
    english_records: list[dict[str, object]],
    heading_records: list[dict[str, object]],
    topic_records: list[dict[str, object]],
    ocr_pages: dict[str, dict[int, str]],
    prompt_records: list[dict[str, object]],
    approved_patterns: list[dict[str, object]],
    approved_vocabulary: list[dict[str, object]],
    protected_unchanged: bool,
    protected_count: int,
    candidate_layer_unchanged: bool,
    candidate_snapshot_count: int,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def add(name: str, passed: bool, evidence: str, *, warn: bool = False) -> None:
        status = "PASS" if passed else ("WARN" if warn else "FAIL")
        checks.append({"check": name, "status": status, "evidence": evidence})

    add("六份来源已登记", len(manifest) == 6, f"manifest entries={len(manifest)}")
    checksum_ok = all(bool(record["expected_sha256_match"]) for record in manifest)
    add("来源 SHA-256 匹配审计基线", checksum_ok, f"matched={sum(bool(record['expected_sha256_match']) for record in manifest)}/6")
    page_ok = all(bool(record["expected_page_count_match"]) for record in manifest)
    add("来源页数匹配审计基线", page_ok, f"matched={sum(bool(record['expected_page_count_match']) for record in manifest)}/6")
    core_count = sum(len(pages) for pages in core_pages.values())
    core_expected = sum(EXPECTED_PAGES[source.source_id] for source in CORE_SOURCES)
    add("核心三册逐页文本齐全", core_count == core_expected, f"page_text_files={core_count}/{core_expected}")
    nonempty = sum(bool(text.strip()) for pages in core_pages.values() for text in pages.values())
    add("核心页文本非空", nonempty == core_expected, f"nonempty_pages={nonempty}/{core_expected}")
    add("英文句/短语候选已生成", len(english_records) > 100, f"records={len(english_records)}")
    add("标题层级候选已生成", len(heading_records) > 10, f"records={len(heading_records)}")
    add("主题词候选已生成", len(topic_records) > 100, f"records={len(topic_records)}")
    ocr_count = sum(len(pages) for pages in ocr_pages.values())
    add("附件 1/2 英文 OCR 逐页齐全", ocr_count == 33, f"ocr_pages={ocr_count}/33")
    prompt_markers_ok = all(record["extraction_status"] == "year_segment_extracted" for record in prompt_records)
    add("历年题面年份切分完整", prompt_markers_ok, f"records={len(prompt_records)}, expected=78")
    add("历年题面记录数完整", len(prompt_records) == 78, f"records={len(prompt_records)}/78")
    add("已知错误全部隔离", len(KNOWN_ERRORS) == 3, "rejected=3, corrected=3")
    add("附件 3 仅登记打印资产", PRINT_SOURCE.role == "print_asset_only", "ocr=not_run, language_candidates=0")
    add(
        "受保护正式/全局文件构建前后未变",
        protected_unchanged,
        f"sha256 unchanged={protected_count}/{protected_count}; script write roots only: raw/writing_reference, wiki/writing",
    )
    add("候选语言状态", all(record["language_status"] == "unreviewed" for record in english_records + topic_records + prompt_records), "all generated candidates=unreviewed")
    add("附件中文视觉状态", all(record["chinese_visual_status"] == "待核验" for record in prompt_records), "78/78 prompt candidates=待核验")
    add("人工批准句型数量", len(approved_patterns) == 25, f"approved_patterns={len(approved_patterns)}/25")
    add("人工批准主题词/搭配数量", len(approved_vocabulary) == 46, f"approved_vocabulary={len(approved_vocabulary)}/46")
    add(
        "批准条目状态与来源",
        all(record["status"] in {"approved", "corrected"} and record["language_status"] == "reviewed" for record in approved_patterns + approved_vocabulary),
        "all usable entries=approved|corrected and reviewed; source PDFs=core text layer only",
    )
    corrected_patterns = [record for record in approved_patterns if record["status"] == "corrected"]
    add(
        "句型更正状态受控",
        bool(len(corrected_patterns) == 1
        and corrected_patterns[0]["approved_id"] == "WRITING-APPROVED-PATTERN-003"
        and corrected_patterns[0].get("correction_note")
        and corrected_patterns[0].get("source_verbatim")),
        "corrected_patterns=1; PATTERN-003 keeps source_verbatim and correction_note",
    )
    corrected_sources = [record for record in approved_vocabulary if record["source_candidate_status"] == "rejected"]
    corrected_ok = len(corrected_sources) == 1 and all(
        record.get("known_error_id") == "WRITING-ERR-003"
        and record["approval_basis"] == "known_error_corrected_candidate"
        and record["normalized"] == "be riddled with challenges"
        for record in corrected_sources
    )
    add("已知错误只允许更正候选", corrected_ok, "rejected source approvals=1; only WRITING-ERR-003 corrected form")
    direct_text = "\n".join(
        str(record.get("template", "")) + "\n" + str(record.get("normalized", ""))
        for record in approved_patterns + approved_vocabulary
    ).lower()
    forbidden = (
        "if there had been no your",
        "riddle with challenges",
        "attend in",
        "times more than",
        "push the social progress",
        "referring to the statistics provided",
        "which overshadows all other components",
    )
    add(
        "批准文本排除已知病句与高风险片段",
        not any(item in direct_text for item in forbidden),
        "forbidden_fragments=0",
    )
    add(
        "完整候选层保持不变",
        candidate_layer_unchanged,
        f"sha256 unchanged={candidate_snapshot_count}/{candidate_snapshot_count}",
    )
    return checks


def readme_text(build_date: date) -> str:
    return "\n".join(
        [
            "# Writing reference raw layer",
            "",
            f"Build date: {build_date.isoformat()}",
            "",
            "This directory contains page-level text/OCR and unreviewed candidate records derived from six external PDFs for private study.",
            "",
            "- `manifest.json`: immutable source identity, path, page count, and SHA-256.",
            "- `core/*/pages/`: page-by-page `pdftotext -layout` output for the three text-layer PDFs.",
            "- `extracted/english_candidates.jsonl`: unreviewed English sentence/phrase line spans with source page and line.",
            "- `extracted/headings.jsonl`: unreviewed heading hierarchy candidates.",
            "- `extracted/topic_candidates.jsonl`: unreviewed bilingual topic-word candidates.",
            "- `extracted/known_error_candidates.jsonl`: source errors explicitly rejected and corrected.",
            "- `ocr/*/pages/`: English-only OCR for the historical prompt attachments.",
            "- `ocr/prompt_candidates.jsonl`: 78 year/paper/page prompt candidates; Chinese visuals remain pending review.",
            "- `print_asset_registry.json`: answer-sheet registration only.",
            "- `reviewed/approved_patterns.jsonl`: manually reviewed sentence-pattern whitelist.",
            "- `reviewed/approved_vocabulary.jsonl`: manually reviewed topic/collocation whitelist.",
            "",
            "Nothing in this directory is approved for automatic promotion into the formal vocabulary or sentence-pattern banks.",
            "",
        ]
    )


def build(args: argparse.Namespace) -> dict[str, object]:
    require_tools()
    build_date = date.fromisoformat(args.date)
    protected_before = protected_file_snapshot()
    candidate_before = relative_file_snapshot(UNREVIEWED_CANDIDATE_PATHS)
    for source in SOURCES:
        if not source.path.exists():
            raise FileNotFoundError(source.path)

    manifest: list[dict[str, object]] = []
    source_info: dict[str, dict[str, object]] = {}
    for source in SOURCES:
        info = pdf_info(source.path)
        digest = sha256(source.path)
        source_info[source.source_id] = info
        manifest.append(source_manifest(source, info, digest))

    core_pages: dict[str, dict[int, str]] = {}
    english_records: list[dict[str, object]] = []
    heading_records: list[dict[str, object]] = []
    topic_records: list[dict[str, object]] = []
    for source in CORE_SOURCES:
        pages: dict[int, str] = {}
        page_count = int(source_info[source.source_id]["pages"])
        for page in range(1, page_count + 1):
            text = extract_text_page(source, page)
            pages[page] = text
            write_text(RAW_ROOT / "core" / source.slug / "pages" / f"page-{page:03d}.txt", text)
        core_pages[source.source_id] = pages
        english_records.extend(english_line_candidates(source, pages))
        heading_records.extend(heading_candidates(source, pages))
        if source.source_id == "WRITING-SRC-TOPIC-VOCAB":
            topic_records.extend(topic_candidates(source, pages))

    ocr_pages: dict[str, dict[int, str]] = {}
    prompt_records: list[dict[str, object]] = []
    for source, mapping in zip(OCR_SOURCES, (HISTORICAL_BIG_PAGE_MAP, HISTORICAL_SMALL_PAGE_MAP)):
        pages: dict[int, str] = {}
        page_count = int(source_info[source.source_id]["pages"])
        for page in range(1, page_count + 1):
            pages[page] = ocr_page(source, page, refresh=args.refresh_ocr)
        ocr_pages[source.source_id] = pages
        prompt_records.extend(prompt_candidates(source, pages, mapping))

    approved_patterns = build_approved_patterns(english_records, core_pages, build_date)
    approved_vocabulary = build_approved_vocabulary(topic_records, build_date)

    write_text(RAW_ROOT / "README.md", readme_text(build_date))
    write_json(
        RAW_ROOT / "manifest.json",
        {
            "schema": "writing_reference_manifest_v1",
            "generated_date": build_date.isoformat(),
            "candidate_policy": "unreviewed_not_promoted",
            "sources": manifest,
        },
    )
    write_jsonl(RAW_ROOT / "extracted" / "english_candidates.jsonl", english_records)
    write_jsonl(RAW_ROOT / "extracted" / "headings.jsonl", heading_records)
    write_jsonl(RAW_ROOT / "extracted" / "topic_candidates.jsonl", topic_records)
    write_jsonl(RAW_ROOT / "extracted" / "known_error_candidates.jsonl", KNOWN_ERRORS)
    write_jsonl(RAW_ROOT / "ocr" / "prompt_candidates.jsonl", prompt_records)
    write_jsonl(RAW_ROOT / "reviewed" / "approved_patterns.jsonl", approved_patterns)
    write_jsonl(RAW_ROOT / "reviewed" / "approved_vocabulary.jsonl", approved_vocabulary)
    write_json(
        RAW_ROOT / "print_asset_registry.json",
        {
            "schema": "writing_print_asset_registry_v1",
            "source_id": PRINT_SOURCE.source_id,
            "external_source_path": str(PRINT_SOURCE.path),
            "pages": source_info[PRINT_SOURCE.source_id]["pages"],
            "role": "print_asset_only",
            "text_extraction": "not_run",
            "language_candidate_count": 0,
        },
    )

    protected_after = protected_file_snapshot()
    protected_unchanged = protected_before == protected_after
    candidate_after = relative_file_snapshot(UNREVIEWED_CANDIDATE_PATHS)
    candidate_layer_unchanged = not candidate_before or candidate_before == candidate_after
    checks = checks_for_build(
        manifest,
        core_pages,
        english_records,
        heading_records,
        topic_records,
        ocr_pages,
        prompt_records,
        approved_patterns,
        approved_vocabulary,
        protected_unchanged,
        len(protected_before),
        candidate_layer_unchanged,
        len(candidate_after),
    )
    markdown_files = build_markdown(
        build_date,
        manifest,
        english_records,
        heading_records,
        topic_records,
        prompt_records,
        approved_patterns,
        approved_vocabulary,
        checks,
    )
    for path, content in markdown_files.items():
        write_text(path, content)

    receipt = {
        "schema": "writing_reference_build_receipt_v1",
        "generated_date": build_date.isoformat(),
        "source_count": len(manifest),
        "core_page_text_count": sum(len(pages) for pages in core_pages.values()),
        "ocr_page_text_count": sum(len(pages) for pages in ocr_pages.values()),
        "english_candidate_count": len(english_records),
        "heading_candidate_count": len(heading_records),
        "topic_candidate_count": len(topic_records),
        "prompt_candidate_count": len(prompt_records),
        "known_error_count": len(KNOWN_ERRORS),
        "approved_pattern_count": len(approved_patterns),
        "approved_vocabulary_count": len(approved_vocabulary),
        "unreviewed_candidate_layer_unchanged_during_build": candidate_layer_unchanged,
        "unreviewed_candidate_hashes": candidate_after,
        "formal_data_paths_excluded": list(FORMAL_DATA_PATHS),
        "protected_files_unchanged_during_build": protected_unchanged,
        "protected_file_hashes": protected_after,
        "checks": checks,
    }
    write_json(RAW_ROOT / "build_receipt.json", receipt)
    return receipt


def verify_existing() -> dict[str, object]:
    required = [
        RAW_ROOT / "manifest.json",
        RAW_ROOT / "build_receipt.json",
        RAW_ROOT / "extracted" / "english_candidates.jsonl",
        RAW_ROOT / "extracted" / "headings.jsonl",
        RAW_ROOT / "extracted" / "topic_candidates.jsonl",
        RAW_ROOT / "extracted" / "known_error_candidates.jsonl",
        RAW_ROOT / "ocr" / "prompt_candidates.jsonl",
        RAW_ROOT / "reviewed" / "approved_patterns.jsonl",
        RAW_ROOT / "reviewed" / "approved_vocabulary.jsonl",
        RAW_ROOT / "print_asset_registry.json",
        WIKI_ROOT / "作文资料总索引.md",
        WIKI_ROOT / "大作文候选索引.md",
        WIKI_ROOT / "小作文候选索引.md",
        WIKI_ROOT / "主题词候选索引.md",
        WIKI_ROOT / "历年题目索引.md",
        WIKI_ROOT / "验证报告.md",
        WIKI_ROOT / "作文造句可用白名单.md",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"Missing generated files: {missing}")
    manifest = json.loads((RAW_ROOT / "manifest.json").read_text(encoding="utf-8"))
    sources = manifest["sources"]
    prompt_records = read_jsonl(RAW_ROOT / "ocr" / "prompt_candidates.jsonl")
    errors = read_jsonl(RAW_ROOT / "extracted" / "known_error_candidates.jsonl")
    english_records = read_jsonl(RAW_ROOT / "extracted" / "english_candidates.jsonl")
    topic_records = read_jsonl(RAW_ROOT / "extracted" / "topic_candidates.jsonl")
    approved_patterns = read_jsonl(RAW_ROOT / "reviewed" / "approved_patterns.jsonl")
    approved_vocabulary = read_jsonl(RAW_ROOT / "reviewed" / "approved_vocabulary.jsonl")
    core_page_files = list((RAW_ROOT / "core").glob("*/pages/page-*.txt"))
    ocr_page_files = list((RAW_ROOT / "ocr").glob("*/pages/page-*.txt"))
    validation = {
        "status": "PASS",
        "source_count": len(sources),
        "source_sha_current_match": 0,
        "core_page_text_count": len(core_page_files),
        "ocr_page_text_count": len(ocr_page_files),
        "prompt_candidate_count": len(prompt_records),
        "known_error_count": len(errors),
        "approved_pattern_count": len(approved_patterns),
        "approved_vocabulary_count": len(approved_vocabulary),
        "formal_data_paths_excluded": list(FORMAL_DATA_PATHS),
    }
    for source_record in sources:
        path = Path(source_record["external_source_path"])
        if path.exists() and sha256(path) == source_record["sha256"]:
            validation["source_sha_current_match"] += 1
    english_by_id = {str(record["candidate_id"]): record for record in english_records}
    topic_by_id = {str(record["topic_candidate_id"]): record for record in topic_records}
    errors_by_id = {str(record["error_id"]): record for record in errors}
    common_required = {
        "approved_id",
        "source_id",
        "source_page",
        "source_candidate_id",
        "verbatim",
        "normalized",
        "template",
        "status",
        "review_note",
        "usage_constraints",
    }
    pattern_required = common_required | {"source_verbatim"}
    vocabulary_required = common_required | {"source_candidate_status", "theme", "form"}
    pattern_source_mapping = True
    for record in approved_patterns:
        source_ids = record.get("source_candidate_ids", [record.get("source_candidate_id")])
        source_records = [english_by_id.get(str(candidate_id)) for candidate_id in source_ids]
        if not source_records or any(source is None for source in source_records):
            pattern_source_mapping = False
            break
        if any(
            source["source_id"] != record["source_id"]
            or int(source["source_page"]) != int(record["source_page"])
            for source in source_records
            if source is not None
        ):
            pattern_source_mapping = False
            break
        source = SOURCE_BY_ID[str(record["source_id"])]
        page_path = RAW_ROOT / "core" / source.slug / "pages" / f"page-{int(record['source_page']):03d}.txt"
        if not page_path.exists() or normalize_for_source_match(str(record["verbatim"])) not in normalize_for_source_match(page_path.read_text(encoding="utf-8")):
            pattern_source_mapping = False
            break

    vocabulary_source_mapping = all(
        str(record.get("source_candidate_id")) in topic_by_id
        and topic_by_id[str(record["source_candidate_id"])]["source_id"] == record["source_id"]
        and int(topic_by_id[str(record["source_candidate_id"])]["source_page"]) == int(record["source_page"])
        and topic_by_id[str(record["source_candidate_id"])]["english_candidate"] == record["verbatim"]
        for record in approved_vocabulary
    )
    rejected_approvals = [record for record in approved_vocabulary if record.get("source_candidate_status") == "rejected"]
    corrected_only = len(rejected_approvals) == 1 and all(
        record.get("known_error_id") in errors_by_id
        and record.get("approval_basis") == "known_error_corrected_candidate"
        and isinstance(errors_by_id[str(record["known_error_id"])]["corrected_candidate"], str)
        and normalize_for_source_match(str(errors_by_id[str(record["known_error_id"])]["corrected_candidate"]))
        == normalize_for_source_match(str(record["normalized"]))
        for record in rejected_approvals
    )
    approved_text = "\n".join(
        str(record.get("normalized", "")) + "\n" + str(record.get("template", ""))
        for record in approved_patterns + approved_vocabulary
    ).lower()
    forbidden = (
        "if there had been no your",
        "riddle with challenges",
        "attend in",
        "times more than",
        "push the social progress",
        "referring to the statistics provided",
        "which overshadows all other components",
    )
    assertions = {
        "six_sources": len(sources) == 6,
        "all_source_sha_current": validation["source_sha_current_match"] == 6,
        "all_core_pages": len(core_page_files) == 129,
        "all_ocr_pages": len(ocr_page_files) == 33,
        "all_prompt_candidates": len(prompt_records) == 78,
        "all_known_errors": len(errors) == 3,
        "prompt_statuses": all(
            record["language_status"] == "unreviewed"
            and record["chinese_visual_status"] == "待核验"
            for record in prompt_records
        ),
        "errors_rejected_corrected": all(
            record["candidate_status"] == "rejected"
            and record["corrected_candidate_status"] == "corrected"
            for record in errors
        ),
        "approved_pattern_count": len(approved_patterns) == 25,
        "approved_vocabulary_count": len(approved_vocabulary) == 46,
        "approved_ids_unique": len({record["approved_id"] for record in approved_patterns + approved_vocabulary})
        == len(approved_patterns) + len(approved_vocabulary),
        "approved_required_fields": all(pattern_required <= set(record) for record in approved_patterns)
        and all(vocabulary_required <= set(record) for record in approved_vocabulary),
        "approved_statuses": all(
            record.get("status") in {"approved", "corrected"}
            and record.get("language_status") == "reviewed"
            and isinstance(record.get("usage_constraints"), list)
            and bool(record["usage_constraints"])
            for record in approved_patterns + approved_vocabulary
        ),
        "approved_core_sources_only": all(
            record.get("source_id") in {source.source_id for source in CORE_SOURCES}
            for record in approved_patterns + approved_vocabulary
        ),
        "approved_pattern_source_mapping": pattern_source_mapping,
        "approved_vocabulary_source_mapping": vocabulary_source_mapping,
        "known_errors_corrected_only": corrected_only,
        "approved_no_forbidden_fragments": not any(fragment in approved_text for fragment in forbidden),
        "approved_templates_no_ellipsis": all(
            "..." not in str(record["template"]) and "…" not in str(record["template"])
            for record in approved_patterns
        ),
        "corrected_pattern_policy": len([record for record in approved_patterns if record.get("status") == "corrected"]) == 1
        and any(
            record.get("approved_id") == "WRITING-APPROVED-PATTERN-003"
            and record.get("status") == "corrected"
            and bool(record.get("source_verbatim"))
            and bool(record.get("correction_note"))
            and record.get("template") == "The statistics provided in the [CHART_TYPE] show that [DATA_DESCRIPTION]."
            for record in approved_patterns
        ),
    }
    validation["assertions"] = assertions
    if not all(assertions.values()):
        validation["status"] = "FAIL"
        raise RuntimeError(json.dumps(validation, ensure_ascii=False, indent=2))
    return validation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat(), help="Build date in YYYY-MM-DD format")
    parser.add_argument("--refresh-ocr", action="store_true", help="Re-render and OCR all attachment pages")
    parser.add_argument("--verify-only", action="store_true", help="Validate existing generated outputs without writing")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = verify_existing() if args.verify_only else build(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
