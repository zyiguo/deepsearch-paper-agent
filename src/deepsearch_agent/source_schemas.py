"""Additive citation metadata shared by question and conversation APIs."""
from pydantic import BaseModel, Field


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
