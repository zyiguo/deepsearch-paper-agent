# 2026-10-07 小批量组件消融预登记

## 范围

在读取本轮模型结果之前冻结以下规则。使用既有 `ablation-20261005` 数据与索引，在六个题型内按 case ID 字典序各取第一题；不参考预测、答案或历史得分。它们仍属于已用开发诊断集，不是未见测试集。

- ChartQA human: `chartqa/val/human/32`
- ChartQA augmented: `chartqa/val/augmented/238`
- PDFVQA A counting: `pdfvqa/validation/88/12`
- PDFVQA A existence: `pdfvqa/validation/209/0`
- PDFVQA B object_recognition: `pdfvqa/validation/644/13`
- PDFVQA B structural_understanding: `pdfvqa/validation/206/18`

每题五组：fixed_workflow、agent_full、agent_no_rerank、agent_no_ocr、agent_no_visual，共 30 条。各一次生成，不删除失败、不择优重跑；按既有 seed 打乱执行顺序、并发 2。先执行 2 条验证接入，正常则用同一 run identity 补齐剩余 28 条，不另算验收样本。

## 固定配置

- 模型 `mimo-v2.6-pro`，context_window 1048576，max_output_tokens 8192，在线 OCR 输出 8192，单请求超时 120 秒。
- 输出预算为本次受控实验预算，不声称它是最优值；环境文件不修改。最大窗口设置不代表实际发送了 1M Token。
- 每题最多 18 次生成请求、6 次工具调用、1 次在线 OCR；k=3。最多 540 次生成请求，若已记录服务故障明显阻断实验则停止并报告未完成分母。
- 共用现有 BM25 + 预处理 OCR 索引。原索引回退原因为历史 image_embedding_http_402，本轮不重新探测或重建，不声称当前仍返回 402。
- 用户随后明确“先不考虑图像向量的部分”，与上述范围一致；图像向量不作为本次缺失对照或待补验收。
- memory、skills、外部 MCP 不参与；no_ocr 只移除在线 OCR，no_visual 只移除研究/回答/核验角色原图，仍保留视觉重排及 OCR。
- 不新增 oracle，旧模型 oracle 不混入新模型比较；新运行与历史运行的模型/预算/代码均不同，不做单因素因果归因。

## 判分与产物

保持原答案口径，ChartQA relaxed accuracy、PDFVQA A accuracy、B 集合 EM/F1 分别报告。缺失、失败、弃答留在分母，保留 Wilson/配对 bootstrap 区间与样本数，不宣称显著性。

使用此前已实现的 DAG 阶段代理规则，不根据本轮结果调权重。报告 evidence/context/answer/citation 的原始分数、依赖阻塞、未知与工具诊断；金页覆盖不是语义忠实性。

结果目录：`data/evaluations/ablation-20261007-small`。
DAG 输出：`data/evaluations/dag-20261007-small`。
报告：`docs/ABLATION_20261007_SMALL.md`，补充总结：`docs/ABLATION_20261007_FINDINGS.md`。

只修复报告的小批量兼容和陈旧描述，不在本轮运行中修改 Agent、提示词或评分规则。预算拦截不计已发送请求；usage 缺失不记零。自动 badcase 标签不代替专家复核。

## 执行恢复记录

用户继续任务时，原执行会话已失效，本机未发现仍在运行的 Python 进程。预测文件已保存 13 条，summary 仍为最初 2 条验收后的旧快照。恢复前核对代码指纹与 run.json 一致，使用同配置 `--resume` 只运行剩余 17 个组合；保留已落盘的错误与弃答。

中断时可能存在未落盘的在途请求，本地无法恢复其 usage 或判定云端是否完成。这部分不伪装成零成本；最终账单可能高于保存轨迹中上报的 Token 合计。恢复会重新安排未落盘组合，但不是按答案好坏挑选重试。540 次是完整计划保存轨迹的请求预算上限，不再用作跨中断的实际累计调用保证。
