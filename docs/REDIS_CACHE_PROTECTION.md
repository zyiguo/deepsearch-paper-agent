# Embedding 缓存保护：singleflight 与 TTL 抖动

2026-10-20 文本 Embedding 缓存已接入跨请求 singleflight 和向下 TTL 抖动。它们分别减少同一热点未命中的重叠计算、分散多键集中到期；实际效果以[本轮验证](REDIS_CACHE_PROTECTION_VALIDATION_20261020.md)为准。历史[容器与 Redis 验收](CONTAINER_REDIS_E2E_20261018.md)和治理发布中的 1059 项通过属于各自当时的版本及负载，不能替代本轮证据。

实现入口为 [CachedEmbeddingClient](../src/deepsearch_agent/adapters/infrastructure.py)，环境配置示例为 [.env.infrastructure.example](../.env.infrastructure.example)。当前部署仍为单 API 进程；Redis 保存可丢弃向量与限流计数，工具账本、费用事实、文献与检查点继续各司其职。

## 配置与禁用对照

| 环境变量 | 默认值 | 范围与语义 |
|---|---:|---|
| `EMBEDDING_CACHE_TTL_SECONDS` | `86400` | 正整数，缓存 freshness 上限 |
| `EMBEDDING_CACHE_TTL_JITTER_RATIO` | `0.1` | 有限数值，`0 ≤ r < 1`；`0` 为固定 TTL |
| `EMBEDDING_SINGLEFLIGHT_ENABLED` | `true` | 只接受 `true`/`false`，忽略大小写 |
| `EMBEDDING_SINGLEFLIGHT_WAIT_SECONDS` | `65` | 有限且大于 0，等待所有重叠键共用一个预算 |
| `EMBEDDING_SINGLEFLIGHT_MAX_KEYS` | `4096` | 正整数，每缓存客户端实例的在途键容量 |

这些配置读取项目根目录 `.env.infrastructure`，其中同名值覆盖系统环境变量。已有配置应直接编辑对应项，避免复制示例覆盖其他设置。`REDIS_URL` 为空时不会装配 Redis 缓存包装器；仅设置 singleflight 开关不会独立创建缓存客户端。

保护配置示例：

```ini
EMBEDDING_CACHE_TTL_SECONDS=86400
EMBEDDING_CACHE_TTL_JITTER_RATIO=0.1
EMBEDDING_SINGLEFLIGHT_ENABLED=true
EMBEDDING_SINGLEFLIGHT_WAIT_SECONDS=65
EMBEDDING_SINGLEFLIGHT_MAX_KEYS=4096
```

做固定条件对照时，可以在隔离验证中关闭两项机制：

```ini
EMBEDDING_CACHE_TTL_JITTER_RATIO=0
EMBEDDING_SINGLEFLIGHT_ENABLED=false
```

关闭 singleflight 后仍保留输入校验、批内去重、向量校验和既有回源治理，只是不共享并发未命中的计算。抖动开关独立生效。比较时固定模型、凭据 scope、输入、缓存初态、并发、TTL 上限、预算和输出正确性，不把拒绝请求计入成功吞吐。

## 一次调用如何执行

1. 拒绝空白或非字符串输入，用 `dict.fromkeys` 对当前批次去重；逐 key 读取 Redis，检查维度、有限数值和单位范数，坏值按 miss 处理。
2. 将全部 miss 在一个短锁内登记到本实例的在途表。已有 Future 的键记为 followed，新增键记为 owned；先检查整批是否满足容量，超限整体拒绝，不登记一部分再回源。
3. owner 对自己拥有的键再读一次缓存，处理首次 miss 与认领之间已经回填的竞争窗口。仍未命中的 owned 文本一次交给 delegate，保留批量 Embedding。
4. owner 先完成全部 owned 键，再等待其他 owner 的重叠键。因此 `[a,b]` 与 `[b,a]` 等交叉批次不会因为先等对方而互相阻塞。表锁不覆盖 Redis 或模型网络调用。
5. 实际模型调用沿用 owner 当前线程的 `ContextVar` 用户、工作空间、取消与费用上下文；共享结果不创建新的供应商预占。向量验证后写 Redis，并以不可变 tuple 发布给 followers。
6. 每个调用者恢复自己的输入顺序及重复项，以独立 list 返回向量；修改一个调用者的结果不会污染另一份结果。owner 的实际计算与发布结束后才清理在途条目。

例如两个请求分别输入 `[a,b,a]` 与 `[b,c]`，它们可以共享正在计算的 `b`，仍分别得到三项与两项结果。一次调用可以同时拥有新键并跟随重叠键；不能把“leader 事件数”当成唯一键数或实际供应商请求数。

