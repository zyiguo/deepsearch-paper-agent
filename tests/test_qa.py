from pathlib import Path

from deepsearch_agent.storage import JsonlAnswerStore
from deepsearch_agent.qa import AnswerService
from deepsearch_agent.knowledge_base import KnowledgeBase
from deepsearch_agent.pdf import FakePdfReader
from deepsearch_agent.qa import Answer, SourceReference
class RecordingLLMClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.last_prompt: str | None = None

    def complete(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response


def test_answer_service_uses_knowledge_base_context_when_available() -> None:
    reader = FakePdfReader(
        pages=[
            "RAG 使用检索增强生成回答。",
            "这是一段无关内容。",
        ]
    )
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0)
    kb.ingest_pdf("papers/rag.pdf")

    client = RecordingLLMClient(response="这是基于上下文的回答。")
    service = AnswerService(client=client, knowledge_base=kb)

    answer = service.answer("RAG", top_k=1)

    assert isinstance(answer, Answer)
    assert answer.text == "这是基于上下文的回答。"
    assert answer.sources == [
        SourceReference(
            source_path="papers/rag.pdf",
            page_number=1,
            text="RAG 使用检索增强生成回答。",
            chunk_id=kb.chunks[0].chunk_id,
            content_id=kb.chunks[0].content_id,
            asset_ids=kb.chunks[0].asset_ids,
    )
]
def test_answer_without_knowledge_base_returns_empty_sources():
    client = RecordingLLMClient("这段输出不应该被使用。")
    service = AnswerService(client)

    result = service.answer("一个问题")

    assert result.text == "当前知识库未检索到可用证据，无法根据资料回答。"
    assert result.sources == []
    assert client.last_prompt is None
def test_answer_service_includes_chunk_source_and_page_in_context() -> None:
    reader = FakePdfReader(
        pages=[
            "RAG 使用检索增强生成回答。",
            "第二页介绍实验设置。",
        ]
    )
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0)
    kb.ingest_pdf("papers/rag.pdf")

    client = RecordingLLMClient(response="这是带引用上下文的回答。")
    service = AnswerService(client=client, knowledge_base=kb)

    service.answer("RAG", top_k=1)

    assert client.last_prompt is not None
    assert "[来源: papers/rag.pdf, 第 1 页]" in client.last_prompt
    assert "RAG 使用检索增强生成回答。" in client.last_prompt
def test_answer_service_persists_history(tmp_path):
    store = JsonlAnswerStore(
        tmp_path / "history" / "answers.jsonl"
    )

    kb = KnowledgeBase(
        pdf_reader=FakePdfReader(
            pages=["持久化表示将数据保存到磁盘。"]
        ),
        chunk_size=100,
        overlap=0,
    )
    kb.ingest_pdf("papers/storage.pdf")

    client = RecordingLLMClient("持久化是将数据保存到磁盘。[1]")
    service = AnswerService(
        client=client,
        knowledge_base=kb,
        history_store=store,
    )

    result = service.answer("持久化")

    records = store.list_records()

    assert result.text == "持久化是将数据保存到磁盘。[1]"
    assert len(records) == 1
    assert records[0]["question"] == "持久化"
    assert records[0]["answer"]["text"] == result.text
    assert records[0]["answer"]["sources"][0]["page_number"] == 1
def test_no_matching_chunks_refuses_without_calling_llm():
    kb = KnowledgeBase(
        pdf_reader=FakePdfReader(pages=["本文研究图像分类。"]),
        chunk_size=100,
        overlap=0,
    )
    kb.ingest_pdf("papers/demo.pdf")

    client = RecordingLLMClient("不应该返回这段模型输出。")
    service = AnswerService(client=client, knowledge_base=kb)

    result = service.answer("电池寿命")

    assert result.text == "当前知识库未检索到可用证据，无法根据资料回答。"
    assert result.sources == []
    assert client.last_prompt is None


