"""Additive citation metadata shared by question and conversation APIs."""
from pydantic import BaseModel, Field
from typing import Literal


class PageEvidence(BaseModel):
    page_number: int = Field(ge=1)
    text: str


class CitationResponse(BaseModel):
    citation_id: int
    source_path: str
    page_number: int
    text: str
    page_end: int | None = None
    section_path: list[str] = Field(default_factory=list)
    pages: list[PageEvidence] = Field(default_factory=list)
    evidence_kind: Literal['text', 'ocr', 'figure', 'table', 'formula', 'image'] = 'text'
    visual_pages: list[int] = Field(default_factory=list)
    chunk_id: str = ''
    content_id: str = ''
    asset_ids: list[str] = Field(default_factory=list)