## 共享范围与身份

在途表属于 `CachedEmbeddingClient` 实例；它的 `__deepcopy__` 返回同一个客户端，使请求/入库快照共用 delegate、Redis 连接资源和在途表。两个独立装配的客户端不共享表；团队模式不同工作空间的运行环境也没有部署级共享表。本实现不提供跨进程协调或 Redis 分布式锁。

Redis key 维持原有 `namespace:embedding:v1:SHA256(identity, scope, text)` 格式，模型/地址/维度组成的 identity 和凭据摘要 scope 均未改变。scope 不是 workspace_id：相同 namespace、模型、凭据与文本的独立客户端可能通过 Redis 先后复用已确认向量，但它们不会共同选举一个进程内 leader。资料访问、证据引用和记忆授权仍由业务层独立校验；命中向量不授予原文权限。

## 等待、容量与异常

| 情况 | 当前行为 | 恢复含义 |
|---|---|---|
| 新键超过在途容量 | `GovernanceDenied('embedding_singleflight_capacity', 503)`，整批拒绝 | 不发生该批的部分认领或部分回源；按入口表现为 HTTP/SSE 容量错误 |
| follower 等待超时 | `RuntimeError` 子类，代码 `embedding_singleflight_timeout` | 不取消 Future，不接管 leader，不自动重试 |
| follower 取消 | 至多每 0.1 秒检查 token 并抛 `ResearchCancelled` | 只结束自己的等待，其他请求继续 |
| leader 失败或取消 | leader 原样收到自身异常；follower 收到 `RuntimeError('embedding_singleflight_leader_failed')` | 不传播另一请求的 operation_id、供应商详情或 `ToolNotExecuted` 分类 |
| Redis GET/SET 失败 | 短超时、冷却后绕过缓存；重叠计算仍可在同一实例合并 | 实际回源仍受 provider 容量与费用配额保护 |
| 进程退出 | 内存 Future 和在途表丢失 | 依靠原运行、工具账本与费用账本核对，不从“表为空”推断远端未执行 |

等待预算覆盖 follower 等待全部 followed 键的时间，不是每个键重新等 65 秒。owner 先执行自己的计算，这段时间不属于之后的 follower 等待预算；参数也不是整个 `embed` 的端到端超时。follower 的 0.1 秒取消检查粒度不表示能强制停止 provider。底层模型调用仍使用原有超时与资源生命周期。

独立研究即使参数相同，也保留各自 run/slot 操作身份。singleflight 只共享同一时段的可重建向量，不合并工具账本记录，不提供付费 exactly-once；同一原 run/slot 已进入 paid/write unknown 时，恢复必须先经工具账本决策。缓存过期、follower 超时、leader 条目清理或锁丢失均不能成为自动重发依据。费用已结算但工具结果未知也继续阻断；详见[工具执行账本](TOOL_EXECUTION_LEDGER.md)及[费用与容量治理](GOVERNANCE_CAPACITY.md)。

## TTL 如何抖动

每次有效回填写入采用：

```text
spread = floor(base_ttl × ratio)
ttl = max(1, base_ttl - randint(0, spread))
```

Redis `EX` 接收整数秒，最终范围是 `[max(1, base-floor(base×ratio)), base]`，两端可取。默认 `base=86400,r=0.1`，范围为 77760–86400 秒；`r=0` 精确退回固定 TTL。对很短 TTL，整数取整可能使 spread 为 0；比例接近 1 时下限仍至少 1 秒。

抖动只缩短有效期，不延长 freshness 上限，不改变向量与键身份。随机分布缓解集中到期，仍可能出现相同 TTL；它无法消除单个热点竞争、Redis 整体宕机或大量不同新文本产生的真实计算需求。恢复预热与旧值读取尚未接入。

## 观测出口与统计分母

启用治理上下文时，`/api/governance/metrics` 提供以下低基数进程指标；团队模式按已有权限要求抓取。重启会重置这些计数与直方图，长期趋势需要外部采集。

| 指标 | 标签与解释 |
|---|---|
| `deepsearch_embedding_singleflight_total` | `outcome=leader/follower/capacity_rejected/timeout/cancelled/error/completed`，每次调用按发生的事件记录 |
| `deepsearch_embedding_singleflight_wait_seconds` | `outcome=completed/timeout/cancelled/error`，包含等待全部 followed 键的时长，以直方图的 bucket/sum/count 输出 |
| `deepsearch_embedding_cache_total` | 既有 hit/miss，按本次输入去重后的首次读取统计；owner 的二次 lookup 不重复计数 |

