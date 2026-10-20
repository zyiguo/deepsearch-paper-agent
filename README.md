# DeepSearch Paper Agent

面试与源码导读：[项目技术报告](docs/INTERVIEW_TECHNICAL_REPORT_20261019.md)已更新到 2026-10-20，包含总体架构、模块设计、关键执行链路、恢复与配额机制；[后端工程知识点](docs/BACKEND_ENGINEERING_KNOWLEDGE_20261020.md)按输入、执行顺序、故障场景与源码入口展开。

[高并发、高可用、高性能与 Redis 缓存治理](docs/CONCURRENCY_AVAILABILITY_PERFORMANCE_REDIS.md)结合当前链路解释准入、背压、性能分析、缓存穿透/击穿/雪崩、请求合并与分布式锁，区分现有保护和多副本扩展设计。

最新后端补充：[恢复、工具核对与入库一致性](docs/BACKEND_RECOVERY_CONSISTENCY.md)，包括工作记忆授权恢复、未知调用审计、原任务继续、入库步骤持久化及向量复用。

[运行治理与容量基线](docs/GOVERNANCE_CAPACITY.md) 提供受保护的业务指标、持久告警状态、用户/工作空间成本点配额，以及部署共享的并发准入与合成压测入口。

最新验证：后端 1059 项通过、14 项跳过；合成容量基线的 144 条请求中，68 条完成、76 条被容量保护明确拒绝。实现、验证口径及公开文件范围见 [2026-10-20 发布记录](docs/GITHUB_PUBLICATION_20261020.md)。

本地单用户个人文献研究助手：导入自己的文字型 PDF，在指定文献范围内进行快速问答或迭代式 DeepSearch，所有回答都附带**可核验的证据**（论文、页码、原文片段）。FastAPI 后端 + React 前端，单机运行，开箱即用。

可选小团队模式现已提供本地账号、登录会话、工作空间和 owner/editor/viewer 权限；文献、会话、附件、Wiki、检查点与轨迹按空间隔离，旧单用户数据不会自动共享。启用与边界见 [团队账号与工作空间](docs/TEAM_ACCESS.md)。仍维持单 API 进程部署。

可选 [PostgreSQL 业务数据库](docs/DATABASE.md) 保存账号权限、会话答案、记忆和研究运行元数据，提供连接池、版本升级、只读预检与显式 SQLite 导入。SQLite 保持默认；原文、索引、检查点和独立轨迹继续使用各自存储。

[任务可靠性、数据恢复与质量闭环](docs/RELIABILITY_RECOVERY_QUALITY.md) 提供持久任务、可选幂等提交、恢复身份校验、离线完整备份与隔离还原演练，以及轨迹冻结、人工错例审核和匹配条件回归。执行完成、内部核验与人工正确性分别记录。

可选多模态阅读：独立配置云端视觉模型后，可识别扫描文字、图、表和公式；另配置视觉 Embedding，可直接检索原页图像，与正文检索通过 RRF 融合，并在重排、回答与核验时读取原图。支持为旧文献单独补建图像索引。配置与使用见 [MULTIMODAL_SETUP.md](docs/MULTIMODAL_SETUP.md)。当前按页单向量定位，真实论文集的检索与识别精度仍需评测。

## 功能特性

MiMo-V2.6-Pro 的模型切换、1M 上下文和输出预算说明见 [MIMO_V26_SETUP.md](docs/MIMO_V26_SETUP.md)。
新模型接入、badcase 配对回归与状态转换修复结果见 [PAIRED_REGRESSION_20261006.md](docs/PAIRED_REGRESSION_20261006.md)。

