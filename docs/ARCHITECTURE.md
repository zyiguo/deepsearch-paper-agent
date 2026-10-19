# 当前架构与依赖边界

适用版本：2026-10-18 模块整理后。生产行为以本文和 [ADR-0010](adr/0010-isolated-agents-modular-boundaries.md) 为准；旧实验的代码身份与分数见 [历史版本索引](HISTORICAL_VERSIONS.md)。

同日增加的可选团队身份边界见 [ADR-0011](adr/0011-team-workspace-access.md) 与 [操作说明](TEAM_ACCESS.md)：`application.bootstrap_team` 负责多工作空间资源装配，`application.access` 负责身份与成员策略，`adapters.persistence.access` 独立保存身份数据，`web.access` 在访问业务路由前执行授权与空间分派。每个空间复用下述原业务架构和数据格式，不复制问答实现。

可选 PostgreSQL 业务存储见 [ADR-0012](adr/0012-postgresql-business-store.md)：装配层通过 `adapters.persistence.business` 选择身份、会话/记忆与运行仓储；所有空间共享一个受控连接池，并以事务局部空间、复合约束和强制 RLS 隔离业务行。`MemoryManager` 只调用存储公开操作，不执行 SQL。文件/索引/检查点/轨迹保持独立；启动不自动升级表或迁移旧数据。操作与故障边界见 [数据库说明](DATABASE.md)。

## 业务主线

任务可靠性与恢复边界由 [ADR-0013](adr/0013-task-recovery-quality-loop.md) 补充：同步/流式会话共用持久任务和每空间最多 4 个活动任务的容量；同步请求及后台任务共同参与关闭取消和完成等待。`application.run_reliability` 只处理身份和安全诊断；存储适配器执行条件状态转换，已提交轮次用于修复状态歧义。API 根生命周期与离线维护共用部署文件锁；`maintenance_cli` 调用恢复适配器，备份和还原不经过业务 Web 路由。`deepsearch_tracing.quality` 独立冻结、审核与比较轨迹，生产问答不导入该评测流程。操作见 [运行保障说明](RELIABILITY_RECOVERY_QUALITY.md)。

真实问答统一由父 Agent 分派自包含目标与必要约束，子 Agent 在独立消息历史中执行工具循环，返回有预算的结构化结果。父调度器读取候选结论、证据 ID、缺口和停止原因；Answerer 与独立 Verifier 读取授权原文。只有核验后的最终答案可以保存并通过 SSE 发布。演示模式保留无网络执行。

```mermaid
flowchart TD
    UI[Web / CLI] --> APP[application 业务用例与资源装配]
    APP --> AG[agents 父子编排与独立核验]
    APP --> WS[workspace 文献、会话、记忆、Wiki]
    AG --> KB[knowledge 检索与索引服务]
    WS --> KB
    AG --> PORT[core 证据、答案、模型与存储协议]
    KB --> PORT
    WS --> PORT
    AD[adapters HTTP、PDF、SQLite、PostgreSQL、JSON、Redis、MCP] --> PORT
    APP -.装配与注入.-> AD
    AG -.业务事件.-> EV[deepsearch_tracing 事件接口]
    APP -.注入记录器.-> EV
    EV --> TRACE[独立轨迹库、分类与只读导出]
    OFF[deepsearch_experiments / search_policy 离线扩展] --> APP
    OFF --> TRACE
```

图表示业务调用、接口实现和事件流；精确 Python 导入边界由 `python -m deepsearch_experiments.architecture` 检查。

## 目录与所有权

| 模块 | 所有权与主要入口 |
|---|---|
| `deepsearch_agent.core` | 标准库共享契约：`TextChunk`、`SectionConfig`、`Answer`、引用、`LLMResponse`、模型错误与安全诊断、取消信号、内容身份、实际跨边界协议、资源根路径 |
| `deepsearch_agent.application` | `AnswerService` 生产入口；`session_runs` 管理有序轮次、线程、取消、恢复与保存；`library_service` 组织问答与索引用例；`WikiCompiler` 复用公开问答服务；`bootstrap` 装配并关闭资源 |
| `deepsearch_agent.agents` | `runtime` 承载 LangGraph 主图；`agentic_research` 承载子图；角色处理分为 coordinator/researcher/answering，模型调用与上下文组装分离；任务契约、预算守卫与工具业务服务独立 |
| `deepsearch_agent.knowledge` | 分块算法、BM25、ANN、图像检索、重排、查询扩展、召回与上下文组装；`IndexService` 独占知识库、锁、写保护、脏标记和快照发布 |
| `deepsearch_agent.workspace` | 文献原件、附件、会话、用户/工作记忆、Wiki 页面与技能；文献库只接收索引服务，不接收 FastAPI 状态对象 |
| `deepsearch_agent.adapters` | 统一模型网关、配置与身份、PDF 子进程、视觉/Embedding、Redis、MCP、JSON/SQLite/PostgreSQL/checkpoint 持久化；工具包装器把业务工具转换成 LangGraph 工具 |
| `deepsearch_agent.web` | HTTP 校验与错误转换、响应模型、SSE 编码、中间件和路由；`RuntimeState` 为原 HTTP 消费方提供索引状态视图，数据仍由 `IndexService` 持有 |
| `deepsearch_tracing` | 仅标准库；事件作用域、SQLite 哈希链、可选记录生命周期、分类、父子交接查询与独立分组导出；不导入应用模型错误或 SDK |
| `deepsearch_experiments` | 评测、消融、多跳对照、旧快速 QA/查询规划、审计及依赖检查；不由生产启动加载 |
| `search_policy` | 标准库离线训练扩展：决策协议、审核、分组切分、SFT 导出与 reader；通过公共轨迹包读取审计流 |

