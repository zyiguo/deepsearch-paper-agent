from contextlib import asynccontextmanager
from threading import Lock
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from copy import deepcopy
import os
from fastapi.responses import JSONResponse
from deepsearch_agent.llm_config import LLMSettings
from deepsearch_agent.llm_factory import build_llm
from deepsearch_agent.llm_gateway import (
    ModelServiceError,
    OpenAICompatibleClient,
)
from deepsearch_agent.conversations import ConversationStore
from deepsearch_agent.conversation_api import router as conversation_router
from deepsearch_agent.demo import (
    DEMO_QUESTIONS,
    DEMO_SOURCE,
    build_demo_knowledge_base,
    build_demo_llm,
)
from deepsearch_agent.qa import AnswerService
from deepsearch_agent.reranker import LLMReranker
from deepsearch_agent.query_expander import LLMQueryExpander
from deepsearch_agent.embedding_runtime import EmbeddingSettings, build_knowledge_base
from deepsearch_agent.documents import DocumentLibrary
from deepsearch_agent.document_api import router as document_router
from deepsearch_agent.source_schemas import CitationResponse as SourceResponse
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX_PATH = PROJECT_ROOT / "data" / "indexes" / "demo.json"

class HealthResponse(BaseModel):
    status: str
    mode: str
    retrieval_mode: str | None = None

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


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=3, ge=1, le=10)
    document_ids: list[str] | None = Field(default=None, max_length=1000)

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


