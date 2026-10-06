# Badcase 归因标准 v1.1

本标准用于小批量、多变体配对实验。自动归因的单位是 `(case_id, variant)`：先按数据集官方口径判断回答是否正确，再从实际记录的阶段输入输出确定一个主标签，保留恢复过的失败和其他现象作为次标签。**阶段现象不等于已证明的根因。** 同页多元素问题中，命中页码也不等于命中了答案元素。

实现入口为 `deepsearch_agent.benchmarks.badcases.attribute_case` 和 `attribute_cases`。整个归因过程离线、确定性，无额外模型判官调用；重复处理相同输入得到相同输出。

## 评分与证据

- ChartQA：`relaxed_accuracy`，沿用项目已有 5% 数值相对误差及字符串规则；同时保留 exact match。
- PDFVQA A：`accuracy`。
- PDFVQA B/C：`element_exact_match`；元素 F1 仅作辅助，部分命中不能算整题正确。
- 请求错误、格式无效、拒答均为失败题，分母不剔除；空金标元素集合也不能让失败请求自动得分。
- `ranking` 是最终引用页；`evidence_ranking` 是已收集到工作流状态的证据页，不能据此假定完整内容实际传给了 Answerer。`retrieval_calls[].ranking` 是初始候选，`rerank_calls[]` 记录重排前后候选。
- `context_ranking` 记录最后一次 Answerer 实际文本上下文涉及的页；`original_image_pages` 单独记录实际送入模型的原图页。收集、上下文、原图、最终引用的金标页覆盖分别报告。旧字段 `gold_coverage.admitted` 保留为 `collected` 的兼容别名，不代表实际模型输入。
- `retrieved_element_ids`、`evidence_element_ids`、`context_element_ids` 分别记录检索、已收集、实际文本上下文中的元素 ID。覆盖率为命中的金标元素数除以金标元素总数。字段缺失或金标集合为空时为 `null`，不能当作 0。
- 主标签判断是否完全漏检时检查已观测的全部候选；`gold_coverage.final@k` 独立记录最终引用的 top-k 覆盖，不把金标恰好排在 k 之后等同于从未召回。
- `confirmed` 表示所描述的现象有日志或金标直接支持；`needs_review` 表示现有证据不足以细分。后者自动设置 `manual_review_required=true`。

## 主标签字典

主标签只取一个。表中“修复方向”是排查入口，不能当作修复后一定提升的因果结论。

