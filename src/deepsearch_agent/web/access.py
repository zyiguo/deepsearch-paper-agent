"""Session transport and default-deny workspace dispatch, including files and SSE."""
import re

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from fastapi.responses import JSONResponse

from deepsearch_agent.core.errors import ApplicationError

router = APIRouter(tags=['账号与工作空间'])
WRITE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
PUBLIC = frozenset({'/api/access/status', '/api/access/login', '/api/health/live', '/api/health/ready'})


class AccessBoundary:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self.app(scope, receive, send)
        if scope['type'] != 'http':
            return await send({'type': 'websocket.close', 'code': 1008})
        request = Request(scope, receive=receive)
        path = scope['path']
        service = scope['app'].state.access
        headers = Headers(scope=scope)

        async def private_send(message):
            if message['type'] == 'http.response.start':
                response_headers = MutableHeaders(scope=message)
                response_headers['Cache-Control'] = 'private, no-store'
                response_headers['X-Content-Type-Options'] = 'nosniff'
            await send(message)

        try:
            if scope['method'] in WRITE_METHODS:
                origin = headers.get('origin')
                if (headers.get('x-deepsearch-request') != '1'
                        or (origin is not None and origin not in self.settings.origins)
                        or headers.get('sec-fetch-site') == 'cross-site'):
                    raise ApplicationError(403, '请求来源校验失败，请从工作台重新操作')
                if path.startswith('/api/access/') or path.startswith('/api/workspaces') or path.startswith('/api/admin/'):
                    length = headers.get('content-length', '0')
                    if not length.isdigit() or int(length) > 8192:
                        raise ApplicationError(413, '请求过大')
                    # Also bound chunked requests before Pydantic reads the JSON body.
                    body = bytearray()
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > 8192:
                            raise ApplicationError(413, '请求过大')
                        body.extend(chunk)
                    upstream_receive = receive
                    delivered = False
                    async def replay():
                        nonlocal delivered
                        if delivered:
                            return await upstream_receive()
                        delivered = True
                        return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                    receive = replay
            if path in PUBLIC:
                return await self.app(scope, receive, private_send)
            token = request.cookies.get(self.settings.cookie_name)
            user = await run_in_threadpool(service.require_user, token)
            scope.setdefault('state', {})['access_user'] = user
            match = re.fullmatch(r'/w/([a-f0-9]{32})(/api(?:/.*)?)', path)
            if match:
                workspace, inner = match.groups()
                role = await run_in_threadpool(service.authorize, user, workspace,
                                               write=scope['method'] not in {'GET', 'HEAD'})
                child = await scope['app'].state.workspace_runtimes.get(workspace)
                child_scope = {**scope, 'path': inner, 'raw_path': inner.encode('utf-8'), 'root_path': '',
                               'state': {**scope['state'], 'workspace_id': workspace, 'workspace_role': role,
                                         'allowed_origins': self.settings.origins}}
                # Keep one request ID through both composition roots.
                child_scope['headers'] = [(k, v) for k, v in scope['headers'] if k.lower() != b'x-request-id']
                child_scope['headers'].append((b'x-request-id', scope['state']['request_id'].encode('ascii')))
                return await child(child_scope, receive, private_send)
            if path.startswith(('/api/access/', '/api/workspaces', '/api/admin/')):
                return await self.app(scope, receive, private_send)
            raise ApplicationError(404, '请通过工作空间入口访问资源')
        except ApplicationError as error:
            return await JSONResponse({'detail': error.detail}, status_code=error.status_code,
                headers={'Retry-After': '60'} if error.status_code == 429 else {})(scope, receive, private_send)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LoginBody(StrictBody):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128, repr=False)


class UserBody(LoginBody):
    display_name: str = Field(min_length=1, max_length=100)


class WorkspaceBody(StrictBody):
    name: str = Field(min_length=1, max_length=100)


class PasswordBody(StrictBody):
    current_password: str = Field(min_length=1, max_length=128, repr=False)
    new_password: str = Field(min_length=12, max_length=128, repr=False)


class MemberBody(StrictBody):
    username: str = Field(min_length=3, max_length=64)
    role: str = Field(pattern='^(editor|viewer)$')


def current(request):
    return request.state.access_user


