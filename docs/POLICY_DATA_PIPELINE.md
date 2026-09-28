# 搜索策略轨迹与后训练数据管线

此模块只负责采集与整理数据，不加载模型、不配置云端、不训练，也不评价真实模型效果。默认关闭记录。

## 1. 模块边界

```text
src/search_policy/                   Python 标准库，可脱离 RAG 使用
  intent.py                         search/answer 协议、客户端接口、决策事件
  events.py                         ContextVar 隔离每个运行的 observer
  store.py                          SQLite 事件日志、哈希链、快照校验
  dataset.py                        候选、审核、去重、分组切分、清单校验
  training.py                       向训练器提供单步样本/编码函数适配
  __main__.py                       独立 CLI

src/deepsearch_agent/
  search_intent.py                   旧导入兼容层，转换 RAG 的服务异常类型
  observability.py                  可选事件回调及片段序列化
  trajectory_adapter.py             RAG 到独立轨迹库的唯一存储桥接层
```

独立包不导入 `deepsearch_agent`、LangGraph、FastAPI、HTTP 客户端或 Torch。RAG 的 AnswerService/ResearchWorkflow 只接收可选 recorder/observer；没有 recorder 时不会创建数据库。旧 `deepsearch_agent.search_intent.SearchIntent` 入口继续可用。

`MessageClient.complete_messages(messages) -> str` 是推理接口；独立实现应将网络失败转换为 `PolicyServiceError`。既有 RAG 兼容入口另外接受 `ModelServiceError`。配置模型身份时明确传 `model_id` 与 `model_revision`，后者应填写部署检查点/adapter 的版本或哈希，而非只写基座名称。

## 2. 先离线跑通

在项目根目录使用 PowerShell。下面命令仅使用虚构模型与虚构检索结果，不读真实论文，也不发起网络请求。输出目录必须是新的目录。

```powershell
.venv\Scripts\python.exe examples\policy_trace_demo.py --output output\policy-demo
.venv\Scripts\python.exe -m search_policy inspect --db output\policy-demo\traces.sqlite3
.venv\Scripts\python.exe -m search_policy verify output\policy-demo\dataset
```

得到 `traces.sqlite3`、`raw.jsonl`、`candidates.jsonl`、待审核模板、演示审核记录，以及 `dataset/train.jsonl`、`val.jsonl`、`manifest.json`。仅这个显式标记为 synthetic 的演示脚本会批准自己的虚构样本，生产导出没有自动批准机制。不要混入正式训练集。

也可在仅有 Python 的环境中把本仓库 `src` 加入 `PYTHONPATH`，运行 `python -S -m search_policy --help`；或者单独复制 `src/search_policy` 到训练项目的可导入路径。

## 3. 开启本地 RAG 记录

无需修改 `.env`、`.env.embedding` 或云端设置。启动服务前设置本地环境变量：

```powershell
$env:RAG_TRACE_DB = Join-Path (Get-Location) 'data\policy-traces\runs.sqlite3'
.venv\Scripts\python.exe -m uvicorn deepsearch_agent.api:app --host 127.0.0.1 --port 8000
```

取消记录后重启服务：

```powershell
Remove-Item Env:RAG_TRACE_DB -ErrorAction SilentlyContinue
```

这只启用事件记录，不启用新的模型。当前没有策略模型时，RAG 运行仍会记录，但 policy 候选样本数量为 0，这是预期行为。基础非图问答仅记录请求配置和最终答案；完整角色/检索事件来自 DeepSearch 图路径。

程序化接入（可以使用本地 mock）：

```python
from pathlib import Path
from deepsearch_agent.api import create_app
from deepsearch_agent.search_intent import SearchIntent

class LocalStub:
    def complete_messages(self, messages):
        return '{"search":"检索器训练方法"}'

policy = SearchIntent(LocalStub(), model_id="local-fixture", model_revision="fixture-v1")
app = create_app(search_intent=policy, trace_path=Path("data/policy-traces/runs.sqlite3"))
```

若直接使用服务类：`AnswerService(..., search_intent=policy, run_recorder=RAGTraceRecorder(path))`，调用时传 `trace_context={"conversation_id": "稳定的会话ID"}`。Web 会话接口自动传会话 ID；无会话的 `/api/ask` 按规范化问题分组。

## 4. 记录内容与状态

每条运行具有 `schema_version`、`run_id`、请求元数据、状态和有序事件。事件含 UTC 时间、序号、前一事件哈希及自身哈希。哈希用于发现意外修改与缺失，不是数字签名。

| 事件 | 内容 |
|---|---|
| `run.started` | 问题、历史、会话、文献范围、记忆上下文、top_k |
| `policy.request` | 实际发送的完整 messages、模型/协议身份、call_id |
| `policy.response` | 原始返回文本、解析/失败状态、耗时；不保存服务异常正文 |
| `search.plan` | 实际查询来源 policy/researcher、轮次、关联 policy_call_id |
| `retrieval.config` | 检索模式、融合权重、ANN 阈值、分块/嵌入身份、上下文预算 |
| `retrieval.request/candidates/result` | 实际查询、候选/重排后的片段内容和顺序、内容ID、论文页码 |
| `rag.request/response/parsed` | 主模型各角色的实际提示、原始输出、解析结果 |
| `rag.outcome` | 引用/核验处理后的结论、支持项、缺口、证据、是否完整 |
| `answer.final` | 最终返回的答案与来源 |
| `run.error/finished` | 异常类型及终止状态 |

