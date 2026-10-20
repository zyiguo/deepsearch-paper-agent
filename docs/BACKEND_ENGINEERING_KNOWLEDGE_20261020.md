# 后端工程知识点：从真实执行顺序讲清设计与边界

本文用于结合项目代码准备后端面试。每一节按“输入 → 执行顺序 → 输出/解决问题 → 源码入口 → 概念与失败示例 → 面试追问”展开。先用一个具体请求解释行为，再介绍事务、幂等、隔离和监控等概念，避免只背定义。

范围是 2026-10-20 工作区已实现的模块化单体、单 API 进程。业务数据可选 PostgreSQL；检索索引仍是内存对象与 JSON 快照，检查点、工具执行账本、费用控制库和文件产物仍有各自的存储。后文的多副本、供应商幂等协议、外部监控栈等只在明确标注时作为扩展方向。

项目全貌与研究评测见[主技术报告](INTERVIEW_TECHNICAL_REPORT_20261019.md)，部署治理接口与配置见[运行治理说明](GOVERNANCE_CAPACITY.md)，恢复操作见[后端恢复与入库一致性](BACKEND_RECOVERY_CONSISTENCY.md)。本文侧重解释机制及其失败边界。

## 阅读前先固定证据口径

| 已有证据 | 能支持的结论 | 不能直接推出的结论 |
|---|---|---|
| [治理验证清单](../eval/results/governance-validation-20261020.json)：1059 passed、14 skipped；214 模块、544 依赖、0 循环、0 违规 | 该源码快照下的本地回归与架构检查通过；跳过项单列 | 云模型、真实 Redis/PG、多进程及生产环境全部验收 |
| [治理进程退出报告](../eval/results/governance-recovery-20261020.json)：5 组退出场景通过 | 独立 SQLite 模拟供应商与进程退出下的费用/工具恢复符合预期 | 真实供应商承诺 exactly-once 或机器断电安全 |
| [容量报告](../eval/results/governance-capacity-20261020.json)：144 请求，68 完成，76 保护拒绝，0 意外状态码 | 收紧资源限额时流程可执行，资源峰值未越界，结束后租约释放 | 生产最大 QPS、公开 SLA、真实论文回答正确率 |
| [恢复验证记录](BACKEND_RECOVERY_VALIDATION_20261020.md)：工具 14 组、入库 10 组进程退出 | 相关进程中断窗口被覆盖 | 所有存储构成一个原子事务 |

容量实验使用真实 ASGI、团队入口和父子 Agent 图，但模型是固定 10ms 延迟的合成提供者，每份文献只有一个固定片段，向量为 1024 维合成向量。测量时全局研究限额为 4、供应商限额为 2，区别于默认配置。数据与限制详见[运行治理验证](GOVERNANCE_VALIDATION_20261020.md)。本文引用这些已有报告；重新执行得到的新结果应使用新目录，不能改写历史结果。

## 1. 工具幂等：逻辑操作身份、请求摘要与 owner fencing

### 输入

一个具有 `run_id` 的持久研究任务，准备执行工具，例如某轮研究的第 2 个 OCR 步骤。输入包含逻辑槽位 `logical_slot`、工具名、参数、工具种类，以及可信适配器预先配置的 `idempotency_contract`。

### 执行顺序

1. 计算操作 ID：`fingerprint(['tool-operation-v1', run_id, logical_slot])`。它回答“这是任务中的哪一步”。
2. 另算请求身份摘要：`fingerprint([tool, str(kind), arguments, capability])`。同一槽位若换参数、工具种类或幂等协议，直接拒绝 `tool_operation_conflict`。
3. 在 SQLite 中持久化 `pending` 意图，`UNIQUE(run_id, slot)` 保证一个逻辑槽位只有一条操作记录。
4. 在 `BEGIN IMMEDIATE` 事务内检查状态并认领，生成新的随机 `owner`，状态改为 `running/claimed`，尝试次数增加。
5. 调用前通过带条件的 UPDATE 改为 `running/dispatched`；调用返回后，在一个事务中写结果、`succeeded` 和事件。
6. 图节点重放时，`succeeded` 直接读历史结果；`not_executed` 重放原拒绝；付费/写入的 `unknown` 默认阻断。

### 输出与解决的问题

输出包含结果、稳定 `operation_id` 与 `replayed` 标志。它解决“图节点重新运行，已经完成的工具又执行一次”的问题，也能识别“调用方错误地把同一个幂等键用于另一个请求”。任务提交层的 `Idempotency-Key` 防重复接纳，工具层操作 ID 防任务内部重复执行，两层职责不同。

### 源码入口

- [ToolExecutor](../src/deepsearch_agent/application/tool_execution.py)：准备、发送、提交、重放和阻断策略。
- [SQLiteToolLedger](../src/deepsearch_agent/adapters/persistence/tool_ledger.py)：`prepare/claim/dispatch/succeed` 与事件事务。
- [任务提交与恢复](../src/deepsearch_agent/application/session_runs.py)：`prepare_run/resume_run`。
- [定向测试](../tests/test_tool_execution_ledger.py)：身份冲突、并发认领、旧执行者提交。

### 概念与失败示例

CAS 是“只有当前状态仍满足条件才更新”。这里的提交条件形如：

```sql
UPDATE tool_operations
SET status = 'succeeded', result_json = ?, owner = NULL
WHERE id = ? AND owner = ? AND status = 'running';
```

若旧执行者 A 在恢复后才返回，而新执行者的 owner 已变化，A 更新的行数为 0，收到 `tool_execution_fenced`。这里的 fencing 是本地账本中的 owner 条件隔离；它不是下游供应商识别的递增 fencing token，不能阻止已经发出的远程请求发生效果。认领本身是事务内检查状态后更新，后续提交才使用 owner 条件；不要把所有步骤描述成一条 CAS SQL。

同参数出现在两个不同逻辑步骤时，operation_id 不同，因为业务允许两次独立调用。反过来，不能在 unknown 后换一个槽位或新建任务来“修复”原操作，那会建立新的调用身份，可能再次计费。稳定 ID 只提供本地去重身份；只有适配器实际传递该键，且供应商协议覆盖该操作、参数及重试期限，才可能对远端效果去重。项目没有宣称真实模型/OCR/MCP 已提供 exactly-once。

### 面试追问

- **为什么不只对参数做哈希？** 同参数可能是两个合法步骤；必须先定义逻辑操作，再检查其请求内容一致性。
- **为什么要 owner？有状态字段不够吗？** 状态可能再次进入 running，旧执行者仍能晚到；owner 将写回权限绑定到这次认领。
- **可以按 running 超时自动抢占吗？** 当前没有该机制。超时不证明远端停止，恢复只在独占启动或 interrupted 任务已被原子认领后执行。

## 2. Crash 窗口：unknown 与 not_executed 如何决定重试

### 输入

进程可能在调用前后退出，或者供应商请求超时、取消、返回后结果落库失败。恢复时拥有的是账本阶段、历史请求身份和可能存在的结果，而不是远端执行的全知视角。

### 执行顺序

1. 独占恢复检查 `running` 的阶段：`claimed` 说明尚未越过发送边界，可回到 pending；`dispatched` 表示效果尚未确认，转为 unknown。
2. 可信本地检查若明确证明没有请求/写入发生，抛 `ToolNotExecuted`，保存 `not_executed` 与安全错误码；例如版本冲突、额度拒绝、资料授权拒绝、发送前取消。
3. 普通 I/O 错误、网络超时、发送后的取消不能证明未执行，仍按 unknown 处理。
4. 只读 unknown 可用相同操作身份重试；付费/写入 unknown 等待可信查询、已有供应商幂等协议，或受控人工核对，不自动重发。
5. 可信查询只有返回 `ConfirmedToolResult` 才能把原 unknown 确认为 succeeded。`None`、普通字典 `not_found`、超时都不会触发重发。
6. 人工核对检查原任务 interrupted、会话/运行/空间归属和写权限，将结果、状态、actor、proof_reference、result_digest 在同一账本事务中提交，再从原任务恢复入口继续。

### 输出与解决的问题

输出是“可安全继续、原拒绝重放、结果复用、仍需核对”之一。保留 unknown 解决的是不确定效果下的重复收费/写入风险，不保证每个任务自动完成。

| 工具中断窗口 | 恢复判断 | 不经额外确认的行为 |
|---|---|---|
| claimed 后、dispatch 前 | 尚未发送 | 回到 pending，可认领 |
| dispatched 已保存、实际请求前 | 持久信息不足以证明未发送 | 付费/写入保持 unknown；可能保守阻止一个实际未发送请求 |
| 供应商已产生效果、结果未落库 | 远端完成、本地未确认 | unknown，不再调用 |
| 工具结果已落库、图检查点未落库 | 操作已确认 | 重放已存结果 |

