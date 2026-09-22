# 英语系统数据与流程契约

## Goal

把原始证据、可学习页面、正式学习数据和操作规则分层保存，使每个结果可追溯、可验证，并避免查询或整理任务越权写入正式库。

## Success criteria

- 每项资料有明确层级和来源。
- 候选状态不会被误当成正式事实或已掌握状态。
- practice-safe 页面与受保护答案层保持隔离。
- 正式 bank、句式卡和 review 写入都能追溯到用户授权。
- 派生图、索引和缓存可以重建，且不会反向污染正式来源。
- 当前任务完成后运行对应验证并在证据不足时停止。

## Rule ownership

- prompts/codex_prompt.md 负责跨任务角色、协作方式、权限、路由、聊天输出和停止规则。
- 本文件负责层级、路径和稳定数据边界。
- schema/protected_exam_analysis.md 负责答案存储、读取和解锁。
- schema/reference_grounded_examples.md 负责系统生成例句的四层地基。
- wiki/workflows/ingest.md、query.md、lint.md 分别负责具体操作。
- 专项文件在自己的领域内拥有细节；冲突时对答案披露、来源和正式写入采用更保守的规则。

历史 handoff、validation、raw 包内旧提示和统计快照只作证据，不覆盖现行契约。

## Layer ownership

| 层级 | 路径 | 负责内容 | 边界 |
|---|---|---|---|
| raw sources | raw/ | 原文、字幕、网页清洗、NotebookLM 导出、用户原始摘录 | 保留原貌；不精翻、不替代正式判断 |
| protected evidence | raw/protected/ | 答案、出版方解析、逐页 OCR 和题号索引 | 不进入 practice-safe 页；按解锁条件最小读取 |
| reference and derived | raw/reference_sources/；raw/reference_relations/；raw/articles/exam-reading-corpus/ | PDF manifest、OCR、真题结构化语料、派生关系图 | 候选与词元重叠不等于审核结论 |
| wiki and articles | articles/；wiki/ | practice-safe 文章、精读记录、候选、主题和操作记录 | 候选不等于正式 bank；既有页面不批量覆盖 |
| intake events and projections | intake/events/；intake/segment-frontier/；intake/backlog/；intake/views/；intake/candidates/；intake/nightly/；intake/sentence-support/；intake/receipts/ | 不可变逐句事实、segment continuation 真源、canonical backlog plan/global gate、快速文档、冻结批次、恢复事务、逐句教学支持投影与 receipts | JSONL、完整 package 和 immutable plan 是真源；sentence-support/current/index.json 与 Markdown 是可重建投影；默认 backlog run 只读 |
| formal study data | bank/；review/ | 长期库、掌握排除池、句式卡和复习状态 | 只在对应正式任务中写入 |
| schema | schema/；prompts/；wiki/workflows/ | 权限、路由、字段和验证规则 | 不存学习结论，不复制多份细节真源 |

## Stable paths

