# 基于近期论文的证据覆盖与预算控制改进

开始日期：2026-10-18；真实检查完成于北京时间 2026-10-19。背景：[200 题 Badcase 归因](BADCASE_ATTRIBUTION_20261018.md)。本次交付是新推理策略与验证入口，原 200 题结果作为历史基线保留。

## 论文依据与选择

| 论文 | 已核对的机制 | 本项目取舍 |
|---|---|---|
| [HALT，2026-08-03](https://arxiv.org/html/2608.02009v1) | 按必要推理跳检查证据覆盖；区分问题生成的目标与使用标注的诊断设置 | 采用问题生成的需求清单及逐项覆盖账本；复用独立 Verifier，未复现论文训练的分类器 |
| [Inference-Time Budget Control，2026-05-07](https://arxiv.org/html/2605.05701v1) | 分开控制搜索资源与最终答案；保守处理答案形式转换 | 复用已有预算预留控制器，增加作答检查时机和带类型的最终转换；未实现论文树搜索、critic 或 VOI 公式 |
| [AnySearch，2026-09-01](https://arxiv.org/abs/2609.00813) | 通过强化学习训练可适配预算的搜索策略 | 本轮没有该训练权重与训练验证条件，暂不引入 |
| [Routed Graph Handoff，2026-08-26](https://arxiv.org/abs/2608.25277) | 按任务选择图结构或自然语言交接 | 本轮尚未证实交接裁剪的因果影响，保留既有契约，暂不增加路由模型 |

以上是工程借鉴，不是论文复现；论文中的性能数字不作为本项目收益。论文均通过原文页面核对，研究日期截至 2026-10-18。

## 已实现的行为

1. Supervisor 从原问题生成 1–8 条必要事实/关系及 `answer_type`。清单一旦生成就冻结，后续调度不能悄悄删除未完成项；Verifier 仍需直接检查原问题，避免计划漏项。没有读取 gold、支持文档标签或评分结果。
2. 子 Agent 每使用两次工具，先交回当前结果，进入起草和独立核验。这个间隔是显式检查策略，不意味着两次检索已经足够。现有逐题调用、Token、时间上限继续生效。
3. Verifier 为每条需求返回 `supported / partial / missing` 与引用编号。缺项、重复编号、越界需求索引、空支持引用或未授权引用均不能算完整覆盖；完整覆盖还必须与 claim 核验、问题完整性检查同时通过。
4. 满足上述条件即结束，无需再花一次 Supervisor 调用决定 finish。缺失桥接、未完成问题项或核验问题保留为不完整，不能因为预算耗尽就放宽支持标准。
5. 格式修复由工作流共享一次变为每角色一次、最多四次。仍执行完整严格校验，不删除额外字段以伪造合规。外层总调用与 Token 上限不会增加。
6. 新评测方法只对完整核验且完整覆盖的结果做最终答案转换。布尔问题明确要求 `yes/no`；返回国家等错误类型时拒绝为 `UNKNOWN`，而不是猜一个极性。数字与实体转换也有明确约束；所有生成均计入原预算。

## 模块与兼容性

- `agents/evidence_policy.py`：策略、覆盖账本检查和生产工具/时间边界，仅依赖标准库。
- `agents/contracts.py`：新增 `ControlledDecision`、`RequirementCoverage`、`CoverageVerdict`，保留原协议。
- `agents/coordinator.py`、`researcher.py`、`answering.py`：调度、子任务探测间隔与独立覆盖核验。
- `agents/model_calls.py`：按角色分配严格修复机会，提示禁止把输入计数器回显为输出字段。
- `agents/context.py`：把新增协议和需求字段计入证据上下文预算。
- `application/answer_service.py`：显式装配可选策略，不导入离线评测。
- `deepsearch_experiments/benchmarks/multihop/answer_projection.py`：离线短答案转换；生产仍返回带引用的核验答案。

新增观察事件：`rag.policy`、`verification.coverage`、`rag.coverage`。覆盖状态是模型报告，不是人工真值；已有事件字段、证据 ID、原轨迹分组与人工标签不改写。

原 `mainline` 继续使用旧协议、共享修复额度和原答案投影。新方法为 **`mainline-evidence-controlled-v1`**，方法元数据记录了策略版本、两次工具的检查间隔、修复额度和论文来源，且运行仍保存源码指纹。

## 配置与复现实验

生产配置 `.env.agent` 新增可选项，示例保持关闭：

```dotenv
AGENT_EVIDENCE_CONTROL_ENABLED=false
```

设为 `true` 后，新发起的真实问答使用新策略。未修改本地私有配置、未重启正在运行的服务。正式推广前应完成更大规模配对评测。恢复旧检查点时不要切换策略版本；本轮只验证新策略的新运行，未验证跨策略续跑。

生产侧的资源控制读取当前工具会话余量和时间；无法得知模型调用/Token 总预算时，明确记录 `null`，不冒充已知预算。评测侧继续使用外部精确调用计数及原 Token 预算策略，两者不能混称相同硬约束。

评测入口：

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m deepsearch_experiments.benchmarks.multihop run `
  --manifest <frozen-manifest.json> --method mainline-evidence-controlled-v1 `
  --output <new-run-directory> --llm-env .env --allow-remote --direct `
  --max-model-calls 24 --max-search-calls 8 --token-budget 256000 `
  --max-output-tokens 8192 --context-window 131072 --deadline-seconds 600 --max-steps 6
```

同条件比较应在同一源码版本分别运行 `mainline` 和新方法。历史 200 题可以作历史参照，不能冒充同源码配对实验。不得覆盖旧运行或自动重试已保存失败。

## 验证边界

- 新增 17 项针对性测试：实际图路径、必要关系缺失、伪造/重复引用、角色修复隔离、严格拒绝、布尔类型、工具预算、生产装配与上下文隔离。
- 真实模型开发检查冻结在 `output/evidence-controlled-20261018/canary/plan.json`：4 题、新旧方法各一次、共 8 个结果，同模型/语料/总预算，最多 4 个进程。题目按已知结果选取，用于发现机制问题，不是无偏抽样或总体效果评测。
- 尚未完成新的 200 题配对实验，也没有进行多种随机种子、统计显著性或真实业务容器验收。
- 覆盖核验仍可能误判，问题分解可能漏项；更频繁的检查可能增加核验调用，并过早中断需要长子循环的搜索。新策略的保守拒答也可能降低部分可回答样本的自动得分。

## 已完成验证与真实结果

最终完整后端回归：**877 passed, 14 skipped**，见 `output/evidence-controlled-20261018/pytest-final.log`。依赖检查：196 个模块、0 个循环、0 个违规依赖。跳过项不算通过，未额外进行容器、浏览器或外部服务集成验收。

真实配对检查：同一源码、MiMo-V2.6-Pro、同一冻结语料与问题、24 次模型调用、8 次检索、256000 Token、8192 最大输出、600 秒；每方法 4 题，无重试，所有用量已返回。

| 开发检查指标 | 原 mainline | evidence-controlled-v1 |
|---|---:|---:|
| 问题数 | 4 | 4 |
| EM / F1 | 0.50 / 0.50 | 0.50 / 0.50 |
| 命中标准答案 | 2 | 2 |
| 预算耗尽 | 1 | 0 |
| 格式失败 | 1 | 0 |
| 正常状态但拒答/空答案 | 0 | 2 |
| 模型调用 | 41 | 40 |
| 检索调用 | 14 | 12 |
| 提供方报告 Token | 123583 | 156259 |
| 缺失用量调用 | 0 | 0 |

**没有准确率提升，Token 增加约 26.4%。** 不能把执行失败变成拒答说成完成了更多正确问答，也不能把较少的调用次数称为总成本下降。结果只支持机制接入有效，尚不支持推广新策略为默认值。

逐例观察：

- 2Wiki 两题新旧均命中。新策略每题使用 6 次模型调用，原流程分别 10、12 次。历史 `Canada` 代替 `yes` 的错误在本次原流程中未复现，不能将这题算作因果修复。
- MuSiQue `2hop__128342_375952`：原流程检索预算耗尽；新策略经历三次起草/核验后返回空答案，缺失关系始终未覆盖。初始需求清单将含糊实体视作城市名称，说明计划/实体类型消歧仍是限制，而不只是停止策略问题。
- MuSiQue `2hop__50910_177869`：原流程格式失败；新策略通过协议并找到了标准答案对应的已支持 claim，但核验器记录了不同文档对出版者身份的冲突，最终拒答 `UNKNOWN`。三次草稿/核验未消除问题，增加了 Token。不能只因 gold 名字出现便把关系冲突自动忽略。

本轮没有为这两条失败再调参或重试。后续应先研究如何区分阻断问题与非阻断备注、避免无新增信息的重复修订，以及在证据约束下处理计划中的实体歧义；之后再冻结新版本和更大的配对样本。

最终记录：`output/evidence-controlled-20261018/validation.json`、`canary/summary.json`；逐数据集同条件比较为 `canary/*-comparison.json`。8 个完整结果按自动 EM 存于 `canary/success.jsonl`（4 条）、`failure.jsonl`（4 条）、`unknown.jsonl`（0 条），包含完整原始 attempt 和空人工标签。原 200 份轨迹、清单和 gold 的哈希均验证未变。
