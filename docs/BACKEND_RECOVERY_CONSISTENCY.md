# 后端恢复、工具核对与文献入库一致性

本次保持模块化单体、单 API 进程部署，补充记忆授权恢复、未知调用核对、入库步骤持久化。执行账本、图检查点、文献产物和轨迹分别保存，不以轨迹日志决定是否可以再次计费。

## 1. 工作记忆与确定未执行

工作记忆的读取除了返回文本和 revision，还授予该绑定句柄对这个 revision 的一次替换权限。过去从工具账本重放读取时只恢复 JSON，新的句柄没有获得权限，随后 replace 被错误归为结果未知。

现在重放读取恢复历史 revision 的权限，重放写入撤销权限；工具会话检查点保存 `memory_reads`，支持子任务完成后父图中断的恢复。旧检查点缺少该字段时按空权限读取。写入仍在同一锁内检查当前 revision：其他用户修改过内容时拒绝覆盖，必须重新读取并发起新的逻辑操作。

资料之外的工作记忆根目录增加 `.scope-{session_id}` 撤销标记。读取记录关联 `scope_revision`；清空记忆后，即便重新创建的初始文本相同，旧缓存和旧检查点也不能重新授予写权限。标记随进程重启和备份保留，不改变 PLAN.md/HISTORY.md 内容或 revision 哈希规则。旧读取结果没有 scope_revision 时可查看历史观察，但不会获得新写权限。

`ToolNotExecuted` 只用于可信本地检查明确拒绝、尚未发起请求或写入的路径，例如记忆未读取、版本冲突、预算、资料授权和发送前取消。账本保存 `not_executed` 与安全错误码。同槽位重放返回原拒绝，不再次执行。

网络超时、请求发出后的取消、写入中的普通 I/O 错误和无确认结果的外部异常仍保留 unknown。不能根据错误消息推断供应商未计费。

## 2. 未知操作核对闭环

路径相对于 `/api/sessions/{sid}/runs/{rid}`；团队模式沿用 `/w/{workspace_id}` 前缀、Cookie、成员权限和写入来源校验。

| 方法与路径 | 用途 |
|---|---|
| `GET /operations` | 查看安全状态、尝试次数和未执行原因 |
| `GET /operations/{oid}/audit` | 查看事件、核对操作者、证明引用、结果摘要 |
| `POST /operations/{oid}/reconcile` | 核对中断任务中的 unknown 操作 |
| `POST /resume/stream` | 沿用原任务、原检查点继续执行 |

核对要求原任务 interrupted，工具属于该会话、运行和空间。团队 viewer 无权核对，owner/editor 可操作；actor 来自认证会话，不能通过 JSON 冒充。本地模式记录 `local-operator`。

外部 MCP 工具使用人工核实的供应商回执：操作者在供应商后台核对原 operation_id 后，提交原始文本结果。

```json
{
  "confirmed": true,
  "proof_reference": "供应商工单或回执编号",
  "external_result": "原操作已完成，供应商返回的文本结果"
}
```

这是人工确认，系统不自动验证回执真伪。证明引用不自动抓取 URL。文本遵守原外部工具的观察长度预算，保持外部非可信信息边界。接口禁止人工注入本地检索证据、OCR 证据或子规划决策。

工作记忆 replace 可通过本地回读核对：仅当当前内容 SHA256 与持久写入意图的目标内容一致时构造确认结果。它确认目标状态满足，不证明某个进程实际写入了几次。旧操作没有意图、内容不匹配或读取失败时仍不能确认。

结果、succeeded 状态、核对审计在一个 SQLite 事务中提交。审计保存 actor、proof_reference、result_digest，状态接口不返回参数或原始结果。重复核对通过条件更新拒绝。`ready_to_resume` 表示当前没有未知的付费或副作用操作；恢复仍须通过原有语料、配置、上下文和检查点校验。

没有供应商查询能力的本地付费检索、OCR 和子规划继续保留 unknown，不能通过填写一段证据绕过信任边界。可信适配器仍可使用 `ToolExecutor.reconcile` 与 `ConfirmedToolResult`；确认同样进入审计表。

## 3. 文献入库持久步骤

资料目录新增 `.ingestion/{context_fingerprint}/`。上下文包括资料身份、来源、文本/图像模型身份、视觉处理器、分块参数和全量/图像补建模式。旧资料继续读取，不批量改写；配置变化使用独立产物目录。

| 步骤 | 保存内容 | 恢复行为 |
|---|---|---|
| parse | 原生页文本 | 校验复用，不重复 PDF 子进程 |
| analysis | 处理后的页、分块、告警 | 校验复用；无产物的付费步骤保留未知 |
| text_vectors | 分块、文本向量 | 复用向量，不再次 embedding |
| image_vectors | 图像证据、向量 | 复用图像向量 |
| publication | 源级指纹、页摘要、目标元数据 | 核对发布后修复 ready |

步骤先保存 running 意图，再执行，原子保存带身份与摘要的产物，最后保存 completed。产物已经提交而状态尚未提交时，产物可以确认完成。completed 产物缺失或摘要错误时拒绝继续，不自动重新计费来掩盖数据丢失。

没有确认产物的付费步骤，普通重试返回 409。核对账单后可显式使用：

```text
POST /api/documents/{id}/retry?confirm_unknown=true
POST /api/documents/{id}/analyze?confirm_unknown=true
POST /api/documents/{id}/index-images?confirm_unknown=true
```

前端现有重试按钮在 `retry_requires_confirmation=true` 时增加重复计费确认，只有确认后发送参数。许可只作用于本次提交；不会成为后续自动重发配置。`confirmed_unknown_attempts` 保存确认重试的尝试编号，成功不会抹掉记录。视觉恢复还可复用已有逐页缓存。

## 4. 一致性检查与修复

```text
GET  /api/documents/{id}/consistency
POST /api/documents/{id}/repair
```

GET 只读；POST 仅在当前运行的发布记录与索引、页文本、原件都匹配时修复状态，不生成向量、不重新解析、不覆盖原件。普通 retry 也先尝试这项修复。只检查最近一次入库操作，避免用旧图像补建记录掩盖后来的完整入库失败。

校验包括原件 SHA256、页文本摘要、该文献分块/图像证据和向量指纹。其他文献增加不影响核对。向量按 12 位小数比较，忽略保存/加载归一化的浮点舍入；不是语义相似度匹配。配置变化返回 context_changed；遗留资料无发布记录时返回 legacy_or_unpublished，不猜测历史执行情况。

发布失败保留已生成向量。存储修复后，重新发布会合并当时最新的其他资料，不以旧的全库快照覆盖新文献。

原子文件写入先 flush/fsync，再替换目标。Windows 轮询句柄短暂阻止替换时，只有限次重试文件替换，不重跑生成字节的模型或工具。持久权限错误仍会失败。验证针对进程中断，不承诺机器断电和多进程共享写入。

## 5. 验证与运行边界

见 [2026-10-20 验证记录](BACKEND_RECOVERY_VALIDATION_20261020.md)及 [10 组入库退出报告](../eval/results/ingestion-recovery-faults-20261020.json)。独立 SQLite 模拟服务记录次数，子进程实际执行 `os._exit(73)`；不调用真实付费服务。

没有引入分布式锁、消息队列和自动重试。索引与元数据没有跨存储事务，通过持久步骤与可核对的后置条件恢复。新增文件位于既有资料根目录，进入既有停写备份流程。
