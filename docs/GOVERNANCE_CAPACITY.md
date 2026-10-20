# 业务指标、告警、成本配额与容量基线

## 装配与执行边界

单 API 部署创建一个 `Governance`，所有工作空间共享。配置来自可选 `.env.governance`，示例见 [配置模板](../.env.governance.example)。开发测试可显式注入 `GovernanceSettings`，避免读取用户实际配置。

`core/governance.py` 仅依赖标准库，负责认证身份上下文和确定未执行异常；`application/governance.py` 负责准入、指标及告警策略；持久化和网络计量在 adapters；HTTP 权限与格式在 web。业务层不依赖评测或轨迹数据库。

HTTP 从认证状态取得用户、空间；研究线程和文献线程池复制上下文。持久研究请求保存发起人，恢复由另一名有权成员发起时仍归属原任务发起人。新入库或显式重试归属该次提交者。用户 ID 不接受请求头或 JSON 指定。

## 成本点与配额

成本点是内部额度单位，不是人民币、美元或供应商账单价格。

- 已确认费用：供应商报告的 total_tokens × 对应调用种类的单位换算率；LLM 也可使用完整的 prompt_tokens + completion_tokens。
- 缺失用量：actual 保持 null，继续占用原预占额。成功得到答案不代表用量已知。
- 预占估算：LLM 使用文本 UTF-8 字节数、消息开销、最大输出和图像预留；文本向量使用输入字节数加批次开销；图像向量使用每图预留或查询文本预算。
- 估算不保证覆盖所有供应商的图像和推理计费方式。真实用量超出预占时保存实际值，触发超估告警；不能宣称绝对金额上限。

模型、文本 Embedding、图像 Embedding 和通过模型网关的 OCR/视觉调用已接入。实际适配器发起调用才预占，命中 Redis 缓存、重放子规划或工具确认结果不产生新预占。第三方 MCP 的实际费用尚未自动计量；离线扩展直接调用模型时也不属于 API 部署的额度管理范围。

每次预占在一个 `BEGIN IMMEDIATE` 事务中检查用户和空间，任意一个额度不足都不创建预占。用户额度跨工作空间累计，空间额度覆盖所有成员。默认每日额度未设置，表示不限额；仍记录实际用量并执行并发限制。配置 0 表示禁止付费调用，API 设置 null 表示显式不限额。

| 费用状态 | 含义 | 可用额度处理 |
|---|---|---|
| reserved | 预占完成，尚未进入调用 | 占额度；启动恢复时释放 |
| running | 已进入调用边界 | 占额度；启动恢复时改为 unknown |
| settled | 用量已确认 | 按实际成本点扣除 |
| unknown | 超时、中断或缺少用量 | 按预占额继续冻结 |
| released | 确定未执行 | 不占额度 |

结算日按预占时的 UTC 日期。旧日期未确认的 reserved/running/unknown 仍计入当前冻结额，午夜不释放。确认后费用归原日期。换算率保存在每条预占中，配置变更不会重算旧调用。

费用记录关联 run_id 和 operation_id，保留模型身份、预占、实际值、状态及时间，不保存提示词、图片或凭据。SQLite 文件为本地索引根下或团队根下的 `governance.sqlite3`，进入现有停写备份。业务 PostgreSQL 配置不影响该控制文件；本方案仅支持单 API 部署。

## 管理接口与权限

团队模式：部署级接口直接使用 `/api/governance/...`；空间级用量可使用 `/w/{workspace_id}/api/governance/usage`。沿用 Cookie、成员校验和写入请求的 `X-DeepSearch-Request: 1`/Origin 校验。

| 接口 | 权限与用途 |
|---|---|
| GET `/api/governance/usage` | 已登录用户查看自己的跨空间用量；空间前缀同时返回已授权空间总用量 |
| GET `/api/governance/metrics` | 管理员查看部署聚合指标，不以用户/空间/运行 ID 为标签 |
| GET `/api/governance/status` | 管理员查看容量、滚动延迟、费用积压和告警 |
| GET `/api/governance/alerts` | 管理员查看告警状态 |
| PUT `/api/governance/limits/{user或workspace}/{subject_id}` | 管理员修改每日额度，保存操作者审计 |
| GET `/api/governance/reservations/unknown` | 管理员查看最早的 100 条未知费用及关联身份 |
| POST `/api/governance/reservations/{id}/settle` | 管理员提交核实费用，一条记录只能核对一次 |
| GET `/api/governance/audit` | 管理员查看最近 100 条额度修改与费用核对审计 |

额度请求示例：

```json
{"daily_units": 1000000}
```

未知费用必须核对供应商回执，按该记录的 rate 换算后提交；这是人工确认，不是接口自动鉴别回执真伪：

```json
{"actual_units": 1200, "confirmed": true, "proof_reference": "供应商账单或回执编号"}
```

本地无账号模式作为本地操作者使用这些接口。团队匿名请求不能访问，普通成员不能读取部署聚合或修改配额，也不能指定另一个用户查询。现有团队管理员对私有工作空间仍无隐式访问权限；部署管理接口只提供治理信息。

## 指标与告警

