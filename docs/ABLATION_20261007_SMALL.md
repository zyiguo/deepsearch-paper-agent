# Agentic RAG 小批量消融实验报告

生成日期：2026-10-07（北京时间）。实际完成 **30/30** 个题目-配置组合，本轮已全部完成。

固定样本：ChartQA 2 题，PDFVQA A 2 题，PDFVQA B 2 题。下面均由本轮原始记录离线计算；未合并不同任务的答案指标。

## 本轮结论

完整 agent：ChartQA 0.0% (0/2)，PDF A 100.0% (2/2)，PDF B 集合 EM 50.0% (1/2)。这里只描述这 6 道题的一次运行，不证明组件稳定优劣。

已完成的 30 条 RAG 轨迹自动主标签包括：格式失败 7，预算耗尽 2，弃答 1，待细分的阅读/推理失败 10。因此不能把全部错题都算作召回问题，也不能把“有页仍答错”自动解释成 OCR 错误。

自然工具调用中正常返回 83/86 次。这是执行成功率，不是工具选择准确率或答案正确率；后文分别检查新增证据、重复调用、金页命中和最终回答。

复用索引记录的回退原因为 image_embedding_http_402，不代表本轮重新探测了该服务。**统一采用 BM25＋预处理 OCR 召回**；多模态重排和原图阅读仍参与，图像向量检索不在本轮范围内。

## 数据与干预

| 项目 | 本轮记录 |
| --- | --- |
| 模型 | mimo-v2.6-pro |
| 检索通道 | {'chartqa': 'bm25', 'pdfvqa': 'bm25'} |
| 检索 top-k | 3 |
| 单题模型请求预算 | 18 |
| 上下文 / 最大输出 Token | 1048576 / 8192 |
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

本轮使用 BM25 检索，**直接图像向量检索未评估**。索引记录历史 HTTP 402 回退，各组使用同一 BM25 配置；不是本轮服务状态判断。

PDFVQA A/B 是已给定页面的任务，页级召回只说明该页片段是否被返回，不能衡量多文档选页。ChartQA 的跨图检索包含依赖“这张图”等指代的问题，缺少原任务给定的目标图，因此属于受控检索诊断，不能直接比较官方给定图像成绩。样本很小且每个配置只运行一次；抽样来自本地验证子集，PDF 页面镜像的元素匹配规则也限制了推广范围。本轮 memory、skills、外部 MCP 按配置关闭，不代表这些能力已经通过端到端质量评测。

## 答案效果

| 配置 | ChartQA relaxed accuracy | PDF A accuracy | PDF B 集合 EM | PDF B 集合 F1 | 错误 / 弃答 / 缺失 |
| --- | --- | --- | --- | --- | --- |
| fixed_workflow | 0.0% (0/2) | 0.0% (0/2) | 50.0% (1/2) | 0.8333 (n=2) | 1 / 2 / 0 |
| agent_full | 0.0% (0/2) | 100.0% (2/2) | 50.0% (1/2) | 0.5000 (n=2) | 2 / 0 / 0 |
| agent_no_rerank | 50.0% (1/2) | 0.0% (0/2) | 0.0% (0/2) | 0.5833 (n=2) | 2 / 0 / 0 |
| agent_no_ocr | 50.0% (1/2) | 0.0% (0/2) | 0.0% (0/2) | 0.5833 (n=2) | 1 / 1 / 0 |
| agent_no_visual | 0.0% (0/2) | 50.0% (1/2) | 0.0% (0/2) | 0.3333 (n=2) | 3 / 0 / 0 |

错误包括服务失败与无效输出，弃答（abstention）单列；三者均保留在全题分母并计零。ChartQA relaxed accuracy 使用既有 5% 数值容差规则；PDF A 为固定答案准确率；PDF B 为元素 ID 集合匹配及逐题集合 F1。F1 表格中的 n 是题数，不是元素数。无效格式不会通过事后猜测或答案改写补分。

### 小样本不确定性

| 配置 | 任务 / 指标 | 通过 / 固定题数 | Wilson 95% |
| --- | --- | --- | --- |
| fixed_workflow | chartqa/relaxed_accuracy | 0.0% (0/2) | [0.0%, 65.8%] |
| fixed_workflow | A/accuracy | 0.0% (0/2) | [0.0%, 65.8%] |
| fixed_workflow | B/element_exact_match | 50.0% (1/2) | [9.5%, 90.5%] |
| agent_full | chartqa/relaxed_accuracy | 0.0% (0/2) | [0.0%, 65.8%] |
| agent_full | A/accuracy | 100.0% (2/2) | [34.2%, 100.0%] |
| agent_full | B/element_exact_match | 50.0% (1/2) | [9.5%, 90.5%] |
| agent_no_rerank | chartqa/relaxed_accuracy | 50.0% (1/2) | [9.5%, 90.5%] |
| agent_no_rerank | A/accuracy | 0.0% (0/2) | [0.0%, 65.8%] |
| agent_no_rerank | B/element_exact_match | 0.0% (0/2) | [0.0%, 65.8%] |
| agent_no_ocr | chartqa/relaxed_accuracy | 50.0% (1/2) | [9.5%, 90.5%] |
| agent_no_ocr | A/accuracy | 0.0% (0/2) | [0.0%, 65.8%] |
| agent_no_ocr | B/element_exact_match | 0.0% (0/2) | [0.0%, 65.8%] |
| agent_no_visual | chartqa/relaxed_accuracy | 0.0% (0/2) | [0.0%, 65.8%] |
| agent_no_visual | A/accuracy | 50.0% (1/2) | [9.5%, 90.5%] |
| agent_no_visual | B/element_exact_match | 0.0% (0/2) | [0.0%, 65.8%] |

