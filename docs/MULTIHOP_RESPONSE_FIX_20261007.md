# 结构化响应适配与错误记录修复

## 已修复内容

### JSON 包装适配

LogicRAG 使用独立的 `structured.py`，接受纯 JSON 对象、单个 JSON/无语言代码块，以及含说明文字的唯一 JSON 对象。仅进行确定性包装提取，不补全截断 JSON、不更改字段值、不调用模型修复。

多个候选对象、多个代码块、重复键、非对象根节点、非法 JSON、NaN/Infinity、过大或过深输入均失败。布尔类型、非空依赖、索引有效性和 DAG 无环性仍由方法层严格验证。

方法身份新增 `response_protocol=single-json-object-envelope-v2`。`details.trace` 中的 `structured_response` 事件记录 `stage / envelope / error_code`；失败结果另含 `failure_stage / failure_code`，可区分包装解析失败与业务字段/依赖图失败。

### 模型错误诊断

`ModelServiceError` 保留原有异常类型、中文安全消息及应用层 `status_code`，新增白名单诊断字段：

| 字段 | 含义 |
|---|---|
| `error_code` | `timeout`、`connection_error`、`authentication_error`、`permission_denied`、`rate_limit`、`server_error`、`request_rejected`、`invalid_json`、`invalid_response`、`empty_response`、`output_truncated`、`content_filtered`；未知为 `model_service_error` |
| `provider_status_code` | 上游 HTTP 状态，与应用层返回状态分离；没有 HTTP 响应时为 null |
| `retryable` | 仅标记可能暂时性的传输/服务错误；不触发自动重试，也不保证再次请求成功 |
| `finish_reason` | 仅允许已知枚举值，未知值不写入诊断 |
| `prompt_tokens / completion_tokens / reported_total_tokens` | 保留服务端返回的非负整数，拒绝布尔值和非法类型 |

runtime 模型事件和 runner 错误详情保存上述诊断；模型事件新增 `latency_ms`。取消记录为 `cancelled`，中断为 `interrupted`，其他异常为 `unexpected_error`。不将异常消息、服务端错误正文、URL、请求头或密钥写入错误诊断。

### 失败用量对账

网关现在先读取可用 usage，再判断响应截断、空响应或格式异常。失败且有总 usage 时，runtime 使用真实总量替换预留估算；未知总 usage 仍保留 null 和预留记账，不从部分字段推算总量，不计作零费用。

失败状态不变，失败题仍计 0 分。服务端没有返回的 usage 无法恢复；旧模型服务错误的细分原因也不能事后补造。未改变模型请求参数、思考模式、预算、检索策略或重试策略。

## 离线验证

- 最终全量回归：**704 passed, 2 skipped**（30.75 秒），相较修复前新增 63 个测试用例；本轮没有收费云调用。
- 新增解析、错误诊断、费用对账和持久化测试；覆盖同步请求与实际评测使用的可取消异步请求。
- 对首轮保存的 5 个 `dependency_graph` 失败响应进行离线回放，**5/5 提取成功，5/5 依赖图校验通过**。
- 回放没有继续 Agent 轨迹，没有发出云请求，不产生新答案，不更新 EM/F1。不能将其解释为 5 道题已答对。
- 8 个历史运行的 24 个产物 SHA256 不变；8 份历史报告重评分完全一致。
- 回放证据：`data/benchmarks/multihop/runs/cloud-20261007-pilot10/structured-replay-v2.json`。

运行离线测试：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_multihop_structured.py tests/test_llm_error_diagnostics.py tests/test_multihop_runtime.py tests/test_multihop_runner.py -q --basetemp data/test-tmp/response-fix-review
```

## 实验边界

代码指纹已改变，旧运行目录的 `--resume` 身份检查会拒绝新实现。新云端验收需使用新目录，冻结统一模型与预算后完整运行，不能只把旧失败题替换为新结果。

首轮报告 [MULTIHOP_CLOUD_ACCEPTANCE_20261007.md](MULTIHOP_CLOUD_ACCEPTANCE_20261007.md) 保留原结论。上述离线修复阶段未证明云端执行成功率或问答准确率提高，也未解决此前的 600 秒时限耗尽问题。后续已完成 [V2 云端重验收](MULTIHOP_CLOUD_REACCEPTANCE_V2_20261008.md)，其结果、残留失败及中断恢复成本边界单独记录，不改写本次离线回放结论。
