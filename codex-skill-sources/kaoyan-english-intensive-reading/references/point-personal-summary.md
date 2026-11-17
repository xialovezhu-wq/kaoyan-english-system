# 按点个性化摘要

实时读取走[统一记忆读取合同](memory-context.md)：当前诊断 → 精确对象或有界候选 → 相关摘要及有效性检查。`intake/personal-summary/current.json` 继续保存哈希到内容对象的指针，`objects/<sha256>.json` 保留简短教学字段和该点全部已审核 episode。键前缀为 word、phrase、syntax、option_type、error_type；有限别名在正式维护时建立，候选检索不直接新增别名或学习事实。

每个摘要包含 key、aliases、visibility、teaching_hint、episodes；有真实误译或词形辨析需求时增加 `contrasts`。每个 episode 包含稳定 episode_id、原始学习日期、first_attempt、correction、later_observation 和精确 evidence 路径/SHA，以及定位到该观察的 locator。无实际后续作答写“未观察”，不可把助手讲过写成用户已改正。完整原话保留在原证据中，摘要不替代 Capture，不成为第二套掌握度账本。

切分单位是能改变下一次讲解动作的最小断点：词形、语境义、搭配、主干、从句边界、修饰范围、上下文逻辑、中文表达。仅措辞不自然时不写成理解错，单一语境的误读不泛化为词汇能力不足。明确误认了哪个词才建立具体词对；只说不认识不能推断为形近误认。正式词库/句式卡有收录、仅出现于来源句或助手补充的知识，不等于用户犯过这个错。

每个点的 teaching_hint 概括本点真实变化与下一步，不按最新分包覆盖成只剩该包；较早证据补入时保留较新的纠正和复发。已正确的主干、局部提示后正确和独立作答分别记录。旧助手讲解本身有误时写明“本轮审查修正”，不能伪装成当时用户已经接受了正确讲解。证据不足的原因标未定。

新成稿在有原始观察时用 `first_attempt_hint_dependence` 标明用户首答前的提示暴露（none、hint、explanation、not_observed）。`followup_status` 分开标记后续：not_observed（没再次作答）、acknowledgment_only（只确认理解）、prompted（提示后作答）、self_corrected（无提示自改）、independent（确有无提示后续表现）。前两项的 `hint_dependence` 为 not_observed；prompted 对应 hint 或 explanation；后两项为 none。有后续状态时，除 not_observed 外都要用从零开始的 `followup_evidence_index` 指向本 episode 的 evidence 中某条 `/messages/N`（或其 content）原始用户消息。不要从末答反推首答；新句无提示迁移另保存真实新 episode，可有独立首答且没有后续作答。程序验证原句证据角色及明显的裸确认；是否曾提前获得答案、前后顺序是否合理、表现是否证明该能力，仍须对照完整原对话审阅。旧 episode 保留原格式，不补造标签。

日常增量只在正式入库后更新；用户明确要求从原始资料维护摘要时，使用下文的原始资料复审入口。普通讲解、快速入库与 ZIP 导出不调用发布命令。本地正式审阅者依据完整真实对话形成该批相关点的摘要，正式 writer 成功后使用其可验证回执发布：

```text
python3 scripts/english_learning_pipeline.py publish-personal-summaries --input-json POINTS.json --writer-receipt WRITER_RECEIPT.json
```

POINTS.json 根对象是 `{"schema_version":"english_point_personal_summary_v1","records":[...]}`。发布前核验每个 evidence 的本地原件，写内容寻址对象，再原子更新索引。旧 episode 保留，分包只合并本批点；同 episode_id 的矛盾内容拒绝覆盖。正式收尾绑定本次个人摘要与记忆索引的版本和覆盖范围；任一步失败只补缺失阶段，不重放正式 apply。保留现行生命周期和受影响来源的审计；旧代际留作授权历史核查，不能代替现行失效门禁。没有相关变化时由原生收尾记录有依据的 no-op，不能用空稿伪造完成。

