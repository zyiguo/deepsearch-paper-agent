# PostgreSQL 业务数据库

项目默认仍使用 SQLite。配置 PostgreSQL 后，账号/权限、会话/答案、会话记忆和研究任务状态实际读写 PostgreSQL，未连接成功不会回写 SQLite。原文、附件、检索索引、Wiki、工作记忆文件、Agent 检查点和独立轨迹继续保存在文件卷内。

## 数据归属

| 数据 | SQLite 模式 | PostgreSQL 模式 |
|---|---|---|
| 用户、登录摘要、空间成员、安全事件 | 身份 SQLite | PG 身份表 |
| 会话、答案、会话记忆 | 各空间会话 SQLite | PG 空间业务表、JSONB |
| 研究运行状态/请求、恢复认领、结果关联 | 各空间运行 SQLite | PG 空间运行表 |
| 文献/附件、解析与视觉缓存 | 文件 | 文件 |
| 检索索引和向量 | 内存 + JSON | 保持原方式 |
| Wiki / 工作记忆文件 | Markdown/JSON | 保持原方式 |
| Agent 检查点 / 独立轨迹 | SQLite | 保持独立 SQLite |

各空间在 PostgreSQL 共享表，通过空间键、复合外键、显式查询条件和强制行级安全隔离；本地单用户部署使用 `local` 空间。同一数据库 schema 应对应同一部署及文件卷，不同部署应配置不同 `DATABASE_SCHEMA`。这不是多 API 副本支持。

## 本地后端连接 PostgreSQL

安装额外依赖，并在尚不存在 `.env.database` 时复制示例，再编辑连接信息：

```powershell
.venv/Scripts/python.exe -m pip install -e ".[database]"
Copy-Item .env.database.example .env.database
```

已有配置请直接编辑。使用非 superuser、没有 BYPASSRLS 的应用角色；初始化连接的角色需要在目标数据库创建自己 schema 的权限。示例密码只是占位符，须替换，URL 中的密码须进行 URL 编码。对外连接的 TLS 及证书校验按数据库部署配置。

| 配置 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_BACKEND` | `sqlite` | `sqlite` 或 `postgresql` |
| `DATABASE_URL` | 空 | `postgresql://用户:密码@主机:端口/数据库`，仅允许可选 `sslmode` 查询参数 |
| `DATABASE_SCHEMA` | `deepsearch` | 应用 schema，限定小写短标识符 |
| `DATABASE_POOL_SIZE` | `8` | 单 API 进程共享连接池，1–32 |
| `DATABASE_TIMEOUT_SECONDS` | `3` | 连接、获取连接及锁等待期限，1–30 秒 |
| `DATABASE_STATEMENT_TIMEOUT_MS` | `5000` | SQL 执行期限，100–60000 毫秒 |

文件值覆盖同名环境变量。启动前显式升级并检查，不会在 API 启动时建表：

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.application.database_cli upgrade
.venv/Scripts/python.exe -m deepsearch_agent.application.database_cli status
.venv/Scripts/python.exe -m uvicorn deepsearch_agent.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers --log-config src/deepsearch_agent/logging.json
```

团队模式继续按照 [团队操作说明](TEAM_ACCESS.md) 配置 `.env.access`。全新数据库在升级后，用原 `access_cli create-user` 创建首个管理员；CLI 会根据数据库配置选择正确的身份存储。迁移已有团队时先导入身份，不要先创建一个同名管理员占用空目标库。

## Compose 部署

本轮镜像包含 `[infrastructure,database]` 依赖。原 `compose.yaml` 不要求 PostgreSQL 密码；仅显式组合覆盖文件时启用数据库：

```powershell
Copy-Item .env.database.compose.example .env.database.compose
```

先编辑私有文件，设置独立的数据库管理员密码、应用密码，以及使用同一应用密码且已 URL 编码的 `DATABASE_URL`。容器连接使用主机名 `postgres`；宿主机程序连接使用 `127.0.0.1`，不要把宿主机 `.env.database` 挂入容器覆盖其地址。

```powershell
$databaseCompose = @('--env-file', '.env.database.compose', '-f', 'compose.yaml', '-f', 'compose.database.yaml', '--profile', 'app')
docker compose @databaseCompose build api
docker compose @databaseCompose up -d postgres redis
docker compose @databaseCompose run --rm api python -m deepsearch_agent.application.database_cli upgrade
docker compose @databaseCompose up -d api
```

数据库仅发布到宿主机回环地址，数据保存在独立 `postgres-data` 命名卷；新卷初始化时通过 `deploy/postgres/001-role.sql` 建立非 superuser 应用角色。初始化脚本只对新卷执行，修改环境密码不会自动修改旧卷中已有角色密码。正常停止使用 `down`，保留命名卷。首次容器管理员也必须在这个容器/数据库配置下创建。

API 本地文件卷仍不可省略：索引、原件、Wiki、工作记忆文件、检查点和轨迹都在其中。团队模式开启时应通过私有覆盖配置继续注入 `ACCESS_MODE`、Origin 和 Cookie 配置。

## 迁移已有 SQLite

迁移要求源端和目标 API 停止写入。先保留原 SQLite 和文件卷备份，处理 SQLite 的 WAL/journal，再使用已有文件或其一致性副本。工具只读源文件，不批量重写检查点，不自动将旧单用户资料分配给团队。

本地单用户默认源文件位于 `data/indexes/`。先做只读预检，再显式应用；每次使用新的报告文件：

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.application.database_cli import-workspace `
  --source data/indexes/conversations.sqlite3 --runs data/indexes/research_runs.sqlite3 `
  --workspace-id local --output output/db-import/local-preview.json

.venv/Scripts/python.exe -m deepsearch_agent.application.database_cli import-workspace `
  --source data/indexes/conversations.sqlite3 --runs data/indexes/research_runs.sqlite3 `
  --workspace-id local --apply --output output/db-import/local-apply.json
```

