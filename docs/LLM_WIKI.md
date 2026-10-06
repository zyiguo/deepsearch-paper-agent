# LLM Wiki

## GitHub 调研与取舍

调研日期：2026-10-06。以下是上游公开设计，不代表我们已经安装或验证这些项目的全部能力。

| 项目 | 可借鉴点 | 本项目的取舍 |
| --- | --- | --- |
| [Karpathy 原始设计](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | 原始资料、派生 Markdown Wiki、维护规则分层；编译、查询、检查三类操作 | 保留现有 PDF/图片资料库，增加独立派生知识层 |
| [atomicstrata/llm-wiki-compiler](https://github.com/atomicstrata/llm-wiki-compiler) | 共享编译服务、CLI/SDK/MCP、来源变化追踪、发布时重新检查引用 | 参考服务与编排解耦、发布前再次检查来源和版本；不引入第二套 TypeScript 编译服务 |
| [aws-samples/sample-kiro-llm-wiki](https://github.com/aws-samples/sample-kiro-llm-wiki) | 不可修改的 raw 资料、Markdown 页面、链接和维护约定 | 参考只读原始资料边界；不要求用户安装 Kiro/Obsidian，不启用自动付费编译 |

深入阅读：[compiler 架构](https://github.com/atomicstrata/llm-wiki-compiler/blob/main/ARCHITECTURE.md)、[sources 输入契约](https://github.com/atomicstrata/llm-wiki-compiler/blob/main/SOURCES_CONTRACT.md)。
本模块是对上述设计思想的本地实现，没有复制上游实现代码，也不声称兼容其存储格式或实现完整功能。

## 当前流程

```text
用户指定文献与主题问题
  -> 当前索引的范围受限快照
  -> ResearchWorkflow 检索 / 多模态阅读 / 草稿 / 核验
  -> 必须得到 verified，且至少一条有效原文引用
  -> 再次检查来源仍存在、页面版本未变化
  -> 原子保存 Markdown 知识页

Agent search_wiki / read_wiki
  -> 查看主题摘要、关联页、原文 chunk_id
  -> get_evidence / search_evidence 取回原文
  -> 回答并核验原文引用
```

编译只使用当前索引证据，不把临时在线 OCR 工具产生、尚未入库的片段写为长期引用。
已入库的 OCR、图像片段和原图阅读仍可参与；不是禁用多模态。
重新编译使用指定资料重新生成该页，不自动把旧 Wiki 文字当作事实证据。
模型核验通过不等于人工专家确认；这一版不包含人工审核发布队列。

## 与工作记忆的区别

| 模块 | 用途 | 来源 |
| --- | --- | --- |
| `PLAN.md` / `HISTORY.md` | 当前会话计划、已做事项、明确偏好 | 会话上下文和模型工作笔记 |
| Wiki 主题页 | 可复用的研究知识与资料关联 | 指定文献的已核验回答和稳定原文引用 |

Wiki 与聊天历史、工作记忆分别存储，不把用户偏好和私密会话自动写入知识库。
Wiki 只作为派生导航信息，不能取代原文证据，也不能扩大当前会话选择的文献范围。
混合引用多个文献的页面，只有其全部来源都在当前授权范围且 chunk_id 仍有效时才会向 Agent 返回。

## 存储与关联

- 默认存储：`data/wiki/wiki_<32 位标识>.md`，在测试中随 `index_path` 使用独立目录。
- 文件含 JSON 形式的 YAML frontmatter 与 Markdown 正文；元信息保留题目、模型标识、提示版本、来源路径、页码、chunk_id、content_id、关联页面和生成时间。
- SHA-256 版本用于防止覆盖冲突；只支持单应用进程写入，不支持多 worker 共享写目录。
- 页面关联目前根据共同来源生成，并返回反向链接；不是已经验证的语义知识图谱。
- 页面搜索复用现有 BM25 与中英文分词；不是新增的语义向量索引。
- `/index` 提供可导出的 Markdown 目录；不自动维护额外的 `index.md`/`log.md` 文件。
- 删除或重新切分原文后，失效页面不再进入普通搜索/Agent 阅读；lint 会报告失效来源。
- 文件是本地可读的派生数据，不上传 GitHub；直接修改 frontmatter 可能导致校验失败，推荐通过编译接口更新。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/wiki/compile` | 生成或重新编译一页，会调用模型 |
| GET | `/api/wiki` | 有效页面列表；`?query=关键词` 搜索 |
| GET | `/api/wiki/index` | Markdown 目录 |
| GET | `/api/wiki/lint` | 检查失效来源、断链、孤立页，不调用模型 |
| GET | `/api/wiki/{page_id}` | 正文、来源、关联页、反向链接与版本 |
| DELETE | `/api/wiki/{page_id}?expected_revision=...` | 按版本删除页面 |

新建请求：

```json
{
  "title": "研究方法比较",
  "question": "根据所选文献比较实验方法，说明条件和局限，保留原文引用。",
  "document_ids": ["从文献列表获得的真实文献 ID"],
  "top_k": 5
}
```

更新时增加 `page_id` 和刚读取的 `expected_revision`。版本冲突或来源变化返回 409，不覆盖旧页。
当前没有新增前端 Wiki 面板；可通过 FastAPI `/docs`、API 和 Markdown 文件使用。
应用仍按本地单用户设计，不应直接公开到互联网。

## 验证边界

离线测试覆盖文件持久化、覆盖冲突、来源范围、原文失效、编译拒绝未核验答案、API 关联与删除、生成期间来源删除，以及工具不能把 Wiki 直接注册为证据。
尚未进行真实云端 Wiki 质量评测；也没有实现自动批量编译、跨页语义矛盾判定、修订历史仓库或自动运行的维护任务。