指标出口遵守 [Prometheus 文本格式](https://prometheus.io/docs/instrumenting/exposition_formats/)，不新增运行依赖。HTTP 请求状态和持久会话研究的业务结果分别统计，HTTP 200 的 SSE error 仍记录 interrupted。一次性 `/api/ask` 有 HTTP、供应商指标及资源保护；持久任务完成/取消/阶段指标来自会话运行服务。

| 指标组 | 含义 |
|---|---|
| http_total / http_duration_seconds | HTTP 状态及完整请求耗时，SSE 包含流持续时间 |
| research_total / research_duration_seconds | 持久研究尝试 completed、interrupted、cancelled |
| research_phase_seconds / ingestion_phase_seconds | 研究阶段及解析/分析/向量阶段耗时 |
| provider_total / provider_duration_seconds | success、usage_unknown、error、cancelled、not_executed |
| provider_reported_tokens_total | 已报告 Token，不以估算补齐缺失值 |
| embedding_cache_total / redis_total | 缓存命中、未命中和 Redis 故障事件 |
| tool_total | 成功、未知、确定未执行和确认结果重放 |
| admission_total / resource_rejection_total / quota_rejection_total | 资源拒绝及用户/空间配额拒绝 |
| resource_active / resource_peak / resource_limit | 研究、入库、供应商全局资源量 |
| unknown_count / unknown_tool_count 等 | 未知费用与工具积压、最老时长及当日费用超估 |
| rolling_latency_p95_seconds / alert_firing | 滚动 p95 和告警状态；无延迟样本时不填 0 |

指标统一以 `deepsearch_` 开头。只使用路由模板、HTTP 状态、种类、阶段、结果等有限维度，不包含问题、文件名、用户、空间或运行 ID。指标槽位、事件窗口和延迟样本数量有上限。计数器/直方图是进程指标，重启会重置；额度、核对审计与告警状态持久化。

后台每 5 秒评估：未知费用/工具滞留、当日预占超估、容量拒绝或饱和、配额拒绝、Redis 降级、研究/入库/供应商失败率和研究/供应商 p95。失败率窗口 5 分钟、默认至少 5 条样本；p95 窗口每类最多 1000 个延迟样本，业务事件窗口最多 10000 条。默认失败比例 25%，未知滞留 300 秒，模型 p95 30 秒、研究 p95 120 秒，条件保持 60 秒进入 firing。

告警保存 pending/firing/resolved；状态变化输出不含业务内容的 `governance_alert` 结构化日志。Redis 恢复后按当前后端状态解除降级。工具积压会只读扫描未加载空间账本，不为采集而启动模型或迁移这些空间。

本次没有部署 Prometheus/Alertmanager/Grafana，也没有配置邮件、聊天消息或外部通知。API 和结构化日志提供告警出口；长期趋势需要部署环境配置采集，团队抓取沿用管理员认证。

## 全局资源保护

| 资源 | 默认全局 | 默认每空间 | 默认每用户 |
|---|---:|---:|---:|
| 研究 | 8 | 4 | 4 |
| 入库（含排队任务） | 4 | 4 | 4 |
| 模型/向量供应商调用 | 8 | 4 | 4 |

同一用户跨空间共享用户限制。资源释放覆盖异常、取消、后台结束和队列 Future 取消。调用拒绝发生在供应商请求前；入库接纳失败会撤销 inflight 并标为 failed，不留下无限 queued。已发出的付费请求取消后费用仍可保持未知。原有每空间线程/工作区打开数/文件大小限制继续有效。

超过资源容量返回 503，额度不足返回 429；SSE 已开始则发送带 code 的 error。没有自动排队重试或租约超时抢占。并发控制不是操作系统级硬 CPU/RSS 上限，外部 MCP 也不属于此模型/向量供应商并发池。

## 容量基线

```powershell
$env:PYTHONPATH='src'
$env:PYTHONUTF8='1'
$taskTemp=Join-Path (Get-Location) 'tmp/governance-next-env'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP=$taskTemp
$env:TMP=$taskTemp
.venv/Scripts/python.exe scripts/benchmark_governance_capacity.py `
  --output output/governance-capacity-next --documents 100,1000 `
  --concurrency 1,4,12 --requests 12 --repeats 2 --dimensions 1024
```

脚本运行真实团队 ASGI、三个工作空间、父子 Agent、检索/重排、独立核验、任务保存和计量接口；模型/向量提供者为本地合成服务，不发出真实云请求。每份合成文献只有一个固定片段，向量是 1024 维固定测试向量，不代表整篇 PDF 或真实召回难度。供应商响应使用固定延迟和合成 Token 用量。

每个场景新建目录，独立预热；记录每条请求、完成数、拒绝数、成功吞吐量、全请求/成功请求延迟分位数、资源峰值、RSS 与 Python 堆。客户端与服务端同进程，tracemalloc 有开销，RSS 每 10ms 采样；没有将拒绝计入成功吞吐量，也不把未知内存值填 0。报告不是公网 SLA 或生产最大容量结论。

源码摘要在运行前冻结；过程中发生源码变化则拒绝生成完成报告，保留已写入的逐场景结果。使用新目录，禁止覆盖原验收输出。最终结果见 [本轮验证记录](GOVERNANCE_VALIDATION_20261020.md)。
