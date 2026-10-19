# Existing Dataset Suite Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 优先复用本地 ChartQA、PDFVQA、MuSiQue 和 2Wiki 数据及历史真实输出，提供可重复的离线重评分和待复核记录。

**Architecture:** 新入口放在独立离线扩展，直接调用现有 manifest 校验和评分器。冻结题目、运行和预测身份，输出目录必须新建；不调用模型、不更改生产服务或业务数据。自动指标、执行状态、人工正确性和根因保持分离，不合并不同任务的准确率。

**Tech Stack:** Python、现有 benchmark 评分器、JSON/JSONL、pytest。

---

### Task 1: 冻结现有数据与运行清单

- Create: `eval/existing-datasets-v1.json`
- 固定四套本地数据、两套完整多模态历史运行、两个小型诊断运行及六个多跳运行。
- 记录 manifest、run.json、预测/attempts 的 SHA256；保留 sampled_pool、给定页面、历史模型等边界。
- 不下载新数据，不重采样，不替换失败运行。

### Task 2: 只读重评分与待复核清单

- Create: `src/deepsearch_experiments/existing_datasets.py`
- Create: `tests/test_existing_datasets.py`
- 先测试缺失/失败分母、子集范围、身份变更、不可覆盖、无网络、输入不变，再实现。
- 调用现有 `summarize` / `score_run`，按任务和分组输出；历史成本含全部重试，未知 token 保持 null。
- 逐题输出自动指标分组和输入定位，人工判定及根因默认 null；不伪造生产轨迹。
- 身份或输入校验失败保留拒绝报告，CLI 非零退出。既有原文件和报告不得覆盖。

### Task 3: 本地验收与使用说明

- Create: `docs/EXISTING_DATASETS.md`
- Modify: `README.md`, `docs/RELIABILITY_RECOVERY_QUALITY.md`
- Run: `.venv/Scripts/python.exe -m pytest tests/test_existing_datasets.py tests/test_public_multimodal_evaluation.py tests/test_multihop_runner.py tests/test_architecture.py -q --basetemp output/existing-datasets-tests/pytest-temp`
- Run: `.venv/Scripts/python.exe -m deepsearch_experiments.existing_datasets --catalog eval/existing-datasets-v1.json --output output/existing-datasets-20261018`
- Run: `.venv/Scripts/python.exe -m deepsearch_experiments.architecture`
- 记录实际分项结果与源文件不变校验，明确离线重评分不等于新模型运行或官方完整榜单；40 条未人工审核的虚构文档题仅作检索回归。

本轮在当前工作区直接执行，不提交、不新增工作树，不等待重复确认；已有业务服务和用户数据保持原位。