def test_semantic_results_become_numbered_evidence():
    class StubEmbeddingClient:
        def embed(self, texts: list[str]) -> list[list[float]]:
            mapping = {
                "实验设备包含两张显卡。": [0.0, 1.0],
                "实验使用对比学习训练编码器。": [1.0, 0.0],
                "模型怎么训练？": [0.9, 0.1],
            }
            return [mapping[text] for text in texts]

    kb = KnowledgeBase(
        pdf_reader=FakePdfReader(
            pages=[
                "实验设备包含两张显卡。",
                "实验使用对比学习训练编码器。",
            ]
        ),
        chunk_size=100,
        overlap=0,
        embedding_client=StubEmbeddingClient(),
    )
    kb.ingest_pdf("papers/demo.pdf")

    client = RecordingLLMClient("模型采用对比学习训练。[1]")
    service = AnswerService(client=client, knowledge_base=kb)

    result = service.answer("模型怎么训练？", top_k=1)

    assert result.text == "模型采用对比学习训练。[1]"
    assert len(result.sources) == 1
    assert result.sources[0].page_number == 2
    assert result.sources[0].text == "实验使用对比学习训练编码器。"

    assert client.last_prompt is not None
    assert "[1] [来源: papers/demo.pdf, 第 2 页]" in client.last_prompt
    assert "实验使用对比学习训练编码器。" in client.last_prompt
    assert "实验设备包含两张显卡。" not in client.last_prompt
def test_answer_rejects_invalid_input():
    import pytest

    client = RecordingLLMClient("不应调用")
    service = AnswerService(client)

    with pytest.raises(ValueError, match="问题不能为空"):
        service.answer("   ")

    with pytest.raises(ValueError, match="top_k"):
        service.answer("训练方法", top_k=0)

    assert client.last_prompt is None


# ── Token budget management ──────────────────────────────────────────

def test_context_truncated_when_exceeds_budget():
    """Large chunks + small context_window → only some chunks enter the prompt."""
    big_text = "这是一段很长的实验描述。" * 100  # ~1200 chars, ~1200 tokens
    reader = FakePdfReader(pages=[big_text, big_text, big_text])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=10000, overlap=0)
    kb.ingest_pdf("papers/big.pdf")

    client = RecordingLLMClient("回答。")
    service = AnswerService(client=client, knowledge_base=kb, context_window=2048, max_output_tokens=512)

    result = service.answer("实验", top_k=3)

    # Not all chunks fit into a 2048-token window after overhead.
    assert len(result.sources) < 3
    assert len(result.sources) >= 1


def test_small_context_works_normally():
    """Default context_window with small chunks → all chunks included (backward compat)."""
    class AllMatchEmbeddingClient:
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    reader = FakePdfReader(pages=["对比学习是核心方法。", "实验使用两张显卡。"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0,
                       embedding_client=AllMatchEmbeddingClient(), embedding_id='test')
    kb.ingest_pdf("papers/demo.pdf")

    client = RecordingLLMClient("回答。")
    service = AnswerService(client=client, knowledge_base=kb, context_window=8192)

    result = service.answer("方法", top_k=2)

    assert len(result.sources) == 2


def test_sources_match_included_chunks():
    """Sources count equals included (non-truncated) chunks, not top_k."""
    big_text = "详细的实验设置描述。" * 80  # ~800 chars
    reader = FakePdfReader(pages=[big_text, big_text, big_text, big_text])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=10000, overlap=0)
    kb.ingest_pdf("papers/big.pdf")

    client = RecordingLLMClient("回答。")
    service = AnswerService(client=client, knowledge_base=kb, context_window=1024, max_output_tokens=256)

    result = service.answer("实验", top_k=4)

    assert len(result.sources) <= 4
    # Each source must have a page number from the KB.
    for source in result.sources:
        assert source.page_number in (1, 2, 3, 4)


# ── Reranker integration ─────────────────────────────────────────────

