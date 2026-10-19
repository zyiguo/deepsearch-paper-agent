from deepsearch_agent.knowledge.chunking import chunk_text, chunk_pages
from deepsearch_agent.core.evidence import TextChunk


def test_chunk_text_splits_long_text_with_overlap() -> None:
    text = "0123456789" * 3  # 30 字符

    chunks = chunk_text(text, chunk_size=10, overlap=3)

    assert chunks == [
        "0123456789",
        "7890123456",
        "4567890123",
        "123456789",
    ]
def test_text_chunk_keeps_source_metadata() -> None:
    chunk = TextChunk(
        text="论文提出了一个两阶段检索方法。",
        source_path="papers/rag.pdf",
        page_number=3,
        chunk_index=0,
    )

    assert chunk.text == "论文提出了一个两阶段检索方法。"
    assert chunk.source_path == "papers/rag.pdf"
    assert chunk.page_number == 3
    assert chunk.chunk_index == 0
def test_chunk_pages_keeps_page_numbers_and_chunk_indexes() -> None:
    chunks = chunk_pages(
        pages=["第一页内容很长", "第二页内容也很长"],
        source_path="papers/demo.pdf",
        chunk_size=5,
        overlap=1,
    )

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