@router.get('/api/access/status')
def status():
    return {'mode': 'team'}


@router.get('/api/health/live')
def health():
    # Team control-plane readiness; per-workspace dependency checks require membership.
    return {'status': 'ok', 'scope': 'team-control'}


@router.get('/api/health/ready')
def ready(request: Request):
    database = getattr(request.app.state, 'business_database', None)
    if database is None:
        return health()
    available = database.ping()
    return JSONResponse({'status':'ok' if available else 'not_ready', 'scope':'team-control',
                         'dependencies':{'database':'available' if available else 'unavailable'}},
                        status_code=200 if available else 503)


@router.post('/api/access/login')
def login(body: LoginBody, request: Request, response: Response):
    settings = request.app.state.access_settings
    token = request.app.state.access.login(body.username, body.password, request.cookies.get(settings.cookie_name))
    response.set_cookie(settings.cookie_name, token, max_age=settings.session_seconds,
                        httponly=True, secure=settings.cookie_secure, samesite='strict', path='/')
    return {'authenticated': True}


@router.get('/api/access/me')
def me(request: Request):
    user = current(request)
    return {'user': user, 'workspaces': request.app.state.access.store.workspaces(user['id'])}


@router.post('/api/access/logout')
def logout(request: Request, response: Response):
    settings = request.app.state.access_settings
    request.app.state.access.store.logout(request.cookies[settings.cookie_name], current(request)['id'])
    response.delete_cookie(settings.cookie_name, secure=settings.cookie_secure, httponly=True,
                           samesite='strict', path='/')
    return {'authenticated': False}


@router.post('/api/access/password')
def change_password(body: PasswordBody, request: Request, response: Response):
    request.app.state.access.change_password(current(request), body.current_password, body.new_password)
    settings = request.app.state.access_settings
    response.delete_cookie(settings.cookie_name, secure=settings.cookie_secure, httponly=True,
                           samesite='strict', path='/')
    return {'changed': True}


@router.get('/api/workspaces')
def workspaces(request: Request):
    return request.app.state.access.store.workspaces(current(request)['id'])


@router.post('/api/workspaces', status_code=201)
def create_workspace(body: WorkspaceBody, request: Request):
    return request.app.state.access.create_workspace(current(request), body.name)


@router.get('/api/workspaces/{workspace}/members')
def members(workspace: str, request: Request):
    service = request.app.state.access
    service.authorize(current(request), workspace, owner=True)
    return service.store.members(workspace)


@router.put('/api/workspaces/{workspace}/members')
def set_member(workspace: str, body: MemberBody, request: Request):
    request.app.state.access.set_member(current(request), workspace, body.username, body.role)
    return {'updated': True}


@router.delete('/api/workspaces/{workspace}/members/{user_id}')
def remove_member(workspace: str, user_id: str, request: Request):
    request.app.state.access.remove_member(current(request), workspace, user_id)
    return {'removed': True}


@router.get('/api/admin/users')
def users(request: Request):
    service = request.app.state.access
    service.require_admin(current(request))
    return service.store.users()


@router.post('/api/admin/users', status_code=201)
def create_user(body: UserBody, request: Request):
    service = request.app.state.access
    service.require_admin(current(request))
    return service.create_user(body.username, body.display_name, body.password, actor=current(request)['id'])


@router.post('/api/admin/users/{ident}/disable')
def disable_user(ident: str, request: Request):
    service = request.app.state.access
    service.require_admin(current(request))
    target = next((u for u in service.store.users() if u['id'] == ident), None)
    if target is None:
        raise ApplicationError(404, '用户不存在')
    if target['is_admin']:
        raise ApplicationError(409, '不能通过此接口停用系统管理员')
    service.store.disable_user(ident, current(request)['id'])
    return {'disabled': True}


@router.post('/api/admin/users/{ident}/enable')
def enable_user(ident: str, request: Request):
    service = request.app.state.access
    service.require_admin(current(request))
    target = next((u for u in service.store.users() if u['id'] == ident), None)
    if target is None:
        raise ApplicationError(404, '用户不存在')
    if target['is_admin']:
        raise ApplicationError(409, '不能通过此接口修改系统管理员')
    service.store.enable_user(ident, current(request)['id'])
    return {'enabled': True}
