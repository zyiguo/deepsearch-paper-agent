# 现有数据集真实模型评测：2026-10-18

本轮使用当前 MiMo-V2.6-Pro 实际请求，复用本地固定数据，完成 220/220 题；预检不计入题目得分，失败没有自动重试。图像向量接口返回 HTTP 402，该项未完成。

| 数据/任务 | N | 主指标 | 补充指标 | 执行失败 |
|---|---:|---|---|---:|
| ChartQA oracle | 100 | relaxed accuracy 59.00% | EM 54.00% | 0 |
| PDFVQA A oracle | 50 | accuracy 52.00% | — | 0 |
| PDFVQA B oracle | 50 | element EM 20.00% | element F1 41.08% | 0 |
| musique-mainline | 10 | answer EM 10.00% | answer F1 31.33% | 6 |
| 2wikimultihopqa-mainline | 10 | answer EM 70.00% | answer F1 79.23% | 1 |

## 评测范围与对照

ChartQA/PDFVQA 是给定页面的视觉组件评测，不经过父子 Agent 图；PDFVQA 来自公共镜像的 A50/B50 过滤子集，B 提供 OCR/元素目录。A 的 no 多数类基线是 64%，本轮 52% 仍低于该基线。

MuSiQue/2Wiki 各 10 题经过真实父子 Agent、工具检索、结构化交接和独立核验；固定 sampled_pool 与 BM25，图像、Wiki、记忆、skills、MCP、reranking 和 query expansion 禁用。仅计 answer EM/F1 与文档支持 presence proxy，不是官方完整榜单，也不验证 HTTP、Redis 或业务数据库恢复。

| 历史参考指标 | 旧结果 | 本轮 |
|---|---:|---:|
| ChartQA relaxed accuracy | 56.00% | 59.00% |
| PDFVQA A accuracy | 40.00% | 52.00% |
| PDFVQA B element EM | 8.00% | 20.00% |
| musique-mainline answer EM | 10.00% | 10.00% |
| 2wikimultihopqa-mainline answer EM | 80.00% | 70.00% |

历史多模态模型是 mimo-v2.5，本轮是 mimo-v2.6-pro；代码和视觉上下文配置也存在差异。多跳保留原采样、模型与预算，但实现版本不同。每项条件差异保存在 evaluation-summary.json；这里只作历史描述，不能将差异归因于单项修改，也没有统计显著性或稳定提升结论。

## 成本、轨迹与未知项

- 正式评测模型请求 464 次；含 4 次独立预检/访问诊断，共 468 次远程请求。
- 已知供应商 token 至少 935,774；204 次调用用量缺失，总量保持 null。现有视觉 runner 不记录 token，未把本地估算当作计费量。
- 自动主指标分组计数：{"success": 103, "failure": 117, "unknown": 0}。各任务指标不同，不计算跨数据集总体准确率。
- 人工审核数为 0。benchmark 匹配、执行状态、内部核验、人工正确性和根因分别保存；自动阶段观察不是因果根因。
- 原始多跳 attempts.jsonl 保存完整任务、工具观察、子结果、父摘要和模型输入；总报告只记录计数和定位。页面预测保留原始模型输出。
- 通信统计仅为交接次数和估算载荷大小，semantic_distortion_rate 保持 null，不能用压缩比冒充语义失真率。
- 校验 197 个原有文件，保持不变：True；本轮数据 manifest 与图像资产也通过独立重评分校验。

## 多跳执行失败记录

| 运行 | 题目 ID | 状态 | 已观测原因 |
|---|---|---|---|
| musique-mainline | 2hop__20256_74481 | budget_exhausted | search_calls |
| musique-mainline | 2hop__263728_705527 | budget_exhausted | search_calls |
| musique-mainline | 2hop__53147_7298 | budget_exhausted | search_calls |
| musique-mainline | 3hop1__593288_720914_27537 | budget_exhausted | search_calls |
| musique-mainline | 3hop1__90327_78276_68042 | budget_exhausted | search_calls |
| musique-mainline | 3hop1__9285_5188_86687 | error | content_filtered |
| 2wikimultihopqa-mainline | a457e11c0bdb11eba7f7acde48001122 | invalid_output | mainline_schema_validation_failed |

## 原始证据与使用方式

运行根目录：`output/evaluation-20261018-204144`。

- `evaluation-summary.json`：结果、历史条件差异、通信计数、成本和阻塞项。
- `live-catalog.json`、`scored/report.json`：本轮输入和输出指纹及独立重评分验收。
- `scored/success.jsonl`、`scored/failure.jsonl`、`scored/unknown.jsonl`：逐题分组与原始运行定位。
- 各运行 `run.json`、`report.json`、`predictions.jsonl` 或 `attempts.jsonl` 及旁边日志：完整原始记录。
- `image-embedding-preflight.json`、`image-embedding-access-diagnostic.json`：向量接口失败证据；未自动替换模型，没有召回分数。

优先复核 Supervisor 输出格式失败和 MuSiQue 的重复检索/预算耗尽，再查看已检索支持段落但答案不匹配的记录。本轮不修代码或提高预算来替换失败结果；修复后使用新运行目录和同一批题复测。

## 最终一致性校验

`validation.json` 确认 220 条唯一运行/题目记录、无缺失、原始分数与独立重评分一致、源文件指纹一致、没有活动运行锁、人工标签仍为未知。执行状态为 ok=213、budget_exhausted=5、error=1、invalid_output=1；执行正常不等于答案正确。正式评测进程均正常退出，评测期间未修改生产实现、模型配置、业务数据库或既有数据。
