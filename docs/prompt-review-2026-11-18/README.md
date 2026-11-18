# 英语提示与首译后表达对照

本分支以 `504322fe946328ce70c1bd596886df7bb5ac4344` 为父版本。新增的 `expression-comparison` 位于原英语 CLI。输入一份已获准的文章句或当前用户提供的原句，根据学习阶段返回聊天文本和可复制的建议状态。建议的含义保留或变化由讲解者对照原句核定，程序核对来源版本、会话身份、引句及说话人。

合成验收使用 *Not every shortcut is reliable.*。首译前仅显示原句；首译后保留“每一种捷径都不可靠。”的措辞，并把“不能信赖每一条”和“一条都不能信赖”的范围差放在两行比较。用户的确认、后续复译、模型建议分别取证。选择状态只带当前阶段、建议 ID 和版本、场景与状态版本、来源绑定及 `learning_evidence=false`。

```sh
python3 -B scripts/verify_gpt56_prompt_stack.py --skills-root codex-skill-sources
python3 -B -m unittest tests.english_pipeline.test_expression_comparison tests.english_pipeline.test_memory_query.MemoryQueryTests.test_same_question_number_in_two_articles_requires_exact_source
python3 -B -m unittest discover -s codex-skill-sources/kaoyan-english-daily-intake-curation/tests -p test_contracts.py
```

生产安装由本机六个英语技能、项目提示与当前用户的宿主规则组成。用户级 AGENTS 只留本机，生产项目根没有附加的 AGENTS.md。公开的 `prompts/codex_prompt.md`、精读根、输出模板、机器定位和日终显示说明是脱敏映射。正式 author、宿主侧栏和数据发布的完整运行环境位于生产安装。可在此审阅树独立运行上面的合成验收。T3 消费选择状态与实际用户长期学习效果分别留待宿主接入和真实学习回合验证。

本轮按触发条件缩短根技能，将事务、预检、按原回执恢复的步骤保留在有效引用中；按实际风险选择针对性验收。表格与可复制状态采用本地实现，未引入第三方服务器。[OpenAI 技能设计](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra) · [官方提示指南](https://developers.openai.com/api/docs/guides/latest-model/gpt-6-astra.md#prompting-best-practices)
