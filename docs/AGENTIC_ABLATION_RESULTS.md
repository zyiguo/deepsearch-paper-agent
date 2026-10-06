# Agentic RAG 小批量消融实验报告

生成日期：2026-10-06（北京时间）。实际完成 **72/72** 个题目-配置组合，本轮已全部完成。

固定样本：ChartQA 6 题，PDFVQA A 3 题，PDFVQA B 3 题。下面均由本轮原始记录离线计算；未合并不同任务的答案指标。

## 本轮结论

完整 agent：ChartQA 33.3% (2/6)，PDF A 33.3% (1/3)，PDF B 集合 EM 0.0% (0/3)。这里只描述这 12 道题的一次运行，不证明组件稳定优劣。

已完成的 60 条 RAG 轨迹自动主标签包括：格式失败 15，预算耗尽 9，弃答 11，待细分的阅读/推理失败 12。因此不能把全部错题都算作召回问题，也不能把“有页仍答错”自动解释成 OCR 错误。

自然工具调用中正常返回 160/177 次。这是执行成功率，不是工具选择准确率或答案正确率；后文分别检查新增证据、重复调用、金页命中和最终回答。

本轮图像 embedding 服务 HTTP 402，**统一采用 BM25＋预处理 OCR 召回**；多模态重排和原图阅读仍参与，直接图像向量检索未验收。

## 数据与干预

| 项目 | 本轮记录 |
| --- | --- |
| 模型 | mimo-v2.5 |
| 检索通道 | {'chartqa': 'bm25', 'pdfvqa': 'bm25'} |
| 检索 top-k | 3 |
| 单题模型请求预算 | 18 |
| 上下文 / 最大输出 Token | 32768 / 2048 |
| Agent 预算 | {"enabled": true, "max_tool_calls": 6, "max_steps_per_round": 4, "max_ocr_calls": 1, "max_evidence": 12, "max_images": 6, "timeout_seconds": 600, "observation_chars": 12000} |
| 关闭能力 | memory, skills, external_MCP |
| 运行期 OCR 缓存 | no cross-case or cross-variant runtime cache |
| 图像干预的配置原文 | disable original images for Researcher/Answerer/Verifier; retain OCR and visual reranker |

| 配置 | 干预 |
| --- | --- |
| fixed_workflow | 固定检索工作流 |
| agent_full | 完整工具决策 |
| agent_no_rerank | 关闭重排 |
| agent_no_ocr | 关闭在线 OCR 工具 |
| agent_no_visual | 关闭研究/回答/核验角色原图 |

所有实验组共用同一固定语料和索引；关闭在线 OCR 工具并不删除预处理 OCR。关闭角色原图输入仍保留其余明确配置的能力，不能解释为整个系统没有视觉信息。固定工作流仍包含 Supervisor/Answerer/Verifier，仅研究阶段使用原有固定检索路径。

本轮使用 BM25 检索，**直接图像向量检索未评估**。图像嵌入接口返回 HTTP 402，统一索引构建后改用同一 BM25 配置完成各组。

PDFVQA A/B 是已给定页面的任务，页级召回只说明该页片段是否被返回，不能衡量多文档选页。ChartQA 的跨图检索包含依赖“这张图”等指代的问题，缺少原任务给定的目标图，因此属于受控检索诊断，不能直接比较官方给定图像成绩。样本很小且每个配置只运行一次；抽样来自本地验证子集，PDF 页面镜像的元素匹配规则也限制了推广范围。本轮 memory、skills、外部 MCP 按配置关闭，不代表这些能力已经通过端到端质量评测。

## 答案效果

| 配置 | ChartQA relaxed accuracy | PDF A accuracy | PDF B 集合 EM | PDF B 集合 F1 | 错误 / 弃答 / 缺失 |
| --- | --- | --- | --- | --- | --- |
| fixed_workflow | 0.0% (0/6) | 0.0% (0/3) | 0.0% (0/3) | 0.0000 (n=3) | 6 / 4 / 0 |
| agent_full | 33.3% (2/6) | 33.3% (1/3) | 0.0% (0/3) | 0.0000 (n=3) | 4 / 4 / 0 |
| agent_no_rerank | 0.0% (0/6) | 0.0% (0/3) | 0.0% (0/3) | 0.1667 (n=3) | 5 / 1 / 0 |
| agent_no_ocr | 0.0% (0/6) | 0.0% (0/3) | 0.0% (0/3) | 0.4389 (n=3) | 6 / 2 / 0 |
| agent_no_visual | 0.0% (0/6) | 0.0% (0/3) | 0.0% (0/3) | 0.2222 (n=3) | 7 / 1 / 0 |

错误包括服务失败与无效输出，弃答（abstention）单列；三者均保留在全题分母并计零。ChartQA relaxed accuracy 使用既有 5% 数值容差规则；PDF A 为固定答案准确率；PDF B 为元素 ID 集合匹配及逐题集合 F1。F1 表格中的 n 是题数，不是元素数。无效格式不会通过事后猜测或答案改写补分。

### 给定图像阅读参考

| 配置 | ChartQA relaxed accuracy | PDF A accuracy | PDF B 集合 EM | PDF B 集合 F1 | 错误 / 缺失 |
| --- | --- | --- | --- | --- | --- |
| oracle | 66.7% (4/6) | 33.3% (1/3) | 0.0% (0/3) | 0.4444 (n=3) | 0 / 0 |

