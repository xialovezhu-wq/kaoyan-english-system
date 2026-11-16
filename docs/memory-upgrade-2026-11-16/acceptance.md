# 英语记忆与最终讲解验收

## 冻结版本及门槛

B1 采用本机已启用的 v7 源码和公开审查基线 `b33ff42c9d7bf26faaef6abc6b5e10f33b0eb6e3`。B2 代码采用公开本地 `725496295b7ad926b901df99bbfa6deac5bbef1b`／GitHub `a9614964250ba3f0140a0407e1efd14d3c34d244`，两者 tree 相等。Mac 运行绑定来自本机独立源码快照。生产资料照 `report.md` 的哈希清单只读冻结。最终提交如只含四份报告，按 tree 对照核实引擎、测试及 Skill 的字节无变化。

每种输入 32 轮 B1／B2 交错。两边使用同机、同 T9 源码盘和同输入。冷启动每次启动 Python CLI。热查询重用本版本的 Python 进程。系统页缓存保留。固定最长查询时间 8 秒，热查询 p95 上限取 B1 × 1.05 和 B1 + 20 ms 的较大值。真实单点输入来自既有已读英语证据，不向远端提交题目、首译或原始 JSON。

## E1 完整 CLI 数据

合成语料含 10,000 条 claim，文件为 2,135,572 字节。统一按 UTF-8 和紧凑 JSON 比较输出。以下时间单位为 ms，格式为 p50／p95。每次仅计一条 native 请求。

| 请求 | B1 冷 | B2 冷 | B1 热 | B2 热 | 解析 B1 → B2 | 两版输出 |
|---|---:|---:|---:|---:|---|---|
| 无命中 | 58.77／64.61 | 58.72／62.40 | 1.12／1.67 | 1.13／1.78 | 0 → 0 次，0 → 0 B | 826 B / 0 点 |
| 合成 1 点 | 69.45／74.93 | 70.41／74.64 | 10.11／10.82 | 9.77／10.68 | 1 → 1 次，2,135,572 → 2,135,572 B | 1,808 B / 1 点 |
| 合成 3 点 | 88.12／91.97 | 71.95／73.83 | 26.48／28.02 | 10.51／11.30 | 3 → 1 次，6,406,716 → 2,135,572 B | 3,810 B / 3 点 |
| 合成 8 点教学 | 129.93／138.75 | 73.79／77.36 | 67.62／74.03 | 13.15／14.25 | 8 → 1 次，17,084,576 → 2,135,572 B | 4,899 B / 4 点 |
| 合成 8 点审计 | 129.22／137.10 | 73.80／79.36 | 68.77／73.24 | 13.03／14.44 | 8 → 1 次，17,084,576 → 2,135,572 B | 9,695 B / 8 点 |
| 现行真实单点 | 61.96／65.55 | 62.76／69.58 | 4.31／5.72 | 4.31／5.92 | 0 → 0 次，0 → 0 B | 2,477 B / 1 点 |

教学八点的预算为 6144 B，两版均返回 4 个完整点与部分覆盖状态。审计视图在明确的 16 KiB 预算下返回全部 8 点，两版同样逐字节一致。真实单点输出为 2477 B，生产状态无 claims 文件。生产单点的热 p95 上限为 25.72 ms，实际为 5.92 ms。回放所有请求模型调用数、正式写入数及学习事件写入数均为 0。

八点教学单次冷进程峰值 RSS 为 61.1 → 43.1 MiB。长驻热进程在此次固定顺序的累计峰值为 78.7 → 78.9 MiB。热 RSS 包含先前请求的分配器高水位。

另有 42 条完整 CLI 响应逐字节一致，最大 1,998 B。包含 candidate/confirmed、可并存语境、迟到旧记录、未来与撤回、精确旧摘要回退、同题号跨篇、未解锁及正确解锁、别名和相关点。新增安全修复的故障响应按下节规则有意改变。

## E3 故障和回退

