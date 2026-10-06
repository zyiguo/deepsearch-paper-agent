"""Model-independent page analysis, caching, and evidence preparation."""
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.section_chunking import SectionConfig, section_chunks


class VisualItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['figure', 'table', 'formula']
    label: str = Field(max_length=200)
    description: str = Field(min_length=1, max_length=12000)


class PageAnalysis(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ocr_text: str = Field(max_length=100000)
    items: list[VisualItem] = Field(max_length=40)
    warnings: list[str] = Field(max_length=16)


class PageAnalyzer(Protocol):
    @property
    def identity(self) -> str: ...

    def analyze(self, image: Path, native_text: str) -> PageAnalysis: ...


@dataclass
class PreparedDocument:
    pages: list[str]
    chunks: list[TextChunk]
    warnings: list[str]


class DocumentProcessor(Protocol):
    def prepare(self, native_pages: list[str], source: str, document_id: str,
                render_image: Callable[[int], Path]) -> PreparedDocument: ...


class PageAnalysisCache:
    def __init__(self, root: Path):
        self.root = root

    def path(self, document_id: str, model_id: str, number: int) -> Path:
        if (not document_id or not model_id or type(number) is not int or number < 1
                or any(c not in '0123456789abcdef' for c in document_id + model_id)):
            raise ValueError('视觉缓存标识无效')
        return self.root / f'{document_id}.{model_id}.page-{number}.json'

    def load(self, document_id: str, model_id: str, number: int) -> PageAnalysis | None:
        try:
            return PageAnalysis.model_validate_json(self.path(document_id, model_id, number).read_text(encoding='utf-8'))
        except (OSError, ValidationError):
            return None

    def save(self, document_id: str, model_id: str, number: int, analysis: PageAnalysis):
        path = self.path(document_id, model_id, number)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(analysis.model_dump_json().encode('utf-8'))
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class MultimodalDocumentProcessor:
    def __init__(self, analyzer: PageAnalyzer, cache: PageAnalysisCache, *, allow_image_only=False):
        self.analyzer, self.cache = analyzer, cache
        self.allow_image_only = allow_image_only

    def prepare(self, native_pages: list[str], source: str, document_id: str,
                render_image: Callable[[int], Path]) -> PreparedDocument:
        pages = list(native_pages)
        analyses, warnings = [], []
        ocr_pages = {n for n, text in enumerate(native_pages, 1) if len(''.join(text.split())) < 10}
        for number, text in enumerate(native_pages, 1):
            analysis = self.cache.load(document_id, self.analyzer.identity, number)
            if analysis is None:
                analysis = self.analyzer.analyze(render_image(number), text)
                self.cache.save(document_id, self.analyzer.identity, number, analysis)
            analyses.append(analysis)
            if number in ocr_pages:
                pages[number - 1] = analysis.ocr_text
            elif analysis.ocr_text.strip():
                pages[number - 1] = text + '\n\n[扫描区域文字识别]\n' + analysis.ocr_text
                ocr_pages.add(number)
            warnings.extend(f'第 {number} 页：{message[:300]}' for message in analysis.warnings)
        if sum(len(text) for text in pages) > 100000:
            raise ValueError('识别文字超过 100000 字符，请拆分 PDF')
        chunks = section_chunks(pages, source, SectionConfig())
        visual_pages = ocr_pages | {n for n, a in enumerate(analyses, 1) if a.items}
        for chunk in chunks:
            covered = set(range(chunk.page_number, (chunk.page_end or chunk.page_number) + 1))
            chunk.visual_pages = sorted(covered & visual_pages)
            if covered & ocr_pages:
                chunk.evidence_kind = 'ocr'
        for number, analysis in enumerate(analyses, 1):
            offset = max((c.chunk_index for c in chunks if c.page_number == number), default=-1) + 1
            for i, item in enumerate(analysis.items):
                chunks.append(TextChunk(text=f'{item.label}\n{item.description}'.strip(), source_path=source,
                                        page_number=number, chunk_index=offset + i,
                                        evidence_kind=item.kind, visual_pages=[number]))
        if sum(len(c.text) for c in chunks) > 300000:
            raise ValueError('文字和图表描述超过 300000 字符，请拆分 PDF')
        if not chunks and not self.allow_image_only:
            raise ValueError('没有识别到可用文字或图表，请检查扫描清晰度')
        for chunk in chunks:
            chunk.refresh_identity()
        return PreparedDocument(pages, chunks, warnings)
