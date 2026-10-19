# 工具执行账本与中断恢复

## 当前行为

持久会话任务在独立的工具账本中记录每个逻辑操作。SQLite 文件为当前索引/工作区目录下的 `tool_executions.sqlite3`，与检查点一起进入既有业务备份；即使业务元数据使用 PostgreSQL，账本仍为工作区本地文件。轨迹关闭或记录失败不影响账本的安全决策；账本不可用时不调用工具。

这次覆盖具有 `run_id` 的生产会话任务、子 Agent 规划步骤、本地工具和子 Agent 使用的外部 MCP 工具。无持久运行身份的一次性 `/api/ask`、独立 MCP 服务请求、父 Agent 的 Supervisor/Answerer/Verifier 模型调用不在此次恢复保障范围。子规划步骤虽然不是公开工具，也按付费调用保存决策，避免父图节点重放时重新规划出另一组操作。

任务仍使用提交级 Idempotency-Key 和原子认领；工具账本补上任务内部的执行窗口。现有单 API 进程部署边界不变。

## 身份、状态与恢复

操作 ID 为 `fingerprint(['tool-operation-v1', run_id, logical_slot])`。槽位包含研究轮次、规划/工具阶段及步骤序号；相同参数出现在两个不同步骤时，属于两个独立操作。工具名、参数、种类、可信幂等协议另行计算身份摘要，原槽位复用不同请求将被拒绝。

先持久化 pending，再原子认领为 running/claimed，准备调用前持久化 running/dispatched，最后在一个 SQLite 事务中保存结果并标为 succeeded。每次认领有独立 owner，旧执行者不能覆盖恢复后的状态。事件表追加记录状态变化；没有基于超时自动抢占执行权的逻辑。

启动恢复或原子认领 interrupted 任务后：

| 原状态 | 恢复动作 |
|---|---|
| pending | 可以认领执行 |
| running/claimed | 调用尚未开始，恢复为 pending |
| running/dispatched | 效果未确认，恢复为 unknown |
| succeeded | 复用持久化结果，不再次调用 |
| unknown，只读 | 可以按相同操作身份重试 |
| unknown，付费/写入 | 阻止自动重试，等待可信查询或既有幂等协议 |

没有账本的旧检查点拒绝自动恢复。已有最终答案仍优先返回并修复任务结果关联。取消后的任务保持 cancelled，已发送但未确认的工具调用单独保留 unknown。

模型选定的工具动作先落库，工具调用 ID 同样稳定；重放时恢复原动作、证据片段、工作笔记、工具/OCR 预算和图像计数。外部工具不会因为模型或 MCP 描述宣称“只读”就获得安全重试权限。

## 工具分类

| 类型 | 当前工具 | 结果未确认时 |
|---|---|---|
| 只读 | 列文献、读页、取证据、Wiki 查询/读取、工作笔记读取、纯本地 BM25 检索 | 可以重试；请求次数可能增加 |
| 付费 | OCR；包含 embedding、图像索引或重排器的检索；子 Agent 规划 | 默认阻止重试 |
| 有副作用 | 工作笔记 replace；外部 MCP 工具 | 默认阻止重试 |

检索分类采用保守策略：存在重排/嵌入能力就按付费处理，即使某次调用实际命中缓存。通用外部 MCP 工具当前没有被配置为具有供应商幂等保障；稳定的 tool_call_id 只用于追踪，不能被当作供应商已经实现去重的证明。

## 供应商查询与幂等扩展

执行器支持可信适配器在首次调用时配置版本化 `idempotency_contract`，并把稳定 operation_id 传给回调。回调必须实际向供应商传递幂等键，且供应商承诺覆盖该操作及重试期限；不能只填写协议名。协议身份也进入请求摘要，不能在结果不明后临时切换安全策略。

可信查询适配器可调用 `executor.reconcile(operation_id, lookup)`。只有查询明确确认原操作完成并返回 `ConfirmedToolResult(value)` 时才将 unknown 转为 succeeded。返回 None、普通字典“not_found”或查询超时都不触发重发。查询按 run_id 验证归属。

本次实现了上述扩展接口和模拟供应商验收，没有给真实模型/OCR/MCP 供应商声明 exactly-once 能力。工作笔记文件写入与 SQLite 结果提交也不是跨存储事务：若文件已修改但结果没落库，保持 unknown，不能自动再次 replace。

## 查看状态

`GET /api/sessions/{session_id}/runs/{run_id}/operations` 返回操作 ID、槽位、工具、种类、状态、尝试次数和更新时间。团队模式使用原有 `/w/{workspace_id}` 前缀和成员授权。接口不返回参数、结果、模型提示或证据内容；不属于该会话的运行返回 404。删除会话时清理对应账本，活动任务仍禁止删除。

同步调用的恢复阻断返回安全的 409/503 错误码和 operation_id；SSE 返回 error 事件，任务保留 interrupted。`tool_outcome_unknown` 表示调用效果不明，不等于工具没执行。新建任务会生成新 operation_id，用户明确重新提交仍可能再次产生效果或费用。

## 故障验证

新增测试覆盖 SQLite 原子认领、owner 隔离、并发、参数/策略冲突、取消、付费 OCR、工作笔记写入、外部异步工具、LangGraph 节点重放、工作区权限和备份还原。

另有 [14 组进程退出报告](../eval/results/tool-ledger-faults-20261019.json)：子进程在 after_claim、after_dispatch、after_effect、after_commit 处调用 `os._exit(73)`，再次启动并恢复 SQLite；供应商为独立 SQLite 模拟服务，分别记录请求、效果和计费，未调用真实付费服务。

关键观测：

| 场景 | 请求次数 | 业务效果/模拟计费次数 | 最终状态 |
|---|---:|---:|---|
| 认领后、发送前退出 | 1 | 写入或付费时为 1 | succeeded |
| 只读结果丢失 | 2 | 0 | succeeded |
| 付费效果完成后结果丢失，无供应商幂等 | 1 | 模拟计费 1 | unknown |
| 写入完成后结果丢失，无供应商幂等 | 1 | 效果 1 | unknown |
| 成功结果已提交、图检查点尚未提交 | 1 | 最多 1 | succeeded，复用结果 |
| 供应商幂等支持，效果后结果丢失 | 2 | 效果或模拟计费 1 | succeeded |

after_dispatch 注入发生在实际模拟请求之前；付费/写入也会保持 unknown、请求数 0。这验证了保守边界：没有结果不能证明没有效果，系统可能因此暂时阻止实际上尚未发送的操作。

复现（使用全新输出目录）：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONUTF8='1'
.venv/Scripts/python.exe scripts/verify_tool_ledger_faults.py --output output/tool-ledger-faults-next
.venv/Scripts/python.exe -m pytest tests/test_tool_execution_ledger.py tests/test_tool_ledger_integration.py tests/test_tool_ledger_api.py -q --basetemp tmp/tool-ledger-tests-next
.venv/Scripts/python.exe -m deepsearch_experiments.architecture
```

完整验证结果见 [本轮验证记录](TOOL_EXECUTION_LEDGER_VALIDATION_20261019.md)。
