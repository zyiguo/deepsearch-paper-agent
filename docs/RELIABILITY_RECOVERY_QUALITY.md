# 任务可靠性、数据恢复与质量闭环

当前仍为单 API 进程。任务中断后的继续执行、业务数据灾难恢复、回答质量审核分别有自己的证据与状态，不将一种成功当作另外两种成功。

## 任务可靠性

任务内部现在接入独立的 [工具执行账本](TOOL_EXECUTION_LEDGER.md)：保存逻辑操作、子规划决策和确认结果。恢复时复用已落库结果，付费或写入调用结果不明则阻止自动重试；只读调用允许安全重试。该能力与提交级幂等、图检查点分别负责不同执行边界。

普通 `/api/sessions/{id}/turns` 与流式 `/turns/stream` 都创建持久任务。客户端可发送 `Idempotency-Key`，1–128 位 ASCII 字母、数字或 `_.:-`。幂等键作用于同一会话，传输失败后用原键及完全相同的请求重放；若答案已经落库，直接返回原轮次，不再调用模型。原键不能用于不同问题或参数；执行中的重复提交返回冲突，客户端应查任务列表。

团队模式保持 `/w/{workspace_id}` 前缀、Cookie、`X-DeepSearch-Request: 1` 和 Origin 校验。没有提供幂等键时，每次提交仍代表一个新任务；服务端无法判断用户是否有意再次提问。前端连接断开不会自动取消后台任务；刷新历史和任务列表核对，避免重新提交。

任务 JSON 的 `_execution` 记录尝试次数、安全错误码及是否修复过结果关联。原始异常、SQL、连接字符串、模型凭据不写入任务错误字段。进度只能更新 running 任务，不能覆盖 cancel_requested 或终态。流式终态记录失败仍结束 SSE，任务状态保留未确认，不伪造完成。

每个空间默认最多 4 个活动研究任务，同步和后台请求共享容量与关闭取消信号。超过容量明确拒绝，不创建无界线程；不会承诺已经排入持久消息队列。服务关闭等待已接纳任务退出，线程池里的同步请求通过调用完成事件等待，避免等待常驻线程本身造成死锁。

恢复 `/runs/{run_id}/resume/stream` 只接收 interrupted 任务：

1. 先查询该运行是否已有轮次；有则修复 completed/turn_id 并返回原结果。
2. 没有轮次时，核对授权资料、片段 ID、模型/预算配置、会话上下文、技能文件及 Wiki 身份。
3. 必须存在 LangGraph 检查点，再原子认领执行权；身份缺失/变化或无检查点返回 409，不隐式从头调用模型。

没有检查点的旧任务可以由用户明确发起新任务；这会产生新的运行 ID，可能再次消耗模型费用。完成状态表示答案已保存，回答的正确性仍以独立核验和人工审核分别记录。

## 离线备份

真实 API 根生命周期会持有 `data/runtime/deployment.lock`，数据库写入 CLI、账号 CLI 与备份入口也使用该锁。备份前停止本部署 API，并确认外部 SQL/文件写入工具已经停写；锁被占用时备份拒绝执行，不创建一个看似成功的半备份。前端可以继续运行，API 暂停期间业务请求不可用。

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.application.maintenance_cli backup `
  --postgres-container deepsearch-business-postgres-1 `
  --docker-host 'npipe:////./pipe/dockerDesktopLinuxEngine' `
  --docker-config tmp/docker-config `
  --output backups/business-next
```

PostgreSQL 适配器要求显式选择本机源容器，并核对其回环端口与 `.env.database` 一致。使用官方容器内的 pg_dump 导出应用 schema；打包 `data/indexes`、`ACCESS_DATA_ROOT`、`data/runtime` 与项目技能目录。账号空间的文献、附件、索引、Wiki、工作记忆、检查点和轨迹都在包内。遗留本地数据也保留。

SQLite 模式不要求容器，备份同一组文件；非空 WAL/journal 拒绝封存。输出目录必须全新；检测目录链接、文件链接、文件集变化、文件内容变化及 PG 表变化。只有数据库和文件核对完毕才生成 complete 清单。开发缓存、历史实验输出和代码不属于业务备份；自定义外部原件目录需要另行纳入备份计划。

配置文件仅保存 SHA256，不复制模型密钥、数据库连接密码或首次账号明文密码。备份包含用户内容及密码摘要，保存在项目忽略的私有目录。应保管好模型/数据库密钥和应用版本的独立恢复材料。

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.application.maintenance_cli verify --bundle backups/business-next
```

## 实际还原演练

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.application.maintenance_cli restore-drill `
  --bundle backups/business-next --output output/restore-next `
  --docker-host 'npipe:////./pipe/dockerDesktopLinuxEngine' --docker-config tmp/docker-config
