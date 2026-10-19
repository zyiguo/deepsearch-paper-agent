import asyncio
import json
import sqlite3
from threading import Event, Thread

import httpx
import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_experiments.legacy.workflow import LegacyResearchWorkflow as ResearchWorkflow
from deepsearch_agent.core.cancellation import CancellationToken, ResearchCancelled, current_token
from deepsearch_agent.adapters.persistence.research_runs import ResearchRunStore
from test_research_graph import Client, Library


def test_interrupted_graph_resumes_from_next_node(tmp_path):
    connection = sqlite3.connect(tmp_path / 'checkpoints.sqlite3', check_same_thread=False)
    try:
        saver = SqliteSaver(connection)
        first = ResearchWorkflow(Client([{'queries': ['light']}]), Library(), checkpointer=saver, run_id='run-one')
        with pytest.raises(RuntimeError, match='generator raised StopIteration'):
            first.answer('signal?', 1, [])
        snapshot = first.graph.get_state({'configurable': {'thread_id': 'run-one'}})
        assert snapshot.next == ('answerer',)
        resumed = ResearchWorkflow(Client([{'claims': [{'text': 'white light', 'citations': [1]}]},
                                           {'supported': [0], 'queries': []}]), Library(),
                                   checkpointer=saver, run_id='run-one')
        assert resumed.answer('signal?', 1, [], resume=True).text == 'white light [1]'
    finally:
        connection.close()


def test_run_metadata_survives_restart_and_claim_is_atomic(tmp_path):
    path = tmp_path / 'runs.sqlite3'
    store = ResearchRunStore(path)
    run_id = store.create('session-a', {'question': 'why?', 'top_k': 3})
    store.update(run_id, 'running', stage='retrieving')
    restarted = ResearchRunStore(path)
    assert restarted.get(run_id, 'session-a')['status'] == 'interrupted'
    assert restarted.get(run_id, 'session-b') is None
    assert restarted.claim_resume(run_id, 'session-a')
    assert not restarted.claim_resume(run_id, 'session-a')


def test_inflight_http_request_is_cancelled():
    entered = Event()
    async def handler(request):
        entered.set()
        await asyncio.sleep(60)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'late'}}]})
    client = OpenAICompatibleClient(LLMSettings(backend='openai_compatible', api_key='test',
        base_url='https://example.com/v1', model='test'), transport=httpx.MockTransport(handler))
    token = CancellationToken()
    result = []
    def run():
        reset = current_token.set(token)
        try:
            client.complete('hello')
        except ResearchCancelled:
            result.append('cancelled')
        finally:
            current_token.reset(reset)
    thread = Thread(target=run)
    thread.start()
    assert entered.wait(3)
    token.cancel()
    thread.join(3)
    client.close()
    assert not thread.is_alive()
    assert result == ['cancelled']


def test_stream_run_is_persisted_with_answer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    outputs = [{'queries': ['模型怎么训练？']}, {'claims': [{'text': '对比学习', 'citations': [1]}]},
               {'supported': [0], 'queries': []}] * 4
    monkeypatch.setattr('deepsearch_agent.application.bootstrap.build_llm', lambda _: Client(outputs))
    settings = LLMSettings(backend='openai_compatible', api_key='test',
                           base_url='https://example.com/v1', model='test')
    with TestClient(create_app(index_path=tmp_path / 'index.json', llm_settings=settings)) as api:
        api.post('/api/demo/ingest').raise_for_status()
        session = api.post('/api/sessions', json={'title': 'research'}).json()['id']
        response = api.post(f'/api/sessions/{session}/turns/stream', json={'question': '模型怎么训练？', 'top_k': 1})
        assert response.status_code == 200
        assert '"run_id"' in response.text
        assert 'event: done' in response.text
        runs = api.get(f'/api/sessions/{session}/runs').json()
        assert len(runs) == 1 and runs[0]['status'] == 'completed'
        assert runs[0]['turn_id'] == api.get(f'/api/sessions/{session}').json()['turns'][0]['id']
        assert api.post(f'/api/sessions/{session}/runs/{runs[0]["id"]}/resume/stream').status_code == 409