### 源码入口

- [异常与确认结果合同](../src/deepsearch_agent/core/tool_execution.py)：`ToolNotExecuted/ConfirmedToolResult`。
- [执行器](../src/deepsearch_agent/application/tool_execution.py)：`_start/execute/reconcile`。
- [业务核对](../src/deepsearch_agent/application/tool_reconciliation.py)：核对归属、工作记忆回读及外部结果限制。
- [核对操作说明](BACKEND_RECOVERY_CONSISTENCY.md)与[故障脚本](../scripts/verify_tool_ledger_faults.py)。

### 概念与失败示例

用户取消研究只说明不再需要继续生成答案；已经发出的付费请求仍可能计费，工具记录可以 unknown，而任务是 cancelled。将异常统一改成 `not_executed` 会错误释放重试权限。

工作记忆 replace 的核对可回读文件，只有当前内容 SHA256 等于持久写入意图的目标摘要才确认目标状态满足。它不证明文件只写过一次。外部 MCP 可由 owner/editor 提交核实回执，但回执真伪由操作者承担核实责任，接口不会抓取证明 URL，也不会自动鉴别回执。人工入口不能注入本地检索、OCR 或子规划结果来冒充可信证据。

### 面试追问

- **供应商查询说没找到，为什么还不重试？** 查询可能延迟、范围不完整或暂时失败；当前合同只接受明确完成证明，未实现可信“确定未执行”查询协议。
- **费用已经 settled，能否把工具 unknown 改成功？** 费用确认和结果确认不同，缺少可用结果仍不能继续重放。
- **如何增加自动恢复率？** 为具体供应商实现带稳定键、参数约束、有效期限和可信结果查询的适配器，先验证协议，再启用自动重试。

## 3. 工作记忆：revision 防覆盖，scope epoch 防撤销后授权复活

### 输入

一个已绑定会话的 `BoundWorkingMemory` 句柄，准备读取或完整替换 PLAN/HISTORY。替换必须携带 `expected_revision`，且该句柄曾读取这个 revision。

### 执行顺序

1. `bind` 校验会话与路径，读取持久 `.scope-{session_id}` 随机代际标记，作为 `scope_revision`。
2. `read` 返回内容、内容 SHA256 revision、scope_revision 和 `working_notes_not_evidence` 信任标签；句柄记录读取权限。
3. `replace` 在同一锁内重新读取当前内容，检查句柄仍有效、存在对应读取权限、当前 revision 仍等于 expected_revision，才原子替换文件。
4. 写成功后消耗这次读取权限；写入回执不是新一次读授权。
5. 工具结果重放与图检查点恢复会恢复历史读取权限，但仅在 scope_revision 与当前代际相同的情况下授予。历史观察保持历史内容，不会偷偷更新成新文件内容。
6. `clear` 先持久化新的代际标记，再删除文件；旧句柄、旧缓存和旧检查点失去替换权限，标记跨进程重建与备份保留。

### 输出与解决的问题

输出是读结果、替换结果或明确的 `memory_read_required/memory_conflict/memory_scope_expired`。revision 避免两个编辑者互相覆盖；scope epoch 避免“清空后又重建相同文本”导致旧授权复活。

### 源码入口

- [WorkingMemoryStore/BoundWorkingMemory](../src/deepsearch_agent/workspace/working_memory.py)：`bind/clear/read/restore_read/replace`。
- [PaperToolSession](../src/deepsearch_agent/agents/tool_service.py)：`runtime_snapshot/restore_runtime/invoke` 保存/恢复读取能力。
- [恢复测试](../tests/test_backend_recovery.py)：旧 revision、相同初始文本、重建存储与检查点恢复。

### 概念与失败示例

假设初始文本 A 的摘要是 h。旧任务读到 h，用户随后 clear，新建文件又是 A，摘要仍为 h。若只检查内容哈希，旧任务可以覆盖用户清空后的文件，形成类似 ABA 的问题。代际从 e1 变为 e2 后，即使内容摘要相同，e1 的旧读授权也无效。

旧版读取结果没有 scope_revision 时，可以作为历史观察查看，但不会授予新的写权限。当前锁是单进程锁；此设计不提供多个 API 进程共同写同一目录的分布式并发控制。

### 面试追问

- **为什么已读权限也要进检查点？** 子任务已读但父图中断后，新句柄需要恢复历史能力；只恢复 JSON 会让合法写入被误拒绝。
- **恢复读取权限会不会授权覆盖新内容？** 不会，replace 仍在锁内检查当前 revision，scope 还必须未撤销。
- **工作记忆可以当论文证据吗？** 不能，信任标签明确区分工作笔记与授权原文。

## 4. 分阶段入库：先保存可校验产物，再确认完成

### 输入

文献原件、来源身份、文本/图像模型身份、视觉处理器、分块配置，以及完整入库或图像补建模式。它们共同计算 `context_fingerprint`，在资料根下形成独立 `.ingestion/{context_fingerprint}/`。

### 执行顺序

1. `parse` 保存原生页文本；`analysis` 保存处理后页文本、分块与告警；`text_vectors/image_vectors` 保存对应向量与证据。
2. 每步先保存 running 意图，再调用解析器或供应商。
3. 返回后构造包含 identity、step、digest、value 的产物，校验 digest 对应 value，原子写入 artifact；最后将步骤状态写为 completed。
4. 恢复先找合法 artifact：若存在，可复用并补写 completed；若 completed 的产物缺失或摘要不符，拒绝自动重算，提示核查/恢复备份。
5. 付费步骤若只有 running/unknown、没有合法 artifact，普通 retry 返回 409。用户核对账单后可显式 `confirm_unknown=true`；该许可只对本次提交有效，并保留确认尝试记录。
6. 发布前持久化源级索引指纹、页摘要与目标元数据；发布索引后再完成 publication、更新 ready。
7. 若发布已成功而 ready 未保存，consistency 比对原件 SHA256、页文本摘要、该文献分块/图像和向量指纹，匹配后 repair 只修复状态，不再次解析或生成向量。

### 输出与解决的问题

输出是 ready 文献、可恢复步骤，或明确的 unknown/一致性冲突。它减少已确认步骤的重复开销，并让索引发布与元数据之间的中断有可核对的恢复路径。

### 源码入口

- [IngestionJournal](../src/deepsearch_agent/workspace/ingestion.py)：`run/_artifact/publication/published`。
- [DocumentStore](../src/deepsearch_agent/workspace/documents.py)：`journal/process/consistency/source_snapshot`。
- [atomic_write](../src/deepsearch_agent/adapters/persistence/local_files.py)：临时文件、flush/fsync、替换。
- [入库恢复测试](../tests/test_ingestion_recovery.py)与[故障脚本](../scripts/verify_ingestion_recovery.py)。

### 概念与失败示例

“模型已返回 → 进程退出 → artifact 未落盘”仍是付费结果未知，不能假定没计费。“artifact 已落盘 → completed 未落盘”则可凭身份与摘要恢复。“索引已发布 → ready 未落盘”可凭发布后置条件修复。

这是持久步骤与可核对后置条件，**不是严格的跨存储两阶段提交（2PC）**。文件、索引和元数据没有共同事务协调器。`prepared` 记录不是所有参与者都承诺可提交的分布式 prepare 阶段。checksum 证明产物与记录匹配，不证明模型语义正确，也不抵御能同时改写记录与产物的攻击者。

发布重试会合并当时最新的其他文献，避免旧全库快照覆盖后来资料；一致性检查只核对最近入库模式，配置变化返回 context_changed。向量按 12 位小数比较以容忍保存/加载归一化舍入，不采用语义相似度“差不多就算成功”。Windows 文件替换的有限重试只替换已生成字节，不重跑模型；当前验证针对进程退出，不承诺机器断电或共享多进程写入。

### 面试追问

- **为什么状态 completed 不能单独证明成功？** 状态与产物可能分开保存，恢复必须验证实际产物存在且完整。
- **为什么 repair 不补向量？** repair 只确认既有后置条件，补向量属于新计算，可能计费，需进入明确执行路径。
- **与 Saga 有什么关系？** 都拆分步骤并追踪状态；本项目主要复用产物和前向核对恢复，未实现通用补偿事务编排，不应直接宣称完整 Saga 框架。

## 5. 成本配额：用户与工作空间同时预占，未知费用继续冻结

### 输入

认证的 user/workspace、run_id/operation_id、调用类型、模型、估算 Token 与单位换算率。成本点是内部额度单位，不是人民币、美元或供应商账单价格。

### 执行顺序

