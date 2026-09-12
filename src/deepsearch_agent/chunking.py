"""Text chunking for retrieval: fixed-size pieces with overlap."""
from dataclasses import dataclass, field

@dataclass
class TextChunk:
    text: str
    source_path: str
    page_number: int
    chunk_index: int
    section_path: list[str] = field(default_factory=list)
    page_end: int | None = None
    page_spans: list[dict[str, int]] = field(default_factory=list)
    chunking: dict = field(default_factory=dict)

    @property
    def retrieval_text(self) -> str:
        if not self.section_path:
            return self.text
        return '章节：' + ' > '.join(self.section_path) + '\n\n' + self.text

    def validate_structure(self) -> None:
        """Validate additive metadata before accepting a snapshot."""
        if not isinstance(self.section_path, list) or any(not isinstance(s, str) or not s.strip() for s in self.section_path):
            raise ValueError('片段章节路径无效')
        if self.page_end is not None and (type(self.page_end) is not int or self.page_end < self.page_number):
            raise ValueError('片段结束页码无效')
        if not isinstance(self.page_spans, list) or not isinstance(self.chunking, dict):
            raise ValueError('片段分块元数据无效')
        last_page, last_end = 0, 0
        for span in self.page_spans:
            if not isinstance(span, dict) or set(span) != {'page_number', 'start', 'end', 'chunk_start', 'chunk_end'}:
                raise ValueError('片段页码映射无效')
            if any(type(v) is not int for v in span.values()):
                raise ValueError('片段页码映射无效')
            page, start, end, a, b = (span[k] for k in ['page_number', 'start', 'end', 'chunk_start', 'chunk_end'])
            if not (page > last_page and self.page_number <= page <= (self.page_end or self.page_number)
                    and 0 <= start < end and last_end <= a < b <= len(self.text) and end-start == b-a):
                raise ValueError('片段页码映射越界')
            if self.text[last_end:a].strip():
                raise ValueError('片段页码映射遗漏正文')
            last_page, last_end = page, b
        if self.page_spans and (self.page_spans[0]['page_number'] != self.page_number
                                or last_page != self.page_end or self.text[last_end:].strip()):
            raise ValueError('片段页码范围不一致')
        if self.chunking:
            from deepsearch_agent.section_chunking import SectionConfig
            SectionConfig(**self.chunking)
            if not self.page_spans:
                raise ValueError('结构化片段缺少页码映射')

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
