# 阅读完成后的可选短写

保留当前阅读的原文、本人初译、独立作答与逐题解锁。需要历史时沿现有精读合同只读 1–3 个相关点。本人要求直接讲解就直接讲。完成后最多提出一个目标，允许跳过或暂停，首次试用留 5–8 分钟。按既有到期或异题机会复看。选词和造句仍用原 selector、approved/corrected、掌握排除与自然度校验。

在现有作文资料页练习。内部命令为 `python3 scripts/english_writing.py micro start`，stdin 提交 task 和 reading_completed。先核当次学习确已结束，沿原有文章完成回执绑定文章；已完成的当前单元可由本人明确的结束消息作证。本人在同一文章会话明确结束时也可传 completion_message（link、message_id、sha256、quote），completion_id 取本次文章 attempt_id 与消息摘要的组合哈希，替代 completion_day 和回执哈希。当前窗口有旧稿时从 micro read 取 expected_revision。启动会复用现有作文布局，在横屏显示所选版本的只读原稿，在竖屏显示定位反馈。按回执核对两个窗口的显示结果。同题更新只刷新内容；另一作文会话占用布局时，原稿已保存，保留当前窗口的所有权。一次阅读只提一个训练，跳过即继续原计划。全局浏览器负责显示，原英语宿主负责保存。

三个 task_type 为 relation、evidence、genre。分别练清楚的关系、证据怎样支撑主张、面向指定读者完成交际任务。主张和证据的义项、方向、限定要逐条核原文。材料外的主张可以是应用文里的建议。为当前材料填写 task_id、task_version、rubric_version、task_genre（sentence、source_response、practical、letter、notice、chart 或 argument）、target_skill_id、target_description、prompt、context_id、context_basis、mode、task_relation、opportunity 和 opportunity_reason。模板提供局部标准、两种合格方向及反例，可用 qualified_variants 指定更具体的两种表达。换内容、目标或标准要留新版本。范例由教练在原授权范围编写。正式词库例句仍服从旧来源合同。

source_ref 包含 source_id、unit_id、source_hash、sentence_sha256、question_id（纯正文可空）、completion_day、completion_id 和 completion_sha256。沿现有文章页当前授权定位。开放任务在本人提交前只给正常题面。mode=open 时教练用 `memory context --phase open_assessment`，不显示历史目标。按练习语义核 previous_attempt，跨会话再给 previous_entity_key；照搬旧题或只换题号记录为旧情境。先登记合理机会和判断依据。surfaces 的 prompt、sidebar、adjacent 均保存实际 text、cue 和 support，指定形式为 form，提示表达功能为 function，核过无提示才为 none。forms/functions 可列本次目标的其他定向线索。缺来源为 unknown。observed_scope_complete 仅在当次可见渠道齐全时为 true。

提交后在同页保留只读首稿与每版自改。教练用 micro feedback，绑定 action_id、attempt_id、revision、target_skill_id、direction 和 anchor。anchor 包含 draft_id、draft_sha256、quote 和从零起的 Unicode 码点 offset。示例和局部替换可填 example 和 rewrite，用户按需打开，只有确认采纳才生成新稿。重试默认最多两次主动提示，再由本人决定暂停、直接听解或看例子。紧邻聊天给了答案帮助时以 micro exposure 记录原话、surface、support。同稿换词后仍核原来的目标。

micro assess 绑定 draft_id。review 含 reviewer、rubric_version、target_skill_id、criteria 和 language_findings。逐项标准给 status（met、not_met、unknown）、reason 及同版 anchor。语言观察另存 anchor、reason 和 blocks_meaning；意思无法辨认时局部结果待核。每次判断生成新事件。适用机会事后确需修正时 micro correct_opportunity 记录理由，micro report 保留修正前后的分母。合格分母含结果未知，只把确定达到目标计入分子。一次成功仅是一条观察。

课后由原 memory inspect / receive 全量收件，用 display_attempt_id 绑定本课。资料页实际提交和已见反馈各有 WRUI 原生身份。科代表用 memory action 根据本人实际消息审核，surface_reviews 可按原消息 ID 提交 source_sha256、cue、support、reason。对照原包和不可变资料页记录，只在证据齐全时确认迁移。求助前未提交的输入保留为草稿。先封存，沿原 typed actions 和唯一 writer 正式收尾后才能进入下次教学摘要。

关闭试点可在教练环境设置 KAOYAN_ENGLISH_MICRO_WRITING=off。原稿、历史投影与原复习队列继续按各自入口读取。
