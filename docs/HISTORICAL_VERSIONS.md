# 历史决策与实验适用版本

当前架构自 2026-10-18 按 ADR-0010 生效，2026-10-20 增加工具核对、入库恢复与 ADR-0014 部署治理，见 [当前架构](ARCHITECTURE.md)。以下结果保留其原文、原分数和当时的模型/语料/实现身份；原内部模块路径属于整理前版本。阅读源码时使用迁移清单，不把历史分数解释为本次重构后重新测得的结果。

| 版本记录 | 适用范围 |
|---|---|
| [2026-10-20 恢复验证](BACKEND_RECOVERY_VALIDATION_20261020.md) | 治理实现前的恢复增量：1025 项通过、208 模块；源码摘要保留该阶段身份 |
| [2026-10-20 治理验证](GOVERNANCE_VALIDATION_20261020.md) | 包含恢复增量的后续工作区：1059 项通过、214 模块；容量报告使用合成供应商 |
| [2026-10-20 发布](GITHUB_PUBLICATION_20261020.md) | 当前源码、技术报告、知识点与公开验证材料的发布范围及身份核对 |

| 文档 | 适用范围 |
|---|---|
| [ABLATION_20261007_FINDINGS.md](ABLATION_20261007_FINDINGS.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [ABLATION_20261007_SMALL.md](ABLATION_20261007_SMALL.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [AGENTIC_ABLATION_AUDIT.md](AGENTIC_ABLATION_AUDIT.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [AGENTIC_ABLATION_RESULTS.md](AGENTIC_ABLATION_RESULTS.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [BUDGET_CONTROL_ABLATION_20261015.md](BUDGET_CONTROL_ABLATION_20261015.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [BUDGET_CONTROL_PILOT_20261015.md](BUDGET_CONTROL_PILOT_20261015.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [MAINLINE_MULTIHOP_COMPARISON_20261008.md](MAINLINE_MULTIHOP_COMPARISON_20261008.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [MULTIHOP_CLOUD_ACCEPTANCE_20261007.md](MULTIHOP_CLOUD_ACCEPTANCE_20261007.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [MULTIHOP_CLOUD_REACCEPTANCE_V2_20261008.md](MULTIHOP_CLOUD_REACCEPTANCE_V2_20261008.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [MULTIHOP_RESPONSE_FIX_20261007.md](MULTIHOP_RESPONSE_FIX_20261007.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [PAIRED_REGRESSION_20261006.md](PAIRED_REGRESSION_20261006.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [PUBLIC_MULTIMODAL_RESULTS.md](PUBLIC_MULTIMODAL_RESULTS.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |
| [RELEASE_AUDIT_20261006.md](RELEASE_AUDIT_20261006.md) | 重构前记录；按文内日期、运行清单与代码指纹解释 |

ADR-0001—0009 的结论与取舍保留；0001/0002/0005 已按原索引标为替代，0006 的 JSON/SQLite 持久化、0008 的可选 Redis 和 0009 的上下文隔离仍适用。0008 中“后台处理直接访问应用状态”描述的是当时实现，现已由独立索引服务替代。

`docs/plans/` 保存历史设计和实施计划，不能仅凭计划中的模块名判断当前能力。`eval/`、`data/`、`artifacts/` 和 `output/` 中已有数据或实验结果不因本次代码清理删除或重写。
