# 优先复用现有数据集

当前入口只使用本地已有数据、标签和历史输出，不下载新数据，不调用模型，不改动生产服务、业务数据库或原始实验文件。清单固定在 `eval/existing-datasets-v1.json`，离线实现位于 `deepsearch_experiments.existing_datasets`；生产应用不依赖该入口。

随后已使用 MiMo-V2.6-Pro 完成 220 题真实请求评测，结果与未完成项见 [2026-10-18 真实评测报告](EVALUATION_EXISTING_DATASETS_20261018.md)。下文的离线重评分结果仍保留为独立历史基线。

后续已为 MuSiQue 和 2Wiki 各新增 100 道不重叠题目并完成真实评测，累计覆盖 420 个题目样本；采样协议、分层结果及失败分类见 [扩容评测](EXPANDED_EVALUATION_20261018.md)。

## 数据选择与适用范围

| 数据 | 本地位置 | 当前规模 | 用途与边界 |
|---|---|---:|---|
| ChartQA | `data/benchmarks/chartqa-val100/manifest.json` | 100 题、98 图；human/augmented 各 50 | 图表读图与回答；给定正确图像的组件评估 |
| PDFVQA | `data/benchmarks/pdfvqa-val100/manifest.json` | 100 题、74 页；A/B 各 50 | 页面理解与元素定位；公共镜像过滤子集，B 使用 OCR 和元素目录，无 Task C/完整文档评估 |
| MuSiQue | `data/benchmarks/multihop/prepared/musique/pilot-10.json` | 10 题、11,656 段语料 | 父子 Agent、检索、核验和预算的多跳试验；`sampled_pool` |
| 2WikiMultiHopQA | `data/benchmarks/multihop/prepared/2wikimultihopqa/pilot-10.json` | 10 题、6,119 段语料 | 同上；`sampled_pool` |
| 本地检索 fixture | `eval/retrieval-v1.json` | 40 题、未人工复核 | 继续通过原有检索入口做快速回归，不纳入本次公开数据评分；来自虚构小说和合成章节文档，不能代表论文问答水平 |

四套公开数据共 220 个不同问题。冻结的 10 个历史运行共 275 个“运行 × 问题”记录：ChartQA/PDFVQA 各一个 100 题运行、ChartQA 10 题重排诊断、PDFVQA 5 题检索诊断，以及 MuSiQue/2Wiki 各三个 10 题 Agent 版本。诊断样本和方法变体复用了问题，不能把 275 当成独立问题数。

6 题的多模态消融集、1 题多跳 canary 和已有失败重试仍保留原位。本次不拿小集替换 100 题或 10 题样本，也不挑选后来的成功重试来改善历史分数。暂不新增 QASPER、ViDoRe、ALCE。

## 运行方式

在项目根目录运行，输出必须为新目录：

```powershell
.venv/Scripts/python.exe -m deepsearch_experiments.existing_datasets `
  --catalog eval/existing-datasets-v1.json `
  --output output/existing-datasets-next
```

从其他目录运行时，`--root` 指向项目根目录，同时对 `--catalog`、`--output` 使用明确路径。`--root` 不会改变当前目录。没有网络或真实密钥也可重评分，但需要项目已安装的实验依赖及清单中本地文件。

入口复用原有 manifest 资产检查、`summarize` 和 `score_run`，不实现另一套评分公式。先核对 manifest、run.json、predictions/attempts 的冻结 SHA256，再核对运行中的数据身份与题目列表；图像内容按 manifest 指纹校验。输入变更、未知/重复题号、预测模式不匹配或资产损坏均拒绝该运行，报告保留错误，进程非零退出。正常输出 `status=scored` 只表示重评分完成。

清单中的三个原始文件指纹是字节 SHA256；多跳 scorer 内部还验证规范化 manifest 的语义 SHA256，两者不是同一个指纹字段。历史实现身份和当前 scorer 源码身份分别保留。

## 输出与复核

- `report.json`：逐运行摘要、模型名称、源码/清单身份、输入不变校验；没有跨数据集的总体准确率。
- `reports/<entry-id>.json`：原评分器分数、逐题结果、数据和源文件指纹；多模态按 task/group 拆分，多跳保留完整历史运行条件和全尝试成本。
- `success.jsonl`：自动主指标等于 1。
- `failure.jsonl`：已有预测的自动主指标小于 1，包括执行失败和部分匹配。
- `unknown.jsonl`：缺失预测，保留在指标分母并记零分。

这是**benchmark 自动评分分组**。每条同时保存 `execution_status`、`internal_verifier`、`human_verdict`、`root_causes`；没有对应审核信息时后三者为 null。EM 不匹配不能直接判成幻觉或通信失真，工具成功也不能当成人工正确。重排诊断的主指标是金标页面 Recall@3，不能解释成答案正确率。

待复核记录包含问题、金标、精简预测、主指标、运行/预测/题目指纹和原始运行目录引用。完整工具事件继续保留在原始 attempts 中，不复制到总报告。多跳失败重试的全部成本仍计入 `total_model_calls` 等统计；未知供应商用量保持 null。缺失预测与错误不会从分母移除。

以 MuSiQue 为例，筛选某个版本待复核的题目：

