# 多跳问答外部 Baseline 评测

## 当前范围

第一版接入 **IRCoT-adapted** 和 **LogicRAG-adapted**，使用作者公开的 MuSiQue / 2Wiki 数据格式。基准、方法、模型网关、检索器、运行记录和评分分离，不读取应用会话、Wiki 或工作记忆。

这不是原论文分数复现。两个方法是依据固定版本作者代码核对控制流程后的独立适配实现；IRCoT 可直接使用作者发布的 few-shot 提示词文件。没有复制 LogicRAG 的 GPL 源码或原提示词进项目。更换的模型、检索器、分句器、错误处理都记录在 `baselines.METHODS` 和每次运行的 `run.json` 中。

| 上游 | 固定版本 | 角色 |
|---|---|---|
| [IRCoT](https://github.com/StonyBrookNLP/ircot) | `3c1820f698eea5eeddb4fba3c56b64c961e063e4` | Apache-2.0；方法与提示词参考、处理后的题目格式 |
| [LogicRAG](https://github.com/chensyCN/LogicRAG) | `72a298c95ffd75890c7f9c28fd1c094201f669a7` | GPL-3.0；方法参考、作者已发布的问题和候选语料 |

代码许可不自动等于底层数据许可；下载文件保留来源与哈希，数据不加入 Git。

## 项目结构

```text
src/deepsearch_agent/benchmarks/multihop/
  schema.py       # AgentCase / Document 与 GoldCase 分离
  datasets.py     # IRCoT / LogicRAG 原始格式导入与采样
  upstream.py     # 固定 commit、有限大小下载与来源锁文件
  baselines.py    # 方法控制流程，不持有标准答案
  runtime.py      # 共享 BM25、模型调用、预算与事件
  runner.py       # 逐题持久化、重试留痕、严格续跑检查
  metrics.py      # EM/F1、文档级支持召回诊断
  compare.py      # 同条件对比 JSON / Markdown
  __main__.py     # 独立 CLI
data/benchmarks/multihop/   # 已忽略：下载资源、预处理数据
artifacts/multihop/        # 已忽略：运行与报告
```

## 两种数据协议必须区分

### IRCoT 处理后数据

`import-ircot` 读取作者 JSONL 的 `question_id / question_text / answers_objects / contexts`，另外要求显式提供语料 JSONL，每行包含 `title` 和 `paragraph_text` 或 `text`。不得从少量评测题的支持段落拼出所谓“全量语料”。

作者全量索引的文档数是 MuSiQue 139,416 / 2Wiki 430,225。`--corpus-scope full` 检查这个数量，但数量和哈希本身不能证明文件就是官方版本；来源仍需要核对。其他语料使用 `external` 或 `sampled_pool`。

当前没有自动下载 IRCoT 的大型 Google Drive 原始/处理后数据包，也没有把导入测试宣称为完整作者实验验收。原下载指引保存在下载的 `ircot/download/processed_data.sh`，只作文本参考，不执行脚本。

### LogicRAG 已发布数据

`import-logicrag` 直接读取作者 GitHub 的 JSON 数组。本机已下载并验证：

| 数据 | 原题数量 | 作者候选语料段数 | 已固定 pilot |
|---|---:|---:|---:|
| MuSiQue | 1,000 | 11,656 | seed=42，10 题 |
| 2WikiMultiHopQA | 1,000 | 6,119 | seed=42，10 题 |

**仅采样题目，不缩小作者语料池。** 协议始终为 `logicrag-released-v1`，语料范围为 `sampled_pool`，不能报告成 IRCoT 全量检索或全 Wikipedia 搜索。

导入检查了全部 1,000 题的支持段落。2Wiki 源文件中发现 9 条支持句索引越界：保留原题与可定位的文档级支持标签，在 `annotation_warnings` 记录警告；不修造句子标签，不静默删题。重复但内容一致的题内干扰段落允许合并；同标题内容冲突则拒绝。

## 使用命令

以下命令在仓库根目录的 PowerShell 中运行。可选依赖已安装到项目 `.venv`，新增 `benchmarks` extra 只有 `pysbd`。

```powershell
.venv/Scripts/python.exe -m pip install -e '.[benchmarks]'
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop --help
```

下载通过显式代理；**下载授权不等于模型调用授权**：

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop fetch --dataset references --output data/benchmarks/multihop/upstream --allow-network --proxy http://127.0.0.1:7897
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop fetch --dataset musique --output data/benchmarks/multihop/upstream --allow-network --proxy http://127.0.0.1:7897
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop fetch --dataset 2wikimultihopqa --output data/benchmarks/multihop/upstream --allow-network --proxy http://127.0.0.1:7897
```

导入示例，目标必须是新文件。当前已有 `pilot-10.json`，不要覆盖：

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop import-logicrag --dataset musique --questions data/benchmarks/multihop/upstream/logicrag/dataset/musique.json --corpus data/benchmarks/multihop/upstream/logicrag/dataset/musique_corpus.json --source-revision 72a298c95ffd75890c7f9c28fd1c094201f669a7 --split author-validation --limit 100 --seed 42 --output data/benchmarks/multihop/prepared/musique/pilot-100.json
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop inspect --manifest data/benchmarks/multihop/prepared/musique/pilot-10.json
```

云端运行以下命令会产生费用。已完成的首轮云端验收见 [云端验收报告](MULTIHOP_CLOUD_ACCEPTANCE_20261007.md)；以下是使用默认预算的命令示例，不是该轮运行命令。`--direct` 让云端模型不经过 7897。两种方法必须使用相同题目、模型、检索器、上下文限制和预算。示例参数是本项目 pilot 配置，不是作者原配置。

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop run --manifest data/benchmarks/multihop/prepared/musique/pilot-10.json --method logicrag --output artifacts/multihop/musique-logicrag --llm-env .env --allow-remote --direct
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop run --manifest data/benchmarks/multihop/prepared/musique/pilot-10.json --method ircot --prompt-file data/benchmarks/multihop/upstream/ircot/prompts/musique/gold_with_1_distractors_context_cot_qa_codex.txt --demo-count 4 --output artifacts/multihop/musique-ircot --llm-env .env --allow-remote --direct
```

`--demo-count 4` 明确选择文件中前四个训练示例，是适配设置，不是原文的 GPT-2 长度裁剪。需要作者指定顺序时，使用 `--demonstration-ids` 传入冻结的 JSON ID 数组。运行器拒绝示例 ID 与评测 ID 重叠。纯文本自定义提示词没有元数据，不能证明其训练来源；正式评测应使用作者带元数据的文件。

默认硬限制：最多 24 次模型调用、8 次搜索，单次输出 1,024 tokens；每题 300 秒。Token 预算以 UTF-8 字节估算输入并预留输出，收到服务商 usage 后对账；它不是精确 tokenizer 配额或费用保证。未知 usage 保留为 null，失败调用也计入尝试次数。IRCoT 的 stop 在客户端截断，未节省的云端生成同样计费。模型默认采样行为沿用当前网关，不声称严格随机可复现。

评分与比较全部离线：

```powershell
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop score --manifest data/benchmarks/multihop/prepared/musique/pilot-10.json --run artifacts/multihop/musique-logicrag --output artifacts/multihop/musique-logicrag-rescored.json
.venv/Scripts/python.exe -m deepsearch_agent.benchmarks.multihop compare --manifest data/benchmarks/multihop/prepared/musique/pilot-10.json --runs artifacts/multihop/musique-logicrag artifacts/multihop/musique-ircot --output artifacts/multihop/musique-comparison.json
```

## 结果与恢复

- `run.json`：数据、代码及依赖、方法来源、模型非敏感配置、提示词和预算身份。
- `attempts.jsonl`：逐题持久化；每次重试保留旧记录及成本。调用事件记录查询、返回文档 ID、模型 usage 和模型可见输出。
- `report.json`：所有题目参与分母，失败和缺失不排除。文档召回是诊断指标，不是证明模型理解了证据。
- `compare`：默认拒绝不同数据、模型、语料检索器、代码、预算或未完成样本的混合比较。显式 `--historical-reference` 仅豁免代码身份一致性，并在报告标记跨版本历史对照；不豁免其他条件。
- 同一命令加 `--resume` 仅补未运行的题；`--resume --retry-errors` 才重新尝试失败题。数据、方法配置或代码变化必须使用新目录。
- `run.lock` 防止并发写入。强杀进程后的遗留锁不自动删除，先确认旧进程已停止再人工处理。
- `--responses tape.json` 是无云调用的测试入口，格式为 `{question_id: [response, ...]}`，报告明确标记 `offline-plumbing-only`，不得作为准确率实验发表。

## 指标边界与下一步

预算控制消融新增 `mainline-budget-visible` 和 `mainline-budget-bounded` 两个方法名，
原 `mainline` 仍作为不启用新策略的对照。三组公共预算保持一致，策略与版本写入运行身份。
实现、边界及小样本验收见 [预算感知与有界停止](BUDGET_CONTROL_ABLATION_20261015.md)。
两数据集各 10 题、三组共 60 条结果已完成，详见
[预算控制配对 Pilot](BUDGET_CONTROL_PILOT_20261015.md)。该轮保持同版本同预算，
MuSiQue 有界停止与预算可见组 EM 持平，F1 和执行失败数改善；仍不作显著性结论。
该功能尚未默认开启到业务端，不将 canary 验收当成准确率改进结论。

已实现规范化答案 EM / token F1、显式别名、文档级支持召回，以及“未检索齐支持文档 / 检索到支持文档但答案错误 / 预算耗尽 / 执行失败”等观察性诊断。它们不是严格因果归因。

未实现完整官方 2Wiki 句子支持、证据三元组和 Joint 指标，也没有 MuSiQue-Full 不可答配对充分性指标。本版使用 MuSiQue-Ans。LogicRAG 原文的字符串包含准确率不冒充 EM；当前统一报告 EM/F1，不拿不同口径论文分数横比。

结构化输出适配和错误记录已完成修复及 [V2 云端重验收](MULTIHOP_CLOUD_REACCEPTANCE_V2_20261008.md)：正式 pilot 的 JSON 协议失败为 0，IRCoT / MuSiQue 仍有 4 次截断或内容过滤失败；中断恢复的潜在未落盘费用另行标为未知。应用 Agent 现已接入 `--method mainline`，直接执行真实研究图与工具循环，进展与结果见 [主线对照报告](MAINLINE_MULTIHOP_COMPARISON_20261008.md)。该配置固定 BM25，不包含图像、Wiki、记忆或混合重排。下一步仍需完善生成控制和调用级持久化，再扩大样本。拟研究的规则条件提取/校准门控尚未实现，不能声称已验证这些改进。DAG 标签需要独立标注，不能把 baseline 的运行轨迹自动当成推理金标准。

## 2026-10-07 离线阶段记录

- 新增五组测试覆盖作者格式导入、两种方法的调用顺序、预算、取消在途请求、标签隔离、失败分母、续跑身份、保留重试成本和同条件比较。
- 完整项目测试：`641 passed, 2 skipped`。首次沙盒运行中 5 项既有 Skill 测试因读取临时 `SKILL.md` 被拒绝而失败；单独及全量在获准权限下重跑均通过，未修改 Skill 业务代码。
- 作者 GitHub 下载资源共 12 个参考文件和 4 个数据文件，保存 commit、文件大小和 SHA256。没有执行上游下载脚本，没有把下载文件加入 Git。
- 两套 10 题 pilot 均已通过 `inspect`。MuSiQue 清单 SHA256：`832ac77d083c504f98233f80e76bddf5f673443753c11731e4658fd1c20e1ebd`；2Wiki：`877bde65f4ded597384142d19f37ba746d82df96fe0f9eaaf9266f7ab02f1e2b`。
- 两份 IRCoT 提示词各含 20 个元数据标记示例；选前 4 个时，与对应 pilot 的评测 ID 重叠数均为 0。
- 上述离线阶段没有启动真实云端推理。后续云端阶段已完成 4 次单题 canary 和 40 次 pilot 执行，详见 [云端验收报告](MULTIHOP_CLOUD_ACCEPTANCE_20261007.md)。离线脚本响应测试不作为准确率实验结果，也没有“本方法优于 baseline”的实验结论。