- 回退并发撤回：B1 合成复现返回 1 个旧观察，B2 返回 pending、0 观察。稳定的未建索引精确读取仍以 degraded 输出。已发布索引损坏、遗失指针或不能验回当前来源审计时，停止使用旧摘要。
- 已审计来源变化：正常当前路径返回 conflict。历史查询仅保留相同现行来源审计范围内的有效记录。受影响路线从 1 个历史观测降为 0，无关路线继续读回。定点原生 CLI 实测仅更新 1 个受影响点，其余保留。预览 73.8 ms，为零写入。实际派生重建 83.1 ms，原始证据哈希与正式/学习写入计数无变化。
- 已变为受保护的记录不能通过旧历史代际、同号题、别名或关系回退。保留已明确开放文章与题号的正常读取。撤回后再读以生命周期为准。
- 隔离 writer 实测 APPLIED，`validate-formal-receipt` 返回 PASS。注入索引故障使阶段从 personal_summary 到 memory_index，真实 CLI 恢复后为 ready。同回执再次恢复返回 0 个待补。正式 apply 调用计数 0，正式文件哈希、已确认 writer 回执和摘要指针不变，隔离学习事件 0 → 0。writer 耗时 13.9 ms，首轮恢复 CLI 134.9 ms。发布模拟器返回故障，演练未调用远程发布。
- 只反转 E1 提交后，隔离运行 4 项回退、历史和答案门禁测试，全部通过。保留 E3 修复和当前来源审计，不回退现行生命周期。

## 命令及退出结果

以下变量指已核验的英语源码、隔离 repo/state 及 T9 证据目录。正式热夹具使用临时系统卷，因为原生 Capture 拒绝用挂载盘作前台热暂存。故障注入使用合成目录。

```bash
python3 scripts/english_learning_pipeline.py query-memory-context --help
python3 scripts/english_learning_pipeline.py memory-status --repo-root "$QA_REPO" --state-dir "$QA_STATE"
python3 scripts/english_learning_pipeline.py build-memory-index --repo-root "$QA_REPO" --state-dir "$QA_STATE" --audit-only --source-path "$QA_SOURCE"
python3 scripts/english_learning_pipeline.py build-memory-index --repo-root "$QA_REPO" --state-dir "$QA_STATE" --source-path "$QA_SOURCE"
python3 scripts/english_learning_pipeline.py query-memory-context --repo-root "$QA_REPO" --state-dir "$QA_STATE" --backend native --view teaching --key word:example --max-bytes 6144
python3 scripts/english_learning_pipeline.py validate-formal-receipt --repo-root "$QA_REPO" --state-dir "$QA_STATE" --writer-receipt "$QA_RECEIPT"
python3 scripts/english_learning_pipeline.py recover-memory-closeout --repo-root "$QA_REPO" --state-dir "$QA_STATE" --writer-receipt "$QA_RECEIPT"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/english_pipeline -p "test_memory*.py"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.english_pipeline.test_context_correction_holdout tests.english_pipeline.test_formal_model_review
PYTHONPATH=tests/english_pipeline:. PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.english_pipeline.test_memory_query tests.english_pipeline.test_memory_store tests.english_pipeline.test_memory_upgrade tests.english_pipeline.test_context_correction_holdout tests.english_pipeline.test_formal_model_review
PYTHONDONTWRITEBYTECODE=1 python3 "$QA_EVIDENCE/benchmark_claims.py" "$QA_B1" "$QA_B2" "$QA_SCRATCH"
PYTHONDONTWRITEBYTECODE=1 python3 "$QA_EVIDENCE/compare_holdout_cli.py" "$QA_B1" "$QA_B2"
```

上述验收路线最终退出码均为 0。Mac 全记忆测试 103 → 107 项通过。新增离线留出与正式模型定向共 10 项通过。公开源码 76 项通过。英语精读与日终 Skill 静态检查及 5 个合同测试通过。首次将 Capture 热夹具误放在 T9 的 6 项设置错误已用原生要求的临时热路径复验。公开测试的旧模块导入使用测试目录的 PYTHONPATH 后通过。两版均有既存 SQLite ResourceWarning。

## E2 冻结评估

开发复用了对照证据绑定、跨文章、不同题、补录、时间选择、有限字节和提示依赖等现有测试。24 个独立合成留出来源题族中，12 个是语境／词性或已撤回纠正，其中同一题族没有被分到开发与留出。其余覆盖裸确认、提示、独立表现、当前已对、跨篇同号、仅解锁指定归属、未来、撤回、普通和无记忆。离线层核对完整证据、有界输出及权限，不担任最终讲解的判分。

离线留出语料及断言的 SHA-256 为 `21d9152d16eb70cd603a7910ea6e6d7eb06b7ad38458f6cac6b93f139d1be8f9`。冻结真实模型输入 SHA-256 为 `94fef6a09504042d166246e9cb79266f06e98567fd8a62048946c23534ab0f59`。12 组来自不同合成来源，分别覆盖词性及搭配共存、同范围订正、短确认、提示依赖、当前正确、普通翻译和未解锁题。用户确认本轮 API 额度可用后，使用先前已验回的 OpenAI Responses SDK 2.30.0 与 gpt-6.1-sol／max。串行执行 24 次，每次 8192 输出 token 上限，120 秒 transport timeout，自动重试 0。全部 24 次完成，无失败或超限，无额外开发调用。

