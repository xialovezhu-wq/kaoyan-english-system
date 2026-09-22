# 视频与音频 Ingest 契约

## Goal

把视频、播客、字幕、NotebookLM 导出或用户视频笔记保存为可追溯的 raw 与 wiki 资产，并把词句保留为候选。除非用户明确要求正式入库，否则不写 bank 或 review。

## Success criteria

- 原始来源、平台、标题、source_id、raw_path 和时间戳状态可追溯。
- wiki 页面链接到 raw。
- 主题、表达、句型、长难句和写作素材保持候选状态。
- NotebookLM 总结与原始证据分开。
- 缺时间戳或来源时显式标记，不编造。
- 当前任务的结构和来源验证通过。

## Inputs

只读取：

- prompts/codex_prompt.md
- schema/schema.md
- wiki/workflows/ingest.md
- raw/index.md
- wiki/video/_template.md
- 当前来源文件或用户文本
- 可能重复的 video 或 audio 页面

只有明确正式入库时才读取 bank 和正式写入模板。

## Workflow

1. 判断来源类型：video、audio、subtitle、NotebookLM 或用户笔记。
2. 提取标题、平台、原始链接、发布日期、时间戳范围和材料完整度。
3. 搜索重复来源或已有 source_id。
4. 保存 raw：
   - RAW-VIDEO-YYYYMMDD-NNN
   - RAW-AUDIO-YYYYMMDD-NNN
   - RAW-NBLM-YYYYMMDD-NNN
5. 缺标题、平台或时间戳时写待补充或 VISUAL_PENDING。
6. 用 wiki/video/_template.md 创建或更新目标 wiki。
7. 填写原始片段索引、主题与语篇结构、候选表达、候选句型或长难句、候选写作素材和待复听项。
8. 候选先留在 wiki，不自动进入正式 CSV。
9. 运行 source_id、raw_path、时间戳、反向链接和候选状态检查。
10. 输出回执并停止。

## Candidate rules

高价值候选信号：

- 影响理解或主题判断。
- 熟词僻义、固定搭配或长难句结构。
- 可迁移到考研写作。
- 用户明确不会、误译或反复卡住。

保持候选或不建议长期化：

- 普通背景词、专名或低价值口语。
- 缺真实来源句或时间戳。
- NotebookLM 总结中无法回溯到原材料的表达。

“高价值”不等于正式写入授权。

## Formal entry handoff

用户明确要求正式入库时，转正式入库流程，并满足：

- source_article 使用目标 video wiki 文件名。
- source_sentence 使用带时间戳的真实片段。
- 无时间戳时停止正式写入。
- master_bank 保持 13 列和受控枚举。
- 先排除 mastered items 并查重。
- 写后运行 CSV 和来源检查。
- 只有明确句式入库才写 sentence_patterns。

## Output

回执包含：

- 本次来源类型
- source_id 与 raw_path
- wiki 路径
- 候选摘要
- 正式数据是否未修改
- 已运行验证
- 缺失字段
- 若需正式入库的下一流程

## Stop rules

- 来源不完整但可安全保存时标记待补充后继续。
- 无法区分重复来源时只询问最小字段。
- 无时间戳时不把表达写入正式 CSV。
- NotebookLM 内容无法回溯时停在 raw 或候选层。
- 正式入库未授权时停在候选层。
- 验证失败时报告失败项，不宣称完成。
