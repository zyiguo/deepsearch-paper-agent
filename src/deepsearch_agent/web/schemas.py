from pydantic import BaseModel, Field, field_validator
from deepsearch_agent.web.source_schemas import CitationResponse as SourceResponse


class HealthResponse(BaseModel):
    status: str
    mode: str
    retrieval_mode: str | None = None
    vision_enabled: bool | None = None
    image_retrieval_enabled: bool | None = None
    agentic_rag_enabled: bool | None = None
    mcp_server_enabled: bool | None = None


class PersistenceStatus(BaseModel):
    snapshot_exists: bool
    chunk_count: int
    dirty: bool
    write_blocked: bool
    error: str | None


class PersistenceResponse(BaseModel):
    message: str


class IngestResponse(BaseModel):
    added_chunks: int
    message: str


class DocumentResponse(BaseModel):
    id: str
    title: str
    source_path: str
    chunk_count: int
    status: str = "ready"
    page_count: int = 0
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    file_url: str | None = None
    vision_available: bool = False
    analysis_mode: str = 'text'
    image_index_available: bool = False
    image_indexed_pages: int = 0
    media_type: str = 'application/pdf'


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=3, ge=1, le=10)
    document_ids: list[str] | None = Field(default=None, max_length=1000)
    skill_ids: list[str] | None = Field(default=None, max_length=3)

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("问题不能为空")

        return value


class AskResponse(BaseModel):
    mode: str
    text: str
    sources: list[SourceResponse]
    skills_used: list[dict[str, str]] = Field(default_factory=list)