1. 在真实模型/向量适配器调用边界估算额度。LLM 使用 UTF-8 字节数、消息开销、最大输出与图像预留；Embedding 使用输入字节数和批次开销。
2. `GovernanceStore.reserve` 在一个 `BEGIN IMMEDIATE` 事务中，同时检查用户与空间额度；任何一方不足都不创建预占，避免“用户扣成功、空间扣失败”。
3. 写入 reserved 后，再改为 running，才进入供应商调用。缓存命中、工具结果重放及空向量批次不新增供应商预占。
4. 确认用量后 settled，实际成本点为报告 Token × 该记录保存的 rate；没有用量时 actual 保持 null，状态 unknown，继续冻结预占。
5. 可信的确定未执行释放额度。启动恢复将 reserved 释放，将 running 改为 unknown。费用账本故障发生在请求前时阻断请求；请求后的结算失败不能冒充未执行。
6. 管理员核对原供应商账单，提交 actual_units、confirmed、proof_reference；条件更新只允许 unknown 核对一次，并在同一事务保存操作者审计。

### 输出与解决的问题

输出是接纳/拒绝、剩余额度及可核对费用记录。用户额度跨空间累计，空间额度覆盖全部成员，同时解决个人滥用和团队总预算问题。

| 状态 | 所掌握的信息 | 对额度的处理 |
|---|---|---|
| reserved | 已预占，尚未进入调用边界 | 占用；独占启动恢复时释放 |
| running | 已进入调用边界 | 占用；恢复为 unknown |
| settled | 已确认实际用量 | 按实际成本点计入原预占日 |
| unknown | 效果/用量未确认 | actual=null，原预占继续冻结 |
| released | 确定未执行 | 不占用 |

### 源码入口

- [Governance.invoke](../src/deepsearch_agent/application/governance.py)：供应商资源、预占、发送、结算。
- [GovernanceStore](../src/deepsearch_agent/adapters/persistence/governance.py)：双作用域事务、日界、审计核对。
- [metered](../src/deepsearch_agent/adapters/governed_calls.py)：真实调用边界估算。
- [管理接口](../src/deepsearch_agent/web/governance.py)与[默认配置](../src/deepsearch_agent/adapters/governance_config.py)。

### 概念与失败示例

额度 10000，同时来两个估算 8000 的请求。如果先查询再分别写入，两个线程都可能读到余额 10000；事务化检查与插入只接纳其中一个。系统同时检查 user 与 workspace，保证两个约束一起成立。

23:59 UTC 的 unknown 到次日仍冻结，不能靠跨午夜“洗掉”未知费用；确认后实际费用归原预占日期。若供应商成功返回答案却没报告 Token，actual 仍为 null，不能填 0，也不能把估算伪装成实际值。估算可能不足，actual 超过 reserved 时仍保存实际值并告警，因此它是准入额度控制，不是供应商金额绝对上限。

默认日额度为未设置，即不限额；配置 0 表示禁止付费调用，管理 API 的 null 表示显式不限额。当前已覆盖模型、文本/图像 Embedding 和走模型网关的视觉/OCR；第三方 MCP 实际费用及绕过 API 治理的离线模型调用尚未自动纳入。费用库仍为部署级 SQLite，PG 业务库配置不会改变这一点。

### 面试追问

- **两条记录都叫 unknown，能合并吗？** 工具 unknown 表示结果未确认，费用 unknown 表示用量/费用未确认，两者通过 operation_id 关联但独立演进。
- **限额为什么不直接在模型输出后检查？** 输出后费用可能已经发生，预占把拒绝尽量移到调用前。
- **修改换算率后旧账如何处理？** rate 固定在预占记录内，旧调用不随新配置重算。

## 6. 资源租约：原子多维准入、背压与 Little's Law

### 输入

某个用户在某空间申请 research、ingestion 或 provider 资源。单个部署创建一个共享 `Governance`，不同空间不能各自拥有一份“全局”计数器。

### 执行顺序

1. `resource(kind)` 构造 global、workspace、user 三个计数键。
2. 在同一 RLock 内检查三个维度；任何一项满了均不增加其他计数，拒绝返回 503。
3. 全部允许后同时加一，记录全局峰值；退出上下文时在 finally 同时减一。
4. 入库通过 `GovernedExecutor.submit` 在提交到线程池前取得租约，排队和运行都占容量。
5. Future 在排队时取消、工作函数异常、提交失败都释放租约；释放函数带锁且最多执行一次。
6. 入库未接纳时撤销 inflight 并标为 failed，不留下无限 queued；配额不足另返回 429，SSE 已开始时用带 code 的 error 事件传达。

### 输出与解决的问题

输出是有限并发或明确拒绝，避免线程池队列持续增长、供应商同时请求过多，也防止单一用户跨空间绕开个人限制。当前没有自动等待队列、自动重试或超时抢占。

| 资源 | 默认全局 | 默认每空间 | 默认每用户 |
|---|---:|---:|---:|
| research | 8 | 4 | 4 |
| ingestion（含排队） | 4 | 4 | 4 |
| provider（模型/向量） | 8 | 4 | 4 |

默认值已与[配置类](../src/deepsearch_agent/adapters/governance_config.py)和[模板](../.env.governance.example)核对；实际部署可覆盖。每空间原有线程数、打开空间数和文件大小限制仍可能先成为瓶颈，默认全局 8 不等于一个空间一定能同时运行 8 个研究。

### 源码入口

- [Governance.resource](../src/deepsearch_agent/application/governance.py)：多维原子准入与 finally 释放。
- [GovernedExecutor](../src/deepsearch_agent/adapters/governed_calls.py)：排队时持有租约与取消释放。
- [RunWorkers/execute_run](../src/deepsearch_agent/application/session_runs.py)：线程生命周期与研究资源。
- [资源治理测试](../tests/test_governance.py)与[容量脚本](../scripts/benchmark_governance_capacity.py)。

### 概念与失败示例

背压是让上游感知下游容量不足，当前方式是明确拒绝；它不是“任务最终总会在后台完成”。若需要等待语义，应扩展有界队列、最大等待时间、公平调度、取消和过期机制，再核对费用语义。

Little's Law 为 `L = λW`：在稳定系统、同一观测边界下，平均在途数 L 等于完成吞吐率 λ 乘平均停留时间 W。若某调用阶段平均 2 秒，希望稳定完成 4 次/秒，则平均需要约 8 个在途调用；这是稳态估算，不是对本项目生产 QPS 的实测。多阶段研究会反复调用模型，研究并发不能直接除以单次模型耗时来算端到端吞吐。系统已过载或不断积压时，也不能套稳态公式宣称容量足够。

合成基线中研究全局 4、provider 全局 2，研究接纳后仍可能在中途遭遇 provider 拒绝，已经执行的调用照常计费。68/144 完成与 76 次拒绝必须一起报告；0 意外状态码不等于 144 次业务成功。并发租约也不是操作系统 CPU/RSS 硬限额，外部 MCP 尚不属于此 provider 池。

### 面试追问

- **为什么线程池有 max_workers 还需要入库租约？** max_workers 只限制运行线程，未必限制待执行队列；租约把排队任务也纳入保护。
- **为什么不分别用三个 semaphore？** 分开获取容易占住一个维度等待另一个；本项目在一把锁内完成全检查与全计数。
- **可以扩展多 API 副本吗？** 当前计数器在进程内；多副本需共享准入与资源所有权设计，并先解决索引可见性。

## 7. ContextVar 跨线程：复制执行上下文，恢复原任务归属

### 输入

HTTP 认证后得到的 user/workspace，以及即将转交研究线程或入库线程池的函数。线程内供应商调用需要知道额度归谁、属于哪个 run/operation。

### 执行顺序

1. HTTP 层从认证状态构造 `UsageScope`，通过 `usage_context` 设置 `current_governance/current_usage_scope`，不接受 JSON 或自定义请求头指定计费用户。
2. `usage_context` 使用 token 保存旧值，并在 finally reset，避免后续请求继承当前身份。
3. 研究线程和入库 submit 时调用 `copy_context()`，在新线程中用 `context.run(...)` 执行；普通 Thread/线程池不应被假定会自动继承 ContextVar。
4. 持久研究提交时保存 `_billing_user/_billing_workspace`；任务恢复时重新建立原发起人的 scope，另一名有权成员恢复不会把费用转嫁给恢复人。
5. 新入库或明确重试是本次提交，归本次认证发起人；工具执行器再嵌套 operation_id，费用记录可与原工具操作关联。

### 输出与解决的问题

输出是正确归属的资源计数、费用和审计关联，解决“后台线程默认变成 local 用户”或“恢复由谁点击就扣谁额度”的错误。

### 源码入口

