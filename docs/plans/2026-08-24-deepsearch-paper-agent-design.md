# DeepSearch Paper Agent 总体设计

- 日期：2026-08-24
- 状态：设计已确认；**实现范围已于 2026-09-12 收敛**，以 [ADR-0006](../adr/0006-local-json-sqlite.md) 与仓库 README 为准
- 产品类型：本地单用户个人文献研究助手
- 当前阶段：核心链路已实现（导入/混合检索/快速问答/DeepSearch/会话/流式/评测框架）；基础设施按单机路线交付

> **实现偏差（必读）**  
> 本文中的 PostgreSQL + pgvector、Redis + Celery、Docker Compose、Nginx、集合/标签、证据立场、机器翻译、任务取消与断点恢复 **均未实现**。  
> 当前路线见 ADR-0006：JSON 索引快照 + SQLite 会话 + 单进程 uvicorn。  
> 单 PDF 限制：1–100 页、提取文本 ≤ 100,000 字符、上传 ≤ 10 MiB；不支持 OCR 与加密 PDF。  
> 设计中的 500–3000 篇为长期目标，**未**在当前实现上做规模验证。

## 1. 目标

构建一个可以作为完整简历项目展示的论文知识库问答系统。用户批量导入自己的中英文文字型 PDF，按专题集合管理文献，并在选定范围内提出连续问题。系统必须基于本地论文证据回答，每条主要结论能够追溯到论文、PDF 页码和原文片段。

项目的核心价值不是普通的一次性 RAG，而是两级研究流程：快速模式用于直接问题，DeepSearch 模式用于问题拆解、多轮检索、证据缺口检查和自适应停止。

## 2. 已确认范围

### 2.1 数据来源

- 只分析用户主动导入的 PDF。
- 首版不联网检索或下载论文。
- 只支持能直接提取文字的 PDF。
- 不支持扫描件 OCR。
- 目标文献库规模为 500 至 3,000 篇。
- 支持中英文混合论文。

### 2.2 使用方式

- 本机单用户 Web 应用。
- 只监听 `127.0.0.1`。
- 不实现注册、登录、JWT 或多租户权限。
- React + TypeScript 前端。
- FastAPI Python 后端。
- 云端 LLM 使用 OpenAI-compatible 配置接口。
- BGE-M3 在本地生成 1024 维 dense embedding。

### 2.3 问答能力

- 快速模式：单轮混合检索、重排和回答。
- 深度模式：问题拆解、自适应多轮检索、证据缺口检查和回答。
- 支持连续追问。
- 会话历史仅帮助解析指代；每轮仍必须重新检索证据。
- 检索范围支持当前集合、全部文献库或手动选择 PDF。
- 默认检索当前集合。
- 默认中文回答，输出语言可切换。
- 知识边界可在严格文献库模式和模型知识扩展模式之间切换。
- 严格模式为默认值。

### 2.4 证据要求

- 每条主要结论必须关联论文、页码和原文片段。
- 英文或中文原文始终保留。
- 中文机器翻译作为可展开辅助内容，不替代原文。
- 证据必须标记为支持、反对、限制条件或证据不足。
- 证据不足时明确说明，不允许模型补写论文结论。
- 扩展模式中的模型固有知识必须标记为非文献库证据，且不得伪造引用。

### 2.5 会话与界面

- 新会话默认为临时会话。
- 临时会话短期写入数据库，以支持刷新和任务恢复。
- 用户可以命名并永久保存会话。
- 未保存会话默认 24 小时后清理。
- 不内置 PDF 阅读器。
- 证据卡展示论文、页码、原文、翻译和证据立场。
- 通过 SSE 展示任务阶段并流式输出回答。
- 支持取消任务和断线后恢复进度。

## 3. 首版不做

