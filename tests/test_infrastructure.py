from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import logging
import os

import pytest
from fastapi.testclient import TestClient

from deepsearch_agent.adapters.infrastructure import (
    CachedEmbeddingClient, InfrastructureSettings, RedisBackend, RequestLimiter,
)
from deepsearch_agent.api import create_app
from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
from deepsearch_agent.adapters.llm_config import LLMSettings


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.expiry = {}
        self.broken = False
        self.calls = 0
        self.closed = False

    def get(self, key):
        self.calls += 1
        if self.broken:
            raise OSError('redis://secret@private')
        return self.data.get(key)

    def set(self, key, value, ex):
        self.get(key)
        self.data[key] = value
        self.expiry[key] = ex
        return True

    def ping(self):
        self.get('ping')
        return True

    def close(self):
        self.closed = True


class Provider:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return [[0.6, 0.8] for _ in texts]


def cached(fake=None, identity='model-a', scope='account-a', clock=lambda: 0):
    provider = Provider()
    backend = RedisBackend(fake or FakeRedis(), InfrastructureSettings(cache_ttl_jitter_ratio=0), clock=clock)
    return CachedEmbeddingClient(provider, backend, identity, 2, scope)


def test_cache_order_dedup_ttl_and_snapshot():
    client = cached()
    assert client.embed(['b', 'a', 'b']) == [[.6, .8]] * 3
    client.embed(['a', 'c'])
    assert client.delegate.calls == [['b', 'a'], ['c']]
    assert set(client.backend.client.expiry.values()) == {86400}
    assert all('account-a' not in key for key in client.backend.client.data)
    assert deepcopy(client) is client
    result = client.embed(['a', 'a'])
    result[0][0] = 100
    assert result[1] == [.6, .8]


def test_model_and_account_isolation():
    fake = FakeRedis()
    for identity, scope in [('a', 'x'), ('b', 'x'), ('a', 'y')]:
        client = cached(fake, identity, scope)
        client.embed(['text'])
        assert client.delegate.calls == [['text']]
    assert len(fake.data) == 3


@pytest.mark.parametrize('value', ['invalid json', '[0, 0]', '[true, 1]', '[1]', '[NaN, 0]', '{}'])
def test_corrupt_cache_recomputed(value):
    client = cached()
    client.backend.client.data[client.key('text')] = value
    assert client.embed(['text']) == [[.6, .8]]
    assert client.delegate.calls == [['text']]


def test_outage_cooldown_and_recovery(caplog):
    now = [0]
    fake = FakeRedis()
    client = cached(fake, clock=lambda: now[0])
    fake.broken = True
    with caplog.at_level(logging.WARNING):
        assert client.embed(['a', 'b']) == [[.6, .8]] * 2
        assert client.backend.status == 'unavailable'
        assert fake.calls == 1
        fake.broken = False
        now[0] = 6
        client.embed(['a'])
        assert client.backend.status == 'available'
    assert 'secret' not in caplog.text and 'private' not in caplog.text


def test_provider_failure_does_not_cache():
    client = cached()
    def fail(texts):
        raise RuntimeError('upstream failed')
    client.delegate.embed = fail
    with pytest.raises(RuntimeError):
        client.embed(['text'])
    assert client.backend.client.data == {}


def test_local_limit_concurrency_expiry_and_bounded_memory():
    now = [0]
    limiter = RequestLimiter(InfrastructureSettings(rate_limit=5), clock=lambda: now[0])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.check('peer')[0], range(30)))
    assert sum(results) == 5
    limiter.max_peers = 1
    assert not limiter.check('another')[0]
    now[0] = 61
    assert limiter.check('another')[0]


def test_redis_rate_limit_and_failure_fallback():
    class Counter(FakeRedis):
        def eval(self, script, numkeys, key, window):
            self.get(key)
            self.data[key] = self.data.get(key, 0) + 1
            return self.data[key], window
    settings = InfrastructureSettings(rate_limit=1)
    fake = Counter()
    limiter = RequestLimiter(settings, RedisBackend(fake, settings))
    assert limiter.check('a')[0]
    assert not limiter.check('a')[0]
    fake.broken = True
    assert limiter.check('b')[0]
    assert not limiter.check('b')[0]


