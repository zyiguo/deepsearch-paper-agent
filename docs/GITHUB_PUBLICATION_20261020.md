# GitHub 发布记录：恢复一致性与部署治理

本次发布基于 `3ac13301c89331f5e18b1bd2aa3c968243b3d9b5`，将已完成的恢复一致性、业务观测、成本配额与容量保护一起提交到既有 GitHub 仓库。沿用单 API 进程部署，未改变已有用户原件、资料身份和历史实验结果。

## 实现与文档

| 范围 | 发布内容 | 设计与证据 |
|---|---|---|
| 工具与记忆恢复 | 确定未执行拒绝重放、未知调用审计核对、原运行继续、记忆 revision 与持久 scope 授权恢复 | [恢复设计](BACKEND_RECOVERY_CONSISTENCY.md)、[工具账本](TOOL_EXECUTION_LEDGER.md) |
| 文献一致性 | 持久入库阶段、校验产物复用、付费未知显式确认、索引发布后元数据核对与修复；前端展示确认提示 | [恢复验证](BACKEND_RECOVERY_VALIDATION_20261020.md) |
| 成本与容量 | 部署共享治理服务、用户/空间事务预占、未知费用冻结、管理员核对、全局与用户/空间并发租约 | [治理设计](GOVERNANCE_CAPACITY.md)、[ADR-0014](adr/0014-deployment-governance.md)、[配置模板](../.env.governance.example) |
| 业务观测 | 区分 HTTP、持久研究与供应商结果；低基数指标、滚动 p95、持久告警状态及受保护管理接口 | [治理验证](GOVERNANCE_VALIDATION_20261020.md) |
| 面试与教学 | 更新架构与技术报告正文，新增从执行顺序、故障窗口到源码的知识点说明 | [当前架构](ARCHITECTURE.md)、[技术报告](INTERVIEW_TECHNICAL_REPORT_20261019.md)、[后端知识点](BACKEND_ENGINEERING_KNOWLEDGE_20261020.md) |

## 验证口径

- 当前实现的完整后端回归记录为 **1059 passed、14 skipped**，见[治理验证清单](../eval/results/governance-validation-20261020.json)。发布阶段没有修改生产 Python 源码，重新核对清单内源码与附属文件摘要。
- 发布阶段重新执行架构审计：**214 模块、544 依赖、0 循环、0 违规**。重新执行前端测试：**11 个文件、63 项通过**；TypeScript 与 Vite 构建通过。
- 发布文件清单共 **68 个变更文件**；Git 索引中 **585 个文件**通过现有发布规则扫描，0 条发现。暂存区空白检查、文档相对链接检查通过。扫描结果只覆盖这次索引与规则的范围。
- 工具账本 14 组、入库 10 组、费用与工具组合恢复 5 组真实子进程退出场景通过。这里的供应商计次来自独立模拟服务；测试覆盖进程退出，不等同于物理断电或真实供应商的执行保证。
- 合成容量报告覆盖三工作空间、每空间 100/1000 份文献、并发 1/4/12，每种条件重复两次。144 条测量请求中，**68 条完成且带引用、76 条被保护拒绝、0 条意外状态码**；结束时资源 active 全部归零。研究/供应商限制刻意收紧为 4/2，模型为合成 HTTP 提供者。此报告用于检查保护行为、延迟和内存，不作为生产吞吐、SLA 或回答正确率结果。

本次没有重新执行 Docker、真实 Redis/PostgreSQL 或云模型验收。历史容器与真实 Redis 验收见[原报告](CONTAINER_REDIS_E2E_20261018.md)。也没有重跑或改变历史 Badcase 的模型与语料；v5 扩大重复评测中总 Token 增加 5.06% 的结论继续保留。

## 公开范围与身份

提交采用明确文件清单，包含实现、测试、前端变更、示例配置、文档、三个复现脚本及六份脱敏验证 JSON。实际环境文件、数据库、用户资料与会话、私有运行目录、原始模型输入输出和无关个人文档不在发布范围。发布扫描检查最终 Git 索引，不覆盖全部 Git 历史。

