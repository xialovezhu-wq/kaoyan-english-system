---
name: kaoyan-english-web-return
description: 本机审阅用户提供的英语网页回包；获准正式入库时沿原批次、唯一写者和回执收尾。
---

# 接收英语网页回包

对用户提供的现成回包，保留原 ZIP、来源版本、精确包集合和原学习日。网页建议交当前本地作者审核。仅在用户授权正式入库时提交对应动作。常规正式入库沿英语每日编纂入口。

1. 运行 `python3 scripts/english_web_workflow.py prepare-return --zip RETURN.zip`。按[回包审阅与执行](references/return-workflow.md)核对原始哈希、对话、附件、证据角色和本机正式版本。分别记录 accepted、modified、rejected、needs_user 或 already_formal。网页预筛只辅助定位。
2. 完成审核后用同一入口的 `approve-return` 封存审阅和补充证据。获准正式提交后按原学习日、review_id 顺序 preflight，再运行 `run-day --apply --authorization REVIEW_ID`。复用现有锁、预哈希、支撑预检和唯一 writer。多个分片依次办理，正式版本变化时重核受影响的决定。
3. 各日按原事务验回 writer、句子支撑、展示、点摘要、索引、本机读回和 T9 归档。已有终态只续欠项。回报正式 ID、回执、未决项及缺失组件。已获准正式入库的摘要与反馈沿[每日正式入口](../kaoyan-english-daily-intake-curation/SKILL.md)。有资料辨识疑义时才使用[资料提取](../kaoyan-english-daily-intake-curation/references/material-preprocessing.md)。