oracle 直接给定题目对应原图，并按任务提供输入元素目录，是独立的阅读参考，**不保证是上界**。其提示词与角色流程不同；给定页面也不是模型实际产生的引用。因此不将 oracle 的页命中、引用率或相对差值当作普通组件消融。

### 题型与标签分布

| 配置 | ChartQA human | ChartQA augmented |
| --- | --- | --- |
| fixed_workflow | 0.0% (0/3) | 0.0% (0/3) |
| agent_full | 0.0% (0/3) | 66.7% (2/3) |
| agent_no_rerank | 0.0% (0/3) | 0.0% (0/3) |
| agent_no_ocr | 0.0% (0/3) | 0.0% (0/3) |
| agent_no_visual | 0.0% (0/3) | 0.0% (0/3) |
| oracle | 66.7% (2/3) | 66.7% (2/3) |

human 子集中的泛指问题依赖给定原图，跨图检索任务本身不充分；augmented 子集中的实体和时间通常更明确。两组仍都保留，不按结果删题。

PDFVQA A 金标分布：{"0": 1, "no": 2}；本子集固定回答“no”可得 2/3。这是标签分布的描述性基线，不是检索结果。

## 逐阶段评估

### 检索与证据保留

| 配置 | 全部候选召回 | 首次 Recall@k | 首次 MRR@k | 工具后金页命中 | 已收集金页命中 | 最终引用页精确率 | 最终引用页覆盖 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | 0.9167 (n=12) | 0.4167 (n=12) | 0.4167 (n=12) | 0.0% (0/12) | 66.7% (8/12) | 0.2500 (n=12) | 0.2500 (n=12) |
| agent_full | 0.7500 (n=12) | 0.5000 (n=12) | 0.5000 (n=12) | 91.7% (11/12) | 91.7% (11/12) | 0.2500 (n=12) | 0.2500 (n=12) |
| agent_no_rerank | 0.6667 (n=12) | 0.5000 (n=12) | 0.5000 (n=12) | 91.7% (11/12) | 91.7% (11/12) | 0.2500 (n=12) | 0.2500 (n=12) |
| agent_no_ocr | 0.6667 (n=12) | 0.5833 (n=12) | 0.5833 (n=12) | 83.3% (10/12) | 83.3% (10/12) | 0.5000 (n=12) | 0.5000 (n=12) |
| agent_no_visual | 0.7500 (n=12) | 0.5000 (n=12) | 0.5000 (n=12) | 100.0% (12/12) | 100.0% (12/12) | 0.2500 (n=12) | 0.2500 (n=12) |

这一表按有金标准页的题目平均，表内保留各自 n。全部候选召回使用各次成功检索的去重页并集；首次指标只取第一次检索。固定工作流没有工具节点，其“工具后”列不应解释为检索失败。最终引用页指标只是与金标准页的重合代理，不验证引用是否真正支持结论。

| 配置 | PDF B 检索文本元素覆盖 | PDF B 已收集文本元素覆盖 |
| --- | --- | --- |
| fixed_workflow | 1.0000 (n=3) | 0.6667 (n=3) |
| agent_full | 0.3333 (n=3) | 1.0000 (n=3) |
| agent_no_rerank | 0.3333 (n=3) | 1.0000 (n=3) |
| agent_no_ocr | 0.3333 (n=3) | 1.0000 (n=3) |
| agent_no_visual | 0.3333 (n=3) | 1.0000 (n=3) |

元素覆盖只检查文本中记录的目录 ID，不证明图像可读、模型理解正确或元素完整进入截断后的实际提示。缺少该观测字段时不按零覆盖处理；已收集证据与模型实际上下文的差别需结合提示记录检查。

| 配置 | 收集金页覆盖 | 最后回答上下文金页覆盖 | 最后回答原图金页覆盖 | PDF B 上下文文本元素覆盖 |
| --- | --- | --- | --- | --- |
| fixed_workflow | 0.6667 (n=12) | 0.4167 (n=12) | 0.3333 (n=12) | 0.0000 (n=3) |
| agent_full | 0.9167 (n=12) | 0.7500 (n=12) | 0.7500 (n=12) | 0.6667 (n=3) |
| agent_no_rerank | 0.9167 (n=12) | 0.7500 (n=12) | 0.7500 (n=12) | 0.6667 (n=3) |
| agent_no_ocr | 0.8333 (n=12) | 0.6667 (n=12) | 0.6667 (n=12) | 1.0000 (n=3) |
| agent_no_visual | 1.0000 (n=12) | 0.8333 (n=12) | 0.0000 (n=12) | 1.0000 (n=3) |

这些覆盖率只对相应字段有观测的固定题目计算，n 单列。上下文指最后准备给 Answerer 的输入；实际请求状态见 answerer_request_status。原图列仅统计对应模型请求中确实包含图片、且未被预算拦截的页面。no_visual 的原图覆盖为零是实验干预，不是未完成调用。

### 重排

| 配置 | 调用数 | 前 Recall@k | 后 Recall@k | 前 MRR@k | 后 MRR@k | 状态计数 |
| --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | 51 | 0.6863 (n=51) | 0.7059 (n=51) | 0.6765 (n=51) | 0.6830 (n=51) | {"ok_multimodal": 48, "fallback_multimodal": 3} |
| agent_full | 11 | 0.7273 (n=11) | 0.8182 (n=11) | 0.6818 (n=11) | 0.8182 (n=11) | {"ok_multimodal": 10, "fallback_multimodal": 1} |
| agent_no_rerank | 19 | 0.7895 (n=19) | 0.7895 (n=19) | 0.7632 (n=19) | 0.7632 (n=19) | {"disabled_control": 19} |
| agent_no_ocr | 14 | 0.7857 (n=14) | 0.7857 (n=14) | 0.7857 (n=14) | 0.7857 (n=14) | {"ok_multimodal": 13, "fallback_multimodal": 1} |
| agent_no_visual | 12 | 0.7500 (n=12) | 1.0000 (n=12) | 0.7500 (n=12) | 0.9028 (n=12) | {"ok_multimodal": 11, "fallback_multimodal": 1} |

