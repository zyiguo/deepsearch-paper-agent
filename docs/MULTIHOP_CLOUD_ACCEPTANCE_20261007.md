# 外部 Baseline 云端验收报告

日期：2026-10-07。模型：`mimo-v2.6-pro`。范围：IRCoT-adapted / LogicRAG-adapted，MuSiQue / 2WikiMultiHopQA。

## 验收结论

**首轮固定小样本云端验收已完整执行，评测管道通过，baseline 稳定性未全部通过。**

- 先执行 4 次单题 canary，随后执行 2 种方法 × 2 个数据集 × 10 题，共 40 次正式 pilot 执行。
- 两个数据集各 10 个独立问题，两种方法使用相同问题；不是 40 个不同问题，更不是两个 1,000 题全集的完整评测。
- Canary 独立记录，不混入 pilot 分数。所有题都保留结果，失败计 0 分；没有择优重试、删题或改金标准。
- Pilot 共 32 次正常返回、8 次失败：5 次结构化输出协议失败、2 次时限取消、1 次模型服务错误。没有缺失预测。
- 下载使用的上游文件已在前一阶段固定；本轮模型直连，不经 7897。没有修改方法、提示词、模型网关或预算来迎合本轮结果。
- 这批结果用于工程验收与问题定位。适配偏差和执行失败尚未消除，不能作为原论文复现结果，也不能支持本项目改进优于这些方法的结论。

## 固定条件

| 项目 | 设置 |
|---|---|
| 数据协议 | `logicrag-released-v1`，`sampled_pool`，seed=42 |
| 候选语料 | MuSiQue 11,656 段；2Wiki 6,119 段；只采样题目，不缩小候选语料 |
| 模型与接口 | `mimo-v2.6-pro`；`https://token-plan-cn.xiaomimimo.com/v1`；直连 |
| 模型模式 | 沿用提供方默认思考和采样行为，未强制 JSON response format；不宣称严格随机可复现 |
| 检索 | 统一项目 BM25，k=5，k1=1.5，b=0.75 |
| 轮次与调用上限 | max_steps=6；每题最多 24 次模型调用、8 次检索 |
| Token 限制 | 每次输出最多 8,192；上下文 131,072；每题累计预算 256,000 |
| 时限 | 每题 600 秒，单次 HTTP 超时配置 300 秒 |
| IRCoT demonstrations | 对应作者提示词文件前 4 个带元数据训练示例，与评测 ID 无重叠 |
| 答案指标 | 规范化 EM / token F1；失败留在分母中 |
| 支持召回 | 所有检索轮次累计的文档级支持召回 proxy，不是 Recall@5、句子支持或 Joint 指标 |

方法、上游 commit、许可证与适配边界见 [接入文档](MULTIHOP_BASELINES.md)。IRCoT 原作者模型/Elasticsearch 和 LogicRAG 原作者稠密检索均已被替换，不应标成未经修改的官方实现。

冻结身份：

```text
implementation SHA256:
7bc1a1929826fb7f0848dabf1c4031c29e1753484e867531cb50a7039e06f2d6
MuSiQue pilot manifest SHA256:
832ac77d083c504f98233f80e76bddf5f673443753c11731e4658fd1c20e1ebd
2Wiki pilot manifest SHA256:
877bde65f4ded597384142d19f37ba746d82df96fe0f9eaaf9266f7ab02f1e2b
```

## 正式 Pilot 结果

| 数据集 | 方法 | N | 正常返回 | EM | F1 | 支持召回 proxy | 失败 |
|---|---|---:|---:|---:|---:|---:|---:|
| 2Wiki | IRCoT-adapted | 10 | 10 | 0.8000 | 0.8923 | 0.9500 | 0 |
| 2Wiki | LogicRAG-adapted | 10 | 9 | 0.6000 | 0.7812 | 0.9000 | 1 |
| MuSiQue | IRCoT-adapted | 10 | 7 | 0.2000 | 0.3133 | 0.6500 | 3 |
| MuSiQue | LogicRAG-adapted | 10 | 6 | 0.2000 | 0.2700 | 0.4667 | 4 |

“正常返回”只表示协议和执行状态为 `ok`，不表示答对或证据完整。每组 n=10，以上仅是描述性统计，不做显著性或整体优劣判断。

### 调用与成本

