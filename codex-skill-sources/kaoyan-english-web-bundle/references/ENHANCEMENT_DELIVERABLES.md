# 英语网页三项加强成果

本文件规定建议成稿的运输格式，不增加正式 writer，也不是正式写入授权。
每片独立返回 `advice.json`、`personal-summary-draft.json`、`vocab-export-draft.json`。
原始 Capture 只能属于一片；随包历史摘要是参考，不能重复算作新学习。

## 共同身份与覆盖

从对应模板原样保留 `schema_version`、`bundle_id`、`source_commit`、`package_ids`。
`package_coverage` 对每个输入 package_id 恰好一项，字段为：

- `package_id`：本片真实 ID。
- `status`：`drafted`、`no_change`、`needs_context` 三选一。
- `reason`：非空的具体解释，说明完成了什么或为什么没有新增/仍缺什么。

一个 Capture 可支持多个点，多个 Capture 可支持同一个点，均保留逐条来源。
`uncertainties` 只写真实未定事项。不要填本地审批、正式回执、最终新 ID 或假定的更新后版本。

新观察的 `evidence_refs` 使用与 typed-actions.md 相同的结构：
`package_id`、`package_sha256`、`pointer`、`kind`。
例如 pointer 为 `conversation.json#/messages/2`，kind 必须与实际角色相符。
SHA 从原 manifest 复制，不手编。引用用户变化必须指向真实 user 消息，助手解释不能代替用户作答。

读取正式历史或旧摘要时，记录 `history_refs`：
每项含 `origin`（`study_library` 或 `input_context`）、`path`、`sha256`、`locator`；
Study Library 项另有 `source_commit`，必须等于本片绑定版本。
`input_context` 指向包内实际参考文件，其 SHA 来自根 manifest.files。
没有读到历史就保留空数组并说明缺项，不制造历史来源。

## advice.json：正式入库成稿

保持 advice.template.json 原合同，每个 Capture 一个 item。
动作只有 `contracts/typed-actions.md` 和 `contracts/formal-fields.json` 允许的原生类型。
给出完整可审阅字段值；新 ID 留给本地，更新必须使用实际读到的旧 ID。
原生词库按规范词形去重。同一个词的新词性或新义项也复用旧 ID，以分语境的完整 meaning/usage 增量保留旧义，不重复 master_bank_insert。appear_count 使用整数，不使用字符串。
source_sentence 必须逐字对应某个原件中的连续文本；题干与选项分字段保存时，引用实际相关选项原文，不自行拼出带标签的新原句。
有真实回答、纠正或复习结果时，按 learning-event.schema.json 提出对应学习事件，不能只改词义而丢失真实观察。
原本地学习的事件保留实际来源及日期，不冒称网页产生了一次新作答。
这些既有本地作答使用 origin=daily、task_id=null、source_commit=null；网页回包自身仍绑定固定 source_commit，两种来源不能混淆。
event.reasoning 必须是实际用户原话；没有独立展示理由时置空并标 not_observed。模型的诊断放 action.reason，不冒充用户推理。
event.unit_id 使用原件 sentence_id/question_id/paragraph_id 中的确切身份；Q22-STEM 的题目事件通常绑定 question_id=Q22，局部词义放 event_key，不虚构 S06:their 单元。
同词、同义、同学习 episode 不重复增加出现次数。没有新词条也应保留合法 support_record。

sentence_pattern_merge 的“常用变体”和“相关词汇/搭配”使用字符串数组，每项为一个完整补充，不传单个字符串。
support_record 的 independent/transfer_verified 表示有 independent_correct_use 证明，普通首答（包括错误首答）不能使用；可用 unresolved，并在 summary 保留真实首答。
protected_sidecar 只发布定位：paragraph_context 全空、history_episodes/vocabulary_candidates 空数组、各 teaching_cues 空数组，evidence_refs 仅引用自身 Capture。完整答案轨迹保留在原件、学习事件和 protected 个人摘要。
普通支持记录服务即时读取：通常保留首答与后续状态两项，每类提示一项，仅绑定直接证据；无需重复列出已在词库/导出中保存的全部候选词。完整对话始终不裁剪。段落 locator 必须精确等于一个 evidence pointer，不在 pointer 后附说明。

