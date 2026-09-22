---
name: kaoyan-english-reading-intake
description: "用户明确要求保存英语原句、段落、题目或阅读资料时，一次命令封存完整真实对话和原始附件。独立学习单元不依赖旧续传链；不调用模型、不评分、不更新个人摘要或正式库。"
---

# 英语完整快速保存

复用当前已知的来源、句段题身份和起始消息位置，按 [独立学习单元保存合同](references/conversation-package-contract.md) 一次保存。正常路径不重读全套 schema、来源合同、help 或源码；缺少消息起点时只做一次 prepare，然后保存。

沿用当前模型和供应商。以用户请求到完整成功回复 10–20 秒为目标；必须通过新的真实回合验证，不能拿脚本耗时宣称端到端达标。

## 保存范围

用户明确要求保存或已授权当前单元结束即保存时，收齐本单元原文、必要上下文、全部真实用户/助手对话和所有可用附件。包括最初理解、每次纠正、后续复述和未解决问题；不改写原话，不只保留末轮。普通教学不自动封包。

真实 session 从运行环境和原始日志取得，不能用上下文窗口 ID。已知完整参数直接调用 scripts/capture_current_english.py；它读取原文、计算哈希与时间、写入独立 package 并输出 ZIP，不由模型手填 JSON 或接续令牌。

已准备的文章按 source-id 与 unit-id 精确绑定，保留已知 source-hash 校验。首次导入未准备的原始资料才按需读 [来源合同](references/pipeline-handoff-contract.md)；缺少非破坏性元数据写 missing_fields，不阻止证据保存。不得为提速省略原图或校验。

## 保护与后续

原文、题干、选项和解析按显式 source_kind、answer_exposure 分开。正文为 article，仓外普通句为 user_provided；题干/选项不转成普通正文，答案和解析用 explanation/protected。提供出版方答案资料时按需读 schema/protected_exam_analysis.md。

旧包保持原字节，后到补充只封新消息并关联原包。新单元不需要上一包成功，不读 frontier 或历史归档。相同单元相同证据返回已有包，不生成第二份学习记录。

成功只认本次 created/idempotent_noop、ready 和真实 ZIP；给简短成功说明与链接即结束。不逐文件重复读回，不手工二次打包，不刷新看板，不做整日审计。失败按明确缺项重试同一单元；ZIP 失败复用已封原包，普通教学可继续。

formal_write_count=0、background_processing=none、model_call_count=0、mcp_call_count=0。快速保存不写 bank、学习事件、调度、个人摘要或正式题卡。正式入库转 daily-intake-curation，网页返回转 web-return，整日未处理包转 web-bundle，教学转 intensive-reading。

临时包或附件绝对路径只属于证据容器，不写入正式 Obsidian 页面。正式入库后才发布展示素材与归档。摘要只在正式入库后更新。
