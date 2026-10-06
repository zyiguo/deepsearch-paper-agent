# PDFVQA / ChartQA 公开数据评测

评测代码在 `src/deepsearch_agent/benchmarks/`，不依赖 FastAPI 生命周期，不写入用户文献索引、会话或记忆。无需新增依赖。原始图片、manifest 和运行结果分别位于 `data/benchmarks/` 与 `output/benchmarks/`，默认不提交 Git。

第一轮已经完成的 100 题阅读基线、10 题重排诊断及 5 题 RAG 验收见 [实测报告](PUBLIC_MULTIMODAL_RESULTS.md)。报告将真实结果与尚未执行的设置明确分开。

## GitHub 参考

| 项目 | 采用的设计或规则 |
| --- | --- |
| [PDFVQA 官方仓库](https://github.com/adlnlp/pdfvqa) / [论文](https://arxiv.org/html/2304.06447) | 区分 A 固定答案、B 单页元素选择、C 整篇文档元素集合 |
| [ChartQA 官方仓库](https://github.com/vis-nlp/ChartQA) | 使用原始问题、原图和标签，分开 human / augmented；不使用答案对应的 CSV 表格作为输入 |
| [VisRAG](https://github.com/OpenBMB/VisRAG) | 分离检索评测与给定证据生成，保存检索结果用于分析 |
| [Pix2Struct 指标](https://github.com/google-research/pix2struct/blob/main/pix2struct/metrics.py) / [VLMEvalKit](https://github.com/open-compass/VLMEvalKit/blob/main/vlmeval/dataset/utils/vqa_eval.py) | ChartQA 数值 5% 相对误差、非数值大小写忽略匹配 |
| [ChartQA visual RAG](https://github.com/saeedabc/chartqa_visual_rag) | 原始问题常指代已给定图像，开放检索不能直接冒称原始 ChartQA 任务；本项目不自动改写问题或答案 |

这些是参考项目，不是依赖；不会执行下载仓库中的脚本，也不加载 `trust_remote_code`。

## 三种模式

| 模式 | 输入/流程 | 能说明什么 |
| --- | --- | --- |
| `oracle` | ChartQA/A/B 的已给定原图；C 的完整输入文档页 | 给定上下文阅读能力，不是检索成绩 |
| `rerank` | 固定 seed 的金标准页 + 随机干扰页，调用项目多模态 reranker | 受控候选池排序能力，不是自然召回率；不生成答案 |
| `rag` | 独立 benchmark 索引 → 生产 `retrieve` / `MultimodalReranker` → 结构化生成 | 检索后的回答与排序效果；不覆盖 LangGraph supervisor/verifier、动态 skills 或会话记忆 |

PDFVQA A/B 的原始设置已经指定单页，RAG 在该页来源范围内检索；C 在给定整篇文档范围内检索。原版 ChartQA 的 RAG 在采样图像语料中检索，属于有指代歧义的诊断设置，不能与原始给定图像成绩或完整公开检索榜单直接比较。

PDFVQA B/C 必须返回提供的元素 ID 集合。生成器可以访问输入元素的全部 OCR 文字、坐标和标识，但不访问金标准答案、正确元素 ID 列表或 `relations` 图。B/C 给定 OCR+布局的结果不能宣称是纯原图视觉能力。A 和 ChartQA 只提供原图，不提供元素目录、人工表格或答案。

## 第一轮数据

- ChartQA：固定官方 Git commit，validation human/augmented 各 50 题；保留原题 ID、标签文件哈希和规范 PNG 哈希。
- PDFVQA：通过 [公开页面镜像](https://huggingface.co/datasets/gigant/pdfvqa) 的 JSON 接口获取 validation A/B 各 50 题；固定 seed，记录实际镜像 revision 与页面数。每页每任务最多抽一题。该镜像没有可重建的整篇文档身份，**不用于 Task C**。
- 镜像 B 的答案文字只在唯一匹配页面 OCR 元素时映射为 ID；空标签、歧义和不合法标签计入排除统计。采样集属于明确筛选的子集，不能宣称完整官方 B 成绩，也未证明镜像与原发布逐项一致。
- 官方 PDFVQA 竞赛教程的验证 CSV 不含金标准，导入时拒绝作为有标注集评分。需要原发布有标注数据或自行取得正确标注。
- PKL 只允许由原生数据容器组成的非可执行内容；拒绝 pandas/numpy 类构造及任意全局对象。DataFrame PKL 须在信任来源的隔离环境先导出 records JSON，再导入评测，不会自动执行 pickle。
- 数据集可能存在发布格式、许可证和镜像信息不完善的问题。下载文件保持在本地；发布结果或再分发数据前应核对原发布的许可与引用要求。

## 下载与小样本

数据按用户要求走 `7897`，云端模型用 `--direct` 直连。代理设置仅作用于当前命令进程，不改机器设置或应用的 `.env`。

```powershell
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks download --dataset chartqa --split val --limit 100 --seed 42 --output data/benchmarks/chartqa-val100/manifest.json --allow-network --proxy http://127.0.0.1:7897
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks download --dataset pdfvqa --split validation --limit 100 --seed 42 --output data/benchmarks/pdfvqa-val100/manifest.json --allow-network --proxy http://127.0.0.1:7897
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks run --manifest data/benchmarks/chartqa-val100/manifest.json --mode oracle --limit 5 --workers 2 --output output/benchmarks/chartqa-smoke --allow-remote --direct
```

`--allow-network` 只允许数据下载；`--allow-remote` 显式启用付费模型请求。默认最多 2 个评测并发，允许 1–4；下载文件有大小限制和最多三次网络重试。已存在的 manifest 不会被覆盖。

## 100 题阅读基线

```powershell
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks run --manifest data/benchmarks/chartqa-val100/manifest.json --mode oracle --workers 2 --output output/benchmarks/chartqa-val100-oracle --allow-remote --direct
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks run --manifest data/benchmarks/pdfvqa-val100/manifest.json --mode oracle --workers 2 --output output/benchmarks/pdfvqa-val100-oracle --allow-remote --direct
```

复用 `.env.vision` 的独立模型和凭证，仅在评测进程把结构化答案输出上限设为 1024 Token，不改应用配置。原输出、片段 ID、延迟、错误状态记录在 `predictions.jsonl`。`report.json` 提供总表、任务/问题类型分组、输入及代码指纹。

需要暂停后继续时，追加 `--resume`，要求 manifest、图片校验和、所选题目、代码和模型/预算配置完全匹配。默认不重跑已有错误题；追加 `--retry-errors` 才重新调用模型。代码或配置变化应创建新输出目录，不应覆盖旧结果。

## 重排与 RAG

```powershell
# 固定候选池诊断：原问题不改写，受控注入金标准页
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks run --manifest data/benchmarks/chartqa-val100/manifest.json --mode rerank --pool-size 8 --k 3 --limit 10 --workers 1 --output output/benchmarks/chartqa-rerank10 --allow-remote --direct

# PDFVQA 提供的原 OCR 建立免费 BM25 索引，不调用模型
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks index --manifest data/benchmarks/pdfvqa-val100/manifest.json --retrieval bm25 --output data/benchmarks/pdfvqa-val100/bm25.json
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks run --manifest data/benchmarks/pdfvqa-val100/manifest.json --mode rag --snapshot data/benchmarks/pdfvqa-val100/bm25.json --k 3 --output output/benchmarks/pdfvqa-rag100 --allow-remote --direct

# 图像向量 + 文字混合索引：使用独立 Embedding 配置，会产生费用
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks index --manifest data/benchmarks/chartqa-val100/manifest.json --retrieval hybrid --analyze --output data/benchmarks/chartqa-val100/hybrid.json --allow-remote --direct
```

`image` 只建立图像向量索引；`hybrid` 加文字向量，并使用生产 RRF 融合。ChartQA 没有原生 OCR 内容，BM25 需要 `--analyze` 提取视觉文字/描述，或直接使用图像向量。不会拿问题、金标准标签或答案表格填补检索内容，也不会用假向量代替不可用云端向量服务。

索引按独立 manifest 校验，不允许混入个人文献；模型身份不匹配会拒绝加载。视觉提取缓存按原图内容和模型身份保存。模型权限、余额、原图缺失或上下文预算问题均应作为失败报告。C 的完整文档超出图片/Token 上限时不会偷偷只给金标准页，需要调整真实模型预算或采用另外明确声明的检索设置。

## 原格式导入

```powershell
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks import-chartqa --root E:/datasets/ChartQA --split test --output data/benchmarks/chartqa-full-test/manifest.json
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks import-pdfvqa --questions E:/datasets/pdfvqa/val-taskc-records.json --layout E:/datasets/pdfvqa/val-layout.json --images E:/datasets/pdfvqa/val-images --task C --split val --output data/benchmarks/pdfvqa-taskc/manifest.json
```

原 PDFVQA records 字段：`pmcid`, `question`, `question_type`, `answer`, `global_id`；A/B 还须明确 `page_index`（0 开始，不猜测页码偏移）。`layout[pmcid]['pages'][page_index]` 包含 `name` 和 `objects`；元素包含 `global_id`, `text`, `bbox`。C 会保留给定文档所有页面，答案列表是多个同时成立的元素，不是可任选其一的答案。

## 评分与解释

- ChartQA：`relaxed_accuracy` 使用参考实现的 5% 数值相对误差、百分号转小数和非数值匹配；金标准数值 0 按参考规则回到字符串比较，例如 `0` 与 `0.0` 不匹配。`exact_match` 是额外诊断。不会从长解释里挑一个碰巧匹配的数字，也不把比例表达式自动算成小数。
- PDF A：固定答案 accuracy；PDF B/C：元素集合 exact match 与集合 F1。论文 A/B 报告分类 F1、C 报告 Accuracy；本工具不冒称复现了未取得官方脚本的全部聚合细节。B/C 的 F1 也不是自由文本 Token F1。
- 排序：按独立原页去重的 Recall@k / MRR@k / nDCG@k，同时保留受控候选池的初始排序指标和 reranker 回退数。oracle 不输出虚假的检索指标。
- 失败、缺失预测均计入对应指标分母且记零分；无效模型格式保留原输出。A 和 B 的指标分开统计，不能把不同指标混成一个总体“准确率”。
- 延迟是本次并发、网络与模型请求的观测值，不是固定硬件性能或费用基准；当前不统计实际 Token 账单。

```powershell
# 纯离线重算：不需要任何模型或 Embedding
.\.venv\Scripts\python.exe -m deepsearch_agent.benchmarks score --manifest data/benchmarks/chartqa-val100/manifest.json --predictions output/benchmarks/chartqa-val100-oracle/predictions.jsonl --mode oracle --output output/benchmarks/chartqa-val100-offline-score.json
```

外部预测 JSONL 每行至少包含 `id`、`status`（有效结果为 `ok`）、`answer` 或 `element_ids`。答复字段复用在线严格类型及任务必填项校验，元素 ID 必须属于实际输入目录；RAG 排序须提供页面 ID 列表，PDF 排序不能越过给定输入页范围。无效预测计错误、零分，重复或未知题目 ID 会拒绝评分；缺失题计零。原格式导入及 manifest 校验也拒绝空白/非法 A 标签和缺失/null 的 B/C 标签，显式空集合与缺失标注分开处理。

`score` 按所给 manifest 的全部题目计算分母。例如只预测完整 100 题清单的前 10 题，会报告 90 题缺失，不能把剩余 10 题成绩冒充 100 题结果。小样本运行自身的 `report.json` 会记录本次实际选择的题目列表和对应分母。对同一批问题比较 `oracle`、RAG 和 rerank 时，必须保持数据、原图、模型、预算和 seed 一致，并分别解释任务设置。
