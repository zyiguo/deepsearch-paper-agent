# ADR-0006：单机 JSON 索引 + SQLite 会话，推迟 PostgreSQL/Compose

## 状态

Accepted（2026-09-12）

## 背景

早期 ADR（0001/0002/0005）按「Docker Compose + PostgreSQL/pgvector + Redis/Celery」设计。编码阶段确认产品边界是**本机单用户**、文献规模以可导入文字型 PDF 为主、先跑通证据可溯源问答。引入容器编排与独立数据库会显著拖慢迭代，且当前无多用户、无跨进程恢复需求。

## 决策

1. **索引**：内存中的 `KnowledgeBase`（BM25 + 向量矩阵 + 自研 IVF ANN），持久化为 JSON 快照（`data/indexes/*.json`）。
2. **会话与记忆**：SQLite（`conversations.py`），文件路径由后端管理。
3. **PDF 解析/渲染**：受控子进程（`pdf_worker.py`），索引任务用进程内 `ThreadPoolExecutor`（单 worker）。
4. **部署**：直接 `uvicorn` + `vite dev`，监听 `127.0.0.1`；**不提供** Docker Compose / Nginx / Celery / Redis。
5. **嵌入**：OpenAI 兼容 Embedding API（见 ADR-0003 修订），与对话 LLM 配置分离。

## 后果

- 优点：零外部依赖可演示；调试路径短；备份即拷贝 `data/` 与 SQLite 文件。
- 缺点：全局锁下生成时其他读写可能排队；无任务检查点/断点恢复；向量规模受内存与单机限制；不能声称「PostgreSQL 任务真相」。
- 替代方案：恢复 Compose/PG/Redis 仅在出现多用户或长任务可恢复硬需求时再开 ADR。

## 对既有 ADR 的影响

| ADR | 新状态 | 说明 |
|---|---|---|
| 0001 模块化单体与 Compose | Superseded | 保留模块化单体边界，**放弃** Compose 编排 |
| 0002 PostgreSQL + pgvector | Superseded | 由 JSON 快照 + 内存索引替代 |
| 0003 本地 Embedding 与云端 LLM | Amended | 见该文件修订：当前为云端 OpenAI 兼容 Embedding |
| 0004 证据可溯源问答 | Accepted | 核心仍有效；证据立场/机器翻译未实现 |
| 0005 PostgreSQL 任务真相 | Superseded | 无 Redis/Celery；后台任务状态在文档库内存与文件中 |

## 当前硬限制（与实现一致）

- 单 PDF：1–100 页，提取文本 ≤ 100,000 字符，上传 ≤ 10 MiB
- 不支持加密 PDF、扫描件 OCR
- 目标语料为个人本机库，**未**在 500–3000 篇规模上验证
- 无用户取消推理；断线后需刷新会话核对是否已保存