重排表按实际调用加权，属于诊断；同一道题的多次重排会重复贡献，不能当作独立题目样本。关闭重排组在相同查询和 top-k 下保留相同候选获取规则；不同模型轨迹仍可能产生不同查询、top-k 和调用次数，不保证候选完全相同。单候选可能直接跳过重排。

### 工具调用

| 配置 | 工具 | 尝试 | 成功 | 失败 | 参数无效 | 相同调用重复 | 新增证据调用 | 金页命中 | 错误码 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| agent_full | search_evidence | 14 | 14 | 0 | 0 | 0 | 10 | 78.6% (11/14) | {} |
| agent_full | list_documents | 5 | 5 | 0 | 0 | 0 | 0 | 未观测 (n=0) | {} |
| agent_full | ocr_page | 12 | 7 | 5 | 0 | 2 | 7 | 50.0% (6/12) | {"ocr_budget": 3, "source_not_allowed": 1, "tool_failed": 1} |
| agent_full | read_page | 5 | 5 | 0 | 0 | 1 | 3 | 80.0% (4/5) | {} |
| agent_full | get_evidence | 4 | 4 | 0 | 0 | 0 | 0 | 75.0% (3/4) | {} |
| agent_no_rerank | search_evidence | 23 | 23 | 0 | 0 | 0 | 12 | 73.9% (17/23) | {} |
| agent_no_rerank | list_documents | 4 | 4 | 0 | 0 | 0 | 0 | 未观测 (n=0) | {} |
| agent_no_rerank | ocr_page | 9 | 8 | 1 | 0 | 1 | 8 | 77.8% (7/9) | {"ocr_budget": 1} |
| agent_no_rerank | read_page | 6 | 6 | 0 | 0 | 0 | 5 | 83.3% (5/6) | {} |
| agent_no_rerank | get_evidence | 3 | 3 | 0 | 0 | 0 | 0 | 100.0% (3/3) | {} |
| agent_no_ocr | search_evidence | 22 | 21 | 1 | 0 | 1 | 11 | 59.1% (13/22) | {"source_not_allowed": 1} |
| agent_no_ocr | list_documents | 6 | 6 | 0 | 0 | 0 | 0 | 未观测 (n=0) | {} |
| agent_no_ocr | read_page | 8 | 8 | 0 | 0 | 1 | 4 | 62.5% (5/8) | {} |
| agent_no_ocr | get_evidence | 6 | 6 | 0 | 0 | 0 | 0 | 66.7% (4/6) | {} |
| agent_no_visual | search_evidence | 15 | 14 | 1 | 0 | 0 | 11 | 80.0% (12/15) | {"source_not_allowed": 1} |
| agent_no_visual | read_page | 9 | 9 | 0 | 0 | 3 | 4 | 77.8% (7/9) | {} |
| agent_no_visual | ocr_page | 16 | 7 | 9 | 0 | 5 | 7 | 31.2% (5/16) | {"tool_failed": 2, "ocr_budget": 6, "source_not_allowed": 1} |
| agent_no_visual | get_evidence | 7 | 7 | 0 | 0 | 0 | 0 | 71.4% (5/7) | {} |
| agent_no_visual | list_documents | 3 | 3 | 0 | 0 | 0 | 0 | 未观测 (n=0) | {} |

工具成功表示调用正常返回，不等于答案正确。重复调用按单题内同工具及规范化参数判断；新增证据依赖新的 chunk_id。金页命中仅在工具有明确排名观测时计算，目录查询等无页排名工具不凭空记为零。

### 生成与核验

| 配置 | ChartQA 已获金页条件准确率 | PDF A 已获金页条件准确率 | PDF B 已获金页条件 EM | 核验接受 claim | 正确草稿→错误终答 | 错误草稿→正确终答 |
| --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | 0.0% (0/4) | 0.0% (0/2) | 0.0% (0/2) | 100.0% (4/4) | 0 | 0 |
| agent_full | 40.0% (2/5) | 33.3% (1/3) | 0.0% (0/3) | 57.1% (4/7) | 0 | 0 |
| agent_no_rerank | 0.0% (0/5) | 0.0% (0/3) | 0.0% (0/3) | 100.0% (6/6) | 0 | 0 |
| agent_no_ocr | 0.0% (0/4) | 0.0% (0/3) | 0.0% (0/3) | 87.5% (7/8) | 1 | 0 |
| agent_no_visual | 0.0% (0/6) | 0.0% (0/3) | 0.0% (0/3) | 100.0% (6/6) | 0 | 0 |

条件准确率分母只包含明确收集到金标准页的题目，不能代表全样本质量。核验接受率是模型自检结果，不是人工忠实性评价。草稿到终答变化仅覆盖可以解析为单一任务答案的最终草稿；这种变化记录不证明核验模块的因果贡献。

## 调用、Token 与延迟

