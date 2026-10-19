# Redis 与业务运行基础设施

本次增加的能力已接入真实业务路径：PDF 导入与检索查询共用的文本 Embedding 客户端可缓存向量；所有 `/api/` 下 POST/PUT/PATCH/DELETE 请求可按连接来源限流；请求携带可追踪编号，并提供独立运行检查。原有 SQLite 会话、原文文件和 JSON 索引仍是持久数据来源。

上述 SQLite 描述对应默认本地模式。可选 [PostgreSQL 业务数据库](DATABASE.md) 接管身份、会话/记忆及运行元数据；Redis 仍只负责缓存与限流，数据库故障返回 503，不与 Redis 一样降级为另一份持久数据。

## 本地运行

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[infrastructure]"
Copy-Item .env.infrastructure.example .env.infrastructure
docker compose up -d redis
.\.venv\Scripts\python.exe -m uvicorn deepsearch_agent.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers --log-config src/deepsearch_agent/logging.json
```

已有 `.env.infrastructure` 时，编辑现有文件，避免重复复制覆盖配置。前端按 README 中原有方式启动。

不创建 `.env.infrastructure` 且不设置相关环境变量时，Redis 和限流默认关闭。只需本地限流时，将 `REDIS_URL` 留空、`API_RATE_LIMIT` 设为正数。配置文件同名值覆盖环境变量，与本项目既有配置习惯一致。

| 配置 | 默认值 | 含义 |
|---|---|---|
| `REDIS_URL` | 空 | 关闭 Redis；支持 `redis://`、TLS 的 `rediss://`，禁止 URL 查询参数覆盖连接保护设置 |
| `INFRA_NAMESPACE` | `deepsearch` | 部署级键空间，不同资料所有者应使用不同空间 |
| `EMBEDDING_CACHE_TTL_SECONDS` | `86400` | 向量缓存有效期 |
| `API_RATE_LIMIT` | `0` | 每个来源每窗口允许的修改请求数，0 关闭 |
| `API_RATE_WINDOW_SECONDS` | `60` | 从该来源首个请求开始的固定窗口长度 |
| `REDIS_TIMEOUT_SECONDS` | `0.3` | 连接与读写分别使用的超时，上限 5 秒；单次调用总等待上限为该值的两倍，包含 DNS 等待 |
| `REDIS_COOLDOWN_SECONDS` | `5` | Redis 失败后暂时绕过连接的时间 |

## 业务行为

缓存键包含版本、模型/地址/维度身份与凭据作用域、文本的组合哈希，不直接保存原文或密钥；值为校验过的归一化向量。同批重复文本只计算一次，按输入顺序返回结果。更换模型或凭据会使用不同键。损坏或维度不匹配的缓存会重新计算，模型调用失败不写缓存。缓存失效不等于资料删除；删除资料后历史向量可能保留到 TTL 到期。Redis 应作为受控内部服务管理，向量也属于用户数据。

Redis 使用连接池、短超时、不自动重试和失败冷却，避免故障时逐条拖慢请求。缓存读写失败继续调用原 Embedding 服务；模型本身失败仍按原有逻辑报错。故障冷却后下一次业务请求或就绪检查会尝试恢复。此版本不提供跨进程缓存击穿锁，同时到达的相同请求可能重复调用模型。

实际停机验收发现，操作系统 DNS 查询可能超过 redis-py 的 socket 超时。因此适配器额外限制单次调用的总等待时间，最多使用 16 个并发后台 I/O 线程，与连接池容量一致；达到容量直接降级，不堆积等待队列。超时不代表远端命令一定未执行，限流仍按下面的流量保护语义处理。关闭资源后拒绝新调用，迟到连接结束时再次关闭客户端；后台 DNS 不阻止进程退出。

限流采用 Redis 原子脚本，一次完成计数与设置到期，超过限制返回 HTTP 429 和 `Retry-After`。Redis 不可用时启用线程安全、最多 10000 个活动来源的本地计数，达到容量拒绝新来源。故障切换或进程重启会切换/重置计数，因此这属于流量保护，不是精确计费配额。只信任 socket peer，不读取 `X-Forwarded-For`。在反向代理后可能共享一个来源；当前容器关闭代理头信任，接入网关后应由网关承担按真实用户限流。

HTTP 响应（包括 429、校验错误和未处理异常的 500）带 `X-Request-ID`；接受 1–64 位字母、数字、下划线与连字符，否则重新生成编号。500 响应返回通用错误与请求编号，不把异常详情发给用户。访问日志使用路由模板，记录编号、方法、状态和耗时，不记录问题、请求体、会话 ID 或查询参数。SSE 逐块透传，访问耗时包含流输出时长；不会先收集整个答案再返回。上面的日志配置关闭 Uvicorn 原始访问日志，避免其额外记录查询参数。第三方库日志及异常堆栈不属于这一访问日志脱敏范围。