| 稳定代码 | 阶段 | 自动触发条件 | 排除条件 | 必需证据 | 修复方向 |
| --- | --- | --- | --- | --- | --- |
| `success` | complete | 结果有效且任务主指标为 1 | 请求或格式失败 | 预测、金标、评分口径 | 保留为正例与恢复成功样本 |
| `missing_result` | evaluation | 评测清单存在该题但无对应结果 | 有结果但运行失败 | 题目清单、缺失记录 | 检查调度、恢复和输出落盘 |
| `schema_data_failure` | data | 明确的数据集/清单验证错误，或题目 ID/任务不匹配 | 模型 JSON 格式错、一般 ValidationError | 标准错误类型或 ID/任务字段 | 修复清单生成与校验 |
| `asset_failure` | asset | 终止运行的缺失图片、校验和或图片解码错误 | 已恢复的单次工具失败 | 终止状态与明确错误类型 | 修复下载、路径、校验与解码 |
| `transport_failure` | runtime | 请求因明确的网络、超时、限流、HTTP 错误终止 | 后续同角色成功或最终回答正确 | 行级错误类型 | 调整超时、重试、并发和服务配置 |
| `budget_exhausted` | budget | 预算错误终止，或图记录预算停止且没有有效答案 | 仅出现预算提示，但已输出有效答案 | 终止错误或 `rag.outcome.stop_reason` | 检查调用重复、步数与证据压缩；再考虑预算 |
| `model_format_error` | output_format | 最终预测违反任务 schema，或图明确输出 schema_validation_failed | 有明确拒答、Verifier 丢弃正确草稿 | 预测解析状态或图终止事件 | 检查结构化输出及任务接口 |
| `tool_argument_invalid` | tool | 无效参数/未知工具造成最终 ToolError，最后一次工具调用失败 | 重试成功、模型仍产生答案 | 行级 ToolError、末次工具错误码 | 改进工具 schema、规划器参数与反馈 |
| `tool_execution_failure` | tool | 工具执行失败成为最终 ToolError | 被恢复或仅为附带失败 | 行级 ToolError、末次调用状态 | 排查工具实现和依赖；保留重试语义 |
| `retrieval_miss` | retrieval | 确认做过检索，全部已观测候选和后续证据均无金标页 | 从未记录检索、重排前已召回、部分金标页命中 | 初始检索日志、金标页 | 检查索引、查询、切片和检索范围 |
| `rerank_drop` | rerank | 重排前命中金标，重排后完全丢失，最终也未恢复 | 后续读页/检索已补回金标 | 重排前后排名、最终证据 | 检查重排输入、top-k 截断和评分 |
| `context_selection_loss` | context_selection | 已收集金标页，但观测到的实际文本上下文和原图输入均无金标页，且最终有效答案错误 | 没有实际上下文/图片输入记录；金标原图实际已提供；拒答已有主标签 | 收集页、实际文本上下文页、实际原图页 | 排查上下文截断、过滤及模型输入构造 |
| `evidence_selection_loss` | evidence_selection | 已召回/收集金标页，最终引用却完全不包含它 | 拒答或核验拒绝已有更明确标签 | 中间候选/证据、最终引用 | 检查证据筛选、上下文预算与引用选择 |
| `generation_reading_or_reasoning` | generation | 已提供/最终引用金标页，回答仍错误 | 更明确的运行、格式、选择、核验失败 | 金标页覆盖及错误答案 | 人工区分图像读取、OCR、计算、推理、问题理解 |
| `verifier_rejected_correct` | verification | 最终草稿只有一个可解析任务预测，该预测得分正确，但对应 claim 未被 supported 接受，最终答案错误/缺失 | 更早草稿正确但最新草稿错误；多个可解析预测有歧义 | 最后草稿/终止 claims、supported、金标 | 复核引用支持关系与拒绝理由；不自动关闭核验 |
| `abstention` | abstention | 明确 abstained，或图有空草稿/无支持 claim 导致无法给出有效预测 | 已确定预算停止或拒绝了正确草稿 | 解析状态，或完整终止 claims/supported | 区分合理证据不足与过度保守 |
| `unknown` | unknown | 日志不足、错误类型不明确、无可证明的阶段条件 | 满足其他标签的证据充分 | 现有结果与缺失字段 | 补 trace 或人工复核，不能强行分配 OCR/检索原因 |

判定顺序是：缺失/数据标识错误 → 成功 → 终止运行错误 → 核验拒绝正确草稿 → 无答案且预算耗尽 → 拒答 → 格式失败 → 实际上下文丢失 → 重排丢失 → 完全漏检 → 最终证据丢失 → 有金标页仍答错 → 未知。拒答仍是结果主标签，但已观测的上游漏检/重排丢失必须另存次标签，不能在阶段分析中消失。早期工具出错但后来恢复，不占用主失败标签。

`verifier_rejected_correct` 的“正确”仅指基准任务评分正确，不代表该 claim 引用一定充分；这一类别始终设置 `manual_review_required=true`。`evidence_level=confirmed` 只确认“金标正确草稿未被接受”的现象，人工仍需核查 Verifier 是否合理拒绝了碰巧猜对的答案。`generation_reading_or_reasoning` 固定为 `needs_review`，禁止自动进一步贴 `OCR错误`、`视觉理解错误` 或 `推理错误` 标签。

`agent_no_visual` 是实验干预。实际原图为空时记录 `visual_input_disabled`，只能说明关闭了原图输入，不能单独证明错误由缺图、OCR 或视觉推理导致。如果文本上下文仍包含金标页，错误继续留在待人工细分的生成阶段；如果金标页仅被收集、实际文本与图片上下文均缺失，才使用 `context_selection_loss`。

## 次标签

