from dataclasses import dataclass, field
from deepsearch_agent.core.evidence import TextChunk

@dataclass
class SourceReference:
    source_path: str
    page_number: int
    text: str
    page_end: int | None = None
    section_path: list[str] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    evidence_kind: str = 'text'
    visual_pages: list[int] = field(default_factory=list)
    chunk_id: str = ''
    content_id: str = ''
    asset_ids: list[str] = field(default_factory=list)


@dataclass
class Answer:
    text: str
    sources: list[SourceReference]
    skills_used: list[dict[str, str]] = field(default_factory=list)


def format_chunk_for_context(chunk: TextChunk) -> str:
    page = str(chunk.page_number) if not chunk.page_end or chunk.page_end == chunk.page_number else f'{chunk.page_number}–{chunk.page_end}'
    section = ('章节：' + ' > '.join(chunk.section_path) + '\n') if chunk.section_path else ''
    source = chunk_to_source_reference(chunk)
    body = '\n'.join(f"第 {p['page_number']} 页：\n{p['text']}" for p in source.pages) if source.pages else chunk.text
    return f"[来源: {chunk.source_path}, 第 {page} 页]\n{section}{body}"


def chunk_to_source_reference(chunk: TextChunk) -> SourceReference:
    return SourceReference(
        source_path=chunk.source_path,
        page_number=chunk.page_number,
        text=chunk.text,
        page_end=chunk.page_end,
        section_path=list(chunk.section_path),
        pages=[{'page_number': span['page_number'], 'text': chunk.text[span['chunk_start']:span['chunk_end']]}
               for span in chunk.page_spans],
        evidence_kind=chunk.evidence_kind,
        visual_pages=list(chunk.visual_pages),
        chunk_id=chunk.chunk_id,
        content_id=chunk.content_id,
        asset_ids=list(chunk.asset_ids),
    )