## 运行检查

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health/live
Invoke-RestMethod http://127.0.0.1:8000/api/health/ready
```

- 原 `/api/health` 保持原响应兼容。
- `/api/health/live`：HTTP 200，表示 API 进程能响应。
- `/api/health/ready`：返回 Redis、索引写保护和限流后端状态。Redis 故障返回 `degraded`、HTTP 200，服务仍可处理请求；索引恢复失败导致写保护时返回 `not_ready`、HTTP 503。

就绪检查不调用付费模型，也不证明云端模型可用或检索效果正确。

## 容器运行

```powershell
docker compose --profile app up -d --build
docker compose --profile app ps
docker compose --profile app logs -f api
```

API 默认演示模式、仅绑定宿主机回环地址；Redis 只发布到本机。容器镜像使用非 root 用户，应用数据写入 `application-data` 命名卷；不挂载或迁移现有本地 `data/`。构建上下文采用允许列表，不包含 `.env`、用户资料、Git 或本地运行输出。

Redis 限制 256 MiB、关闭持久化、使用 `noeviction`；避免内存淘汰活动限流计数器。内存满时写入失败会触发上述降级，向量缓存允许丢失。停止容器用 `docker compose --profile app down`，不要使用 `-v`，除非你明确需要删除应用命名卷。

启用真实模型时，可以在本机新建 `compose.override.yaml`：

```yaml
services:
  api:
    env_file:
      - .env
      - .env.embedding
```

仅列出已配置的文件；视觉模型等可以按需要添加对应文件。不要将包含密钥的覆盖配置提交到仓库。生产环境应通过部署平台注入配置/密钥。不要把开发机器的 `.env.infrastructure` 挂到容器内，否则其中的 `127.0.0.1` 会覆盖 Compose 的 Redis 服务地址。

必须维持 **1 个 API Worker / 1 个 API 副本**：当前索引仍驻留内存并写 JSON，多个进程无法自动同步。Redis 加入后并不改变这一约束。前端不包含在本次镜像中，继续本地运行，或后续部署到受控网关。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_infrastructure.py tests/test_embedding_runtime.py tests/test_api.py -q
$env:TEST_REDIS_URL = 'redis://127.0.0.1:6379/15'
.\.venv\Scripts\python.exe -m pytest tests/test_infrastructure.py -q
Remove-Item Env:TEST_REDIS_URL
```

真实 Redis 集成测试必须显式指定地址；使用随机测试命名空间，结束时仅清理该空间。未指定时跳过，不把模拟测试算作真实服务验收。

完整容器验收使用独立 Compose 项目、临时回环端口与新命名卷，执行镜像构建、HTTP 业务、父子 Agent 图、真实缓存与原子限流、Redis 停机/恢复、API 重启持久化，以及启用真实 Redis 的完整后端测试：

```powershell
.venv/Scripts/python.exe -m deepsearch_experiments.infrastructure_e2e --output output/container-redis-e2e-next
```

输出目录必须不存在；失败报告、命令日志和合成轨迹均保留。默认清理该次测试的容器、镜像和命名卷；删除卷前检查 Compose 项目标签。`--keep` 可保留该次测试资源用于诊断。`--docker-host`、`--docker-config` 可选择已有 Docker 引擎及客户端配置；构建需要代理时，可传 `--build-proxy http://host.docker.internal:端口`，仅用于镜像依赖安装，不修改 Docker Desktop 全局设置或运行时配置。未安装基础镜像时仍需 Docker 引擎能够访问镜像仓库，或预先载入已校验的官方镜像。

2026-10-18 后续实服验收已完成：Docker Engine 29.5.2、Compose 5.1.4、Redis 7.4.11；实际构建并启动非 root API 容器。真实 Redis 缓存命中、去重、TTL 到期、损坏值重算、模型/凭据隔离、Lua 并发限流，以及停机降级/恢复均通过。API 重启后原会话答案和两条索引片段保持可读。启用 `TEST_REDIS_URL` 的完整后端回归为 **791 passed, 2 skipped**，两项跳过仅涉及 Windows 符号链接权限。真实停机暴露的 DNS 超时问题已修复，同一合成缓存回退探针耗时由约 3.926 秒降至 0.401 秒。报告、命令、镜像身份与解释边界见 [容器与 Redis 验收记录](CONTAINER_REDIS_E2E_20261018.md)。

## 下一阶段

已补充可选的 [用户、工作空间与权限](TEAM_ACCESS.md)，通过独立空间运行环境隔离业务资源，仍不是多副本 SaaS。后续业务化顺序：补持久任务调度、幂等与费用额度，完善备份恢复和运维监控；有容量需求时再设计共享元数据、索引发布与独立 Worker。直接将当前 `DocumentLibrary` 放入 Celery 会失去 API 的内存索引更新，不能这样接入。

客户端机制参考 Redis 官方文档：[连接与连接池](https://redis.io/docs/latest/develop/clients/redis-py/connect/)、[超时与生产使用](https://redis.io/docs/latest/develop/clients/redis-py/produsage/)。本项目使用独立的短超时与无自动重试策略，实际故障行为由上面的测试验证。
