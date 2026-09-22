---
name: kaoyan-english-intensive-reading
description: 根据英语当前原句和真实作答定位断点，按单词、句型、选项类型和错误类型哈希命中本地个人摘要后讲解；用户要求保存时才封完整学习单元，摘要只在正式入库时更新。
---

# 英语精读

保留用户实际选中的 DeepSeek V4.1 Flash 及其供应商和推理档位，不换模型、不另开任务。普通学习问答以提交到完整必要正文可见 10–20 秒为目标，超过 20 秒未达标；未经真实回合计时不得称达标。进度提示、首字或本地脚本耗时不能代替完整回复。

## 每轮先诊断，再按点读取

先依据当前英文、用户原样初译/选项理由和必要上下文判断：主谓宾/主干、从句边界、修饰范围是否准确，还是词义、搭配或中文表达问题。保留已正确部分，不从历史标签倒推本次错因。

每个实质学习回复都把当前涉及的具体单词、句型、选项类型和已观察到的错误类型转成精确键，在一次命令中查询。当前没有显示某种错误时，不添加那个错误类型。命中就必须读取返回摘要并用于当轮讲解；未命中立即停止查找，直接按当前证据回复。

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-english/scripts/english_learning_pipeline.py query-personal-summary --key 'word:advertiser' --key 'error_type:lookalike' --max-bytes 8192
```

支持 `word:`、`phrase:`、`syntax:`、`option_type:`、`error_type:`。单词用原形，词组保持原词组；已存别名由小型哈希路由自动归并。常用机制键：`syntax:subject-predicate`、`syntax:what-clause`、`syntax:help-object-do`、`error_type:lookalike`、`error_type:invented-causation`、`error_type:contrast-overgeneralization`、`option_type:relation-splicing`。通常传 1–4 个当前点，最多 8 个；一个词未收录仍可独立命中已确认的错误类型。

该命令只读一个小索引和命中的摘要文件，不读全部历史、正式词库、文章全文或原始 Capture。`misses` 不是调用其他搜索的理由；`unavailable` 也直接降级，不重试、不换别名试探、不调用 query-learning-context。只有明确正式维护或历史核查任务才走完整历史入口。

摘要记录最初误解/解法、已讲纠正和用户真实后续表现；据此调整这一处讲解与下一步检查，不展示整份历史。摘要没有独立掌握证据时，不把提示后理解写成独立会做。命中摘要不授权泄漏当前题答案。`--question-review` 仅用于用户明确开放该题答案/解析的复盘。

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

使用 `scripts/capture_current_english.py` 一次读取原始会话并封独立单元、输出 ZIP；缺少起点只执行一次 prepare。讲解中保留本单元起点及已知来源，保存时不查源码/help/历史、不手抄 JSON、不取 previous token、不修旧 frontier。程序保持 `source.review_route="web"`，同单元重试复用原包，后到补充只封新消息并关联旧包。

拿到成功回执和 ZIP 即结束，不逐文件复查、不单独 segment-zip、不刷新看板。失败保留原始证据和精确边界，按缺项重试本单元；旧包故障不阻塞新单元或教学。以真实保存请求到完整成功回复 10–20 秒为目标，未经新回合验证不宣称已达标。

Capture 只记录事实，`formal_write_count=0`、`background_processing=none`。快速入库绝不生成或更新个性化摘要。摘要在正式入库核实真实轨迹后按 [按点摘要合同](references/point-personal-summary.md) 更新；正式维护可从已有正式记录初始化，不吸收未正式入库的 Capture。

## 其他入口

明确正式入库转 kaoyan-english-daily-intake-curation；网页返回转 kaoyan-english-web-return。整篇学习结束需要网页整理时，先按授权封存尚未保存的完整单元，再转 kaoyan-english-web-bundle。导出词表按 kaoyan-english-vocab-export 的正式依据执行。

核对用户当前完整作答确实正确时，已可用的官方 Codex confetti 工具可按既有偏好静默调用一次；不重复探测，不为庆祝创建 Capture，不等同掌握。
