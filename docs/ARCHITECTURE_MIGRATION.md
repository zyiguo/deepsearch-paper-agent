# 删除与迁移清单

适用版本：2026-10-18。下面的删除均针对代码；用户文献、会话、索引、检查点、轨迹和人工审核文件没有被清理。

## 已删除的重复实现

| 原实现 | 处理结果 | 有效行为覆盖 |
|---|---|---|
| `deepsearch_agent/qwen.py` | 删除；统一走 `adapters/llm_gateway.py` | 当前统一网关的结构化输出、限额、取消、安全错误码和 Token 可空性测试 |
| `deepsearch_agent/openai_client.py`、`config.py` | 删除旧客户端和专属 AppConfig/旧环境变量体系 | 原传输断言迁至 `tests/test_model_gateway_transport.py`；当前配置和错误行为仍由现有网关/模型配置测试覆盖 |
| `deepsearch_agent/embeddings.py` | 删除旧 Embedding 客户端；协议移入 core，归一化移入 knowledge | `test_embedding_runtime`、语义/混合/图像检索与 Redis 身份隔离回归 |
| `deepsearch_agent/llm.py` | 合并重复响应/异常；Fake 客户端移入 demo 适配器 | Protocol/Fake 行为、模型诊断与缺失用量回归保留 |
| `deepsearch_agent/pypdf_reader.py` | 删除直接解析器；`adapters/pdf.py` 复用受控 PDF 子进程 | 原逐页提取测试迁到 `tests/test_pdf_reader.py`；上传、页数/文本限额、空白/损坏 PDF 测试保留 |
| 原模块内部重复 `TextChunk`、答案/引用、取消信号、通用轨迹实现 | 单一类型或实现；旧路径不维持通用转发层 | 旧索引、会话、哈希链、pickle 与真实图检查点夹具 |

仅删除 `tests/test_config.py` 中专属于已移除 AppConfig 的 5 个测试；旧客户端和 PDF 测试迁移为当前实现的行为测试，没有用删测试消除回归。测试规模变化见验证记录。

## 共享契约与轨迹提取

| 原位置 | 新位置 |
|---|---|
| `chunking.TextChunk` | `core.evidence.TextChunk`；根 `chunking.py` 仅保留反序列化别名 |
| `section_chunking.SectionConfig` | `core.chunking_config.SectionConfig` |
| `qa.Answer/SourceReference` 与格式化函数 | `core.answers` |
| 网关/旧客户端响应和模型错误 | `core.models` |
| `research_runs` 的取消信号 | `core.cancellation` |
| `content_identity` 与语料 SHA256 指纹 | `core.identity` |
| `observability`、`search_policy.events/store`、`trajectory_adapter`、通信导出 | `deepsearch_tracing.context/events/store/recorder/outcomes/communication`；安全模型诊断位于 `adapters.tracing` |

`search_policy.events/store` 留有训练侧公共转出接口，指向同一标准库轨迹实现，未保留第二套库或哈希算法。

## 完整模块移动映射

下表是原内部导入位置到当前模块位置的映射；不是通用兼容承诺。共享类型按上表使用 canonical 位置。

