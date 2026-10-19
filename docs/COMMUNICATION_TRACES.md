# 父子 Agent 通信轨迹与 badcase 分组

本功能复用现有可选 SQLite TraceStore，在同一条逐事件提交、带哈希链的审计流中关联父子 Agent 交接。结束时持久化 `run.classified`，区分成功、失败和未知；导出时生成三个独立目录。轨迹不进入模型提示词，也不自动生成正确率、失真率或训练标签。

## 开启记录

在启动后端的同一个 PowerShell 中设置：

```powershell
$env:RAG_TRACE_DB = Join-Path (Get-Location) 'data\communication-traces\runs.sqlite3'
.\.venv\Scripts\python.exe -m uvicorn deepsearch_agent.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers --log-config src/deepsearch_agent/logging.json
```

`RAG_TRACE_DB` 是进程环境变量，当前不要仅写进 `.env` 并期待它生效。已运行的服务需要带此变量重启。真实问答统一记录父子交接；演示及显式离线基线只记录其实际产生的运行事件，不伪造父子交接。独立代码调用可以使用 `AnswerService(..., run_recorder=RAGTraceRecorder(path))`，测试或嵌入式 `create_app` 可以传 `trace_path=path`。

关闭后续新进程的记录：

```powershell
Remove-Item Env:RAG_TRACE_DB -ErrorAction SilentlyContinue
```

不设置时保持关闭。记录启动、写入或结束失败不打断问答；已经成功提交的事件保留。写入出错的运行标记 `recording_error`；进程直接中断时可能留下 `running`，两者均进入未知组，不算成功。

## 成功与失败的定义

| 分组 | 判断依据 | 归因边界 |
|---|---|---|
| `success` | 流程完成、`rag.outcome=verified` 且最终答案已记录 | 仅代表通过内部核验，不代表人工确认正确 |
| `failure` | 运行异常、用户取消、结构校验失败，或正常返回但答案不完整/未通过核验 | 记录观察到的失败阶段和停止原因，根因留待审核 |
| `unknown` | 轨迹写入失败、未结束、没有内部核验结果，或最终答案记录缺失 | 不补成成功，也不推断成模型错误 |

每个分类保留 `outcome`、`failure_stage`、`reason` 和 `classification_source`。`answer_correctness_label`、`root_cause_label` 始终默认为 `null`。用户取消属于独立 outcome，不应直接混入模型质量 badcase。内部核验失败与真实答案错误也应分别统计。

模型服务异常额外保存已有网关提供的安全诊断：错误码、HTTP 状态、可重试标志、结束原因和已知 Token 用量；缺失用量保持 `null`，不补为零。不保存原始异常消息或服务端响应体。

原始事件保持一条完整审计链，避免成功/失败在结束前未知导致事件散落。分组以持久化的终态事件记录；只读导出会根据原始事实重新生成分类，并兼容没有新分类事件的旧轨迹。遇到哈希链损坏时拒绝导出，不跳过校验。

## 已记录的数据

| 事件 | 用于后续评估的数据 |
|---|---|
| `communication.task_dispatched` | 自包含任务、父流程背景、缺口、已有证据 ID；关联触发分派的父调用 |
| `communication.model_input` | 角色、结构化输入、调用 ID、隔离标志和提示词 Token 估算；原始文本提示词由对应 `rag.request` 保存 |
| `communication.tool_observation` | 工具返回的接收内容、进入观察缓冲前的裁剪版本、工具调用 ID、可信度和本地证据快照 |
| `communication.child_raw_result` | 证据 ID 校验前的候选结论、未解决项、停止原因和规划步数 |
| `communication.evidence_snapshot` | 请求内可信工具已接纳的原文、来源、页码和稳定 ID |
| `subagent.result` | 校验后的结构化结果，包括被拒绝引用对应的未解决项 |
| `communication.parent_delivery` | 裁剪前后结果、整项省略的任务 ID、裁剪算法版本和 Token 估算 |
| `communication.parent_decision` | 模型建议与程序守卫最终采用的动作，以及停止原因；预算守卫也有独立记录 |
| `communication.task_failed` | 失败任务 ID、异常类型；不保存原始异常文本 |
| `rag.outcome` / `answer.final` | 内部核验结果、支持关系、未解决项和最终答案 |
| `run.classified` | 本次运行的成功/失败/未知分组与可观察失败阶段 |

