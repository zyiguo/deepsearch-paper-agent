# 缺口复用重复评测公开结果

报告：[重复对照评测](../../../docs/REPEATED_GAP_EVALUATION_20261019.md)。

MuSiQue、2Wiki 共 20 题，v4/v5 各重复三次，共 120 次真实预测。实际供应商消耗为 v4 1,683,298 Token、v5 1,768,512 Token，v5 增加 5.06%；自然触发 9 条暂缓路径，覆盖 7 次运行、6 道题。当前没有稳定降本或零误暂缓结论，真实误暂缓率为 null，人工标签数为 0。

公开范围为显式白名单：统计、题目 ID、配对成本、结果分组、模型审计标签、合成安全反例和来源哈希。完整模型输入/输出、自然文档原文、真实引文、私密配置和用户资料不公开；因此此包支持统计复算，完整证据复核需要本地原始记录。

- `summary.json`：成本、质量、按题聚类 bootstrap 区间。
- `paired.json`：60 个按题和轮次配对的记录。
- `per-attempt.json`：120 次预测的分数、用量和阶段成本，不含生成答案原文。
- `success.json`、`failure.json`、`unknown.json`：按自动结果分组，非人工标签。
- `protocol.json`：模型、预算、选择规则、冻结题目和作业顺序。
- `audit-summary.json`、`audit-paths.json`：额外模型审计和清空缺口状态后的核验重放。
- `audit-validation-diagnostics.json`：无效审计原因。
- `rule-replay.json`：9 条自然决策的精确重放结果。
- `synthetic-safety-counterexample.json`：构造性离线反例，不计入自然评测。
- `cost-breakdown.json`：角色成本及跳过核验原因。
- `validation.json`、`provenance.json`：验证范围、原始文件哈希及公开范围。
- `checksums.json`：导出脚本生成的 15 份 JSON 的 SHA-256；本说明和后补发布身份记录不在该导出校验清单内。
- `publication-identity.json`：评测源码与发布时本地源码的身份差异。评测之后仅增加离线发布扫描器的精确测试样例例外，已通过移除此处改动在内存中重建原评测指纹；未改变问答或评测执行行为。原始字节指纹受 Git 换行转换影响。

审计额外消耗 139,554 Token，不计入主对照降本幅度。三个重复不能当作 60 道独立题，区间按题聚类。KABG 错误金标只从正确率统计隔离，原始记录及费用保留。

使用 `scripts/export_gap_repeat_publication.py` 从本地冻结结果生成；目标目录必须不存在，不覆盖已有文件。