```powershell
Get-Content output/existing-datasets-20261018/failure.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object entry_id -eq 'musique-mainline-budget-bounded' |
  Select-Object case_id, execution_status, diagnostic_observation, source_run
```

人工标签应保存为新文件，并绑定 entry_id、case_id、case_sha256、run_sha256、records_sha256，填写审核人、证据、期望和修复引用；不要覆盖这次导出。修复后的候选用**同一 manifest 和完整题目范围**运行，写入新的实验目录，保留失败尝试与实际模型配置，再通过原有评分器复核。若代码、模型、语料或预算变化，必须明确列出，不将其解释成单一模块的因果改进。

生产业务的轨迹冻结、人工 review 和 resolved/regression 判定继续走 [质量闭环](RELIABILITY_RECOVERY_QUALITY.md)。这里的历史 benchmark 记录不伪装成生产轨迹，也不能直接当作 `deepsearch_tracing.quality review` 的 labels；缺少原始生产轨迹或人工依据时保持未知。

## 2026-10-18 实际重评分

结果目录：`output/existing-datasets-20261018`。10/10 运行成功重评分，197 个源文件身份校验通过且保持不变，本轮新增模型调用 0 次。

历史多模态输出使用 **mimo-v2.5**：

| 运行/任务 | N | 指标 | 分数 | 执行错误 |
|---|---:|---|---:|---:|
| ChartQA oracle | 100 | relaxed accuracy / exact match | 56.00% / 51.00% | 1 |
| ChartQA human | 50 | relaxed accuracy | 66.00% | 1 |
| ChartQA augmented | 50 | relaxed accuracy | 46.00% | 0 |
| PDFVQA A oracle | 50 | accuracy | 40.00% | 1 |
| PDFVQA B oracle | 50 | element EM / F1 | 8.00% / 31.58% | 2 |
| ChartQA 重排诊断 | 10 | Recall@3 / MRR@3 / nDCG@3 | 60.00% / 60.00% / 60.00% | 0 |
| PDFVQA 检索诊断 | 5 | page Recall@3 | 60.00% | 0 |

ChartQA 重排使用金标 + 7 张干扰图的受控候选池；PDFVQA 检索诊断是给定页面范围的 A4/B1，不据此声称自然语料库检索或完整 Agent 效果。PDFVQA A 的标签分布偏向 no，历史多数类基线为 64%；当前 40% 需要结合任务分布复核，不能仅凭单个总体数描述能力。

历史多跳输出使用 **mimo-v2.6-pro**，每行固定 10 题：

| 数据 | 方法 | Answer EM | Answer F1 | 执行失败 | 历史模型调用 |
|---|---|---:|---:|---:|---:|
| MuSiQue | mainline | 10.00% | 24.67% | 5 | 107 |
| MuSiQue | budget-visible | 30.00% | 44.67% | 4 | 163 |
| MuSiQue | budget-bounded | 30.00% | 54.33% | 2 | 120 |
| 2Wiki | mainline | 80.00% | 89.23% | 0 | 92 |
| 2Wiki | budget-visible | 80.00% | 97.23% | 0 | 95 |
| 2Wiki | budget-bounded | 80.00% | 89.23% | 0 | 92 |

这组多跳结果使用固定 BM25，图像、Wiki、记忆、skills 和 MCP 未启用。它适合定位执行/预算失败与回答差异，不是官方完整榜单，也不覆盖 HTTP、业务数据库恢复或图像 embedding。文档支持召回是 presence proxy，不证明证据被实际使用；未实现官方句子支持、证据三元组/joint、MuSiQueFull sufficiency。小样本和历史版本差异不支持统计显著性或当前代码效果提升结论。

275 条记录自动分组为 success=119、failure=156、unknown=0；这是任务与方法混合后的记录数量，不能计算为总体准确率。执行状态为 ok=260、error=8、budget_exhausted=6、invalid_output=1；人工审核已知数量为 0。没有缺失预测不代表人工正确性已知。

优先复核 MuSiQue 的预算、服务和输出失败，再复核“已检索到支持段落但答案不匹配”的记录；阶段观察只用于筛选，根因需逐条核对完整执行证据。新图像 embedding 的效果尚未运行，后续也优先在现有数据和固定候选范围上比较。

## 验收记录

```powershell
New-Item -ItemType Directory -Path output/existing-datasets-tests-next | Out-Null
.venv/Scripts/python.exe -m pytest tests/test_existing_datasets.py `
  tests/test_public_multimodal_evaluation.py tests/test_multihop_runner.py `
  tests/test_architecture.py -q `
  --basetemp output/existing-datasets-tests-next/pytest-temp
.venv/Scripts/python.exe -m deepsearch_experiments.architecture
```

实际结果：89 passed in 12.28s；日志 `output/existing-datasets-tests-20261018/pytest-02.log`。覆盖子集分母、缺失/失败分开、指纹/资产/题号/模式拒绝、不可覆盖、重试成本、无网络和生产依赖隔离。架构检查为 194 modules、464 edges、0 cycles、0 violations。首次测试因临时目录父目录未创建而发生 setup 错误，创建后重跑通过；未删除测试或改变断言来消除错误。本轮未运行新模型候选。