| 配置 | 请求 / 预算拒绝 | 成功模型调用 | 已上报总 Token | Token 已上报 / 请求 | 题目延迟均值 / P50 / P95 秒 |
| --- | --- | --- | --- | --- | --- |
| fixed_workflow | 141 / 3 | 140 | 456457 | 140 / 141 | 112.39 / 77.86 / 273.70 |
| agent_full | 170 / 2 | 167 | 772714 | 167 / 170 | 106.71 / 92.37 / 176.76 |
| agent_no_rerank | 140 / 2 | 139 | 726157 | 138 / 140 | 80.78 / 67.72 / 170.23 |
| agent_no_ocr | 141 / 1 | 141 | 508010 | 139 / 141 | 56.07 / 60.15 / 85.53 |
| agent_no_visual | 153 / 2 | 150 | 578073 | 150 / 153 | 94.17 / 69.85 / 211.33 |
| oracle | 12 / 0 | 12 | 12785 | 12 / 12 | 4.19 / 2.13 / 13.15 |

仅汇总服务端实际返回的 Token；“未上报”不是零费用，失败请求也可能产生费用。模型请求不包含发送前预算拒绝。这里不含图像嵌入接口的调用量或 Token 账单，也未换算货币成本。延迟是本机及本轮并发、网络条件下的实测，不能作为独立硬件性能结论。

| 配置 | 阶段 | 模型请求数 | 已上报总 Token | Token 已上报请求数 |
| --- | --- | --- | --- | --- |
| fixed_workflow | answerer | 8 | 36191 | 8 |
| fixed_workflow | rerank | 61 | 270720 | 60 |
| fixed_workflow | researcher | 23 | 11548 | 23 |
| fixed_workflow | supervisor | 41 | 102073 | 41 |
| fixed_workflow | verifier | 8 | 35925 | 8 |
| agent_full | answerer | 26 | 163991 | 26 |
| agent_full | rerank | 16 | 69295 | 15 |
| agent_full | researcher | 45 | 170700 | 45 |
| agent_full | supervisor | 49 | 188972 | 48 |
| agent_full | tool:ocr_page | 8 | 15809 | 7 |
| agent_full | verifier | 26 | 163947 | 26 |
| agent_no_rerank | answerer | 19 | 151183 | 19 |
| agent_no_rerank | researcher | 50 | 210669 | 50 |
| agent_no_rerank | supervisor | 44 | 201498 | 43 |
| agent_no_rerank | tool:ocr_page | 8 | 17453 | 8 |
| agent_no_rerank | verifier | 19 | 145354 | 18 |
| agent_no_ocr | answerer | 15 | 70983 | 15 |
| agent_no_ocr | rerank | 18 | 76104 | 18 |
| agent_no_ocr | researcher | 49 | 162902 | 47 |
| agent_no_ocr | supervisor | 44 | 127147 | 44 |
| agent_no_ocr | verifier | 15 | 70874 | 15 |
| agent_no_visual | answerer | 16 | 78051 | 16 |
| agent_no_visual | rerank | 20 | 96749 | 20 |
| agent_no_visual | researcher | 53 | 180242 | 53 |
| agent_no_visual | supervisor | 40 | 140041 | 39 |
| agent_no_visual | tool:ocr_page | 9 | 14960 | 8 |
| agent_no_visual | verifier | 15 | 68030 | 14 |

### 索引预处理成本

| 数据集 | 模型请求 | 已上报总 Token | 覆盖 | 原始记录 |
| --- | --- | --- | --- | --- |
| chartqa | 12 | 18263 | 12/12 | [preparation-trace.json](&lt;../data/benchmarks/ablation-20261005/chartqa/preparation-trace.json&gt;) |
| pdfvqa | 0 | 未上报 | 0/0 | [preparation-trace.json](&lt;../data/benchmarks/ablation-20261005/pdfvqa/preparation-trace.json&gt;) |

索引预处理与在线题目运行分开统计。预处理记录可能包含失败索引尝试；不能把已缓存的 OCR 视为本轮在线免费产生，也不能将同一次共享预处理成本重复累加到每个消融组。

### 独立工具验收

| 数据集 | 工具 | 状态 | 新增片段 | 错误码 |
| --- | --- | --- | --- | --- |
| chartqa | list_documents | ok | 0 | 无 |
| chartqa | search_evidence | ok | 0 | 无 |
| chartqa | read_page | ok | 0 | 无 |
| chartqa | ocr_page | ok | 2 | 无 |
| chartqa | get_evidence | ok | 0 | 无 |
| pdfvqa | list_documents | ok | 0 | 无 |
| pdfvqa | search_evidence | ok | 3 | 无 |
| pdfvqa | read_page | ok | 1 | 无 |
| pdfvqa | ocr_page | ok | 1 | 无 |
| pdfvqa | get_evidence | ok | 0 | 无 |

人为指定页面的探针共 2 页，生成请求 2 次，上报 Token 3277（2/2 次有 usage）。这里只验证工具可执行和返回内容，不代表 agent 会自主选对工具，也不证明 OCR 识别准确率。[tool-probes.jsonl](<../data/benchmarks/ablation-20261005/tool-probes.jsonl>)

另有 4 条接入验收轨迹，生成请求 37 次，上报 Token 222090（37/37）。它们来自正式冻结前的验收版本，不并入本报告的效果分母。[predictions.jsonl](<../data/evaluations/ablation-20261005/predictions.jsonl>)

## 配对差异

差值方向为“当前配置 − agent_full”。每一行使用相同固定题目，失败和缺失仍记零；正值只表示本次样本得分较高。Bootstrap 区间是固定 seed 的小样本描述，不能支持稳定性或显著性结论；单次生成随机性与多重比较尚未控制。

