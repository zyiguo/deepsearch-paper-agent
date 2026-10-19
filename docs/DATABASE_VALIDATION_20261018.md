# PostgreSQL 业务数据库验收（2026-10-18）

已完成实际 PostgreSQL、Redis 与 API 容器联合验收。最终报告为 `output/postgres-e2e-20261018-02/report.json`，状态 `passed`、错误为空；此前 `-01` 报告亦保留。本轮没有创建真实 `.env.database`、迁移用户原始数据或调用付费云模型。

## 结果

| 验证项 | 实际结果与边界 |
|---|---|
| 完整后端回归 | **821 passed, 2 skipped in 88.84s**；宿主机同时连接本轮真实 PostgreSQL 和 Redis。跳过仅为 Windows 符号链接权限两项 |
| 数据库专项 | 18 项全部执行：8 项配置校验/脱敏，10 项真实 PostgreSQL 测试 |
| SQLite 兼容阶段 | 原会话、记忆、团队权限和架构专项 44 passed；旧 OpenAPI、数据格式与检查点兼容仍由完整回归覆盖 |
| 依赖审计 | **187 个模块、433 条边、0 循环、0 违规边**；核心/独立轨迹继续保持标准库边界 |
| 服务与依赖 | Docker Engine 29.5.2；真实 PostgreSQL **17.11**，psycopg 3.3.6 / psycopg-pool 3.3.3；原 Redis 服务也实际运行 |
| 构建与迁移 | 项目 Dockerfile 构建成功；显式 schema upgrade 通过；非 superuser 数据库角色可运行；API UID 10001 |
| API 业务 | PG 账号/成员、会话/答案、记忆实际读写；跨空间拒绝、只读可读/不可写，空间共用同一应用连接池 |
| API 重启 | 原登录可用，会话答案与记忆保留；索引和检查点仍由原文件卷持有 |
| PostgreSQL 停机 | 就绪检查 503、业务 503、存活检查 200；不回写 SQLite |
| 数据库恢复 | 同一 API 进程通过连接池继续读取原答案，未重建或切换数据来源 |
| 文件边界 | 容器内没有生成身份、会话或运行的影子 SQLite 文件；允许的本地索引/文件/检查点存储保持独立 |
| 清理 | 两轮分别清理自己项目的容器、应用卷、PostgreSQL 卷和应用镜像；报告与日志保留 |

## 数据完整性与并发

- **RLS 与池复用**：检查业务表 RLS/强制标志；故意省略 WHERE 时只读到当前空间，未设置空间时为空；错误写入另一空间失败；失败事务后复用连接不继承空间上下文。复合外键拒绝跨空间会话引用。
- **答案幂等**：20 次并发保存相同运行只产生一个轮次 ID；同会话行锁、运行级事务锁和唯一约束实际在 PostgreSQL 执行。
- **记忆与恢复**：12 次相同 revision 的记忆更新仅 1 次成功；20 次恢复认领仅 1 次成功。打开另一个空间不会中断当前空间任务；重建本空间运行存储后按原规则标记中断。
- **身份**：20 次并发账号登录计数恰好允许 5 次；密码重置撤销会话，旧摘要不能签发新 Token；停用后恢复账号不会恢复旧登录。
- **SQLite 导入**：只读预检不写目标；实际导入保留会话/轮次、答案、记忆和运行关联；新轮次 ID 大于导入记录。身份导入保留密码摘要与成员关系，不迁移登录 Token。源文件 SHA256 不变，非空目标与已有人工报告均拒绝覆盖。
- **中途失败回滚**：真实导入已经 INSERT 空间和会话后，在轮次插入前注入异常，确认空间和会话插入均回滚。另验证损坏 JSON 拒绝导入。源/目标仍要求停写；不声称支持在线无停机迁移。

## 证据身份

最终 API 镜像 ID：`sha256:39e676123059f84d941beb4c739e6547b09dad9cf96f37050fb7ab262a4cd574`（测试结束已清理）。版本 1 表结构指纹为 `c46cc32bf067aae13224311767a6be01ad60676ecd3f2ca95a7c233d7e6c764f`，通过 schema_migrations 核对。

官方 `postgres:17-alpine` 的 linux/amd64 manifest：`sha256:aa90e97ee862e558111d34cfb8b2c4bec768c2b039fb791341686928560263b3`；配置与归档摘要见 `tmp/docker-official-images-20261018/postgres.json`。本机通过已有宿主代理读取官方 registry 并校验各层摘要后载入缓存，未修改 Docker 全局代理。镜像依赖安装使用构建代理参数，不写入运行时模型配置。

每轮容器配置使用新项目、临时回环端口与新命名卷；合成数据库密码仅在本地忽略的验收配置中存在，不出现在报告中，不用于真实部署。PostgreSQL 测试逐项创建独立 `test_<uuid>` schema，并在结束时只删除自己的 schema。没有清库或挂载用户数据目录。

## 重复执行

```powershell
.venv/Scripts/python.exe -m deepsearch_experiments.database_e2e `
  --docker-host 'npipe:////./pipe/dockerDesktopLinuxEngine' `
  --docker-config tmp/docker-config `
  --build-proxy 'http://host.docker.internal:7897' `
  --output output/postgres-e2e-next

.venv/Scripts/python.exe -m deepsearch_experiments.architecture
.venv/Scripts/python.exe -m compileall -q src
git diff --check
```

输出目录必须不存在。编号命令日志包括镜像构建、数据库初始化/升级、完整 pytest、API/数据库重启、服务日志和清理。前端代码本轮未改动，未将此前的前端构建记录冒充为本轮新结果。

尚未验收公网数据库 TLS、灾难恢复、历史真实资料导入后云模型续跑、多 API 副本、分布式任务或计费一致性。当前只保证单 API 进程内的数据库接入与上述持久化行为；迁移和切换说明见 [DATABASE.md](DATABASE.md)。
