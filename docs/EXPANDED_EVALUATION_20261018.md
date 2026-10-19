# 现有数据扩容评测说明

本轮从本地已下载的 MuSiQue、2Wiki 各 1000 题中，分别新增 100 道未测题。旧的 ChartQA/PDFVQA 200 题和多跳 20 题保留原位、不重复调用；新增评测完成后，累计覆盖 420 个题目样本。旧 10 题和新 100 题分别报告，不能冒充同一批题上的修复前后实验。

运行目录为 `output/expanded-evaluation-20261018-100x2`。这是实际 MiMo-V2.6-Pro 调用，最终完成状态与结果以该目录的 `evaluation-summary.json` 和 `REPORT.md` 为准；在这些文件生成前仍是运行中的实验，不将计划题量视为已测题量。

## 实际完成结果

2026-10-18 已完成 200/200 道新增题，无缺失、无自动失败重试，40 个分片均正常退出；完整结果见 [扩容报告](../output/expanded-evaluation-20261018-100x2/REPORT.md)。

| 数据 | 本轮新增 | 精确匹配率 EM | F1 | 执行失败 |
|---|---:|---:|---:|---:|
| MuSiQue | 100 | 23.00% | 26.47% | 68 |
| 2Wiki | 100 | 57.00% | 62.91% | 30 |

本轮 98 次执行失败的已观测原因是：74 次检索次数耗尽、2 次模型调用次数耗尽、18 次结构化输出校验失败、2 次输出截断、2 次内容过滤。详情与题目定位保存在 [失败诊断](../output/expanded-evaluation-20261018-100x2/failure-diagnostics.json)；这些是停止/错误码，不是人工因果根因。

MuSiQue 的 17 道 4 跳题全部执行失败；2Wiki 的 compositional 类 41 题中有 20 次执行失败，EM 为 26.83%。这些分层现象支持优先复核执行链、预算停止与结构化交接；不能将中断后的零分直接等同为纯推理能力评价。

本轮正式模型调用 2541 次，已知供应商 token 至少 9,088,899，2 次用量缺失，总量保持未知。人工审核数为 0，语义失真率为 null。自动主指标分组为 success=80、failure=120、unknown=0，不据此计算跨数据集总体准确率。

验收确认 200 条唯一新增题目记录、200 份完整逐题轨迹与原始 attempts 内容一致；新旧问题 ID 和文本不重叠；206 个前期源文件保持不变；数据、代码、模型、预算及分片身份均匹配；没有活动运行锁。机器可读验收见 [validation.json](../output/expanded-evaluation-20261018-100x2/validation.json)，完整轨迹指纹见 [trace-manifest.json](../output/expanded-evaluation-20261018-100x2/trace-manifest.json)。架构检查仍为 194 modules、464 edges、0 cycles、0 violations。

## 固定采样与运行条件

- 数据来源、源版本、语料池及标签规则沿用上一轮。去除旧题 ID、旧问题文本与新集合内部重复文本；剩余候选为 MuSiQue 989 题、2Wiki 990 题。
- seed 固定为 20261018，按候选题型分布比例进行分层抽样，配额采用最大余数法。抽样只读取题目和题型元数据，不依赖模型结果或答案正确性。
- MuSiQue 配额：2hop=52，3hop1=24，3hop2=7，4hop1=11，4hop2=3，4hop3=3。
- 2Wiki 配额：compositional=41，comparison=24，bridge_comparison=24，inference=11。
- 生产实现、模型、检索和预算身份与上一轮一致，生产源码指纹为 `b5e0df1b620eb6942e40dc3e14dd2050d1e29f97ac422d784031a2e3a791edd9`。本轮只新增离线脚本与测试，没有修改生产实现。
- 每题最多 24 次模型调用、8 次检索、256000 累计预算、8192 输出、131072 上下文、600 秒；BM25 k=5，mainline max_steps=6。
- 40 个独立分片，每片 5 题，最多 8 个子进程并行。与上一轮相比，并发负载提高，供应商时延或服务限制可能影响执行状态；不会将分数差异直接归因于数据规模或某项代码修改。

