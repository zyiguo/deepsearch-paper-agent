# MiMo-V2.6-Pro 与 Badcase 配对回归

日期：2026-10-06（北京时间）。本轮已完成 10 条 PDFVQA 诊断轨迹、1 条 ChartQA 给图验收；另做了 1 次极短文本接口探测。没有启动每个数据集 100 题的正式评测。

## 本轮补齐

1. 现有网关真实接受 `mimo-v2.6-pro` 的结构化文本与图片请求，成功响应报告相同模型名；这不是对服务内部实际权重的独立证明。
2. 增加按题目/变体固定运行清单和模型/上下文/输出/超时的实验级覆盖，不修改应用环境；评分遵循保存的样本清单。
3. 增加离线跨运行比较器：检查数据/索引一致性，保留配置差异、错对翻转、主次标签、usage、延时及最终工作流状态。缺失结果不能算作修复收益。
4. 根据真实轨迹增加状态转换保护：完整核验且没有问题/补查/漏项时结束；有证据但无草稿的 revise 改为 draft，随后仍需核验。通过 `rag.dispatch_guard` 留痕。

## 数据与控制条件

使用原 `ablation-20261005` 的相同 manifest、页面与 BM25/预处理 OCR 索引；没有重建索引，没有再次探测图像 embedding 服务。
选题依据是已知错误机制：`pdfvqa/validation/563/1`（存在性问答、引用越界）与 `pdfvqa/validation/644/13`（元素多选）。这是开发诊断集，不是未见测试集。
两题固定使用 `agent_no_ocr`，保留 Agent 工具循环、预处理 OCR、视觉重排和原图输入。记忆、技能、外部 MCP 关闭。
每题最多 18 次模型请求、6 次工具调用；超时 120 秒；在线 OCR 预算显式记为 4096，但该变体不调用在线 OCR。
完整预登记及追加原因见 [计划](plans/2026-10-06-paired-badcase-regression.md)。所有失败原样保留，不挑选重试结果。

## 答案与流程结果

| 配置 | 模型 | 上下文 / 输出 | A：存在性 accuracy（1 题） | B：元素集合 EM（1 题） | 图终态 A / B |
| --- | --- | --- | --- | --- | --- |
| fixed-old | mimo-v2.5 | 32768 / 2048 | 0 | 0 | incomplete / incomplete |
| model-only | mimo-v2.6-pro | 32768 / 2048 | 1 | 1 | verified / verified |
| context-only | mimo-v2.6-pro | 1048576 / 2048 | 1 | 0 | verified / verified |
| output-budget | mimo-v2.6-pro | 1048576 / 32768 | 0 | 1 | incomplete / incomplete |
| output-budget-guard | mimo-v2.6-pro | 1048576 / 32768 | 0 | 1 | verified / verified |

注意：B 的 output-budget 组虽然受支持 claim 的元素集合命中金标，图仍有未解决意见。既有评分器保留该 claim 的任务得分，不代表工作流完整成功。本次新增比较字段分别记录 graph_status、stop_reason 与任务得分，没有事后改写评分口径。

ChartQA `chartqa/val/human/32` 的独立 oracle 验收返回 `Cuba`，金标为 `cuba`，exact match 和 relaxed accuracy 均为 1。该调用直接提供对应图像，不是跨图检索质量验证。

## Badcase 追踪

| 题目 / 对照 | 观察 | 归因边界 |
| --- | --- | --- |
| 563/1：历史 -> fixed-old | `verifier_rejected_correct` -> `abstention`，仍错误；当前停止原因为 research_limit | 旧代码修复没有在这次旧配置运行中带来答案得分提升 |
| 644/13：历史 -> fixed-old | 原有多选元素 -> 未起草即 revise 后停止 | 暴露非法状态转换；不是召回遗漏 |
| 两题：fixed-old -> model-only | 都由错变对，存在性回答 no、元素仅选 8 | 当前两题观察支持继续验收新模型，不支持总体提升声明 |
| 644/13：model-only -> context-only | 元素从 [8] 变为 [8,9,0]，Verifier 接受 | `verifier_accepted_wrong` 是任务评分错判现象，不等同于所有文字结论均虚假 |
| 563/1：context-only -> output-budget | 正确且已核验的 no 被后续补检清空支持状态，最终停止 | 主标签仍为核验阶段丢失正确草稿；具体机制是后续调度，不应责怪原 Verifier 拒绝了草稿 |
| 644/13：output-budget | 越界引用被定向修复，最终元素正确，但保留核验意见 | 说明引用修复路径能触发；不代表整个问题已无争议 |
| 563/1：output-budget-guard | 输出 0 而非 no，Verifier 接受 | 存在性与计数回答类型未细分；按既有官方任务口径计错，不把 0 事后改成 no |

追加两条云端轨迹没有触发 `rag.dispatch_guard`，因此不能将终态改善直接归因于保护分支。该分支的机制由确定性脚本测试复现验证：旧实现失败，新实现阻止多余补检/修订/停止，并保留原文引用和核验流程。
本轮没有人工专家复核，也没有将助手日志检查填写成 expert_override。

## 请求开销

| 配置 | 已发送模型请求 | 提供方返回的 total_tokens 合计 | 两题延时（秒，按 A/B） |
| --- | --- | --- | --- |
| fixed-old | 27 | 85388 | 49.94 / 101.28 |
| model-only | 21 | 48588 | 75.13 / 110.64 |
| context-only | 25 | 71041 | 84.26 / 91.61 |
| output-budget | 29 | 75012 | 82.41 / 124.64 |
| output-budget-guard | 24 | 68939 | 77.64 / 158.00 |

另有 ChartQA oracle 1 次请求、480 Token、8.78 秒。以上共 127 次请求、349448 Token；极短文本探测另计 1 次、24 Token。
仅统计服务返回 usage，不折算货币账单。PDFVQA 全部请求最大实际输入 6557 Token、最大实际输出 467 Token，远低于原 32K/2K 限额。因此没有证明扩大窗口或输出预算改善了截断，也没有证明服务实际支持完整 1M 输入。

## 产物与复现

原始轨迹保留在本地 `data/evaluations/regression-20261006-{fixed-old,model-only,context-only,output-budget,output-budget-guard,image-smoke}/`，包含 run.json、predictions.jsonl、summary.json 和 badcases.jsonl；不提交运行数据与原图。
最终跨运行对照位于 `data/evaluations/regression-20261006-comparisons/final/`，早期缺少 graph_status 的比较文件亦保留，不覆盖。
比较入口和参数见 [评测说明](AGENTIC_ABLATION.md)。比较器会拒绝 manifest/索引不一致、重复结果和未声明样本，未知 usage 保留 null。

离线回归：`pytest -q -rs --basetemp tmp/dispatch-guard-full`，550 项通过，2 项 Windows 符号链接权限相关跳过。没有因跳过项声称完整平台验证。

## 下一步

1. 按数据提供的任务子类型约束存在性 yes/no 与计数 0-5，并建立 Verifier 的任务契约测试集；不为既有错例补分。
2. 在未参与这轮调试的题目上、多次重复比较固定 RAG 与完整 agent_full，联合报告正确率、核验错判和成本。
3. 独立验收真实图像向量检索；随后再补任务取消、持久化执行与断点恢复。

结论：新模型接入已通过小样本真实调用验证；评测与归因闭环更完整，但答案质量仍不稳定，不能宣称已获得稳定准确率提升或已补齐全部生产能力。