恢复验证清单冻结于治理实现之前，其 1025 项回归和源码摘要继续保留；后续治理实现改变了其中部分文件。最新治理清单内的 `uncommitted_workspace_increment` 描述验收发生时的状态。发布不篡改这两份原始清单。

Git 的换行转换可能使仓库 blob 与本地验收时的原始字节摘要不同。发布阶段逐文件核对验收原始摘要，并将仓库 blob 摘要及换行差异写入独立的[发布身份记录](../eval/results/publication-identity-20261020.json)。原始验证清单保持原样，不能用换行转换后的字节冒充当时的原始输入。

公开验证材料：

- [恢复清单](../eval/results/backend-recovery-validation-20261020.json)、[工具故障注入](../eval/results/tool-ledger-faults-20261020.json)、[入库故障注入](../eval/results/ingestion-recovery-faults-20261020.json)。
- [治理清单](../eval/results/governance-validation-20261020.json)、[费用与工具故障注入](../eval/results/governance-recovery-20261020.json)、[合成容量](../eval/results/governance-capacity-20261020.json)。

## 使用边界

成本点是内部整数单位，未知用量继续冻结，图像及推理估算不是实际金额上限。第三方 MCP 的费用尚未自动计量。指标计数器与直方图重启会重置，费用、审计和告警状态持久保存。告警通过 API 与日志输出，外部监控采集和通知仍需部署配置。工具未知结果的人工核对与费用核对分别审计，任何一方确认都不能替另一方证明完成。

## 同日后续：并发、可用性、性能与 Redis 知识补充

在 `fb440ef` 之后增加[专题说明](CONCURRENCY_AVAILABILITY_PERFORMANCE_REDIS.md)，同步更新技术报告、后端知识点、README、架构与基础设施导航。上述 68 文件清单及验收身份属于此前实现发布；此处记录后续的纯文档增补。

内容包括准入与背压、请求快照开销、Little/Amdahl 定律、成功吞吐与分位数、缓存穿透/击穿/雪崩、singleflight、TTL 抖动、负缓存/Bloom、锁租约与 fencing、Redis 内存策略、Sentinel/Cluster，以及单进程向多副本演进。知识点扩展到 14 节，技术报告追问扩展到 39 题；可运行的合并读取和分位数教学示例通过。

本次没有修改生产源码、测试或公开 JSON，也没有部署 singleflight、TTL 抖动或高可用集群。理论按 Redis/PostgreSQL 官方文档核对，当前机制按源码核对；链接、围栏、PowerShell 语法与发布文件范围另行检查，不用原有测试数字冒充新功能验收。

文档自验：7 份文档的 308 个相对链接有效，17 段 PowerShell 语法检查无错误，问题编号 1–39 与知识点编号 1–14 连续；singleflight 和分位数两个教学示例实际执行通过。治理验收清单的 154 条源码摘要仍匹配；本次未重复完整业务回归或真实服务验收。

## 同日后续实现：缓存击穿与雪崩保护

在 `f4ece49` 后实现有界实例级 singleflight、向下 TTL 抖动、取消/超时不接管、低基数指标和配置开关。原 paid run/slot 与费用 unknown 仍由持久账本决定，新增混合批次持久回归避免将已付费操作误记为确定未执行。

完整回归连接真实 Redis，1144 项通过、13 项跳过；架构为 214 模块、545 依赖、0 循环/违规。隔离 WSL Redis 7.4.11 的 16 项校验通过，受控热点16并发回源由16次减至1次。真实模型探针的4次baseline请求失败，4笔费用unknown保留，protected未启动；没有填造真实模型降本结果。

当前实现、服务身份和复现见[指南](REDIS_CACHE_PROTECTION.md)与[本轮验证报告](REDIS_CACHE_PROTECTION_VALIDATION_20261020.md)；[验证清单](../eval/results/cache-protection-validation-20261020.json)及两份成功/失败报告分别冻结。前述发布清单是历史范围，此后源码自然有新的身份；新[发布身份记录](../eval/results/cache-protection-publication-20261020.json)处理本轮原始字节与仓库换行。运行时账本、Redis控制配置、密钥、原始目录与个人文件不在提交范围。