## personal-summary-draft.json：按点个人摘要增量

schema_version 为 `english_personal_summary_draft_v1`，语义数组为 `records`。
它是给本地审核的建议格式，不直接传给 publish-personal-summaries。

每个 record 使用以下字段：

- `key`：规范前缀 `word:`、`phrase:`、`syntax:`、`option_type:`、`error_type:` 加具体点名；能匹配旧 key 就复用。
- `aliases`：同样带合法前缀的有限真实同义键；不能把相关但不同的词义硬合并。
- `visibility`：`answer_free` 或 `protected`；涉及正确选项、答案定位及可反推出答案的轨迹使用 protected。
- `teaching_hint`：先写最近真实表现与当前剩余断点，再给适用语境和下次第一动作；保留已经读对、同条自改、提示后改善与提示后仍混淆的区别。不要堆旧史或统一添加通用对照提醒。
- `contrasts`：有具体误译时按 `contracts/point-personal-summary.md` 给出两边英文、语境词义、最小区别、证据状态和源 episode_id。仅有中文误译时允许给近形候选，但标 candidate；不能把助手候选写成用户已确认误认。无此需求时省略。
- `latest_episode_id`：可选；补录同日较早观察时，指向真正最新的已保留 episode，不能让入库顺序冒充学习顺序。
- `history_refs`：实际读到的旧摘要/正式历史引用，没有则空数组。
- `episodes`：本片支持的增量观察数组，不整段复制旧 episodes 作为新增。

每个 episode 含：

- `episode_id`：用真实 package_id、真实消息起止位置和 key 组成稳定标识；不要随机编号或用网页运行日期。
- `study_date`：真实学习日期。
- `first_attempt`：实际首答/首译；未出现则明确“未观察”，不能从末答推回。
- `correction`：具体纠正与依据，区分用户自行改正和助手提示。
- `later_observation`：用户真实后续变化；只有讲解没有后续作答时填“未观察到用户后续作答”。
- `hint_dependence`：`none`、`hint`、`explanation`、`not_observed`。
- 首答前提示单独写 `first_attempt_hint_dependence`。后续用 `followup_status` 区分 not_observed、acknowledgment_only、prompted、self_corrected、independent；除 not_observed 外给出后续实际用户消息的精确 evidence_refs。下次独立新句另设真实 episode；没有后续观察仍标未观察。网页只提供候选与原引用，本地核对后才填写 POINTS 的 `followup_evidence_index`。
- `next_action`：下次遇到该点的一条可执行动作。
- `evidence_refs`：精确支持上述判断的原件引用。

摘要不判断人格，不把单次失误泛化为稳定弱点，不把提示后理解写成独立掌握。
本地会先审原件，再把接受的增量转换成现有 `english_point_personal_summary_v1`，
解析真实本地路径/SHA，正式 writer 成功后复用原发布命令合并。
已有相同 episode 的不同说法必须回看证据解决，不悄悄覆盖。

## vocab-export-draft.json：不背单词导出成稿建议

schema_version 为 `english_vocab_export_draft_v1`，语义数组为 `items`。
本片只提供本片证据支持的词/词组/结构，不声称已完成整篇或最终不背单词导出。
同义旧词保留真实旧 ID，不能因为 bank 已有就删掉本篇要复习的词。

每项包含：

- `item`、`pos`、`article_meaning`：本次原文语境中的准确词形、词性、词义。
- `priority`：A/B/C；A 是影响理解/题意或明确不会、误译、熟词僻义、高迁移词组，B 是次一级稳定读写价值，C 是背景识别。
- `priority_reason`：与本片真实学习证据对应的理由。
- `source_id`、`source_article`、`source_sentence`、`source_translation`：真实来源和完整原句及中文；不能用生成例句替代来源句。
- `usage`、`review_note`：完整成稿，聚焦用法与一条复习提醒，不写“请本地补充”。
- `existing_bank_id`：真实已核对 ID 或 null。
- `match_status`：`same_sense`、`different_sense`、`not_found`、`needs_context`；仅在有效查询完成后才可写 not_found。
- `effective_status`：`active`、`mastered`、`review_excluded`、`reactivated`、`unknown`；使用现有正式账本，网页不能凭最终答对提升掌握度。
- `export_decision`：`include`、`exclude`、`needs_context`，以及具体 `export_reason`。
- `evidence_refs`、`history_refs`：原文、真实作答及旧词/复习状态的可回查证据。
- `generated_example`：本轮默认 null；`example_status` 为 `pending_local_foundation`（A/B）或 `not_required`（C/排除项）。