- 自动联网检索论文
- OCR 与复杂版面识别
- 长篇研究报告生成
- Markdown/HTML 研究报告导出
- 内置 PDF.js 阅读器与页内高亮
- Zotero、EndNote 或其他文献管理器同步
- 多 Agent 协作
- 正式离线评测平台
- 公网部署
- Kubernetes、Kafka、Elasticsearch或独立向量数据库

正式评测作为第二阶段，但首版必须保存问题、候选结果、最终证据、检索分数、运行时延、模型版本和索引版本，以便后续构建可复现评测集。

## 4. 总体架构

```text
Browser
  |
  v
Nginx / React
  |
  v
FastAPI API ------------------------------+
  |                                       |
  +--> PostgreSQL + pgvector              |
  +--> Redis --> Celery Worker ------------+
                    |                     |
                    +--> PDF object volume|
                    +--> BGE-M3 GPU       |
                    +--> Cloud LLM API <--+
```

采用模块化单体。API、后台 Worker 和迁移服务共享同一 Python 代码库及镜像，但以不同进程运行。Redis 只负责投递任务；PostgreSQL保存业务状态、任务检查点和事实数据。

## 5. 主要模块

| 模块 | 主要职责 |
|---|---|
| Infrastructure | Compose、容器、网络、卷、迁移、健康检查、备份 |
| Library | PDF 导入、哈希去重、集合、标签、状态与删除 |
| Parsing | 逐页提取、清理、元数据和质量检查 |
| Indexing | 切块、BGE-M3、索引版本和 pgvector 写入 |
| Retrieval | 范围过滤、dense/lexical 检索、融合和重排 |
| Research | 快速问答、问题拆解、迭代检索和停止控制 |
| Evidence | 结论、证据片段、页码、立场和引用校验 |
| Conversation | 临时/保存会话、连续追问和上下文压缩 |
| Streaming | SSE 阶段、Token 流、取消和断线恢复 |
| Frontend | 文献管理、问答界面、证据卡和任务状态 |
| Observability | 日志、指标、成本和错误诊断 |

每个模块在实现前单独提交 `docs/specs/NN-<module>.md` 供审批。

## 6. 文献数据模型

### 6.1 层级

```text
Library
  +-- Collections
  |     +-- Papers (many-to-many)
  +-- Tags
  +-- Papers
        +-- File object
        +-- Pages
        +-- Chunks
        +-- Embeddings
        +-- Notes / Evidences
```

同一篇论文可以属于多个集合，但原始 PDF 只保存一份。原文件使用 SHA-256 内容寻址。完全相同文件按哈希去重；DOI、标准化标题、作者和年份用于提示不同 PDF 版本可能属于同一论文。

### 6.2 索引版本

文本块和向量分表保存。每个索引版本记录：

- parser version
- chunker version
- embedding model and revision
- embedding dimension
- distance metric
- creation time and status

更换 Embedding 模型时构建新版本，通过原子操作切换活动索引，不允许直接覆盖或混用新旧向量。

## 7. PDF 导入流程

```text
Upload
  -> validate PDF and limits
  -> calculate SHA-256
  -> deduplicate
  -> atomically save immutable file
  -> create paper and job
  -> extract page text
  -> clean and quality-check
  -> split page-bounded chunks
  -> generate local embeddings
  -> build lexical index
  -> validate coverage
  -> mark READY
```

首版 Chunk 不跨页，以保证引用页码明确。默认目标长度为 500 至 800 tokens，相邻块重叠 80 至 120 tokens，最终参数必须通过样本 PDF 的切块检查确认。

## 8. 查询流程

### 8.1 快速模式

```text
Question
  -> resolve conversation references
  -> determine search scope
  -> generate search query
  -> dense and lexical retrieval
  -> fuse and deduplicate
  -> rerank
  -> construct evidence set
  -> answer with citations
  -> validate claim-evidence links
```

### 8.2 深度模式

```text
Question
  -> create subquestions
  -> retrieve for each subquestion
  -> extract and classify evidence
  -> inspect coverage and conflicts
  -> formulate next searches
  -> repeat until stop condition
  -> synthesize fragment answer
  -> validate citations
```