记忆层启用后的正式提交，POINTS 顶层 `coverage` 必须逐项说明本次 completed package 的摘要覆盖：每项为 `package_id`、`disposition`、`keys` 及必要的 `reason`。`covered` 的 keys 必须在本份 records 中明确发布；确无相关点才用 `no_relevant_points`、空 keys 和具体理由。只有经过正式回执核验的全无相关点覆盖，才允许 records 为空。needs_user、failed 或其他包不能借此宣告完成。

只有明确维护/初始化任务才可用 `--bootstrap-existing-formal`，从已经正式保存的 articles/bank 证据建立摘要，或细分维护开始时 current 已绑定并通过内容哈希验证的摘要对象。后一种 evidence 指向父对象，episode 必须保留 source_episode_id 和原 study_date；父对象保留其完整原始证据链，受保护父对象不能被降级。未被 current 引用的对象、本批新造对象、待入库 Capture 都不能成为 bootstrap 的依据；用户明确要求的原始资料复审须走独立的来源清单入口，不能伪造正式回执。

正式维护逐项对照正式词库、句式卡和实际学习块，记录已有点、新细点、仅知识条目以及证据冲突，不以摘要数量作为覆盖证明。新点补上有限、无歧义的别名；讲解仍只做一次有界查询。12 KiB 分别约束非 episode 的教学元数据和单条新 episode，不再约束完整轨迹总长。长点保持同一键与全部 episode，current 路由另绑定教学头哈希；普通查询由当前 SQLite 代际读取教学头、最新合适观察及精确定位，不展开旧正文。语义确实不同才拆点；原话过长留在原件并给精确定位。

普通记录使用 answer_free，只保留不会泄漏具体题答案的词义、结构与通用错误机制；具体选项、答案定位、正确判断或可反推出答案的轨迹使用 protected。审核所有实际字段、对照、来源、别名及输出图中的数据；程序会拒绝已识别的错误标签，具体题目绑定的选项轨迹在投影中须经逐题解锁。自动信号检查不能代替语义审阅。更新后的讲义以“最初怎么想 → 本次核实的纠正 → 用户真实后续变化 → 下次第一动作”组织，保留提示依赖和未定项。

## 可直接用于讲解的对照

`contrasts` 是教学投影，不是新学习事件。每项必含 `target`、`target_meaning`、`other`、`other_meaning`、`status`、`distinction`、`evidence_note`、`source_episode_ids`。两侧意义按原语境和词性写；distinction 只说最小有效区别（关键字母、词性、搭配或动作方向），不能只列两条词典义。每点最多四组，讲解时按当前断点选最相关的一至两组。

status 的证据含义：confirmed＝记录明确给出替代词形；candidate＝只有中文误译或语境义证据，另一个英文词尚未确认；recorded_form_uncertain＝词形出现差异但误认、口误、转写等原因未定；sense＝同一个英文词的义项／词性比较。sense 的两边英文必须相同。原记录只说“不认识”或整句不懂，不足以补造任意词对。语义合理的候选可以展示，但不升级成已证实错因。

source_episode_ids 必须指向本点真实保留的 episode；新语境不能引用不相关的旧误认证明复发。未来正式入库应回看此前该点轨迹，补齐对应 contrasts 与 teaching_hint；已有组若只是当前没有再出现，应保留，不由最新分包覆盖。程序在未提供 contrasts 时保留旧值，显式提供则代表经过审核的完整对照集合。

## 明确维护已有摘要

正式维护可只调整摘要投影，不添加 episode：在记录中提供 `key`、`base_summary_sha256`（维护开始时 current 对应的准确哈希），以及需要更新的 `teaching_hint`、`aliases`、`contrasts`、`latest_episode_id`，使用 `--bootstrap-existing-formal` 发布。该模式禁止修改 episodes、日期、visibility 或学习状态；当前哈希漂移就拒绝。旧摘要对象、原始 evidence 和所有学习 episode 原样保留。它解决的是已正式记录的教学表达与检索问题，不把这次维护计作复习或掌握。