Wilson 区间描述二值指标的小样本不确定性，不衡量多次生成波动；这些固定开发题不是总体随机样本，不能据此宣称泛化性能。

### 给定图像阅读参考

本轮未包含 oracle，不借用其他模型或运行的 oracle 结果作为本轮阅读参考。

oracle 直接给定题目对应原图，并按任务提供输入元素目录，是独立的阅读参考，**不保证是上界**。其提示词与角色流程不同；给定页面也不是模型实际产生的引用。因此不将 oracle 的页命中、引用率或相对差值当作普通组件消融。

### 题型与标签分布

| 配置 | ChartQA human | ChartQA augmented |
| --- | --- | --- |
| fixed_workflow | 0.0% (0/1) | 0.0% (0/1) |
| agent_full | 0.0% (0/1) | 0.0% (0/1) |
| agent_no_rerank | 0.0% (0/1) | 100.0% (1/1) |
| agent_no_ocr | 0.0% (0/1) | 100.0% (1/1) |
| agent_no_visual | 0.0% (0/1) | 0.0% (0/1) |

human 子集中的泛指问题依赖给定原图，跨图检索任务本身不充分；augmented 子集中的实体和时间通常更明确。两组仍都保留，不按结果删题。

PDFVQA A 金标分布：{"0": 1, "no": 1}；本子集固定回答“0”可得 1/2。这是标签分布的描述性基线，不是检索结果。

## 逐阶段评估

### 检索与证据保留

| 配置 | 全部候选召回 | 首次 Recall@k | 首次 MRR@k | 工具后金页命中 | 已收集金页命中 | 最终引用页精确率 | 最终引用页覆盖 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | 0.8333 (n=6) | 0.6667 (n=6) | 0.6667 (n=6) | 0.0% (0/6) | 83.3% (5/6) | 0.5000 (n=6) | 0.5000 (n=6) |
| agent_full | 0.5000 (n=6) | 0.5000 (n=6) | 0.5000 (n=6) | 83.3% (5/6) | 83.3% (5/6) | 0.8333 (n=6) | 0.8333 (n=6) |
| agent_no_rerank | 0.6667 (n=6) | 0.5000 (n=6) | 0.5000 (n=6) | 83.3% (5/6) | 83.3% (5/6) | 0.8333 (n=6) | 0.8333 (n=6) |
| agent_no_ocr | 0.8333 (n=6) | 0.6667 (n=6) | 0.6667 (n=6) | 100.0% (6/6) | 100.0% (6/6) | 0.6667 (n=6) | 0.6667 (n=6) |
| agent_no_visual | 0.8333 (n=6) | 0.6667 (n=6) | 0.6667 (n=6) | 83.3% (5/6) | 83.3% (5/6) | 0.5000 (n=6) | 0.5000 (n=6) |

这一表按有金标准页的题目平均，表内保留各自 n。全部候选召回使用各次成功检索的去重页并集；首次指标只取第一次检索。固定工作流没有工具节点，其“工具后”列不应解释为检索失败。最终引用页指标只是与金标准页的重合代理，不验证引用是否真正支持结论。

| 配置 | PDF B 检索文本元素覆盖 | PDF B 已收集文本元素覆盖 |
| --- | --- | --- |
| fixed_workflow | 1.0000 (n=2) | 1.0000 (n=2) |
| agent_full | 0.5000 (n=2) | 1.0000 (n=2) |
| agent_no_rerank | 0.5000 (n=2) | 1.0000 (n=2) |
| agent_no_ocr | 0.5000 (n=2) | 1.0000 (n=2) |
| agent_no_visual | 1.0000 (n=2) | 1.0000 (n=2) |

元素覆盖只检查文本中记录的目录 ID，不证明图像可读、模型理解正确或元素完整进入截断后的实际提示。缺少该观测字段时不按零覆盖处理；已收集证据与模型实际上下文的差别需结合提示记录检查。

| 配置 | 收集金页覆盖 | 最后回答上下文金页覆盖 | 最后回答原图金页覆盖 | PDF B 上下文文本元素覆盖 |
| --- | --- | --- | --- | --- |
| fixed_workflow | 0.8333 (n=6) | 0.6667 (n=6) | 0.6667 (n=6) | 1.0000 (n=2) |
| agent_full | 0.8333 (n=6) | 0.8333 (n=6) | 0.8333 (n=6) | 1.0000 (n=2) |
| agent_no_rerank | 0.8333 (n=6) | 0.8333 (n=6) | 0.8333 (n=6) | 1.0000 (n=2) |
| agent_no_ocr | 1.0000 (n=6) | 1.0000 (n=6) | 1.0000 (n=6) | 1.0000 (n=2) |
| agent_no_visual | 0.8333 (n=6) | 0.8333 (n=6) | 0.0000 (n=6) | 1.0000 (n=2) |

这些覆盖率只对相应字段有观测的固定题目计算，n 单列。上下文指最后准备给 Answerer 的输入；实际请求状态见 answerer_request_status。原图列仅统计对应模型请求中确实包含图片、且未被预算拦截的页面。no_visual 的原图覆盖为零是实验干预，不是未完成调用。

### 重排

