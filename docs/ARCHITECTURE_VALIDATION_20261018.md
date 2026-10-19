# 架构整理验证记录（2026-10-18）

本记录验证代码结构、数据契约与离线行为，不作为模型效果评测。测试中的模型/云接口使用合成或模拟响应；现有用户资料只在副本上读取。

后续新增团队身份与空间隔离后的最新完整回归为 **803 passed / 2 skipped**，详见 [团队模式验证记录](TEAM_ACCESS_VALIDATION_20261018.md)。下表保留架构整理及第一轮容器验收时的结果。

再后引入 PostgreSQL 的完整回归为 **821 passed / 2 skipped**；包含真实 PostgreSQL、Redis 和容器故障恢复，详见 [数据库验收记录](DATABASE_VALIDATION_20261018.md)。

## 结果

| 检查 | 结果 | 证明边界 |
|---|---|---|
| 后端完整回归 | **791 passed，2 skipped**（后续真实 Redis 复验） | 含原业务、Agent 隔离/交接/证据授权、预算/裁剪、独立核验、取消/恢复、并发、模型诊断、记录失败、图像/文献、会话/记忆/Wiki，以及真实 Redis 集成 |
| 前端单元测试 | **10 个测试文件，57 passed** | 保留原请求、会话、记忆、文献、流式、技能与证据映射测试 |
| 前端构建 | TypeScript + Vite 成功 | feature 目录、共享 API、文献 hook/侧栏与引用路径完整，56 个模块完成打包 |
| 依赖审计 | **169 个 Python 模块、388 条边、0 循环、0 违规边** | 后续新增独立离线容器验收入口；包含延迟导入；核心/轨迹仅标准库；业务不导入训练/评测或轨迹存储；应用用例不导入 HTTP 层 |
| 独立运行 | `python -S` 子进程通过 | 无 Web、LangGraph、模型 HTTP SDK 时，共享契约、轨迹记录/查询/分类与 search_policy 训练接口可用 |
| 生产加载范围 | 独立进程启动演示 API、入库及问答通过 | 不加载 `deepsearch_experiments`、`search_policy`；记录关闭时不加载轨迹数据库实现 |
| HTTP 兼容 | **37 条路径，OpenAPI 完全相同** | 重构前源代码生成的 schema 与当前逐项比较，无路径、请求/响应模型或操作定义差异 |
| 旧类路径与格式 | 合成旧版本索引、会话、轨迹、pickle 和检查点通过 | 原文/证据 ID/哈希链可验证；仅保留 `deepsearch_agent.chunking.TextChunk` 的最小别名 |
| 真实图断点续跑 | 重构前源码生成的 Agent 检查点在新主线继续执行通过 | 冻结位置为子研究完成、父调度未执行；新主线完成起草与独立核验；旧夹具校验和不变 |
| 资源生命周期 | 运行线程关闭与启动中途失败回归通过 | 先取消/等待会话线程，再关闭其他资源；构造技能失败时，已创建文献执行器、检查点连接与模型均关闭 |
| 显式旧配置 | 迁移错误回归通过 | `AGENTIC_RAG_ENABLED=false` 在创建真实模型客户端前拒绝启动；搜索策略注入也提示离线入口 |
| 轨迹端到端合成演示 | **4 条运行：success 1 / failure 2 / unknown 1；3 次子任务交接** | 使用真实应用图和本地合成模型响应，不代表真实模型正确率 |
| 搜索策略合成演示 | 12 条运行，8 train / 4 val | 保留审核、去重、分组切分和清单哈希；合成批准不是人工标注 |
| CLI | 10 类模块入口 `--help` 回归通过 | 含评测、消融、DAG、多跳、通信导出、MCP 和 search_policy；真实 MCP 本地 stdio 回归也通过 |
| Python 分发包 | wheel 构建及内容检查通过 | 168 个 Python 模块、4 个包根与 `logging.json` 均包含；被删除旧客户端/配置/直接 PDF 实现均不包含 |
| 容器与真实 Redis | **镜像构建、非 root 启动、真实缓存/Lua/TTL、HTTP 限流、故障恢复、API 重启持久化全部通过** | Docker 29.5.2、Redis 7.4.11；合成业务输入，未调用付费模型；详细记录见 [后续验收](CONTAINER_REDIS_E2E_20261018.md) |
| 代码基础检查 | 编译与 `git diff --check` 通过 | 无语法/差异空白错误；Git 仅提示既有 Windows 行尾规范 |