记录实际检索片段与角色所见上下文，而不是完整知识库备份；未保存所有向量、索引或模型生成概率，因此不能据此宣称完整复现 ANN 或做离线 on-policy GRPO。后续若需要重跑检索，应另行保存相应索引版本。

逐事件提交；正常返回为 `completed`，程序异常为 `error`，记录中途失败为 `recording_error`，进程崩溃/无法写入结尾留下 `running`。`completed` 只表示调用正常返回，不代表回答或搜索策略正确。验证器判定单独保留，永不自动转成 reward。

SQLite 每次写入独立连接/事务；ContextVar 按请求绑定，结束后恢复。写盘失败只打印固定诊断码，不中断问答。记录的是本地原文数据，包括用户问题和论文片段，应保存在已忽略的 `data/` 下；不会写入 API key 或上传到第三方追踪服务。

## 5. 导出、审核与冻结

```powershell
.venv\Scripts\python.exe -m search_policy inspect --db data\policy-traces\runs.sqlite3
.venv\Scripts\python.exe -m search_policy raw --db data\policy-traces\runs.sqlite3 --output output\policy-raw-v1.jsonl
.venv\Scripts\python.exe -m search_policy candidates --traces output\policy-raw-v1.jsonl --output output\policy-candidates-v1.jsonl --review-template output\policy-reviews-v1.jsonl
```

候选记录包含输入、输出、后续观察和来源哈希；审核模板只包含 eligible 项，初始 `decision="pending"`。对照候选文件审核，把合适样本改成 `approve`，填写非空 `reviewer`；不合适改成 `reject`。不要改 `sample_id` 或 `sample_hash`。

```json
{"sample_id":"来自模板的ID","sample_hash":"来自模板的哈希","decision":"approve","reviewer":"your-name","note":"已检查查询与用户问题相符"}
```

审核粒度是**搜索决策**，不是最终问答正确性。第一版只允许完整运行中已实际执行并返回结果的合法 search 动作；answer/UNKNOWN、解析失败、回退 Researcher、未完成搜索或错误运行都不能批准为 SFT 样本。检索返回空结果不自动说明决策好坏，仍由审核判断。

```powershell
.venv\Scripts\python.exe -m search_policy export --traces output\policy-raw-v1.jsonl --reviews output\policy-reviews-v1.jsonl --output output\policy-dataset-v1 --val-ratio 0.2 --seed 42
.venv\Scripts\python.exe -m search_policy verify output\policy-dataset-v1
```

导出会检查审核哈希、重复/未知审核、冲突标签；无批准样本时报错且不创建训练集。已有文件/输出目录拒绝覆盖。以规范化问题与会话的连通分组做确定性切分，再按输入与目标去重，保留所有来源。小数据量可能出现空验证集；不会为了凑比例拆开同一组。

CLI 支持 `--db` 或 `--traces` 二选一；正式审核推荐先冻结原始 JSONL，再以该快照生成候选与数据集。离线机器只需快照、审核文件和独立包，不需要访问 RAG 数据库。

`manifest.json` 保存格式版本、seed、比例、计数、轨迹快照身份、审核身份以及文件 SHA256。冻结后的同一快照与审核记录，无论输入顺序如何，导出字节一致。增量数据可能连接原本独立的会话组；正式实验应冻结数据版本与切分，不在训练中途重导出覆盖。

## 6. 对接“后训练简单版”

SFT 行格式为：

```json
{"id":"...","messages":[{"role":"system","content":"..."},{"role":"user","content":"..."}],"text":"{\"search\":\"检索词\"}","loss_scope":"completion_only","group_id":"...","provenance":[]}
```

`messages` 精确保留模型决策当时的输入，`text` 保留当时的模型输出。后续检索结果只进入审核上下文，不倒灌进该动作的输入；其他角色输出不算策略模型生成 token。

训练项目现有 `search_task.encode_step` 接口可以复用：

```python
from search_policy.training import encode_sft_steps
from search_task import encode_step

# tokenizer 由云端训练项目加载；此管线不负责加载模型。
train_items = list(encode_sft_steps(
    "policy-dataset-v1", "train", tokenizer, max_length=4096, encode_step=encode_step
))
```

该适配器只传递已校验的 messages/text；编码器负责 chat template、EOS、长度拒绝，以及提示/观察 token 的 `-100` 掩码。产出的 `sample_id/question_id` 是训练元数据，应在传给模型前由 collator 去除。沿用已导出的 train/val，不再次按动作随机切分。

**没有修改外部训练项目，也没有自动接入其训练命令。** 原 `load_bundle/sft_steps` 要求 corpus/train/val/test、标准答案与完整动作回放，不能直接把此文件当作该 bundle。需要在训练入口选择单步 SFT reader。GRPO 仍需可运行的检索环境和独立奖励定义；原始日志为后续标注/环境构建保留材料，不伪造黄金答案、reward 或旧策略 logprob。

## 7. 本地验证

```powershell
$testTemp = Join-Path (Get-Location) ('tmp\policy-tests-' + [guid]::NewGuid().ToString('N'))
.venv\Scripts\python.exe -m pytest tests\test_policy_pipeline.py tests\test_search_intent.py tests\test_research_graph.py --basetemp $testTemp -q
```

覆盖协议、并发隔离、失败/中断轨迹、哈希损坏、审核门禁、去重与切分、无 RAG 依赖导入、真实 RAG 控制流中的模拟模型采集。只验证软件与数据契约，不代表真实模型效果。
