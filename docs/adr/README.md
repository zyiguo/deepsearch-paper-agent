# Architecture Decision Records

| ADR | 决策 | 状态 |
|---|---|---|
| [0001](0001-modular-monolith-compose.md) | 模块化单体与 Docker Compose | Superseded by [0006](0006-local-json-sqlite.md) |
| [0002](0002-postgresql-pgvector.md) | PostgreSQL + pgvector 统一数据层 | Superseded by [0006](0006-local-json-sqlite.md) |
| [0003](0003-local-embedding-cloud-llm.md) | Embedding 与对话 LLM 配置分离 | Amended（当前为云端 OpenAI 兼容 Embedding） |
| [0004](0004-evidence-grounded-qa.md) | 结构化证据约束问答 | Accepted（立场/翻译未实现） |
| [0005](0005-postgres-job-truth.md) | PostgreSQL 保存任务真相 | Superseded by [0006](0006-local-json-sqlite.md) |
| [0006](0006-local-json-sqlite.md) | 单机 JSON 索引 + SQLite 会话 | Accepted |

ADR 一旦 Accepted 不直接改写决策结论；若决策变化，新增 ADR 并标记被替代关系。Superseded/Amended 文件保留原文，仅更新「状态」与替代指向，便于对照设计演进。