| 配置 | 任务 / 指标 | 固定 n | 两组均有记录 | 分数差 | 升 / 降 / 平 | Bootstrap 95% |
| --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | chartqa/relaxed_accuracy | 6 | 6 | -0.3333 | 0/2/4 | [-0.6667, +0.0000] |
| fixed_workflow | A/accuracy | 3 | 3 | -0.3333 | 0/1/2 | [-1.0000, +0.0000] |
| fixed_workflow | B/element_exact_match | 3 | 3 | +0.0000 | 0/0/3 | [+0.0000, +0.0000] |
| fixed_workflow | B/element_f1 | 3 | 3 | +0.0000 | 0/0/3 | [+0.0000, +0.0000] |
| agent_no_rerank | chartqa/relaxed_accuracy | 6 | 6 | -0.3333 | 0/2/4 | [-0.6667, +0.0000] |
| agent_no_rerank | A/accuracy | 3 | 3 | -0.3333 | 0/1/2 | [-1.0000, +0.0000] |
| agent_no_rerank | B/element_exact_match | 3 | 3 | +0.0000 | 0/0/3 | [+0.0000, +0.0000] |
| agent_no_rerank | B/element_f1 | 3 | 3 | +0.1667 | 1/0/2 | [+0.0000, +0.5000] |
| agent_no_ocr | chartqa/relaxed_accuracy | 6 | 6 | -0.3333 | 0/2/4 | [-0.6667, +0.0000] |
| agent_no_ocr | A/accuracy | 3 | 3 | -0.3333 | 0/1/2 | [-1.0000, +0.0000] |
| agent_no_ocr | B/element_exact_match | 3 | 3 | +0.0000 | 0/0/3 | [+0.0000, +0.0000] |
| agent_no_ocr | B/element_f1 | 3 | 3 | +0.4389 | 3/0/0 | [+0.2500, +0.6667] |
| agent_no_visual | chartqa/relaxed_accuracy | 6 | 6 | -0.3333 | 0/2/4 | [-0.6667, +0.0000] |
| agent_no_visual | A/accuracy | 3 | 3 | -0.3333 | 0/1/2 | [-1.0000, +0.0000] |
| agent_no_visual | B/element_exact_match | 3 | 3 | +0.0000 | 0/0/3 | [+0.0000, +0.0000] |
| agent_no_visual | B/element_f1 | 3 | 3 | +0.2222 | 1/0/2 | [+0.0000, +0.6667] |

## 错误归因与逐题追踪

以下是固定规则产生的自动归因，**人工复核待完成**；`confirmed` 只说明记录满足该规则，不代表人工确认完整原因。“generation_reading_or_reasoning”不能进一步区分 OCR、图像阅读和推理；`verifier_rejected_correct` 表示观察到正确结构化草稿被拒绝，也不是完整反事实实验。本轮归因文件有 72 条记录，带人工覆盖记录 0 条。

补充的助手日志/原图抽查见 [Badcase 抽查记录](AGENTIC_ABLATION_AUDIT.md)。它不替代人工专家审核，也不覆盖自动判断。例如基准答案正确但引用越界时，程序拒绝答案不等于核验模型误判；必须查看原始 supported 与程序最终 supported 的差别。

| 配置 | abstention | budget_exhausted | evidence_selection_loss | generation_reading_or_reasoning | model_format_error | retrieval_miss | success | unknown | verifier_rejected_correct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | 4 | 2 | 0 | 2 | 4 | 0 | 0 | 0 | 0 |
| agent_full | 4 | 2 | 0 | 0 | 1 | 1 | 3 | 1 | 0 |
| agent_no_rerank | 1 | 2 | 2 | 3 | 2 | 1 | 0 | 1 | 0 |
| agent_no_ocr | 1 | 1 | 0 | 4 | 5 | 0 | 0 | 0 | 1 |
| agent_no_visual | 1 | 2 | 1 | 3 | 3 | 0 | 0 | 2 | 0 |

| 配置 | 次标签 | 题数 |
| --- | --- | --- |
| fixed_workflow | incomplete_context_element_coverage | 3 |
| fixed_workflow | incomplete_text_element_coverage | 1 |
| fixed_workflow | oracle_correct_rag_failed | 5 |
| fixed_workflow | recovered_model_failure | 1 |
| fixed_workflow | recovered_rerank_drop | 1 |
| fixed_workflow | rerank_drop_observed | 2 |
| fixed_workflow | retrieval_miss_observed | 1 |
| fixed_workflow | verifier_accepted_wrong | 2 |
| agent_full | incomplete_context_element_coverage | 1 |
| agent_full | model_failure_observed | 2 |
| agent_full | oracle_correct_rag_failed | 4 |
| agent_full | recovered_model_failure | 1 |
| agent_full | retrieval_miss_observed | 1 |
| agent_full | tool_failure_observed | 3 |
| agent_full | verifier_accepted_wrong | 1 |
| agent_no_rerank | incomplete_context_element_coverage | 1 |
| agent_no_rerank | model_failure_observed | 1 |
| agent_no_rerank | oracle_correct_rag_failed | 5 |
| agent_no_rerank | retrieval_miss_observed | 1 |
| agent_no_rerank | tool_failure_observed | 1 |
| agent_no_rerank | verifier_accepted_wrong | 6 |
| agent_no_ocr | oracle_correct_rag_failed | 5 |
| agent_no_ocr | recovered_tool_failure | 1 |
| agent_no_ocr | retrieval_miss_observed | 1 |
| agent_no_ocr | verifier_accepted_wrong | 4 |
| agent_no_visual | model_failure_observed | 3 |
| agent_no_visual | oracle_correct_rag_failed | 5 |
| agent_no_visual | tool_failure_observed | 7 |
| agent_no_visual | verifier_accepted_wrong | 4 |
| agent_no_visual | visual_input_disabled | 12 |