需要新细分点时继续使用上文父摘要证据链，保持 source_episode_id、study_date 和答案保护；新 episode 只标细分来源，不伪造新作答。可变 articles/bank 的整文件哈希若因后续追加而变化，记录该漂移并按具体条目核查；不得伪称旧证据哈希仍与当前文件相同。


## 实时读取与真正的后续变化

普通讲解用 `query-memory-context --view teaching`，保留完整教学状态、真实观察、适用语境、提示依赖和覆盖限制，不全量展开 episodes。明确跨文章或时间比较用 `--view history`；明确维护用 `--view audit`。`--as-of` 是学习发生时间截止，不能把较晚导入旧材料当成新学习；历史视图不能用当前 teaching_hint 冒充过去的判断。旧 `query-personal-summary --view full` 仍是兼容读取能力，不绕开统一入口的失效和授权检查。

同单元同上下文可复用已读键；新键、已知正式更新或用户要求刷新再查。Skill 不每轮强制读文件。

teaching_hint 优先写最近能改变讲解的事实：已经读对什么、仍错在哪、对应语境、下一步第一动作。长历史只留会改变当前动作的转折；没有后续独立复测，不等于可以忽略已经出现的局部正确或同条自改。不要把统一“下次并排比较”等流程话塞进每个点。

补录早先遗漏证据时保持原日期，标 `observation_status=historical_evidence_backfill_not_new_learning`。只按原发生时间及其精度比较观察，同日只有日期、时间相同或时间缺失时保留可能最新的并列组。明确知道同一原对话中的先后时，`latest_episode_id` 可指向保留的较晚观察，程序核对其准确原话、回合定位和原件 SHA 再采用。撤回已被证据否定的旧观察继续走既有定点入口。

多义词的对照可加 applies_when，明确对应当前搭配或错法；它不证明本次已发生错误。sense 的 other_meaning 是用户当时的误译，不保证是该词的合法义项；展示时标“你当时的误译”。


## 派生判断、更正与依赖

既有日期、evidence、source_episode_id、提示依赖和对照状态继续复用。原教学提示缺少逐结论证据分类时保留 `legacy_unclassified`；全量结构检查不等于逐条语义复审，也不能把旧助手文本自动转成已确认用户事实。只对明确冲突、失效或评测失败的条目重新核读原件。

新的派生判断要标明它是直接观察还是推断，并引用实际 episode；推断保持候选性质，不用自报 confidence 代替证据。若需撤回或替代旧判断，必须指向具体旧判断／episode并保留更正依据，不能把一般语言纠偏、复发或新一次答题自动解释成旧事件撤回。

有真实需要时，在同一 POINTS 中使用原生可选字段；不要为每个词机械生成判断：

- `memory_claims`：每项含稳定 `claim_id`、`point_key`、`content`、`basis`、`status`、`evidence_episode_ids`，可含 `valid_from`／`valid_to`。basis 为 user_explicit、observed 或 inference；status 为 candidate 或 confirmed，inference 不能使用 confirmed。证据必须属于该点并能回到真实用户原件，时间依据不明则省略。
- `memory_corrections`：每项含稳定 `correction_id`、`action`、`reason`、`evidence_episode_ids`，并通过 `target_claim_ids`／`target_episode_ids` 指向实际旧对象。action 为 retract 或 supersede；supersede 还需 `replacement_claim_id`，retract 不带替代项。替代结论不能依赖正在撤回的 episode。

supersede 若直接或经 episode 依赖使 confirmed 判断失效，替代项须为 confirmed 且有 observed 或 user_explicit 依据。证据只支持 candidate 时可单独保留候选，确认项保持有效。独立有据的 retract 继续沿原依赖生效，重试沿用原有回执。

这两类字段只接受已验证的正式回执或明确原始资料复审依据，bootstrap 不授予新的判断／更正权限。同 ID 不同内容失败关闭；重新措辞也不能悄悄覆盖已发布判断。旧 episode 撤回的是作为派生判断依据的有效性，原始学习记录仍保留。