- [UsageScope/usage_context](../src/deepsearch_agent/core/governance.py)：不可变 scope、ContextVar 与 token reset。
- [HTTP 上下文](../src/deepsearch_agent/web/operations.py)：认证身份进入治理上下文。
- [RunWorkers 与原任务身份](../src/deepsearch_agent/application/session_runs.py)：`start/prepare_run/execute_run`。
- [线程池上下文复制](../src/deepsearch_agent/adapters/governed_calls.py)：`GovernedExecutor.submit`。

### 概念与失败示例

ContextVar 隔离的是执行上下文，不是线程安全锁，也不是持久身份存储。HTTP 请求结束后，复制给线程的上下文仍可使用；进程重启后则必须从可信持久记录恢复身份。不能把内存中的 ContextVar 当作崩溃恢复依据。

```python
from contextvars import copy_context
from threading import Thread
from deepsearch_agent.core.governance import current_usage_scope, usage_context

observed = []
with usage_context(user='demo-user', workspace='demo-space'):
    context = copy_context()
    worker = Thread(target=lambda: context.run(
        lambda: observed.append(current_usage_scope.get().user)))
    worker.start()
    worker.join()
assert observed == ['demo-user']
assert current_usage_scope.get().user == 'local'
```

### 面试追问

- **ContextVar 能控制租约并发吗？** 不能，它传递身份；租约计数仍要锁与 finally。
- **多个线程可同时进入同一个复制后的 Context 吗？** 不应共享同一 Context 并发进入；本实现为每次线程创建/submit 单独复制。
- **管理员恢复别人任务能否越过资料授权？** 计费归属与资料访问是两套约束；恢复仍校验文献范围、配置/上下文身份及空间权限。

## 8. 可观测性：HTTP 成功、业务完成与答案正确分别计量

### 输入

HTTP 请求生命周期、持久研究终态、阶段耗时、供应商结果、缓存事件、容量拒绝，以及工具/费用未知积压。指标只应描述运行状态，不携带问题和原文。

### 执行顺序

1. HTTP 中间件记录路由模板、状态与完整耗时；SSE 持续时间包含在 HTTP 耗时中。
2. 持久研究服务单独记录 completed/interrupted/cancelled 和阶段耗时；供应商记录 success/usage_unknown/error/cancelled/not_executed。
3. 指标标签仅用路由模板、状态、调用种类、阶段与结果等有限维度；不用用户、空间、运行 ID、问题或文件名。
4. Counter/Histogram 有槽位上限；近 5 分钟事件最多 10000 条，每类 provider/research 延迟最多 1000 条。
5. p95 在当前窗口排序后取 nearest-rank；无样本为 null，指标出口不把缺失值伪装成 0。
6. 默认每 5 秒评估未知滞留、配额/容量、Redis 降级、失败率和延迟；条件出现进入 pending，持续默认 60 秒进入 firing，条件解除进入 resolved，告警状态持久化。

### 输出与解决的问题

输出是 Prometheus 文本指标、管理员状态 API 和结构化告警日志。它让“服务能响应但研究失败”“模型调用成功但用量未知”“缓存降级但业务仍可用”等情况可区分。

### 源码入口

- [OperationsMiddleware](../src/deepsearch_agent/web/operations.py)及[任务终态计量](../src/deepsearch_agent/application/session_runs.py)。
- [Metrics/Governance.snapshot](../src/deepsearch_agent/application/governance.py)：有界标签、窗口、p95 和条件。
- [告警持久化](../src/deepsearch_agent/adapters/persistence/governance.py)：`alerts` 状态转换。
- [治理 API 测试](../tests/test_governance_api.py)：HTTP 200/SSE error、身份与管理权限。

### 概念与失败示例

SSE 已发送 HTTP 200 后，供应商超时，流内发出 error；HTTP 200 仍然真实，但 research 应记 interrupted，费用可能 unknown。completed 也仅说明业务执行完成，答案正确需要独立质量标签。

若把 run_id 放入标签，每个请求建立新时间序列，监控存储会持续膨胀；定位单次问题应使用请求 ID 与受控明细查询。当前 Counter/Histogram 重启归零，额度、核对审计、告警状态持久化，不能把这两类数据的保留能力混淆。

默认失败率至少 5 条样本、阈值 25%；延迟默认 provider 30 秒、research 120 秒；未知滞留默认 300 秒。小样本 p95 不等于公网 SLA。当前提供指标/API/日志出口，没有部署 Prometheus、Alertmanager、Grafana 或外部消息通知。可控时钟测试验证了 pending/firing/resolved；高负载实验观测 pending 不代表已实测完整外部通知链路。

### 面试追问

- **HTTP 成功率高，用户仍失败，先看什么？** 研究终态、SSE error code、供应商结果与 unknown 积压，再关联具体 run。
- **为什么 pending？** 持续时间门槛降低短暂抖动导致的告警切换，但它不替代合理窗口与最小样本要求。
- **多个副本的 p95 可以平均吗？** 不能直接平均分位数；当前是单进程滚动样本，扩展需明确可聚合直方图与统一统计口径。

## 9. Redis、SQL RLS 与内存索引：按职责选择故障策略

### 输入

Embedding 请求、API 限流请求、某空间的业务数据库查询与知识库查询。它们访问不同存储，不能用“都存数据”推导相同的故障行为。

### 执行顺序

1. Redis 缓存键由模型/端点等 identity、凭据作用域 scope 与文本共同哈希；读取后验证向量维度、有限数值和归一化。
2. 合法命中直接返回；缺失/损坏调用真实适配器，成功后才写入缓存。Redis 故障进入短超时/冷却，缓存绕过，限流回退有界本地窗口。
3. PostgreSQL 模式下业务请求使用连接池；事务内 `set_config` 设置当前空间，查询显式带 workspace_id，复合键/外键与 FORCE RLS 共同隔离空间。
4. 应用角色必须是非 superuser 且无 BYPASSRLS；RLS 不替代 HTTP 成员与编辑权限检查。
5. PostgreSQL 不可用时业务与 ready 返回 503，live 仍表示进程存活；不回写旧 SQLite，避免两个业务事实源。
6. 检索仍读单 API 的内存索引并发布 JSON 快照。PG 和 Redis 的加入都不自动同步另一进程的内存对象，因此部署保持一个 API worker/副本。

### 输出与解决的问题

输出是缓存加速、降级保护和多层空间隔离。缓存故障可牺牲性能继续执行；业务事实库故障必须显式拒绝，防止读旧数据/分叉写入。

### 源码入口

- [RedisBackend/CachedEmbeddingClient/RequestLimiter](../src/deepsearch_agent/adapters/infrastructure.py)。
- [PostgreSQL 连接与事务 scope](../src/deepsearch_agent/adapters/persistence/postgres.py)。
- [表结构与 RLS](../src/deepsearch_agent/adapters/persistence/postgres_schema.py)。
- [数据库边界与部署说明](DATABASE.md)及[健康检查](../src/deepsearch_agent/web/operations.py)。

### 概念与失败示例

Redis 清空后应能从可信原件与模型重算缓存；它不是论文证据、任务结果或费用的唯一事实库。相反，PG 不可用时偷偷回到 SQLite，会导致恢复后新记录分属两个数据库，不能称为透明高可用。

SQL 加上 `WHERE workspace_id=?` 防正常路径串空间，复合外键防跨空间关联，RLS 在漏写条件时再提供约束；它不能阻止错误配置的 superuser 绕过。即使 SQL 全部正确，多 worker 各自加载 JSON 后也可能看见不同版本索引，这是另一层一致性问题。

### 面试追问

- **有 Redis 为什么仍不能直接加 Uvicorn workers？** 缓存与限流并未解决索引发布、文件写入、检查点所有权和资源计数共享。
- **如何扩展多副本？** 先定义共享检索/索引发布版本、任务所有权与共享治理，再引入持久队列和独立 worker；这些尚未实现。
- **readiness 与 liveness 为什么分开？** 依赖不可用不意味着进程必须重启；二者分别反映服务接纳能力与进程存活。

## 10. 五类记录：轨迹、检查点、工具账本、费用账本、人审真值

### 输入

同一次研究会产生请求、模型决策、工具观察、恢复状态、用量和答案质量评价。它们可通过身份关联，但保存目的与证据强度不同。

### 执行顺序

1. 图运行时保存检查点与待继续状态；恢复前校验语料范围、配置/模型/预算/上下文身份。
2. 每次受保护逻辑工具操作通过执行账本决定是否发送、重放或阻断，不能从轨迹文本推断“没执行”。
3. 实际供应商调用通过费用账本预占/结算；工具结果存在不代表 Token 已知，费用已确认不代表工具结果可用。
4. 独立轨迹记录角色请求、工具、证据与错误，支持定位与复盘；关闭或记录失败不应削弱账本的安全决策。
5. 质量流程冻结确切轨迹摘要，再由人审给 correct/incorrect/unknown、期望与证据；incorrect 还需要根因标签。模型内部 verified 不能自动充当人审正确标签。

