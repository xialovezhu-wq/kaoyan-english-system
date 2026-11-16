# 英语专属 Skill 对齐

## 实际 Mac 加载

使用 Mac 随 ChatGPT Desktop 安装的 Codex app-server 0.169.0-alpha.11.1 启动新诊断进程。用 `skills/list` 和 `forceReload: true` 读取英语仓库适用的 Skill，再从 app-server 返回的实际文件内容计算 SHA-256。6 个入口均为 user scope、enabled，英语加载错误 0。每个活跃源码目录与实际安装位置的 Markdown、Python、HTML、YAML、JSON 资源比对一致，共 55 份。安装路径为 `~/.codex/skills/<名称>/`，源为英语仓库 `codex-skill-sources/<名称>/`。安装末端是普通目录和文件。

| 名称 | Mac 源及已加载 SKILL.md SHA-256 | 实际入口与边界 |
|---|---|---|
| kaoyan-english-intensive-reading | `27ad307445739ddd1f895b20e24b2c02e9691ea787114e57ebbd1e74e127a87f` | 已安装 `english_skill.py` → `query-memory-context`。当前原句及作答优先，一次有界读取。逐题答案边界和保存授权沿原合同。 |
| kaoyan-english-daily-intake-curation | `c78d481974985cfbeb355dbc6ce1c4bef88a34cd42581163d9a405381f3ab8d8` | 本地作者 `author-local-day`、唯一 typed writer 和派生恢复。B1 已启用的正式模型策略保持；教学查询只读。 |
| kaoyan-english-reading-intake | `f2d50c8e249178ade7743f160a0e604f1262c2d8b331ca26d776e8550e33de25` | `capture_current_english.py` 与会话包合同。仅在授权时封原件，新手动保存仍不进入正式账本。 |
| kaoyan-english-web-bundle | `336f7d031d4b61bb13061ccdb491ab86ae394c542808b4898d3e99691f8a2a6a` | 兼容明确外发的本地分包。旧默认外发路线仅历史保留。 |
| kaoyan-english-web-return | `fccc23515bba8f54e1daa9e37711d7eac7a944f955fc78d37c136eb2387ba098` | 现有回包本地审阅，授权后进入同一 writer。网页决定与本地正式事实独立。 |
| kaoyan-english-vocab-export | `499472b72515be5923cdd8275cc2a8d21e86acc1267db1a842a99b8d4262fcc7` | 按同版正式闭合与现有掌握证据导出。导出不授予新的学习事实。 |

下列同一诊断进程返回的内容，也与安装目标逐字节一致。

| 已加载引用或脚本 | SHA-256 |
|---|---|
| 精读 `references/memory-context.md` | `ed30461d0b8f5360a3e33cbd60d14db5910e05caf9d64f0f6d7cfa64bf87f45f` |
| 精读 `references/point-personal-summary.md` | `f2d6759e344f5eb2f84a2545130fe5c380842b8b625f06051981c3c261ef685e` |
| 精读 `scripts/english_skill.py` | `d0cd62f625237528ec0916cbe7878c4c6c646c77a5c0272918f40c2deb498e36` |
| 日终 `references/nightly-curation-contract.md` | `0abc700afb7f6e80d8d88bf7ffe96b4c07c1d192279c14d78477772618e82230` |

现行 `conversation-package`、`typed-actions`、`sentence-support`、归档与发布引用已比对。只有精读入口、两份精读引用、一份日终引用和隔离评测说明有新内容。保留各 Skill 原有自动选择策略。评测说明 `evals/context-correction-candidate.md` 的 SHA-256 为 `4cec4df0d675c9474deaea1496e1ca35bb051b58f4591c2d8962bf10c1758e74`。它不被日常教学加载。

已实际运行诊断所指向的精读 wrapper，并在现行投影读回 2477 UTF-8 字节、单点 ready、native、接口模型请求及正式写入均为 0。加载的 `memory_query.py` SHA-256 为 `41852dc2eeea5483ba24c813028868f7b45691cd18f03607935281003a706c3a`。新加载验证不推断已有聊天的指令文本重载。真实解释采用相同模型和已安装 wrapper，另见验收；桌面 UI 回合未运行。

公开源码库沿用可移植 Skill 文本，精读说明使用会话当前模型，外发仅显式启用。公开精读 `SKILL.md` 和日终合同的字节与 Mac 私有运行说明不同，运行接口及本轮门禁已对齐。公开 Skill 不覆盖 Mac 原有精读呈现和本机专属作者入口。