| 数据集 / 方法 | 模型调用 | 检索调用 | 已知服务端 tokens | 缺失 usage 的调用 | 预算记账 tokens | 累计题目耗时 / 秒 | 中位题目耗时 / 秒 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2Wiki / IRCoT | 56 | 46 | 249,988 | 0 | 249,988 | 402.02 | 20.85 |
| 2Wiki / LogicRAG | 33 | 11 | 21,418 | 0 | 21,418 | 207.83 | 18.70 |
| MuSiQue / IRCoT | 68 | 58 | 286,613（部分已知） | 4 | 382,581 | 2,648.76 | 214.16 |
| MuSiQue / LogicRAG | 33 | 11 | 32,112 | 0 | 32,112 | 426.20 | 38.01 |
| Pilot 合计 | 190 | 126 | 590,131（部分已知） | 4 | 686,099 | — | — |

Canary 另有 17 次模型调用、11 次检索，服务端报告 55,654 tokens。因此本轮全部执行已知 tokens 为 645,785，仍有 4 次调用的 usage 未知。MuSiQue / IRCoT 的 `reported_total_tokens` 正确保留为 null，而不是把部分已知总量冒充完整总量。

预算记账在 usage 缺失时保留 UTF-8 字节估算和输出预留，不是实际账单。取消客户端请求不保证服务端停止计算或不计费；本轮未查询账户账单，也不推算人民币费用。耗时是逐题耗时之和，不是整个批次的墙钟时间，批次存在并发执行，不能当作严格性能基准。

## 分阶段检查

| 检查层 | 结果 |
|---|---|
| 云模型与检索调用 | 已真实执行；正常返回事件中的模型名与配置一致 |
| 方法流程 | IRCoT 覆盖答案标记停止、轮次耗尽后的最终问答；LogicRAG 覆盖 warmup 直接回答和依赖补检后回答 |
| 输出协议 | 不通过：LogicRAG 的依赖图阶段出现 5 次解释文字包装 JSON，严格解析拒绝 |
| 长任务终止 | 2 次在约 600 秒触发 `deadline_cancelled_inflight_request`；失败和未知成本保留 |
| 逐题记录与失败分母 | 40/40 完整；每题恰好一次尝试；8 次失败均保留并计 0 分 |
| 离线重评分 | 8 个 canary/pilot 运行目录全部与保存报告完全一致 |
| 完成后续跑 | 8 个目录均用禁止创建客户端的工厂验证；零模型调用，运行记录字节不变 |
| 同条件比较 | 两个数据集的 compare 均通过模型、数据、检索器、代码与预算一致性检查 |
| 事件与 usage 对账 | 模型/搜索事件数量与调用数一致；已知 usage 汇总一致；未知不补造 |
| 敏感信息 | 检查运行元数据、尝试记录和报告未包含配置中的 API 密钥 |

程序回归：相关 70 项测试通过；最终全量 **641 passed, 2 skipped**（33.28 秒）。较早一次全量出现扫描件 ready 状态断言、SQLite 并发 `database is locked` 两项失败（639 passed, 2 failed, 2 skipped），两项单独重跑通过，最终全量也通过。未修改这些无关模块；保留为间歇性测试风险，不声称已经修复其根因。

## Badcase 标准化记录

逐题结构化记录位于 `acceptance-audit.json` 的 `per_case`：题目 ID、状态、EM/F1、标准答案、模型答案、诊断标签、失败阶段、终止原因、每轮累计支持召回，以及原始尝试记录的引用。标签是观察性诊断，不等于因果证明。

| 一级观察标签 | 2Wiki IRCoT | 2Wiki LogicRAG | MuSiQue IRCoT | MuSiQue LogicRAG |
|---|---:|---:|---:|---:|
| 答对且支持文档检索齐 | 7 | 5 | 2 | 2 |
| 答对但支持文档未检索齐 | 1 | 1 | 0 | 0 |
| 支持文档检索齐但 EM 不匹配 | 2 | 3 | 2 | 0 |
| EM 不匹配且支持文档未检索齐 | 0 | 0 | 3 | 4 |
| 输出协议失败 | 0 | 1 | 0 | 4 |
| 时限耗尽 | 0 | 0 | 2 | 0 |
| 模型服务错误，具体原因未确定 | 0 | 0 | 1 | 0 |

### 可定位的失败