自适应停止条件：

- 所有子问题达到最低证据覆盖；或
- 连续两轮没有新增高价值证据；或
- 达到最大五轮；或
- 达到配置的 Token、调用次数或费用上限；或
- 用户取消任务。

回答必须保存实际停止原因。预算耗尽不能表示为证据充分。

## 9. 回答数据契约

```json
{
  "answer": "...",
  "language": "zh-CN",
  "mode": "deep",
  "knowledge_boundary": "corpus_only",
  "claims": [
    {
      "text": "...",
      "support_status": "supported",
      "evidence_ids": ["..."]
    }
  ],
  "evidences": [
    {
      "paper_id": "...",
      "paper_title": "...",
      "page_number": 8,
      "original_text": "...",
      "translated_text": "...",
      "stance": "support",
      "retrieval_score": 0.87
    }
  ],
  "stop_reason": "evidence_saturation"
}
```

前端渲染结构化数据，不从 Markdown 字符串中反向解析引用。

## 10. 任务与故障恢复

任务状态：

```text
QUEUED -> RUNNING -> SUCCEEDED
                  -> RETRY_WAIT
                  -> FAILED
                  -> CANCELLED
                  -> STALE
```

Worker 每个阶段写入进度和心跳。任务函数必须幂等。云端超时、429 和 5xx 使用指数退避；损坏 PDF、无文本和向量维度不匹配直接产生明确的不可重试错误。Worker 重启后扫描超时任务并重新入队。

## 11. 安全与隐私

- PDF 原文、页面、Chunk 和向量保留在本地。
- 只有检索出的少量证据片段发送给云端 LLM。
- 用户切换扩展知识模式时，界面明确提示知识边界变化。
- API Key 只由后端读取，不进入前端、数据库、队列或日志。
- 校验 PDF 文件头、大小、页数和路径。
- 解析器以非管理员用户运行。
- 日志清除 Authorization Header 和可能的密钥字段。

## 12. 可观测性

结构化日志必须包含 `request_id`、`job_id`、`paper_id`、`session_id`、阶段、耗时和错误码。至少收集：

- 导入成功率和失败原因
- 解析、Embedding 和检索耗时
- 队列长度与停滞任务数
- 每次回答的检索轮数和候选数量
- 云端模型调用次数、Token 与估算成本
- 主要结论引用覆盖率
- 磁盘余量

## 13. 验证顺序

每个模块采用以下交付顺序：

1. 模块规格审批。
2. 单元测试。
3. 集成测试。
4. 失败路径与重启恢复测试。
5. 小型真实 PDF 样本验证。
6. 检查数据库、文件和日志产物。
7. 更新模块文档和状态清单。

禁止把设计、部分测试或单次演示称为模块完成。

## 14. 分阶段路线

### 第一阶段：核心功能

1. Infrastructure
2. Library and import jobs
3. Parsing and chunking
4. Local embedding and indexing
5. Hybrid retrieval
6. Evidence-grounded quick answer
7. DeepSearch state machine
8. Conversation and streaming UI
9. Backup, restore and end-to-end verification

### 第二阶段：评测

- 建立中英文问题和人工证据标注集
- Recall@K、MRR 和重排评测
- 引用准确率与无依据结论率
- 快速模式和深度模式对照
- 导入成功率、故障恢复和延迟测试

### 第三阶段：可选扩展

- OCR
- 报告生成
- PDF 阅读器
- 联网论文搜索
- Zotero/EndNote 同步
- 公网部署和身份认证

## 15. 设计冻结规则

本文件描述已确认的产品边界。新增首版功能必须先更新本文件并说明对工期、数据模型、测试和运维的影响。具体实现参数可以在模块规格中调整，但不得削弱本文件规定的证据可追溯性、知识边界和任务可恢复性。