| 配置 | 调用数 | 前 Recall@k | 后 Recall@k | 前 MRR@k | 后 MRR@k | 状态计数 |
| --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | 17 | 0.8235 (n=17) | 0.8235 (n=17) | 0.8235 (n=17) | 0.8235 (n=17) | {"ok_multimodal": 17} |
| agent_full | 5 | 0.6000 (n=5) | 0.6000 (n=5) | 0.6000 (n=5) | 0.6000 (n=5) | {"ok_multimodal": 5} |
| agent_no_rerank | 8 | 0.7500 (n=8) | 0.7500 (n=8) | 0.7500 (n=8) | 0.7500 (n=8) | {"disabled_control": 8} |
| agent_no_ocr | 9 | 0.6667 (n=9) | 0.7778 (n=9) | 0.6667 (n=9) | 0.7037 (n=9) | {"ok_multimodal": 9} |
| agent_no_visual | 10 | 0.7000 (n=10) | 0.7000 (n=10) | 0.7000 (n=10) | 0.7000 (n=10) | {"ok_multimodal": 10} |

重排表按实际调用加权，属于诊断；同一道题的多次重排会重复贡献，不能当作独立题目样本。关闭重排组在相同查询和 top-k 下保留相同候选获取规则；不同模型轨迹仍可能产生不同查询、top-k 和调用次数，不保证候选完全相同。单候选可能直接跳过重排。

### 工具调用

| 配置 | 工具 | 尝试 | 成功 | 失败 | 参数无效 | 相同调用重复 | 新增证据调用 | 金页命中 | 错误码 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| agent_full | list_documents | 4 | 4 | 0 | 0 | 0 | 0 | 未观测 (n=0) | {} |
| agent_full | search_evidence | 6 | 6 | 0 | 0 | 0 | 4 | 50.0% (3/6) | {} |
| agent_full | get_evidence | 2 | 2 | 0 | 0 | 0 | 0 | 50.0% (1/2) | {} |
| agent_full | read_page | 4 | 4 | 0 | 0 | 1 | 3 | 100.0% (4/4) | {} |
| agent_full | ocr_page | 2 | 1 | 1 | 0 | 0 | 1 | 50.0% (1/2) | {"tool_failed": 1} |
| agent_no_rerank | search_evidence | 10 | 10 | 0 | 0 | 0 | 8 | 60.0% (6/10) | {} |
| agent_no_rerank | list_documents | 2 | 2 | 0 | 0 | 0 | 0 | 未观测 (n=0) | {} |
| agent_no_rerank | read_page | 6 | 4 | 2 | 0 | 2 | 3 | 66.7% (4/6) | {"page_not_found": 2} |
| agent_no_rerank | ocr_page | 1 | 1 | 0 | 0 | 0 | 1 | 100.0% (1/1) | {} |
| agent_no_ocr | list_documents | 2 | 2 | 0 | 0 | 0 | 0 | 未观测 (n=0) | {} |
| agent_no_ocr | search_evidence | 10 | 10 | 0 | 0 | 0 | 8 | 70.0% (7/10) | {} |
| agent_no_ocr | get_evidence | 3 | 3 | 0 | 0 | 0 | 0 | 100.0% (3/3) | {} |
| agent_no_ocr | read_page | 8 | 8 | 0 | 0 | 3 | 4 | 75.0% (6/8) | {} |
| agent_no_visual | search_evidence | 11 | 11 | 0 | 0 | 0 | 8 | 63.6% (7/11) | {} |
| agent_no_visual | list_documents | 2 | 2 | 0 | 0 | 0 | 0 | 未观测 (n=0) | {} |
| agent_no_visual | get_evidence | 4 | 4 | 0 | 0 | 1 | 0 | 100.0% (4/4) | {} |
| agent_no_visual | read_page | 6 | 6 | 0 | 0 | 1 | 4 | 83.3% (5/6) | {} |
| agent_no_visual | ocr_page | 3 | 3 | 0 | 0 | 0 | 3 | 100.0% (3/3) | {} |

工具成功表示调用正常返回，不等于答案正确。重复调用按单题内同工具及规范化参数判断；新增证据依赖新的 chunk_id。金页命中仅在工具有明确排名观测时计算，目录查询等无页排名工具不凭空记为零。

### 生成与核验

| 配置 | ChartQA 已获金页条件准确率 | PDF A 已获金页条件准确率 | PDF B 已获金页条件 EM | 核验接受 claim | 正确草稿→错误终答 | 错误草稿→正确终答 |
| --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | 0.0% (0/1) | 0.0% (0/2) | 50.0% (1/2) | 75.0% (3/4) | 1 | 0 |
| agent_full | 0.0% (0/1) | 100.0% (2/2) | 50.0% (1/2) | 100.0% (5/5) | 0 | 0 |
| agent_no_rerank | 100.0% (1/1) | 0.0% (0/2) | 0.0% (0/2) | 100.0% (5/5) | 0 | 0 |
| agent_no_ocr | 50.0% (1/2) | 0.0% (0/2) | 0.0% (0/2) | 83.3% (5/6) | 1 | 0 |
| agent_no_visual | 0.0% (0/1) | 50.0% (1/2) | 0.0% (0/2) | 100.0% (3/3) | 0 | 0 |

条件准确率分母只包含明确收集到金标准页的题目，不能代表全样本质量。核验接受率是模型自检结果，不是人工忠实性评价。草稿到终答变化仅覆盖可以解析为单一任务答案的最终草稿；这种变化记录不证明核验模块的因果贡献。