两套新题仍采用 `sampled_pool`，没有扩大原语料池，不是官方全量语料或完整排行榜。完整父子 Agent 图、独立核验和结构化交接保持启用；图像、Wiki、记忆、skills、MCP、reranking、query expansion 保持禁用。图像向量接口之前返回 HTTP 402，本轮未再次调用该接口。

## 执行与恢复

离线脚本复用原有 manifest、CLI、运行器和评分器，位于 `scripts/evaluate_expanded_multihop.py`。例如为另一个新目录准备实验：

```powershell
.venv/Scripts/python.exe scripts/evaluate_expanded_multihop.py prepare `
  --output output/expanded-evaluation-next `
  --reference output/evaluation-20261018-204144 `
  --count 100 --workers 8 --shard-size 5
.venv/Scripts/python.exe scripts/evaluate_expanded_multihop.py run `
  --root output/expanded-evaluation-next --allow-remote
```

prepare 不联网，输出目录必须不存在，源文件必须匹配旧 pilot。run 核对当前模型与冻结身份后才启动真实调用，逐题持久化完整 attempts。结束的分片不重跑；续跑只补缺失题，不使用 `--retry-errors`，已有失败记录继续进入分母与成本。

协调器使用 `batch.lock`，各子运行使用 `run.lock`；不自动移除已有锁。中断后需先确认协调器和相应子进程已停止，保留故障与锁证据后再决定恢复；不能在活动写入期间启动第二个协调器。

汇总写入新目录：

```powershell
.venv/Scripts/python.exe scripts/evaluate_expanded_multihop.py score `
  --root output/expanded-evaluation-next --output output/expanded-evaluation-next/scored
```

缺失分片或题目仍保留在完整样本分母中，报告标记 incomplete，缺失题归 unknown。发生数据、代码、模型、预算或分片覆盖身份冲突则拒绝汇总。已经存在的评分目录禁止覆盖。

本轮另有只读完成处理器 `output/expanded-evaluation-20261018-100x2/finalize.py` 等待全部子运行结束，然后调用上述评分入口、核对旧输入不变并导出报告，不额外调用模型。

## 产物与评估语义

- `plan.json`、`manifests/`：新题清单、分层配额、旧题排除清单、语料与运行身份、40 个独立分片。
- `runs/`：每个分片的 run.json、attempts.jsonl、report.json。完整模型输入、父子交接、工具观察与失败尝试保留原文。
- `scored/<dataset>.json`：每套完整 100 题与各跳数/题型的 EM、F1、文档支持召回代理、执行失败、缺失与全尝试成本。
- `scored/success.jsonl`、`failure.jsonl`、`unknown.jsonl`：按自动主指标分组的逐题记录、题目指纹与原运行定位。
- `traces/success/`、`traces/failure/`、`traces/unknown/`：物理分组的完整逐题轨迹副本，不重写原始 attempts 或生产轨迹数据库。
- `evaluation-summary.json`、`REPORT.md`：最终结果、源文件完整性及本轮边界。

benchmark 的 success 指答案 EM=1，failure 指已有预测但未精确命中，unknown 指缺失预测。执行正常、内部核验、人工正确性与因果根因均为独立字段。预算耗尽、格式失败或服务异常不会被剔除，供应商 token 用量缺失时保持未知，不以本地预算估算代替计费量。

自动分组不等于人工审核；人工正确性、根因标签和语义失真率保持未知。完整轨迹可供后续复核，但不会直接转成生产 `quality review` 标签或训练样本。

## 已完成的离线验证

扩容专项及既有 runner 测试为 21 passed，日志位于 `output/expanded-evaluation-tests-20261018/pytest.log`。覆盖确定性分层、去重与旧题排除、分片交叉/遗漏拒绝、输入指纹变更拒绝、模型/预算混用拒绝、完整分母、失败成本、未知标签、禁止覆盖和远程调用显式开关。这些测试验证协议，不代表真实模型得分。
