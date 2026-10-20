# 后端恢复与入库一致性验证记录（2026-10-20）

## 版本与范围

实现基于提交 `3ac13301c89331f5e18b1bd2aa3c968243b3d9b5` 的工作区增量。保持单 API 进程；验收时尚未提交或推送 GitHub，没有修改用户原件、历史实验输出和人工记录。后续发布见 [2026-10-20 发布记录](GITHUB_PUBLICATION_20261020.md)。

本文与机器可读清单冻结于治理实现之前。后续治理增量修改了部分相同源文件，旧摘要继续表示该次验证身份；当前源码与完整回归以 [治理验证](GOVERNANCE_VALIDATION_20261020.md) 及其清单为准，不重写旧清单来冒充同一版本。

设计与使用方法见 [恢复与入库一致性](BACKEND_RECOVERY_CONSISTENCY.md)，实施计划见 [计划文件](plans/2026-10-20-backend-recovery-consistency.md)。本轮关键源文件 SHA256 保存于 [机器可读验证清单](../eval/results/backend-recovery-validation-20261020.json)。

## 最终检查

| 检查 | 结果 |
|---|---|
| 后端整套 pytest | 1025 passed，14 skipped，88.80 秒 |
| 前端 Vitest | 11 个文件，63 passed |
| 前端 TypeScript 与 Vite 构建 | 通过 |
| AST 依赖约束 | 208 模块，512 依赖，0 循环，0 违规 |
| 工具账本真实进程退出 | 14 组全部通过 |
| 入库真实进程退出 | 10 组全部通过 |
| Git 差异空白检查 | 通过 |

前端首次运行使用沙箱默认 Temp 时有缓存文件 ENOENT；设置项目内 TEMP/TMP 后重跑全部通过。Windows 轮询与原子替换竞争在代码中限制为只重试替换已生成文件，没有重试业务调用。

14 项跳过未计入通过数量。本轮没有重新进行容器、真实 Redis、PostgreSQL 灾难恢复或真实模型供应商的端到端验收。故障计费统计来自模拟供应商，不能据此声称供应商具备 exactly-once 能力。

## 新增行为回归

- 新记忆句柄重放读取后可以写入；缓存写入会消耗旧权限。
- 过期 revision 不能覆盖人工修改，拒绝按 not_executed 保存并重放。
- 清空记忆的撤销标记跨进程重建保留；即使初始文本相同，旧读取和旧检查点也不恢复写权限。
- 父图工具会话快照恢复读取权限，发送前取消记录为确定未执行。
- unknown 核对检查运行/会话/空间归属；viewer 无写入权限，操作者来自认证会话。
- 回执结果落库后原运行通过 resume SSE 完成，不重新调用原工具。
- 本地记忆回读只确认匹配目标；不能注入 OCR、检索或规划证据；存储错误保留未知并返回安全诊断。
- 已完成解析、文本向量和图像向量可以复用；付费步骤没有产物时要求明确确认重试。
- 索引发布成功但 ready 保存前退出时，重启后可核对并修复，索引与原件字节不变。
- 发布失败后的重试复用向量并保留后来增加的其他资料。
- 资料原件丢失、页文本变化、索引不匹配、产物损坏或丢失时不伪造修复成功。
- 图像补建的普通重试继续原图像步骤，不重新生成文本向量。

原 HTTP 契约快照保持不变；测试只允许新增四个核对/修复路径、已有 operations 路径，以及三个重试动作的可选确认参数和文献响应的确认标志。旧格式索引、会话和检查点读取测试仍通过。

## 真实进程退出

[工具账本报告](../eval/results/tool-ledger-faults-20261020.json)重新运行已有 14 组实验，覆盖只读、付费、写入和供应商幂等协议；使用 `os._exit(73)` 以及独立 SQLite 模拟服务。

[入库报告](../eval/results/ingestion-recovery-faults-20261020.json)覆盖视觉分析、文本向量、图像向量的三个边界，以及索引发布后的退出，共 10 组：

| 退出点 | 未经额外确认的恢复 | 明确确认后的结果 |
|---|---|---|
| running 意图已保存、模拟请求尚未调用 | 保留未知，不发请求 | 该步骤调用 1 次 |
| 模拟服务已计次、产物尚未保存 | 保留未知，计次不增加 | 该步骤累计 2 次，确认重试有重复计费风险 |
| 产物已保存、步骤完成状态尚未保存 | 复用产物 | 已完成步骤累计 1 次 |
| 索引已发布、ready 尚未保存 | 核对后修复元数据 | 全流程各付费步骤各 1 次 |

报告 SHA256：

```text
ingestion-recovery-faults-20261020.json
bc80d6330b58062778c899df81f8de20670407566e3806d74a8035c68290eba9

tool-ledger-faults-20261020.json
43220ac2ab43d886db5d45a461fad921447909eb7e3294714f7f88397eb16340
```

## 复现

使用新的输出和临时目录，不覆盖已有验收材料：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONUTF8='1'
$taskTemp=Join-Path (Get-Location) 'tmp/backend-recovery-next-env'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP=$taskTemp
$env:TMP=$taskTemp
.venv/Scripts/python.exe -m pytest -q --basetemp tmp/backend-recovery-next --tb=short
.venv/Scripts/python.exe -m deepsearch_experiments.architecture
.venv/Scripts/python.exe scripts/verify_ingestion_recovery.py --output output/ingestion-next
.venv/Scripts/python.exe scripts/verify_tool_ledger_faults.py --output output/tool-ledger-next
Push-Location frontend
npm test
npm run build
Pop-Location
```
