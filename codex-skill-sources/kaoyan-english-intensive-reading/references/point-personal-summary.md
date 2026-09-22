# 按点个性化摘要

实时路径是当前诊断 → 精确键哈希 → 小型路由表 → 命中摘要。`intake/personal-summary/current.json` 仅存哈希到内容对象的指针，`objects/<sha256>.json` 存该点的简短语义摘要。键前缀为 word、phrase、syntax、option_type、error_type；有限别名在正式维护时建立，实时不做模糊扩搜。

每个摘要包含 key、aliases、visibility、teaching_hint、episodes。每个 episode 包含稳定 episode_id、原始学习日期、first_attempt、correction、later_observation 和精确 evidence 路径/SHA。无实际后续作答写“未观察”，不可把助手讲过写成用户已改正。完整原话保留在原证据中，摘要不替代 Capture，不成为第二套掌握度账本。

更新只发生于正式入库。普通讲解、快速入库与 ZIP 导出不调用发布命令。网页只提出建议；本地正式审阅者依据完整真实对话形成该批相关点的摘要，正式 writer 成功后使用其可验证回执发布：

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py publish-personal-summaries --input-json POINTS.json --writer-receipt WRITER_RECEIPT.json
```

POINTS.json 根对象是 `{"schema_version":"english_point_personal_summary_v1","records":[...]}`。发布前核验每个 evidence 的本地原件，写内容寻址对象，再原子更新索引。旧 episode 保留，分包只合并本批点；同 episode_id 的矛盾内容拒绝覆盖。摘要更新失败只补摘要，不重放正式 apply。

只有明确维护/初始化任务才可用 `--bootstrap-existing-formal`，且只从已经正式保存的 articles/bank 证据建立初始摘要；不得以此吸收待入库 Capture。

普通记录使用 answer_free，只保留不会泄漏具体题答案的词义、结构与通用错误机制；具体选项、答案定位、正确判断或可反推出答案的轨迹使用 protected。不得通过改标签把答案内容变成普通讲解材料。更新后的讲义以“最初怎么想 → 本次核实的纠正 → 用户真实后续变化 → 下次第一动作”组织，保留提示依赖和未定项。