前端 `app/App.tsx` 只协调视图、文献选择范围和证据弹层；`features/library/useLibrary.ts` 管理文献请求、轮询与快照操作，`LibrarySidebar.tsx` 负责文献库呈现。会话、证据预览、Wiki 分别位于 `features/chat`、`features/evidence`、`features/wiki`，请求类型与客户端位于 `shared/api.ts`。

## 强制依赖规则

1. `core` 和 `deepsearch_tracing` 只使用标准库及自身契约；分块元数据校验不再反向导入分块算法。
2. 生产业务禁止导入 `search_policy`、评测运行器，以及轨迹 SQLite/导出/分类实现。显式调用的历史 CLI 薄入口是唯一离线桥接例外。
3. 问答服务接收 `RunRecorder` / `EventSink`；应用轨迹适配器将统一网关异常转换为安全诊断。记录器不识别供应商异常或复制原始错误消息。
4. 工具业务服务不构造 LangGraph 工具。运行实现/装配层注入 `langgraph_tools`；框架只出现在主图、子图及工具适配器中。
5. 应用用例不导入 FastAPI/Starlette 或 Web DTO；`bootstrap` 与 `mcp_cli` 是明确的装配边界。HTTP 路由只校验、转换并调用应用用例。
6. `IndexService.publish` 先持久化、再切换活动快照；保存失败不替换原知识库。启动、手动恢复和文献后台入库共用同一所有者。
7. 只有实际跨模型、证据、记录和存储边界使用协议；不为每个内部类增加接口。

检查器包含函数内延迟导入，避免用“改为局部 import”隐藏循环。CI 回归还在独立进程中验证生产启动不加载离线包，关闭记录时不加载轨迹数据库实现。

## 问答与轨迹生命周期

```mermaid
sequenceDiagram
    participant Web as Web / CLI
    participant App as 应用用例
    participant Parent as 父调度器
    participant Child as 独立子循环
    participant Verify as Answerer / Verifier
    participant Trace as 可选轨迹记录器
    Web->>App: 已校验的问题、资料范围和技能
    App->>Parent: 请求内索引快照与共享预算
    Parent->>Child: SubagentTask 目标、约束、缺口
    Child->>Child: 工具规划、观察、证据授权与预算
    Child-->>Parent: SubagentResult 候选结论与证据 ID
    Parent->>Verify: 授权原文与待核验结论
    Verify-->>App: 独立核验后的最终答案或明确的不完整结果
    App->>App: 检查取消信号并保存最终结果
    App-->>Web: 原格式响应 / SSE
    App-->>Trace: 业务事件、运行 ID 与恢复关联
    Trace->>Trace: success / failure / unknown 分类与导出
```

- 主图检查点保存业务状态、可信证据及结构化交接；子图禁止继承父图检查点与消息历史。
- 逐会话锁保证轮次顺序；模型调用不长期持有索引锁。`RunWorkers` 拥有工作线程，退出时先取消并等待线程，再关闭 PDF 执行器、检查点连接和模型/Redis 资源。启动中途失败也按注册顺序释放已创建资源。
- 主流程运行 ID、每次记录 ID、子任务 ID、模型调用 ID 分别保留；恢复会产生新的记录并关联同一业务运行，不合并或覆盖历史尝试。
- 原始事件留在一条完整哈希链中。运行分类持久化为终态事件，导出到 `success/`、`failure/`、`unknown/`。内部核验、子任务完成、人工正确性、根因标签分别保存。
- 未结束、记录损坏/失败、缺失核验或最终答案的数据保持未知；人工标签默认 `null`。记录失败不改变问答结果，也不自动补为成功。
- Redis 只是可丢弃缓存和限流计数器；配置身份隔离、故障绕过/本地回退与资源关闭保持原行为。SQLite、原文和 JSON 索引仍是持久数据来源。

## 配置与数据兼容

`core.paths.PROJECT_ROOT` 集中定位项目根目录，默认指向仓库根；可显式设置 `DEEPSEARCH_ROOT`。模块移动不参与数据目录推算。现有 `create_app(index_path=..., conversation_path=..., trace_path=...)` 注入路径和模型/资料身份仍有效；没有迁移或改写 `.env`、文献、检查点和人工审核文件。

真实模型默认启用父子 Agent。显式 `AGENTIC_RAG_ENABLED=false` 启动失败并提供离线入口提示，避免静默退回旧生产流程。`create_app(search_intent=...)` 同样给出离线迁移提示。旧算法通过 `deepsearch_experiments.legacy.qa.AnswerService` 显式使用；演示仍由配置选择 `DemoAnswerService`。演示和离线快速 QA 共用检索回答实现，不维护两套检索业务。

共享数据类型改变 Python 位置，SQLite 表、JSON schema、证据 ID、事件字段和 SHA256 计算保持原格式。唯一历史类路径别名为 `deepsearch_agent.chunking.TextChunk`；检查点适配器明确允许旧/新两个证据类路径，支持严格反序列化配置。旧文件直接读取，不批量重写。

评测代码身份仍使用 SHA256，但计算范围覆盖移动后的应用、轨迹与离线包；本次源码重构自然产生新的代码身份。已有实验可只读重新评分，不能用新代码冒充同一实现继续带身份校验的旧实验；新运行使用新目录。

当前验证、删除明细和命令见 [迁移清单](ARCHITECTURE_MIGRATION.md)、[验证记录](ARCHITECTURE_VALIDATION_20261018.md) 与 [容器及真实 Redis 验收](CONTAINER_REDIS_E2E_20261018.md)。
