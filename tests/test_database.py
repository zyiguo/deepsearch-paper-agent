"""Optional real PostgreSQL contracts, isolated in a new schema for every test."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from deepsearch_agent.adapters.database_config import DatabaseSettings
from deepsearch_agent.adapters.access_config import AccessSettings
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.core.answers import Answer
from deepsearch_agent.core.errors import ApplicationError


@pytest.mark.parametrize('options',[
    {'backend':'other'}, {'backend':'postgresql'}, {'backend':'sqlite','url':'postgresql://u:secret@host/db'},
    {'schema':'public;DROP SCHEMA public'}, {'schema':'pg_catalog'}, {'pool_size':0},
    {'backend':'postgresql','url':'postgresql://u:secret@host/db?options=-c%20role=postgres'},
])
def test_invalid_database_settings_fail_without_echoing_secrets(options):
    with pytest.raises(ValueError) as caught:
        DatabaseSettings(**options)
    assert 'secret' not in str(caught.value)


def test_settings_redaction_file_precedence_and_sqlite_default(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_BACKEND','postgresql')
    monkeypatch.setenv('DATABASE_URL','postgresql://u:secret@localhost/db')
    path = tmp_path/'database.env'
    path.write_text('DATABASE_BACKEND=sqlite\nDATABASE_URL=\n')
    assert DatabaseSettings.load(path).backend=='sqlite'
    assert 'secret' not in repr(DatabaseSettings(backend='postgresql',url='postgresql://u:secret@localhost/db?sslmode=require'))


@pytest.fixture
def database():
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set TEST_DATABASE_URL for real PostgreSQL tests')
    psycopg = pytest.importorskip('psycopg')
    from psycopg import sql
    from deepsearch_agent.adapters.persistence.postgres import PostgresDatabase
    from deepsearch_agent.adapters.persistence.postgres_schema import upgrade,validate_schema
    schema = 'test_'+uuid4().hex
    settings = DatabaseSettings(backend='postgresql',url=url,schema=schema,pool_size=4,timeout=2)
    db = PostgresDatabase(settings,check_schema=False)
    try:
        assert upgrade(db) is True
        validate_schema(db)
        yield db
    finally:
        db.close()
        assert schema.startswith('test_') and len(schema)==37
        # Only this generated test schema can be removed, never a database or another deployment.
        with psycopg.connect(url) as connection:
            connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


def stores(database,tmp_path,workspace='local'):
    from deepsearch_agent.adapters.persistence.postgres_conversations import PostgresConversationStore
    from deepsearch_agent.adapters.persistence.postgres_runs import PostgresResearchRunStore
    conversations = PostgresConversationStore(database,workspace,tmp_path/workspace/'conversations.sqlite3')
    runs = PostgresResearchRunStore(database,workspace,tmp_path/workspace/'research_runs.sqlite3')
    return conversations,runs


def test_schema_idempotency_checksum_and_forced_rls(database):
    from deepsearch_agent.adapters.persistence.postgres import PostgresDatabase
    from deepsearch_agent.adapters.persistence.postgres_schema import upgrade,SCOPED,CHECKSUM
    assert upgrade(database) is False
    with database.transaction() as db:
        flags = db.execute('SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity FROM pg_class c '
            'JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=ANY(%s)',
            (database.settings.schema,list(SCOPED))).fetchall()
        assert len(flags)==len(SCOPED) and all(r['relrowsecurity'] and r['relforcerowsecurity'] for r in flags)
        db.execute("UPDATE schema_migrations SET checksum='invalid'")
    with pytest.raises(RuntimeError,match='mismatch'):
        PostgresDatabase(database.settings)
    with database.transaction() as db:
        db.execute('UPDATE schema_migrations SET checksum=%s',(CHECKSUM,))


def test_rls_pool_reuse_scope_and_foreign_keys(database,tmp_path):
    a,_ = stores(database,tmp_path,'a'*32)
    b,_ = stores(database,tmp_path,'b'*32)
    sid = a.create('Private A')['id']
    assert b.list_sessions()==[]
    with pytest.raises(KeyError):
        b.get(sid)
    with database.transaction('a'*32) as db:
        assert len(db.execute('SELECT * FROM sessions').fetchall())==1  # Intentionally omit WHERE to exercise RLS.
    with database.transaction() as db:
        assert db.execute('SELECT * FROM sessions').fetchall()==[]
    with pytest.raises(ApplicationError):
        with database.transaction('b'*32) as db:
            db.execute("INSERT INTO sessions VALUES(%s,%s,'forbidden','now','now')",('a'*32,str(uuid4())))
    with pytest.raises(ApplicationError):
        with database.transaction('b'*32) as db:
            db.execute("INSERT INTO turns(workspace_id,session_id,question,answer_json,created_at) VALUES(%s,%s,'x','{}','now')",('b'*32,sid))
    with database.transaction() as db:
        assert db.execute("SELECT current_setting('deepsearch.workspace_id',true) AS scope").fetchone()['scope']==''
    assert len(a.list_sessions())==1 and not b.list_sessions()


def test_concurrent_idempotent_turns_memory_cas_and_delete(database,tmp_path):
    from deepsearch_agent.workspace.memory import MemoryManager
    store,runs = stores(database,tmp_path)
    sid = store.create('fixture')['id']
    run_id = runs.create(sid,{'question':'fixture'})
    def append(_):
        return store.append_turn(sid,'fixture',Answer(text='Supported fixture',sources=[]),run_id=run_id)['id']
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(append,range(20)))
    assert len(set(ids))==1 and len(store.get(sid)['turns'])==1
    manager = MemoryManager(store)
    initial = manager.get(sid)
    with ThreadPoolExecutor(max_workers=8) as pool:
        saved = list(pool.map(lambda _:manager.save(sid,initial,initial.revision),range(12)))
    assert sum(saved)==1
    runs.update(run_id,'completed',turn_id=ids[0])
    assert runs.get(run_id,sid)['turn_id']==ids[0]
    store.delete(sid)
    assert runs.list(sid)==[]
    with database.transaction('local') as db:
        assert db.execute('SELECT * FROM session_memories').fetchall()==[]


def test_run_recovery_is_scoped_and_claim_is_atomic(database,tmp_path):
    from deepsearch_agent.adapters.persistence.postgres_runs import PostgresResearchRunStore
    a,aruns = stores(database,tmp_path,'a'*32)
    sid = a.create('A')['id']
    rid = aruns.create(sid,{'question':'fixture'})
    aruns.update(rid,'running')
    stores(database,tmp_path,'b'*32)
    assert aruns.get(rid,sid)['status']=='running'
    restored = PostgresResearchRunStore(database,'a'*32,tmp_path/'runs.placeholder')
    assert restored.get(rid,sid)['status']=='interrupted'
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _:restored.claim_resume(rid,sid),range(20)))==1


def test_pg_submission_claim_and_late_progress_are_atomic(database,tmp_path):
    store,runs = stores(database,tmp_path)
    sid = store.create('atomic submission')['id']
    rid = 'f' * 32
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _:runs.create(sid,{'question':'fixture'},run_id=rid),range(20)))
    assert set(ids)=={rid} and len(runs.list(sid))==1
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _:runs.update(rid,'running',expected={'queued'},execution={'new_attempt':True}),range(20)))
    assert sum(claims)==1
    assert runs.get(rid,sid)['request']['_execution']['attempts']==1
    assert runs.update(rid,'cancel_requested',expected={'running'})
    assert not runs.update(rid,'running',expected={'running'})
    assert runs.update(rid,'cancelled',expected={'cancel_requested'},execution={'error_code':'cancelled'})
    assert not runs.update(rid,'running',expected={'queued'})
    turn=store.append_turn(sid,'fixture',Answer('committed before cancellation',[]),run_id=rid)
    assert store.find_turn(rid,sid)==turn
    assert store.find_turn(rid,'another-session') is None


def test_identity_concurrency_password_change_and_revocation(database):
    from deepsearch_agent.adapters.persistence.postgres_access import PostgresAccessStore
    from deepsearch_agent.adapters.passwords import hash_password
    store = PostgresAccessStore(database)
    encoded = hash_password('synthetic-test-password')
    user = store.create_user('alice','Alice',encoded)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _:store.login_allowed('alice'),range(20)))==5
    token = store.new_session(user,300,expected_hash=encoded)
    assert store.authenticate(token)['id']==user
    store.reset_password('alice',hash_password('changed-test-password'))
    assert store.authenticate(token) is None
    with pytest.raises(ValueError):
        store.new_session(user,300,expected_hash=encoded)
    other = store.new_session(user,300)
    store.disable_user(user,None)
    assert store.authenticate(other) is None
    store.enable_user(user,None)
    assert store.authenticate(other) is None


def test_sqlite_import_preserves_ids_memory_and_source_and_refuses_overwrite(database,tmp_path):
    from deepsearch_agent.workspace.conversations import ConversationStore
    from deepsearch_agent.workspace.memory import MemoryManager
    from deepsearch_agent.adapters.persistence.research_runs import ResearchRunStore
    from deepsearch_agent.adapters.persistence.database_import import import_workspace,checksum
    source = ConversationStore(tmp_path/'legacy.sqlite3')
    sid = source.create('Old fixture')['id']
    turn = source.append_turn(sid,'question',Answer(text='fixture answer',sources=[]),run_id='legacy-run')
    MemoryManager(source).edit(sid,research_goal='Old goal',language='zh',answer_style='short')
    runs = ResearchRunStore(tmp_path/'legacy-runs.sqlite3')
    rid = runs.create(sid,{'question':'question'})
    runs.update(rid,'completed',turn_id=turn['id'])
    original = checksum(source.path),checksum(runs.path)
    report = import_workspace(database,source.path,'local',runs_path=runs.path)
    assert report['applied'] is False
    with database.transaction('local') as db:
        assert not db.execute('SELECT * FROM sessions').fetchall()
    report = import_workspace(database,source.path,'local',runs_path=runs.path,apply=True)
    assert report['counts']=={'sessions':1,'turns':1,'memories':1,'runs':1}
    restored,_ = stores(database,tmp_path)
    assert restored.get(sid)==source.get(sid)
    assert MemoryManager(restored).get(sid).research_goal=='Old goal'
    new = restored.append_turn(sid,'next',Answer(text='next',sources=[]))
    assert new['id']>turn['id']
    with pytest.raises(ValueError,match='not empty'):
        import_workspace(database,source.path,'local',apply=True)
    assert original==(checksum(source.path),checksum(runs.path))


def test_identity_import_requires_fresh_login_and_preserves_members(database,tmp_path):
    from deepsearch_agent.adapters.persistence.access import AccessStore
    from deepsearch_agent.adapters.persistence.postgres_access import PostgresAccessStore
    from deepsearch_agent.adapters.persistence.database_import import import_identity,checksum
    from deepsearch_agent.adapters.passwords import hash_password
    source = AccessStore(tmp_path/'identity.sqlite3')
    uid = source.create_user('alice','Alice',hash_password('synthetic-test-password'),admin=True)
    ws = source.create_workspace(uid,'Old workspace')
    token = source.new_session(uid,300)
    original = checksum(source.path)
    report = import_identity(database,source.path,apply=True)
    target = PostgresAccessStore(database)
    assert report['login_sessions_imported']==0 and target.authenticate(token) is None
    assert target.role(uid,ws)=='owner'
    assert target.user_by_name('alice')['password_hash']==source.user_by_name('alice')['password_hash']
    assert checksum(source.path)==original
    with pytest.raises(ValueError,match='not empty'):
        import_identity(database,source.path,apply=True)


def test_import_is_atomic_on_bad_source_and_reports_are_never_overwritten(database,tmp_path,monkeypatch):
    from deepsearch_agent.workspace.conversations import ConversationStore
    from deepsearch_agent.adapters.persistence.database_import import import_workspace
    from deepsearch_agent.application.database_cli import main
    source = ConversationStore(tmp_path/'bad.sqlite3')
    sid = source.create('fixture')['id']
    source.append_turn(sid,'question',Answer(text='fixture',sources=[]))
    with source._db() as db:
        db.execute("UPDATE turns SET answer_json='not-json'")
    with pytest.raises(ValueError):
        import_workspace(database,source.path,'local',apply=True)
    with database.transaction('local') as db:
        assert not db.execute('SELECT * FROM sessions').fetchall()
    # Inject an interruption after real scope/session INSERTs, before turn INSERTs.
    with source._db() as db:
        db.execute('UPDATE turns SET answer_json=?',(json.dumps({'text':'fixture','sources':[]}),))
    from contextlib import contextmanager
    original_transaction = database.transaction
    @contextmanager
    def interrupted_transaction(scope=None):
        with original_transaction(scope) as connection:
            class Interrupted:
                def execute(self,query,*args,**kwargs):
                    if isinstance(query,str) and query.startswith('INSERT INTO turns('):
                        raise ApplicationError(503,'fixture interruption after session insert')
                    return connection.execute(query,*args,**kwargs)
            yield Interrupted()
    with monkeypatch.context() as patch:
        patch.setattr(database,'transaction',interrupted_transaction)
        with pytest.raises(ApplicationError):
            import_workspace(database,source.path,'local',apply=True)
    with database.transaction('local') as db:
        assert not db.execute('SELECT * FROM sessions').fetchall()
        assert not db.execute('SELECT * FROM business_scopes').fetchall()
    output = tmp_path/'review.json'
    output.write_text('human review')
    assert main(['import-workspace','--source',str(source.path),'--workspace-id','local','--apply','--output',str(output)])==1
    assert output.read_text()=='human review'


def test_team_api_postgres_runtime_pool_restart_and_no_sqlite_shadow(database,tmp_path):
    from deepsearch_agent.application.bootstrap import create_app
    config = AccessSettings(mode='team',root=tmp_path/'team',cookie_secure=False,origins=('http://testserver',))
    options = dict(llm_settings=LLMSettings(),access_settings=config,database_settings=database.settings)
    app = create_app(**options)
    headers = {'X-DeepSearch-Request':'1'}
    with TestClient(app) as client:
        service = app.state.access
        service.create_user('alice','Alice','synthetic-test-password',admin=True)
        service.create_user('bob','Bob','synthetic-test-password')
        assert client.post('/api/access/login',json={'username':'alice','password':'synthetic-test-password'},headers=headers).status_code==200
        ws = client.post('/api/workspaces',json={'name':'Alpha'},headers=headers).json()['id']
        prefix = f'/w/{ws}/api'
        sid = client.post(prefix+'/sessions',json={'title':'Persisted'},headers=headers).json()['id']
        assert client.post(prefix+'/demo/ingest',headers=headers).status_code==200
        assert client.post(prefix+f'/sessions/{sid}/turns',json={'question':'模型怎么训练？'},headers=headers).status_code==201
        assert app.state.workspace_runtimes.apps[ws].state.business_database is app.state.business_database
        assert client.put(f'/api/workspaces/{ws}/members',json={'username':'bob','role':'viewer'},headers=headers).status_code==200
        cookie = dict(client.cookies)
        client.cookies.clear()
        assert client.post('/api/access/login',json={'username':'bob','password':'synthetic-test-password'},headers=headers).status_code==200
        assert client.get(prefix+f'/sessions/{sid}').status_code==200
        assert client.post(prefix+'/sessions',json={'title':'denied'},headers=headers).status_code==403
    assert app.state.business_database.closed
    with TestClient(create_app(**options)) as client:
        client.cookies.update(cookie)
        assert client.get(prefix+f'/sessions/{sid}').json()['turns']
        assert client.get(prefix+'/health/ready').json()['dependencies']['database']=='available'
    assert not (config.root/'identity.sqlite3').exists()
    assert not list(config.root.glob('workspaces/*/conversations.sqlite3'))


def test_local_api_postgres_and_fail_closed_database(database,tmp_path):
    from deepsearch_agent.application.bootstrap import create_app
    with TestClient(create_app(tmp_path/'local/index.json',llm_settings=LLMSettings(),database_settings=database.settings)) as client:
        sid = client.post('/api/sessions',json={'title':'local postgres'}).json()['id']
        assert client.get('/api/sessions/'+sid).status_code==200
        client.app.state.business_database.close()
        response = client.post('/api/sessions',json={'title':'must not fall back'})
        assert response.status_code==503 and 'postgresql://' not in response.text
        assert client.get('/api/health/ready').status_code==503
    assert not (tmp_path/'local/conversations.sqlite3').exists()
