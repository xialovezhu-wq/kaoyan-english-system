# 按需资料提取与疑义复核

本批 PDF、截图或网页原件辨识困难时，才复用 `bridge_root/scripts/study_material_preprocess.py`：用 `bridge_python` 执行，传 `--subject english --source 原件绝对路径 --pages 1,3 --purpose reference`，purpose按来源角色选reference/question/user_work/solution；公开网页可用 `--url`，不带浏览器凭证。先本地文字提取，扫描页/图像按需调用视觉 API；不为清楚的资料重复增加一层模型。输出原件副本、提取稿和缓存走本科已核验 T9 冷目录，来源/hash/页码保持可追溯，不修改 Capture 或正式库。

`--review auto` 是默认，仅对提取疑义页追加 Decisions；已有明确疑义可用 `--review-pages 1,3`，页码必须属于本次 `--pages`，`--review off` 只跳过此旁路。它比较原页图与提取稿，提示可能漏项、混区、角色或文字不一致；HTML/TXT 没有原页图时明确未筛查。无疑义不默认每页复核；空白/过少内容等可程序核查的情况先用代码。失败保留已提取结果与未筛查原因，不重跑成功正式写入。

默认 `protected`，`after_attempt` 也只返回元数据；只有当前任务已授权读取该内容及相应答案范围时，才用 `--view direct --allow-answer-text`。正式审核授权不扩展到未来题、兄弟小问或其他答案。机器提取、疑义复核和回包 `evidence_prescreen` 是不同的派生辅助：均不替代实际核读原图、完整真实对话与本科语义审核；未标记不是审核通过，提示后完成不是独立掌握，缓存不是新学习。只将原证据支持的结论交既有唯一正式 writer，保留原日期、身份、原评分/掌握度与幂等恢复规则。