- **`dependency_graph` 输出包装问题（5 次）**：模型返回解释文字加 fenced JSON，例如 `2hop__20256_74481` 的依赖边包含 `[[2,0],[2,1],[3,1],[3,2]]`，但整体不是纯 JSON。当前严格解析器拒绝；不能据此认定模型没有构建依赖图的能力。它暴露了云模型输出与适配器之间的兼容性问题，会压低当前 LogicRAG 结果。
- **IRCoT 在途请求取消（2 次）**：`2hop__20256_74481` 和 `2hop__263728_705527` 均已发出第 7 次模型调用，在约 600 秒被取消。这是总时限耗尽，不是 256K token 预算耗尽；需要区分。
- **模型服务错误（1 次）**：`3hop1__90327_78276_68042` 记录 `ModelServiceError`。现有安全日志只有异常类型，不能断言是超时、输出截断、限流或鉴权失败；细分原因仍未知。

### 答案与检索诊断示例

- `1900f7600baf11ebab90acde48001122`：两种方法均返回 `Hugh Stafford, 2nd Earl of Stafford`，标签为 `Hugh de Stafford, 2nd Earl of Stafford`，EM=0；疑似名称变体问题，需要别名依据，不能直接算作推理失败。
- `38be3008086a11ebbd5fac1f6bf848b6`：LogicRAG 的片名附带 `(2013)`，EM 不匹配。当前不额外剥离年份，也不更换评分口径。
- `1c0dd3b00bdc11eba7f7acde48001122`：两种方法均答 `Poland`，标签为 `Polish-Lithuanian Commonwealth`。属于需要区分历史实体的答案，不应像标点那样直接归一化。
- `2hop__53147_7298`：两种方法均答 `Josh Groban`，标签为 `Michael Bublé`，支持召回均为 0.5。可观察到支持缺口和跨跳目标混淆，未做反事实实验，不宣称检索缺口是唯一原因。
- `3hop1__9285_5188_86687`：IRCoT 答 `1955`，LogicRAG 答 `September 12, 1990`，标签包含 `1990` / `May 1990`。支持召回均为 1/3，需要检查具体历史事件与时间粒度；不为提高 EM 而事后放宽规则。

累计检索到文档，不代表文档进入最后提示词或模型正确使用了它。尤其 IRCoT 存在段落数量和长度过滤，LogicRAG 使用摘要；本报告不把检索召回冒充证据利用率。DAG 金标准、官方句子支持和 Joint 指标仍未实现。

## 产物与复查

所有原始产物保留在被 Git 忽略的数据目录，没有上传数据或凭据：

```text
data/benchmarks/multihop/runs/cloud-20261007-canary/
  {dataset}-{method}/{run.json,attempts.jsonl,report.json}
  {dataset}-comparison.json / .md
data/benchmarks/multihop/runs/cloud-20261007-pilot10/
  {dataset}-{method}/{run.json,attempts.jsonl,report.json}
  {dataset}-comparison.json / .md
  acceptance-audit.json
```

`acceptance-audit.json` 保存每个运行的检查结果、原始产物 SHA256、逐题诊断及搜索召回轨迹。下列命令只读已有记录、不调用云模型；输出使用新文件名：

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop score --manifest data/benchmarks/multihop/prepared/musique/pilot-10.json --run data/benchmarks/multihop/runs/cloud-20261007-pilot10/musique-ircot --output data/benchmarks/multihop/runs/musique-ircot-rescored-review.json
```

本轮运行使用 `run` 子命令，统一追加以下参数；IRCoT 另传对应 `--prompt-file` 与 `--demo-count 4`。重新执行会产生费用，必须使用新目录；同身份 `--resume` 不会自动重跑失败题。

```text
--allow-remote --direct --max-steps 6 --k 5
--max-model-calls 24 --max-search-calls 8
--max-output-tokens 8192 --context-window 131072
--token-budget 256000 --deadline-seconds 600
```

## 正式对照前的阻塞项

1. 修复结构化响应适配，并补纯 JSON、fenced JSON、解释包装、多个候选对象与非法依赖图的回归测试。改变解析/请求策略后必须建立新实验身份，不能覆盖本轮分数，也不能仅重跑失败题后称为新的完整同条件对照。
2. 增加脱敏的模型错误类别与可用 usage 保留，区分 HTTP 超时、限流、截断、服务端错误；当前 1 例模型错误原因和 4 次 usage 尚不完整。
3. 在独立开发题上检查 IRCoT 终止和重复检索，再冻结正式预算。单纯增加预算无法证明性能改善，更不能给不同方法暗中配置不同上限。
4. 上述修复验收后再扩展每数据集约 100 题，并接入项目自身方法；这批 10 题已经被人工检查，应作为开发验收集，不再作为未经接触的最终测试集。

本轮没有实现或验证规则条件提取、校准丢弃、注入门控，也没有图像向量实验。