一个调用可能同时记录 leader、follower 和 completed，这些标签不是互斥分区；`completed` 指开启合并且存在 miss 的调用成功完成，纯命中不记录该事件。leader 事件表示认领了键，二次命中后也可能没有实际回源。等待直方图只覆盖进入 followed 等待的调用，不覆盖 owner 计算或全部研究时延；关闭 singleflight 时不会产生其事件。

指标不携带文本、缓存 key、用户/空间、文献名或 operation_id。比较实际收益还要从隔离 runner 的计数 provider 或真实供应商证据取得 dispatch/输入数，并同时保留完成量、拒绝量、p95、使用量已知/未知与成本归属，不能用 leader/follower 比例直接代替费用节省比例。

## 验证入口与证据边界

本地行为回归入口为 [基础设施测试](../tests/test_infrastructure.py)与 [singleflight、取消及持久计费专项测试](../tests/test_cache_protection.py)，真实 Redis/故障 runner 为 [scripts/verify_cache_protection.py](../scripts/verify_cache_protection.py)：

```powershell
$env:PYTHONPATH = 'src'
.venv/Scripts/python.exe scripts/verify_cache_protection.py --help
$cacheProbeId = [guid]::NewGuid().ToString('N')
$cacheProbeOutput = Join-Path 'output' ('cache-protection-' + $cacheProbeId)
$env:TEST_REDIS_URL = 'redis://127.0.0.1:6379/15'
.venv/Scripts/python.exe scripts/verify_cache_protection.py --output $cacheProbeOutput --requests 16 --run-id $cacheProbeId
Remove-Item Env:TEST_REDIS_URL
```

地址必须指向为该验证准备的隔离真实 Redis，输出目录必须不存在；runner 使用随机命名空间，只清理自己创建的键，不执行 FLUSH。`--redis-url` 可替代环境变量，含凭据的地址宜通过环境变量传入。不带受控故障设施时，停机与恢复场景保持 unverified，退出码为 3。

完整故障验证可使用互斥的两种方式：`--docker-container` 要求隔离容器的 `deepsearch.cache-test` 标签与 `--run-id` 一致，还核对实际 Redis 对端；`--wsl-control` 则要求专用控制 JSON 位于项目 `tmp/`，校验二进制、配置、PID、端口与 Redis run ID 后，只向该自有进程发信号。专用配置需要 `daemonize yes`、`set-proc-title no` 与准确的 pidfile/port。WSL 字段及本轮服务身份见[验证报告](REDIS_CACHE_PROTECTION_VALIDATION_20261020.md)。不要将控制文件指向用户业务服务。

runner 用可计数的合成 provider 验证真实 Redis 下的并发、TTL、写失败、停机/恢复与费用/工具未知行为。真实 Redis + 合成 provider 不等于真实模型验收；小规模真实模型探针、供应商实际调用与用量缺失需单独记录。失败和 `UNVERIFIED` 保留原状，退出成功也不能代替逐场景状态核对。

独立真实模型入口为 [scripts/verify_cache_real_model.py](../scripts/verify_cache_real_model.py)，仅使用固定合成文本，baseline/protected 采用相同的 4 并发，网络发送总预算上限为 5 次，费用控制库放在本次隔离输出中。它会真实调用已配置的付费 Embedding 服务，应在已授权的本轮验证范围或自己的明确调用预算内运行：

```powershell
$realCacheProbeId = [guid]::NewGuid().ToString('N')
$realCacheProbeOutput = Join-Path 'output' ('cache-real-model-' + $realCacheProbeId)
$env:TEST_REDIS_URL = 'redis://127.0.0.1:6379/15'
.venv/Scripts/python.exe scripts/verify_cache_real_model.py --output $realCacheProbeOutput --model-config .env.embedding
Remove-Item Env:TEST_REDIS_URL
```

该入口单独记录实际 dispatch、已知 Token 与 unknown；缺失的供应商用量不以本地估算填成真实用量，也不从总预算推断已发送次数。配置文件与原文不会写入公开报告，实际结果仍需查看本轮验证记录。

本轮结果、源码哈希、真实服务身份与限制统一以 [2026-10-20 验证报告](REDIS_CACHE_PROTECTION_VALIDATION_20261020.md)为准。当前仍无 L1、MGET/pipeline、negative cache、Bloom Filter、stale-while-revalidate、Sentinel/Cluster 或跨进程合并；[并发与 Redis 专题](CONCURRENCY_AVAILABILITY_PERFORMANCE_REDIS.md)保留这些扩展的取舍与故障推演。