## 调用、Token 与延迟

| 配置 | 请求 / 预算拒绝 | 成功模型调用 | 已上报总 Token | Token 已上报 / 请求 | 题目延迟均值 / P50 / P95 秒 |
| --- | --- | --- | --- | --- | --- |
| fixed_workflow | 62 / 0 | 62 | 183027 | 60 / 62 | 80.69 / 76.68 / 136.87 |
| agent_full | 60 / 0 | 59 | 177529 | 59 / 60 | 87.69 / 72.77 / 157.33 |
| agent_no_rerank | 68 / 0 | 68 | 273038 | 66 / 68 | 78.81 / 73.24 / 125.73 |
| agent_no_ocr | 70 / 0 | 70 | 310570 | 70 / 70 | 84.56 / 79.14 / 131.73 |
| agent_no_visual | 89 / 2 | 89 | 405901 | 87 / 89 | 107.71 / 102.36 / 138.82 |

仅汇总服务端实际返回的 Token；“未上报”不是零费用，失败请求也可能产生费用。模型请求不包含发送前预算拒绝。这里不含图像嵌入接口的调用量或 Token 账单，也未换算货币成本。延迟是本机及本轮并发、网络条件下的实测，不能作为独立硬件性能结论。

| 配置 | 阶段 | 模型请求数 | 已上报总 Token | Token 已上报请求数 |
| --- | --- | --- | --- | --- |
| fixed_workflow | answerer | 9 | 29040 | 7 |
| fixed_workflow | rerank | 17 | 63729 | 17 |
| fixed_workflow | researcher | 8 | 4095 | 8 |
| fixed_workflow | supervisor | 21 | 57210 | 21 |
| fixed_workflow | verifier | 7 | 28953 | 7 |
| agent_full | answerer | 7 | 26448 | 7 |
| agent_full | rerank | 5 | 20473 | 5 |
| agent_full | researcher | 20 | 53730 | 20 |
| agent_full | supervisor | 20 | 52918 | 20 |
| agent_full | tool:ocr_page | 2 | 2054 | 1 |
| agent_full | verifier | 6 | 21906 | 6 |
| agent_no_rerank | answerer | 10 | 59389 | 10 |
| agent_no_rerank | researcher | 23 | 69138 | 23 |
| agent_no_rerank | supervisor | 24 | 83984 | 22 |
| agent_no_rerank | tool:ocr_page | 1 | 1996 | 1 |
| agent_no_rerank | verifier | 10 | 58531 | 10 |
| agent_no_ocr | answerer | 6 | 34280 | 6 |
| agent_no_ocr | rerank | 10 | 46075 | 10 |
| agent_no_ocr | researcher | 28 | 121082 | 28 |
| agent_no_ocr | supervisor | 20 | 74992 | 20 |
| agent_no_ocr | verifier | 6 | 34141 | 6 |
| agent_no_visual | answerer | 13 | 67979 | 11 |
| agent_no_visual | rerank | 10 | 43485 | 10 |
| agent_no_visual | researcher | 29 | 107833 | 29 |
| agent_no_visual | supervisor | 23 | 110325 | 23 |
| agent_no_visual | tool:ocr_page | 3 | 9175 | 3 |
| agent_no_visual | verifier | 11 | 67104 | 11 |

### 索引预处理成本

| 数据集 | 模型请求 | 已上报总 Token | 覆盖 | 原始记录 |
| --- | --- | --- | --- | --- |
| chartqa | 12 | 18263 | 12/12 | [preparation-trace.json](../data/benchmarks/ablation-20261005/chartqa/preparation-trace.json) |
| pdfvqa | 0 | 未上报 | 0/0 | [preparation-trace.json](../data/benchmarks/ablation-20261005/pdfvqa/preparation-trace.json) |

索引预处理与在线题目运行分开统计。预处理记录可能包含失败索引尝试；不能把已缓存的 OCR 视为本轮在线免费产生，也不能将同一次共享预处理成本重复累加到每个消融组。

### 共享索引中的历史工具验收

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

复用准备目录内的历史探针，共 2 页，生成请求 2 次，上报 Token 3277（2/2 次有 usage）；不计为本轮新增调用。这里只验证工具可执行和返回内容，不代表 agent 会自主选对工具，也不证明 OCR 识别准确率。[tool-probes.jsonl](../data/benchmarks/ablation-20261005/tool-probes.jsonl)

## 配对差异

差值方向为“当前配置 − agent_full”。每一行使用相同固定题目，失败和缺失仍记零；正值只表示本次样本得分较高。Bootstrap 区间是固定 seed 的小样本描述，不能支持稳定性或显著性结论；单次生成随机性与多重比较尚未控制。

