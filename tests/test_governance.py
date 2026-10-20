from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier, Event

import httpx
import pytest

from deepsearch_agent.adapters.governance_config import GovernanceSettings
from deepsearch_agent.adapters.persistence.governance import GovernanceStore
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
from deepsearch_agent.application.governance import Governance
from deepsearch_agent.core.governance import GovernanceDenied, UsageScope, usage_context
from deepsearch_agent.core.models import LLMResponse, ModelServiceError
from deepsearch_agent.core.cancellation import ResearchCancelled
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked


def governor(tmp_path, **kwargs):
    return Governance(GovernanceStore(tmp_path/'governance.sqlite3'),GovernanceSettings(**kwargs))


def rows(g):
    with g.store.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM cost_reservations ORDER BY created_at')]


@pytest.mark.parametrize('same_user',[True,False])
def test_atomic_dual_scope_reservations_cannot_overspend(tmp_path,same_user):
    g = governor(tmp_path,user_daily_units=10000,workspace_daily_units=10000)
    barrier = Barrier(2)
    def reserve(n):
        scope = UsageScope(user='alice' if same_user else str(n),workspace=str(n) if same_user else 'shared')
        barrier.wait()
        try:
            return g.store.reserve(scope,'llm','synthetic',8000,1,g.settings)
        except GovernanceDenied:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve,range(2)))
    assert sum(r is not None for r in results)==1
    assert len(rows(g))==1
    assert rows(g)[0]['reserved']==8000


def test_unknown_survives_restart_and_midnight_until_audited_settlement(tmp_path):
    now = [datetime(2026,10,20,23,59,tzinfo=timezone.utc).timestamp()]
    store = GovernanceStore(tmp_path/'g.sqlite3',clock=lambda:now[0])
    settings = GovernanceSettings(user_daily_units=10000)
    scope = UsageScope(user='alice',workspace='alpha')
    ident = store.reserve(scope,'llm','synthetic',8000,1,settings)
    store.dispatch(ident)
    now[0] += 120
    recovered = Governance(GovernanceStore(store.path,clock=lambda:now[0]),settings)
    assert recovered.store.usage('user','alice',10000)['used_or_held']==8000
    assert recovered.store.unsettled()[0]['status']=='unknown'
    with pytest.raises(GovernanceDenied):
        recovered.store.reserve(scope,'llm','synthetic',3000,1,settings)
    recovered.store.settle_unknown(ident,2000,'admin','verified receipt')
    # Settled expenses belong to their original accounting day.
    assert recovered.store.usage('user','alice',10000)['used_or_held']==0
    with recovered.store.connect() as db:
        assert db.execute('SELECT actor,proof_reference FROM governance_audit').fetchone()['actor']=='admin'
    with pytest.raises(GovernanceDenied,match='conflict'):
        recovered.store.settle_unknown(ident,0,'admin','must not edit settled cost')


def test_reserved_before_dispatch_releases_on_startup(tmp_path):
    g = governor(tmp_path)
    g.store.reserve(UsageScope(),'llm','synthetic',8000,1,g.settings)
    g.store.recover()
    assert rows(g)[0]['status']=='released'
    assert g.store.usage('user','local')['used_or_held']==0


@pytest.mark.parametrize('result,expected',[(LLMResponse('ok',total_tokens=20),'settled'),
    (LLMResponse('ok',prompt_tokens=10,completion_tokens=5),'settled'),(LLMResponse('ok'),'unknown')])
def test_provider_reported_usage_and_missing_usage_are_separate(tmp_path,result,expected):
    g = governor(tmp_path)
    with usage_context(g,user='alice',workspace='alpha',run_id='run',operation_id='op'):
        assert g.invoke('llm','synthetic',100,lambda:result)==result
    row = rows(g)[0]
    assert row['status']==expected and row['run_id']=='run' and row['operation_id']=='op'
    if expected=='unknown':
        assert row['actual'] is None and g.store.usage('user','alice')['used_or_held']==100
    assert g.snapshot()['resources']['provider']['active']==0


@pytest.mark.parametrize('error,expected',[(TimeoutError(),'unknown'),(ResearchCancelled(),'unknown'),
    (ValueError('invalid input'),'released'),(ModelServiceError('truncated',usage={'total_tokens':40}),'settled')])
