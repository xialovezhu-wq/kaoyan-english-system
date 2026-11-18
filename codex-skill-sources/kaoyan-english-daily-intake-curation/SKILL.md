---
name: kaoyan-english-daily-intake-curation
description: 英语正式入库、获准的真实学习回流或旧待办处置；按原学习日完成本机核验及晚间反馈。
---

# 英语正式入库

收到“开始英语正式入库”或明确的日期、文章、包集合时，在当前英语会话沿本科唯一写者处理。默认覆盖截至上海当日、缺少可信终态的包，指定截止日、only-today 或包集合时按原范围。单次真实作答回流按[学习回流](references/learning-backflow-contract.md)限定该事件。仅要求查看时用只读参数。快存与日终编纂各有触发条件。

## 按原学习日编纂

1. 在英语项目运行 `python3 scripts/english_learning_pipeline.py plan-local-intake`。按[清单合同](references/backlog-gate-contract.md)核对逐日范围、已有终态和待恢复组件。空清单返回 NOOP。旧事件处置也在该合同中。
2. 每日按该清单的 local_review_id 和精确包 ID 运行 `freeze-nightly`。核读完整包、原始对话、附件与正式快照。以 `author-local-day` 编成逐包动作和句子支撑，作者判断与原始证据分别保留。[编纂与事务](references/nightly-curation-contract.md)给出 author、失败恢复和采用依据。[动作合同](references/typed-actions-contract.md)与[句子支撑](references/sentence-support-contract.md)适用于新提交。
3. 在同一 manifest 上完成 dry-run 和支撑预检，再按 batch_id 授权唯一 writer。沿原 `formal.lock`、预哈希与幂等回执提交一次。遇到超时先查原事务和 journal，已提交只续未完成的后置步骤。需要重新判定的包留为未决，继续处理独立项。

## 完成与读回

原 writer 回执、支撑、受影响个人摘要与索引、本机教学验回、展示和归档均闭合后，按[输出合同](references/output-contract.md)回报实际结果，交付[当天反馈](references/evening-feedback.md)。缺层按[归档与恢复](references/archive-contract.md)保留原包和具体欠项。仅在用户明确要求时发布远程副本。

点摘要按[既有证据字段](../kaoyan-english-intensive-reading/references/point-personal-summary.md)保留首译、提示、实际修订与未观察状态。材料辨识有疑义时再读[资料提取](references/material-preprocessing.md)。原始资料难以定位时用[机器路径](../kaoyan-english-intensive-reading/references/local-paths.md)。仅有当前任务的明确委派授权且确有需要时，才用[原生只读查询](references/native-luna-read-contract.md)。
