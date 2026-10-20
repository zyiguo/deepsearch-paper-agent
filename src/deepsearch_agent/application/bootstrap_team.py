"""Team composition root; existing API runtimes have isolated resource lifetimes."""
from contextlib import asynccontextmanager, AsyncExitStack, ExitStack
import asyncio
import re

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from deepsearch_agent.adapters.infrastructure import InfrastructureSettings, RequestLimiter
from deepsearch_agent.adapters.persistence.access import AccessStore
from deepsearch_agent.application.access import AccessService
from deepsearch_agent.core.errors import ApplicationError
from deepsearch_agent.core.filesystem import native_path
from deepsearch_agent.web.access import AccessBoundary, router
from deepsearch_agent.web.errors import register_error_handlers
from deepsearch_agent.web.operations import OperationsMiddleware
from deepsearch_agent.adapters.persistence.business import build_database, access_store
from deepsearch_agent.application.governance import Governance
from deepsearch_agent.adapters.persistence.governance import GovernanceStore
from deepsearch_agent.web.governance import router as governance_router
from deepsearch_agent.adapters.persistence.tool_ledger import read_backlog


class WorkspaceRuntimes:
    def __init__(self, settings, factory, stack):
        self.settings, self.factory, self.stack = settings, factory, stack
        self.apps = {}
        self.lock = asyncio.Lock()

    async def get(self, ident):
        if not re.fullmatch(r'[a-f0-9]{32}', ident):
            raise ApplicationError(404, '工作空间不存在')
        async with self.lock:
            if ident not in self.apps:
                if len(self.apps) >= self.settings.max_open_workspaces:
                    raise ApplicationError(503, '已达到本进程工作空间容量，请联系管理员')
                root = (self.settings.root / 'workspaces' / ident).resolve()
                if (not root.is_relative_to(self.settings.root)
                        or root.parent != (self.settings.root / 'workspaces').resolve()):
                    raise ApplicationError(503, '工作空间路径无效')
                app = self.factory(native_path(root), ident)
                await self.stack.enter_async_context(app.router.lifespan_context(app))
                self.apps[ident] = app
            return self.apps[ident]


def create_team_app(settings, runtime_factory, database_settings, *, lock_path=None, governance_settings):
    @asynccontextmanager
    async def lifespan(app):
        with ExitStack() as resources:
            if lock_path is not None:
                from deepsearch_agent.adapters.persistence.deployment_lock import DeploymentLock
                resources.enter_context(DeploymentLock(lock_path))
            database = build_database(database_settings)
            app.state.business_database = database
            if database is not None:
                resources.callback(database.close)
            store = access_store(database, settings.root / 'identity.sqlite3')
            app.state.access = AccessService(store, settings)
            app.state.governance = Governance(GovernanceStore(settings.root / 'governance.sqlite3'),governance_settings,
                backlog_reader=lambda:read_backlog(settings.root/'workspaces',workspaces=True))
            app.state.governance.start()
            resources.callback(app.state.governance.close)
            app.state.request_limiter = RequestLimiter(InfrastructureSettings(rate_limit=30, rate_window=60))
            async with AsyncExitStack() as stack:
                factory = lambda root, ident: runtime_factory(root,ident,database,app.state.governance)
                app.state.workspace_runtimes = WorkspaceRuntimes(settings, factory, stack)
                yield

    app = FastAPI(title='DeepSearch Team', version='0.1.0', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.access_settings = settings
    app.include_router(router)
    app.include_router(governance_router)
    register_error_handlers(app)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Pydantic errors can echo submitted passwords; never return their input fields.
        return JSONResponse({'detail': '请求参数不符合要求，请检查输入长度与格式'}, status_code=422)

    app.add_middleware(AccessBoundary, settings=settings)
    app.add_middleware(OperationsMiddleware)
    return app
