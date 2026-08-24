# 模块规格 01：Infrastructure

- 状态：待审批
- 依赖：无
- 被依赖：所有业务模块
- 本规格审批前不得开始实现

## 1. 模块目标

提供本地单用户应用的可重复运行环境，包括容器编排、服务依赖、网络隔离、持久化卷、数据库迁移、健康检查、任务队列、配置、备份恢复和最小可观测性。

## 2. 交付范围

### 2.1 服务

| 服务 | 镜像/入口 | 职责 |
|---|---|---|
| gateway | Nginx + React build | 唯一宿主机入口、静态资源和反向代理 |
| api | backend image / Uvicorn | REST、SSE、上传和任务创建 |
| worker | backend image / Celery | 长任务和恢复 |
| embed-worker | backend image / Celery queue | 单进程加载 BGE-M3，GPU/CPU 回退 |
| migrate | backend image / Alembic | 一次性数据库升级 |
| postgres | PostgreSQL + pgvector | 事实数据和向量 |
| redis | Redis | 队列、短期锁和限流 |

首版不增加 Celery Beat 容器。停滞任务恢复在 Worker 启动时执行，并由应用内轻量周期任务补充；若后续需要精确定时任务，再单独审批调度模块。

### 2.2 网络

- `gateway` 只监听 `127.0.0.1:8080`。
- `gateway` 和 `api` 位于前端网络。
- `api`、`worker`、`embed-worker`、`postgres` 和 `redis` 位于内部网络。
- PostgreSQL 和 Redis 在默认运行配置中不映射宿主机端口。
- 开发 profile 可以显式开启调试端口。

### 2.3 持久化

| 卷 | 内容 | 真相级别 |
|---|---|---|
| postgres_data | 元数据、任务、文本、向量、证据 | 主事实来源 |
| paper_objects | 不可变 PDF 原文件 | 主事实来源 |
| artifacts | 可导出或可重建产物 | 次级 |
| backups | 数据库、文件和清单备份 | 恢复来源 |
| model_cache | 固定 revision 的 BGE-M3 文件 | 可重新获取 |
| redis_data | 队列 AOF | 可重建 |

Redis 不得成为唯一任务状态来源。Celery 消息只传递任务 ID，不传递 PDF 文本、API Key 或大对象。

## 3. 启动与停止

启动顺序：

```text
postgres/redis healthy
  -> migrate completed successfully
  -> api/worker/embed-worker healthy
  -> gateway ready
```

停止时先停止接收新请求，再停止 Worker，最后停止 Redis 和 PostgreSQL。强制中断后，运行中的任务依靠心跳超时和幂等检查恢复。

## 4. 配置

配置优先级：

```text
environment variables > .env > safe defaults
```

必须提供 `.env.example`，不得包含真实密钥。至少包含：

```text
APP_ENV
APP_HOST
APP_PORT
DATABASE_URL
REDIS_URL
PAPER_OBJECT_ROOT
ARTIFACT_ROOT
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
EMBEDDING_MODEL
EMBEDDING_REVISION
EMBEDDING_DEVICE
SESSION_TTL_HOURS
MAX_PDF_BYTES
MAX_PDF_PAGES
```

启动时必须校验配置并报告缺失项，但错误信息不得回显密钥值。

## 5. Embedding 运行策略

- 默认模型：`BAAI/bge-m3`。
- 模型 revision 必须固定，不能使用未记录的浮动版本。
- 输出维度：1024。
- 默认 GPU，CUDA 不可用时允许 CPU 回退并记录警告。
- `embed-worker` 并发数固定为 1，避免重复加载模型占用显存。
- 初始 batch size 可配置；显存不足时按 16、8、4、2 降级。
- 单批失败不得导致已完成批次丢失。
- 模型文件首次准备流程与业务运行分开，离线运行时不能隐式重新下载。

## 6. 数据库迁移

- 使用 Alembic。
- 只有 `migrate` 服务可以在标准启动流程中执行迁移。
- API 和 Worker 不得自行运行 `upgrade head`。
- CI 必须验证空数据库升级及上一版本数据库升级。
- 破坏性迁移必须有备份、兼容窗口和显式审批。
- pgvector 扩展由初始化迁移启用并验证。