### 固定题目

| 题目 ID | 任务 / 分组 | 原问题 | 金标准 |
| --- | --- | --- | --- |
| chartqa/val/human/32 | chartqa/human | Which country data analysed here? | ["cuba"] |
| chartqa/val/human/714 | chartqa/human | How many years are represented in the data? | ["3"] |
| chartqa/val/human/344 | chartqa/human | What's the value of the rightmost first bar from the bottom? | ["8"] |
| chartqa/val/augmented/565 | chartqa/augmented | What was the value of end-user payments for NC software in 2013? | ["1.64"] |
| chartqa/val/augmented/284 | chartqa/augmented | What was the revenue of the Arizona Cardinals in 2019? | ["422"] |
| chartqa/val/augmented/238 | chartqa/augmented | How many dollars did retail sales of cannabis reach in Canada in December of 2020? | ["298.44"] |
| pdfvqa/validation/88/12 | A/counting | How many figures can you find above 'Role of the VAPB Gene in the Pathogenesis of ALS'? | ["0"] |
| pdfvqa/validation/563/1 | A/existence | Is there any table? | ["no"] |
| pdfvqa/validation/209/0 | A/existence | Confirm if there are 2 table in this page. | ["no"] |
| pdfvqa/validation/644/13 | B/object_recognition | What is the right table about? | ["8"] |
| pdfvqa/validation/326/4 | B/structural_understanding | What is the main contents of the last section in this page? | ["5"] |
| pdfvqa/validation/206/18 | B/structural_understanding | When you check the bottom of this page, what information can you get? | ["6"] |

### 每题每组结果