- **Agentic RAG 与 MCP**：Researcher 自主选择检索、读页和按需 OCR 工具，LangGraph 执行并反馈观察，保留回答与引用核验；共享工具服务支持对外 MCP 和受信任外部 MCP 扩展，见 [AGENTIC_RAG.md](docs/AGENTIC_RAG.md) 与 [MCP.md](docs/MCP.md)。MCP 默认关闭，云端端到端效果仍需独立验收。
- **子 Agent 上下文隔离**：主 Agent 传递目标与必要约束，Researcher 独立执行并返回候选结论、证据 ID 和未解决项；调度上下文不携带完整工具轨迹，原文继续供独立核验，见 [SUBAGENT_CONTEXT.md](docs/SUBAGENT_CONTEXT.md)。
- **父子通信轨迹与 badcase 分组**：可选本地记录任务下发、工具观察、结构化结果、裁剪后父输入与后续决策；按内部核验成功、失败和未知分组导出，保留证据和哈希链，见 [COMMUNICATION_TRACES.md](docs/COMMUNICATION_TRACES.md)。
- **多候选证据路径（可选 v3）**：保留证据不足的候选，独立批量核验替代路径，对越界引用与不实引文执行可重放的路径剪枝；默认关闭，机制、误剪统计边界和对照评测见 [多候选证据路径](docs/MULTICANDIDATE_EVIDENCE_PATHS_20261019.md)。
- **候选复用实验（可选 v4）**：稳定编号补证、保守去重及空路径/重复输入守卫；四题配对中正确率持平但总 Token 增加 16.95%，保持实验状态，见 [候选复用与成本评测](docs/CANDIDATE_REUSE_20261019.md)。
- **缺口增量核验（可选 v5）**：保留未知、记录缺失要求、按新引文重开核验并约束诊断长度；本次四题配对正确率持平、总 Token 下降 9.37%，具体触发情况与局限见 [增量核验评测](docs/INCREMENTAL_GAPS_20261019.md)。
- **统一证据 ID 与多模态重排**：内容指纹、来源片段 ID 和原页关联贯穿检索与引用；独立视觉重排器按图片与 Token 预算分批读取原图评分，失败回退原序，见 [CONTENT_IDS_RERANKING.md](docs/CONTENT_IDS_RERANKING.md)
- **公开多模态评测**：ChartQA 与 PDFVQA 独立数据适配、阅读基线、受控重排与 RAG 评测，保留逐题输出、输入指纹及可续跑结果，见 [PUBLIC_MULTIMODAL_EVAL.md](docs/PUBLIC_MULTIMODAL_EVAL.md)
- **DAG 任务评测**：用节点规则、AND/OR 依赖、权重和必达目标定义任务评分，区分失败、依赖阻塞与观测未知；支持对已有消融轨迹离线重评分，保留原始答案指标。自动迁移图属于阶段代理，题目级子任务仍需独立标注，见 [DAG_BENCHMARK.md](docs/DAG_BENCHMARK.md)。
- **多查询混合检索**：LLM 查询扩展（含多轮对话指代消解）→ 多路召回按片段 ID 合并去重 → BM25（jieba 分词）与文本向量加权融合，可与图像向量通过 RRF 融合 → 文字或多模态重排 → Token 预算内贪心组装上下文
- **自研 ANN**：少于 1024 个向量时精确扫描，达到阈值后切换 numpy 实现的 IVF 索引（k-means 聚类 + 探测），支持增量添加
- **迭代式 DeepSearch**：LangGraph 编排的研究工作流（supervisor → researcher → answerer → verifier），多轮补充检索，只保留通过验证且可溯源的结论
- **可核验引用**：回答中的每条结论都对应论文、页码和原文片段，前端内置 PDF 原文定位
- **多轮会话**：会话隔离的对话历史与结构化记忆，可配置记忆范围
- **模型工作记忆**：Agentic RAG 可调用 `working_memory` 读写当前会话的 `PLAN.md`（待实施计划）和 `HISTORY.md`（已实施事项、明确用户偏好），支持版本检查、清空撤销与非证据隔离，见 [WORKING_MEMORY.md](docs/WORKING_MEMORY.md)。
- **LLM Wiki**：从指定文献编译带原文引用的 Markdown 知识页，支持版本更新、关联页、搜索和来源失效检查；Agent 通过只读工具访问，见 [LLM_WIKI.md](docs/LLM_WIKI.md)。
- **动态研究技能**：发现本地 `SKILL.md` 元信息，Supervisor 按需加载正文；支持热更新、手动指定与关闭，保留独立证据核验，见 [SKILLS.md](docs/SKILLS.md)
- **PDF 与图片管理**：上传解析（后台任务）、按资料范围检索、原页预览；会话支持图片附件提问、粘贴图片及附件转入资料库
- **流式输出**：SSE 推送处理阶段，核验完成后分段发送最终答案
- **独立父子 Agent**：父 Agent 分派目标和约束，子 Agent 使用独立消息历史执行并返回结构化结果，最终答案经过独立核验。
- **独立轨迹与离线策略扩展**：轨迹按成功、失败、未知查询和导出；搜索决策模型、旧查询规划和训练管线仅作为离线扩展。
- **可选后训练数据管线**：本地运行轨迹 → 人工审核 → 单步搜索决策 SFT 数据；独立标准库模块，默认关闭，见 [使用说明](docs/POLICY_DATA_PIPELINE.md)

