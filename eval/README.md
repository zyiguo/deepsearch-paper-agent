# 检索评测：先固定标注，再比较改动

## 当前数据与边界

`retrieval-v1.json` 含 40 个问题、期望文献 URI、实际 PDF 页码和支持原文。
32 条来自已导入的《雾港来信》，8 条来自 `section-integration-fixture.pdf`。
两者都是测试材料，并非真实论文。标注由助手逐页阅读建立，原文匹配校验已通过，但尚未经用户人工复核。
这是一套可运行的回归基线，不能用于声称真实论文检索性能或推广到大规模语料。
后续导入真实论文时，应补充或替换成 30–50 条实际研究问题并人工核对相关页。

每条问题目前标一个足够回答问题的页，并非穷尽所有相关页面；该口径下 Recall@k 等于 Hit@k。
支持在 relevant 中增加多个正确页，届时按唯一正确页数计算召回率。
标注不得根据待评测系统的检索结果反向修改；出现遗漏相关页时统一复核、升数据版本并重跑所有基线。

开发集和测试集各 20 条，按奇偶 PDF 页分组，避免同页问题跨集合，但仍共享文档，不能当作独立泛化测试。
初次全量运行只用于建立 smoke baseline；权重选择用 dev，冻结配置后再运行 test。

## 指标与真实调用链

- Recall@k：前 k 个 chunk 覆盖的唯一正确文献页 / 全部标注正确页，再对问题取平均。
- MRR@k：首个正确 chunk 的排名倒数；前 k 个都不正确记 0，再取平均。不是全排名 MRR。
- 同页多个 chunk 不重复增加召回；但仍占用实际排名位置。
- 跨页 chunk 只认可 page_spans 明确列出的页面，不推断中间页。
- candidate_recall：实际送入重排前的候选集合覆盖率。无重排时候选数量更小，应结合 rows 看候选数。
- semantic-ann 额外报告与精确向量 top-k 的重合率，以及 IVF 分桶数和探测桶数。

`retrieval_pipeline.retrieve` 同时被普通 AnswerService 和评测使用；不生成回答，不评测上下文截断、LangGraph 多轮研究或回答真实性。
保留既有行为：无重排时原问题结果达到 k 会提前退出扩展；重排最多读取前 20 个候选。
这些限制可能使查询扩展收益为零。本次不改变这些行为，以便先建立可比较基线。
默认应用权重仍为 0.5/0.5；增加 semantic_weight 参数用于受控实验。

## 一条命令跑离线基线

从仓库根目录运行（快照需来自当前已保存索引）：

```powershell
.\.venv\Scripts\python.exe -m deepsearch_agent.evaluation --snapshot data/indexes/embedding-37ed086adb0e412980c4e8f3.json --split all --output output/eval/bm25-baseline.json
```

默认仅 BM25，不读取模型密钥、不访问模型服务。索引只读，不会重新导入或覆盖线上数据。
报告保存每条问题的排名、候选、实际扩展查询、模型回退状态，以及数据/索引/源代码哈希。
页码或原文不在快照中会直接失败，不能悄悄删掉难例。

## 消融实验

下面的命令会产生真实 Embedding 和 LLM 调用及费用；没有 `--allow-remote` 不会发起调用。
查询向量缓存位于 output/eval/query-vectors.json，校验模型标识，后续不重复计算相同查询。
LLM 扩展和重排每次重新调用，具有随机性；正式结论应多次运行、保留全部报告，不能只挑最好一次。
.env.embedding 和 .env 必须与应用真实模型配置一致；也可通过参数指定其他配置路径。

```powershell
.\.venv\Scripts\python.exe -m deepsearch_agent.evaluation --snapshot data/indexes/embedding-37ed086adb0e412980c4e8f3.json --split dev --configs bm25 semantic hybrid-0.25 hybrid-0.5 hybrid-0.75 hybrid-expand hybrid-rerank hybrid-both semantic-ann --allow-remote --output output/eval/dev-ablation.json
```

所有 hybrid-* 除权重扫描外都使用 0.5。以 hybrid-0.5 为基准比较 expand/rerank/both。
reranker 只能重排候选，无法找回候选池以外的证据；看最终 Recall@5、MRR@5 和候选召回一起判断。
ANN 对照强制阈值为 1，精确对照阈值为 chunk_count+1。当前仅 18 个 chunk，IVF 会探测全部桶，不能证明大规模 ANN 加速或近似召回。
计时为预热索引后的运行耗时，含缓存命中或远程调用；不能把不同配置间的总耗时直接解释为算法加速。

## 每次改检索后回归

使用同一数据集、同一快照、同一 split/k 和模型配置：

```powershell
.\.venv\Scripts\python.exe -m deepsearch_agent.evaluation --snapshot data/indexes/embedding-37ed086adb0e412980c4e8f3.json --split all --baseline output/eval/bm25-baseline.json --output output/eval/bm25-after.json
```

报告 deltas 给出同名配置的 Recall/MRR 差值，不兼容的基线会拒绝比较。
不要覆盖旧报告。缓存未命中而未允许远程调用时会失败，不回退成演示向量。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_evaluation.py -q
```

简历当前可写“建立 40 条带原文证据的测试文档检索评测集及 Recall@k/MRR@k 回归脚本”。
尚不能写“reranker 将 Recall@5 从 X 提升至 Y”：必须完成真实模型消融与人工复核后再填实测值。
