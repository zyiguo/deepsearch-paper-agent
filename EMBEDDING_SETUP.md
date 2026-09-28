# Real embedding setup

The backend reads `.env.embedding` independently of the chat-model `.env`.
Fill `EMBEDDING_API_KEY` and `EMBEDDING_BASE_URL` locally. Use the OpenAI-compatible
base URL shown by the provider console, without `/embeddings`. The checked-in
`.env.embedding.example` now selects SiliconFlow `BAAI/bge-m3`, 1024 dimensions,
batches of up to 10 texts. The code's fallback model remains `text-embedding-v4`;
always set the model explicitly when switching providers. Do not share keys.

Copy the template only when creating a new local configuration; preserve an
existing `.env.embedding` and its credentials. The portable copy is also at
`examples/config/siliconflow-embedding.env.example`. Pricing, quotas and model
availability are controlled by the provider, not this project.

For BGE-M3 on `api.siliconflow.cn`, the client omits the `dimensions` request
parameter because this integration uses fixed-size vectors. It still validates
all returned vectors against `EMBEDDING_DIMENSIONS`. Other models retain the
existing dimensions parameter.

Restart the backend after editing configuration. A missing key or invalid URL
stops startup explicitly; it never silently switches to demo embeddings.
Set `EMBEDDING_BACKEND=demo` to explicitly return to offline demo retrieval.

Upload a text PDF from the frontend after the first switch. Extracted texts are
sent to the embedding provider and calls may incur charges. Upload limits are
10 MiB, 100 pages and 100,000 extracted characters. Scans require OCR, which is
not included. Processing runs in the background; failed jobs can be retried.
Uploaded originals and page metadata remain on the local backend. All documents
currently share one retrieval corpus. Citation page buttons open rendered pages.

Remote indexes are saved beside the original index as `embedding-<identity>.json`.
Identity includes endpoint, model, dimensions and encoding version, but not keys.
Old demo snapshots and conversations are preserved. Changing model, endpoint or
dimensions selects a new index. Rebuild document vectors for the new model;
re-uploading the same PDF can trigger document deduplication and is not a rebuild
mechanism. Keep the old index for rollback and do not mix vector spaces. Same model names may
change upstream: version-pinned models are preferable for reproducibility.

Both question endpoints accept arbitrary text only when real embeddings are
enabled. Retrieval still returns nearest chunks without a relevance threshold;
a returned citation does not prove the material answers the question. Live mode
uses the DeepSearch graph with generated research queries; the basic non-graph
path supports LLM query expansion / coreference rewrite with fallback.

Tests should inject both `llm_settings=LLMSettings()` and
`embedding_settings=EmbeddingSettings()` to avoid local credentials and network.
For backward compatibility, explicitly supplying `llm_settings` alone defaults
embeddings to demo; pass explicit real embedding settings when testing that mode.

Offline focused test: `.venv/Scripts/python.exe -m pytest tests/test_embedding_runtime.py -q`
## BM25 and hybrid retrieval

Real retrieval defaults to `hybrid`. Optionally set `RETRIEVAL_MODE=hybrid`,
`semantic`, or `bm25` in the backend's `.env.embedding`, then restart the backend.
The frontend displays the active mode. This setting does not change the vector
space and does not require re-embedding existing documents.

BM25 uses local Jieba search-mode Chinese tokenization, case-folded English,
preserved identifiers such as AX-247, and a small stopword list. Parameters are
k1=1.5 and b=0.75. Its in-memory inverted index is derived from stored chunks
and refreshed after their text changes, including loading or appending a PDF.

Hybrid retrieves up to max(20, top_k) candidates from each branch, min-max
normalizes branch scores, then combines them with a weighted sum, deduplicating
chunk IDs. The default semantic/BM25 weights are 0.5/0.5 (`semantic_weight` in
KnowledgeBase); this is score fusion, not reciprocal rank fusion. Scores are not confidence probabilities. An unmatched
keyword query contributes no lexical candidates; semantic results remain.
Embedding failures are reported rather than silently switching retrieval modes.

BM25-only queries do not call the embedding service, but PDF ingestion still
builds vectors, and answer generation can still call the configured cloud LLM.
Hybrid quality improvements require evaluation on representative questions;
keyword matching alone does not establish that a passage answers the question.

Offline tests: `.venv/Scripts/python.exe -m pytest tests/test_hybrid_search.py -q`

## 论文分节分块（新上传 PDF）

新上传文件使用 `section-rules-v1`，默认参数集中在
`src/deepsearch_agent/section_chunking.py` 的 `SectionConfig`：目标 600 字符、
最大 900 字符、完整单元重叠不超过 120 字符、短尾目标 120 字符。
这里统计 Python 字符，不是 token。重叠不保证恰好 120 字符。

先识别独立行的中英文标准标题、数字层级标题和中文章/节标题，再在章节内
按段落合并，长段落按句子切分，超长句才硬切。不同章节不重叠。
没有识别到标题时，回退为逐页段落/句子分块。规则不能恢复双栏错序或所有异常排版。

章节允许跨页；每块保存章节路径、起止页和逐页字符偏移。正文不改写，检索输入
额外附加章节路径。前端按页展示引用片段，并提供各页的原文入口。
快照格式升级为 2，仍可读取格式 1；每块的 `chunking` 元数据描述其真实分块参数。
顶层 chunk_size/overlap 描述 KnowledgeBase 直接 ingest 的默认设置，不代表已合并的
每一块都使用同一策略。旧块没有新元数据时仍按原样加载，不伪造其章节或参数。

这次没有改动现有 PDF、历史会话或索引文件。重启后端后，新上传 PDF 使用新策略。
已入库文件重复上传仍走去重，不会自动重分块或重新计费；旧资料迁移/重建入口需另行实现。
