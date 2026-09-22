# 生词例句四层地基契约

## Role

本契约只负责系统新生成英语例句的选材、证据、自然度门禁和停止条件。真实文章原句、用户原句和题目选项不是系统生成例句，不得为了命中参考层而改写。

## Goal

为目标词生成一条自然、可追溯、可复习的英语例句，同时保持 selector、派生图和正式学习数据之间的只读边界。

## Success criteria

最终例句只有在以下条件全部满足时才可输出：

1. 有真实 source_sentence 和本次目标词义、词性。
2. 目标 item 不在 bank/mastered_items.csv。
3. 有用户当前措辞或可追溯的明确历史错词证据。
4. 有一个 approved 或 corrected 的作文句型。
5. 有至少一个 approved 或 corrected 的作文词组或搭配。
6. 有至少一个 verification_status 以 verified 开头的大纲 occurrence。
7. 目标词义、词性、结构、搭配和语域自然。
8. 所有引用 ID 均来自 selector 返回的证据，不由模型补造。
9. selector、图谱构建和造句过程没有写入正式 bank、句式卡或 review。

SP 卡是可选联动，不能替代作文句型。目标词可同时承担作文词和大纲词命中，但两类来源必须分别核对。

## Evidence sources

- 用户证据：当前原句、当前措辞、明确不会、误译或错用记录。
- 作文白名单：wiki/writing/作文造句可用白名单.md。
- 大纲词汇：wiki/vocabulary/大纲词汇参考库.md。
- 派生关系图：raw/reference_relations/writing-vocabulary-foundation/。
- 图谱说明：wiki/relationships/作文-大纲词-错词关系图谱.md。
- 只读 selector：scripts/select_bbdc_foundation.py。
- 可选 SP：bank/sentence_patterns.md。

以下状态只允许定位，不允许自动用于例句：

- unreviewed
- pending
- rejected
- OCR 未人工核验
- lexical_candidate 词元重叠

lexical_candidate 不是语义关系、固定搭配或自然度证明。

## Workflow

1. 从用户当前语境确定 item、meaning、pos、source_article、source_sentence 和 user_evidence。
2. 缺 source_sentence 时返回 needs_context，停止生成。
3. 缺少用户当前措辞，且也没有 unknown、mistranslated 或 missed 类明确错词证据时，返回 needs_user_evidence，停止生成。candidate 标记本身不等于明确错词证据。
4. 先读 bank/mastered_items.csv；命中时排除并说明该词已证明掌握。
5. 运行只读 selector 获取地基包。不要手工拼接静态文件绕过 selector。
6. selector 返回 needs_reference_graph 时：
   - 重建派生图；
   - 运行 builder 的 verify-only；
   - 重跑 selector 一次。
7. selector 成功后，检查作文句型、作文词组、大纲 occurrence 和可选 SP 的审核状态与来源。
8. 生成 12 至 28 个英文词的自然句。
9. 检查目标词义和词性、作文句型结构、作文词组、大纲 occurrence、可选 SP 以及整体自然度。
10. 第一组地基不自然时，可以换一个不同的合规地基包再试一次。
11. 第二组仍不自然或证据仍不足时，输出待审核参考缺口并停止。

## Selector call

最小字段：

- item
- meaning
- pos
- source_article
- source_sentence
- user_wording
- user_evidence
- mode

推荐调用：

    python3 scripts/select_bbdc_foundation.py       --item "目标词" --meaning "本次词义" --pos noun       --source-article "真实来源" --source-sentence "真实来源句"       --user-wording "用户当前措辞" --user-evidence unknown       --mode argumentative

图谱重建与验证：

    python3 scripts/build_writing_vocabulary_relationship_graph.py
    python3 scripts/build_writing_vocabulary_relationship_graph.py --verify-only

## Output

成功时输出：

- 用户措辞或错词依据
- 采用作文句型：来源 ID、结构名、审核状态
- 作文词组依据：来源 ID、item、审核状态
- 大纲词汇依据：occurrence ID、item、核验状态
- 可选句式卡：SP ID 与骨架，或无
- 双重命中：是或否，并注明 item
- 例句
- 例句中文
- 结构拆解
- 约束检查
- 自然度检查：通过

失败时只输出：

- 状态：needs_context、needs_user_evidence、needs_reference_graph 或待审核参考缺口
- 缺少或失败的证据
- 已执行的只读验证
- 未生成最终例句
- 正式数据未修改

## Formal-data boundary

- bank/master_bank.csv 保持 13 列，不为参考 ID 增加字段。
- 真实 source_sentence 与系统生成例句严格分开。
- selector、关系图和例句生成不得自动更新 master_bank、mastered_items、sentence_patterns、appear_count、last_seen 或 review。
- 参考 ID 可以留在文章候选、精读记录或 review_note 短标记中，但只有相应正式任务获得授权后才能写入。
- 大纲参考词、作文候选和主题词不自动代表用户不会。

## Stop rules

- 缺 source_sentence：返回 needs_context 并停止。
- 缺用户当前措辞和明确错词证据：返回 needs_user_evidence 并停止。
- 图谱重建或 verify-only 失败：报告 needs_reference_graph 并停止。
- 只能找到未审核或 rejected 证据：报告待审核参考缺口并停止。
- 两个不同地基包都无法形成自然句：报告待审核参考缺口并停止。
- 引用 ID 无法追溯：删除该候选，不补造。
- 核心证据齐全且自然度通过后停止，不为增加装饰性例子继续检索。

## Validation

至少检查：

- selector verify-only 通过。
- 图谱输入和 manifest 未过期。
- 每个引用节点状态合规且可追溯。
- 生成句长度、词义、词性、结构和搭配通过。
- 正式 bank、句式卡和 review 在任务前后哈希不变。
