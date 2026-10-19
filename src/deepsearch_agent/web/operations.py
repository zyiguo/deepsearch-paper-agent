"""Streaming-safe request IDs, metadata-only access logs, and dependency readiness."""
import json
import logging
import re
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders

log = logging.getLogger('deepsearch.access')
router = APIRouter(prefix='/api/health', tags=['运行状态'])


class OperationsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        supplied = headers.get(b'x-request-id', b'').decode('ascii', errors='replace')
        request_id = supplied if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', supplied) else uuid.uuid4().hex
        scope.setdefault('state', {})['request_id'] = request_id
        started, status = time.monotonic(), 500

        async def tracked_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                MutableHeaders(scope=message)['X-Request-ID'] = request_id
            await send(message)

        try:
            state = scope['app'].state
            limiter = getattr(state, 'request_limiter', None)
            if limiter and scope['method'] in {'POST', 'PUT', 'PATCH', 'DELETE'} and scope['path'].startswith('/api/'):
                peer = scope.get('client') or ('unknown', 0)
                allowed, retry = await run_in_threadpool(limiter.check, peer[0])
                if not allowed:
                    response = JSONResponse({'detail': '请求过于频繁，请稍后重试', 'request_id': request_id},
                                            status_code=429, headers={'Retry-After': str(retry)})
                    return await response(scope, receive, tracked_send)
            await self.app(scope, receive, tracked_send)
        finally:
            # Route templates avoid logging questions, filenames, session IDs or query strings.
            route = getattr(scope.get('route'), 'path', 'unmatched')
            log.info(json.dumps({'event': 'http_request', 'request_id': request_id,
                'method': scope['method'], 'route': route, 'status': status,
                'duration_ms': round((time.monotonic() - started) * 1000, 2)}))


@router.get('/live')
def live():
    return {'status': 'ok'}


@router.get('/ready')
def ready(request: Request):
    state = request.app.state
    backend = state.redis_backend
    redis_status = 'disabled'
    if backend:
        redis_status = 'available' if backend.call('ping') else 'unavailable'
    blocked = state.write_blocked
    database = getattr(state, 'business_database', None)
    database_ready = database is None or database.ping()
    status = 'not_ready' if blocked or not database_ready else ('degraded' if redis_status == 'unavailable' else 'ok')
    return JSONResponse({'status': status, 'dependencies': {
        'redis': redis_status, 'index': 'write_blocked' if blocked else 'ready',
        'rate_limit': 'disabled' if not state.infrastructure_settings.rate_limit else (
            'redis' if redis_status == 'available' else 'local'),
        **({'database':'available' if database_ready else 'unavailable'} if database is not None else {}),
    }}, status_code=503 if blocked or not database_ready else 200)