## 当前架构边界

实现为**本机单用户**路线（详见 [ADR-0006](docs/adr/0006-local-json-sqlite.md)）；增加了可选业务运行基础设施，见 [运行与部署](docs/INFRASTRUCTURE.md) 和 [ADR-0008](docs/adr/0008-optional-redis-infrastructure.md)：

- 索引：内存知识库 + JSON 快照（`data/indexes/`）
- 会话：SQLite
- 进程：单进程 FastAPI + 线程池解析 PDF；支持可选 Redis 缓存、限流和 Docker Compose 部署；尚无独立任务 Worker / PostgreSQL
- 运行保障：请求 ID、访问日志、存活与就绪检查；Redis 故障时绕过缓存并回退本地限流
- 限制：单 PDF ≤100 页且正文文本 ≤10 万字符；OCR 需要启用视觉模型；不支持加密 PDF
- 嵌入：OpenAI 兼容云端 API（独立 `.env.embedding`），见 [EMBEDDING_SETUP.md](EMBEDDING_SETUP.md)

早期设计中的 pgvector/任务队列等已标记 Superseded，不代表当前能力；新 Compose 配置以根目录 `compose.yaml` 为准。当前尚未具备多租户认证与资料权限隔离，不应直接作为公网多人服务部署。

## 检索管线

检索与重排由 `knowledge` 统一提供。下面展示演示/离线快速 QA 的一次检索流程；生产子 Agent 按需调用其中的召回与重排，自行决定检索词和是否继续：

```
用户问题 + 对话历史
   │
   ▼
查询扩展（LLMQueryExpander）—— 改写为最多 5 条检索查询，利用历史消解指代
   │
   ▼
多路召回 —— 每条查询独立检索，按 chunk_id 合并去重，保留不同来源
   │
   ▼
混合检索（knowledge_base.py）—— BM25 分数 ⊕ 语义相似度
   │                            min-max 归一化后加权融合（0.5 / 0.5）
   │                            向量侧：<1024 精确扫描，≥1024 切换 IVF
   ▼
可选图像向量召回 —— 与正文检索通过 RRF 融合
   │
   ▼
重排 —— 纯文字 listwise；含视觉页时分批读取原图评分，失败回退原序
   │
   ▼
Token 预算 —— 按上下文窗口贪心填充，放不下的片段跳过
   │
   ▼
交付 —— 生产主线送入 Answerer / Verifier；演示或离线基线生成答案
```

所有 LLM 增强环节（扩展、重排）失败时自动降级为原始行为，不阻断问答。

## 快速开始

环境要求：Python ≥ 3.12、Node.js ≥ 20.19（Vite 7 要求）。

### 1. 安装后端

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade "pip>=26.2,<27"
.\.venv\Scripts\python.exe -m pip install -c constraints-security.txt -e ".[dev]"
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

## 生产问答与离线基线

真实模型的 `/api/ask`、会话问答与 Wiki 编译统一走父子 Agent 主线。Supervisor 分派自包含任务，Researcher 在独立工具循环中执行，只返回有预算的结构化结果；Answerer / Verifier 读取授权原文，最终答案独立核验。工具日志进入子循环与轨迹，不回灌父调度器。