顶层审计事件有 `run_id`、序号、UTC 时间与内容哈希。子任务事件附带 `task_id`、`parent_call_id`、研究轮次和业务 `research_run_id`；父输入与决策通过 `parent_delivery_id` / `delivery_id` 关联。请求级 ContextVar 会穿过异步子图和工具线程，退出时恢复，避免并发串号。

父 Agent 的完整对话背景只进入审计事件，不会因此注入子 Agent；详细工具观察也不会注入父 Agent。模型输入记录的位置为 `workflow_before_provider_adapter`：父 Supervisor 的结构化交接数据在此已完成裁剪；视觉适配器和外部 MCP 可能在此前/此后执行各自的裁剪。图片仅保存来源与证据引用，不导出图像 base64，因此并非完整网络报文回放。

Token 字段为本地估算，不充当提供商计费量。原始模型输出、核验结果和人工评测标签保持分离。证据快照仅覆盖本次请求已接纳的材料，不是完整语料备份；语料指纹用于绑定所选索引的片段身份。

## 查看和按组导出

```powershell
.\.venv\Scripts\python.exe -m deepsearch_agent.communication_traces inspect --db data/communication-traces/runs.sqlite3
.\.venv\Scripts\python.exe -m deepsearch_agent.communication_traces inspect --db data/communication-traces/runs.sqlite3 --group failure
.\.venv\Scripts\python.exe -m deepsearch_agent.communication_traces export-split --db data/communication-traces/runs.sqlite3 --output output/communication-review-001
```

导出目录必须不存在，已有导出或人工记录不会覆盖：

```text
communication-review-001/
  success/runs.jsonl       # 成功组完整运行轨迹
  success/handoffs.jsonl   # 每行一个父子交接
  failure/runs.jsonl       # 包括尚未分派子任务就失败的运行
  failure/handoffs.jsonl
  unknown/runs.jsonl
  unknown/handoffs.jsonl
  manifest.json           # 各文件行数与 SHA256
```

单独导出失败交接：

```powershell
.\.venv\Scripts\python.exe -m deepsearch_agent.communication_traces export --db data/communication-traces/runs.sqlite3 --group failure --output output/badcases-001.jsonl
```

支持 `--run-id` 和 `--task-id` 过滤。`--task-id` 只过滤 handoffs 文件，完整 runs 文件仍保存选定运行，以免上下文被截断。读取源数据库使用 SQLite 只读连接。

交接 JSONL 保留裁剪前结果、父输入实际包含的结果、子 Agent 的输入与工具观察、证据快照、父决策、最终答案及关联原始事件。`stage_presence` 显式标记各阶段是否观测到；只有输入尝试而没有模型响应时，不标记为父 Agent 已完成处理。恢复后的新记录用业务 `research_run_id` 和 `resume` 关联前次运行；前次任务结果可能从检查点带入，导出不会伪造已缺失的分派/工具事件。

## 离线示例和测试

下面运行真实应用图与本地工具，但模型输出和文献均是合成 fixture，不访问云端：

```powershell
.\.venv\Scripts\python.exe examples/record_communication_demo.py --output output/communication-demo-001
.\.venv\Scripts\python.exe -m pytest tests/test_communication_traces.py tests/test_policy_pipeline.py tests/test_subagent_boundary.py -q
```

示例生成成功、核验拒绝、模型异常和未结束四种运行。它用来检查记录与分组行为，不用于报告模型性能。

记录内容可能包含问题、对话历史和文献原文，保存在项目现有忽略的 `data/`、`output/` 目录；不会自动上传或并入训练数据。后续统计语义失真率仍需要独立审核的信息原子和支持关系标签，本次仅提供可追溯的观测材料。

2026-10-18 验证记录：完整回归 `769 passed, 3 skipped`。离线示例生成 1 个内部核验成功、2 个失败（核验拒绝/模型连接异常）和 1 个未结束运行，并验证了所有导出文件的 SHA256、行数与原始事件哈希链。该示例路径为 `output/communication-demo-20261018-02/`，全部为合成 fixture。恢复测试确认同一业务运行跨两次记录仍能关联同一 task_id，并保留恢复段缺失的子任务执行信息为未知。

实现现独立位于 `deepsearch_tracing`，可直接使用 `python -m deepsearch_tracing.communication`。应用的安全模型诊断桥接位于 `deepsearch_agent.adapters.tracing`；旧 CLI 路径继续转发。模块依赖见 [当前架构](ARCHITECTURE.md)。
