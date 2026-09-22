# 历年阅读答案解析受保护契约

## Scope

适用于 2010 至 2024 年考研英语一 Section II Reading Comprehension Part A，共 60 篇、300 题。

## Goal

把用户提供的解析 PDF 预处理为可按年份、Text 和题号最小读取的受保护证据。自主练习阶段不泄漏答案；用户明确请求判定、讲题或进入复盘后，可以只读取被问到的题号块。

“隐藏”只表示默认不展示。完成状态要求答案正文、解析正文、题号定位和来源页都已落盘，不能用 hidden_until_review 占位替代内容。

## Success criteria

- 60 篇逐篇记录和 300 道逐题记录完整。
- 每题有 A 至 D 中的一个答案、非空出版方解析和来源页。
- practice-safe 语料和自主练习页不含答案、正确选项或解析正文。
- 解锁后只读取当前题号所需的最小块。
- OCR、PDF 文字层和视觉证据可追溯。
- 聊天只披露用户明确要求的范围。
- 本层不会自动写 Tutor、bank、句式卡或 review。

## Storage

- 外部 PDF：只读视觉权威，不移动、不覆盖。
- 逐页证据：raw/protected/exam-reading-analysis/source-pages/YYYY/page-NNN.json 及同名 Markdown。
- 逐篇预处理：raw/protected/exam-reading-analysis/YYYY/text-N.json 及同名 Markdown。
- 机器索引：raw/protected/exam-reading-analysis/index.json。
- Obsidian 入口：raw/protected/exam-reading-analysis/index.md。

本层属于 raw 或 derived evidence，不是用户作答记录。

## Required page fields

- source_id
- year
- pdf_page
- source_pdf
- source_sha256
- vision_ocr_text
- pdf_text_layer
- extraction_method
- visibility=hidden_until_review

Vision OCR 保存中文排版内容；PDF 文字层用于交叉核对和英文拼写。两者都保留，不能把润色文本冒充出版方原文。

## Required article fields

- analysis_id=EXAM-ANALYSIS-YYYY-TN
- reference_id=EXAM-READING-YYYY-TN
- 年份、Text、题号范围
- 来源 PDF、SHA-256 和实际来源页
- answer_key
- questions，其中每题至少含：
  - question_number
  - correct_answer
  - answer_evidence_page
  - analysis_source_pages
  - analysis_text
  - verification_status
- full_source_analysis_text
- visibility=hidden_until_review
- extraction_status

跨页解析要保存全部 analysis_source_pages。答案摘要页和详解页可以不同，分别记录证据页；不得为了形式一致伪造成同一页，也不得删掉错项分析、定位说明或出版方语篇分析。

## Answer verification

每个答案至少由一种用户来源内证据确认：

1. 出版方明确写出正确项或正确答案。
2. 出版方用颜色或字体标出正确选项，并经过渲染页视觉核验。
3. 既有结构化 raw 包与同一解析 PDF 视觉页一致。

只凭题意推断、模型记忆或非用户来源不合格。不确定时进入人工复核，不伪造 PASS。

## Access and disclosure

默认只读 practice-safe 层的场景：

- 自主练习。
- 题干或选项翻译。
- 词义、句法、指代、定位句和局部排除思路。
- 用户只陈述“我选 X”但没有请求判定。
- 用户只要求保存答案或解析。

允许读取当前题号最小受保护块的场景：

- 用户明确问答案、我选 X 对不对或正确选项是什么。
- 用户明确要求讲这一题。
- 用户已经提交作答并明确进入核对或复盘。
- 当前任务是受保护层本身的验证。

披露规则：

- 只回答被问到的题号，不顺带披露同篇其他题。
- “只记录答案或解析”可以执行被授权保存，但聊天回执不复述答案。
- 局部语言或结构问题不因答案已存在而自动解锁。
- 用户明确要求直接答案时不应过度保护。

## Stop rules

- 未满足解锁条件：停在 practice-safe 语言或结构解释。
- 题号、年份或 Text 无法确定：只询问最小缺失字段。
- 本地受保护块缺失或校验失败：报告缺口，不凭记忆补答案。
- OCR 与出版方页面冲突：回到实际来源页视觉核验；仍不确定则进入人工复核。
- 当前题证据足够并已回答后停止，不读取相邻题。

## Validation

最终验收证明：

1. 60/60 篇 JSON 与 Markdown 存在。
2. 300/300 题都有一个 A 至 D 答案。
3. 300/300 题都有非空解析和来源页。
4. 逐篇全文解析文本非空且可回溯。
5. 15 份解析 PDF 当前 SHA-256 与 manifest 一致。
6. 扫描页使用适合中文排版的 Vision OCR，不只依赖 eng OCR。
7. practice-safe JSON 与自主练习页无答案泄漏。
8. Obsidian CLI 可按年份、Text 和题号定位受保护记录。
9. Text 1 至 3 的末题不串入下一 Text，Text 4 的末题不串入 Part B。

只有全部通过，才能声称答案和解析已经完整预处理。