重构前基线为 769 passed / 3 skipped。删除了 5 个仅验证旧 AppConfig 的测试；新增 22 个架构/兼容/入口/生命周期用例，以及 1 个生产拒绝离线策略注入的用例，因此本轮为 787 passed / 3 skipped。旧 OpenAI 客户端和 PDF 读取器的有效测试迁入当前实现，并未作为回归失败被删除。

后续容器验收新增 2 个输出/清理安全测试和 1 个 DNS 总等待期限回归测试；启用真实 Redis 后，原跳过的 Redis 集成测试也实际执行，最新结果为 791 passed / 2 skipped。上表 wheel 的 168 模块结果属于此前打包验证；后续新增的离线入口已在本次 Docker 可编辑安装和实际运行中验证，没有重新构建并验收独立 wheel。

## 现有数据副本检查

对以下 6 个现有文件，先记录 SHA256，复制或使用 SQLite 只读连接备份到 `tmp/existing-data-compatibility/`，再使用新模块读取。检查后原文件 SHA256 全部一致；没有调用模型或修改其配置身份。

| 类型 | 实际读取 |
|---|---|
| JSON 索引 | 3 份；2 / 18 / 18 个文本片段，逐片段结构与内容身份校验通过 |
| SQLite 会话 | 3 个会话、3 个历史轮次读取成功 |
| SQLite 检查点 | 当前文件为空；仅验证副本打开，**不以此宣称真实历史任务续跑成功**；非空旧检查点续跑由上面的旧源码夹具验证 |
| 既有通信演示轨迹库 | 4 条记录，哈希链校验通过；重新分类保持成功 1、失败 2、未知 1 |

本地详细清单为 `tmp/existing-data-compatibility/report.json`，含原文件校验和；未把用户资料内容复制进文档。提交用旧格式夹具位于 `tests/fixtures/architecture_v1/`，全部由虚构文本和重构前源码生成，包含旧类路径、缺少新分类事件的轨迹，以及完整冻结 HTTP schema。

## 可重复命令

在仓库根目录执行：

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest tests/test_architecture.py -q
.venv/Scripts/python.exe -m deepsearch_experiments.architecture
.venv/Scripts/python.exe -m compileall -q src examples scripts

git diff --check

.venv/Scripts/python.exe examples/record_communication_demo.py --output output/architecture-communication-next
.venv/Scripts/python.exe -m deepsearch_tracing.communication inspect --db output/architecture-communication-next/traces.sqlite3
.venv/Scripts/python.exe examples/policy_trace_demo.py --output output/architecture-policy-next

.venv/Scripts/python.exe -m pip wheel --no-deps --no-build-isolation --wheel-dir tmp/architecture-wheel-next .

docker compose config --quiet
```

演示输出目录必须是新目录；不要复用已包含审核或导出文件的目录。本次实际新输出位于 `output/architecture-communication-20261018-01/` 与 `output/architecture-policy-20261018-01/`，旧输出均保留。

前端常规命令：

```powershell
cd frontend
npm test
npm run build
```

本机 npm 包装器引用了缺失的用户全局路径，验证时使用 `C:/Program Files/nodejs/node.exe` 直接运行同目录 `node_modules/npm/bin/npm-cli.js` 执行上述脚本，未修改全局配置。

## 环境与解释边界

- 初次架构验收的三项跳过包括真实 Redis 集成和 Windows 符号链接权限两项；后续已启动真实 Redis 并执行完整测试，仅余符号链接权限两项跳过。
- 初次验收时 Docker 引擎未运行；后续已完成实际镜像构建、容器运行与 Redis 故障注入验收。独立项目、临时端口和新卷不挂载用户资料；测试专属容器、卷与应用镜像已清理。详见 [容器与 Redis 验收记录](CONTAINER_REDIS_E2E_20261018.md)。
- 本轮未产生真实云模型付费调用，也未重跑历史实验分数。成功组表示内部核验通过且最终答案已记录，不代表人工确认正确；会话保存与业务运行状态通过运行 ID 单独关联。
- `RAG_TRACE_DB` 保持可选、默认关闭；日志持久化仍逐事件提交，记录错误不改变问答结果，不等于独立后台采集进程。
- 生产仍是单 API 进程、进程内线程与 JSON/SQLite 路线；没有新增队列、分布式 Worker 或多租户能力。
- 源码/可编辑安装与现有 Docker `/app` 路径已检查。独立 wheel 部署应显式指定 `DEEPSEARCH_ROOT`，不要通过安装目录猜测用户数据位置。