每例先由已安装英语 wrapper 冻结真实 native 上下文，两版逐字节一致。使用同一当前原句、合成首译或首答、授权及上下文，一臂增加已隔离的 B2 条款。输出只交付本组明示要求的完整文字讲解。冻结的判分材料与配对映射未传给模型。模型完成后按匿名 X/Y 输出逐例审核，再解盲。判分检查结论、条件、来源、提示依赖、答案边界及完整性。显式来源标记仅在核对正文确实有匹配证据后单独计数。首次有用时间对应第一条实质完整句。计时为模型流式墙钟加实际 wrapper 检索用时，未计入口 UI。p95 是小样本观测。

| 指标 | B1 | B2 |
|---|---:|---:|
| 完整解释通过 | 12/12 | 12/12 |
| 适用语境纠正 | 7/7 | 7/7 |
| 其余普通、提示依赖及题号保护 | 5/5 | 5/5 |
| 明确引用本次有效观察 | 2/12 | 7/12 |
| 首段有用 p50／p95（秒） | 23.93／41.00 | 24.06／41.94 |
| 完整讲解 p50／p95（秒） | 24.61／41.75 | 24.95／43.07 |
| 普通讲解 p50／p95（秒） | 31.00／41.75 | 28.09／43.07 |
| 普通讲解 ≤20 秒 | 1/11 | 0/11 |
| 实际输入 / 输出 token | 5,653 / 16,276 | 9,037 / 17,551 |
| 总 token | 21,929 | 26,588 |

首段与完整 p95 相对倍数分别是 1.023 和 1.032，均在 1.10 门槛内。两版的普通完整讲解 p95 都超过 20 秒。候选增加 21.2% token。整组实际用量 48,517 token，费用未核验。未观察到候选的正确率收益，故保持现行教学规则。24 条输出无答案泄漏、无将提示后表现升级成独立掌握。

| 留出题族 | B1 首段 / 完整（秒） | B2 首段 / 完整（秒） | B1 → B2 输出 B | B1 → B2 token |
|---|---:|---:|---:|---:|
| charge | 38.60 / 39.09 | 22.95 / 24.11 | 344 → 379 | 2158 → 1894 |
| file | 23.93 / 24.61 | 24.06 / 24.96 | 438 → 466 | 1653 → 2138 |
| fine | 33.94 / 34.77 | 27.69 / 28.41 | 414 → 398 | 2176 → 2459 |
| subject | 32.78 / 33.72 | 22.12 / 22.83 | 392 → 393 | 2179 → 1946 |
| screen | 21.40 / 21.95 | 33.55 / 34.27 | 340 → 388 | 1531 → 2705 |
| address | 30.52 / 31.00 | 31.06 / 31.60 | 429 → 403 | 1966 → 2441 |
| account | 31.51 / 32.05 | 22.44 / 23.15 | 385 → 388 | 2052 → 1924 |
| infer | 22.22 / 22.91 | 27.33 / 28.09 | 341 → 448 | 1593 → 2283 |
| coerce | 41.00 / 41.75 | 41.94 / 43.07 | 398 → 364 | 2645 → 2917 |
| tender | 16.75 / 17.15 | 23.09 / 23.66 | 228 → 242 | 1327 → 1831 |
| lend | 21.00 / 21.92 | 35.36 / 36.13 | 329 → 321 | 1486 → 2683 |
| 未解锁题 | 19.92 / 20.39 | 15.29 / 15.39 | 282 → 172 | 1163 → 1367 |

逐例证据保存在已核验 T9 批次。模型回放使用 `/usr/local/bin/python3 -B <T9 evidence>/run_context_teaching.py freeze live`（退出码 0）。命令有实际 API 用量，不用于日常回合。移除或打开历史材料的额外模型实验未运行。

## Mac 加载与共享

Mac 实际 app-server 进程强制刷新，6 个英语入口和引用的读取哈希见 [Skill 对齐](skill-alignment.md)。实际安装 wrapper 运行成功，仍读取 v7。共享既有快照只读验回通过。未运行桌面 UI 的真实学习回合、自动调度、新共享候选联调或生产正式写入。绝对教学时延目标和模型端观察分开记录。

## GitHub 发布

本机 gh 凭据无法登录，使用已有 GitHub 连接器对目标库创建同一已验 tree 的提交。实际 Git fetch 已核对 GitHub tested SHA 的父提交和 tree。只向新审查分支发布，后续用远端 ref 与 final tree 验证。资料库和 main 未写入。