@pytest.mark.parametrize('kwargs', [dict(redis_url='http://secret@host'), dict(namespace='a:b'),
    dict(cache_ttl=0), dict(rate_limit=-1), dict(socket_timeout=float('nan')),
    dict(redis_url='redis://host?socket_timeout=100'), dict(cooldown=float('inf'))])
def test_settings_validation(kwargs):
    with pytest.raises(ValueError):
        InfrastructureSettings(**kwargs)


def test_config_precedence_and_no_secret_errors(tmp_path, monkeypatch):
    path = tmp_path / '.env.infrastructure'
    monkeypatch.setenv('API_RATE_LIMIT', '10')
    path.write_text('API_RATE_LIMIT=20\n', encoding='utf-8')
    assert InfrastructureSettings.load(path).rate_limit == 20
    path.write_text('REDIS_URL=http://secret@host\n', encoding='utf-8')
    with pytest.raises(ValueError) as caught:
        InfrastructureSettings.load(path)
    assert 'secret' not in str(caught.value)


def app_args(tmp_path, **kwargs):
    return dict(index_path=tmp_path / 'index.json', llm_settings=LLMSettings(),
        embedding_settings=EmbeddingSettings(), **kwargs)


def test_api_limit_request_ids_readiness_and_safe_logs(tmp_path, caplog):
    app = create_app(**app_args(tmp_path, infrastructure_settings=InfrastructureSettings(rate_limit=1)))
    with TestClient(app) as client, caplog.at_level(logging.INFO, logger='deepsearch.access'):
        assert client.get('/api/health').json() == {'status': 'ok', 'mode': 'demo'}
        assert client.get('/api/health/live').status_code == 200
        assert client.get('/api/health/ready').json()['dependencies']['rate_limit'] == 'local'
        first = client.post('/api/sessions?secret=never-log-this', json={'title': 'private-title'}, headers={'X-Request-ID': 'test-id'})
        assert first.status_code == 201
        assert first.headers['x-request-id'] == 'test-id'
        second = client.post('/api/sessions', json={'title': 'another'}, headers={'X-Forwarded-For': 'different-peer'})
        assert second.status_code == 429
        assert int(second.headers['retry-after']) > 0
        assert second.json()['request_id'] == second.headers['x-request-id']
        assert client.get('/api/documents').status_code == 200
        app.state.write_blocked = True
        assert client.get('/api/health/ready').status_code == 503
    assert 'never-log-this' not in caplog.text and 'private-title' not in caplog.text


def test_error_responses_keep_request_ids(tmp_path):
    app = create_app(**app_args(tmp_path))

    @app.get('/api/fail')
    def fail():
        raise RuntimeError('private-implementation-details')

    with TestClient(app, raise_server_exceptions=False) as client:
        for response in [client.get('/missing'), client.post('/api/ask', json={}), client.get('/api/fail')]:
            assert response.status_code in {404, 422, 500}
            assert len(response.headers['x-request-id']) == 32
            assert 'private-implementation-details' not in response.text
        assert client.get('/api/fail').json()['request_id']


def test_api_redis_integration_and_cleanup(tmp_path, monkeypatch):
    fake = FakeRedis()
    config = InfrastructureSettings(redis_url='redis://localhost')
    backend = RedisBackend(fake, config)
    monkeypatch.setattr('deepsearch_agent.application.bootstrap.build_redis', lambda settings: backend)
    monkeypatch.setattr(RemoteEmbeddingClient, 'embed', lambda self, texts: [[.6, .8] for _ in texts])
    embeddings = EmbeddingSettings(backend='openai_compatible', api_key='dummy',
                                   base_url='https://example.test/v1', dimensions=2)
    args = app_args(tmp_path, infrastructure_settings=config)
    args['embedding_settings'] = embeddings
    app = create_app(**args)
    with TestClient(app) as client:
        assert isinstance(app.state.kb.embedding_client, CachedEmbeddingClient)
        assert client.post('/api/demo/ingest').status_code == 200  # deepcopy handles pooled client
        assert client.post('/api/ask', json={'question': 'anything'}).status_code == 200
        assert fake.data
        fake.broken = True
        result = client.get('/api/health/ready')
        assert result.status_code == 200
        assert result.json()['status'] == 'degraded'
    assert fake.closed


