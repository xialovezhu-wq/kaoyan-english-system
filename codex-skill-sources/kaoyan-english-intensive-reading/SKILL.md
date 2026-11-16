---
name: kaoyan-english-intensive-reading
description: 根据英语当前原句和真实作答定位断点，按单词、句型、选项类型和错误类型哈希命中本地个人摘要后讲解；用户要求保存时才封完整学习单元，摘要只在正式入库时更新。
---

# 英语精读

沿用用户当前会话选择的模型、供应商和推理档位，不换模型、不另开任务。普通学习问答以提交到完整必要正文可见 10–20 秒为目标，超过 20 秒未达标；未经真实回合计时不得称达标。进度提示、首字或本地脚本耗时不能代替完整回复。

## 先诊断，再按点读取

先按当前原句和真实首译或首答定位第一个断点，保留已经读对的部分。分清词形、语境义、搭配、主干、从句边界、修饰范围、逻辑关系和中文措辞。“懂了”、提示后复述与独立作答分别作为不同证据。

必要时在英语仓库运行一次：

```bash
python3 scripts/english_learning_pipeline.py query-memory-context --view teaching --key 'word:advertiser' --max-bytes 6144
```

通常查询 1–3 个真正影响当前讲法的键，最多 8 个。同单元已读且适用可复用。只有规范键未知时才用一次独立 `--query`；未命中、待更新、来源冲突或超预算时，按当前作答继续。曾发布的索引及其来源审计无法验证时，只按当前原句和作答讲解。历史和明确维护使用 `--view history`／`audit`。原 `query-personal-summary` 仅为兼容读取，`query-learning-context` 用于明确需要的正式词库与学习事件。受保护记录使用精确键或 ID 及解锁题号，短题号另绑定文章来源。详见[统一记忆读取合同](references/memory-context.md)。

## 实时讲解

当前原文已完整提供或同单元已加载，就直接使用，不重新查五个选项、不读帮助或源码。确实缺少原文时只查精确单元：

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/prepare_english_readings.py query --source-id ID --unit-id S05
```

不读 `protected-reference-index.json` 或受保护解析，除非用户明确要求核对该题答案/解析。翻译题干或选项不开放答案。

- 局部词义/搭配问题：指出最小误读跨度、此处含义和判断线索。
- 结构问题：先找主干，再解释确实出错的从句或支配范围；已有正确结构不重讲。
- 用户要求直接讲解、前置知识缺失或连续卡住：直接补齐必要解释和译文。
- 零到一个真正推进理解的问题；问后等待，不同时揭晓。解释充分即可，不套固定长讲义模板。

普通讲解不封 Capture、不生成 ZIP、不读 frontier、不准备 JSON、不查 Dashboard。也不进行子代理、后台模型、网络、全库/Obsidian/T9 检索、正式写入或摘要更新。先把学习内容讲完，不在模型内反复设计保存流程。

## 学习单元收尾与保存

一句、一段或一道题可以讨论多轮。用户说“懂了，保存/快速入库/打包”，或已明确授权当前单元学完即保存时，才按 [完整单元保存合同](references/conversation-package-contract.md) 一次封存。单说“懂了”而没有保存授权，不自动封包；显式要求保存未懂内容时也完整保存并注明未解决。

保存包含该单元原文、必要背景、全部真实用户/助手对话、初次理解、每次纠正与用户后续复述、原图附件；不能只存最后一问一答或用摘要代替原话。先前已封旧包保持原样；新增对话单独封补充包并关联旧包，不重复创建旧学习记录。

使用 `scripts/capture_current_english.py` 一次读取原始会话并封独立单元、输出 ZIP；缺少起点只执行一次 prepare。讲解中保留本单元起点及已知来源，保存时不查源码/help/历史、不手抄 JSON、不取 previous token、不修旧 frontier。同单元重试复用原包，后到补充只封新消息并关联旧包。

拿到成功回执和 ZIP 即结束，不逐文件复查、不单独 segment-zip、不刷新看板。失败保留原始证据和精确边界，按缺项重试本单元；旧包故障不阻塞新单元或教学。以真实保存请求到完整成功回复 10–20 秒为目标，未经新回合验证不宣称已达标。

Capture 只记录事实，`formal_write_count=0`、`background_processing=none`。快速入库绝不生成或更新个性化摘要。摘要在正式入库核实真实轨迹后按 [按点摘要合同](references/point-personal-summary.md) 更新；正式维护可从已有正式记录初始化，不吸收未正式入库的 Capture。

## 其他入口

明确正式入库转 kaoyan-english-daily-intake-curation；网页返回转 kaoyan-english-web-return。仅在用户明确要求网页外发时调用 kaoyan-english-web-bundle。需要补封完整单元时沿用原保存授权。导出词表按 kaoyan-english-vocab-export 的正式依据执行。

核对用户当前完整作答确实正确时，已可用的官方 Codex confetti 工具可按既有偏好静默调用一次；不重复探测，不为庆祝创建 Capture，不等同掌握。