来源／事件到当前摘要的反向依赖由确定性投影建立。更正后先停止使用受影响的旧结论，再更新相关摘要、受管理知识点页面与索引；不删除原始事件、旧摘要对象或原日期。手写正文不属于受管理更新范围，检索相似也不直接产生跨文章“同一种错误”的关系。

## 记忆收尾与维护命令

查询与快速保存不运行维护命令。已授权正式或摘要维护任务使用同一 `english_learning_pipeline.py`：

- `memory-status` 只读当前 generation、后端与待完成组件。
- `build-memory-index --audit-only` 只做结构／来源检查，不发布、不启用、不改页面；不能与 `--enable` 或 `--publish-pages` 混用。
- `build-memory-index` 重建派生投影；`--key` 可限定已确认受影响的点。明确初始化／维护需要展示页时才加 `--publish-pages`，首次验收完成后用 `--enable` 启用新正式提交的记忆收尾。不在每次讲解时运行。
- 已知来源变化时用 `build-memory-index --source-path 原件路径 --audit-only` 定位全部已索引依赖并核对；接受维护时按同路径参数去掉 `--audit-only`。定位失败先核对原件身份；没有哈希等于没做实时内容验证。正式或原始资料复审后才修正语义，不因整文件追加自动定为学习错误。
- `recover-memory-closeout --writer-receipt WRITER_RECEIPT.json` 只恢复该正式回执绑定的缺失记忆阶段，不重放 writer。personal_summary 仍缺语义成稿时，当前作者须按原证据补齐并调用 publish-personal-summaries；恢复命令不会自动编造摘要。已发布摘要后的 memory_index 失败则只补索引。

原生返回 `FORMAL_COMMITTED_MEMORY_PENDING` 时保留 personal_summary／memory_index 的具体原因与已完成范围。只在摘要覆盖、索引版本和原 writer 绑定都验回后报告教学记忆就绪；快照 PUBLISHED 或 build 命令成功不能单独证明正式包已全部覆盖。

## 用户明确要求从原始包重新审阅

这是摘要维护，不是正式入库。先盘点原始Capture、正式处理包、旧原话记录与附件；按原件内容哈希、原始消息ID/时间和会话身份核对副本。包数、相同文本次数、加工文稿数量不等于独立学习样本。先完整阅读真实用户—助手轨迹，再读正式成稿与当前摘要比较；不得只搜索几个词或只读旧摘要来声称全量审阅。

逐份记录实际读过的范围和未读项。正式文稿只能辅助审核，不单独证明用户作答或掌握；旧助手错误要单列修正，不能算用户已经接受。原始快速记录即使尚未正式入库，也可在本次明确授权范围内作为观察事实，但不因此写bank、判分、更新复习或改变原包状态。

使用：

```text
python3 scripts/english_learning_pipeline.py publish-personal-summaries --input-json POINTS.json --source-review-manifest REVIEW.json
```

REVIEW.json 的 schema_version 为 english_summary_source_review_v1，purpose 为 explicit_summary_maintenance，summary_input_sha256 绑定 POINTS 的规范JSON哈希。sources 逐文件列 path、sha256、kind、locators 与可核对的原 study_dates；kind 区分 learner_dialogue、learner_record、assistant_explanation、formal_advice。locator 使用精确JSON指针（如 /messages/3）或 lines/起行-止行。可带逐字quote，发布时核对该定位中的原文。

每个新增摘要观察须有原始用户证据。资料可用 `source_text` 作为辅助来源。用户消息含转贴块时，须用准确原话定位该观察对应的用户自写作答。保留原学习日期，历史复审按原身份记录。已有点可用 expected_summary_sha256 绑定审阅前版本，漂移则重核该点。原有 episodes、来源与答案保护继续保留。

教学成稿应让讲解者立刻决定：本句哪个最小点需要介入、哪些正确部分不用重教、历史末次改变了什么、先展示哪一组对照或结构、什么仍未验证。已有改善不能被“未做独立复测”抹去；旧误译也不能因为用户说理解了就自动关闭。
