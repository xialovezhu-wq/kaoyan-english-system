# 首译与模型改写

独立首译前只呈现已获准的原句，等用户作答。用户已给首译或明确要求直接讲解，且比较表达有助于当前目标时，先说清本句最关键的理解，再给一至两种所需的正式、简洁或口语表达。逐项核对否定与量词的范围、时态、强弱、因果和指代。指出保留的关系；有变化则点明具体差异。

聊天里分清原句、用户原样首译、提示后实际发出的修订和**模型改写**。首译的提示情况未定时保留未定。用户只说“懂了”记作确认，未见复译就写“尚未观察到用户复译”。直接讲解可以没有首译。正文允许短对照表；只有一个断点时，用短段落就够。逐题反馈沿当前文章和题号的原权限。词条、点击和复制都不产生学习事件。

## 可复制的选择状态

当前对话已有可靠原文和作答时，直接按上文讲。需要生成可复制的建议状态或把已过滤的来源绑定交给界面时，在英语仓库运行 `python3 scripts/english_learning_pipeline.py expression-comparison --input-json -`。回执中的 `chat_text` 用于聊天，已有句图沿原展示入口。每个 `copy_states` 对应一条建议，包含当前阶段、来源与句号、选择 ID、建议版本、场景与状态版本。复制其中一条即可表达本会话的选择；向外回传由宿主负责。

输入 `source` 用既有 `source_id`、`unit_id` 和已知 `source_hash`，已知分句版本时加 `record_sha256`。用户当场提供、尚未编目的原句用 `source_kind=user_provided`，在 `original_message` 中给原用户消息的 `message_id` 和逐字 `quote`。只取普通文章句；题干与选项仍由逐题入口展示。

- `stage=before_translation` 只返回原句和等待提示。
- `stage=after_translation` 的 `first_attempt` 引用真实用户消息的 ID 和原文引句。`first_attempt_hint_dependence` 沿用 `none / hint / explanation / not_observed`。有提示后修订时，`followup` 给后续用户消息 ID、引句、`status=prompted`，以及 `hint` 中间助手提示的消息 ID 和引句。只有确认时用 `acknowledgment_only`，未回复则省略 followup。
- `stage=direct_explanation` 的 `direct_request` 引用当前用户明确要求讲解的原话。先由讲题者核实授权范围和语义，再由入口校验说话人和逐字引句。
- `suggestions` 只放模型建议。每项给 `style`（正式、简洁或口语）、`text`、`meaning_relation`（preserved 或 changed）和 `basis`（本句实际保留或改变的含义）。回执将建议标为 assistant。

同一会话默认使用实际线程身份。隔离验收可指定合成的 `--session-id` 与 `--rollout`。正式保存仍沿原 Capture 与 writer，个人摘要使用已有 first_attempt、correction、later_observation、hint_dependence 和 followup_status。