## 7. 健康检查

### Liveness

`GET /health/live` 只判断进程是否响应，不依赖外部云端模型。

### Readiness

`GET /health/ready` 检查：

- 数据库连接
- 迁移 revision
- Redis 连接
- paper object 与 artifact 卷可访问
- 活动索引配置是否一致

云端 LLM 健康状态单独展示，不应因服务商短暂故障让本地文献管理完全不可用。

## 8. 日志和指标

- 标准输出使用 JSON 日志。
- 日志字段包含时间、级别、服务、request/job/paper/session ID、阶段、耗时和错误码。
- Authorization、API Key、Cookie 和原始完整 PDF 文本不得写入日志。
- 提供 `/metrics`，至少包含请求计数、任务状态、队列长度、阶段耗时、Embedding 批次、LLM 调用和磁盘余量。
- 首版不部署 ELK、Prometheus Server 或 Grafana；指标端点供后续接入。

## 9. 备份与恢复

备份产物：

```text
backup-<timestamp>/
  database.dump
  paper_objects/
  artifacts/
  manifest.json
```

清单至少保存 schema revision、论文数、页面数、Chunk 数、活动索引版本、对象数量及文件哈希。备份脚本默认暂停新的写任务，使用 PostgreSQL custom dump，并复制不可变对象。

恢复必须写入新数据库卷和新文件目录，验证以下项目后才能切换：

- schema revision
- 数据表计数
- PDF 对象哈希
- READY 论文是否拥有页面、Chunk 和活动向量
- 随机检索 smoke test

## 10. 安全要求

- 容器使用非 root 用户。
- 可行的容器根文件系统设为只读。
- 临时文件使用专门目录并限制容量。
- 只允许 PDF 文件头与白名单 MIME。
- 所有外部 HTTP 调用配置连接、读取和总超时。
- 默认服务只绑定回环地址。
- 依赖镜像和 Python/Node 依赖固定版本。

## 11. 开发与测试 profile

### Development

- 前后端热更新。
- 可选暴露数据库和 Redis 调试端口。
- 默认使用 Mock LLM。
- 使用小型本地 PDF fixture。

### Test

- 临时 PostgreSQL/Redis 和临时文件卷。
- 禁止真实云端模型调用。
- 测试完成销毁测试资源。

### Local production

- 固定镜像版本。
- 不暴露数据库和 Redis。
- 关闭热更新和调试接口。
- 使用持久化卷和备份目录。

## 12. 验收标准

- `docker compose config` 成功。
- 新环境可通过一个明确命令启动。
- PostgreSQL 和 Redis 未就绪时 API/Worker 不会错误启动。
- 数据库迁移失败时 Gateway 不进入 ready 状态。
- 只有 `127.0.0.1:8080` 对宿主机提供应用入口。
- 重启 API 和 Worker 后，持久化数据保持完整。
- Redis 清空后，可依据 PostgreSQL 找回未完成任务。
- 模拟 Worker 中断后，幂等任务能够恢复且不生成重复数据。
- BGE-M3 只被专用 Worker 加载一次，并能在 GPU 不可用时按配置回退。
- `.env.example` 不包含真实密钥，日志测试确认敏感字段被脱敏。
- 备份能恢复到新卷，并通过计数、哈希和 smoke test。
- Windows 11 + Docker Desktop 环境有经过验证的 PowerShell 启动、停止、备份和恢复命令。

## 13. 非目标

- 公网 TLS 和域名
- 用户认证与权限
- Kubernetes
- 多机 Worker
- 自动横向扩容
- 云端托管数据库
- 对象存储兼容服务
- 全量日志平台

## 14. 审批点

审批本规格即表示同意：

1. 使用 Docker Compose 管理本地运行环境。
2. 使用 PostgreSQL + pgvector 和 Redis。
3. API、通用 Worker、Embedding Worker 分进程运行。
4. Embedding Worker 默认单并发加载 BGE-M3。
5. 首版只提供本地回环入口，不实现身份认证。
6. 先完成可恢复、可备份的基础设施，再实现业务模块。
