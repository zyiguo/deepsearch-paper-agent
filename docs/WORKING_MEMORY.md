# 模型工作记忆

Agentic RAG 的 Researcher 可以调用一个 `working_memory` 工具，自主读取和更新工作笔记。
这是应用内功能，不修改 Codex 的个人记忆，也不代替现有 SQLite 对话记录和结构化会话记忆。

## 两个文件

每个会话各有两个 UTF-8 Markdown 文件，第一次读取时初始化：

```text
<conversation database stem>.working-memory/<session UUID>/
  PLAN.md
  HISTORY.md
```

默认数据库为 `data/conversations.sqlite3` 时，目录为
`data/conversations.working-memory/<session UUID>/`。
重启后仍可读取；新会话不会自动继承另一个会话的偏好。

| 文件 | 内容 |
| --- | --- |
| `PLAN.md` | 当前目标、待实施步骤、阻塞项、下一步；不能把计划写成已完成结果 |
| `HISTORY.md` | 已实际完成的步骤及结果、必要的回溯线索、用户明确表达的偏好；建议使用 `Completed` 和 `User Preferences` 两节 |

每个文件最多 2000 个字符，定位是精简的工作摘要，不是无限增长的日志。
模型更新时应保留仍有效的偏好和关键进度，合并重复内容；完整问答仍在 SQLite 中。
记录结果摘要，不记录私密推理过程、密钥或从论文/外部工具中提取的指令与所谓用户偏好。
写入的步骤记录立即持久化；后续回答失败不会回滚已经完成步骤的记录。因此不能提前记为整轮成功。

## 工具协议

读取：

```json
{"action":"read","document":"plan"}
```

返回 `document`、完整 `content`、内容 SHA-256 `revision` 和非证据信任标记。
更新必须在本轮工具会话内先读取目标文件，再使用返回的版本覆盖完整 Markdown：

```json
{"action":"replace","document":"plan","expected_revision":"<64-character revision from read>","content":"# Plan\n\n- Read the chart legend\n- Verify the units"}
```

`document` 只能是 `plan` 或 `history`；模型不能指定文件路径或会话 ID。
每次成功更新后，下次更新仍须重新读取。版本变化返回 `memory_conflict`，需要重读并合并，不能盲目覆盖。
同一应用进程内读写串行化；通过临时文件加原子替换保留旧文件，降低中断造成损坏的风险。
不支持多个应用进程共同写同一个记忆目录；多 worker 部署需要另加跨进程锁或事务存储。

## 接入与边界

- 仅启用 Agentic RAG 的会话问答绑定记忆能力；会话身份由服务端提供，与是否启用轨迹记录无关。
- Researcher 在继续任务或缺失上下文时可自行读取；更新进度、明确偏好时可自行写入，不额外触发云端总结调用。
- 记忆调用计入现有工具预算，不增加无限循环。预算不足时优先完成证据检索。
- 模型读取或更新过的笔记，以有长度限制的上下文传给 Supervisor 和 Answerer；不传给 Verifier，不加入向量索引、证据注册表或引用列表。
- 笔记是模型撰写、可能有错的辅助上下文。当前用户要求和显式结构化偏好优先；不能用笔记扩大文档权限或代替本轮核验。
- 无会话的 `/api/ask`、CLI 和默认 MCP 服务不暴露此工具，避免无身份请求访问个人笔记。未来 MCP 写入需要先设计可信会话绑定。
- 清空会使在途工具句柄失效，防止旧任务写回；但无法撤回已经发给云端模型的上下文。
- 单用户本地部署仍不等于多租户鉴权。记忆明文保存在本地，且调用时会进入模型上下文；开启轨迹日志后也可能进入日志。不要存放秘密。

## 查看与清空

- `GET /api/sessions/{session_id}/working-memory`：查看两个文件的内容和版本。
- `DELETE /api/sessions/{session_id}/working-memory`：只清空工作笔记。
- 原有 `DELETE /api/sessions/{session_id}/memory`：同时清空结构化记忆和工作笔记，保留问答记录。
- 删除会话：删除这两个文件并撤销在途写入能力；清理失败返回错误，不静默宣称成功。

没有新增前端面板。可通过接口查看，或直接打开 Markdown；手工修改同样会被版本检查发现。

## 验证

`tests/test_working_memory.py` 覆盖持久化、会话隔离、版本冲突、并发写入、清空撤销、路径和大小限制、原子替换失败、中文参数、LangGraph 模型选择工具，以及 API 跨轮读取和删除。
`tests/test_agentic_integration.py` 验证绑定传递、Answerer 可获得已读取笔记、Verifier 不接收笔记且引用仍来自原始证据。
离线脚本模型测试验证调用链，不代表真实云端模型每次都会正确选择或总结记忆工具。