def create_app(
    index_path: Path = DEFAULT_INDEX_PATH,
    conversation_path: Path | None = None,
    llm_settings: LLMSettings | None = None,
    embedding_settings: EmbeddingSettings | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = (
            llm_settings
            if llm_settings is not None
            else LLMSettings.load(PROJECT_ROOT / ".env")
        )
        embeddings = embedding_settings if embedding_settings is not None else (
            EmbeddingSettings() if llm_settings is not None
            else EmbeddingSettings.load(PROJECT_ROOT / ".env.embedding")
        )
        llm_client = build_llm(settings)

        try:
            app.state.mode = (
                "demo"
                if settings.backend == "demo"
                else "live-llm-demo-retrieval"
            )
            app.state.llm_model = (
                "fake"
                if settings.backend == "demo"
                else settings.model
            )

            app.state.demo_retrieval = embeddings.backend == "demo"
            if not app.state.demo_retrieval:
                app.state.mode = "live-retrieval-demo-llm" if settings.backend == "demo" else "live-llm-live-retrieval"
            app.state.kb = build_knowledge_base(embeddings)
            app.state.index_path = (Path(index_path) if app.state.demo_retrieval else
                Path(index_path).with_name(f"embedding-{embeddings.identity}.json"))
            app.state.persistence_error = None
            app.state.dirty = True
            app.state.write_blocked = False
            app.state.lock = Lock()

            app.state.conversations = ConversationStore(
                conversation_path
                if conversation_path is not None
                else Path(index_path).with_name("conversations.sqlite3")
            )

            if app.state.index_path.exists():
                try:
                    app.state.kb.load(app.state.index_path)
                    app.state.dirty = False
                except (OSError, ValueError, KeyError, TypeError) as error:
                    app.state.persistence_error = f"知识库恢复失败：{error}"
                    app.state.write_blocked = True

            # 无论快照是否存在、是否成功恢复，都要创建问答服务。
            is_live = settings.backend != 'demo'
            reranker = LLMReranker(
                llm_client,
                context_window=settings.context_window,
                max_output_tokens=settings.max_output_tokens,
            ) if is_live else None
            query_expander = LLMQueryExpander(llm_client) if is_live else None
            app.state.answer_service = AnswerService(
                client=llm_client,
                knowledge_base=app.state.kb,
                use_graph=is_live,
                context_window=settings.context_window,
                max_output_tokens=settings.max_output_tokens,
                reranker=reranker,
                query_expander=query_expander,
            )

            app.state.documents = DocumentLibrary(Path(index_path).parent / 'documents', app.state)
            try:
                yield
            finally:
                app.state.documents.close()
        finally:
            if isinstance(llm_client, OpenAICompatibleClient):
                llm_client.close()

    app = FastAPI(
        title="DeepSearch Paper Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    @app.exception_handler(ModelServiceError)
    async def handle_model_error(
        request: Request,
        error: ModelServiceError,
    ):
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": str(error)},
        )
    app.include_router(conversation_router)
    app.include_router(document_router)

    @app.get("/api/health", response_model=HealthResponse, response_model_exclude_none=True)
    def health(request: Request):
        return HealthResponse(
            status="ok",
            mode=request.app.state.mode,
            retrieval_mode=request.app.state.kb.retrieval_mode if not request.app.state.demo_retrieval else None,
        )

    @app.post("/api/demo/ingest", response_model=IngestResponse)
    def ingest_demo(request: Request):
        state = request.app.state

        with state.lock:
            if state.write_blocked:
                raise HTTPException(
                    status_code=409,
                    detail="快照恢复异常，已禁止写入。请修复快照后重新加载。",
                )

            if any(
                chunk.source_path == DEMO_SOURCE
                for chunk in state.kb.chunks
            ):
                return IngestResponse(
                    added_chunks=0,
                    message="演示文献已导入",
                )

            # 当前使用纯内存的演示客户端，可以复制候选知识库。
            candidate = deepcopy(state.kb)
            chunks = candidate.ingest_pdf(DEMO_SOURCE)

            try:
                candidate.save(state.index_path)
            except (OSError, ValueError, TypeError) as error:
                state.persistence_error = f"导入保存失败：{error}"
                raise HTTPException(
                    status_code=500,
                    detail="保存失败，本次导入未生效，原知识库保持不变。",
                ) from error

            # 文件保存成功后，再切换正在使用的知识库。
            state.kb = candidate
            state.answer_service.knowledge_base = candidate
            state.persistence_error = None
            state.dirty = False

            return IngestResponse(
                added_chunks=len(chunks),
                message="演示文献已导入并保存",
            )

    @app.get(
        "/api/documents",
        response_model=list[DocumentResponse],
    )
    def list_documents(request: Request):
        uploaded = request.app.state.documents.list()
        uploaded_sources = {record['source_path'] for record in uploaded}
        with request.app.state.lock:
            counts: dict[str, int] = {}

            for chunk in request.app.state.kb.chunks:
                counts[chunk.source_path] = (
                    counts.get(chunk.source_path, 0) + 1
                )

            return uploaded + [
                DocumentResponse(
                    id=source,
                    title=source.replace("\\", "/").rsplit("/", 1)[-1],
                    source_path=source,
                    chunk_count=count,
                )
                for source, count in counts.items() if source not in uploaded_sources
            ]

    @app.post("/api/ask", response_model=AskResponse)
    def ask(body: AskRequest, request: Request):
        if request.app.state.demo_retrieval and body.question not in DEMO_QUESTIONS:
            raise HTTPException(
                status_code=400,
                detail=(
                    "演示模式仅支持："
                    + "、".join(DEMO_QUESTIONS)
                ),
            )

        with request.app.state.lock:
            from deepsearch_agent.retrieval_scope import resolve_sources
            sources = resolve_sources(request.app.state, body.document_ids)
            result = request.app.state.answer_service.answer(
                body.question,
                top_k=body.top_k,
                **({'allowed_sources': sources} if sources is not None else {}),
            )

            return AskResponse(
                mode=request.app.state.mode,
                text=result.text,
                sources=[
                    SourceResponse(
                        citation_id=index,
                        source_path=source.source_path,
                        page_number=source.page_number,
                        text=source.text,
                        page_end=source.page_end,
                        section_path=source.section_path,
                        pages=source.pages,
                    )
                    for index, source in enumerate(
                        result.sources,
                        start=1,
                    )
                ],
            )
    @app.get(
    "/api/knowledge-base/status",
    response_model=PersistenceStatus,
)
    def persistence_status(request: Request):
        state = request.app.state

        with state.lock:
            return PersistenceStatus(
                snapshot_exists=state.index_path.exists(),
                chunk_count=len(state.kb.chunks),
                dirty=state.dirty,
                write_blocked=state.write_blocked,
                error=state.persistence_error,
            )


    @app.post(
        "/api/knowledge-base/save",
        response_model=PersistenceResponse,
    )
    def save_knowledge_base(request: Request):
        state = request.app.state

        with state.lock:
            if state.write_blocked:
                raise HTTPException(
                    status_code=409,
                    detail="快照恢复异常，禁止覆盖。请修复后重新加载。",
                )

            try:
                state.kb.save(state.index_path)
            except (OSError, ValueError, TypeError) as error:
                state.dirty = True
                state.persistence_error = f"保存失败：{error}"

                raise HTTPException(
                    status_code=500,
                    detail="知识库保存失败，请检查状态信息。",
                ) from error

            state.dirty = False
            state.persistence_error = None

            return PersistenceResponse(message="知识库已保存")


    @app.post(
        "/api/knowledge-base/load",
        response_model=PersistenceResponse,
    )
    def load_knowledge_base(request: Request):
        state = request.app.state

        with state.lock:
            if not state.index_path.exists():
                state.persistence_error = "尚无可加载的知识库快照"

                raise HTTPException(
                    status_code=404,
                    detail=state.persistence_error,
                )

            try:
                # 已有 load() 会在全部校验成功后才替换内部数据。
                state.kb.load(state.index_path)
            except (OSError, ValueError, KeyError, TypeError) as error:
                state.persistence_error = f"加载失败：{error}"
                state.write_blocked = True

                raise HTTPException(
                    status_code=409,
                    detail="快照无法加载，当前内存知识库保持不变，已禁止覆盖写入。",
                ) from error

            state.dirty = False
            state.write_blocked = False
            state.persistence_error = None

            return PersistenceResponse(message="知识库已重新加载")
    return app


app = create_app()
