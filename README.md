# DeepSearch Paper Agent

本地单用户个人文献研究助手：导入自己的文字型 PDF，在指定文献范围内进行快速问答或迭代式 DeepSearch，所有回答都附带**可核验的证据**（论文、页码、原文片段）。FastAPI 后端 + React 前端，单机运行，开箱即用。

## 功能特性

- **多查询混合检索**：LLM 查询扩展（含多轮对话指代消解）→ 多路召回合并去重 → BM25（jieba 分词）与语义向量加权融合 → LLM listwise 重排 → Token 预算内贪心组装上下文
- **自研 ANN**：少于 1024 个向量时精确扫描，达到阈值后切换 numpy 实现的 IVF 索引（k-means 聚类 + 探测），支持增量添加
- **迭代式 DeepSearch**：LangGraph 编排的研究工作流（supervisor → researcher → answerer → verifier），多轮补充检索，只保留通过验证且可溯源的结论
- **可核验引用**：回答中的每条结论都对应论文、页码和原文片段，前端内置 PDF 原文定位
- **多轮会话**：会话隔离的对话历史与结构化记忆，可配置记忆范围
- **PDF 文献管理**：上传解析（后台任务）、按文献范围检索、页面预览
- **流式输出**：SSE 推送处理阶段，核验完成后分段发送最终答案
- **可选搜索决策模型**：独立模型提供首轮检索词；无效输出或服务失败回退原 Researcher，保留后续补检和引用核验
- **可选后训练数据管线**：本地运行轨迹 → 人工审核 → 单步搜索决策 SFT 数据；独立标准库模块，默认关闭，见 [使用说明](docs/POLICY_DATA_PIPELINE.md)

## 当前架构边界

实现为**本机单用户**路线（详见 [ADR-0006](docs/adr/0006-local-json-sqlite.md)）：

- 索引：内存知识库 + JSON 快照（`data/indexes/`）
- 会话：SQLite
- 进程：单进程 FastAPI + 线程池解析 PDF；无 Docker / Redis / Celery / PostgreSQL
- 限制：单 PDF ≤100 页且提取文本 ≤10 万字符；不支持 OCR 与加密 PDF
- 嵌入：OpenAI 兼容云端 API（独立 `.env.embedding`），见 [EMBEDDING_SETUP.md](EMBEDDING_SETUP.md)

早期设计中的 Compose/pgvector/任务队列等已标记 Superseded，不代表当前能力。

## 检索管线

基础问答路径的完整检索流程（`qa.py`）：

```
用户问题 + 对话历史
   │
   ▼
查询扩展（LLMQueryExpander）—— 改写为最多 5 条检索查询，利用历史消解指代
   │
   ▼
多路召回 —— 每条查询独立检索，按（来源, 页码, 片段）合并去重
   │
   ▼
混合检索（knowledge_base.py）—— BM25 分数 ⊕ 语义相似度
   │                            min-max 归一化后加权融合（0.5 / 0.5）
   │                            向量侧：<1024 精确扫描，≥1024 切换 IVF
   ▼
LLM 重排（LLMReranker）—— listwise 对候选池精选 top_k，失败回退原序
   │
   ▼
Token 预算 —— 按上下文窗口贪心填充，放不下的片段跳过
   │
   ▼
生成 —— 带编号引用的上下文 → 答案 + 来源列表
```

所有 LLM 增强环节（扩展、重排）失败时自动降级为原始行为，不阻断问答。

## 快速开始

环境要求：Python ≥ 3.12、Node.js ≥ 20.19（Vite 7 要求）。

### 1. 安装后端

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 2. 配置模型

后端读取项目根目录下的 `.env`（对话模型）与 `.env.embedding`（向量模型）两个文件，均支持 OpenAI 兼容接口：

```ini
# .env —— 对话 LLM
LLM_BACKEND=openai_compatible
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.example.com/v1
LLM_MODEL=your-model
```

```ini
# .env.embedding —— 向量模型
EMBEDDING_BACKEND=openai_compatible
EMBEDDING_API_KEY=your-provider-key
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSIONS=1024
EMBEDDING_BATCH_SIZE=10
RETRIEVAL_MODE=hybrid
```

注意：**`.env` 文件内容会覆盖同名系统环境变量**。把两个 `*_BACKEND` 都改成 `demo` 可进入无网络的演示模式（内置示例文献，仅支持固定演示问题）。向量模型配置详见 [EMBEDDING_SETUP.md](EMBEDDING_SETUP.md)。

### 3. 启动后端

```powershell
.\.venv\Scripts\python.exe -m uvicorn deepsearch_agent.api:app --reload --host 127.0.0.1 --port 8000
```

启动无网络请求；`/api/health` 返回当前模式（如 `live-llm-live-retrieval`）。

### 4. 启动前端

```powershell
cd frontend
npm install
npm run dev
```

打开 **http://127.0.0.1:5173**（Vite 已将 `/api` 代理到 8000 端口，无需关心跨域）。

## 两种问答方式

| | 快速问答 | DeepSearch 迭代研究 |
|---|---|---|
| 路径 | 单轮检索 → 生成（`qa.py`） | LangGraph 工作流（`research_graph.py`） |
| 行为 | 一次管线出答案 | supervisor 分解问题 → researcher 多轮补充检索 → answerer 起草 → verifier 逐条核验引用 |
| 适用 | 事实型问题 | 需要综合多篇/多处的开放问题 |

