from contextlib import asynccontextmanager, AsyncExitStack, ExitStack
from pathlib import Path
from fastapi import FastAPI
import os
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.llm_factory import build_llm
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_agent.workspace.conversations import ConversationStore
from deepsearch_agent.web.conversation_api import router as conversation_router
from deepsearch_agent.application.answer_service import AnswerService
from deepsearch_agent.application.demo import DemoAnswerService
from deepsearch_agent.application.snapshots import snapshot_answer_service
from deepsearch_agent.knowledge.reranker import LLMReranker
from deepsearch_agent.knowledge.multimodal_reranker import MultimodalReranker
from deepsearch_agent.knowledge.query_expander import LLMQueryExpander
from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings, build_knowledge_base
from deepsearch_agent.workspace.documents import DocumentLibrary
from deepsearch_agent.web.document_api import router as document_router
from deepsearch_agent.adapters.vision_config import VisionSettings
from deepsearch_agent.adapters.vision import VisionReader
from deepsearch_agent.workspace.multimodal import MultimodalDocumentProcessor, PageAnalysisCache
from deepsearch_agent.adapters.visual_evidence import VisualEvidence
from deepsearch_agent.adapters.image_embeddings import ImageEmbeddingSettings, SiliconFlowImageEmbeddingClient
from deepsearch_agent.knowledge.image_index import ImageVectorIndex
from deepsearch_agent.workspace.image_assets import AttachmentStore
from deepsearch_agent.web.image_api import router as image_router
from deepsearch_agent.workspace.skills import SkillRegistry
from deepsearch_agent.web.skill_api import router as skill_router
from deepsearch_agent.agents.agent_config import AgentSettings
from deepsearch_agent.adapters.mcp_config import MCPSettings
from deepsearch_agent.adapters.infrastructure import (
    InfrastructureSettings, build_redis, CachedEmbeddingClient, RequestLimiter,
)
from deepsearch_agent.web.operations import OperationsMiddleware, router as operations_router
from deepsearch_agent.knowledge.index_service import IndexService
from deepsearch_agent.web.runtime_state import RuntimeState
from deepsearch_agent.core.paths import PROJECT_ROOT, DEFAULT_INDEX_PATH
from deepsearch_agent.adapters.access_config import AccessSettings
from deepsearch_agent.adapters.database_config import DatabaseSettings
from deepsearch_agent.adapters.tracing_config import tracing_enabled
from deepsearch_agent.adapters.persistence.business import build_database, conversation_store, research_run_store
from deepsearch_agent.adapters.governance_config import GovernanceSettings
from deepsearch_agent.adapters.persistence.governance import GovernanceStore
from deepsearch_agent.application.governance import Governance
from deepsearch_agent.web.governance import router as governance_router
from deepsearch_agent.adapters.persistence.tool_ledger import read_backlog


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
    infrastructure_settings: InfrastructureSettings | None = None,
    access_settings: AccessSettings | None = None,
    _workspace_id: str | None = None,
    database_settings: DatabaseSettings | None = None,
    _database=None,
    governance_settings: GovernanceSettings | None = None,
    _governance=None,
) -> FastAPI:
    governance_config = governance_settings if governance_settings is not None else (
        GovernanceSettings() if llm_settings is not None else GovernanceSettings.load(PROJECT_ROOT / '.env.governance'))
    agents = agent_settings if agent_settings is not None else (
        AgentSettings(enabled=True) if llm_settings is not None else AgentSettings.load(PROJECT_ROOT / '.env.agent'))
    mcp_config = mcp_settings if mcp_settings is not None else (
        MCPSettings() if llm_settings is not None else MCPSettings.load(PROJECT_ROOT / '.env.mcp'))
    access = access_settings if access_settings is not None else (
        AccessSettings() if llm_settings is not None else AccessSettings.load())
    database_config = database_settings if database_settings is not None else (
        DatabaseSettings() if llm_settings is not None else DatabaseSettings.load())
    if access.mode == 'team':
        if mcp_config.server_enabled or mcp_config.enabled:
            raise ValueError('Team mode requires MCP disabled until workspace-scoped MCP authorization is configured')
        if conversation_path is not None or Path(index_path) != DEFAULT_INDEX_PATH:
            raise ValueError('Team mode uses ACCESS_DATA_ROOT; legacy index/session paths must not be reused')
        from dataclasses import replace
        import hashlib
        from deepsearch_agent.application.bootstrap_team import create_team_app
        infrastructure = infrastructure_settings if infrastructure_settings is not None else (
            InfrastructureSettings() if llm_settings is not None else
            InfrastructureSettings.load(PROJECT_ROOT / '.env.infrastructure'))
        def runtime_factory(root, ident, database, governor):
            namespace = infrastructure.namespace[:16] + '_' + hashlib.sha256(
                (infrastructure.namespace + ':' + ident).encode()).hexdigest()[:40]
            scoped_infra = replace(infrastructure, namespace=namespace)
            tracing = (trace_path is not None or bool(os.environ.get('RAG_TRACE_DB'))
                       or (llm_settings is None and tracing_enabled(PROJECT_ROOT / '.env.tracing')))
            return create_app(index_path=root / 'index.json', llm_settings=llm_settings,
                embedding_settings=embedding_settings, search_intent=search_intent,
                trace_path=root / 'traces.sqlite3' if tracing else None,
                vision_settings=vision_settings, image_embedding_settings=image_embedding_settings,
                skills_path=skills_path, agent_settings=agents, mcp_settings=mcp_config,
                infrastructure_settings=scoped_infra, access_settings=AccessSettings(), _workspace_id=ident,
                database_settings=database_config, _database=database, _governance=governor,
                governance_settings=governance_config)
        return create_team_app(access, runtime_factory, database_config,
            lock_path=PROJECT_ROOT / 'data/runtime/deployment.lock' if llm_settings is None else None,
            governance_settings=governance_config)
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
        if settings.backend != 'demo' and not agents.enabled:
            raise ValueError('AGENTIC_RAG_ENABLED=false moved to offline baselines; use deepsearch_experiments.legacy.qa')
        if search_intent is not None:
            raise ValueError('Search policy injection is offline only: deepsearch_experiments.legacy.qa')
        llm_client = build_llm(settings)
        resources = ExitStack()
        if callable(getattr(llm_client, "close", None)):
            resources.callback(llm_client.close)
        vision_client = None
        redis_backend = None

        try:
            if llm_settings is None and _workspace_id is None:
                from deepsearch_agent.adapters.persistence.deployment_lock import DeploymentLock
                resources.enter_context(DeploymentLock(PROJECT_ROOT / 'data/runtime/deployment.lock'))
            app.state.governance = _governance or Governance(GovernanceStore(Path(index_path).parent / 'governance.sqlite3'),governance_config,
                backlog_reader=lambda:read_backlog(Path(index_path).parent))
            if _governance is None:
                app.state.governance.start()
                resources.callback(app.state.governance.close)
            app.state.business_database = _database if _database is not None else build_database(database_config)
            if _database is None and app.state.business_database is not None:
                resources.callback(app.state.business_database.close)
            infrastructure = infrastructure_settings if infrastructure_settings is not None else (
                InfrastructureSettings() if llm_settings is not None else
                InfrastructureSettings.load(PROJECT_ROOT / '.env.infrastructure'))
            redis_backend = build_redis(infrastructure)
            if redis_backend is not None:
                resources.callback(redis_backend.close)
            app.state.infrastructure_settings = infrastructure
            app.state.redis_backend = redis_backend
            app.state.request_limiter = RequestLimiter(infrastructure, redis_backend)
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
            app.state.index = IndexService(build_knowledge_base(embeddings), Path(index_path))
            if redis_backend is not None and not app.state.demo_retrieval:
                import hashlib
                app.state.kb.embedding_client = CachedEmbeddingClient(
                    app.state.kb.embedding_client, redis_backend, embeddings.identity,
                    embeddings.dimensions, hashlib.sha256(embeddings.api_key.encode()).hexdigest())
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
            from weakref import WeakValueDictionary
            app.state.turn_locks = WeakValueDictionary()
            app.state.agentic_rag_enabled = is_agentic = settings.backend != 'demo' and agents.enabled
            app.state.mcp_server_enabled = mcp_config.server_enabled

            app.state.conversations = conversation_store(app.state.business_database, _workspace_id or 'local',
                conversation_path
                if conversation_path is not None
                else Path(index_path).with_name("conversations.sqlite3")
            )

            if app.state.index_path.exists():
                try:
                    app.state.index.restore()
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
                if local_trace_path is None and tracing_enabled(PROJECT_ROOT / '.env.tracing'):
                    local_trace_path = Path(index_path).parent / 'traces.sqlite3'
            if local_trace_path is not None:
                from deepsearch_agent.adapters.tracing import RAGTraceRecorder
                recorder = RAGTraceRecorder(local_trace_path, metadata={
                    'application': 'deepsearch-paper-agent', 'main_model': settings.model,
                    'embedding_model': embeddings.model, 'retrieval_backend': embeddings.backend,
                    'index_path': str(app.state.index_path),
                    **({'workspace_id': _workspace_id} if _workspace_id is not None else {}),
                })
            visual_settings = vision_settings if vision_settings is not None else (
                VisionSettings() if llm_settings is not None else VisionSettings.load(PROJECT_ROOT / '.env.vision', shared_llm=settings)
            )
            from dataclasses import asdict
            from deepsearch_agent.core.identity import fingerprint
            app.state.run_identity = fingerprint({
                'tool_ledger_protocol': 1,
                'llm': asdict(settings), 'embedding': asdict(embeddings),
                'image_embedding': asdict(image_settings), 'vision': asdict(visual_settings),
                'agents': asdict(agents), 'mcp': asdict(mcp_config)})
            from deepsearch_agent.adapters.persistence.research_runs import ResearchRunStore
            from deepsearch_agent.adapters.persistence.checkpoints import checkpoint_saver
            import sqlite3
            run_root = Path(index_path).parent
            from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger
            app.state.tool_ledger = SQLiteToolLedger(run_root / 'tool_executions.sqlite3')
            app.state.tool_ledger.recover_interrupted()
            app.state.governance.register_workspace(_workspace_id or 'local',app.state.tool_ledger,redis_backend)
            app.state.research_runs = research_run_store(app.state.business_database, _workspace_id or 'local',
                                                        run_root / 'research_runs.sqlite3')
            app.state.run_tokens = {}
            checkpoint_connection = sqlite3.connect(run_root / 'research_checkpoints.sqlite3',
                                                     check_same_thread=False)
            resources.callback(checkpoint_connection.close)
            app.state.research_checkpointer = checkpoint_saver(checkpoint_connection)
            from deepsearch_agent.workspace.working_memory import WorkingMemoryStore
            def session_exists(session_id):
                try:
                    app.state.conversations.get(session_id)
                    return True
                except KeyError:
                    return False
            app.state.working_memory = WorkingMemoryStore(
                app.state.conversations.path.with_suffix('.working-memory'), session_exists=session_exists)
            from deepsearch_agent.workspace.llm_wiki import WikiStore
            app.state.wiki = WikiStore(Path(index_path).parent / 'wiki')
            app.state.vision_enabled = visual_settings.backend == 'openai_compatible'
            processor = None
            document_root = Path(index_path).parent / 'documents'
            if app.state.vision_enabled:
                vision_client = OpenAICompatibleClient(visual_settings.llm)
                resources.callback(vision_client.close)
                processor = MultimodalDocumentProcessor(VisionReader(vision_client, visual_settings),
                                                        PageAnalysisCache(document_root / 'visual-cache'),
                                                        allow_image_only=app.state.image_retrieval_enabled)
            app.state.documents = DocumentLibrary(document_root, app.state.index, processor=processor)
            resources.callback(app.state.documents.close)
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
                from deepsearch_agent.agents.tool_service import PaperToolSession, ToolError
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
            app.state.answer_service = (AnswerService if is_live else DemoAnswerService)(
                client=llm_client,
                knowledge_base=app.state.kb,
                context_window=settings.context_window,
                max_output_tokens=settings.max_output_tokens,
                reranker=reranker,
                query_expander=query_expander,
                run_recorder=recorder,
                visual_evidence=visual_evidence,
                skill_registry=app.state.skills,
                agent_settings=agents if is_agentic else AgentSettings(),
                tool_session_factory=tool_session_factory,
                mcp_settings=mcp_config,
                wiki_store=app.state.wiki,
                checkpointer=app.state.research_checkpointer,
                tool_ledger=app.state.tool_ledger,
            )

            app.state.index.on_publish = lambda kb: setattr(app.state.answer_service, 'knowledge_base', kb)
            from deepsearch_agent.application.session_runs import RunWorkers
            app.state.run_workers = RunWorkers()
            resources.callback(app.state.run_workers.close)
            async with AsyncExitStack() as stack:
                if mcp_server is not None:
                    await stack.enter_async_context(mcp_server.session_manager.run())
                yield
        finally:
            resources.close()

    app = FastAPI(
        title="DeepSearch Paper Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state = RuntimeState()
    from deepsearch_agent.web.main_routes import router as main_router
    from deepsearch_agent.web.errors import register_error_handlers
    app.include_router(main_router)
    register_error_handlers(app)
    app.add_middleware(OperationsMiddleware)
    app.include_router(operations_router)
    app.include_router(governance_router)

    from deepsearch_agent.web.wiki_api import router as wiki_router
    app.include_router(wiki_router)
    if mcp_config.server_enabled:
        from deepsearch_agent.adapters.mcp_runtime import create_mcp_server
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
    app.include_router(conversation_router)
    app.include_router(image_router)
    app.include_router(document_router)
    app.include_router(skill_router)

    @app.get('/api/access/status', include_in_schema=False)
    def access_status():
        return {'mode': 'local'}


    return app
