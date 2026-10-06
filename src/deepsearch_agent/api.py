from contextlib import asynccontextmanager, AsyncExitStack
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
from deepsearch_agent.qa import AnswerService, snapshot_answer_service
from deepsearch_agent.reranker import LLMReranker
from deepsearch_agent.multimodal_reranker import MultimodalReranker
from deepsearch_agent.query_expander import LLMQueryExpander
from deepsearch_agent.embedding_runtime import EmbeddingSettings, build_knowledge_base
from deepsearch_agent.documents import DocumentLibrary
from deepsearch_agent.document_api import router as document_router
from deepsearch_agent.source_schemas import CitationResponse as SourceResponse
from deepsearch_agent.vision_config import VisionSettings
from deepsearch_agent.vision import VisionReader
from deepsearch_agent.multimodal import MultimodalDocumentProcessor, PageAnalysisCache
from deepsearch_agent.visual_evidence import VisualEvidence
from deepsearch_agent.image_embeddings import ImageEmbeddingSettings, SiliconFlowImageEmbeddingClient
from deepsearch_agent.image_index import ImageVectorIndex
from deepsearch_agent.image_assets import AttachmentStore
from deepsearch_agent.image_api import router as image_router
from deepsearch_agent.skills import SkillRegistry, SkillError
from deepsearch_agent.skill_api import router as skill_router, validate_skills
from deepsearch_agent.agent_config import AgentSettings
from deepsearch_agent.mcp_config import MCPSettings
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX_PATH = PROJECT_ROOT / "data" / "indexes" / "demo.json"

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