| 原模块 | 新模块 |
|---|---|
| `deepsearch_agent.agent_config` | `deepsearch_agent.agents.agent_config` |
| `deepsearch_agent.agentic_research` | `deepsearch_agent.agents.agentic_research` |
| `deepsearch_agent.ann` | `deepsearch_agent.knowledge.ann` |
| `deepsearch_agent.benchmarks` | `deepsearch_experiments.benchmarks` |
| `deepsearch_agent.bm25` | `deepsearch_agent.knowledge.bm25` |
| `deepsearch_agent.chunking` | `deepsearch_agent.knowledge.chunking` |
| `deepsearch_agent.conversation_api` | `deepsearch_agent.web.conversation_api` |
| `deepsearch_agent.conversations` | `deepsearch_agent.workspace.conversations` |
| `deepsearch_agent.demo` | `deepsearch_agent.adapters.demo` |
| `deepsearch_agent.document_api` | `deepsearch_agent.web.document_api` |
| `deepsearch_agent.documents` | `deepsearch_agent.workspace.documents` |
| `deepsearch_agent.embedding_runtime` | `deepsearch_agent.adapters.embedding_runtime` |
| `deepsearch_agent.evaluation` | `deepsearch_experiments.retrieval` |
| `deepsearch_agent.image_api` | `deepsearch_agent.web.image_api` |
| `deepsearch_agent.image_assets` | `deepsearch_agent.workspace.image_assets` |
| `deepsearch_agent.image_embeddings` | `deepsearch_agent.adapters.image_embeddings` |
| `deepsearch_agent.image_index` | `deepsearch_agent.knowledge.image_index` |
| `deepsearch_agent.infrastructure` | `deepsearch_agent.adapters.infrastructure` |
| `deepsearch_agent.knowledge_base` | `deepsearch_agent.knowledge.knowledge_base` |
| `deepsearch_agent.llm_config` | `deepsearch_agent.adapters.llm_config` |
| `deepsearch_agent.llm_factory` | `deepsearch_agent.adapters.llm_factory` |
| `deepsearch_agent.llm_gateway` | `deepsearch_agent.adapters.llm_gateway` |
| `deepsearch_agent.llm_wiki` | `deepsearch_agent.workspace.llm_wiki` |
| `deepsearch_agent.local_files` | `deepsearch_agent.adapters.persistence.local_files` |
| `deepsearch_agent.mcp_config` | `deepsearch_agent.adapters.mcp_config` |
| `deepsearch_agent.mcp_runtime` | `deepsearch_agent.adapters.mcp_runtime` |
| `deepsearch_agent.memory` | `deepsearch_agent.workspace.memory` |
| `deepsearch_agent.multimodal` | `deepsearch_agent.workspace.multimodal` |
| `deepsearch_agent.multimodal_reranker` | `deepsearch_agent.knowledge.multimodal_reranker` |
| `deepsearch_agent.operations` | `deepsearch_agent.web.operations` |
| `deepsearch_agent.pdf` | `deepsearch_agent.core.pdf` |
| `deepsearch_agent.pdf_worker` | `deepsearch_agent.adapters.pdf_worker` |
| `deepsearch_agent.prompts` | `deepsearch_agent.core.prompts` |
| `deepsearch_agent.qa` | `deepsearch_agent.application.answer_service` |
| `deepsearch_agent.query_expander` | `deepsearch_agent.knowledge.query_expander` |
| `deepsearch_agent.reranker` | `deepsearch_agent.knowledge.reranker` |
| `deepsearch_agent.research_graph` | `deepsearch_agent.agents.runtime` |
| `deepsearch_agent.research_runs` | `deepsearch_agent.adapters.persistence.research_runs` |
| `deepsearch_agent.retrieval_pipeline` | `deepsearch_agent.knowledge.retrieval_pipeline` |
| `deepsearch_agent.retrieval_scope` | `deepsearch_agent.application.scope` |
| `deepsearch_agent.search_intent` | `deepsearch_experiments.search_intent` |
| `deepsearch_agent.section_chunking` | `deepsearch_agent.knowledge.section_chunking` |
| `deepsearch_agent.skill_api` | `deepsearch_agent.web.skill_api` |
| `deepsearch_agent.skills` | `deepsearch_agent.workspace.skills` |
| `deepsearch_agent.source_schemas` | `deepsearch_agent.web.source_schemas` |
| `deepsearch_agent.storage` | `deepsearch_agent.adapters.persistence.storage` |
| `deepsearch_agent.subagent_contracts` | `deepsearch_agent.agents.subagent_contracts` |
| `deepsearch_agent.tokens` | `deepsearch_agent.core.tokens` |
| `deepsearch_agent.tool_adapters` | `deepsearch_agent.adapters.tool_adapters` |
| `deepsearch_agent.tool_service` | `deepsearch_agent.agents.tool_service` |
| `deepsearch_agent.vision` | `deepsearch_agent.adapters.vision` |
| `deepsearch_agent.vision_config` | `deepsearch_agent.adapters.vision_config` |
| `deepsearch_agent.visual_evidence` | `deepsearch_agent.adapters.visual_evidence` |
| `deepsearch_agent.wiki_api` | `deepsearch_agent.web.wiki_api` |
| `deepsearch_agent.working_memory` | `deepsearch_agent.workspace.working_memory` |
| `deepsearch_agent.budget_control` | `deepsearch_agent.agents.budget_control` |
| `scripts/audit_*.py` 的实现 | `deepsearch_experiments.audits` |

## 新增职责拆分

- 原 `research_graph` 拆为 `agents/runtime.py`、角色处理、模型调用和上下文组装；LangGraph 路由终止映射位于运行实现。
- 原 API 主体拆为 `application/bootstrap.py`、`application/library_service.py`、`application/session_runs.py` 和 `web` 路由/响应转换；会话线程具有独立生命周期所有者。
- 原 `DocumentLibrary(state)` 改为接收 `IndexService`；后者拥有索引、锁、写保护、恢复和原子发布。
- Wiki 编译从工作区存储移入 `application/wiki.py`，通过公开生产问答接口接收独立核验结果；只允许稳定入库证据用于可复用 Wiki。
- 旧快速 QA/查询规划位于 `deepsearch_experiments.legacy`；演示与离线快速 QA 共用 `application/retrieval_answer.py` 的真实检索/上下文/模型适配器实现。
- `frontend/src/App.tsx` → `app/App.tsx`；文献状态/请求 → `features/library/useLibrary.ts`；文献侧栏 → `LibrarySidebar.tsx`；其余页面按 chat/evidence/wiki 与 shared 分类。

## 保留的薄入口

- `deepsearch_agent.api:app` / `create_app`，保持 ASGI 和嵌入式应用入口。
- `python -m deepsearch_agent.pdf_worker`，保持子进程入口及固定安全错误消息。
- `python -m deepsearch_agent.mcp_runtime`。
- `python -m deepsearch_agent.evaluation`、`deepsearch_agent.communication_traces`。
- `python -m deepsearch_agent.benchmarks`、`benchmarks.ablation`、`ablation_report`、`dag`、`paired_regression`、`benchmarks.multihop`。
- `python scripts/audit_release.py`、`audit_mainline_pilot.py`、`audit_budget_pilot.py`。
- `python -m search_policy` 与现有 examples 脚本。

生产入口不会导入这些离线运行器。历史 CLI 薄模块只转发，不承诺原内部符号仍可导入。

## 明确迁移的配置行为

`AGENTIC_RAG_ENABLED=false` 不再选择旧生产流程，真实模型启动时提示改为 true，或显式使用 `deepsearch_experiments.legacy.qa.AnswerService`。`create_app(search_intent=...)` 也改为明确错误。模型、向量身份、数据目录和 `.env` 内容均不自动更换。
