# ADR-0012：可选 PostgreSQL 业务数据库

状态：Accepted（2026-10-18）。补充 ADR-0006、0008、0010 与 0011；单机 SQLite 模式继续有效，不恢复旧版 pgvector/Celery 方案。

## 背景

团队模式已有账号、空间权限和独立资源目录，但身份、会话与任务元数据仍分散在 SQLite 文件中。需要可独立部署的业务数据库、明确事务和升级机制，以及安全迁移现有记录的入口。内存索引和本地文件仍由单个 API 进程持有，迁移数据库不等于解决跨进程发布或任务调度。

## 决策

- 显式选择 `DATABASE_BACKEND=postgresql`。默认继续 SQLite；配置错误、数据库不可用或版本不匹配时不回退、不双写。
- PostgreSQL 保存账号、登录会话摘要、空间/成员、安全事件、研究会话、答案、会话记忆和研究任务元数据。JSON 值使用 JSONB；已有 UUID、轮次 ID、证据 ID、记忆版本与 HTTP 对象语义保留。
- SQL 位于 `adapters.persistence.postgres_*`；原应用服务继续使用存储操作。`MemoryManager` 不再执行 SQLite SQL，使用存储公开的初始化与乐观版本更新方法。公共答案序列化由两种存储复用。
- 一个 API 进程共用一个有界 psycopg 连接池（默认最多 8 连接、64 个等待请求）；团队空间不另建连接池。事务结束归还连接，任务线程关闭后才释放池。连接、获取连接、SQL 和锁等待均有期限；写入不自动重试。
- 团队业务表使用 `(workspace_id, id)` 复合键/外键，查询显式限制空间，同时启用并强制 RLS。每次事务用事务局部配置设置空间，未设置空间时无法读取业务行。应用拒绝 PostgreSQL superuser / BYPASSRLS 角色。身份表是控制入口，由现有应用认证授权访问，不使用业务空间 RLS。
- 同一研究运行保存答案时，用空间/运行粒度的事务锁及唯一约束避免重复轮次；会话行锁保证同会话提交顺序；记忆用 revision CAS，恢复任务用条件更新。数据库仅保证这些提交的幂等，不保证外部模型请求只收费一次。
- 使用显式 `database_cli upgrade` 和版本/DDL 校验和记录，迁移在事务与 advisory lock 下进行。API 启动仅检查版本，不执行表结构升级。

## 迁移与部署

迁移工具默认只读预检，显式 `--apply` 才写目标；拒绝覆盖已有目标数据，报告采用独占创建。源 SQLite 只读打开、检查完整性及 SHA256，要求源端停写且无待回放 WAL/journal。导入事务锁住目标业务写入，避免空库检查后被普通写请求插入；保留旧 ID，并向前调整新轮次序列。

账号密码摘要可迁移；已有登录 Token 不迁移，切换后重新登录。团队身份先导入，再逐个指定空间导入会话/记忆/运行记录。文件、索引、Wiki、工作记忆文件和检查点仍在原空间目录，不能只导入元数据就声明完整资料迁移。导入失败或连接断开时需检查报告与目标状态，不自动重试未知提交。

独立 `compose.database.yaml` 覆盖文件启用 PostgreSQL 17，不影响原 Compose 配置解析。新的 PG 卷初始化非 superuser 应用角色；密码从私有环境文件注入。PostgreSQL 故障时就绪检查和业务返回 503，存活检查仍 200；恢复后继续访问同一数据库。

## 取舍与未实现项

选择 psycopg 显式仓储，避免在少量存储操作上增加 ORM 映射及全局会话。代价是维护两套数据库方言的存储适配器，必须运行真实 PostgreSQL 契约回归。SQLite 本地模式不依赖 PostgreSQL SDK 的加载；独立轨迹包保持仅标准库。

检索 JSON/向量、文献/附件、Wiki Markdown、工作记忆文件、LangGraph SQLite 检查点与独立 SQLite 轨迹不迁入 PostgreSQL。本轮不增加 pgvector、分布式队列、多 API 副本、费用账本或自动备份系统。备份恢复需同时覆盖 PostgreSQL 和文件卷；切换前保留 SQLite 可供回退，但不能无损覆盖切换后只存在 PostgreSQL 的新增记录。

参考：[psycopg 连接池与事务](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)、[PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)。