- articles/：文章学习页；自主练习页保持 practice-safe。
- bank/master_bank.csv：13 列长期结构化库。
- bank/mastered_items.csv：有主动正确使用证据的排除池。
- bank/sentence_patterns.md：SP 句式卡正式库。
- review/tomorrow_review.md：临时复习清单。
- review/daily_review_log.md：每日复盘日志。
- review/weekly_review.md：只在周复盘任务中更新。
- raw/protected/exam-reading-analysis/：历年阅读答案与解析受保护层。
- raw/reference_relations/writing-vocabulary-foundation/：可重建四层地基关系图。
- intake/packages/YYYY-MM-DD/PACKAGE_ID/：现行逐句完整会话、来源、附件、manifest 与 receipt 真源；本机正式前暂存。
- intake/events/YYYY-MM-DD/：历史 capture event 兼容证据；不可覆盖，不接收新学习，也不能补造成完整 package。
- intake/unit-captures/：当前独立学习单元的幂等绑定，只关联该次真实会话与起点、请求哈希和包号；新单元不依赖其他包或旧链。
- intake/segment-frontier/YYYY-MM-DD/THREAD_REF_SHA256.jsonl：历史 v2 的按线程 append-only continuation 真源；继续保留和兼容，不接管新独立单元。
- intake/segment-frontier/current.json：历史 v2 当前 token 投影；缺失只影响旧链恢复，不阻塞新独立包。
- intake/backlog/plans/CUTOFF_DATE/：canonical package backlog plan 与 plan SHA；默认覆盖所有不晚于 cutoff 的未可信正式终态 package。
- intake/backlog/gates/PLAN_ID/：completed、already_consumed、needs_user、failed、archive_pending 五类 global gate 快照；零 package 计划必须标记 NOOP，不得写成 COMPLETE。
- intake/legacy-package-evidence/YYYY-MM-DD/：仅用于 legacy event 显式迁移资格审计的权威完整会话证据 sidecar；普通 audit 不创建 sidecar 或 package。
- intake/views/YYYY-MM-DD/：按事件重建的快速入库 Markdown。
- intake/candidates/YYYY-MM-DD/：历史候选兼容投影；不接收新 package，不定义现行正式判断。
- intake/nightly/YYYY-MM-DD/：批次 manifest、Sol actions、提交日志与可恢复事务。
- intake/sentence-support/objects/：正式 writer closeout 后生成的内容寻址逐句教学支持记录；不是真源，不替代完整会话。
- intake/sentence-support/current/index.json：单一原子 generation root；精确查询只读取当前 PASS receipt、至多 16 代确定性 pointer chain 和一个不超过 12 KiB 的 record，不扫描 Obsidian、T9 或完整索引。
- intake/sentence-support/generations/：不可变 lookup generation；普通批次只发布 touched sentence/source pointer，chain 达到上限时才压实，避免每晚重建全库。
- intake/receipts/sentence-support-preflight/YYYY-MM-DD/：formal dry-run 之后、apply 之前的不可变预检回执，绑定 exact proposal SHA、action set、manifest、current generation、completed subset 与 12 KiB record/16 KiB query 预算；writer transaction 和 closeout 必须回绑其路径与 SHA。
- intake/receipts/sentence-support/YYYY-MM-DD/：support proposal、writer closeout、前后 index SHA 和逐 package 结果的刷新回执。
- intake/receipts/capture/YYYY-MM-DD/：快速捕获与文章完成 receipt。
- intake/receipts/nightly/YYYY-MM-DD/：夜间 dry-run 与正式写入 receipt。
- intake/receipts/recovery/YYYY-MM-DD/：中断事务恢复 receipt。

旧目录继续兼容。除非用户明确要求结构迁移，否则不重命名路径或字段。

## Authorization

默认只读正式学习数据的任务：

- 回答、解释、查询、诊断和计划。
- 普通 intake、候选筛选和词汇导出。
- selector、关系图构建或 verify-only。
- lint、Dashboard 汇总和第二大脑候选同步。

允许写入的任务：

- 当前 intake 可以写被点名的 raw、索引、practice-safe 文章页和候选区。
- 明确正式入库可以写被点名的 master_bank 条目。
- 明确句式入库可以写 sentence_patterns。
- 明确掌握维护且证据充分时可以写 mastered_items。
- 明确每日、明日或周复盘任务可以写对应 review 文件。
- “开始英语正式入库”以上海当前日为 cutoff；显式日期命令以该日为 cutoff。两者允许确定性 writer 逐日应用 plan 中通过门禁的安全 action，并复用 freeze-nightly、apply-nightly、refresh-sentence-support 和 archive-nightly，不跨日 freeze。歧义项保留 needs_user。run-backlog CLI 仍需显式 --apply --authorization PLAN_ID；默认调用和 --no-persist summary 都完全只读。
- 新正式批次只允许 english_nightly_manifest_v3；v3 必须绑定 postformal_contract_version、history scope，并要求 sentence_support closure。v2 只读兼容历史 closeout，不允许新 apply。package 只有在 writer closeout、sentence-support generation/refresh receipt、archive receipt、locator 重读、cleanup intent、archive pointer 与 cleanup receipt 全部验真后才能进入 completed 或 already_consumed。support 刷新失败保持 pending_component=support_refresh 并保留本地 package；恢复只补 support 后的链路，不重复 apply。
- 新 capture 必须显式记录 source_kind 与 answer_exposure。只有 article/user_provided、无 question_id 且 answer_exposure=answer_free 的 sentence 才能进入普通 query；缺失、题目/选项/解析或 protected exposure 一律成为不可查询 sidecar。
- sentence-support 只缓存 package-bound 教学证据与 formal/SP ID locator，不缓存正式行的 meaning、state 或内容哈希。查询或导出需要正式值时按精确 ID 读取当前正式数据，避免旧句持有陈旧的正式语义副本。

写入正式数据前先验证来源、去重、字段和当前授权。普通生词、候选价值、系统生成例句或查询结果本身不提供写入授权。

## Reference sources

