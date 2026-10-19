"""Text chunking for retrieval: fixed-size pieces with overlap."""

from deepsearch_agent.core.evidence import TextChunk


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk_size")
    text = text.strip()
    if not text:
        return []
    step = chunk_size - overlap
    chunks: list[str] = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + chunk_size])
        if i + chunk_size >= len(text):
            break
        i += step
    return chunks
def chunk_pages(
    pages: list[str],
    source_path: str,
    chunk_size: int = 1000,
    overlap: int = 200,
) -> list[TextChunk]:
    chunks: list[TextChunk] = []

    for page_index, page_text in enumerate(pages):
        page_chunks = chunk_text(page_text, chunk_size=chunk_size, overlap=overlap)
        for chunk_index, chunk in enumerate(page_chunks):
            chunks.append(
                TextChunk(
                    text=chunk,
                    source_path=source_path,
                    page_number=page_index + 1,
                    chunk_index=chunk_index,
                )
            )

    return chunks