def test_provider_errors_keep_unknown_or_proven_usage(tmp_path,error,expected):
    g = governor(tmp_path)
    with usage_context(g),pytest.raises(type(error)):
        g.invoke('llm','synthetic',100,lambda:(_ for _ in ()).throw(error))
    assert rows(g)[0]['status']==expected
    assert g.snapshot()['resources']['provider']['active']==0


def test_global_provider_limit_shared_across_workspaces_and_releases_on_error(tmp_path):
    g = governor(tmp_path,global_provider=2,workspace_provider=1,user_provider=1)
    held, release = Barrier(3),Event()
    def hold(user,workspace):
        with usage_context(g,user=user,workspace=workspace),g.resource('provider'):
            held.wait(timeout=3)
            release.wait(timeout=3)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(hold,'alice','alpha')
        second=pool.submit(hold,'bob','beta')
        held.wait(timeout=3)
        with usage_context(g,user='carol',workspace='gamma'),pytest.raises(GovernanceDenied,match='global_provider'):
            with g.resource('provider'):
                pytest.fail('capacity exceeded')
        assert g.snapshot()['resources']['provider']['peak']==2
        release.set()
        first.result(); second.result()
    with usage_context(g),pytest.raises(RuntimeError):
        with g.resource('provider'):
            raise RuntimeError('fixture')
    assert g.snapshot()['resources']['provider']['active']==0


def test_quota_denial_never_calls_real_http_boundary(tmp_path):
    g = governor(tmp_path,user_daily_units=0)
    requests=[]
    client=OpenAICompatibleClient(LLMSettings(backend='openai_compatible',api_key='test',base_url='https://example.test/v1',model='synthetic'),
                                 transport=httpx.MockTransport(lambda request: requests.append(request)))
    try:
        with usage_context(g),pytest.raises(GovernanceDenied,match='user_quota_exceeded'):
            client.generate('question')
        assert requests==[] and rows(g)==[]
    finally:
        client.close()


