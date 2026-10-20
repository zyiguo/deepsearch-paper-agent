# Redis 缓存保护验证（2026-10-20）

## 版本与结论

在 `f4ece494975e6dbbbd59cc62945b329989e15f21` 基础上实现客户端实例级跨请求 singleflight 与向下 TTL 抖动。原 Redis key、模型/凭据身份、持久工具状态和费用状态保持各自语义。使用方法见[缓存保护指南](REDIS_CACHE_PROTECTION.md)，实施顺序见[计划](plans/2026-10-20-redis-cache-protection.md)。

| 检查 | 本轮实际结果 |
|---|---|
| 连接本轮真实 Redis 的完整 pytest | **1144 passed、13 skipped，136.31 秒** |
| 架构审计 | **214 模块、545 依赖，0 循环、0 违规** |
| 真实 Redis 专用故障 runner | **16 项通过、0 失败、0 未验证**，含源码冻结与清理检查 |
| 原槽位未知恢复 | 16 个 paid 槽位全部阻断重发，模拟供应商持久效果仍为 1 |
| 小规模真实模型配对 | **未完成**：baseline 4 次 HTTP 派发尝试均为 ModelServiceError，protected 组未启动 |

完整测试在设置 `TEST_REDIS_URL` 后运行，既有真实 Redis 集成测试执行而非跳过；其余 13 项跳过没有计入通过。前端代码未变，本轮没有重跑前端构建。原 Badcase、容量实验和旧验证 JSON 均未改写。

机器证据：[真实 Redis 报告](../eval/results/cache-protection-real-20261020.json)、[真实模型失败报告](../eval/results/cache-real-model-20261020.json)、[本轮验证清单](../eval/results/cache-protection-validation-20261020.json)。清单保留验收时未提交状态；后续 Git 换行转换由[发布身份清单](../eval/results/cache-protection-publication-20261020.json)连接到仓库 blob，不修改原始验收摘要。

## 实现与并发语义

- 原子登记整批 miss，容量不足时整批拒绝；owner 二次查询 Redis，仍未命中的 owned 文本保持批量调用。
- 先计算 owned，再等待 followed，避免反序/重叠批次互等。表锁不覆盖网络调用或 Future 通知。
- follower 总等待有界，至多每 0.1 秒检查取消。超时与取消不取消 shared Future、不移除 owner、不接管或重发。
- leader 在原 caller 线程和计费上下文执行；followers 不新增供应商费用预占。结果以 tuple 发布、各请求以独立 list 返回。
- leader 保留自身异常；followers 接收固定安全 RuntimeError，不继承另一操作的 ID 或 ToolNotExecuted 分类。
- 费用和工具结果独立确认。新增持久回归覆盖“B 已完成 owned 付费，随后 A 的 shared 拒绝”：B 的费用保持 settled，工具结果保持 unknown；原槽位恢复不重发。

默认 singleflight 开启、等待 65 秒、容量 4096 键；TTL 基础值 86400、抖动比率 0.1，写入 TTL 在 77760–86400 秒之间。抖动只缩短 freshness 上限，比例 0 可做固定 TTL 对照。

## 真实 Redis：并发和过期

服务为 **WSL Ubuntu-24.04 中的独立 Redis 7.4.11 进程**，使用随机回环端口与专用配置，64 MiB、noeviction、关闭 RDB/AOF。用于归因的 provider 是有持久计数及事件同步的合成实现，不是云模型。

| 场景 | 观测 |
|---|---|
| 16 并发，同一冷 key，singleflight 关闭 | 16 次 provider 派发，完成后可命中缓存 |
| 同条件，singleflight 开启 | 1 次派发、其余共享结果，最终 inflight=0 |
| 真实 Redis TTL 到期后 16 并发 | 初建 1 次，到期后重建 1 次 |
| 4 个重叠批次、4 个不同文本 | 每文本计算 1 次；本次合并为 2 个 provider 批次，无死锁、顺序正确 |
| follower 超时或取消 | leader 仍完成，派发仍为 1，后续缓存命中 |
| 损坏缓存 | 2 条坏值一次批量重算，并修复真实 Redis 值 |

off/on 场景在隔离缓存层使用计数 provider，未声称走完整研究图或代表生产 provider 并发。16→1 是该受控热点重叠窗口的派发下降，不是端到端 Token、费用或 QPS 的普遍收益。

TTL 抖动在真实服务各写入 128 个键，读取 PTTL：

| base / ratio | 配置 TTL 范围 | 实际 PTTL 最小–最大 | 观测到的整数秒桶 |
|---|---|---|---:|
| 60 秒 / 0.2 | 48–60 秒 | 47891–59990 ms | 13 |
| 120 秒 / 0.2 | 96–120 秒 | 95926–119985 ms | 25 |

PTTL 记录时键已消耗少量生存时间，因此最小值可以略低于配置秒数。抖动分散了本次批量写入的到期时间；它不消除 Redis 整体故障，也不合并大量不同的新文本。

## 真实故障与 unknown

控制器首次比对 Redis INFO run_id/PID/端口，且每次信号操作前核对 `/proc/PID/exe`、专用配置与 PID。只使用该进程的 pidfd 发送暂停、继续和终止；不执行全机 kill、发行版关闭或数据库 FLUSH。