### 输出与解决的问题

| 记录 | 回答的问题 | 不能代替谁 |
|---|---|---|
| 检查点 | 图执行到哪里、下一步继续什么 | 不能证明外部副作用未发生 |
| 工具执行账本 | 原逻辑操作是否有确认结果、能否再次发送 | 不能充当真实供应商账单或答案真值 |
| 费用账本 | 谁占了多少额度、实际用量是否确认 | 不能恢复丢失的工具文本结果 |
| 轨迹 | 决策、调用与证据如何形成，在哪失败 | 不能作为允许付费重试的事实依据 |
| 人审质量记录 | 在该轨迹及证据下答案是否正确、根因是什么 | 不能从程序 completed 或内部 verified 自动推导 |

### 源码入口

- [检查点序列化](../src/deepsearch_agent/adapters/persistence/checkpoints.py)与[恢复身份校验](../src/deepsearch_agent/application/session_runs.py)。
- [工具账本](../src/deepsearch_agent/adapters/persistence/tool_ledger.py)与[费用账本](../src/deepsearch_agent/adapters/persistence/governance.py)。
- [轨迹记录器](../src/deepsearch_tracing/recorder.py)与[执行结果分类](../src/deepsearch_tracing/outcomes.py)。
- [质量快照、人审与配对比较](../src/deepsearch_tracing/quality.py)。

### 概念与失败示例

“图没记住该调用”不等于“供应商没执行”；“轨迹标 internally_verified”不等于“人工确认正确”；“unknown 费用”不等于“费用为 0”。质量标签绑定 trace_sha256，防止换了轨迹还沿用旧标签；无人工标签时准确率/误暂缓率应保留 null，而不是用模型自评填满。

历史 Badcase 对照也保留原口径：[主技术报告的重复对照](INTERVIEW_TECHNICAL_REPORT_20261019.md)记录固定 20 题、v4/v5 各三次，120 次预测、60 个配对观测。v5 合计 Token **增加 5.06%**，成本下降比例区间 `[-18.78%, +8.29%]` 跨零，正确率分母为 57，`22/57 → 25/57`；不能据局部跳过核验宣称稳定降本。该历史实验、治理成本点与合成容量实验是三种不同口径，不合并成一个“性能提升百分比”。

### 面试追问

- **为什么不只保留一张日志表？** 恢复控制、额度事务和可选观测的可用性要求不同，混用会让日志故障改变业务安全语义。
- **备份只带 PG 够吗？** 不够，还需同一部署的文件卷、索引、工作记忆撤销标记、检查点、工具账本与费用控制库，按既有停写备份流程保持一致。
- **测试全绿能证明答案正确吗？** 只能证明测试覆盖的行为满足断言；真实任务质量还需固定语料、模型、预算与独立标签的评测。

## 11. 高并发：先限制接纳量，再减少相同读取的重复工作

### 输入

多个用户同时发起研究，其中一些请求需要相同模型、凭据作用域和文本的 Embedding。这里有两个独立问题：系统能同时承担多少工作，以及这些工作中有多少可以合并。第 6 节的资源租约解决前者；跨请求 singleflight 是后者的一种设计，目前未接入本项目。

### 执行顺序

1. 当前 `Governance.resource` 在一个进程共享锁内同时检查 global/workspace/user，全部允许后才增加三类计数，超限立即返回 503。默认全局研究 8、入库 4、供应商调用 8；入库租约覆盖队列等待与实际执行，结束、异常和队列取消均释放。
2. research、ingestion、provider 分别计数，形成资源隔离的基础。供应商很慢时，供应商池能够拒绝更多调用；它没有把研究、解析、内存和所有外部 MCP 都变成独立的硬资源隔离区。
3. 每个 RedisBackend 另有 16 个连接与 16 个在途操作上限，团队模式多空间运行环境的连接池会叠加，当前没有部署共享的 Redis I/O 总池。默认 socket timeout 为 0.3 秒，调用等待的墙钟上限为其两倍；失败后冷却 5 秒。等待超时会返回降级，但底层已开始的线程可能仍在运行，在途名额要等它真正结束才释放。
4. 当前 `CachedEmbeddingClient.embed` 只在一次输入内用 `dict.fromkeys` 去重。两个请求同时对同一个 key 未命中，仍可能分别进入供应商调用，供应商资源池只限制同时数量，不合并其内容。
5. 若以后引入 singleflight，应在授权和输入检查后，用明确的模型、参数、凭据作用域与文本身份定位一个共享 Future；首个调用者成为 leader，其余调用者有界等待同一结果。私有部署还可把 workspace 纳入合并键，这属于更保守的新策略；当前缓存 scope 是凭据摘要，并非 workspace ID。
6. leader 结束后清理共享状态；异常也要释放。一个 follower 取消或超时，不应取消所有用户正在共享的 leader，更不能立即抢占为第二个 leader。leader 必须有独立的调用超时与资源预算，不能把它的生存期绑在第一个 HTTP 连接上。

### 输出与解决的问题

现有输出是有界接纳、清楚的拒绝原因及异常后的资源释放；拟议的 singleflight 输出是多个请求复用一次正在执行的相同读取。前者是 admission/backpressure，后者是工作合并，两者需要共同使用。当前没有持久消息队列、自动排队重试或跨进程请求合并。

### 源码入口

- [Governance.resource](../src/deepsearch_agent/application/governance.py)与[资源配置](GOVERNANCE_CAPACITY.md)：三维原子检查、资源计量和 fail-fast 拒绝。
- [GovernedExecutor](../src/deepsearch_agent/adapters/governed_calls.py)：入库队列持有租约及取消释放。
- [RedisBackend/CachedEmbeddingClient](../src/deepsearch_agent/adapters/infrastructure.py)：Redis 有界等待与批内去重。
- [一次性问答入口](../src/deepsearch_agent/application/library_service.py)：先在锁内创建快照，再申请 research 租约，快照成本尚未被该准入覆盖。

### 概念与失败示例

32 个请求同时读取一个刚过期的热点向量，即使每个请求自身只含一份文本，也可能发生多次回源。给 Redis 加一个更大的连接池不会消除重复计算，还可能更快耗尽付费额度。应先测热点重复调用与拒绝情况，再评估请求合并、TTL 抖动和容量配置。

singleflight 只协调“当前是否已有相同计算”，它的内存表随进程退出丢失；工具账本协调“原逻辑操作是否已有确认结果、是否允许重发”。两个任务的同参数付费步骤可能本来就是不同业务操作，不能只按参数合并。对已经发送却结果未知的付费/写入操作，取消 leader、锁过期或 follower 超时都不能证明远端未执行，仍须保持第 1–2 节的 unknown 策略。

### 面试追问

- **连接池、限流与并发准入有何不同？** 连接池限制连接资源，时间窗口限流限制请求频率，并发准入限制同时占用的工作；它们的边界与计数周期不同。
- **为什么不让请求一直等信号量？** 无限等待会把过载变成内存积压和长尾；若业务需要排队，还需有界队列、超时、取消、恢复与公平性设计。
- **singleflight 能保证只收费一次吗？** 不能。它不覆盖进程重启和远端结果未知，也不提供供应商幂等协议。
- **有 503 能否称“支持高并发”？** 可说已实现并发保护；是否满足业务容量要看固定负载下的成功吞吐量、延迟、拒绝率和资源使用，不能只统计所有 HTTP 返回数。

## 12. 高可用：把恢复能力、持续服务与数据保留分别验收

### 输入

Redis 不可达、API 进程退出、业务数据库故障，或整台机器及其磁盘丢失。故障位置不同，恢复方法与可接受的数据损失也不同，不能用一次进程退出测试覆盖全部情况。

### 执行顺序