| 配置 | 任务 / 指标 | 固定 n | 两组均有记录 | 分数差 | 升 / 降 / 平 | Bootstrap 95% |
| --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | chartqa/relaxed_accuracy | 2 | 2 | +0.0000 | 0/0/2 | [+0.0000, +0.0000] |
| fixed_workflow | A/accuracy | 2 | 2 | -1.0000 | 0/2/0 | [-1.0000, -1.0000] |
| fixed_workflow | B/element_exact_match | 2 | 2 | +0.0000 | 0/0/2 | [+0.0000, +0.0000] |
| fixed_workflow | B/element_f1 | 2 | 2 | +0.3333 | 1/0/1 | [+0.0000, +0.6667] |
| agent_no_rerank | chartqa/relaxed_accuracy | 2 | 2 | +0.5000 | 1/0/1 | [+0.0000, +1.0000] |
| agent_no_rerank | A/accuracy | 2 | 2 | -1.0000 | 0/2/0 | [-1.0000, -1.0000] |
| agent_no_rerank | B/element_exact_match | 2 | 2 | -0.5000 | 0/1/1 | [-1.0000, +0.0000] |
| agent_no_rerank | B/element_f1 | 2 | 2 | +0.0833 | 1/1/0 | [-0.5000, +0.6667] |
| agent_no_ocr | chartqa/relaxed_accuracy | 2 | 2 | +0.5000 | 1/0/1 | [+0.0000, +1.0000] |
| agent_no_ocr | A/accuracy | 2 | 2 | -1.0000 | 0/2/0 | [-1.0000, -1.0000] |
| agent_no_ocr | B/element_exact_match | 2 | 2 | -0.5000 | 0/1/1 | [-1.0000, +0.0000] |
| agent_no_ocr | B/element_f1 | 2 | 2 | +0.0833 | 1/1/0 | [-0.5000, +0.6667] |
| agent_no_visual | chartqa/relaxed_accuracy | 2 | 2 | +0.0000 | 0/0/2 | [+0.0000, +0.0000] |
| agent_no_visual | A/accuracy | 2 | 2 | -0.5000 | 0/1/1 | [-1.0000, +0.0000] |
| agent_no_visual | B/element_exact_match | 2 | 2 | -0.5000 | 0/1/1 | [-1.0000, +0.0000] |
| agent_no_visual | B/element_f1 | 2 | 2 | -0.1667 | 0/1/1 | [-0.3333, +0.0000] |

## 错误归因与逐题追踪

以下是固定规则产生的自动归因，**人工复核待完成**；`confirmed` 只说明记录满足该规则，不代表人工确认完整原因。“generation_reading_or_reasoning”不能进一步区分 OCR、图像阅读和推理；`verifier_rejected_correct` 表示观察到正确结构化草稿被拒绝，也不是完整反事实实验。本轮归因文件有 30 条记录，带人工覆盖记录 0 条。

历史运行的助手日志/原图抽查见 [Badcase 抽查记录](AGENTIC_ABLATION_AUDIT.md)，不代表本轮已完成抽查。它不替代人工专家审核，也不覆盖自动判断。例如基准答案正确但引用越界时，程序拒绝答案不等于核验模型误判；必须查看原始 supported 与程序最终 supported 的差别。

| 配置 | abstention | budget_exhausted | evidence_selection_loss | generation_reading_or_reasoning | model_format_error | success | verifier_rejected_correct |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fixed_workflow | 1 | 0 | 0 | 2 | 1 | 1 | 1 |
| agent_full | 0 | 0 | 0 | 1 | 2 | 3 | 0 |
| agent_no_rerank | 0 | 0 | 0 | 3 | 2 | 1 | 0 |
| agent_no_ocr | 0 | 0 | 1 | 2 | 1 | 1 | 1 |
| agent_no_visual | 0 | 2 | 0 | 2 | 1 | 1 | 0 |

| 配置 | 次标签 | 题数 |
| --- | --- | --- |
| fixed_workflow | retrieval_miss_observed | 1 |
| fixed_workflow | verifier_accepted_wrong | 2 |
| agent_full | recovered_model_failure | 1 |
| agent_full | recovered_tool_failure | 1 |
| agent_full | retrieval_miss_observed | 1 |
| agent_full | verifier_accepted_wrong | 1 |
| agent_no_rerank | retrieval_miss_observed | 1 |
| agent_no_rerank | tool_failure_observed | 1 |
| agent_no_rerank | verifier_accepted_wrong | 3 |
| agent_no_ocr | verifier_accepted_wrong | 3 |
| agent_no_visual | retrieval_miss_observed | 1 |
| agent_no_visual | verifier_accepted_wrong | 2 |
| agent_no_visual | visual_input_disabled | 6 |

### 固定题目

| 题目 ID | 任务 / 分组 | 原问题 | 金标准 |
| --- | --- | --- | --- |
| chartqa/val/human/32 | chartqa/human | Which country data analysed here? | ["cuba"] |
| chartqa/val/augmented/238 | chartqa/augmented | How many dollars did retail sales of cannabis reach in Canada in December of 2020? | ["298.44"] |
| pdfvqa/validation/88/12 | A/counting | How many figures can you find above 'Role of the VAPB Gene in the Pathogenesis of ALS'? | ["0"] |
| pdfvqa/validation/209/0 | A/existence | Confirm if there are 2 table in this page. | ["no"] |
| pdfvqa/validation/644/13 | B/object_recognition | What is the right table about? | ["8"] |
| pdfvqa/validation/206/18 | B/structural_understanding | When you check the bottom of this page, what information can you get? | ["6"] |

### 每题每组结果