def create_app(
    index_path: Path = DEFAULT_INDEX_PATH,
    conversation_path: Path | None = None,
    llm_settings: LLMSettings | None = None,
    embedding_settings: EmbeddingSettings | None = None,
    search_intent=None,
    trace_path: Path | None = None,
    vision_settings: VisionSettings | None = None,
    image_embedding_settings: ImageEmbeddingSettings | None = None,
    skills_path: Path | None = None,
    agent_settings: AgentSettings | None = None,
    mcp_settings: MCPSettings | None = None,
) -> FastAPI:
    agents = agent_settings if agent_settings is not None else (
        AgentSettings() if llm_settings is not None else AgentSettings.load(PROJECT_ROOT / '.env.agent'))
    mcp_config = mcp_settings if mcp_settings is not None else (
        MCPSettings() if llm_settings is not None else MCPSettings.load(PROJECT_ROOT / '.env.mcp'))
    mcp_server = None
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
        vision_client = None

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
            image_settings = image_embedding_settings if image_embedding_settings is not None else (
                ImageEmbeddingSettings() if llm_settings is not None else ImageEmbeddingSettings.load(
                    PROJECT_ROOT / '.env.image_embedding', shared_embeddings=embeddings)
            )
            app.state.image_retrieval_enabled = image_settings.backend != 'disabled'
            if app.state.image_retrieval_enabled:
                app.state.kb.image_index = ImageVectorIndex(image_settings.identity, SiliconFlowImageEmbeddingClient(image_settings))
            app.state.index_path = (Path(index_path) if app.state.demo_retrieval else
                Path(index_path).with_name(f"embedding-{embeddings.identity}.json"))
            app.state.persistence_error = None
            app.state.dirty = True
            app.state.write_blocked = False
            app.state.lock = Lock()
            from weakref import WeakValueDictionary
            app.state.turn_locks = WeakValueDictionary()
            app.state.agentic_rag_enabled = is_agentic = settings.backend != 'demo' and agents.enabled
            app.state.mcp_server_enabled = mcp_config.server_enabled

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
            recorder = None
            local_trace_path = trace_path
            if local_trace_path is None and llm_settings is None:
                local_trace_path = os.environ.get('RAG_TRACE_DB') or None
            if local_trace_path is not None:
                from deepsearch_agent.trajectory_adapter import RAGTraceRecorder
                recorder = RAGTraceRecorder(local_trace_path, metadata={
                    'application': 'deepsearch-paper-agent', 'main_model': settings.model,
                    'embedding_model': embeddings.model, 'retrieval_backend': embeddings.backend,
                    'index_path': str(app.state.index_path),
                })
            visual_settings = vision_settings if vision_settings is not None else (
                VisionSettings() if llm_settings is not None else VisionSettings.load(PROJECT_ROOT / '.env.vision', shared_llm=settings)
            )
            from deepsearch_agent.working_memory import WorkingMemoryStore
            def session_exists(session_id):
                try:
                    app.state.conversations.get(session_id)
                    return True
                except KeyError:
                    return False
            app.state.working_memory = WorkingMemoryStore(
                app.state.conversations.path.with_suffix('.working-memory'), session_exists=session_exists)
            from deepsearch_agent.llm_wiki import WikiStore
            app.state.wiki = WikiStore(Path(index_path).parent / 'wiki')
            app.state.vision_enabled = visual_settings.backend == 'openai_compatible'
            processor = None
            document_root = Path(index_path).parent / 'documents'
            if app.state.vision_enabled:
                vision_client = OpenAICompatibleClient(visual_settings.llm)
                processor = MultimodalDocumentProcessor(VisionReader(vision_client, visual_settings),
                                                        PageAnalysisCache(document_root / 'visual-cache'),
                                                        allow_image_only=app.state.image_retrieval_enabled)
            app.state.documents = DocumentLibrary(document_root, app.state, processor=processor)
            app.state.attachments = AttachmentStore(document_root / 'attachments')
            def resolve_image(source, page):
                resolver = app.state.attachments if source.startswith('attachment://') else app.state.documents
                return resolver.resolve_visual_image(source, page)
            if app.state.kb.image_index is not None:
                if app.state.kb.image_index.hydrate_legacy_identities(resolve_image):
                    app.state.dirty = True
            visual_evidence = VisualEvidence(vision_client, visual_settings, resolve_image) if vision_client else None
            if reranker is not None and visual_evidence is not None:
                reranker = MultimodalReranker(visual_evidence, text_reranker=reranker)
            app.state.skills = SkillRegistry(skills_path if skills_path is not None else (
                Path(index_path).parent / 'skills' if llm_settings is not None else
                Path(os.environ.get('SKILLS_DIR', str(PROJECT_ROOT / 'skills')))))
            def tool_session_factory(kb, *, settings=None, observer=None, input_evidence=None,
                                     working_memory=None):
                from deepsearch_agent.tool_service import PaperToolSession, ToolError
                from dataclasses import replace
                with app.state.documents.lock:
                    catalog = [dict(record) for record in app.state.documents.records.values()]
                by_source = {d['source_path']: d for d in catalog}
                attached = {c.source_path: c for c in input_evidence or []}
                def read_page(source, page):
                    if source in attached and page == 1:
                        return {'text': attached[source].text, 'page_number': 1}
                    record = by_source.get(source)
                    if record is None:
                        raise ToolError('page_not_found')
                    return app.state.documents.page(record['id'], page)
                limits = settings or agents
                if visual_evidence is not None:
                    limits = replace(limits, max_images=min(limits.max_images, visual_settings.max_images))
                return PaperToolSession(kb, settings=limits, catalog=catalog, page_reader=read_page,
                    image_resolver=resolve_image, analyzer=processor.analyzer if processor else None,
                    cache=processor.cache if processor else None, reranker=reranker,
                    observer=observer, input_evidence=input_evidence, working_memory=working_memory,
                    wiki_store=app.state.wiki)
            app.state.tool_session_factory = tool_session_factory
            app.state.answer_service = AnswerService(
                client=llm_client,
                knowledge_base=app.state.kb,
                use_graph=is_live,
                context_window=settings.context_window,
                max_output_tokens=settings.max_output_tokens,
                reranker=reranker,
                query_expander=query_expander,
                search_intent=search_intent,
                run_recorder=recorder,
                visual_evidence=visual_evidence,
                skill_registry=app.state.skills,
                agent_settings=agents if is_agentic else AgentSettings(),
                tool_session_factory=tool_session_factory,
                mcp_settings=mcp_config,
            )

            try:
                async with AsyncExitStack() as stack:
                    if mcp_server is not None:
                        await stack.enter_async_context(mcp_server.session_manager.run())
                    yield
            finally:
                app.state.documents.close()
        finally:
            if vision_client is not None:
                vision_client.close()
            if isinstance(llm_client, OpenAICompatibleClient):
                llm_client.close()

    app = FastAPI(
        title="DeepSearch Paper Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    from deepsearch_agent.wiki_api import router as wiki_router
    from deepsearch_agent.llm_wiki import WikiError
    app.include_router(wiki_router)
    @app.exception_handler(WikiError)
    async def handle_wiki_error(request: Request, error: WikiError):
        code = str(error)
        status = 404 if code in {'wiki_not_found', 'wiki_not_available'} else 409
        return JSONResponse(status_code=status, content={'detail': code})
    if mcp_config.server_enabled:
        from deepsearch_agent.mcp_runtime import create_mcp_server
        from mcp.server.transport_security import TransportSecuritySettings
        def provider():
            with app.state.lock:
                kb = snapshot_answer_service(app.state).knowledge_base
                if mcp_config.server_sources:
                    kb = kb.restrict_sources(set(mcp_config.server_sources))
            return app.state.tool_session_factory(kb)
        mcp_server = create_mcp_server(provider, settings=mcp_config)
        app.mount('/mcp', mcp_server.streamable_http_app(streamable_http_path='/',
            stateless_http=True, transport_security=TransportSecuritySettings(
                allowed_hosts=['127.0.0.1:*', 'localhost:*', '[::1]:*', 'testserver'],
                allowed_origins=['http://127.0.0.1:*', 'http://localhost:*', 'http://[::1]:*'])))
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
    app.include_router(image_router)
    app.include_router(document_router)
    app.include_router(skill_router)

    @app.exception_handler(SkillError)
    async def handle_skill_error(request: Request, error: SkillError):
        return JSONResponse(status_code=422, content={'detail': str(error)})

    @app.get("/api/health", response_model=HealthResponse, response_model_exclude_none=True)
    def health(request: Request):
        return HealthResponse(
            status="ok",
            mode=request.app.state.mode,
            retrieval_mode=request.app.state.kb.retrieval_mode if not request.app.state.demo_retrieval else None,
            vision_enabled=True if request.app.state.vision_enabled else None,
            image_retrieval_enabled=True if request.app.state.image_retrieval_enabled else None,
            agentic_rag_enabled=True if request.app.state.agentic_rag_enabled else None,
            mcp_server_enabled=True if request.app.state.mcp_server_enabled else None,
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
            validate_skills(request.app.state, body.skill_ids)
            sources = resolve_sources(request.app.state, body.document_ids)
            service = snapshot_answer_service(request.app.state)
        result = service.answer(
            body.question,
            top_k=body.top_k,
            **({'allowed_sources': sources} if sources is not None else {}),
            **({'skill_ids': body.skill_ids} if body.skill_ids is not None else {}),
        )

        return AskResponse(
            mode=request.app.state.mode,
            text=result.text,
            skills_used=getattr(result, 'skills_used', []),
            sources=[
                SourceResponse(
                    citation_id=index,
                    source_path=source.source_path,
                    page_number=source.page_number,
                    text=source.text,
                    page_end=source.page_end,
                    section_path=source.section_path,
                    evidence_kind=source.evidence_kind,
                    visual_pages=source.visual_pages,
                    pages=source.pages,
                    chunk_id=source.chunk_id,
                    content_id=source.content_id,
                    asset_ids=source.asset_ids,
                )
                for index, source in enumerate(result.sources, start=1)
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