| 题目 ID | 配置 | 状态 | 预测 | 自动主归因 | 原始预测行 | JSON 证据指针 |
| --- | --- | --- | --- | --- | --- | --- |
| chartqa/val/human/32 | fixed_workflow | error | "" | budget_exhausted | [L55](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L55&gt;) | /status=error; /error_type |
| chartqa/val/human/32 | agent_full | ok | "Brazil" | retrieval_miss | [L35](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L35&gt;) | /trace/retrieval_calls; /trace/tool_calls; /case/gold_pages |
| chartqa/val/human/32 | agent_no_rerank | ok | "Mexico" | evidence_selection_loss | [L31](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L31&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| chartqa/val/human/32 | agent_no_ocr | error | "" | budget_exhausted | [L36](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L36&gt;) | /status=error; /error_type |
| chartqa/val/human/32 | agent_no_visual | invalid_output | "" | model_format_error | [L9](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L9&gt;) | /prediction_status; /status |
| chartqa/val/human/32 | oracle | ok | "Cuba" | success | [L5](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L5&gt;) | /score/relaxed_accuracy=1 |
| chartqa/val/human/714 | fixed_workflow | invalid_output | "" | model_format_error | [L58](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L58&gt;) | /prediction_status; /status |
| chartqa/val/human/714 | agent_full | error | "" | unknown | [L50](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L50&gt;) | /status=error; /error_type |
| chartqa/val/human/714 | agent_no_rerank | ok | "4" | retrieval_miss | [L63](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L63&gt;) | /trace/retrieval_calls; /trace/tool_calls; /case/gold_pages |
| chartqa/val/human/714 | agent_no_ocr | invalid_output | "" | model_format_error | [L69](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L69&gt;) | /prediction_status; /status |
| chartqa/val/human/714 | agent_no_visual | invalid_output | "" | model_format_error | [L48](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L48&gt;) | /prediction_status; /status |
| chartqa/val/human/714 | oracle | ok | "3" | success | [L47](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L47&gt;) | /score/relaxed_accuracy=1 |
| chartqa/val/human/344 | fixed_workflow | invalid_output | "" | model_format_error | [L68](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L68&gt;) | /prediction_status; /status |
| chartqa/val/human/344 | agent_full | ok | "" | abstention | [L37](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L37&gt;) | /trace/events/66/payload/claims; /trace/events/66/payload/supported |
| chartqa/val/human/344 | agent_no_rerank | ok | "765" | evidence_selection_loss | [L24](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L24&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| chartqa/val/human/344 | agent_no_ocr | invalid_output | "" | model_format_error | [L41](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L41&gt;) | /prediction_status; /status |
| chartqa/val/human/344 | agent_no_visual | ok | "30" | evidence_selection_loss | [L38](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L38&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| chartqa/val/human/344 | oracle | ok | "15" | evidence_selection_loss | [L66](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L66&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| chartqa/val/augmented/565 | fixed_workflow | ok | "1.64 billion U.S. dollars" | generation_reading_or_reasoning | [L11](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L11&gt;) | /context_ranking; /original_image_pages; /score/relaxed_accuracy=0 |
| chartqa/val/augmented/565 | agent_full | ok | "1.64" | success | [L17](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L17&gt;) | /score/relaxed_accuracy=1 |
| chartqa/val/augmented/565 | agent_no_rerank | invalid_output | "" | model_format_error | [L46](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L46&gt;) | /prediction_status; /status |
| chartqa/val/augmented/565 | agent_no_ocr | invalid_output | "" | model_format_error | [L40](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L40&gt;) | /prediction_status; /status |
| chartqa/val/augmented/565 | agent_no_visual | ok | "1.64 billion U.S. dollars" | generation_reading_or_reasoning | [L14](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L14&gt;) | /context_ranking; /original_image_pages; /score/relaxed_accuracy=0 |
| chartqa/val/augmented/565 | oracle | ok | "1.64 billion U.S. dollars" | evidence_selection_loss | [L43](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L43&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| chartqa/val/augmented/284 | fixed_workflow | invalid_output | "" | model_format_error | [L26](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L26&gt;) | /prediction_status; /status |
| chartqa/val/augmented/284 | agent_full | error | "" | budget_exhausted | [L30](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L30&gt;) | /status=error; /error_type |
| chartqa/val/augmented/284 | agent_no_rerank | ok | "422 million" | generation_reading_or_reasoning | [L6](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L6&gt;) | /context_ranking; /original_image_pages; /score/relaxed_accuracy=0 |
| chartqa/val/augmented/284 | agent_no_ocr | ok | "422 million U.S. dollars" | generation_reading_or_reasoning | [L4](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L4&gt;) | /context_ranking; /original_image_pages; /score/relaxed_accuracy=0 |
| chartqa/val/augmented/284 | agent_no_visual | error | "" | budget_exhausted | [L23](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L23&gt;) | /status=error; /error_type |
| chartqa/val/augmented/284 | oracle | ok | "422" | success | [L29](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L29&gt;) | /score/relaxed_accuracy=1 |
| chartqa/val/augmented/238 | fixed_workflow | ok | "298.44 million Canadian dollars" | generation_reading_or_reasoning | [L72](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L72&gt;) | /context_ranking; /original_image_pages; /score/relaxed_accuracy=0 |
| chartqa/val/augmented/238 | agent_full | ok | "298.44" | success | [L56](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L56&gt;) | /score/relaxed_accuracy=1 |
| chartqa/val/augmented/238 | agent_no_rerank | ok | "298.44 million Canadian dollars" | generation_reading_or_reasoning | [L7](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L7&gt;) | /context_ranking; /original_image_pages; /score/relaxed_accuracy=0 |
| chartqa/val/augmented/238 | agent_no_ocr | invalid_output | "" | model_format_error | [L62](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L62&gt;) | /prediction_status; /status |
| chartqa/val/augmented/238 | agent_no_visual | error | "" | unknown | [L27](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L27&gt;) | /status=error; /error_type |
| chartqa/val/augmented/238 | oracle | ok | "298.44" | success | [L49](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L49&gt;) | /score/relaxed_accuracy=1 |
| pdfvqa/validation/88/12 | fixed_workflow | error | "" | budget_exhausted | [L67](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L67&gt;) | /status=error; /error_type |
| pdfvqa/validation/88/12 | agent_full | invalid_output | "" | model_format_error | [L10](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L10&gt;) | /prediction_status; /status |
| pdfvqa/validation/88/12 | agent_no_rerank | error | "" | budget_exhausted | [L71](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L71&gt;) | /status=error; /error_type |
| pdfvqa/validation/88/12 | agent_no_ocr | ok | "" | abstention | [L45](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L45&gt;) | /trace/events/56/payload/claims; /trace/events/56/payload/supported |
| pdfvqa/validation/88/12 | agent_no_visual | invalid_output | "" | model_format_error | [L33](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L33&gt;) | /prediction_status; /status |
| pdfvqa/validation/88/12 | oracle | ok | "0" | success | [L3](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L3&gt;) | /score/accuracy=1 |
| pdfvqa/validation/563/1 | fixed_workflow | ok | "" | abstention | [L54](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L54&gt;) | /trace/events/51/payload/claims; /trace/events/51/payload/supported |
| pdfvqa/validation/563/1 | agent_full | ok | "" | abstention | [L8](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L8&gt;) | /trace/events/68/payload/claims; /trace/events/68/payload/supported |
| pdfvqa/validation/563/1 | agent_no_rerank | ok | "" | abstention | [L16](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L16&gt;) | /trace/events/57/payload/claims; /trace/events/57/payload/supported |
| pdfvqa/validation/563/1 | agent_no_ocr | ok | "" | verifier_rejected_correct | [L19](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L19&gt;) | /trace/events/68/payload/claims; /trace/events/68/payload/supported; /score/accuracy=0 |
| pdfvqa/validation/563/1 | agent_no_visual | ok | "" | abstention | [L21](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L21&gt;) | /trace/events/52/payload/claims; /trace/events/52/payload/supported |
| pdfvqa/validation/563/1 | oracle | ok | "yes" | evidence_selection_loss | [L70](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L70&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| pdfvqa/validation/209/0 | fixed_workflow | ok | "" | abstention | [L53](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L53&gt;) | /trace/events/59/payload/claims; /trace/events/59/payload/supported |
| pdfvqa/validation/209/0 | agent_full | ok | "no" | success | [L51](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L51&gt;) | /score/accuracy=1 |
| pdfvqa/validation/209/0 | agent_no_rerank | error | "" | budget_exhausted | [L2](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L2&gt;) | /status=error; /error_type |
| pdfvqa/validation/209/0 | agent_no_ocr | invalid_output | "" | model_format_error | [L60](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L60&gt;) | /prediction_status; /status |
| pdfvqa/validation/209/0 | agent_no_visual | error | "" | budget_exhausted | [L12](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L12&gt;) | /status=error; /error_type |
| pdfvqa/validation/209/0 | oracle | ok | "1" | evidence_selection_loss | [L39](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L39&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| pdfvqa/validation/644/13 | fixed_workflow | ok | [] | abstention | [L42](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L42&gt;) | /trace/events/22/payload/claims; /trace/events/22/payload/supported |
| pdfvqa/validation/644/13 | agent_full | ok | [] | abstention | [L64](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L64&gt;) | /trace/events/24/payload/claims; /trace/events/24/payload/supported |
| pdfvqa/validation/644/13 | agent_no_rerank | invalid_output | [] | model_format_error | [L13](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L13&gt;) | /prediction_status; /status |
| pdfvqa/validation/644/13 | agent_no_ocr | ok | ["8", "0", "9", "11"] | generation_reading_or_reasoning | [L1](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L1&gt;) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/644/13 | agent_no_visual | error | [] | unknown | [L57](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L57&gt;) | /status=error; /error_type |
| pdfvqa/validation/644/13 | oracle | ok | ["8", "11"] | evidence_selection_loss | [L65](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L65&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| pdfvqa/validation/326/4 | fixed_workflow | ok | [] | abstention | [L59](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L59&gt;) | /trace/events/56/payload/claims; /trace/events/56/payload/supported |
| pdfvqa/validation/326/4 | agent_full | ok | [] | abstention | [L28](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L28&gt;) | /trace/events/68/payload/claims; /trace/events/68/payload/supported |
| pdfvqa/validation/326/4 | agent_no_rerank | ok | ["5", "0", "2"] | generation_reading_or_reasoning | [L25](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L25&gt;) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/326/4 | agent_no_ocr | ok | ["5", "0"] | generation_reading_or_reasoning | [L15](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L15&gt;) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/326/4 | agent_no_visual | ok | ["0", "5"] | generation_reading_or_reasoning | [L61](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L61&gt;) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/326/4 | oracle | ok | ["5", "0"] | evidence_selection_loss | [L44](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L44&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| pdfvqa/validation/206/18 | fixed_workflow | invalid_output | [] | model_format_error | [L34](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L34&gt;) | /prediction_status; /status |
| pdfvqa/validation/206/18 | agent_full | error | [] | budget_exhausted | [L52](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L52&gt;) | /status=error; /error_type |
| pdfvqa/validation/206/18 | agent_no_rerank | error | [] | unknown | [L20](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L20&gt;) | /status=error; /error_type |
| pdfvqa/validation/206/18 | agent_no_ocr | ok | ["2", "1", "4", "3", "0", "6", "5"] | generation_reading_or_reasoning | [L22](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L22&gt;) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/206/18 | agent_no_visual | ok | ["2"] | generation_reading_or_reasoning | [L18](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L18&gt;) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/206/18 | oracle | ok | ["2"] | evidence_selection_loss | [L32](&lt;../data/evaluations/ablation-20261005-formal/predictions.jsonl#L32&gt;) | /evidence_ranking; /trace/retrieval_calls; /ranking |

行链接指向 predictions.jsonl，路径通常相对于该行对象；带等号的记录是路径加触发条件注记，不是严格的 RFC JSON Pointer。完整 `/trace/model_calls`、`/trace/retrieval_calls`、`/trace/rerank_calls`、`/trace/tool_calls` 和 `/trace/events` 保留模型输出与阶段记录。归因中的 `/case/`、`/score/`、`/oracle_row` 为关联对象位置，分别查数据清单、归因记录与同题 oracle 行。

## 证据与复现

- [run.json](<../data/evaluations/ablation-20261005-formal/run.json>)
- [predictions.jsonl](<../data/evaluations/ablation-20261005-formal/predictions.jsonl>)
- [summary.json](<../data/evaluations/ablation-20261005-formal/summary.json>)
- [badcases.jsonl](<../data/evaluations/ablation-20261005-formal/badcases.jsonl>)

- [chartqa manifest](<../data/benchmarks/ablation-20261005/chartqa/manifest.json>)，SHA-256 `724f3f3b8f9acc6e5f661c39a2d730c96474ff62ef2c0643bbec688459bef545`
- [pdfvqa manifest](<../data/benchmarks/ablation-20261005/pdfvqa/manifest.json>)，SHA-256 `2bae51f666b8f7bc888aef037605399595398f70f9db13e88df1d2d9b4a3985e`

原运行代码 SHA-256：`50751db23df7495f3e9c7862e7ff87914340f2006eeaa9df3e002c33c147bde6`。

本报告核对了数据清单身份、预测 ID 与重新计算的汇总分数；未重新调用模型、选择性重试失败题或覆盖原始预测。后续复验需保留同一清单、索引、模型、预算及代码身份；报告所列结果只对应这些本地记录。

## 软件验证

最终整套回归：500 passed、1 skipped、0 failed。此前一轮空白 PDF 导入断言失败，单独复跑及随后完整复跑通过，根因未确认；本次未修改 PDF 生产代码。[verification.json](<../data/evaluations/ablation-20261005-formal/verification.json>)

正式轨迹完成后修正了一处离线统计：目录工具 list_documents 的金页命中指标不适用，不将空排名记成失败。原始预测未改写，也没有新增云端调用；旧统计模块已保留，并验证可重构原运行代码指纹。因此当前工作区代码指纹与 run.json 不同，不应直接恢复旧实验；重新生成轨迹需使用新目录。[postprocessing.json](<../data/evaluations/ablation-20261005-formal/postprocessing.json>)