| 题目 ID | 配置 | 状态 | 预测 | 自动主归因 | 原始预测行 | JSON 证据指针 |
| --- | --- | --- | --- | --- | --- | --- |
| chartqa/val/human/32 | fixed_workflow | invalid_output | "" | model_format_error | [L17](../data/evaluations/ablation-20261007-small/predictions.jsonl#L17) | /prediction_status; /status |
| chartqa/val/human/32 | agent_full | invalid_output | "" | model_format_error | [L28](../data/evaluations/ablation-20261007-small/predictions.jsonl#L28) | /prediction_status; /status |
| chartqa/val/human/32 | agent_no_rerank | invalid_output | "" | model_format_error | [L21](../data/evaluations/ablation-20261007-small/predictions.jsonl#L21) | /prediction_status; /status |
| chartqa/val/human/32 | agent_no_ocr | ok | "Brazil" | evidence_selection_loss | [L14](../data/evaluations/ablation-20261007-small/predictions.jsonl#L14) | /evidence_ranking; /trace/retrieval_calls; /ranking |
| chartqa/val/human/32 | agent_no_visual | invalid_output | "" | model_format_error | [L27](../data/evaluations/ablation-20261007-small/predictions.jsonl#L27) | /prediction_status; /status |
| chartqa/val/augmented/238 | fixed_workflow | ok | "298.44 million Canadian dollars" | generation_reading_or_reasoning | [L3](../data/evaluations/ablation-20261007-small/predictions.jsonl#L3) | /context_ranking; /original_image_pages; /score/relaxed_accuracy=0 |
| chartqa/val/augmented/238 | agent_full | ok | "298.44 million Canadian dollars" | generation_reading_or_reasoning | [L1](../data/evaluations/ablation-20261007-small/predictions.jsonl#L1) | /context_ranking; /original_image_pages; /score/relaxed_accuracy=0 |
| chartqa/val/augmented/238 | agent_no_rerank | ok | "298.44" | success | [L6](../data/evaluations/ablation-20261007-small/predictions.jsonl#L6) | /score/relaxed_accuracy=1 |
| chartqa/val/augmented/238 | agent_no_ocr | ok | "298.44" | success | [L18](../data/evaluations/ablation-20261007-small/predictions.jsonl#L18) | /score/relaxed_accuracy=1 |
| chartqa/val/augmented/238 | agent_no_visual | ok | "298440000" | generation_reading_or_reasoning | [L19](../data/evaluations/ablation-20261007-small/predictions.jsonl#L19) | /context_ranking; /original_image_pages; /score/relaxed_accuracy=0 |
| pdfvqa/validation/88/12 | fixed_workflow | ok | "" | abstention | [L30](../data/evaluations/ablation-20261007-small/predictions.jsonl#L30) | /trace/events/55/payload/claims; /trace/events/55/payload/supported |
| pdfvqa/validation/88/12 | agent_full | ok | "0" | success | [L13](../data/evaluations/ablation-20261007-small/predictions.jsonl#L13) | /score/accuracy=1 |
| pdfvqa/validation/88/12 | agent_no_rerank | invalid_output | "" | model_format_error | [L20](../data/evaluations/ablation-20261007-small/predictions.jsonl#L20) | /prediction_status; /status |
| pdfvqa/validation/88/12 | agent_no_ocr | invalid_output | "" | model_format_error | [L7](../data/evaluations/ablation-20261007-small/predictions.jsonl#L7) | /prediction_status; /status |
| pdfvqa/validation/88/12 | agent_no_visual | error | "" | budget_exhausted | [L26](../data/evaluations/ablation-20261007-small/predictions.jsonl#L26) | /status=error; /error_type |
| pdfvqa/validation/209/0 | fixed_workflow | ok | "" | verifier_rejected_correct | [L11](../data/evaluations/ablation-20261007-small/predictions.jsonl#L11) | /trace/events/55/payload/claims; /trace/events/55/payload/supported; /score/accuracy=0 |
| pdfvqa/validation/209/0 | agent_full | ok | "no" | success | [L12](../data/evaluations/ablation-20261007-small/predictions.jsonl#L12) | /score/accuracy=1 |
| pdfvqa/validation/209/0 | agent_no_rerank | ok | "yes" | generation_reading_or_reasoning | [L16](../data/evaluations/ablation-20261007-small/predictions.jsonl#L16) | /context_ranking; /original_image_pages; /score/accuracy=0 |
| pdfvqa/validation/209/0 | agent_no_ocr | ok | "" | verifier_rejected_correct | [L4](../data/evaluations/ablation-20261007-small/predictions.jsonl#L4) | /trace/events/56/payload/claims; /trace/events/56/payload/supported; /score/accuracy=0 |
| pdfvqa/validation/209/0 | agent_no_visual | ok | "no" | success | [L29](../data/evaluations/ablation-20261007-small/predictions.jsonl#L29) | /score/accuracy=1 |
| pdfvqa/validation/644/13 | fixed_workflow | ok | ["8"] | success | [L25](../data/evaluations/ablation-20261007-small/predictions.jsonl#L25) | /score/element_exact_match=1 |
| pdfvqa/validation/644/13 | agent_full | ok | ["8"] | success | [L2](../data/evaluations/ablation-20261007-small/predictions.jsonl#L2) | /score/element_exact_match=1 |
| pdfvqa/validation/644/13 | agent_no_rerank | ok | ["8", "0", "9"] | generation_reading_or_reasoning | [L10](../data/evaluations/ablation-20261007-small/predictions.jsonl#L10) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/644/13 | agent_no_ocr | ok | ["8", "0", "9"] | generation_reading_or_reasoning | [L22](../data/evaluations/ablation-20261007-small/predictions.jsonl#L22) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/644/13 | agent_no_visual | ok | ["8", "11"] | generation_reading_or_reasoning | [L23](../data/evaluations/ablation-20261007-small/predictions.jsonl#L23) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/206/18 | fixed_workflow | ok | ["6", "5"] | generation_reading_or_reasoning | [L5](../data/evaluations/ablation-20261007-small/predictions.jsonl#L5) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/206/18 | agent_full | invalid_output | [] | model_format_error | [L9](../data/evaluations/ablation-20261007-small/predictions.jsonl#L9) | /prediction_status; /status |
| pdfvqa/validation/206/18 | agent_no_rerank | ok | ["6", "5"] | generation_reading_or_reasoning | [L8](../data/evaluations/ablation-20261007-small/predictions.jsonl#L8) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/206/18 | agent_no_ocr | ok | ["6", "5"] | generation_reading_or_reasoning | [L24](../data/evaluations/ablation-20261007-small/predictions.jsonl#L24) | /context_ranking; /original_image_pages; /score/element_exact_match=0 |
| pdfvqa/validation/206/18 | agent_no_visual | error | [] | budget_exhausted | [L15](../data/evaluations/ablation-20261007-small/predictions.jsonl#L15) | /status=error; /error_type |

行链接指向 predictions.jsonl，路径通常相对于该行对象；带等号的记录是路径加触发条件注记，不是严格的 RFC JSON Pointer。完整 `/trace/model_calls`、`/trace/retrieval_calls`、`/trace/rerank_calls`、`/trace/tool_calls` 和 `/trace/events` 保留模型输出与阶段记录。归因中的 `/case/`、`/score/`、`/oracle_row` 为关联对象位置，分别查数据清单、归因记录与同题 oracle 行。

## 证据与复现

- [run.json](../data/evaluations/ablation-20261007-small/run.json)
- [predictions.jsonl](../data/evaluations/ablation-20261007-small/predictions.jsonl)
- [summary.json](../data/evaluations/ablation-20261007-small/summary.json)
- [badcases.jsonl](../data/evaluations/ablation-20261007-small/badcases.jsonl)

- [chartqa manifest](../data/benchmarks/ablation-20261005/chartqa/manifest.json)，SHA-256 `724f3f3b8f9acc6e5f661c39a2d730c96474ff62ef2c0643bbec688459bef545`
- [pdfvqa manifest](../data/benchmarks/ablation-20261005/pdfvqa/manifest.json)，SHA-256 `2bae51f666b8f7bc888aef037605399595398f70f9db13e88df1d2d9b4a3985e`

原运行代码 SHA-256：`c45f21d2e6d89a7f46eff6ca4230e2186203448dd964f3856277f3d11dd33eb1`。

本报告核对了数据清单身份、预测 ID 与重新计算的汇总分数；未重新调用模型、选择性重试失败题或覆盖原始预测。后续复验需保留同一清单、索引、模型、预算及代码身份；报告所列结果只对应这些本地记录。

## DAG 依赖与分阶段判分

标注层级：{"stage_proxy": 6}。stage_proxy 是阶段代理，不是已标注中间计算结果的题目级基准。分数是评估指标，不是 Agent 的执行路线。

| 配置 | 任务 | 必达目标通过 / 固定 n | 依赖计分 | 原始节点分数 | 观测覆盖 |
| --- | --- | --- | --- | --- | --- |
| agent_full | A | 2/2 | 1.0000 | 1.0000 | 100.0% |
| agent_full | B | 1/2 | 0.7500 | 0.7500 | 100.0% |
| agent_full | chartqa | 0/2 | 0.2500 | 0.2500 | 100.0% |
| agent_no_ocr | A | 0/2 | 0.4500 | 0.4500 | 100.0% |
| agent_no_ocr | B | 0/2 | 0.5000 | 0.5000 | 100.0% |
| agent_no_ocr | chartqa | 1/2 | 0.7000 | 0.7000 | 100.0% |
| agent_no_rerank | A | 0/2 | 0.5000 | 0.5000 | 100.0% |
| agent_no_rerank | B | 0/2 | 0.5000 | 0.5000 | 100.0% |
| agent_no_rerank | chartqa | 1/2 | 0.5000 | 0.5000 | 100.0% |
| agent_no_visual | A | 1/2 | 0.7000 | 0.7000 | 100.0% |
| agent_no_visual | B | 0/2 | 0.4500 | 0.4500 | 100.0% |
| agent_no_visual | chartqa | 0/2 | 0.2500 | 0.2500 | 100.0% |
| fixed_workflow | A | 0/2 | 0.3000 | 0.3000 | 100.0% |
| fixed_workflow | B | 1/2 | 0.7500 | 0.7500 | 100.0% |
| fixed_workflow | chartqa | 0/2 | 0.2500 | 0.2500 | 90.0% |

原始节点分数忽略依赖，依赖计分要求上游达标；未知与阻塞均不给分，但不混为已观测失败。各任务分别汇总，不将不同答案指标合并为统一准确率。

| 配置 | 节点 | n | 通过 | 失败 | 未知 | 阻塞 | 不考虑依赖时通过 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| agent_full | answer | 6 | 3 | 2 | 0 | 1 | 3 |
| agent_full | citations | 6 | 5 | 0 | 0 | 1 | 5 |
| agent_full | context | 6 | 5 | 0 | 0 | 1 | 5 |
| agent_full | element_text | 2 | 2 | 0 | 0 | 0 | 2 |
| agent_full | evidence | 6 | 5 | 1 | 0 | 0 | 5 |
| agent_full | tool_result | 6 | 5 | 1 | 0 | 0 | 5 |
| agent_no_ocr | answer | 6 | 1 | 5 | 0 | 0 | 1 |
| agent_no_ocr | citations | 6 | 4 | 2 | 0 | 0 | 4 |
| agent_no_ocr | context | 6 | 6 | 0 | 0 | 0 | 6 |
| agent_no_ocr | element_text | 2 | 2 | 0 | 0 | 0 | 2 |
| agent_no_ocr | evidence | 6 | 6 | 0 | 0 | 0 | 6 |
| agent_no_ocr | tool_result | 6 | 6 | 0 | 0 | 0 | 6 |
| agent_no_rerank | answer | 6 | 1 | 4 | 0 | 1 | 1 |
| agent_no_rerank | citations | 6 | 5 | 0 | 0 | 1 | 5 |
| agent_no_rerank | context | 6 | 5 | 0 | 0 | 1 | 5 |
| agent_no_rerank | element_text | 2 | 2 | 0 | 0 | 0 | 2 |
| agent_no_rerank | evidence | 6 | 5 | 1 | 0 | 0 | 5 |
| agent_no_rerank | tool_result | 6 | 5 | 1 | 0 | 0 | 5 |
| agent_no_visual | answer | 6 | 1 | 4 | 0 | 1 | 1 |
| agent_no_visual | citations | 6 | 3 | 2 | 0 | 1 | 3 |
| agent_no_visual | context | 6 | 5 | 0 | 0 | 1 | 5 |
| agent_no_visual | element_text | 2 | 2 | 0 | 0 | 0 | 2 |
| agent_no_visual | evidence | 6 | 5 | 1 | 0 | 0 | 5 |
| agent_no_visual | tool_result | 6 | 5 | 1 | 0 | 0 | 5 |
| fixed_workflow | answer | 6 | 1 | 3 | 0 | 2 | 1 |
| fixed_workflow | citations | 6 | 3 | 1 | 0 | 2 | 3 |
| fixed_workflow | context | 6 | 4 | 1 | 0 | 1 | 4 |
| fixed_workflow | element_text | 2 | 2 | 0 | 0 | 0 | 2 |
| fixed_workflow | evidence | 6 | 5 | 0 | 1 | 0 | 5 |
| fixed_workflow | tool_result | 6 | 0 | 6 | 0 | 0 | 0 |

tool_result 和 element_text 在默认图中为零权重诊断，不是必达目标。固定工作流不使用工具节点，其 tool_result 为零不代表检索无效；金页重合不证明语义支持。依赖失败前沿不能当作已验证的因果根因。

| 题目 | 配置 | 失败前沿 | 被阻塞节点 | 未知观测 | 预测行 |
| --- | --- | --- | --- | --- | --- |
| chartqa/val/human/32 | fixed_workflow |  | context, answer, citations | evidence | predictions.jsonl#L17 |
| chartqa/val/augmented/238 | fixed_workflow | answer |  |  | predictions.jsonl#L3 |
| pdfvqa/validation/88/12 | fixed_workflow | context | answer, citations |  | predictions.jsonl#L30 |
| pdfvqa/validation/209/0 | fixed_workflow | answer, citations |  |  | predictions.jsonl#L11 |
| pdfvqa/validation/206/18 | fixed_workflow | answer |  |  | predictions.jsonl#L5 |
| chartqa/val/human/32 | agent_full | evidence | context, answer, citations |  | predictions.jsonl#L28 |
| chartqa/val/augmented/238 | agent_full | answer |  |  | predictions.jsonl#L1 |
| pdfvqa/validation/206/18 | agent_full | answer |  |  | predictions.jsonl#L9 |
| chartqa/val/human/32 | agent_no_rerank | evidence | context, answer, citations |  | predictions.jsonl#L21 |
| pdfvqa/validation/88/12 | agent_no_rerank | answer |  |  | predictions.jsonl#L20 |
| pdfvqa/validation/209/0 | agent_no_rerank | answer |  |  | predictions.jsonl#L16 |
| pdfvqa/validation/644/13 | agent_no_rerank | answer |  |  | predictions.jsonl#L10 |
| pdfvqa/validation/206/18 | agent_no_rerank | answer |  |  | predictions.jsonl#L8 |
| chartqa/val/human/32 | agent_no_ocr | answer, citations |  |  | predictions.jsonl#L14 |
| pdfvqa/validation/88/12 | agent_no_ocr | answer |  |  | predictions.jsonl#L7 |
| pdfvqa/validation/209/0 | agent_no_ocr | answer, citations |  |  | predictions.jsonl#L4 |
| pdfvqa/validation/644/13 | agent_no_ocr | answer |  |  | predictions.jsonl#L22 |
| pdfvqa/validation/206/18 | agent_no_ocr | answer |  |  | predictions.jsonl#L24 |
| chartqa/val/human/32 | agent_no_visual | evidence | context, answer, citations |  | predictions.jsonl#L27 |
| chartqa/val/augmented/238 | agent_no_visual | answer |  |  | predictions.jsonl#L19 |
| pdfvqa/validation/88/12 | agent_no_visual | answer, citations |  |  | predictions.jsonl#L26 |
| pdfvqa/validation/644/13 | agent_no_visual | answer |  |  | predictions.jsonl#L23 |
| pdfvqa/validation/206/18 | agent_no_visual | answer, citations |  |  | predictions.jsonl#L15 |

- [suite.json](../data/evaluations/dag-20261007-small/suite.json)
- [summary.json](../data/evaluations/dag-20261007-small/summary.json)
- [node-results.jsonl](../data/evaluations/dag-20261007-small/node-results.jsonl)