1. 先定义服务目标与故障范围：RTO 是允许业务中断的恢复时间目标，RPO 是允许恢复后丢失的数据时间范围目标。它们是目标；只有演练覆盖故障发现、切换/重启、数据恢复及业务重新可用，才能报告该场景实测值。本项目未给出生产 RTO/RPO 承诺。
2. 当前 Redis 故障绕过缓存并回退本地固定窗口；PostgreSQL 故障使依赖该事实库的业务与 readiness 拒绝接纳，不能改写回旧 SQLite。缓存可丢弃，只表示允许重新计算，不表示可以丢弃费用或工具账本。
3. 当前 API 退出后依赖持久检查点、工具账本、费用账本、原文和索引快照恢复。已有退出实验支持单进程故障窗口中的恢复行为；启动、检测和接流量仍需要部署系统处理，不等于两个服务副本自动故障切换。
4. 若扩展 API 多副本，必须先设计共享索引及发布版本、任务归属、文件写入协调、共享准入和账本访问边界。增加副本数不会自动让进程内索引、计数器和 Future 表一致。
5. Redis Sentinel 提供非分片 Redis 的监测与自动故障切换；Redis Cluster 提供按槽分片及相关故障切换能力。二者都需要相应拓扑与客户端发现/重连配置，不能靠现有单地址 `Redis.from_url` 自动获得。本项目尚未部署、演练这些拓扑。[Redis Sentinel 文档](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)、[Redis Cluster 规范](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)。
6. 故障演练分别检查服务恢复、已确认数据保留、未知操作阻断和授权正确性。Redis 复制与故障切换存在其一致性边界，不能由“有主从”推导所有确认写都具备零 RPO；费用和执行账本应按其自身持久性与备份方案验收。[Redis Sentinel 的复制边界](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)。

### 输出与解决的问题

输出应是一张明确的故障合同：哪些依赖故障允许降级，哪些必须拒绝，丢失什么可以重建，哪些记录必须恢复，恢复后怎样确认可以重新接纳。高可用要求在指定故障范围内维持或恢复服务；“任务能够从检查点继续”只覆盖其中一个部分。

### 源码入口

- [Redis 降级](../src/deepsearch_agent/adapters/infrastructure.py)与[健康检查](../src/deepsearch_agent/web/operations.py)。
- [IndexService.publish/restore](../src/deepsearch_agent/knowledge/index_service.py)：候选保存成功后切换内存；恢复异常时保护写入。
- [会话恢复](../src/deepsearch_agent/application/session_runs.py)、[工具执行器](../src/deepsearch_agent/application/tool_execution.py)与[费用治理](../src/deepsearch_agent/application/governance.py)。
- [停写备份与恢复](BACKEND_RECOVERY_CONSISTENCY.md)、[已有验证边界](GOVERNANCE_VALIDATION_20261020.md)。

### 概念与失败示例

如果只备份业务 PostgreSQL，机器损坏后仍可能丢失本地 `governance.sqlite3`、工具账本、检查点、原文、工作记忆撤销代际与索引快照。服务即使启动，也不能安全推断某次付费请求“没有记录，所以没执行”。恢复方案必须覆盖这些事实源及其一致性边界。

再看分布式锁：A 获得 300ms 租约后发起耗时 2 秒的调用，租约到期，B 再获得锁；A 与 B 可能同时对供应商产生效果。随机 owner token 与比较后释放能避免 A 删除 B 的锁，但不能撤销 A 已发出的调用。需要下游接受的 fencing 或稳定幂等协议、持久状态与未知结果核对；分布式锁本身不是 exactly-once。[Redis 分布式锁文档](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)。

### 面试追问

- **Redis 挂了还能回答，就是高可用吗？** 这是缓存依赖的降级行为；API 主机、业务数据库、索引和账本故障仍需单独定义与演练。
- **Sentinel 和 Cluster 怎么选？** 先区分需要非分片故障切换，还是容量分片与相应可用性；还要评估客户端、复制丢失窗口和运维成本，本项目没有完成任一拓扑验收。
- **为什么 RPO 不能填 0？** 需要明确确认写的持久边界、复制方式、备份时间点及故障模型；现有进程退出测试无法证明机器或磁盘故障零丢失。
- **缓存丢失只影响性能吗？** 还会增加回源、Token 费用与拒绝概率，因此需容量与额度保护；它仍不能改变付费 unknown 的重试语义。

## 13. 高性能：先拆开成本，再判断优化能改变哪一段

### 输入

固定语料、模型身份、请求集合和预算下的逐请求记录：接纳/拒绝、快照构造、缓存命中/未命中、检索/重排、供应商等待、核验、持久化耗时，以及 CPU、RSS 和堆内存。只给一个平均响应时间无法定位慢点。

### 执行顺序

