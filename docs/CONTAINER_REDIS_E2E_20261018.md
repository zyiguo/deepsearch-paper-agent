# 容器与真实 Redis 验收（2026-10-18）

此前“容器运行与真实 Redis 尚未验收”的缺口已补齐。最终报告为 `output/container-redis-e2e-20261018-07/report.json`，状态 `passed`、错误为空；执行时间为北京时间 16:04:00–16:06:13。该目录保留全部编号命令日志、实际 Compose 配置和容器内复制出的合成 Agent 轨迹。

## 已观测结果

| 项目 | 实际结果 |
|---|---|
| 引擎与服务 | Docker Engine 29.5.2、Compose 5.1.4、Redis 7.4.11 |
| 镜像构建与启动 | 使用项目 Dockerfile；API UID 为 10001；单 API 进程；就绪检查正常 |
| HTTP 业务 | 演示文献入库 2 条片段；创建会话并保存带引用答案；请求编号回传 |
| 生产 Agent 图 | 在容器内运行真实 AnswerService/LangGraph 编排、检索、Redis 缓存和独立核验；模型与向量提供者为合成夹具；2 次父子交接、2 次独立核验、2 条成功轨迹 |
| 真实缓存 | 命中、批内去重、2 秒 TTL 到期、损坏向量重算、模型与凭据作用域隔离均通过；重复检索命中缓存 |
| Redis 原子限流 | 40 次并发调用恰好允许 5 次，脚本在真实 Redis 服务端执行 |
| HTTP 并发限流 | 20 次并发写请求：5 次成功、15 次 HTTP 429；含 Retry-After 与请求编号 |
| Redis 停机 | 就绪状态 degraded；缓存绕过；问答仍响应；本地限流生效；合成缓存探针耗时 0.401 秒 |
| Redis 恢复 | 就绪状态 ok；限流切回 Redis；写请求可用 |
| API 重启 | 原会话答案逐项相同；JSON 索引快照存在、2 条片段可读 |
| 后端完整回归 | 宿主机连接本轮真实 Redis：791 passed, 2 skipped in 52.25s；两项跳过仅因 Windows 符号链接权限 |
| 依赖审计 | 169 个模块、388 条边、0 循环、0 违规依赖 |
| 资源清理 | 测试项目的容器、1 个命名卷和专属应用镜像删除；再次按项目标签查询容器/卷、按镜像名查询均为空 |

容器图验证的 SQLite 原始轨迹为 `fixture-graph-traces.sqlite3`，分组导出位于同目录 `graph-traces-split/`，包含 success/failure/unknown 各自的 runs 与 handoffs 文件及 SHA256 清单。此次图夹具为 success 2、failure 0、unknown 0；人工标签为空，不代表人工确认正确。基础设施停机是故障注入成功，不把它伪造为 Agent 错误样本；模型拒绝、取消、中断等分类仍由完整回归与此前通信演示分别覆盖。

## 实服发现与修复

停止 Redis 后，容器服务名的操作系统 DNS 查询超出了 redis-py socket 超时。原缓存回退探针实际耗时约 **3.926 秒**，该次验收按失败保留。`RedisBackend` 现增加单次调用总等待上限 `2 × REDIS_TIMEOUT_SECONDS`；最多 16 个后台 I/O 线程，无排队积压，迟到连接在关闭后释放，失败冷却与缓存绕过/本地限流保留。新增长 DNS、冷却和迟到资源关闭回归。相同实服探针复测为 **0.401 秒**；这是本机该合成探针观测值，不是吞吐量或通用延迟承诺。

API 重启验收还发现 Docker 会重新分配临时发布端口。脚本现于重启后重新读取端口，再检查就绪状态与持久数据。此前失败日志显示 API 已正常启动，不能将访问旧端口失败归因为业务服务启动失败。

## 重复执行与证据身份

常规可用 Docker 环境：

```powershell
.venv/Scripts/python.exe -m deepsearch_experiments.infrastructure_e2e --output output/container-redis-e2e-next
```

本机实际命令如下，输出目录必须为新目录；代理端口需按自己的环境配置：

```powershell
.venv/Scripts/python.exe -m deepsearch_experiments.infrastructure_e2e `
  --docker-host 'npipe:////./pipe/dockerDesktopLinuxEngine' `
  --docker-config tmp/docker-config `
  --build-proxy 'http://host.docker.internal:7897' `
  --output output/container-redis-e2e-20261018-07

.venv/Scripts/python.exe -m deepsearch_tracing.communication export-split `
  --db output/container-redis-e2e-20261018-07/fixture-graph-traces.sqlite3 `
  --output output/container-redis-e2e-20261018-07/graph-traces-split
```

已执行的目录不能再次使用。运行器默认清理本轮资源；`--keep` 留存它们供诊断。清理前校验卷的 Compose 项目标签，拒绝删除其他项目卷；不会执行全库 flush 或挂载宿主用户资料。脚本位于独立离线包，不由生产入口加载。

| 身份 | 值 |
|---|---|
| 最终 Compose 项目 | `deepsearch-e2e-3909347de521` |
| 本次 API 镜像 ID（已清理） | `sha256:a7318562cd6b8aa540b076f2813d310b514cf8801350fefda237b9a85693dae6` |
| Dockerfile + compose.yaml SHA256 | `69d471d0df3508248aec6977e4c81653bb4ae7bf2d44c5849c1ee239184c8d62`（仅部署文件身份，并非全部源码哈希） |
| 官方 Python 基础镜像 manifest | `sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe`，`python:3.12-slim`，linux/amd64 |
| 官方 Redis 基础镜像 manifest | `sha256:ca0acbb137c1dc3339c8b147a58fd6f42775d4599327b50e7b116c23de501af2`，`redis:7.4-alpine`，linux/amd64 |

本机 Docker 引擎直连 Docker Hub 鉴权超时；使用已有宿主代理读取官方 registry 镜像，校验 manifest、配置、压缩层与展开层摘要，再导入引擎缓存。原始身份记录位于 `tmp/docker-official-images-20261018/python.json` 与 `redis.json`。构建代理仅作为依赖安装的构建参数，没有修改 Docker 全局代理。Docker Desktop 启动前的零字节运行时 socket 目录冲突通过保留备份目录后重建解决，未执行恢复出厂设置或删除镜像、已有用户卷；旧 socket 目录备份仍保留在原目录旁。

## 失败留存与边界

`output/container-redis-e2e-20261018-01` 至 `-06` 均保留：依次涉及引擎未启动、仓库鉴权网络、HTTP 头大小写断言、宿主临时目录权限、真实 DNS 总等待超时、重启临时端口变化。另保留 `output/container-redis-dns-diagnostic-20261018-01` 的定位日志。最终报告没有覆盖或改写这些失败。

本次使用真实 Docker、API、Redis、SQLite/JSON 卷与网络故障注入；HTTP 问答使用演示配置，生产图模型调用使用合成响应，未调用付费云模型。由此验证部署和资源交互，不证明真实模型质量、外部模型网络可用性、前端浏览器链路、多副本部署或灾难恢复。前端测试/构建沿用此前已通过记录，本轮未修改前端。
