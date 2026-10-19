# 任务可靠性、恢复与质量闭环验证（2026-10-18）

已完成代码实现、真实 PostgreSQL/Redis 回归、当前账号数据的离线备份和隔离还原，以及质量闭环的合成验收。原业务数据库保留运行，最终 API 和前端已恢复；本轮没有付费模型调用，也没有将合成标签混入真实业务数据。

## 最终结果

| 验证 | 实际结果 |
|---|---|
| 完整后端回归 | **852 passed, 2 skipped in 93.91s**；真实 PostgreSQL 与独立临时 Redis；跳过为 Windows 符号链接权限 |
| 依赖审计 | **193 模块、459 条边、0 循环、0 违规** |
| 独立导入 | `python -S` 成功导入质量模块，不加载 FastAPI、LangGraph、模型或数据库 SDK |
| Linux 冒烟 | 官方 Python 3.12 slim 临时容器验证进程文件锁、标准库质量模块和 12 条并发完整轨迹 |
| 当前数据备份 | **57 个文件**，包含 schema 逻辑备份及业务文件；完整清单封存并可校验 |
| 真实隔离还原 | **12 张表**的记录数及内容指纹全部一致，文件哈希全部一致；非 superuser 业务存储读取通过 |
| 还原业务内容 | 1 个账号、1 个 owner 空间；当前空间 3 会话、3 问答、3 份记忆，另保留之前 local 过渡导入的 3/3/3 记录 |
| 还原耗时 | 演练流程实测 **4.36 秒**，不含备份、前置排障及服务切换；不作为生产 RTO 承诺 |
| 在线互斥 | 真实 API 占用部署锁时，备份命令返回 DeploymentBusy，并且不创建输出目录 |
| 最终在线核对 | 通过原前端代理登录管理员、读取原空间 3 个会话/答案/记忆和就绪状态，退出后拒绝访问 |
| 源保护 | 迁移前保留的原始 29 个文件 SHA256 未变；原模型、Embedding、视觉和图像向量配置未变 |
| 质量合成验收 | resolved、regression、awaiting_review、awaiting_regression、incomparable 各 1；另测旧运行重标只能 awaiting_new_run |
| 真实历史质量 | 当前 3 条历史答案缺少业务运行关联和人工标签，**正确性未知，正确率为 null**；不报告真实质量提升 |

## 故障注入覆盖

- 同步和 SSE 以同一幂等键重放，只产生一次模型调用及一个轮次；不同请求复用键拒绝。
- 答案提交后完成状态写入失败，通过已保存轮次修复；恢复时发现已有答案，无检查点也只返回原结果，不再次执行模型。
- 终态数据库记录失败仍能结束 SSE，错误不向客户端暴露原始异常。
- 缺少检查点、旧任务身份缺失、模型/预算/上下文变化拒绝隐式重跑。
- 前端断连后工作线程仍执行；取消后的迟到进度不能覆盖状态；同步/后台共享容量；关闭先取消并等待调用完成。
- PostgreSQL 20 次并发创建同一提交只保留一个任务，20 次原子执行认领仅一次成功。
- 文件损坏、目录逃逸、非空 WAL、嵌套备份目标和已有文件均拒绝；深层 Windows 工作空间路径能够备份并还原。
- 还原失败保留失败报告；清理只有在随机所有权标签匹配时执行，标签不匹配拒绝删除。
- 内部核验成功但人工判错仍计为错例；未知/未审不进入准确率分母；缺候选、缺人工复核、条件变化、缺修复引用、旧运行重新标注均不能关闭错例。

## 实际文件与身份

- 最终回归日志：`output/reliability-20261018/pytest-full-final.log`。
- 备份包：`backups/business-reliability-20261018-02/`；清单 SHA256 为 `b15c0a9302fe88e8bdebf50b68730994dd2ae0133b31be57f94ebc5e4f39cb28`。
- 备份时点：**2026-10-18 18:13:15（Asia/Shanghai）**；恢复报告保存 UTC 机器时间。
- 真实还原报告：`output/reliability-20261018/restore-01/restore-report.json`；隔离容器 `deepsearch-restore-cc00db421ede4339` 已按标签清理。
- Linux 验收：`output/reliability-20261018/linux-smoke.json`；临时容器退出自动删除。
- 质量合成报告：`output/reliability-20261018/quality-synthetic-final/report.json`，配套冻结、审核与比较文件完整保留。
- 真实质量盘点：`output/reliability-20261018/real-quality-inventory.json`；仅盘点当前空间，不修改或伪造历史轨迹。
- 最终在线报告：`output/reliability-20261018/live-final.json`；服务 PID 记录在 `services-final.json`。

业务 PostgreSQL `deepsearch-business-postgres-1` 与持久卷保留；本轮临时 Redis 已按明确标签删除。原管理员凭据未改变，验收登录已退出。新增私有 `.env.tracing` 启用未来各空间轨迹，旧历史不补造轨迹。

## 保留的失败与修复记录

首轮完整回归因系统临时目录 `pytest-of-root` 权限报 280 个 setup error，日志 `pytest-full.log` 保留；之后使用项目内新临时目录。第二轮发现模拟请求缺少 headers，以及压力下轨迹写入竞争，分别修正有效测试请求和进程内写入锁，日志 `pytest-full-02.log` 保留。随后完整回归 848/2 通过；最终补入容量、清理所有权与旧运行关闭保护后为上述 852/2。

第一次真实备份在深层 Windows 路径复制时失败，目录 `backups/business-reliability-20261018/` 保留 failure.json；未封存 complete 清单。修复扩展路径后，以全新 `-02` 目录完成。SQLite 还原校验的扩展 URI 问题由新增长路径测试定位并修复，没有删除相应行为测试。

## 边界

本轮运行的完整测试在宿主机，数据库与恢复环境为真实 Linux 容器；Linux 冒烟仅覆盖锁/轨迹标准库行为，未重新构建 API 镜像或声称完成新一轮前端构建。没有云模型真实效果评测、自动定时备份、异地灾备、连续 WAL 恢复、多 API 副本或生产 RTO/RPO SLA。当前质量审核和维护以 CLI 提供，不新增管理界面。

操作命令和指标分母见 [运行保障说明](RELIABILITY_RECOVERY_QUALITY.md)。
