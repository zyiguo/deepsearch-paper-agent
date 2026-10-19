from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.core.pdf import FakePdfReader


class StubEmbeddingClient:
    """人为指定向量，只用于验证检索流程。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        mapping = {
            "实验使用了对比学习训练编码器。": [1.0, 0.0],
            "实验设备包含两张显卡。": [0.0, 1.0],
            "模型是怎么训练的？": [0.9, 0.1],
        }
        return [mapping[text] for text in texts]


def test_search_ranks_by_similarity_and_preserves_source():
    kb = KnowledgeBase(
        pdf_reader=FakePdfReader(
            pages=[
                "实验设备包含两张显卡。",
                "实验使用了对比学习训练编码器。",
            ]
        ),
        chunk_size=100,
        overlap=0,
        embedding_client=StubEmbeddingClient(),
    )
    kb.ingest_pdf("papers/demo.pdf")

    results = kb.search("模型是怎么训练的？", top_k=1)

    assert len(results) == 1
    assert results[0].text == "实验使用了对比学习训练编码器。"
    assert results[0].source_path == "papers/demo.pdf"
    assert results[0].page_number == 2


def test_empty_query_and_zero_top_k_return_no_results():
    kb = KnowledgeBase(
        pdf_reader=FakePdfReader(
            pages=["实验使用了对比学习训练编码器。"]
        ),
        embedding_client=StubEmbeddingClient(),
    )
    kb.ingest_pdf("papers/demo.pdf")

    assert kb.search("   ") == []
    assert kb.search("模型是怎么训练的？", top_k=0) == []