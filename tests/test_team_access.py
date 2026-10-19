"""Team authorization through real ASGI routes and isolated synthetic stores."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from deepsearch_agent.adapters.access_config import AccessSettings
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.mcp_config import MCPSettings
from deepsearch_agent.adapters.passwords import hash_password, verify_password
from deepsearch_agent.adapters.persistence.access import AccessStore
from deepsearch_agent.application.access import AccessService
from deepsearch_agent.application.bootstrap import create_app


PASSWORD = 'synthetic-test-password-123'
WRITE = {'X-DeepSearch-Request': '1'}


@pytest.fixture
def team(tmp_path):
    config = AccessSettings(mode='team', root=tmp_path / 'team', cookie_secure=False,
                            origins=('http://testserver',), max_open_workspaces=3)
    app = create_app(llm_settings=LLMSettings(), access_settings=config, trace_path=tmp_path / 'never-global.sqlite3')
    with TestClient(app) as client:
        service = app.state.access
        for name in ('alice', 'bob', 'admin'):
            service.create_user(name, name.title(), PASSWORD, admin=name == 'admin')
        yield app, client, config


def login(client, name):
    # Simulate separate browser sessions; an actual re-login rotates the old cookie.
    client.cookies.clear()
    result = client.post('/api/access/login', json={'username': name, 'password': PASSWORD}, headers=WRITE)
    assert result.status_code == 200, result.text


def workspace(client, name):
    result = client.post('/api/workspaces', json={'name': name}, headers=WRITE)
    assert result.status_code == 201, result.text
    return result.json()['id']


def test_password_hashes_salt_work_factor_and_wrong_password():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and PASSWORD not in first
    assert first.startswith('scrypt-v1$') and verify_password(PASSWORD, first)
    assert not verify_password('wrong', first)
    assert not verify_password(PASSWORD, 'corrupt')
    with pytest.raises(ValueError):
        hash_password('short')


def test_sessions_expire_are_hashed_rotate_and_revoke(tmp_path):
    now = [1000.0]
    store = AccessStore(tmp_path / 'identity.sqlite3', clock=lambda: now[0])
    ident = store.create_user('alice', 'Alice', hash_password(PASSWORD))
    first = store.new_session(ident, 300)
    second = store.new_session(ident, 300, first)
    assert store.authenticate(first) is None
    assert store.authenticate(second)['id'] == ident
    with store.connect() as db:
        assert db.execute('SELECT token_hash FROM access_sessions').fetchone()[0] == hashlib.sha256(second.encode()).hexdigest()
    now[0] += 301
    assert store.authenticate(second) is None
    token = store.new_session(ident, 300)
    store.reset_password('alice', hash_password(PASSWORD + 'new'))
    assert store.authenticate(token) is None
    assert all(secret not in store.path.read_bytes() for secret in (PASSWORD.encode(), second.encode()))


def test_account_login_attempt_reservation_is_atomic(tmp_path):
    store = AccessStore(tmp_path / 'identity.sqlite3')
    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(pool.map(lambda _: store.login_allowed('unknown-user'), range(20)))
    assert sum(allowed) == 5


def test_team_anonymous_default_deny_csrf_cookie_and_no_password_echo(team):
    app, client, config = team
    assert client.get('/api/access/status').json() == {'mode': 'team'}
    for path in ('/api/sessions', '/api/documents', '/api/wiki', '/mcp', '/w/' + 'a'*32 + '/api/documents'):
        result = client.get(path)
        assert result.status_code == 401 and result.headers['cache-control'] == 'private, no-store'
        assert result.headers.get('x-request-id')
    assert client.get('/api/health/ready').json()['scope'] == 'team-control'
    body = {'username': 'alice', 'password': PASSWORD}
    assert client.post('/api/access/login', json=body).status_code == 403
    for origin in ('https://evil.example', 'null'):
        assert client.post('/api/access/login', json=body, headers={**WRITE, 'Origin': origin}).status_code == 403
    invalid = client.post('/api/access/login', json={**body, 'password': PASSWORD*10}, headers=WRITE)
    assert invalid.status_code == 422 and PASSWORD not in invalid.text
    assert client.post('/api/access/login', content=b'x'*8193, headers=WRITE).status_code == 413
    response = client.post('/api/access/login', json=body, headers=WRITE)
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Max-Age=28800' in cookie
    assert client.get('/api/sessions').status_code == 404
    raw = client.cookies.get(config.cookie_name)
    assert client.post('/api/access/logout', headers=WRITE).status_code == 200
    client.cookies.set(config.cookie_name, raw)
    assert client.get('/api/access/me').status_code == 401


def test_workspace_isolation_and_admin_has_no_implicit_data_access(team):
    app, client, config = team
    login(client, 'alice')
    alpha = workspace(client, 'Alpha')
    prefix = f'/w/{alpha}/api'
    assert client.post(prefix + '/demo/ingest', headers=WRITE).status_code == 200
    session = client.post(prefix + '/sessions', json={'title': 'Alice private'}, headers=WRITE).json()['id']
    assert client.post(prefix + f'/sessions/{session}/turns', json={'question': '模型怎么训练？'}, headers=WRITE).status_code == 201
    assert client.get(prefix + f'/sessions/{session}').json()['turns']
    login(client, 'bob')
    beta = workspace(client, 'Beta')
    bob_prefix = f'/w/{beta}/api'
    assert client.get(bob_prefix + '/documents').json() == []
    assert client.get(bob_prefix + '/sessions').json() == []
    for path in ('/documents', f'/sessions/{session}', '/wiki', '/knowledge-base/status'):
        assert client.get(prefix + path).status_code == 404
    assert client.get(bob_prefix + f'/sessions/{session}').status_code == 404
    login(client, 'admin')
    assert client.get('/api/admin/users').status_code == 200
    assert client.get(prefix + '/sessions').status_code == 404
    first = app.state.workspace_runtimes.apps[alpha].state
    second = app.state.workspace_runtimes.apps[beta].state
    assert first.index_path.parent != second.index_path.parent
    assert first.conversations.path != second.conversations.path
    assert first.infrastructure_settings.namespace != second.infrastructure_settings.namespace
    assert first.answer_service.run_recorder is not second.answer_service.run_recorder
    assert not (config.root.parent / 'never-global.sqlite3').exists()


def test_role_changes_apply_each_request_and_owner_is_protected(team):
    app, client, _ = team
    login(client, 'alice')
    ident = workspace(client, 'shared')
    owner_token = dict(client.cookies)
    members = f'/api/workspaces/{ident}/members'
    assert client.put(members, json={'username': 'bob', 'role': 'viewer'}, headers=WRITE).status_code == 200
    assert client.put(members, json={'username': 'alice', 'role': 'viewer'}, headers=WRITE).status_code == 409
    alice_id = app.state.access.store.user_by_name('alice')['id']
    assert client.delete(members + '/' + alice_id, headers=WRITE).status_code == 409
    login(client, 'bob')
    bob_token = dict(client.cookies)
    prefix = f'/w/{ident}/api'
    assert client.get(prefix + '/documents').status_code == 200
    for path in ('/demo/ingest', '/ask', '/sessions', '/wiki/compile', '/knowledge-base/load',
                 '/documents/upload', '/sessions/foreign/runs/foreign/resume/stream'):
        assert client.post(prefix + path, json={}, headers=WRITE).status_code == 403
    assert client.get(members).status_code == 403
    client.cookies.clear()
    client.cookies.update(owner_token)
    assert client.put(members, json={'username': 'bob', 'role': 'editor'}, headers=WRITE).status_code == 200
    client.cookies.clear()
    client.cookies.update(bob_token)
    assert client.post(prefix + '/sessions', json={'title': 'editor'}, headers=WRITE).status_code == 201
    client.cookies.clear()
    client.cookies.update(owner_token)
    bob_id = app.state.access.store.user_by_name('bob')['id']
    assert client.delete(members + '/' + bob_id, headers=WRITE).status_code == 200
    client.cookies.clear()
    client.cookies.update(bob_token)
    assert client.get(prefix + '/documents').status_code == 404


def test_disabled_account_sessions_and_user_provisioning(team):
    app, client, config = team
    login(client, 'bob')
    token = dict(client.cookies)
    body = {'username': 'charlie', 'display_name': 'Charlie', 'password': PASSWORD}
    assert client.post('/api/admin/users', json=body, headers=WRITE).status_code == 403
    login(client, 'admin')
    assert client.post('/api/admin/users', json=body, headers=WRITE).status_code == 201
    assert client.post('/api/admin/users', json=body, headers=WRITE).status_code == 409
    bob = app.state.access.store.user_by_name('bob')
    assert client.post('/api/admin/users/' + bob['id'] + '/disable', headers=WRITE).status_code == 200
    admin = app.state.access.store.user_by_name('admin')
    assert client.post('/api/admin/users/' + admin['id'] + '/disable', headers=WRITE).status_code == 409
    client.cookies.clear()
    client.cookies.update(token)
    assert client.get('/api/access/me').status_code == 401
    login(client, 'admin')
    assert client.post('/api/admin/users/' + bob['id'] + '/enable', headers=WRITE).status_code == 200
    client.cookies.clear()
    client.cookies.update(token)
    assert client.get('/api/access/me').status_code == 401  # Enabling does not revive old cookies.
    login(client, 'bob')
    assert client.get('/api/access/me').status_code == 200


def test_password_change_revokes_all_devices_and_login_cannot_race_reset(team, monkeypatch):
    app, client, _ = team
    login(client, 'alice')
    old = dict(client.cookies)
    response = client.post('/api/access/password', json={'current_password':'wrong', 'new_password':PASSWORD+'new'}, headers=WRITE)
    assert response.status_code == 403
    response = client.post('/api/access/password', json={'current_password':PASSWORD, 'new_password':PASSWORD+'new'}, headers=WRITE)
    assert response.status_code == 200
    client.cookies.update(old)
    assert client.get('/api/access/me').status_code == 401
    store = app.state.access.store
    user = store.user_by_name('alice')
    store.reset_password('alice', hash_password(PASSWORD))
    with pytest.raises(ValueError, match='Credentials changed'):
        store.new_session(user['id'], 300, expected_hash=user['password_hash'])


def test_workspace_runtime_failure_is_not_cached_and_shutdown_closes_resources(tmp_path):
    import asyncio
    from contextlib import asynccontextmanager, AsyncExitStack
    from fastapi import FastAPI
    from deepsearch_agent.application.bootstrap_team import WorkspaceRuntimes
    calls = []
    @asynccontextmanager
    async def failing(app):
        try:
            raise RuntimeError('fixture startup failure')
            yield
        finally:
            calls.append('failed-closed')
    @asynccontextmanager
    async def healthy(app):
        try:
            yield
        finally:
            calls.append('healthy-closed')
    attempt = [0]
    def factory(root, ident):
        attempt[0] += 1
        return FastAPI(lifespan=failing if attempt[0] == 1 else healthy)
    async def check():
        async with AsyncExitStack() as stack:
            manager = WorkspaceRuntimes(AccessSettings(root=tmp_path), factory, stack)
            with pytest.raises(RuntimeError):
                await manager.get('a'*32)
            assert manager.apps == {}
            first, second = await asyncio.gather(manager.get('a'*32), manager.get('a'*32))
            assert first is second and attempt[0] == 2
        assert calls == ['failed-closed', 'healthy-closed']
    asyncio.run(check())


def test_runtime_capacity_and_unsafe_mcp_fail_closed(team):
    app, client, _ = team
    login(client, 'alice')
    for _ in range(3):
        ident = workspace(client, 'loaded')
        assert client.get(f'/w/{ident}/api/documents').status_code == 200
    extra = workspace(client, 'extra')
    assert client.get(f'/w/{extra}/api/documents').status_code == 503
    with pytest.raises(ValueError, match='MCP'):
        create_app(llm_settings=LLMSettings(), access_settings=app.state.access_settings,
                   mcp_settings=MCPSettings(server_enabled=True))
    assert client.get('/w/../../api/documents').status_code in {401,404}


def test_secure_cookie_default_and_local_http_contract(tmp_path):
    config = AccessSettings(mode='team', root=tmp_path / 'team', origins=('https://testserver',))
    with TestClient(create_app(llm_settings=LLMSettings(), access_settings=config), base_url='https://testserver') as client:
        client.app.state.access.create_user('alice', 'Alice', PASSWORD)
        response = client.post('/api/access/login', json={'username': 'alice', 'password': PASSWORD}, headers=WRITE)
        assert response.status_code == 200
        assert '__Host-deepsearch_session=' in response.headers['set-cookie']
        assert '; Secure' in response.headers['set-cookie']
        assert client.get('/api/access/me').status_code == 200
    with TestClient(create_app(tmp_path / 'local' / 'index.json', llm_settings=LLMSettings())) as client:
        assert client.get('/api/access/status').json() == {'mode':'local'}
        assert client.get('/api/sessions').status_code == 200


def test_real_pdf_attachment_wiki_and_resume_boundaries_survive_restart(tmp_path, monkeypatch):
    import io
    import time
    from reportlab.pdfgen import canvas
    from PIL import Image
    from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
    from deepsearch_agent.workspace.llm_wiki import WikiPage, WikiSource
    monkeypatch.setattr(RemoteEmbeddingClient, 'embed', lambda self, texts: [[1., 0.] for _ in texts])
    config = AccessSettings(mode='team', root=tmp_path / 'team', cookie_secure=False, origins=('http://testserver',))
    options = dict(llm_settings=LLMSettings(), access_settings=config,
                   embedding_settings=EmbeddingSettings(backend='openai_compatible', api_key='fixture',
                       base_url='https://fixture.invalid/v1', dimensions=2))
    app = create_app(**options)
    old = tmp_path / 'legacy' / 'untouched.json'
    old.parent.mkdir()
    old.write_text('legacy user data', encoding='utf-8')
    old_hash = hashlib.sha256(old.read_bytes()).hexdigest()
    pdf = io.BytesIO()
    writer = canvas.Canvas(pdf)
    writer.drawString(50, 700, 'Private fixture measurement: twelve.')
    writer.save()
    image = io.BytesIO()
    Image.new('RGB', (16,16), 'green').save(image, format='PNG')
    with TestClient(app) as client:
        app.state.access.create_user('alice', 'Alice', PASSWORD)
        app.state.access.create_user('bob', 'Bob', PASSWORD)
        login(client, 'alice')
        alpha = workspace(client, 'Alpha')
        prefix = f'/w/{alpha}/api'
        result = client.post(prefix + '/documents/upload?filename=fixture.pdf', content=pdf.getvalue(),
                             headers={**WRITE, 'Content-Type':'application/pdf', 'Origin':'http://testserver'})
        assert result.status_code == 202, result.text
        document = result.json()['id']
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = client.get(prefix + f'/documents/{document}').json()
            if result['status'] not in {'queued','parsing','embedding'}:
                break
            time.sleep(.05)
        assert result['status'] == 'ready', result
        assert client.get(prefix + f'/documents/{document}/file').content == pdf.getvalue()
        assert client.get(prefix + f'/documents/{document}/pages/1/image').content.startswith(b'\x89PNG')
        state = app.state.workspace_runtimes.apps[alpha].state
        session = client.post(prefix+'/sessions', json={'title':'private fixture'}, headers=WRITE).json()['id']
        # Exercise real attachment storage/transport without calling a visual model.
        state.vision_enabled = True
        result = client.post(prefix+f'/sessions/{session}/attachments?filename=fixture.png', content=image.getvalue(),
                             headers={**WRITE, 'Content-Type':'image/png'})
        assert result.status_code == 201, result.text
        attached = result.json()['id']
        assert client.get(prefix+f'/sessions/{session}/attachments/{attached}/image').status_code == 200
        chunk = state.kb.chunks[0]
        wiki = state.wiki.save(WikiPage(id='wiki_'+'a'*32, title='Private fixture', markdown='Twelve [1]',
            sources=[WikiSource(chunk_id=chunk.chunk_id, source_path=chunk.source_path, page_number=1)], updated_at='2026-10-18T00:00:00Z'))
        assert client.get(prefix+'/wiki/'+wiki.id).status_code == 200
        run_id = state.research_runs.create(session, {'question':'fixture'})
        state.research_runs.update(run_id, 'interrupted')
        login(client, 'bob')
        beta = workspace(client, 'Beta')
        for resource in (f'/documents/{document}/file', f'/documents/{document}/pages/1/image',
                         f'/sessions/{session}/attachments/{attached}/image', '/wiki/'+wiki.id,
                         f'/sessions/{session}/memory', f'/sessions/{session}/runs'):
            assert client.get(prefix+resource).status_code == 404
            assert client.get(f'/w/{beta}/api'+resource).status_code == 404
        assert client.post(f'/w/{beta}/api/sessions/{session}/runs/{run_id}/resume/stream', json={}, headers=WRITE).status_code == 404
        login(client, 'alice')
        cookie = dict(client.cookies)
    with TestClient(create_app(**options)) as client:
        client.cookies.update(cookie)
        assert client.get(prefix + f'/documents/{document}/file').content == pdf.getvalue()
        assert client.get(prefix + f'/sessions/{session}').status_code == 200
        assert client.get(prefix+'/wiki/'+wiki.id).status_code == 200
        assert client.get(prefix+f'/sessions/{session}/attachments/{attached}/image').status_code == 200
        assert client.get(prefix+f'/sessions/{session}/runs').json()[0]['status'] == 'interrupted'
    assert hashlib.sha256(old.read_bytes()).hexdigest() == old_hash
