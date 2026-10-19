# Agentic RAG 配对消融与阶段评估

入口：`python -m deepsearch_agent.benchmarks.ablation`。实验仅写入指定的 benchmark/evaluation 目录，不修改应用资料库、对话或生产环境配置。归因标准见 [BADCASE_TAXONOMY.md](BADCASE_TAXONOMY.md)。

如需按任务依赖评分，可用独立入口 `python -m deepsearch_agent.benchmarks.dag` 对同一批记录进行离线 DAG 评估，定义、口径和命令见 [DAG 任务评测](DAG_BENCHMARK.md)。原始分数不被替换，新增节点分数与阻塞诊断保存到新目录。

报告生成支持按 `run.json` 冻结的子集，并可合并对应 DAG 输出；会核对预测/运行指纹和重新计算的节点结果，拒绝混用其他运行。示例：

```powershell
.venv\Scripts\python.exe -m deepsearch_agent.benchmarks.ablation_report --prepared data/benchmarks/ablation-20261005 --run data/evaluations/ablation-20261007-small --dag data/evaluations/dag-20261007-small --output docs/ABLATION_20261007_SMALL.md
```

2026-10-07 小批量实验的固定样本、预算与排除范围见 [预登记](plans/2026-10-07-small-ablation.md)。图像向量不在该轮评估范围，BM25/OCR 召回、多模态重排与角色原图阅读分别保留实际配置。

该轮已完成 6 题 x 5 组共 30 条真实轨迹，见 [结论与 Badcase 复核](ABLATION_20261007_FINDINGS.md) 和 [完整阶段/DAG 报告](ABLATION_20261007_SMALL.md)。小样本结果用于开发诊断，不与旧模型或后训练实验指标混算。

## 实验单位与控制条件

单位是 `(case_id, variant)`，固定 12 道题：ChartQA human/augmented 各 3；PDFVQA A counting 1、existence 2；B object recognition 1、structural understanding 2。按题型和种子抽样，不按模型历史表现选择。PDFVQA 的页级镜像不能支持 Task C 的跨页文档评价。

| 组 | 干预 |
| --- | --- |
| fixed_workflow | 原查询规划 Researcher，不使用新工具循环；保留 Supervisor/Answerer/Verifier |
| agent_full | LangGraph 工具循环，在线 OCR、视觉重排、角色原图阅读均可用 |
| agent_no_rerank | 候选池仍为生产逻辑的最多 4k 条，只保留原顺序前 k 条 |
| agent_no_ocr | 不向规划器暴露在线 OCR 工具；统一索引中的预处理 OCR 仍保留 |
| agent_no_visual | 仅关闭 Researcher/Answerer/Verifier 的原图输入，保留在线 OCR 和视觉重排 |
| oracle | 直接给定正确输入图像与任务允许的元素目录，单列阅读参照，不算普通消融组 |

所有角色使用相同云端视觉模型的文本/图像接口，避免生产默认的文本模型与视觉模型不同造成混淆。每题最多 18 次生成模型请求，agent 最多 6 次工具调用、每轮 4 步、在线 OCR 1 次。工作流和评分的 k=3，模型仍可按生产工具 schema 请求其他合法 top-k，实际参数记录在 trace；同一查询/top-k 下的候选获取规则一致，不保证独立轨迹产生完全相同的查询与候选。单题 runtime OCR 不跨变体共享缓存。保留生产调度和核验逻辑，仅在角色指令中加入统一的基准输出适配：单个带引用 claim 的 text 是任务 JSON。技能、记忆、外部 MCP 不参与本轮。

ChartQA 采用选中图表加 6 张随机干扰图的跨图召回诊断，原问题可能含“此图”等指代，所以不是官方给图问答成绩。PDFVQA A/B 限定原题提供页面，页命中率可能天然很高，不能称为开放检索能力。元素文本覆盖、实际上下文和最终答案分开统计。

## 运行

以下路径为示例，新一轮应使用新目录。`--direct` 仅对当前 Python 进程绕过代理。

```powershell
.venv\Scripts\python.exe -m deepsearch_agent.benchmarks.ablation prepare --prepared data/benchmarks/ablation-new
.venv\Scripts\python.exe -m deepsearch_agent.benchmarks.ablation index --prepared data/benchmarks/ablation-new --retrieval bm25-image --allow-remote --direct
.venv\Scripts\python.exe -m deepsearch_agent.benchmarks.ablation run --prepared data/benchmarks/ablation-new --output data/evaluations/ablation-new --allow-remote --direct --limit-jobs 4
.venv\Scripts\python.exe -m deepsearch_agent.benchmarks.ablation run --prepared data/benchmarks/ablation-new --output data/evaluations/ablation-new --allow-remote --direct --resume
.venv\Scripts\python.exe -m deepsearch_agent.benchmarks.ablation score --prepared data/benchmarks/ablation-new --output data/evaluations/ablation-new
```