| 稳定代码 | 含义和限制 |
| --- | --- |
| `recovered_tool_failure` | 同工具后续成功，或该题最后答对。表示恢复现象，不代表失败没有增加耗时。 |
| `tool_failure_observed` | 记录到工具失败，尚不能证明它造成最终错误。 |
| `recovered_model_failure` | 同角色/阶段后续成功，或最终回答正确。 |
| `model_failure_observed` | 模型调用失败，但无足够证据作为终止原因。 |
| `recovered_rerank_drop` | 某轮重排丢掉金标，但后续证据或最终引用重新包含金标。 |
| `retrieval_miss_observed` | 已完成检索的全部已观测候选及后续证据均无金标页。拒答时仍保留此现象，不声称已证明拒答因果。 |
| `rerank_drop_observed` | 重排前有金标、重排后丢失且未观察到恢复。可以与拒答主标签并存。 |
| `verifier_accepted_wrong` | Verifier 接受的可解析 claim 按金标为错。模型自评通过不是事实正确。 |
| `partial_gold_coverage` | 已收集部分而非全部金标页；不是完全漏检。 |
| `incomplete_text_element_coverage` | PDFVQA 金标文本元素未完全覆盖；原图仍可能足够作答，不直接证明检索/OCR失败。 |
| `incomplete_context_element_coverage` | 实际 Answerer 文本上下文缺少部分金标元素；与完整收集记录分开，原图仍可能足够作答。 |
| `visual_input_disabled` | no_visual 消融分支实际没有发送原图，是实验干预记录，不是 OCR/推理因果标签。 |
| `budget_limit_observed` | 观察到预算停止事件，但它可能只影响补查。 |
| `oracle_correct_rag_failed` | 同题直接给定相关页的对照答对、当前 RAG 答错。仅为配对差异，不能据此单独断言 OCR 或检索因果。 |

次标签可以出现在成功题中。统计恢复率、工具错误率时以实际调用次数或题数为分母，不能混用。

## 输出与人工复核

每条注释保存 `schema_version=1`、`taxonomy_version=1.1`、`id`、`variant`、任务评分、主阶段、证据等级、具体 JSON 路径、次标签和覆盖率。`automatic` 保存完整自动判断副本；`expert_override` 初始为 `null`，不能把未发生的人工审阅标记为已完成。

人工复核采用以下独立结构，保留原始自动结果，不直接覆盖它：

```json
{
  "expert_override": {
    "schema_version": 1,
    "reviewer": "reviewer_id",
    "reviewed_at": "ISO-8601 timestamp",
    "primary_code": "generation_reading_or_reasoning",
    "mechanism": "ocr_transcription_error",
    "evidence_level": "confirmed",
    "evidence": ["/trace/tool_calls/2/data", "asset:page_1.png"],
    "rationale": "State the observed discrepancy and how it affects the answer.",
    "counterfactual_checked": false
  }
}
```

`mechanism` 的人工允许值建议为 `ocr_transcription_error`、`visual_reading_error`、`calculation_error`、`reasoning_error`、`question_misinterpretation`、`evidence_support_mismatch`、`annotation_issue`、`undetermined`。必须有原图/标准文本/计算过程等可核对证据才能细分；没有反事实修复并重跑时，`counterfactual_checked` 必须为 false。人工纠正基准标注时单列记录，不悄悄改金标或重算主报告。

复核顺序：先确认题目、模型配置、原始输出和金标对应；再核对最终输入包含什么；沿时间顺序检查候选、重排、工具返回、草稿和核验。优先审查变体间同题胜负翻转、Verifier 拒绝正确草稿、工具未恢复失败和 `needs_review` 项。报告区分自动统计、人工确认和待审阅，不能把小样本诊断升级为显著性结论。

## 验证

运行 `python -m pytest tests/test_badcases.py -q`。测试覆盖任务评分口径、失败请求的空元素防误判、恢复事件、候选与重排区别、最终证据损失、格式失败、拒答、Verifier 两类错判、元素覆盖和缺失 trace。归因代码不读取 API 密钥、不请求外部模型、不把原始未受控输出当成可执行指令。
