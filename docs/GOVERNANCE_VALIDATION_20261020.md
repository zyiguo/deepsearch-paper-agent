# 运行治理、配额与容量验证（2026-10-20）

## 版本与结果

基于 `3ac13301c89331f5e18b1bd2aa3c968243b3d9b5` 及同工作区的恢复增量，新增部署治理实现。验收时尚未提交或推送 GitHub；未修改既有资料和历史验收输出。后续文档更新、前端复核与发布身份见 [2026-10-20 发布记录](GITHUB_PUBLICATION_20261020.md)。机器可读清单保留验收当时的工作区状态字段。

| 检查 | 结果 |
|---|---|
| 后端完整回归 | **1059 passed，14 skipped，110.08 秒** |
| 架构约束 | **214 模块，544 依赖，0 循环，0 违规** |
| 成本/工具真实进程退出 | **5 组全部通过** |
| 生产 ASGI/Agent 合成容量基线 | **12 组，144 条测量请求，2 次重复** |
| Git 差异空白检查 | 通过 |

本轮没有修改前端源码，没有重新运行前端测试与构建。没有重新进行 Docker/真实 Redis/PostgreSQL 或云模型验收；既有相关验收材料保留。新增数据库是部署级 SQLite 控制文件，未变更 PostgreSQL schema。

## 功能与权限

- 10000 点额度遇到两个并发 8000 点预占时，只允许一个；用户或空间任一不足都不部分扣除。
- 同一用户跨空间累计；不同用户共享空间额度。未知费用跨午夜、重建服务后仍冻结。
- 已知 Token 结算、未知用量 null、取消/超时保留费用和确定未执行释放分别验证。
- 模型 HTTP、文本向量批次的报告用量接入；空批次不产生费用记录。缓存命中与工具结果重放不新增费用预占。
- 账务存储故障发生在调用前时拒绝请求；费用结果提交失败发生在调用后时保留未确认状态。
- 同一部署的空间共享研究、入库、供应商资源池；异常、取消和被取消的排队 Future 释放资源。
- 入库未接纳不会留下 queued/inflight；配额拒绝不会把未发生的付费步骤标为 unknown。
- 后台 SSE 按认证发起人计费；幂等重放不增加供应商调用。HTTP 200/SSE error 与研究完成分开统计。
- 未登录访问被拒绝；普通成员不能查看部署指标、修改额度或核对费用。用户查询只返回本人用量和已授权空间总量。
- 管理员修改和核对保存操作者与证明引用，重复核对拒绝；伪造 actor、布尔额度等输入被拒绝。
- 告警 pending/firing/resolved、延迟无样本 null、Redis 恢复和指标记录失败不影响答案均验证。
- 未打开空间的未知工具积压可只读统计，账本原文件摘要不变。

## 五组真实进程退出

[原始报告](../eval/results/governance-recovery-20261020.json)使用 `os._exit(73)`，SQLite 模拟供应商独立提交执行次数，再重建治理服务与工具执行器：

| 中断点 | 恢复后的费用状态 | 供应商执行次数 | 扣除/冻结成本点 | 原工具恢复 |
|---|---|---:|---:|---|
| 费用预占后，调用标记前 | released | 0 | 0 | 仍按工具账本保守阻断 |
| 调用标记后，实际请求前 | unknown | 0 | 8000 | 阻断 |
| 模拟服务完成，费用未确认 | unknown | 1 | 8000 | 阻断 |
| 费用已结算，工具结果未提交 | settled | 1 | 10 | 工具结果仍未知，阻断 |
| 工具结果已提交 | settled | 1 | 10 | 复用结果 |

费用是否确认与工具结果是否确认是两个状态，不互相替代。所有恢复均没有再次调用该模拟付费操作。

## 容量基线

[完整报告与逐请求记录](../eval/results/governance-capacity-20261020.json)冻结了源码摘要。实际执行三空间团队入口、完整父子 Agent 图、检索/重排、独立核验、会话保存和费用计量。每份文献一个固定片段，使用 1024 维固定合成向量；模型为固定 10ms 响应延迟的合成提供者，每次报告 32 个测试 Token。预热使用单独会话。

测量配置主动收紧为全局研究 4、供应商调用 2。每格是两次独立重复，每次 12 条请求：

| 每空间文献数 | 请求并发 | 完成数，两次 | 拒绝数，两次 | 成功请求 p95 秒，两次 | 模型峰值 | 研究峰值 |
|---:|---:|---|---|---|---:|---:|
| 100 | 1 | 12 / 12 | 0 / 0 | 0.675 / 0.654 | 1 | 1 |
| 100 | 4 | 2 / 3 | 10 / 9 | 1.050 / 1.470 | 2 | 4 |
| 100 | 12 | 2 / 2 | 10 / 10 | 1.306 / 1.175 | 2 | 4 |
| 1000 | 1 | 12 / 12 | 0 / 0 | 0.839 / 0.896 | 1 | 1 |
| 1000 | 4 | 3 / 4 | 9 / 8 | 1.941 / 2.053 | 2 | 4 |
| 1000 | 12 | 2 / 2 | 10 / 10 | 1.468 / 1.670 | 2 | 4 |

合计 **68 条完成并带引用，76 条以 HTTP 503 明确拒绝，0 条意外状态码**。所有结束时资源 active 都为 0，供应商峰值不超过 2、研究峰值不超过 4，没有新增未知费用。合成引用结果只证明流程执行，不计算真实回答正确率。

这是收紧限额下的保护基线。研究准入与供应商额度不匹配时，研究过程中也可能遇到供应商容量拒绝，已发生的模型请求仍正常计费。部署调参需要结合真实供应商延迟和可接受拒绝率；需要等待语义时应另设计有界等待与超时，当前采用明确拒绝和显式恢复。

100 文献/空间场景的采样峰值 RSS 为 189.8–204.9 MiB；1000 文献/空间为 364.4–438.8 MiB。RSS 含同进程客户端与服务器，Python 堆采集有 tracemalloc 开销；受预热、分配器、完成任务数量和采样时点影响，用于内存预算参考。小样本 p95 是 nearest-rank 统计，不作为公网 SLA。

高负载基线观测到容量拒绝、研究失败率等告警进入 pending；firing/resolved 由可控时钟测试验证。没有对外发送通知，也没有部署外部监控栈。

## 材料与复现

设计与接口：[运行治理说明](GOVERNANCE_CAPACITY.md)、[ADR-0014](adr/0014-deployment-governance.md)。配置模板：[.env.governance.example](../.env.governance.example)。机器可读验证记录：[验证清单](../eval/results/governance-validation-20261020.json)。

使用新目录，不覆盖旧结果：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONUTF8='1'
$taskTemp=Join-Path (Get-Location) 'tmp/governance-verification-next-env'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP=$taskTemp
$env:TMP=$taskTemp
.venv/Scripts/python.exe -m pytest -q --basetemp tmp/governance-verification-next --tb=short
.venv/Scripts/python.exe -m deepsearch_experiments.architecture
.venv/Scripts/python.exe scripts/verify_governance_recovery.py --output output/governance-recovery-next
.venv/Scripts/python.exe scripts/benchmark_governance_capacity.py `
  --output output/governance-capacity-next --documents 100,1000 `
  --concurrency 1,4,12 --requests 12 --repeats 2 --dimensions 1024
```

报告 SHA256：

```text
governance-capacity-20261020.json
13b43e587aa7167693d7030233741d7ec448e9b1f5faf45403b973e868c6f3ac

governance-recovery-20261020.json
01a0a92d91a828e6210a3285e798e7c1eab55c4546aa80d041f5564d3b2ea69a
```