当前 Web 服务在使用真实对话模型时为 `/api/ask` 和会话问答统一启用 DeepSearch 图流程；表中的基础问答是 `AnswerService(use_graph=False)` 的代码路径，并非两个 API 的固定分工。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查与当前模式 |
| POST | `/api/ask` | 单次提问；真实对话模型走 DeepSearch（可指定 `document_ids`） |
| POST | `/api/documents/upload` | 上传 PDF（后台解析，202） |
| GET | `/api/documents` / `/api/documents/{id}` | 文献列表 / 详情 |
| GET | `/api/documents/{id}/pages/{n}/image` | 页面图片（前端 PDF 预览） |
| POST | `/api/sessions` | 创建会话 |
| POST | `/api/sessions/{id}/turns` | 会话内提问（多轮） |
| POST | `/api/sessions/{id}/turns/stream` | 会话内流式提问（SSE） |
| PUT/GET/DELETE | `/api/sessions/{id}/memory` | 结构化记忆管理 |
| GET | `/api/knowledge-base/status` | 索引状态（片段数、快照、脏标记） |
| POST | `/api/knowledge-base/save` / `load` | 持久化 / 恢复向量索引快照 |
| POST | `/api/demo/ingest` | 导入演示文献（演示模式） |

## 项目结构

```
src/deepsearch_agent/
├── api.py / document_api.py / conversation_api.py   # FastAPI 接口层
├── qa.py / research_graph.py                        # 问答服务与 LangGraph 研究工作流
├── knowledge_base.py / retrieval_scope.py           # 知识库、按文献范围检索
├── bm25.py / ann.py / embeddings.py                 # 词法检索、ANN、向量客户端
├── query_expander.py / reranker.py / tokens.py      # 查询扩展、LLM 重排、Token 估算
├── chunking.py / section_chunking.py                # 固定分块 / 章节感知分块
├── pdf.py / pypdf_reader.py / pdf_worker.py         # PDF 读取与后台解析
├── documents.py / conversations.py / storage.py     # 文献、会话、持久化
├── llm.py / llm_gateway.py / llm_config.py           # LLM 客户端（Protocol + DI）
├── search_intent.py / trajectory_adapter.py         # 策略兼容入口与可选轨迹桥接
└── prompts.py / demo.py
src/search_policy/                                  # 独立搜索协议、轨迹库、审核导出和训练 reader
examples/policy_trace_demo.py                        # 无网络的完整数据管线演示
frontend/                                            # React + TypeScript + Vite
tests/                                               # 离线单元、API、轨迹与数据契约测试
docs/
├── POLICY_DATA_PIPELINE.md                           # 采集、审核、导出与训练对接手册
├── adr/                                             # 架构决策记录
├── plans/                                           # 总体设计与模块设计文档
└── specs/
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

测试默认不依赖网络与真实密钥；`test_api.py` 等接口测试需要以演示配置运行（见测试文件内说明）。

## 设计文档

- 总体设计：[docs/plans/2026-08-24-deepsearch-paper-agent-design.md](docs/plans/2026-08-24-deepsearch-paper-agent-design.md)
- 架构决策：[docs/adr/](docs/adr/)（以 ADR-0006 的本机架构为准）
- 模块设计：[docs/plans/](docs/plans/)（PDF 上传、LangGraph 工作流、章节分块、流式界面）
- 验收记录：[docs/](docs/)（首次检索门槛、失败诊断、模型联调等）

## 搜索策略与后训练数据

搜索模型和原 RAG 使用独立接口，当前只参与**首轮检索词生成**。后续研究调度、补查、回答及引用核验由原工作流负责。模型返回 `answer/UNKNOWN` 不会跳过首次取证，也不会直接发布为最终答案。

```text
可选搜索策略模型 ──→ 首轮查询 ──→ 现有检索/重排 ──→ 原回答与核验
        │                               │                    │
        └──────────── 可选事件记录 ───────┴────────────────────┘
                                ↓
                      本地 SQLite → 冻结 JSONL
                                ↓
                      独立审核 → 去重与分组切分
                                ↓
                 action-SFT train/val + SHA256 manifest
```

`src/search_policy` 仅依赖 Python 标准库，可单独迁移到训练环境；RAG 通过可选 recorder/observer 接入。记录默认关闭，写入失败不阻断问答。实际模型输出与回退 Researcher 的查询分别标记来源，审核通过的已执行 search 动作才能进入训练数据。按问题与会话共同分组，避免同组样本跨 train/val。

先运行完全离线的虚构样例（输出目录须为新目录）：

```powershell
.venv\Scripts\python.exe examples\policy_trace_demo.py --output output\policy-demo
.venv\Scripts\python.exe -m search_policy inspect --db output\policy-demo\traces.sqlite3
.venv\Scripts\python.exe -m search_policy verify output\policy-demo\dataset
```

真实采集可在启动后端前设置 `$env:RAG_TRACE_DB = 'data/policy-traces/runs.sqlite3'`。这只开启日志，不配置云端或自动启用策略模型；未接入策略模型时可以记录 RAG 轨迹，但不会生成策略训练样本。

当前交付的是轨迹与**单步搜索决策 SFT** 管线，不是已部署模型或完整 GRPO 训练系统。原始轨迹、审核与最终数据分开保存，不把 RAG 验证器结论自动当作奖励。训练 reader、配置和全部命令见 [数据管线手册](docs/POLICY_DATA_PIPELINE.md)。

本次离线验收：**245 项测试通过**；虚构演示的 12 条轨迹导出为 8 条训练、4 条验证样本，哈希校验通过。以上只证明软件数据契约，不表示模型效果提升。

## 技术栈

Python 3.12 · FastAPI · LangGraph · numpy · jieba · pypdf · React 19 · TypeScript · Vite 7

## 检索质量评测

已新增 40 条测试文档问题与原文页码标注、Recall@k/MRR@k 脚本及消融入口。数据尚待人工复核；运行方式和指标边界见 [eval/README.md](eval/README.md)。
