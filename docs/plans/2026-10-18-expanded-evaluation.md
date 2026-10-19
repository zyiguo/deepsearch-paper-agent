# Expanded Evaluation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 从已下载的 MuSiQue/2Wiki 各 1000 题中新增评测各 100 题，保留原 220 题结果。

**Architecture:** 只增加离线批处理脚本，不修改生产实现或评分公式。分层、去重并排除旧 pilot，冻结两个 100 题 manifest；每 5 题一个独立子运行，最多 8 个进程并行，逐题仍使用原应用图和原预算。汇总先逐分片验证数据、代码、模型与运行身份，再调用原评分器按每套完整 100 题计分。

**Tech Stack:** Python、现有 multi-hop CLI/manifest/scorer、ThreadPoolExecutor 管理独立子进程、JSON/JSONL、pytest。

---

### Task 1: 准备可复现的新增题集

- Create `scripts/evaluate_expanded_multihop.py`、`tests/test_expanded_evaluation.py`。
- 固定 seed，MuSiQue 按跳数、2Wiki 按题型分层比例抽样；排除旧题 ID 和重复问题文本。
- 检查原始 questions/corpus 指纹与旧 manifest 一致，维持原语料池与标签规则；manifest/plan 输出禁止覆盖。
- 测试采样确定性、排除旧题和分层范围、题量不足拒绝、分片完整无交叉。

### Task 2: 运行与汇总

- 复用既有 CLI 执行 mainline；预算沿用 24 次模型调用、8 次检索、256000 tokens、8192 输出、131072 上下文、600 秒/题。
- 每个运行有独立目录/锁/完整 attempts；新失败不自动重试。续跑只补未保存题，已有锁不自动删除。
- 汇总调用 `read_attempts`、`score_run` 和 `evaluate_predictions`，核对分片身份及完整题目覆盖；缺失保持未知且进入分母。
- 成功/失败/未知导出分别写入新目录，人工结论与语义失真率不自动生成。

### Task 3: 真实扩容验收

- 用项目环境运行专项测试，临时目录先创建父目录；保留日志。
- 准备和冻结各 100 题、40 个 5 题分片，执行实际 MiMo-V2.6-Pro 请求。
- 汇总两套指标、分层指标、失败及成本；核对所有源输入和旧结果未变化，写明与旧 10 题不重叠。
- 保留原 ChartQA/PDFVQA 的 200 题结果，本轮不重复支付调用；图像向量接口已知 HTTP 402，不重试该项。

用户已授权扩大并实际测试，本轮直接执行，不提交、不创建新工作树，不重复请求批准。