没有旧运行库时省略 `--runs`；不要把另一个空间的运行库配给当前会话库。目标空间有会话、轮次、记忆或运行记录时拒绝覆盖。表结构升级与数据导入是两个独立操作。

已有团队先导入身份，再逐个明确指定已有空间 ID：

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.application.database_cli import-identity `
  --source data/team/identity.sqlite3 --output output/db-import/identity-preview.json
.venv/Scripts/python.exe -m deepsearch_agent.application.database_cli import-identity `
  --source data/team/identity.sqlite3 --apply --output output/db-import/identity-apply.json

# 将下列“空间ID”替换为已有的 32 位工作空间 ID；自定义根目录使用实际路径。
.venv/Scripts/python.exe -m deepsearch_agent.application.database_cli import-workspace `
  --source 'data/team/workspaces/空间ID/conversations.sqlite3' `
  --runs 'data/team/workspaces/空间ID/research_runs.sqlite3' `
  --workspace-id 空间ID --output output/db-import/workspace-preview.json
```

检查预检报告后，再增加 `--apply` 并选择新报告文件。身份导入保留用户、密码摘要、成员关系和安全事件，**不迁移登录会话**；切换后重新登录。会话导入保留 ID、答案引用、附件引用、记忆 revision 与运行关联，并将新轮次序列向前调整。

文件、索引、Wiki 和检查点仍需保留在同一部署的原空间目录。把数据复制到另一台机器时，还需同步完整文件卷；仅 PG 导入成功不能证明原文与任务检查点完整迁移。预检不会执行模型或重建向量。

每个导入在单个数据库事务中完成，导入期间锁住对应业务表的普通写入，防止空库检查与提交之间混入新数据。报告含数量与源文件哈希，不含密码、会话 Token 或问题原文；报告和人工文件均不覆盖。若发生连接中断或报告写盘失败，命令报告未知/已确认提交状态，先检查目标记录，禁止直接假定失败后安全重试。

切换前可以保留 SQLite 作为回退来源；切换后的新记录只在 PostgreSQL 中，直接改回 SQLite 无法保留这些新写入。本轮不提供反向同步或双写。

## 故障与验证

PostgreSQL 是业务数据来源：不可用时业务请求及 `/api/health/ready` 返回 503；`/api/health/live` 仍表示 API 进程存活。Redis 仍是可降级缓存，二者故障语义不同。数据库恢复后连接池重新建立可用连接，继续访问原数据；不会创建影子 SQLite 业务文件。

```powershell
# 使用一个明确的测试数据库地址；测试只创建/删除自身随机 test_* schema。
$env:TEST_DATABASE_URL = 'postgresql://测试应用角色:已编码密码@127.0.0.1:5432/测试数据库'
.venv/Scripts/python.exe -m pytest tests/test_database.py -q
Remove-Item Env:TEST_DATABASE_URL

# 容器验收使用新项目、新卷、临时端口、合成账号，结束清理自身资源。
.venv/Scripts/python.exe -m deepsearch_experiments.database_e2e --output output/postgres-e2e-next
```

缺少 `TEST_DATABASE_URL` 时，真实 PG 用例明确跳过，不将配置测试算作实服验收。本机实际结果见 [数据库验收记录](DATABASE_VALIDATION_20261018.md)，设计取舍见 [ADR-0012](adr/0012-postgresql-business-store.md)。数据库加入后仍限制单 API 进程，未引入任务队列或多副本。
