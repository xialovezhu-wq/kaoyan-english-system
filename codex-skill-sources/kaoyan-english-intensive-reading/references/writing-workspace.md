# 视频逐点操作

正常入口为已安装精读 Skill 的 `scripts/english_skill.py run english_writing.py point`。start-today 或 resume 先找原点的状态。需要跨聊天接续时，point resume 传原 workspace 与 attempt-id；开始另一份练习用新的 practice_id。已知点引用原课程 lesson_id 和哈希，只有用户转述时用 reported_video 的真实消息与准确引文，原视频未核的时间留空。真实阅读题沿原文章、题号、句哈希和授权页回跳。已处于开放评估时先按原入口结束该场。

point start 从 stdin 读 lesson_id、lesson_point_id、point_version、rubric_version、practice_id、source、goal、target_kind、target_skill_id、criteria、qualified_variants、counterexamples、question；每项 criteria 有 description、required、evidence（meaning、written_form 或 reading_judgment）。源可用 registered_lesson 和 lesson_sha256，或 reported_video 与 link、message_id、sha256、excerpt。创建时可传当前 revision。资料不足则请学生继续视频并提供实际点，不凭课名编题。

原消息绑定使用 UTF-8 原样文本 SHA-256。point delivered 需带真实助手消息、question_id 和当时 revision/attempt_id/action_id；资料屏渲染的题由页面确认。聊天收到消息可直接运行 point turn --latest，程序以同一聊天真实助手问题和当前用户消息核对送达并返回 apply_request。对明确定位的消息也可用 point turn（message 带 link、message_id、sha256），让当前教练按[逐点合同](guided-course-points.md)同时判条件与下一动作。point apply 在原绑定和 packet_sha256 上写 decision；一个动作 ID 用于同一操作重试。本轮只发已验反馈和一个支持当前缺口的问题。确认已发的讲评或示范用 feedback-seen。当前点过关或本人跳过后，point next 按已冻结的课程顺序开下一点；下一点未知时请本人继续视频。point query --point-id 精确读正式观察，point read 读本课 pending 状态。point feature --set off 在本机持久关闭逐点教学，--set on 恢复。单次教练进程也可用 KAOYAN_ENGLISH_GUIDED_POINTS=off。关闭后沿 V2 上课，历史记录仍可读。