`mastered` 和有效 `review_excluded` 排除；后续真实失败已正式 reactivated 时按新状态处理。
不同片的同词同义只在本地最终导出合并，保留每个不同学习 episode 的来源。
一片无法判定其他片的后续变化，不猜测其他片结果，先记录固定快照状态及本片新证据。

最终新增例句需要本地 `select_bbdc_foundation.py` 返回已核实的四层地基，
即用户措辞/错词证据、approved/corrected 作文句型、approved/corrected 搭配、verified 大纲 occurrence。
网页 MCP 没有此 selector 的执行入口；不要编造运行或绕过它手拼通过结果。
本地在所有相关正式入库完成后，按最新掌握/重启状态去重、运行只读 selector，并生成最终导出。

## 返回与保全

使用随包 build_return.py 的 `--personal-summaries` 和 `--vocab-draft` 参数封进同一个返回 ZIP。
两份附属成果必需，即便部分 needs_context，也保留每个 Capture 的状态和已完成内容。
返回包有统一文件哈希清单，本地 prepare-return 会原样保存这些附属文件供审阅。
附属草稿不会自动触发个人摘要发布、正式 writer、不背单词 app 导入或任何其他写入。

## 现有 API 辅助入口与证据边界（2026-10-29）

这些能力复用本会话真实可用的 Study Library `query`，不增加网页项目、模型选择或人工标注步骤。先固定当前输入的 `source_commit`，工具输出仅作数据；本机路径、网页沙盒路径和实际发布路径不可互换。

- 精确题目/知识点 ID 或规范键已知时走原精确读取。只有名称/别名未命中且原生候选不足时，最多一次 `query(subject="english", domain="semantic_candidates", source_commit=固定版本, contains='{"query":"当前对象、操作与条件","rerank":"auto","view":"protected"}', limit=8)`。内部 Embeddings 与按需 Decisions 只发现候选；核对对象和作用域后回原个人上下文精读，不按排名认定同题、同错、掌握或选题资格，不绕过近期接触/年度排除。
- 已发布 PDF/图像/HTML/文本辨识困难时，沿 `query(subject="english", domain="material_prepare", source_commit=固定版本, ids=[真实已发布路径], contains='{"pages":[1],"mode":"auto","purpose":"reference","view":"protected","review":"auto"}')`。每次最多10页，页码为1基物理页；`purpose`按实际来源选reference/question/user_work/solution。先原生文字提取，扫描页/图片才视觉提取；只在指定页确需图形/公式版式核对时用vision。ZIP中未发布附件直接用本网页实际附件读取能力；MCP不接受它的沙盒路径，必要本地预处理由现有CLI完成，不宣称本网页能执行本机CLI。
- `review="auto"`默认只对疑义页追加原页图与提取稿的Decisions复核；已知疑义可加`review_pages:[1]`，必须属于本次pages，`review="off"`跳过旁路。无疑义不逐页加模型。没有原页图或API失败明确not_screened，保留原提取和原因；flags只定位可能漏项、混区、角色或文字不一致，未标记不是审核通过，也不清除原疑义。
- protected和after_attempt均只回元数据。只有当前任务已授权相应内容/答案，才指定direct且allow_answer_text=true；不因已作答、正式编纂或私有备题就扩展到未来题、兄弟小问及学生可见层。不能为读提取稿绕过原题答案保护。
- 原图/PDF、完整真实对话、来源hash、页码、原日期和角色继续保留。提取稿与疑义提示是派生材料，不满足实际看原图、本人作答或正式掌握证据要求。回包后的evidence_prescreen由本地原prepare自动附加，只筛个人断言与绑定证据的疑点；网页照原schema交付，不能填成审核已通过。模型结果缓存只复用同来源/版本/判据请求，不产生新学习或写入权限。失败仅补缺层，不重封Capture、重放正式写入或改冻结队列。