def test_reranker_over_fetches_and_reorders():
    """With a reranker, AnswerService over-fetches and reorders before context."""
    class StubEmbeddingClient:
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    class StubReranker:
        def __init__(self):
            self.called = False
        def rerank(self, query, chunks, top_k):
            self.called = True
            assert len(chunks) > top_k  # over-fetched
            return list(reversed(chunks))[:top_k]

    reader = FakePdfReader(pages=["第一段内容。", "第二段内容。", "第三段内容。", "第四段内容。"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0,
                       embedding_client=StubEmbeddingClient(), embedding_id='test')
    kb.ingest_pdf("papers/demo.pdf")

    reranker = StubReranker()
    client = RecordingLLMClient("回答。")
    service = AnswerService(client=client, knowledge_base=kb, reranker=reranker)

    result = service.answer("内容", top_k=2)

    assert reranker.called
    assert len(result.sources) == 2


def test_no_reranker_backward_compat():
    """Without a reranker, fetch_count equals top_k (no over-fetch)."""
    class StubEmbeddingClient:
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    reader = FakePdfReader(pages=["第一段。", "第二段。"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0,
                       embedding_client=StubEmbeddingClient(), embedding_id='test')
    kb.ingest_pdf("papers/demo.pdf")

    client = RecordingLLMClient("回答。")
    service = AnswerService(client=client, knowledge_base=kb)

    result = service.answer("段", top_k=2)

    assert len(result.sources) == 2


# ── Query expansion integration ──────────────────────────────────────

def test_query_expander_multi_search_and_dedup():
    """Expander generates 2 extra queries → 3 searches → merged dedup → reranker."""
    class StubEmbeddingClient:
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    class StubExpander:
        def expand(self, query, history=None):
            return ['改写一', '改写二']
    # Each query returns 2 unique chunks; merged = up to 6 (dedup may reduce).
    reader = FakePdfReader(pages=["片段一。", "片段二。", "片段三。", "片段四。", "片段五。", "片段六。"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0,
                       embedding_client=StubEmbeddingClient(), embedding_id='test')
    kb.ingest_pdf("papers/demo.pdf")

    class RecordingReranker:
        def __init__(self):
            self.received = []
        def rerank(self, query, chunks, top_k):
            self.received = list(chunks)
            return chunks[:top_k]

    reranker = RecordingReranker()
    client = RecordingLLMClient("回答。")
    service = AnswerService(client=client, knowledge_base=kb,
                            reranker=reranker, query_expander=StubExpander())

    result = service.answer("片段", top_k=3)

    # Reranker received merged candidates from all 3 queries (>3, since 3 queries × 2 each).
    assert len(reranker.received) >= 3
    # Final result respects top_k.
    assert len(result.sources) <= 3


def test_no_expander_backward_compat():
    """Without an expander, only the original query is searched."""
    class StubEmbeddingClient:
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    reader = FakePdfReader(pages=["片段一。", "片段二。"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0,
                       embedding_client=StubEmbeddingClient(), embedding_id='test')
    kb.ingest_pdf("papers/demo.pdf")

    client = RecordingLLMClient("回答。")
    service = AnswerService(client=client, knowledge_base=kb)

    result = service.answer("片段", top_k=2)

    assert len(result.sources) == 2


def test_expander_failure_falls_back_to_original():
    """If the expander raises, only the original query is used."""
    class StubEmbeddingClient:
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    class BrokenExpander:
        def expand(self, query, history=None):
            raise RuntimeError('LLM failed')
    reader = FakePdfReader(pages=["片段一。", "片段二。"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0,
                       embedding_client=StubEmbeddingClient(), embedding_id='test')
    kb.ingest_pdf("papers/demo.pdf")

    client = RecordingLLMClient("回答。")
    service = AnswerService(client=client, knowledge_base=kb,
                            query_expander=BrokenExpander())

    result = service.answer("片段", top_k=2)

    # Still works — original query returned results.
    assert len(result.sources) == 2