```

演练创建随机命名的新 PostgreSQL 容器、临时数据库文件系统和随机回环端口，向新目录恢复业务文件。不会接收一个现有数据库地址作为覆盖目标。还原先核对所有表的记录数与内容指纹，再撤销恢复副本中的登录会话，用没有 BYPASSRLS 的应用角色检查 schema、空间隔离及真实会话/记忆/任务存储读取。原账号密码摘要保留，用户在正式恢复环境需要重新登录。

演练结束，只清理具有本轮随机所有权标签的容器；恢复后的文件和 `restore-report.json` 保留供检查。失败也保留失败报告；清理失败不会报告成功。报告包含备份时点、备份年龄和实测演练耗时，不能把一次耗时当作生产 RTO 承诺。恢复副本中的运行按原启动规则标记中断，不自动执行模型。

当前工具提供备份校验和隔离还原演练。正式切换恢复环境需要操作者配置新数据库角色/密钥、文件根目录、匹配的模型配置、应用版本，然后检查账号和资料；不提供一键覆盖原库。没有连续 WAL 归档、定时任务、异地保存、加密备份或多副本能力。本轮未自动创建任何定时自动化。

## 质量闭环

已有公开数据及历史输出优先使用 [现有数据集评测](EXISTING_DATASETS.md) 做离线重评分和待复核筛选。该入口冻结数据/运行/预测身份，自动指标与执行状态分开；不将历史 benchmark 输出伪装为生产轨迹，或直接写成人工正确性、根因和 resolved。

可选 `.env.tracing` 设置 `TRACING_ENABLED=true` 后，生产问答记录完整轨迹；团队模式固定写入各空间 `traces.sqlite3`。录制失败仍不阻断问答，缺失轨迹保持未知。已有 `RAG_TRACE_DB` 与显式装配路径继续兼容。轨迹元数据关联业务运行 ID、会话 ID、模型/上下文身份与语料身份；完整子工具观察仍只进入子循环及轨迹。

冻结只读轨迹，不改原库：

```powershell
.venv/Scripts/python.exe -m deepsearch_tracing.quality freeze `
  --db data/team/workspaces/空间ID/traces.sqlite3 --output output/quality-baseline
```

得到 `snapshot.json` 和 `runs.json`；运行指纹与完整事件均保留。按 snapshot 中的 run ID 和 trace_sha256 创建独立人工标签文件，例如：

```json
[
  {
    "trace_run_id": "从snapshot复制的运行ID",
    "trace_sha256": "从snapshot复制的指纹",
    "case_id": "paper-answer-001",
    "verdict": "incorrect",
    "root_causes": ["evidence_loss"],
    "expected": "原文支持的期望行为或答案",
    "evidence": "经过人工核对的文献、页码和依据",
    "notes": "具体差异；这是人工判断，不是内部核验的自动映射"
  }
]
```

verdict 为 correct / incorrect / unknown；已知正确性必须填写期望与支持依据。错例必须标记根因，可明确选 unknown。当前根因包括 retrieval_miss、evidence_loss、constraint_loss、hallucination、verifier_false_accept、verification_reject、tool_failure、model_service、persistence、cancellation、budget、unknown。

```powershell
.venv/Scripts/python.exe -m deepsearch_tracing.quality review `
  --snapshot output/quality-baseline/snapshot.json --labels output/labels-baseline.json `
  --reviewer 实际审核人 --output output/review-baseline.json
```

审核报告分别给出执行分组、内部核验结果、人工正确率、审核覆盖率、已知正确性覆盖率、未知/未审数量和错例根因。正确率分母仅为人工 correct + incorrect；分母为零时为 null，不填 0 或 1。不从内部 verifier 推断人工正确性。

修复后按相同 case_id、问题、语料、模型预算与期望依据重新运行原业务流程，冻结候选轨迹并人工审核。候选标签可增加 `fix_id`，引用提交、工单或明确的修复记录。然后比较：

```powershell
.venv/Scripts/python.exe -m deepsearch_tracing.quality compare `
  --baseline output/review-baseline.json --candidate output/review-candidate.json `
  --output output/quality-comparison.json
```

旧错例人工判错、新结果人工判对、条件匹配且有 fix_id 时才是 resolved。缺少候选保持 awaiting_regression；缺评为 awaiting_review；身份缺失/变化为 incomparable；无修复引用为 awaiting_fix_reference；仍错为 open；原来正确现在错误为 regression。审核文件与导出均不覆盖，修订使用新文件。

候选还必须来自新的业务运行与轨迹；仅将同一次旧运行改标为正确会得到 awaiting_new_run，不算修复回归。可用 `python examples/reliability/quality_loop_demo.py --output output/quality-demo-next` 在全新目录执行明确标记的合成验收；它不会调用模型，不能用来报告真实业务正确率。

完整工具日志不传回父 Agent；审核/对比报告仅包含结论、身份与引用。人工标签不自动进入训练集，原有训练审核与 SFT 导出规则继续独立执行。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest tests/test_task_reliability.py tests/test_recovery.py tests/test_quality_loop.py -q
.venv/Scripts/python.exe -m deepsearch_experiments.architecture
```

本轮实际结果及未验证项见 [验证记录](RELIABILITY_VALIDATION_20261018.md)。合成质量闭环只证明状态和统计规则，不代表真实模型回答质量提升；真实历史没有轨迹或人工标签时仍为未知。

PostgreSQL 工具行为依据官方 [pg_dump 文档](https://www.postgresql.org/docs/17/app-pgdump.html) 和 [pg_restore 文档](https://www.postgresql.org/docs/17/app-pgrestore.html)；当前本机实际验收使用 PostgreSQL 17。逻辑备份不是连续时间点恢复。
