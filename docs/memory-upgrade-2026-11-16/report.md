# 英语记忆增量与发布报告

2026-11-16 · KAOYAN-MEMORY-20261116 · 英语

## 实际基线

| 层级 | 核定版本 |
|---|---|
| GitHub 源码库 main | `4809ca08ce97a9ed4c3e2f5f4143973439188cdd`，与任务书一致 |
| GitHub 资料库 main | `087be8e7089a02fa3822bff2164d04af123f2d68`，旧资料快照 |
| 源码审查分支基线 | `b33ff42c9d7bf26faaef6abc6b5e10f33b0eb6e3`，包含 11 月 15 日文件及正式接入记录 |
| Mac 私有仓库跟踪 HEAD | `9bbb7c1cf84fbe769cdaa5f38085068fe4b86414`，原 Gitee origin 及工作改动保留 |
| Mac 实际源码对照 | `c9e496f194e5c448cce39b170ade8321e461b973`。两个被改引擎文件与公开对照对应的文件一致 |
| Mac 当前原生投影 | `b0aa1a257534f9c7bf4c3f6b94f98ee28739c5fea35165c46d435ae6ae6d6d4c`，native，正式模型策略启用，closeout ready |
| 本机隔离被测源码 | `7cdb2f64d19ced5672a4b9a472143d02364c442f`，只将本轮匹配的英语文件同步到实际工作目录 |
| 公开隔离被测源码 | `725496295b7ad926b901df99bbfa6deac5bbef1b`，tree `43120d38dacd1b9fe285450ac2c75f201e646e4e` |
| GitHub tested SHA | `a9614964250ba3f0140a0407e1efd14d3c34d244`，同一被测 tree。final SHA 由远端分支读回提供 |

ZIP 内计划与单独文件逐字节相同，8 个清单成员 SHA-256 均通过。其余文档提供诊断与复现材料，附带 AST 探针的答案检查使用无答案桩。它只作为定位证据。正式验收用实际英语 CLI 和访问门禁。

## 逐项判定

| 工作 | 结果 | 本轮证据 |
|---|---|---|
| E1 单请求 claims 复用 | ACCEPTED，已在 Mac 生效 | 首次合格命中时读 JSON，保持原过滤、状态指纹及证据输出；热查询一次请求外无缓存。0／1／3／8 点输出逐字节相同。42 个稳定权限与证据对照响应也逐字节相同。10,000 条合成 claim 的 8 点解析量下降 87.5%。生产单点满足同机时延门槛。当前生产状态尚无 claims 文件。 |
| E2 最终语境纠正 | REJECTED，保留 v7 日常讲解 | 复用已存在的条件并存、时间、题号与提示依赖。冻结 24 个离线来源题族和一次 12 组配对的真实解释。gpt-6.1-sol／max 两版 12/12，适用语境纠正均 7/7。候选完整讲解 p95 为 43.07 秒，超过普通讲解的 20 秒目标，且没有正确率收益。候选仅在隔离评测说明中保留。 |
| E3 重建和正式关闭 | KEEP_EXISTING：使用现有定点审计、索引和回执恢复。ACCEPTED：补回退和历史的来源门禁 | 基线在回退途中撤回、审计失效后回看旧代际，以及已发布索引损坏或指针缺失时存在重露旧观察的路径。现对这些路径施加当前审计与撤回约束。首次构建前仍允许无来源／单元／题号范围的受限精确读取。实跑已验证 writer 回执的派生恢复两次，原摘要指针与正式哈希稳定。 |

源记录、事件和掌握度沿用原权威。14 个生产数据或清单路径前后相同，其中学习事件 1018 条、排除事件 413 条。普通查询新增远程模型调用与正式写入均为 0。已启用的 Embeddings／Decisions 策略保持原配置。真实模型请求单独用于评价，见验收。没有新增 schema、依赖或常驻服务。公开仓库主分支、数据仓库、旧审查分支与 Gitee origin 仍保持各自身份。

## 研究取舍

将“取得纠正”和“解释采用了适用纠正”分别验收。该设计参考作者的受控冲突实验，本文只报告本项目同模型测试。 [When Retrieval Is Not Enough](https://arxiv.org/html/2610.36308v1)

沿用原始学习记录与有来源的派生投影分层，并核查定点恢复。没有引入图数据库或后台合并。 [MIRIX-2.0 论文](https://arxiv.org/html/2611.12068v1)

复用同一请求中的读取和解析。保持按需读英语证据与独立的只读 MCP；Hindsight 自动历史摄入、Unharnessed 持久内核与模型编排均未采用。网页工具未读到三个固定 GitHub 源树，产品行为判断限于已读文档、随包固定证据和本机复现。 [Hindsight 官方集成文档](https://hindsight.vectorize.io/sdks/integrations/coding-agents) [随包引用的 Unharnessed 固定源码](https://github.com/unharnessed-agent/Unharnessed/tree/fe2cf2815f78463464b91c598fc0c31c0af98f4c)

## 兼容与恢复

日常语义未通过或尚待验证的试验说明存于专属 Skill 的 `evals/`，日常入口不会调用。若需撤回 E1 性能改动，隔离演练已证明可单独逆用本地 E1 提交 `3c617e0`，保留 E3 门禁。需要恢复 Skill 文案时只还原本轮修改的文件，检查现有备份和安装目标哈希。

发现来源确知变化时沿现有 `build-memory-index --source-path` 定点审计并重建。已确认 writer 回执且派生 `memory_index` 待补时用 `recover-memory-closeout --writer-receipt`。在已有投影失效、当前审计不可验证期间只讲当前原句。现行生命周期、失效状态和正式账本保持为当前版本，历史代际保留供受控核查。

[验收](acceptance.md) · [Skill 对齐](skill-alignment.md) · [共享组件](shared-change-request.md)