| 场景 | 断言与结果 |
|---|---|
| SIGSTOP 暂停 Redis | 短超时后缓存绕过；16 个重叠请求只派发 1 次；治理容量/费用拒绝仍发生在额外调用前 |
| SIGTERM 停机再启动 | 相同 backend 能恢复写入并命中；费用和资源池不因缓存故障失效 |
| 临时 ACL 用户禁止 SET | 真实服务器返回 NoPermissionError；合法向量仍返回，未伪造缓存写入成功 |
| provider 已计次后抛异常 | 16 个各自的 paid 工具槽位均为 unknown，费用预占只有 1 条、冻结 7 个合成成本点，actual=null |
| 新进程打开原账本并恢复 | 16 个原 run/slot 全部阻断，持久模拟效果保持 1；没有自动再次调用 |

每轮只清理随机命名空间。最终回归后，专用 Redis 进程已核验身份并停止；没有删除报告、SQLite 证据或源文件。

## 真实模型探针：失败也保留证据

已获用户授权，使用现有配置的 **BAAI/bge-m3、1024 维**，仅发送固定合成文本。原计划两组各 4 并发，baseline 最多 4 次 HTTP 尝试、protected 最多 1 次，总硬上限 5；失败后不自动重试或启动下一组。

实际在 baseline 发起 4 次 HTTP 派发尝试，4 个生产客户端调用均抛 ModelServiceError，成功批次为 0，protected 未执行。报告只记录安全异常类别，缺少足够收据区分供应商拒绝与传输原因，不能从 request hook 推断实际服务端执行或计费。

费用控制库保留 **4 条 unknown**，合计冻结 **332 个内部成本点**，报告 Token 与实际费用均为 null；这不是 332 元或美元，也不是确认已收费。未换文本、换操作或重发这些请求。原始控制库保留在私有输出 `private/cost.sqlite3`，公开结果只包含脱敏收据。

因此本轮不声称真实模型配对通过，不给真实 Token 降幅或回答质量结论。真实 Redis 的并发/故障断言由独立计数 provider 提供。

## 服务来源、环境障碍与复现

Docker Desktop 先后因旧运行时 socket 和 Secrets Engine socket 无法访问而退出。仅对已停止进程的普通 Docker run 目录保留备份并重建；没有继续改动秘密存储，也没有执行恢复出厂设置。此次改用 WSL，不能将本报告称为新容器验收；此前容器记录仍见[2026-10-18 报告](CONTAINER_REDIS_E2E_20261018.md)。

Redis 源码取自[官方仓库 7.4.11 标签](https://github.com/redis/redis/tree/7.4.11)，当时标签指向 `aaf0ce63b3239f4b51f86ca1da8711b055721993`。GitHub 标签归档 SHA256 为 `e973da69febfea096ab94690b44bf976482788a1b5e070df373b4f27697d57d4`；这是该归档的摘要，不是 Redis 官方 release tarball 的摘要。使用 `MALLOC=libc OPTIMIZATION=-O2` 单次 all 目标构建，二进制摘要保存在验证清单。

初次在 Windows 挂载目录并行启动两个 make 目标导致 Lua 链接失败，改用全新的 WSL 临时目录、单次 all 构建成功。首轮实服场景通过，但 WSL 的混合编码 stderr 触发子进程读线程告警；控制协议改为 bytes 捕获、严格解析 UTF-8 stdout，补充字节边界测试后，在新目录完整复验 16 项通过。早期输出和日志保留，没有覆盖它们。

常规独立 Docker 环境可按指南提供自有容器；WSL 环境使用项目 tmp 内的控制 JSON：

```json
{
  "distro": "Ubuntu-24.04",
  "run_id": "本轮16至64位十六进制标识",
  "binary": "/mnt/c/项目根目录/tmp/专用目录/redis-server",
  "config": "/mnt/c/项目根目录/tmp/专用目录/redis.conf",
  "pid_file": "/mnt/c/项目根目录/tmp/专用目录/redis.pid",
  "port": 16389,
  "redis_run_id": "当前INFO中的40位小写十六进制标识"
}
```

上述为字段示意，路径与 ID 必须替换成通过实际身份校验的自有服务。config 还必须指定 `daemonize yes`、`set-proc-title no`、对应 pidfile/port，且不允许 include。运行器需要 WSL 执行权限，stop/start 后新的一轮运行前应刷新 JSON 中的 Redis run ID。

```powershell
$env:PYTHONPATH = 'src'
$env:PYTHONUTF8 = '1'
# TEST_REDIS_URL 由本地隔离环境注入；不要复制业务实例或秘密到仓库。
.venv/Scripts/python.exe -B scripts/verify_cache_protection.py `
  --output output/cache-protection-next --run-id $ownedRunId `
  --wsl-control $ownedControlJson
.venv/Scripts/python.exe -m deepsearch_experiments.architecture
# pytest 先把 TEMP/TMP 与 --basetemp 设为项目内的新目录。
```

边界：没有跨进程 singleflight、分布式锁、Sentinel/Cluster、L1 或失效旧值读取；不同客户端实例可能独立回源。无 Redis 装配时也不会创建这个缓存包装器。未知的同一付费操作仍需原账本与可信核对，缓存保护不改变这一保证。