演示模式保持无网络。旧快速 QA、查询规划与搜索策略注入位于 `deepsearch_experiments.legacy.qa`，用于离线对照。`AGENTIC_RAG_ENABLED=false` 不再静默切回旧生产流程，会给出迁移错误；真实模型默认启用父子 Agent。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查与当前模式 |
| GET | `/api/health/live` / `/api/health/ready` | 存活检查 / Redis 与索引写保护状态 |
| POST | `/api/ask` | 单次提问；真实对话模型走 DeepSearch（可指定 `document_ids`） |
| POST | `/api/documents/upload` | 上传 PDF（后台解析，202） |
| GET | `/api/documents` / `/api/documents/{id}` | 文献列表 / 详情 |
| GET | `/api/documents/{id}/pages/{n}/image` | 页面图片（前端 PDF 预览） |
| POST | `/api/sessions` | 创建会话 |
| POST | `/api/sessions/{id}/turns` | 会话内提问（多轮） |
| POST | `/api/sessions/{id}/turns/stream` | 会话内流式提问（SSE） |
| PUT/GET/DELETE | `/api/sessions/{id}/memory` | 结构化记忆管理 |
| GET/DELETE | `/api/sessions/{id}/working-memory` | 查看/清空两个 Markdown 工作记忆文件 |
| GET | `/api/knowledge-base/status` | 索引状态（片段数、快照、脏标记） |
| POST | `/api/knowledge-base/save` / `load` | 持久化 / 恢复向量索引快照 |
| POST | `/api/demo/ingest` | 导入演示文献（演示模式） |

## 项目结构

```text
src/deepsearch_agent/
  core/          标准库共享类型、协议、身份与取消信号
  application/   问答、文献、会话运行、Wiki 编译与资源装配
  agents/        父子任务契约、角色处理、图运行、预算与工具业务
  knowledge/     分块、检索、重排、索引所有权与快照发布
  workspace/     文献、附件、会话、记忆、Wiki 与技能
  adapters/      模型 HTTP、PDF、存储、Redis、MCP
  web/           HTTP 校验、响应、SSE 与中间件
  api.py         保留的 ASGI 薄入口
src/deepsearch_tracing/       标准库轨迹、分类、查询与分组导出
src/deepsearch_experiments/   离线评测、旧基线和审计
src/search_policy/           标准库审核、SFT 导出与训练 reader
frontend/src/app/            视图协调
frontend/src/features/       chat / library / evidence / wiki
frontend/src/shared/         API、类型与共享组件
```

架构图与依赖规则见 [当前架构](docs/ARCHITECTURE.md)，删除与移动明细见 [迁移清单](docs/ARCHITECTURE_MIGRATION.md)，验收证据见 [验证记录](docs/ARCHITECTURE_VALIDATION_20261018.md)。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

测试默认不依赖网络与真实密钥；`test_api.py` 等接口测试需要以演示配置运行（见测试文件内说明）。

## 设计文档

- 总体设计：[docs/plans/2026-08-24-deepsearch-paper-agent-design.md](docs/plans/2026-08-24-deepsearch-paper-agent-design.md)
- 架构决策：[docs/adr/](docs/adr/)（当前依赖与主线以 ADR-0010 为准，持久化遵循 ADR-0006）
- 模块设计：[docs/plans/](docs/plans/)（PDF 上传、LangGraph 工作流、章节分块、流式界面）
- 验收记录：[docs/](docs/)（首次检索门槛、失败诊断、模型联调等）

## 搜索策略与后训练数据

离线搜索模型与 RAG 使用独立接口，在旧基线中只参与**首轮检索词生成**。后续研究调度、补查、回答及引用核验由原工作流负责。模型返回 `answer/UNKNOWN` 不会跳过首次取证，也不会直接发布为最终答案。

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

`src/search_policy` 与共享 `src/deepsearch_tracing` 仅依赖 Python 标准库，可一起迁移到训练环境；RAG 通过可选 recorder/observer 接入。记录默认关闭，写入失败不阻断问答。实际模型输出与回退 Researcher 的查询分别标记来源，审核通过的已执行 search 动作才能进入训练数据。按问题与会话共同分组，避免同组样本跨 train/val。

先运行完全离线的虚构样例（输出目录须为新目录）：

```powershell
.venv\Scripts\python.exe examples\policy_trace_demo.py --output output\policy-demo
.venv\Scripts\python.exe -m search_policy inspect --db output\policy-demo\traces.sqlite3
.venv\Scripts\python.exe -m search_policy verify output\policy-demo\dataset
```

