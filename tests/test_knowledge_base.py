from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.knowledge_base import KnowledgeBase
from deepsearch_agent.pdf import FakePdfReader


def test_knowledge_base_ingests_pdf_pages_into_chunks() -> None:
    reader = FakePdfReader(pages=["第一页内容很长", "第二页内容也很长"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=5, overlap=1)

    chunks = kb.ingest_pdf("papers/demo.pdf")

    assert chunks == [
        TextChunk(
            text="第一页内容",
            source_path="papers/demo.pdf",
            page_number=1,
            chunk_index=0,
        ),
        TextChunk(
            text="容很长",
            source_path="papers/demo.pdf",
            page_number=1,
            chunk_index=1,
        ),
        TextChunk(
            text="第二页内容",
            source_path="papers/demo.pdf",
            page_number=2,
            chunk_index=0,
        ),
        TextChunk(
            text="容也很长",
            source_path="papers/demo.pdf",
            page_number=2,
            chunk_index=1,
        ),
    ]
def test_knowledge_base_stores_ingested_chunks_in_memory() -> None:
    reader = FakePdfReader(pages=["第一页内容很长"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=5, overlap=1)

    chunks = kb.ingest_pdf("papers/demo.pdf")

    assert kb.chunks == chunks
def test_knowledge_base_accumulates_chunks_from_multiple_pdfs() -> None:
    reader = FakePdfReader(pages=["第一页内容很长"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=5, overlap=1)

    first_chunks = kb.ingest_pdf("papers/first.pdf")
    second_chunks = kb.ingest_pdf("papers/second.pdf")

    assert kb.chunks == first_chunks + second_chunks
def test_knowledge_base_search_returns_chunks_containing_query_text() -> None:
    reader = FakePdfReader(
        pages=[
            "这篇论文介绍了向量检索方法。",
            "这篇论文还讨论了实验设置。",
        ]
    )
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0)
    kb.ingest_pdf("papers/demo.pdf")

    results = kb.search("向量检索")

    assert results == [
        TextChunk(
            text="这篇论文介绍了向量检索方法。",
            source_path="papers/demo.pdf",
            page_number=1,
            chunk_index=0,
        )
    ]
def test_knowledge_base_search_limits_results_by_top_k() -> None:
    reader = FakePdfReader(
        pages=[
            "向量检索可以用于论文问答。",
            "向量检索需要 embedding。",
            "关键词检索是更简单的 baseline。",
        ]
    )
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0)
    kb.ingest_pdf("papers/demo.pdf")

    results = kb.search("检索", top_k=2)

    assert len(results) == 2
    assert results[0].text == "向量检索可以用于论文问答。"
    assert results[1].text == "向量检索需要 embedding。"


def test_knowledge_base_search_returns_empty_list_for_empty_query() -> None:
    reader = FakePdfReader(pages=["这是一篇关于 RAG 的论文。"])
    kb = KnowledgeBase(pdf_reader=reader, chunk_size=100, overlap=0)
    kb.ingest_pdf("papers/demo.pdf")

    assert kb.search("") == []
    assert kb.search("   ") == []