1. 先固定统计对象与时间边界：成功吞吐量为完成数除以同一测量区间；同时保留拒绝数、失败数和全请求/成功请求延迟。快速拒绝很多请求可能降低全请求 p95，但不意味着用户完成更多研究。
2. 关联请求总时延与各阶段耗时，比较命中和未命中样本。当前治理指标已有供应商、研究、研究/入库阶段、缓存事件与资源量；Redis 逐 key 耗时、锁等待及快照单独耗时仍需专门剖析，不能当作已有完整分布式追踪。
3. 对缓存路径，令一次输入中去重后文本数为 K、未命中数为 M：当前逐 key 执行 K 次 GET、成功回源后 M 次 SET，并将 M 个文本一次传给 delegate；不是逐文本调用模型。若 K 较大且 RTT 可观，才考虑 MGET 或 pipeline 批量访问，并测量序列化、网络和线程创建成本。
4. 批量读必须恢复原输入顺序与重复项，逐项验证维度、finite 与归一化；批量写必须保持每个值的 TTL 和失败降级语义。pipeline 减少网络往返，不会自动去重供应商请求，也不是读—算—写的原子事务；本项目尚未实现 MGET/pipeline/L1。[Redis pipelining](https://redis.io/docs/latest/develop/using-commands/pipelining/)、[MGET 命令](https://redis.io/docs/latest/commands/mget/)。
5. 区分 I/O 等待和 CPU 工作。线程可以并行等待供应商/磁盘，但不能据此声称纯 Python 计算获得同等多核加速；向量库可能使用原生计算，需实测。增加进程还会复制内存状态，并触发现有单进程所有权边界。
6. 对索引成本分开测量：问答 snapshot 是服务浅拷贝加 `restrict_sources` 列表视图，重置该视图的 BM25/ANN 缓存；入库发布则在索引锁内 deepcopy 候选、合并、保存后切换。它们都可能随语料增长产生开销，但不能把两者都描述成“每次问答深拷贝全部向量”，也没有现成实测数字证明谁是主瓶颈。

### 输出与解决的问题

输出应是一个可验证的假设，例如“热点向量未命中造成供应商重复调用”“大量小 GET 的 RTT 占比高”“锁内发布阻塞快照”，以及对应证据与一次有限优化。现有 144 个合成请求中 68 完成、76 保护拒绝的报告证明保护路径可执行，不能推成生产高 QPS、真实模型吞吐量或高可用承诺。

### 源码入口

- [缓存访问与校验](../src/deepsearch_agent/adapters/infrastructure.py)、[真实调用计量边界](../src/deepsearch_agent/adapters/governed_calls.py)。
- [指标实现](../src/deepsearch_agent/application/governance.py)与[容量脚本](../scripts/benchmark_governance_capacity.py)：逐请求、成功吞吐量、延迟分位数和资源使用。
- [快照入口](../src/deepsearch_agent/application/library_service.py)、[AuditedAnswerService.snapshot](../src/deepsearch_agent/application/service_base.py)、[restrict_sources](../src/deepsearch_agent/knowledge/knowledge_base.py)。
- [入库候选深拷贝](../src/deepsearch_agent/workspace/documents.py)与[IndexService.publish](../src/deepsearch_agent/knowledge/index_service.py)。

### 概念与失败示例

设同一请求类型的命中比例为 h，命中、未命中延迟分布分别是 `F_hit(t)`、`F_miss(t)`，混合分布为 `F(t)=h·F_hit(t)+(1-h)·F_miss(t)`。在均值存在且统计边界一致时，可写 `E[T]=h·E[T_hit]+(1-h)·E[T_miss]`；**不能用这个均值公式直接推 p95，也不能把两个 p95 加权平均**。一个请求含多个文本时，还要统计“整请求是否需要回源”，不能直接拿逐 key hit rate 代替 h。

例如 90 个请求耗时 1ms、10 个请求耗时 1000ms，均值为 100.9ms，按 nearest-rank 算 p95 是 1000ms。若变为 96 个 1ms、4 个 1000ms，p95 变为 1ms，p99 仍为 1000ms。各阶段 p95 相加也不等于总时延 p95，尤其存在并行步骤与相关长尾时。第 6 节的 `平均在途量≈到达率×平均停留时间` 同样要求稳定系统与一致测量边界，不能把一次瞬时峰值当作容量保证。

### 面试追问

- **缓存命中率高，p95 为什么仍差？** 少量未命中、热点失效、供应商长尾、锁等待或冷启动可能落在尾部；先看分段和分组分布。
- **pipeline 就是高性能答案吗？** 只有逐命令 RTT 是主要成本时才可能有效；还需限制批量大小与内存，保留顺序、校验和超时降级。
- **索引越大越应该加 worker 吗？** 多进程可能放大快照、索引与内存成本；先测当前路径，再解决共享检索和发布一致性。
- **压测报告中要保留哪些分母？** 总提交、接纳、成功完成、拒绝、失败和测量时长；成功吞吐量与拒绝吞吐量分开。

## 14. Redis 缓存穿透、击穿、雪崩：按故障模式选择保护

### 输入

一批重复或新文本、过期缓存、无效业务 ID，或者不可用的 Redis。首先明确回源对象：Embedding 对任意合法新文本通常都有可计算结果，“缓存里不存在”本身不代表非法或业务不存在。

### 执行顺序

1. 先做输入和授权校验，再查缓存。当前 Embedding 包装器拒绝空白/非字符串文本；key 是 `identity + scope + text` 的摘要，装配时 scope 为 API 凭据摘要。同凭据、同模型身份的不同空间可共享同文本向量；这不是文献授权缓存，向量命中不能授予原文访问权。
2. 命中后仍验证长度、有限数值和归一化。错误 JSON、NaN、维度变化及坏向量被当作 miss，成功生成且通过验证后才写回；供应商报错不会写成一个“有效空向量”。
3. 识别故障模式，再选择机制；下表的新增机制均为设计方向，当前没有 negative cache、Bloom、跨请求 singleflight、TTL jitter 或 stale-while-revalidate。

| 模式 | 具体输入与压力 | 可选保护及适用条件 |
|---|---|---|
| 穿透 | 反复请求权威业务库确认不存在的 ID，每次都查事实源 | 输入校验、授权、限流；可短 TTL 缓存“确定不存在”，并绑定空间/模型/数据版本等必要身份，新增或授权变化时失效 |
| 击穿 | 单个热点 key 过期，许多并发请求都回源 | 同身份请求合并、有界回源并发、热点预热；允许旧值时可后台刷新，但须遵守模型身份与授权边界 |
| 雪崩 | 大量 key 同期过期，或 Redis 整体故障使大范围回源 | TTL 抖动分散过期、分批预热、容量/额度保护与降级；Redis 故障还需独立超时、冷却和依赖告警 |

4. negative cache 只能保存有语义的确定否定，例如在某授权范围与数据版本下 ID 确定不存在；使用短 TTL，并考虑新增数据后的失效。网络超时、模型错误、未授权与“还没算过向量”不能统统写成“不存在”，否则会把临时故障或合法新输入长期屏蔽。
5. Bloom filter 用于维护一个已知集合的近似成员判断。它可能把不存在元素判断为可能存在；只有集合完整、更新及时且作用域正确时，否定结果才能用于前置过滤。任意新 Embedding 文本没有预先枚举的合法全集，不能用 Bloom 阴性拒绝；集合过期漏加新业务 ID 也会错拒合法请求。[Redis Bloom filter 文档](https://redis.io/docs/latest/develop/data-types/probabilistic/bloom-filter/)。
6. 若允许失效读取，要明确“旧多久、哪个版本、何时必须回源”。缓存原文、检索结果或答案时，资料撤权与模型/索引身份变化应使相关结果失效；缓存 scope 和 TTL 不能替代授权。失效读只能放宽产品明确允许的时效，不得绕过权限或费用未知核对。

### 输出与解决的问题

输出是有针对性的回源控制，而非一个“用了 Redis”的笼统结论。当前已经有命中校验、批内去重、固定 TTL、Redis 短超时/冷却、限流回退与治理资源/额度保护；这些提供基础保护，但没有消除热点同时回源和大范围过期的问题。详细设计与官方资料见[高并发、高可用、高性能与 Redis 专题](CONCURRENCY_AVAILABILITY_PERFORMANCE_REDIS.md)。

### 源码入口

- [CachedEmbeddingClient.key/valid/embed](../src/deepsearch_agent/adapters/infrastructure.py)：身份、命中验证、批内去重与固定 `ex=cache_ttl`。
- [适配器装配](../src/deepsearch_agent/application/bootstrap.py)：模型身份、维度和凭据摘要 scope。
- [RequestLimiter/RedisBackend](../src/deepsearch_agent/adapters/infrastructure.py)：Lua 原子 INCR/首次 EXPIRE、最多 10000 活动来源的本地固定窗口与冷却。
- [缓存与故障测试](../tests/test_infrastructure.py)、[资源与额度保护](../src/deepsearch_agent/application/governance.py)。

### 概念与失败示例

如果给同一批缓存都设置 86400 秒 TTL，它们在相近时间写入就可能相近时间失效；随机抖动可以分散到期，但无法解决 Redis 完全不可用，也无法保证单个热点只有一个回源者。当前项目使用固定 TTL，不能在面试中说“已经用随机过期防雪崩”。

再如 Redis 故障后，两个 API 进程各自启用本地窗口，各自允许 N 次请求，并不会自动得到部署级 N 次限额。当前部署只支持单 API；本地回退强调存活与有界保护，不能被解读为多副本时严格共享的全局限流。即使 Lua 原子 INCR/EXPIRE 正确，也只覆盖它所在 Redis 的计数操作，不覆盖本地副本、远端付费调用和跨存储事务。

### 面试追问

- **新文本没有缓存就是穿透吗？** 不是；它通常是合法冷 miss。应通过输入长度、授权、配额与并发控制成本，不能把未知文本当成不存在。
- **为什么负缓存 TTL 要短？** 否定事实可能因新增数据而改变，还必须有版本/失效机制；短 TTL 只是降低陈旧窗口，不能保证正确授权。
- **Bloom 能替代业务查询吗？** 不能；阳性仍可能不存在，且过滤前提是集合覆盖、更新和作用域正确。
- **热点锁过期就重试可以吗？** 对纯读取也要限制重复回源；对付费或写入未知，锁过期无法授予重发权限，仍看持久账本与供应商合同。

## 可执行练习：先用本地合成数据验证机制

以下命令在项目根目录、PowerShell 7 与已有虚拟环境执行。只使用测试与临时合成数据，不需要真实供应商密钥或管理员账号。先设置项目内临时目录；`$practiceId` 每次新生成，避免覆盖历史输出。测试本身仍可能使用模拟网关/故障注入，不表示真实付费服务验收。

```powershell
$env:PYTHONPATH = 'src'
$env:PYTHONUTF8 = '1'
$practiceId = [guid]::NewGuid().ToString('N')
$practiceTemp = Join-Path (Get-Location) ('tmp/backend-knowledge-' + $practiceId)
New-Item -ItemType Directory -Force -Path $practiceTemp | Out-Null
$env:TEMP = $practiceTemp
$env:TMP = $practiceTemp
```

### 练习 A：付费 unknown 不会被第二次调用

此处用一个内存计数器模拟“供应商已发生效果、随后结果丢失”，SQLite 账本放在临时目录。运行后应输出 `unknown 1`。它演示执行器的默认策略，不测试真实供应商幂等。

```powershell
@'
from pathlib import Path
from tempfile import TemporaryDirectory
from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger
from deepsearch_agent.application.tool_execution import ToolExecutor
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked, ToolKind

with TemporaryDirectory() as folder:
    ledger = SQLiteToolLedger(Path(folder) / 'ledger.sqlite3')
    ledger.begin_run('demo-run')
    executor = ToolExecutor(ledger, 'demo-run')
    calls = []
    def invoke(operation_id):
        calls.append(operation_id)
        raise TimeoutError('synthetic result lost after effect')
    for _ in range(2):
        try:
            executor.execute('round-1/tool-1', 'demo-paid', ToolKind.PAID,
                             {'query': 'synthetic'}, invoke)
        except ToolExecutionBlocked as error:
            assert error.code == 'tool_outcome_unknown'
    row = ledger.list_run('demo-run')[0]
    assert row['status'] == 'unknown' and len(calls) == 1
    print(row['status'], len(calls))
'@ | .venv/Scripts/python.exe -
```

### 练习 B：按照知识点选择对应回归

每条命令的临时目录不同，可独立运行。预期结果应以该次真实输出为准，不把本文已有回归数字复制为本次结果。

```powershell
# 1–2：操作身份、并发认领、owner 隔离与崩溃窗口。
.venv/Scripts/python.exe -m pytest tests/test_tool_execution_ledger.py -q --basetemp ($practiceTemp + '/ledger')

# 3–4：scope 撤销、历史读取恢复、入库产物与发布后修复。
.venv/Scripts/python.exe -m pytest tests/test_backend_recovery.py tests/test_ingestion_recovery.py -q --basetemp ($practiceTemp + '/recovery')

# 5–8：双作用域额度、资源释放、线程归属、SSE 业务结果、告警。
.venv/Scripts/python.exe -m pytest tests/test_governance.py tests/test_governance_api.py -q --basetemp ($practiceTemp + '/governance')

# 9–10：缓存/数据库边界、轨迹与质量流程。未配置真实服务的用例可跳过。
.venv/Scripts/python.exe -m pytest tests/test_infrastructure.py tests/test_database.py tests/test_quality_loop.py -q --basetemp ($practiceTemp + '/storage-quality')
```

需要观察真实进程退出与容量保护时，可执行以下本地合成脚本；它们需要较长时间与本机资源，输出依然不是生产验收：

```powershell
.venv/Scripts/python.exe scripts/verify_governance_recovery.py --output ('output/knowledge-governance-' + $practiceId)
.venv/Scripts/python.exe scripts/verify_ingestion_recovery.py --output ('output/knowledge-ingestion-' + $practiceId)
.venv/Scripts/python.exe scripts/benchmark_governance_capacity.py `
  --output ('output/knowledge-capacity-' + $practiceId) `
  --documents 100,1000 --concurrency 1,4,12 --requests 12 --repeats 2 --dimensions 1024
```

### 练习 C：在运行中的本地服务只读查看治理状态

下例要求本地无账号模式服务已在回环地址运行。团队部署使用现有认证 Cookie；`status/alerts/metrics` 需要部署管理员，空间级 usage 还需空间成员授权。不要把登录 Cookie 或真实凭据写入文档、脚本仓库或终端输出。

```powershell
$practiceBase = 'http://127.0.0.1:8000'
Invoke-RestMethod -Uri ($practiceBase + '/api/governance/usage')
Invoke-RestMethod -Uri ($practiceBase + '/api/governance/status')
Invoke-RestMethod -Uri ($practiceBase + '/api/governance/alerts')
```

真实核对/额度修改属于管理写入，须在具备相应权限且已核实实际记录后按[治理操作说明](GOVERNANCE_CAPACITY.md)执行。工具核对需要原空间 owner/editor，费用核对与部署额度修改需要管理员。证明引用是审计线索，不是系统自动验证过的证据。

### 练习 D：标准库演示热点读取 singleflight

对应第 11、14 节。此例只验证**同一进程里正在执行的相同读取可以合并、follower 等待有超时、leader 失败后能清理**。它使用同步线程、事件和合成返回值，没有 Redis、网络、文件或付费调用；等待顺序由事件确定，不以“睡一会儿大概会重叠”为依据。

`timeout` 只限制 follower 等待，不会强制停止 leader。示例 loader 自己使用有限等待；生产 loader 还需真实下游超时、取消策略、共享状态容量、资源准入与监控。此代码没有持久状态和跨进程协调，不能复制成付费 unknown 自动重试方案。

```powershell
@'
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock

class ReadFlights:
    def __init__(self):
        self.lock = Lock()
        self.flights = {}

    def run(self, key, load, timeout=1.0, joined=None):
        with self.lock:
            future = self.flights.get(key)
            leader = future is None
            if leader:
                future = Future()
                self.flights[key] = future
        if not leader:
            if joined is not None:
                joined.set()
            return future.result(timeout=timeout)
        try:
            value = load()
            future.set_result(value)
            return value
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                if self.flights.get(key) is future:
                    del self.flights[key]

flights = ReadFlights()
entered, release = Event(), Event()
calls = []
value = (0.6, 0.8)  # Immutable result shared by this teaching example.

def load():
    calls.append('read')  # Only the leader is allowed to call load.
    entered.set()
    if not release.wait(timeout=5):
        raise TimeoutError('synthetic loader budget exceeded')
    return value

with ThreadPoolExecutor(max_workers=3) as pool:
    leader = pool.submit(flights.run, 'model/scope/hot-text', load)
    joined = [Event(), Event()]
    followers = []
    try:
        assert entered.wait(timeout=2)
        followers = [pool.submit(flights.run, 'model/scope/hot-text', load,
                                 timeout=3, joined=event) for event in joined]
        assert all(event.wait(timeout=2) for event in joined)
        try:
            flights.run('model/scope/hot-text', load, timeout=0.01)
        except TimeoutError:
            pass  # This follower times out; the leader is still running.
        else:
            raise AssertionError('follower must have bounded waiting')
    finally:
        release.set()
    assert leader.result(timeout=2) == value
    assert all(future.result(timeout=2) == value for future in followers)
assert len(calls) == 1

def fail():
    raise RuntimeError('synthetic read failure')

try:
    flights.run('broken-read', fail)
except RuntimeError:
    pass
else:
    raise AssertionError('leader failure must propagate')
assert flights.run('broken-read', lambda: value) == value
assert flights.flights == {}
print('merged_reads=1 follower_timeout=ok failure_release=ok')
'@ | .venv/Scripts/python.exe -
```

预期输出 `merged_reads=1 follower_timeout=ok failure_release=ok`。移除异常路径的清理后，最后一次 `broken-read` 会重放旧异常；移除共享 Future 查找后，合并或同步断言应失败，并可能触发多次 loader。不要把同样的“失败后再调用”练习用于真实付费或写入操作，后者必须遵守第 1–2 节账本合同。

### 练习 E：给故障分类，再确定能否降级与重试

对应第 12、14 节。先独立填写“模式、现有行为、仍需验证的能力”，再与下表核对，不连接真实 Redis，也不修改额度或账本。

| 场景 | 应得到的判断 | 不应作出的推断 |
|---|---|---|
| 一个热点向量过期，32 个并发请求都查不到 | 击穿风险；批内去重不合并跨请求，供应商池与额度先限制成本 | 已有 singleflight、只会回源一次 |
| Redis 整体不可达 | 大范围回源风险；短超时/冷却、缓存绕过、本地限流回退 | Sentinel 已切换；服务已通过整机故障验收 |
| 用户提交一个从未出现的合法新文本 | 冷 miss；输入、权限与配额检查后可计算 | Bloom 阴性足以拒绝，或该文本必然是穿透攻击 |
| 文献 ID 确定不存在，稍后管理员新建该文献 | 可讨论短 TTL 负缓存，但要考虑新增失效与授权范围 | 否定结果可以永久缓存或跨空间共享 |
| 工具 dispatched 后进程退出，Redis 锁随后过期 | 原逻辑付费/写入结果 unknown，等待可信核对或供应商协议 | 锁已过期，因此远端未执行、可以换新操作 ID |
| API 磁盘损坏，仅恢复业务 PG | 需核查本地账本、检查点、原件和撤销标记是否完整 | 业务表恢复即代表费用、执行事实和资料权限都恢复 |

进一步为“进程退出”和“整机损坏”分别画出检测、恢复数据、启动、恢复任务、允许接纳的时间线；指出 RTO 的业务终点和 RPO 的数据边界。没有演练时间与确认写保留证据时，填写“未实测”，不要从合成容量数字推算。

### 练习 F：用确定样本观察均值、p95 与 p99

对应第 13 节。单位都是毫秒，采用 `ceil(p·N)` 的 nearest-rank 定义；这是统计教学，不是本项目性能测试。这个定义也不等于监控直方图的分桶插值算法。

```powershell
@'
from math import ceil
from statistics import mean

def nearest_rank(values, p):
    ordered = sorted(values)
    return ordered[ceil(p * len(ordered)) - 1]

for hits in (90, 96):
    samples = [1] * hits + [1000] * (100 - hits)
    p95 = nearest_rank(samples, 0.95)
    p99 = nearest_rank(samples, 0.99)
    assert p95 == (1000 if hits == 90 else 1)
    assert p99 == 1000
    print(f'hit={hits}% mean={mean(samples):.2f} p95={p95} p99={p99}')
'@ | .venv/Scripts/python.exe -
```

预期两行分别为 `hit=90% mean=100.90 p95=1000 p99=1000`、`hit=96% mean=40.96 p95=1 p99=1000`。解释为什么 p95 显著下降时最慢的 4% 请求仍然存在，再说明为什么生产中需要同时看成功吞吐量、拒绝率、命中/未命中分布与资源峰值。

希望结合项目实现继续练习时，可复用练习 B 的 `tests/test_governance.py` 与 `tests/test_infrastructure.py` 入口。后者带真实 Redis 的集成用例只在设置 `TEST_REDIS_URL` 时运行；本次教学不要求开启它，也不新增一轮容量评测。检查测试输出时，应区分 FakeRedis、未监听端口故障测试、真实 Redis 服务三种证据。

## 面试串讲练习

用一个“用户发起研究 → 子 Agent 调用付费 OCR → 调用后进程退出 → 同事恢复”的例子串联：认证上下文确定归属；多维资源准入保护容量；费用事务同时预占 user/workspace；工具槽位固定身份与请求摘要；结果丢失转 unknown；检查点只负责图恢复；可信核对后重放原工具结果；费用仍可独立待核对；SSE/业务终态分别计量；最终答案交由独立质量评测。把“已实现的保护”“仍需人工核实的未知”和“计划扩展的能力”分别说清楚。
