# 纯文本公开语料（用于检索评测）

目标：在**无图片、文字可提取**的公开材料上做检索回归，避免扫描件/复杂图表干扰。  
应用硬限制：单 PDF ≤100 页、提取文本 ≤100,000 字符、≤10 MiB。

## 已验证可下载源（2026-09-12 本机抽检）

| 来源 | 示例 | 抽检结果 | 许可 | 适合 |
|---|---|---|---|---|
| IETF RFC Editor | `rfc-editor.org/rfc/rfcNNNN.txt` → 转 PDF | 纯文本、无图、结构清晰 | IETF Trust，可再分发 | 协议事实型问答、多文档检索 |
| ACL Anthology | `aclanthology.org/N19-1423.pdf`（BERT）、`.../2020.emnlp-main.523.pdf`（RAG） | 13–16 页，约 5–6 万字符，页页可提取 | 论文页通常可下载；**入库请自行确认再分发条款** | 真实“论文感”语料 |
| PLOS ONE | `journals.plos.org/...&type=printable` | 21 页，约 5 万字符 | 多为 CC-BY | 开放获取论文 |
| Project Gutenberg | `gutenberg.org/cache/epub/84/pg84.txt`（Frankenstein） | 纯文本小说，可转 PDF | 公有领域 | 长文跨页指代、叙事检索 |
| NIST SP 800-53 | `nvlpubs.nist.gov/.../NIST.SP.800-53r5.pdf` | 492 页，**超 100 页限制** | 公开出版物 | 需裁剪后再用 |

本环境访问 `arxiv.org` / `export.arxiv.org` 连接会被重置，**未能**稳定拉取 arXiv PDF。若你的网络可访问，理论/纯文本类 arXiv 论文仍是优质补充。

## 一键生成 RFC 文本 PDF

```powershell
# 仓库根目录
.\.venv\Scripts\python.exe examples\eval_corpus\build_text_pdfs.py --fetch
```

输出：`output/eval-corpus/pdf/*.pdf` 与 `manifest.json`（页数、提取字符数）。

默认 RFC（已验证可落入 100 页 / 10 万字符限制）：

| ID | 主题 | 本机生成结果 |
|---|---|---|
| rfc8259 | JSON | 9 页 / ~2.2 万提取字符 |
| rfc7519 | JWT | 17 页 / ~5.2 万提取字符 |
| rfc7232 | HTTP 条件请求 | 16 页 / ~4.8 万提取字符 |

**体积超限、脚本会拒绝**：`rfc6749`（OAuth，约 12 万字符）、`rfc3986`（URI，约 10.3 万字符）。需要时可按章节裁成多份再导入。

示例问题见 [sample_questions.json](sample_questions.json)（**英文问题**，与英文语料一致；中文问题对英文 BM25 基本无效）。正式写入 `eval/retrieval-v1.json` 前须人工核对页码。

不要把生成 PDF 提交进 git（`output/` 已忽略）；需要固定基线时，用 `manifest.json` 记录哈希与页数。

## 离线 BM25 评测（已跑通）

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe examples\eval_corpus\evaluate_rfc_bm25.py
```

2026-09-12 本机结果（3 份 RFC、262 chunk、6 题）：

| 指标 | 值 |
|---|---|
| Recall@5 | **0.9167** |
| MRR@5 | 0.6667 |
| candidate_recall@5 | 0.9167 |

报告：`output/eval/rfc-bm25.json`。局限：仅 BM25、样本极小、金标由引文定位生成，**不能**当作真实论文检索性能结论。中文对照仍用《雾港来信》套件。

## 论文语料 + hybrid/rerank 消融（已跑通）

本地论文（`output/eval-corpus/papers/`，**不入库 git**）：

| 文件 | 来源 | 说明 |
|---|---|---|
| `acl-bert-2019.pdf` | ACL Anthology N19-1423 | BERT，16 页 |
| `rag-lewis-2020.pdf` | NeurIPS 2020 | Lewis et al. RAG，16 页 |
| （曾误标）`acl-rag-2020.pdf` | 实为 LUKE (EMNLP 2020) | 未纳入题库 |

题目：`paper_questions.json` + RFC `sample_questions.json`，共 17 条可用。

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe examples\eval_corpus\evaluate_corpus_ablation.py `
  --configs bm25 hybrid-0.5 hybrid-rerank hybrid-both `
  --allow-remote `
  --output output/eval/corpus-ablation.json
```

2026-09-12 实测（532 chunk，k=5，Embedding `text-embedding-v4`，重排 `mimo-v2.5-pro`）：

| 配置 | Recall@5 | MRR@5 | candidate_recall |
|---|---:|---:|---:|
| bm25 | 0.7059 | 0.5441 | 0.7059 |
| hybrid-0.5 | 0.7647 | 0.5951 | 0.7647 |
| **hybrid-rerank** | **0.9412** | **0.8627** | **1.0000** |
| hybrid-both（扩展+重排） | 0.9412 | 0.8137 | 1.0000 |

解读（样本仅 17 题，结论限于本语料）：

- 相对 BM25，**hybrid 重排把 Recall@5 提高约 +23.5 个百分点**，MRR@5 +31.9 个百分点。
- 重排候选召回率达到 1.0：瓶颈在排序而非候选池。
- 加上查询扩展后 MRR 略降，说明扩展在该小英文集上不稳定，不能单独宣称有益。
- 这些数字**可以**写进简历/作品集时注明条件：公开英文论文+RFC 语料、17 题、固定模型版本；**不能**外推到大规模中文学术库。

## 推荐组合（贴近“多篇论文库”）

1. **协议库（可公开入库）**  
   5–8 个短 RFC PDF，问：状态码语义、OAuth 授权类型、JWT `alg` 字段、URI 百分号编码等，引用应能落到具体 RFC 页。
2. **论文抽检（本地，不一定提交）**  
   ACL BERT + RAG + 一篇 PLOS，问：训练数据规模、检索器类型、评测集名称、限制条件。
3. **对照中文**  
   保留现有《雾港来信》+ 合成 fixture，验证中文分词与章节分块，不与英文协议库混为同一“文献规模”叙事。

## 标注建议（对接 `eval/retrieval-v1.json` 口径）

- 每条问题标注 `relevant`：文档 URI + **PDF 实际页码** + 支持原文（可复制粘贴）。
- 不要根据检索结果反向改标注；发现相关页遗漏时升数据版本。
- 英文问题与中文问题分开 split，避免只优化某一分词器。

## 暂不推荐

- 扫描版书籍 / 会议 PPT PDF（无 OCR 会直接拒识）
- 含大量公式图片的论文（文字层残缺时引用会空）
- 超过 100 页的规范全文（可先按章节裁成多份）