2026-10-05 图像 embedding 服务返回 HTTP 402；本轮所有组明确改用 `--retrieval bm25`，复用已完成的 OCR 索引文本，运行记录设置 `--retrieval-fallback-reason image_embedding_http_402`。没有用模拟图像向量代替真实向量，没有声称完成直接图像向量检索验收。后续恢复服务应重建新实验，不与本轮混算。

需要独立验证各工具是否可执行时运行 `probe --prepared ... --output .../tool-probes.jsonl --allow-remote --direct`。该过程人为指定页面并依次调用工具，和 agent 自主工具选择、自然调用成功率完全分开。

定向回归可重复传入 `--case-id` 并通过 `--variants agent_no_ocr` 选择变体；完整计划写入 `run.json`，评分只使用计划内样本，不把未选择的题记为缺失。`--model`、`--context-window`、`--timeout-seconds` 只覆盖该次实验，不编辑应用环境文件。`--ocr-output-tokens` 独立控制在线 OCR 预算，用于复现旧配置。

跨运行对照使用 `python -m deepsearch_agent.benchmarks.paired_regression --prepared ... --before ... --after ... --output .../comparison.json`。比较器拒绝数据或索引指纹不一致，显式列出代码与配置变化；胜负翻转是观察结果，不自动解释为已证实的因果收益。当前小批量计划见 [配对回归计划](plans/2026-10-06-paired-badcase-regression.md)。

## 可检查产物

- `plan.json` / 两份 manifest：抽样规则、题目 ID、原始输入与校验和。
- 两份 `index.json`：独立候选索引及实际召回方式；`preparation-trace.json` 保留预处理模型调用。
- `run.json`：代码、数据、索引指纹及运行条件。恢复要求完全一致；纯 Markdown 报告生成模块不进入轨迹代码指纹。
- `predictions.jsonl`：每条轨迹的结果与调用 trace，失败不删除、不挑选重试。
- `summary.json`：各任务分母、答案、检索、重排、工具、核验、调用量和延时。
- `badcases.jsonl`：一条主标签、多条次标签、证据位置、自动判断副本及人工复核槽位。

## 阶段口径

1. **候选召回**：初次搜索 Recall/MRR@k；多次查询的候选并集召回。候选并集命中不代表后续使用了证据。
2. **重排**：同一次调用的前后 Recall/MRR/nDCG；明确为调用加权诊断，不与逐题指标混用。
3. **工具**：按名称记录成功/错误码、参数、返回证据、新增片段和完全相同参数重复调用。成功返回不等于取证有效。
4. **证据收集**：`evidence_ranking` 与 `evidence_element_ids` 是已收集证据；异常终止时保留已观察值，完整性另记。
5. **模型上下文**：`context_ranking` 与 `context_element_ids` 来自最后 Answerer 的实际输入；`original_image_pages` 区分原图是否送入回答角色。片段截断后，收集覆盖和上下文覆盖可能不同。
6. **生成**：ChartQA relaxed accuracy、PDFVQA A accuracy、B 元素 exact match/F1，分开报告。失败、格式错误、弃答都占分母。
7. **核验**：比较可解析的最后草稿与最终结果；核验器通过仅代表模型自检，不是人工事实核验。

成本只汇总提供方实际返回的生成 token；未知 usage 不计为 0。预算拦截不计为已发送云请求。预处理、在线轨迹、独立工具验收分开；embedding 请求及货币费用不能从生成 token 推算。单次小样本配对差异是排查线索，不支持稳定优劣或显著提升的结论。

## Badcase 复核纪律

自动标签定位的是可观察失败阶段。没有原图、OCR 原文或计算证据时，不自动把“有正确页但答错”细分为视觉、OCR 或推理错误。`expert_override` 未填写即未完成专家审阅；由代码或助手做的日志复核也应明确标记审阅者，不能冒充人工专家。

人工复核时保留原 `automatic`，附预测文件行号与 trace JSON 路径。即使基准草稿碰巧答对，Verifier 因引用不足拒绝也未必错误，应核查支持关系。修复一个推定原因后，只有同题反事实重跑才能进一步支持因果解释。
