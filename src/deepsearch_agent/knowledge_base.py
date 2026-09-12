from deepsearch_agent.chunking import TextChunk, chunk_pages
from deepsearch_agent.embeddings import EmbeddingClient, normalize
from deepsearch_agent.pdf import PdfReader
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from copy import copy

class KnowledgeBase:
    def __init__(
        self,
        pdf_reader: PdfReader,
        chunk_size: int | None = None,
        overlap: int | None = None,
        embedding_client: EmbeddingClient | None = None,
        embedding_id: str | None = None,
        retrieval_mode: str = "semantic",
        chunking_strategy: str = 'fixed-v1',
        max_chunk_size: int | None = None,
        ann_threshold: int = 1024,
        semantic_weight: float = 0.5,
    ) -> None:
        self.pdf_reader = pdf_reader
        self.chunking_strategy = chunking_strategy
        if chunking_strategy not in {'fixed-v1', 'section-rules-v1'}:
            raise ValueError('不支持的分块策略')
        from deepsearch_agent.section_chunking import SectionConfig
        defaults = SectionConfig()
        structured = chunking_strategy == 'section-rules-v1'
        self.chunk_size = chunk_size if chunk_size is not None else (defaults.target_size if structured else 1000)
        self.overlap = overlap if overlap is not None else (defaults.overlap if structured else 200)
        self.max_chunk_size = max_chunk_size if max_chunk_size is not None else defaults.max_size
        self.embedding_client = embedding_client
        self.embedding_id = embedding_id
        self.retrieval_mode = retrieval_mode
        if not 0 <= semantic_weight <= 1:
            raise ValueError("semantic_weight 必须在 0 到 1 之间")
        self.semantic_weight = semantic_weight
        self.ann_threshold = ann_threshold
        # 惰性缓存：按已索引的片段数量判断增量/重建，避免每次搜索全量重建。
        self._bm25_index = None
        self._bm25_count = 0
        self._ann = None
        self._ann_size = 0

        self.chunks: list[TextChunk] = []
        self._vectors: list[list[float]] = []

    def ingest_pdf(self, path: str) -> list[TextChunk]:
        pages = self.pdf_reader.read(path)
        if self.chunking_strategy == 'section-rules-v1':
            from deepsearch_agent.section_chunking import SectionConfig, section_chunks
            chunks = section_chunks(pages, path, SectionConfig(target_size=self.chunk_size, overlap=self.overlap,
                                                              max_size=self.max_chunk_size, min_tail=min(SectionConfig().min_tail, self.chunk_size)))
        else:
            chunks = chunk_pages(
                pages=pages,
                source_path=path,
                chunk_size=self.chunk_size,
                overlap=self.overlap,
            )

        # 过滤没有实际文本的片段。
        chunks = [chunk for chunk in chunks if chunk.text.strip()]

        if not chunks:
            return []

        if self.embedding_client is not None:
            raw_vectors = self.embedding_client.embed(
                [chunk.retrieval_text for chunk in chunks]
            )

            if len(raw_vectors) != len(chunks):
                raise ValueError("向量数量与片段数量不一致")

            vectors = [normalize(vector) for vector in raw_vectors]

            expected_dimension = (
                len(self._vectors[0])
                if self._vectors
                else len(vectors[0])
            )

            if any(
                len(vector) != expected_dimension
                for vector in vectors
            ):
                raise ValueError("向量维度不一致，请检查 Embedding 模型")

            # 整批成功后再更新内存，避免 API 失败留下半批数据。
            self._vectors.extend(vectors)

        self.chunks.extend(chunks)
        return chunks

    def merge_index(self, other: "KnowledgeBase") -> None:
        """Merge prepared vectors without repeating paid embedding calls."""
        if self.embedding_id != other.embedding_id or len(other.chunks) != len(other._vectors):
            raise ValueError("不能合并不兼容的索引")
        vectors = [normalize(v) for v in other._vectors]
        dimension = len(self._vectors[0]) if self._vectors else (len(vectors[0]) if vectors else 0)
        if any(len(v) != dimension for v in vectors):
            raise ValueError("合并向量维度不一致")
        self.chunks.extend(other.chunks)
        self._vectors.extend(vectors)

    def restrict_sources(self, sources: set[str]) -> "KnowledgeBase":
        """Request-local candidate corpus; never mutate or re-embed the stored index."""
        if self.embedding_client is not None and len(self.chunks) != len(self._vectors):
            raise ValueError('片段与向量不同步，请重新导入文档')
        view = copy(self)
        indices = [i for i, chunk in enumerate(self.chunks) if chunk.source_path in sources]
        view.chunks = [self.chunks[i] for i in indices]
        view._vectors = [self._vectors[i] for i in indices] if self.embedding_client is not None else []
        view._bm25_index = None
        view._bm25_count = 0
        view._ann = None
        view._ann_size = 0
        return view

    def _lexical_index(self):
        """BM25 索引增量维护：片段只增时只索引新增尾部，缩小或重置时全量重建。"""
        from deepsearch_agent.bm25 import BM25Index
        if self._bm25_index is None or len(self.chunks) < self._bm25_count:
            self._bm25_index = BM25Index([c.retrieval_text for c in self.chunks])
        elif len(self.chunks) > self._bm25_count:
            self._bm25_index.add_documents(
                [c.retrieval_text for c in self.chunks[self._bm25_count:]]
            )
        self._bm25_count = len(self.chunks)
        return self._bm25_index

    def _ann_search(self, query_vector: list[float], limit: int) -> list[tuple[int, float]]:
        """向量检索：小语料精确扫描，超过阈值自动切换 ANN(IVF)，增量添加。"""
        from deepsearch_agent.ann import AnnIndex
        if self._ann is None or self._ann_size > len(self._vectors):
            self._ann = AnnIndex(len(query_vector), exact_threshold=self.ann_threshold)
            self._ann.add(self._vectors)
        elif self._ann_size < len(self._vectors):
            self._ann.add(self._vectors[self._ann_size:])
        self._ann_size = len(self._vectors)
        return self._ann.search(query_vector, limit)

    def search(self, query: str, top_k: int = 5) -> list[TextChunk]:
        if top_k < 0:
            raise ValueError("top_k 不能为负数")

        query = query.strip()

        if not query or top_k == 0 or not self.chunks:
            return []

        if self.retrieval_mode not in {'semantic', 'bm25', 'hybrid'}:
            raise ValueError('检索模式必须是 semantic、bm25 或 hybrid')
        lexical_scored: list[tuple[int, float]] = []
        candidate_count = max(20, top_k)
        if self.retrieval_mode in {'bm25', 'hybrid'}:
            lexical_scored = self._lexical_index().rank_with_scores(query, candidate_count)
            if self.retrieval_mode == 'bm25' or self.embedding_client is None:
                return [self.chunks[i] for i, _ in lexical_scored[:top_k]]

        if self.embedding_client is None:
            return [
                chunk
                for chunk in self.chunks
                if query in chunk.text
            ][:top_k]

        if len(self._vectors) != len(self.chunks):
            raise ValueError("片段与向量不同步，请重新导入文档")

        query_vectors = self.embedding_client.embed([query])

        if len(query_vectors) != 1:
            raise ValueError("问题必须对应一个向量")

        query_vector = normalize(query_vectors[0])

        if len(query_vector) != len(self._vectors[0]):
            raise ValueError("问题向量与文档向量维度不一致")

        scored = self._ann_search(query_vector, candidate_count)

        if self.retrieval_mode == 'hybrid':
            from deepsearch_agent.bm25 import weighted_score_fusion
            ranked_indices = weighted_score_fusion(
                [scored, lexical_scored], [self.semantic_weight, 1 - self.semantic_weight], top_k,
            )
        else:
            ranked_indices = [index for index, _ in scored[:top_k]]

        return [
            self.chunks[index]
            for index in ranked_indices[:top_k]
        ]
    def save(self, path: str | Path) -> None:
        if self.embedding_client is None or not self.embedding_id:
            raise ValueError("保存语义索引需要 Embedding 客户端和模型标识")

        if len(self.chunks) != len(self._vectors):
            raise ValueError("片段与向量数量不一致")

        vectors = [normalize(vector) for vector in self._vectors]
        dimension = len(vectors[0]) if vectors else 0

        if any(len(vector) != dimension for vector in vectors):
            raise ValueError("向量维度不一致")

        payload = {
            "schema_version": 2,
            "embedding_id": self.embedding_id,
            "dimension": dimension,
            "chunk_size": self.chunk_size,
            "overlap": self.overlap,
            "chunking_strategy": self.chunking_strategy,
            "max_chunk_size": self.max_chunk_size,
            "entries": [
                {
                    "chunk": asdict(chunk),
                    "vector": vector,
                }
                for chunk, vector in zip(
                    self.chunks,
                    vectors,
                    strict=True,
                )
            ],
        }

        # 先序列化，确保能转换成合法 JSON。
        content = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                file.write(content)

            # Windows 下先关闭临时文件，再替换目标文件。
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    def load(self, path: str | Path) -> None:
        if self.embedding_client is None or not self.embedding_id:
            raise ValueError("加载语义索引需要 Embedding 客户端和模型标识")

        payload = json.loads(
            Path(path).read_text(encoding="utf-8")
        )

        if payload["schema_version"] not in (1, 2):
            raise ValueError("不支持的知识库格式版本")

        if payload["embedding_id"] != self.embedding_id:
            raise ValueError("Embedding 模型标识不一致")

        chunk_size = payload["chunk_size"]
        overlap = payload["overlap"]
        dimension = payload["dimension"]
        strategy = payload.get('chunking_strategy', 'fixed-v1')
        max_size = payload.get('max_chunk_size', 900)
        if strategy not in {'fixed-v1', 'section-rules-v1'} or type(max_size) is not int or max_size <= 0:
            raise ValueError('快照分块策略无效')

        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("快照中的 chunk_size 无效")

        if type(overlap) is not int or not 0 <= overlap < chunk_size:
            raise ValueError("快照中的 overlap 无效")

        if type(dimension) is not int or dimension < 0:
            raise ValueError("快照中的向量维度无效")

        chunks: list[TextChunk] = []
        vectors: list[list[float]] = []

        for entry in payload["entries"]:
            chunk = TextChunk(**entry["chunk"])
            vector = normalize(entry["vector"])

            if len(vector) != dimension:
                raise ValueError("快照中的向量维度不一致")

            if not isinstance(chunk.text, str) or not chunk.text.strip():
                raise ValueError("快照中的片段文本无效")

            if not isinstance(chunk.source_path, str):
                raise ValueError("快照中的来源路径无效")

            if type(chunk.page_number) is not int or chunk.page_number < 1:
                raise ValueError("快照中的页码无效")

            if type(chunk.chunk_index) is not int or chunk.chunk_index < 0:
                raise ValueError("快照中的片段编号无效")

            chunk.validate_structure()

            chunks.append(chunk)
            vectors.append(vector)

        if not chunks and dimension != 0:
            raise ValueError("空索引的向量维度应为 0")

        # 所有读取和校验成功后，才替换当前知识库。
        self.chunks = chunks
        self._vectors = vectors
        self._bm25_index = None
        self._bm25_count = 0
        self._ann = None
        self._ann_size = 0
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.chunking_strategy = strategy
        self.max_chunk_size = max_size