def test_middleware_does_not_buffer_stream():
    import asyncio
    from types import SimpleNamespace
    from deepsearch_agent.web.operations import OperationsMiddleware

    async def verify():
        release, first_frame = asyncio.Event(), asyncio.Event()
        messages = []

        async def downstream(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'event: status\n\n', 'more_body': True})
            await release.wait()
            await send({'type': 'http.response.body', 'body': b'event: done\n\n', 'more_body': False})

        async def send(message):
            messages.append(message)
            if message['type'] == 'http.response.body':
                first_frame.set()

        scope = dict(type='http', method='POST', path='/api/stream',
            headers=[(b'x-request-id', b'bad id')], app=SimpleNamespace(state=SimpleNamespace()))
        task = asyncio.create_task(OperationsMiddleware(downstream)(scope, None, send))
        try:
            await asyncio.wait_for(first_frame.wait(), timeout=2)
            assert not task.done()
            assert messages[1]['body'] == b'event: status\n\n'
            request_id = dict(messages[0]['headers'])[b'x-request-id'].decode()
            assert len(request_id) == 32
        finally:
            release.set()
            await task
        assert messages[-1]['body'] == b'event: done\n\n'

    asyncio.run(verify())


def test_real_client_outage_is_bounded_and_recovers_to_local_limit():
    pytest.importorskip('redis')
    import socket
    import time
    from deepsearch_agent.adapters.infrastructure import build_redis
    # Reserve an unlistened local port. No dependency on a running server or Docker.
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        config = InfrastructureSettings(redis_url=f'redis://127.0.0.1:{listener.getsockname()[1]}/0',
                                        socket_timeout=.05, rate_limit=1)
        backend = build_redis(config)
        try:
            start = time.monotonic()
            limiter = RequestLimiter(config, backend)
            assert limiter.check('peer')[0]
            assert not limiter.check('peer')[0]
            assert time.monotonic() - start < 2
            assert backend.status == 'unavailable'
        finally:
            backend.close()


@pytest.mark.skipif(not os.environ.get('TEST_REDIS_URL'), reason='Set TEST_REDIS_URL to run real Redis integration')
def test_real_redis_cache_and_atomic_limit():
    from deepsearch_agent.adapters.infrastructure import build_redis
    import uuid
    config = InfrastructureSettings(redis_url=os.environ['TEST_REDIS_URL'],
        namespace='test-' + uuid.uuid4().hex, rate_limit=5, rate_window=2, cache_ttl=2)
    backend = build_redis(config)
    assert backend.call('ping') is True
    client = CachedEmbeddingClient(Provider(), backend, 'test', 2)
    try:
        client.embed(['a'])
        client.embed(['a'])
        assert client.delegate.calls == [['a']]
        assert 0 < backend.client.ttl(client.key('a')) <= 2
        limiter = RequestLimiter(config, backend)
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert sum(pool.map(lambda _: limiter.check('a')[0], range(20))) == 5
    finally:
        for key in backend.client.scan_iter(f'{config.namespace}:*'):
            backend.client.delete(key)
        backend.close()


def test_slow_dns_has_a_total_deadline_and_late_connections_close():
    import time
    from threading import Event
    entered, release, late_close = Event(), Event(), Event()
    class SlowRedis(FakeRedis):
        def __init__(self):
            super().__init__()
            self.close_calls = 0
        def get(self, key):
            self.calls += 1
            entered.set()
            release.wait(timeout=5)  # DNS can ignore socket_connect_timeout.
            return '[1.0, 0.0]'
        def close(self):
            self.close_calls += 1
            self.closed = True
            if self.close_calls >= 2:
                late_close.set()
    server = SlowRedis()
    backend = RedisBackend(server, InfrastructureSettings(socket_timeout=.05, cooldown=1))
    client = CachedEmbeddingClient(Provider(), backend, 'fixture', 2)
    try:
        started = time.monotonic()
        assert client.embed(['a']) == [[.6, .8]]
        assert time.monotonic() - started < .7
        assert entered.is_set()
        assert backend.status == 'unavailable'
        assert client.embed(['a']) == [[.6, .8]]
        assert server.calls == 1  # Cooldown prevents another DNS worker.
        backend.close()
        assert backend.call('get', 'a') is None
    finally:
        backend.close()
        release.set()
    assert late_close.wait(timeout=1)