- 历年阅读 practice-safe 入口：wiki/reading/历年真题阅读总索引.md。
- 受保护答案入口：raw/protected/exam-reading-analysis/index.md。
- 大纲词汇入口：wiki/vocabulary/大纲词汇参考库.md。
- 作文审核白名单：wiki/writing/作文造句可用白名单.md。
- 四层地基关系图说明：wiki/relationships/作文-大纲词-错词关系图谱.md。

参考 ID 证明来源，不替代 master_bank ID 或 SP ID。未审核 OCR、pending、rejected 和 lexical_candidate 只用于定位。

## Task routes

- 新材料进入系统：wiki/workflows/ingest.md。
- 查询表达、句型、旧词或写作素材：wiki/workflows/query.md。
- 检查结构、来源和边界：wiki/workflows/lint.md。
- 系统生成例句：schema/reference_grounded_examples.md。
- 历年答案与解析：schema/protected_exam_analysis.md。
- 视频与 NotebookLM：schema/video_ingest_template.md。
- 完整会话 package、夜间正式编纂与 sentence-support：wiki/workflows/english-async-intake.md。

只读取当前路线需要的文件，不把所有索引和模板作为每次任务的固定前置。

## Automation boundary

可以自动执行：

- raw 登记、网页清洗、字幕分段和索引链接。
- practice-safe 语料、OCR 参考层和派生关系图的可重建输出。
- 候选提取、只读 selector、lint 报告和验证。
- 不可变完整会话 package、文章结束 A/B/C 输出、sentence-support 与只读 Dashboard 投影。
- 正式提交后的确定性 sentence-support 增量刷新和只读精确查询；刷新不调用第二个模型、不写正式数据。

需要明确任务授权：

- master_bank、mastered_items、sentence_patterns 和 review 写入。
- 既有正式文章学习记录的修改。
- 重复词合并、目录迁移、字段重命名或历史删除。

Sol 和可选只读叶子没有正式库写权限。Sol 只提交类型化 formal action 与无写权限的 sentence-support proposal；确定性 writer 在精确批次授权、写前哈希、文件锁、提交日志和写后验证全部成立时执行正式写入。

## Stop and validation

- 来源字段缺失且会改变结果时，标为待确认并停止该写入。
- practice-safe 与受保护答案边界不清时，不写文章页答案，转 schema/protected_exam_analysis.md。
- 关系图或缓存过期时，重建并 verify-only；验证失败就停止。
- 正式写入未授权时，在候选结果处停止。
- 同一幂等键对应不同 payload、来源哈希漂移或正式文件写前哈希漂移时失败关闭，不覆盖旧事实，也不做部分无回执写入。
- writer 已提交但 sentence-support 刷新失败时，正式事实保持有效，状态为 FORMAL_COMMITTED_SUPPORT_PENDING；保留本地 package，只恢复 support，禁止重放 apply。
- 当前独立单元经过同父目录 staging 全包写入、重验、原子 rename、父目录 fsync 和 receipt v3 验证后即保存完成；同次命令输出原包 ZIP。重试只重开该单元，不读其他旧包。历史 v2 保留原 continuation 和 recover-segment-gate 恢复合同。
- 修改后运行当前层的最小结构、来源、哈希或零写入检查；未通过时不得声称完成。

## 当前学习状态与真实回流

`query-learning-context` 是实际讲解的有界正式读取入口：按现有 bank/SP ID、同科 concept key 或无歧义别名，每次重读当前正式值与已记录事件，返回 formal_version。`bank/learning_events.jsonl` 只有类型化 writer 在明确实际回流授权下创建；没有记录就保持不存在，不补造空学习历史。新纠正/复发不抹去旧独立证据，旧掌握也不覆盖更新的正式复发。正式提交后本地失效与锁外确定性发布分开恢复，云失败不重放 apply。

## DeepSeek 英语网页审阅入口

普通本地学习使用实际选中的 DeepSeek-V4.1-Flash。source.review_route=web 的原始 capture 与被显式打包的待处理包由现有正式入口保持等待，只有本地 GPT-6 已审核的网页返回可通过 web_review_id 和精确 action 哈希进入原 writer。历史本地路径不改。

所有未处理 capture 打包由 kaoyan-english-web-bundle 使用 scripts/english_web_workflow.py bundle 完成，默认跨日期、文章和任务；打包或上传不算处理完成。网页项目 E 独立于数学 A/B/C 与408 D。kaoyan-english-web-return 处理精确返回集合，保留学习原日和实际裁决时间；project-E adjudication 是修改建议的本地裁决，不是学习作答或掌握证据。正式完成后通过 scripts/english_vocab_handoff.py --source-id ID 交给新的 DeepSeek 导出会话。