def test_actual_gateway_and_embedding_batch_usage_are_metered(tmp_path):
    g=governor(tmp_path)
    def handler(request):
        return httpx.Response(200,json={'choices':[{'message':{'content':'answer'},'finish_reason':'stop'}],
            'usage':{'total_tokens':12}})
    client=OpenAICompatibleClient(LLMSettings(backend='openai_compatible',api_key='test',base_url='https://example.test/v1',model='synthetic'),transport=httpx.MockTransport(handler))
    embed=RemoteEmbeddingClient(EmbeddingSettings(backend='openai_compatible',api_key='test',base_url='https://example.test/v1',dimensions=2,batch_size=1),
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':[{'index':0,'embedding':[1.,0.]}],'usage':{'total_tokens':3}})))
    try:
        with usage_context(g,user='alice',workspace='alpha'):
            assert client.complete('question')=='answer'
            assert len(embed.embed(['one','two']))==2
        assert [r['actual'] for r in rows(g)]==[12,6]
    finally:
        client.close()


def test_alert_pending_firing_resolved_is_persisted(tmp_path):
    now=[1000.]
    store=GovernanceStore(tmp_path/'g.sqlite3',clock=lambda:now[0])
    g=Governance(store,GovernanceSettings(unknown_alert_seconds=1,alert_for_seconds=10),clock=lambda:now[0])
    with usage_context(g):
        g.invoke('llm','synthetic',100,lambda:LLMResponse('unknown'))
    now[0]+=2
    def alert():
        return next(a for a in g.snapshot()['alerts'] if a['name']=='unknown_cost_stale')['state']
    assert alert()=='pending'
    now[0]+=11
    assert alert()=='firing'
    store.settle_unknown(rows(g)[0]['id'],10,'admin','receipt')
    assert alert()=='resolved'
    with store.connect() as db:
        assert db.execute("SELECT state FROM governance_alerts WHERE name='unknown_cost_stale'").fetchone()['state']=='resolved'


def test_accounting_failure_blocks_invocation_and_sanitizes_error(tmp_path,monkeypatch):
    g=governor(tmp_path)
    def unavailable(*args):
        raise OSError('private SQL / credentials')
    monkeypatch.setattr(g.store,'reserve',unavailable)
    with usage_context(g),pytest.raises(GovernanceDenied,match='quota_store_unavailable') as raised:
        g.invoke('llm','synthetic',100,lambda:pytest.fail('must not invoke'))
    assert 'private' not in str(raised.value)


def test_settlement_failure_after_effect_is_not_classified_as_not_executed(tmp_path,monkeypatch):
    g=governor(tmp_path)
    monkeypatch.setattr(g.store,'finish',lambda *a,**k:(_ for _ in ()).throw(OSError('fixture')))
    with usage_context(g),pytest.raises(ToolExecutionBlocked,match='quota_settlement_unavailable'):
        g.invoke('llm','synthetic',100,lambda:LLMResponse('effect complete',total_tokens=10))
    assert rows(g)[0]['status']=='running'
    g.store.recover()
    assert rows(g)[0]['status']=='unknown'


def test_metrics_export_has_histogram_buckets_and_no_identity_labels(tmp_path):
    g=governor(tmp_path)
    with usage_context(g,user='secret-user-id',workspace='secret-workspace-id'):
        g.invoke('llm','synthetic',100,lambda:LLMResponse('private answer',total_tokens=10))
    text=g.render()
    assert 'le="+Inf"' in text and '# TYPE deepsearch_provider_duration_seconds histogram' in text
    assert all(value not in text for value in ('secret-user-id','secret-workspace-id','private answer'))


def test_tool_result_replay_never_adds_a_second_provider_reservation(tmp_path):
    from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger
    from deepsearch_agent.application.tool_execution import ToolExecutor
    from deepsearch_agent.core.tool_execution import ToolKind
    g=governor(tmp_path)
    ledger=SQLiteToolLedger(tmp_path/'tools.sqlite3'); ledger.begin_run('run')
    ex=ToolExecutor(ledger,'run')
    def call(_):
        return {'text':g.invoke('llm','synthetic',100,lambda:LLMResponse('completed',total_tokens=10)).text}
    with usage_context(g,user='alice',workspace='alpha',run_id='run'):
        first=ex.execute('step','paid',ToolKind.PAID,{},call)
        replay=ex.execute('step','paid',ToolKind.PAID,{},lambda _:pytest.fail('no repeat'))
    assert first.value==replay.value and replay.replayed
    assert len(rows(g))==1 and rows(g)[0]['operation_id']==first.operation_id


def test_embedding_cache_hit_does_not_reserve_quota_again(tmp_path):
    from deepsearch_agent.adapters.infrastructure import CachedEmbeddingClient, RedisBackend, InfrastructureSettings
    from test_infrastructure import FakeRedis
    g=governor(tmp_path)
    delegate=RemoteEmbeddingClient(EmbeddingSettings(backend='openai_compatible',api_key='test',base_url='https://example.test/v1',dimensions=2),
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':[{'index':0,'embedding':[1.,0.]}],'usage':{'total_tokens':5}})))
    backend=RedisBackend(FakeRedis(),InfrastructureSettings())
    cache=CachedEmbeddingClient(delegate,backend,'model',2)
    try:
        with usage_context(g):
            cache.embed(['text']); cache.embed(['text'])
        assert len(rows(g))==1 and rows(g)[0]['actual']==5
        assert 'deepsearch_embedding_cache_total{outcome="hit"} 1' in g.render()
    finally:
        backend.close()


def test_unknown_tool_backlog_in_unopened_workspace_is_read_only(tmp_path):
    import hashlib
    from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger,read_backlog
    from deepsearch_agent.core.tool_execution import ToolKind
    path=tmp_path/'workspaces'/'inactive'/'tool_executions.sqlite3'
    ledger=SQLiteToolLedger(path); ledger.begin_run('run')
    ident=ledger.prepare('run','step','write',ToolKind.EFFECTFUL,{},'')
    owner=ledger.claim(ident); ledger.dispatch(ident,owner); ledger.uncertain(ident,owner)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_backlog(tmp_path/'workspaces',workspaces=True)['unknown_count']==1
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_metrics_failure_does_not_replace_successful_paid_result(tmp_path,monkeypatch):
    g=governor(tmp_path)
    monkeypatch.setattr(g.metrics,'inc',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('metrics unavailable')))
    with usage_context(g):
        assert g.invoke('llm','synthetic',100,lambda:LLMResponse('done',total_tokens=10)).text=='done'
    assert rows(g)[0]['status']=='settled'


def test_quota_denied_ingestion_is_failed_before_execution_not_unknown(tmp_path):
    from deepsearch_agent.workspace.ingestion import IngestionJournal
    g=governor(tmp_path,user_daily_units=0)
    journal=IngestionJournal(tmp_path/'ingestion',{'document':'synthetic'})
    with usage_context(g),pytest.raises(GovernanceDenied):
        journal.run('embedding',lambda:g.invoke('embedding','synthetic',10,lambda:pytest.fail('not contacted')),paid=True)
    assert journal.read('embedding')['status']=='failed' and journal.pending_unknown()==[]


def test_cancelled_queued_ingestion_releases_admission_lease(tmp_path):
    from deepsearch_agent.adapters.governed_calls import GovernedExecutor
    g=governor(tmp_path)
    underlying=ThreadPoolExecutor(1)
    release=Event(); started=Event()
    def hold():
        started.set(); release.wait(timeout=3)
    first=underlying.submit(hold); assert started.wait(timeout=2)
    wrapper=GovernedExecutor(underlying)
    try:
        with usage_context(g):
            pending=wrapper.submit(lambda:pytest.fail('cancelled work must not execute'))
        assert g.snapshot()['resources']['ingestion']['active']==1
        assert pending.cancel()
        assert g.snapshot()['resources']['ingestion']['active']==0
    finally:
        release.set(); first.result(); wrapper.shutdown(wait=True)


def test_latency_alert_requires_observations_and_preserves_unknown(tmp_path):
    now=[1000.]
    store=GovernanceStore(tmp_path/'g.sqlite3',clock=lambda:now[0])
    g=Governance(store,GovernanceSettings(failure_min_samples=1,slow_provider_seconds=1,alert_for_seconds=0),clock=lambda:now[0])
    assert g.snapshot()['latency']['provider']['p95_seconds'] is None
    def slow():
        now[0]+=2
        return LLMResponse('done',total_tokens=10)
    with usage_context(g):
        g.invoke('llm','synthetic',100,slow)
    snapshot=g.snapshot()
    assert snapshot['latency']['provider']=={'p95_seconds':2,'samples':1}
    assert next(a for a in snapshot['alerts'] if a['name']=='provider_latency_high')['state']=='firing'


def test_redis_alert_resolves_after_backend_recovers(tmp_path):
    from types import SimpleNamespace
    g=governor(tmp_path,alert_for_seconds=0)
    class Ledger:
        def summary(self):
            return {'unknown_count':0,'oldest_unknown_seconds':0}
    backend=SimpleNamespace(status='unavailable')
    g.register_workspace('synthetic',Ledger(),backend)
    def state():
        return next(a for a in g.snapshot()['alerts'] if a['name']=='redis_degraded')['state']
    assert state()=='firing'
    backend.status='available'
    assert state()=='resolved'


def test_global_ingestion_rejection_does_not_leave_queued_document(tmp_path):
    from test_ingestion_recovery import library,IDENT
    g=governor(tmp_path,global_ingestion=1)
    docs=library(tmp_path,[])
    try:
        with usage_context(g),g.resource('ingestion'),pytest.raises(GovernanceDenied):
            docs.retry(IDENT)
        assert docs.view(IDENT)['status']=='failed' and IDENT not in docs.inflight
        assert docs.journal(IDENT).pending_unknown()==[]
    finally:
        docs.close()


def test_empty_embedding_batch_requires_no_quota_or_reservation(tmp_path):
    g=governor(tmp_path,user_daily_units=0)
    delegate=RemoteEmbeddingClient(EmbeddingSettings(backend='openai_compatible',api_key='test',base_url='https://example.test/v1',dimensions=2),
        transport=httpx.MockTransport(lambda request:pytest.fail('empty batch must not send')))
    with usage_context(g):
        assert delegate.embed([])==[]
    assert rows(g)==[]
