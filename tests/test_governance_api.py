from dataclasses import replace
import json

import httpx
from fastapi.testclient import TestClient

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_agent.application.bootstrap import create_app
from deepsearch_agent.core.answers import Answer
from deepsearch_agent.core.governance import usage_context
from deepsearch_agent.core.models import LLMResponse
from test_team_access import team,login,workspace,WRITE


QUESTION='模型怎么训练？'


def provider(handler):
    return OpenAICompatibleClient(LLMSettings(backend='openai_compatible',api_key='synthetic',
        base_url='https://example.test/v1',model='synthetic',max_output_tokens=64,context_window=1024),
        transport=httpx.MockTransport(handler))


def success(request):
    return httpx.Response(200,json={'choices':[{'message':{'content':'answer'},'finish_reason':'stop'}],
                                  'usage':{'total_tokens':12}})


def test_team_deployment_metrics_and_mutations_require_admin(team):
    app,client,_=team
    assert client.get('/api/governance/status').status_code==401
    login(client,'alice')
    assert client.get('/api/governance/status').status_code==403
    assert client.get('/api/governance/metrics').status_code==403
    assert client.put('/api/governance/limits/user/target',json={'daily_units':1},headers=WRITE).status_code==403
    assert client.get('/api/governance/usage').status_code==200
    login(client,'admin')
    assert client.get('/api/governance/status').status_code==200
    response=client.get('/api/governance/metrics')
    assert response.status_code==200 and 'version=0.0.4' in response.headers['content-type']
    assert client.put('/api/governance/limits/user/target',json={'daily_units':1}).status_code==403
    assert client.put('/api/governance/limits/user/target',json={'daily_units':False},headers=WRITE).status_code==422
    assert client.put('/api/governance/limits/user/target',json={'daily_units':1,'actor':'alice'},headers=WRITE).status_code==422
    assert client.put('/api/governance/limits/user/target',json={'daily_units':1},headers=WRITE).status_code==200
    audit=client.get('/api/governance/audit').json()
    assert audit[0]['action']=='set_limit' and audit[0]['target']=='user:target'


def test_workspace_runtimes_share_governor_and_usage_is_current_user_only(team):
    app,client,_=team
    login(client,'alice')
    alpha,beta=workspace(client,'Alpha'),workspace(client,'Beta')
    user=client.get('/api/access/me').json()['user']['id']
    for wid in (alpha,beta):
        assert client.get(f'/w/{wid}/api/health').status_code==200
    assert app.state.workspace_runtimes.apps[alpha].state.governance is app.state.governance
    assert app.state.workspace_runtimes.apps[beta].state.governance is app.state.governance
    for wid in (alpha,beta):
        with usage_context(app.state.governance,user=user,workspace=wid):
            app.state.governance.invoke('llm','synthetic',100,lambda:LLMResponse('done',total_tokens=10))
    usage=client.get(f'/w/{alpha}/api/governance/usage').json()
    assert usage['user']['used_or_held']==20 and usage['workspace']['used_or_held']==10
    login(client,'bob')
    assert client.get(f'/w/{alpha}/api/governance/usage').status_code==404
    assert client.get('/api/governance/usage').json()['user']['used_or_held']==0


def test_background_sse_charges_authenticated_actor_and_idempotent_replay_is_free(team):
    app,client,_=team
    login(client,'alice')
    wid=workspace(client,'Metered')
    prefix=f'/w/{wid}/api'
    sid=client.post(prefix+'/sessions',json={'title':'cost'},headers=WRITE).json()['id']
    child=app.state.workspace_runtimes.apps[wid]
    model=provider(success)
    try:
        child.state.answer_service.answer=lambda question,**kwargs:Answer(text=model.generate(question).text,sources=[])
        headers={**WRITE,'Idempotency-Key':'metered-request'}
        first=client.post(prefix+f'/sessions/{sid}/turns/stream',json={'question':QUESTION},headers=headers)
        assert first.status_code==200 and 'event: done' in first.text,first.text
        replay=client.post(prefix+f'/sessions/{sid}/turns/stream',json={'question':QUESTION},headers=headers)
        assert 'event: done' in replay.text
        usage=client.get(prefix+'/governance/usage').json()
        assert usage['user']['used_or_held']==12 and usage['workspace']['used_or_held']==12
        with app.state.governance.store.connect() as db:
            row=db.execute('SELECT * FROM cost_reservations').fetchone()
            assert row['user_id']==usage['user']['subject'] and row['workspace_id']==wid and row['run_id']
        assert app.state.governance.snapshot()['resources']['research']['active']==0
    finally:
        model.close()


def test_http_200_sse_failure_is_business_failure_and_unknown_cost(team):
    app,client,_=team
    login(client,'alice')
    wid=workspace(client,'Failure')
    prefix=f'/w/{wid}/api'
    sid=client.post(prefix+'/sessions',json={'title':'failure'},headers=WRITE).json()['id']
    child=app.state.workspace_runtimes.apps[wid]
    def timeout(request):
        raise httpx.ReadTimeout('private upstream details')
    model=provider(timeout)
    try:
        child.state.answer_service.answer=lambda question,**kwargs:model.generate(question)
        response=client.post(prefix+f'/sessions/{sid}/turns/stream',json={'question':QUESTION},headers=WRITE)
        assert response.status_code==200 and 'event: error' in response.text
        assert 'private upstream' not in response.text
        snapshot=app.state.governance.snapshot()
        assert snapshot['expenses']['unknown_count']==1 and snapshot['resources']['provider']['active']==0
        text=app.state.governance.render()
        assert 'deepsearch_research_total{outcome="interrupted"} 1' in text
        assert 'status="200"' in text
    finally:
        model.close()


def test_admin_settles_unknown_once_with_actor_and_proof(team):
    app,client,_=team
    login(client,'alice')
    user=client.get('/api/access/me').json()['user']['id']
    with usage_context(app.state.governance,user=user,workspace='synthetic-workspace'):
        app.state.governance.invoke('llm','synthetic',100,lambda:LLMResponse('missing usage'))
    ident=app.state.governance.store.unsettled()[0]['id']
    url=f'/api/governance/reservations/{ident}/settle'
    body={'actual_units':10,'confirmed':True,'proof_reference':'provider receipt R-1'}
    assert client.post(url,json=body,headers=WRITE).status_code==403
    login(client,'admin')
    assert client.post(url,json={**body,'confirmed':False},headers=WRITE).status_code==422
    assert client.post(url,json=body,headers=WRITE).status_code==200
    assert client.post(url,json=body,headers=WRITE).status_code==409
    assert app.state.governance.store.usage('user',user)['used_or_held']==10


def test_local_quota_blocks_before_provider_and_returns_safe_sse_code(tmp_path):
    with TestClient(create_app(tmp_path/'index.json',llm_settings=LLMSettings())) as client:
        sid=client.post('/api/sessions',json={'title':'quota'}).json()['id']
        assert client.put('/api/governance/limits/user/local',json={'daily_units':0}).status_code==200
        model=provider(lambda request:(_ for _ in ()).throw(AssertionError('not contacted')))
        try:
            client.app.state.answer_service.answer=lambda question,**kwargs:model.generate(question)
            response=client.post(f'/api/sessions/{sid}/turns/stream',json={'question':QUESTION})
            assert response.status_code==200 and 'user_quota_exceeded' in response.text
            assert client.get('/api/governance/status').json()['expenses']['unknown_count']==0
        finally:
            model.close()
