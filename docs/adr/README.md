# Architecture Decision Records

| ADR | 决策 | 状态 |
|---|---|---|
| [0001](0001-modular-monolith-compose.md) | 模块化单体与 Docker Compose | Superseded by [0006](0006-local-json-sqlite.md) |
| [0002](0002-postgresql-pgvector.md) | PostgreSQL + pgvector 统一数据层 | Superseded by [0006](0006-local-json-sqlite.md) |
| [0003](0003-local-embedding-cloud-llm.md) | Embedding 与对话 LLM 配置分离 | Amended（当前为云端 OpenAI 兼容 Embedding） |
| [0004](0004-evidence-grounded-qa.md) | 结构化证据约束问答 | Accepted（立场/翻译未实现） |
| [0005](0005-postgres-job-truth.md) | PostgreSQL 保存任务真相 | Superseded by [0006](0006-local-json-sqlite.md) |
| [0006](0006-local-json-sqlite.md) | 单机 JSON 索引 + SQLite 会话 | Accepted |
| [0007](0007-task-dag-evaluation.md) | 独立 DAG 任务评测 | Accepted（离线扩展，目录由 0010 调整） |
| [0008](0008-optional-redis-infrastructure.md) | 可选 Redis 缓存与运行保障 | Accepted（索引所有权由 0010 调整） |
| [0009](0009-subagent-context-boundary.md) | 独立子上下文与结构化交接 | Accepted |
| [0010](0010-isolated-agents-modular-boundaries.md) | 生产父子 Agent 主线、轨迹独立与模块解耦 | Accepted，当前架构 |
| [0011](0011-team-workspace-access.md) | 可选团队身份、会话与独立工作空间授权 | Accepted，补充 0010 |
| [0012](0012-postgresql-business-store.md) | 可选 PostgreSQL 业务数据库、显式迁移与空间行级隔离 | Accepted，补充 0006/0011 |
| [0013](0013-task-recovery-quality-loop.md) | 任务幂等、安全恢复、离线还原演练与人工质量回归闭环 | Accepted，补充 0010/0012 |
| [0014](0014-deployment-governance.md) | 部署级指标告警、用户/空间成本预占与全局资源保护 | Accepted，补充 0010/0012/0013 |

ADR 一旦 Accepted 不直接改写决策结论；若决策变化，新增 ADR 并标记被替代关系。Superseded/Amended 文件保留原文，仅更新「状态」与替代指向，便于对照设计演进。

历史文件原结论保持；模块位置与实验适用版本以 [历史版本索引](../HISTORICAL_VERSIONS.md) 和 [迁移清单](../ARCHITECTURE_MIGRATION.md) 为准。