真实采集可在启动后端前设置 `$env:RAG_TRACE_DB = 'data/policy-traces/runs.sqlite3'`。这只开启日志，不配置云端或自动启用策略模型；未接入策略模型时可以记录 RAG 轨迹，但不会生成策略训练样本。

当前交付的是轨迹与**单步搜索决策 SFT** 管线，不是已部署模型或完整 GRPO 训练系统。原始轨迹、审核与最终数据分开保存，不把 RAG 验证器结论自动当作奖励。训练 reader、配置和全部命令见 [数据管线手册](docs/POLICY_DATA_PIPELINE.md)。

完整重构验收见 [验证记录](docs/ARCHITECTURE_VALIDATION_20261018.md)。历史虚构演示的 12 条轨迹导出为 8 条训练、4 条验证样本，仅证明数据契约，不表示模型效果提升。

## 技术栈

Python 3.12 · FastAPI · LangGraph · numpy · jieba · pypdf · React 19 · TypeScript · Vite 7

## 检索质量评测

已完成缺口复用 v4/v5 的 20 题×3 次重复×2 版本真实对照：自然触发 9 条路径，但总 Token 增加 5.06%，尚未验证稳定降本；另复现同引文下正确主张修正被暂缓的安全反例。完整成本区间、独立审计和证据边界见 [重复对照评测](docs/REPEATED_GAP_EVALUATION_20261019.md)，v5 仍保留为实验选项。

优先复用本地 ChartQA、PDFVQA、MuSiQue 和 2Wiki 数据及冻结的历史运行，统一离线重评分、按任务报告并导出成功/失败/未知待复核记录。清单、运行方式、实测分数和范围限制见 [现有数据集评测](docs/EXISTING_DATASETS.md)。该入口不下载数据或调用模型，自动分数不替代人工审核。

已完成 MuSiQue、2Wiki 各新增 100 题的真实模型扩容评测，保留 200 份按自动评分分组的完整轨迹；结果、失败分类与运行方式见 [扩容评测](docs/EXPANDED_EVALUATION_20261018.md)。

这轮失败的逐例证据见 [Badcase 归因](docs/BADCASE_ATTRIBUTION_20261018.md)。基于 HALT 与推理时预算控制论文新增了可选的证据覆盖策略、按角色修复额度和答案类型保护，原流程保留用于对照；实现、配置与验证边界见 [论文驱动改进](docs/EVIDENCE_CONTROLLED_RESEARCH_20261018.md)。

针对 v1 暴露的重复修订与实体解释问题，新增了可执行核验动作、精确输入复用守卫和带证据的假设修正；配置、消融入口及回归边界见 [Badcase 优化 v2](docs/ACTIONABLE_REVIEW_V2_20261019.md)。

已新增 40 条测试文档问题与原文页码标注、Recall@k/MRR@k 脚本及消融入口。数据尚待人工复核；运行方式和指标边界见 [eval/README.md](eval/README.md)。

公开多模态数据集的 Agentic RAG 配对消融、逐阶段追踪与独立工具验收见 [实验手册](docs/AGENTIC_ABLATION.md)。Badcase 使用统一阶段标签、证据定位与独立人工复核记录，规则见 [归因标准](docs/BADCASE_TAXONOMY.md)；自动阶段标签不是已证实的因果根因。

本轮实际完成度、任务分数、工具结果与成本见 [消融报告](docs/AGENTIC_ABLATION_RESULTS.md)，日志及原图抽查见 [Badcase 抽查](docs/AGENTIC_ABLATION_AUDIT.md)。

## 长任务恢复与 Wiki 检索

持久会话任务已接入 [工具执行账本与幂等恢复](docs/TOOL_EXECUTION_LEDGER.md)，区分只读、付费和写入工具；结果不明的付费/写入调用保留 unknown 并阻止自动重试。14 组真实进程退出实验以模拟供应商分别验证请求、效果和计费次数，结果已保存。

流式研究支持取消、重启后从 LangGraph 检查点恢复，以及 Wiki 页面参与原文片段检索。接口和边界见 [Research runs and Wiki retrieval](docs/RESEARCH_RUNS_AND_WIKI.md)。
