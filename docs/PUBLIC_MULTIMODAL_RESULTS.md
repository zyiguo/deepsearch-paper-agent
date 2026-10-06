# 公开多模态评测：第一轮实测

整理日期：2026-10-05。这里记录实际运行结果，不是目标值；后续优化必须新建运行目录，不能覆盖或选择性重试本轮失败题。

## 数据与配置

| 项目 | 本轮设置 |
| --- | --- |
| 视觉模型 | `mimo-v2.5`，使用应用独立视觉配置 |
| ChartQA | validation 100 题，human / augmented 各 50 题，98 张原图 |
| PDFVQA | validation 页级公开镜像，A / B 各 50 题，74 页 |
| 抽样 | seed 42；固定清单、原图哈希和来源版本 |
| 网络 | 数据下载经 `http://127.0.0.1:7897`；云端模型 `--direct`，不修改应用或系统代理 |
| 预算 | 32,768 Token 上下文，最多 8 张图，每图预留 2,500 Token，输出最多 1,024 Token |
| 并发 | 问答 2；重排诊断 1 |

来源：[ChartQA 官方仓库](https://github.com/vis-nlp/ChartQA)、[PDFVQA 官方仓库](https://github.com/adlnlp/pdfvqa)、[PDFVQA 页面镜像](https://huggingface.co/datasets/gigant/pdfvqa)。

ChartQA 固定版本为 `044eabfc306abfe9340c5741f0093aefc5973d06`，PDFVQA 镜像版本为 `088f7bfef3c235c99a5222e824e0b5d92c4b23e0`。PDF 镜像扫描 96 页，累计排除 196 个歧义或无效标签；每页每任务最多抽取一题，B 只保留答案文字能唯一匹配 OCR 元素的题目。排除数包含扫描页中未入选题目的无效标签，不能据此计算完整数据集缺失率。

## 给定上下文问答

| 数据 / 任务 | 题数 | 指标 | 得分 | 失败数 |
| --- | ---: | --- | ---: | ---: |
| ChartQA 总体 | 100 | relaxed accuracy | 56.00% | 1 |
| ChartQA human | 50 | relaxed accuracy | 66.00% | 1 |
| ChartQA augmented | 50 | relaxed accuracy | 46.00% | 0 |
| PDFVQA A | 50 | accuracy | 40.00% | 1 |
| PDFVQA B | 50 | 元素集合 exact match | 8.00% | 2 |
| PDFVQA B | 50 | 元素集合 F1 | 31.58% | 2 |

ChartQA 额外文本 exact match 为 51.00%。PDF A 含 49 道 existence 和 1 道 counting，金标准分别为 `no` 32 题、`yes` 17 题、`0` 1 题；全部回答 `no` 的朴素基线为 64.00%，高于本轮模型的 40.00%。A 的类别覆盖很不均衡，不能泛化为完整 PDFVQA A 的表现。

本轮 ChartQA 只输入原图和原问题；PDF A 同样只输入原图，PDF B 还输入给定 OCR、元素 ID 和坐标，不提供答案和关系标注。PDF B 不是纯原图视觉设置，也不是原始官方元素 ID 发布的完整复现。集合 F1 不能等同于论文分类 F1。失败及缺失均保留在分母中，失败题不重试。

问答平均观测延迟：ChartQA 3.61 秒，PDFVQA 3.27 秒。它们包含本次网络、并发与请求开销；未统计真实 Token 账单。

本地原始输出：

- `output/benchmarks/chartqa-val100-oracle/{run.json,predictions.jsonl,report.json}`
- `output/benchmarks/pdfvqa-val100-oracle/{run.json,predictions.jsonl,report.json}`

## 重排诊断

ChartQA 清单的前 10 题，均来自 human。每题候选池包含 1 张金标准图和 7 张固定 seed 的干扰图；不生成答案，模型不知道哪一张是金标准。这是受控候选池诊断，不是自然检索召回成绩。

| 指标 | 固定随机初始排序 | 多模态重排后 |
| --- | ---: | ---: |
| Recall@3 | 40.00% | 60.00% |
| MRR@3 | 0.2833 | 0.6000 |
| nDCG@3 | 0.3131 | 0.6000 |

10 题全部完成，无重排回退，平均耗时 9.05 秒。这个规模只能验证受控排序行为，尚不足以判断稳定提升；ChartQA 原题依赖已给定图像的指代，不能把这个结果当作官方开放检索榜单。输出位于 `output/benchmarks/chartqa-rerank10/`。

## RAG 小样本验收

PDFVQA 的 74 页提供 OCR 建立独立 BM25 索引，共 359 个片段。前 5 题（A 4 题、B 1 题）经过生产 `retrieve`、多模态 reranker 和原图生成适配器：

- 5 题完成，无格式或服务错误；有 2 题 BM25 无命中，空答案计零。
- A accuracy：2/4 = 50.00%。
- B 元素集合 exact match：0/1；集合 F1：0.50。
- 页级 Recall@3：3/5 = 60.00%；平均耗时 4.27 秒。

A/B 原始任务已经给定页面，检索被限制在该页范围内，因此这个页级召回只反映有无返回该页的片段，不能说明多文档选页能力。只有有多候选的请求实际执行视觉重排，单候选请求会跳过重排。输出位于 `output/benchmarks/pdfvqa-rag-smoke5/`。

## 失败与限制

- ChartQA 的 1 个失败为 `ModelServiceError`；仅记录异常类别，不暴露服务凭证，也未据此断言余额或网络原因。
- PDFVQA 的 3 个失败均为格式或任务输出无效：额外 `explanation` 字段、错误元素 ID、把 `answer` 输出成列表。原始输出完整保留。
- PDF A 的 49 道判断题中，16 题被答成计数值；不将数字偷偷转换成 yes/no。B 有 35/50 题选得比金标准集合更多，不从候选输出中事后挑金标准 ID。
- PDF 镜像无法重建整篇文档身份。本轮未运行 Task C；原发布有标注 records / layout / 原图的导入适配已提供，但尚未以原始 C 数据验收。
- 本轮没有执行真实图像向量 / 混合索引的完整公开集对比，也未跑 100 题 RAG。相关命令已实现，不用假向量或假成绩补齐结果。
- 本评测覆盖独立检索、重排和结构化问答，不覆盖 LangGraph supervisor / verifier、动态 skills、会话记忆或完整前端体验。
- 数据是验证子集而不是完整 test benchmark，不可直接与论文表格或排行榜比较。原图与原始预测留在本地，未提交或重新分发数据集。

## 验证与复现

最终完整后端回归测试：**355 passed, 1 skipped**，其中本次公开评测模块 45 项测试通过。两组 100 题预测均已按严格类型、元素目录和页面范围校验离线重算评分，与运行报告一致。复核修复前，ChartQA 100 题已完成严格身份匹配的 `--resume` 验证，无新题执行，错误题未重试。

正式评测完成后，复核发现并修复了原格式 A/B 检索可能扩大到同文档其他页、空金标准被接受、外部预测缺少格式校验三个边界问题；还补充了外部预测元素 ID 与实际输入目录的校验。它们不影响本轮公开镜像数据的成绩。旧 `run.json`、原预测和报告不改写，严格重评分输出另存为 `output/benchmarks/{chartqa,pdfvqa}-val100-catalog-checked-score.json`。当前修复版本的代码指纹已经改变，因此不能拿它对旧运行执行 `--resume`；后续模型实验须新建输出目录。

本轮正式结果的代码指纹：

`ccca82b7f77adaff2feefcd7c72c5e788e2aa06716c322d4b77d812f9349cf64`

数据清单 SHA-256：

- ChartQA：`6930e54edb03bdb949389b4ce3bc5e133d38041bf71d9e7970515b3f37423798`
- PDFVQA：`538b7bcb06b5cf2a74a4e604024f13d2002cd7410e16a217673fafedfb022048`

实现采用 VisRAG 的检索 / 生成分离思路，ChartQA 数值评分参考 Pix2Struct / VLMEvalKit。数据、代码、模型身份和预算必须一致才续跑。完整操作说明与来源见 [评测使用文档](PUBLIC_MULTIMODAL_EVAL.md)；运行输出默认被 Git 忽略，新机器需按文档重新下载和评测。
