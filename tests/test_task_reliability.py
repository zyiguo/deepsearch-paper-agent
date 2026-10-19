"""Fault injection for persisted submissions, cancellation and ambiguous commits."""
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from deepsearch_agent.api import create_app
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.application.commands import TurnCommand
from deepsearch_agent.application import session_runs
from deepsearch_agent.core.answers import Answer
from deepsearch_agent.core.cancellation import current_token
from deepsearch_agent.core.cancellation import CancellationToken
from deepsearch_agent.core.errors import ApplicationError

QUESTION = '模型怎么训练？'


@pytest.fixture
def runtime(tmp_path):
    app = create_app(tmp_path / 'index.json', llm_settings=LLMSettings())
    with TestClient(app, raise_server_exceptions=False) as api:
        api.post('/api/demo/ingest').raise_for_status()
        sid = api.post('/api/sessions', json={'title': 'reliability'}).json()['id']
        yield api, app.state, sid


def test_sync_and_stream_replay_share_one_durable_submission(runtime, monkeypatch):
    api, state, sid = runtime
    calls = []
    monkeypatch.setattr(state.answer_service, 'answer', lambda **kwargs: calls.append(kwargs) or Answer('fixture', []))
    path = f'/api/sessions/{sid}/turns'
    payload = {'question': QUESTION}
    headers = {'Idempotency-Key': 'client-request-one'}
    a = api.post(path, json=payload, headers=headers)
    b = api.post(path, json=payload, headers=headers)
    assert a.status_code == b.status_code == 201
    assert a.json() == b.json() and len(calls) == 1
    c = api.post(path + '/stream', json=payload, headers=headers)
    assert 'event: done' in c.text and len(calls) == 1
    assert api.post(path, json={'question': QUESTION, 'top_k': 1}, headers=headers).status_code == 409
    runs = api.get(f'/api/sessions/{sid}/runs').json()
    assert len(runs) == 1 and runs[0]['status'] == 'completed'
    assert runs[0]['request']['_execution']['attempts'] == 1
    assert len(state.conversations.get(sid)['turns']) == 1


def test_ambiguous_completion_is_reconciled_without_model_reexecution(runtime, monkeypatch):
    api, state, sid = runtime
    calls = []
    monkeypatch.setattr(state.answer_service, 'answer', lambda **kwargs: calls.append(1) or Answer('persisted', []))
    original = state.research_runs.update
    failed = []
    def update(rid, status, **kwargs):
        if status == 'completed' and not failed:
            failed.append(1)
            raise RuntimeError('injected after answer commit')
        return original(rid, status, **kwargs)
    monkeypatch.setattr(state.research_runs, 'update', update)
    response = api.post(f'/api/sessions/{sid}/turns/stream', json={'question': QUESTION})
    assert 'event: done' in response.text
    run = state.research_runs.list(sid)[0]
    assert run['status'] == 'completed' and run['request']['_execution']['result_reconciled']
    assert len(calls) == len(state.conversations.get(sid)['turns']) == 1


def test_terminal_database_failure_still_terminates_sse(runtime, monkeypatch):
    api, state, sid = runtime
    def fail(**kwargs):
        raise RuntimeError('never disclose provider-secret')
    monkeypatch.setattr(state.answer_service, 'answer', fail)
    original = state.research_runs.update
    def update(rid, status, **kwargs):
        if status == 'interrupted':
            raise RuntimeError('injected unavailable database')
        return original(rid, status, **kwargs)
    monkeypatch.setattr(state.research_runs, 'update', update)
    response = api.post(f'/api/sessions/{sid}/turns/stream', json={'question': QUESTION})
    assert 'event: error' in response.text
    assert 'provider-secret' not in response.text
    assert state.conversations.get(sid)['turns'] == []


def test_resume_rejects_changed_runtime_and_missing_checkpoint_without_spending(runtime, monkeypatch):
    api, state, sid = runtime
    rid, _ = session_runs.prepare_run(sid, TurnCommand(question=QUESTION), state)
    state.research_runs.update(rid, 'interrupted')
    calls = []
    monkeypatch.setattr(state.answer_service, 'answer', lambda **kwargs: calls.append(1) or Answer('never', []))
    path = f'/api/sessions/{sid}/runs/{rid}/resume/stream'
    assert api.post(path).status_code == 409
    assert state.research_runs.get(rid, sid)['status'] == 'interrupted'
    state.run_identity = 'changed-model-and-budget'
    assert api.post(path).status_code == 409
    assert calls == []


def test_resume_repairs_saved_answer_even_without_checkpoint(runtime):
    api, state, sid = runtime
    rid, _ = session_runs.prepare_run(sid, TurnCommand(question=QUESTION), state)
    turn = state.conversations.append_turn(sid, QUESTION, Answer('already saved', []), run_id=rid)
    state.research_runs.update(rid, 'interrupted')
    response = api.post(f'/api/sessions/{sid}/runs/{rid}/resume/stream')
    assert 'event: done' in response.text
    assert state.research_runs.get(rid, sid)['turn_id'] == turn['id']
    assert len(state.conversations.get(sid)['turns']) == 1


def test_disconnect_does_not_strand_the_worker_and_cancel_does_not_regress(runtime, monkeypatch):
    _, state, sid = runtime
    entered, release = Event(), Event()
    def answer(**kwargs):
        entered.set()
        assert release.wait(5)
        current_token.get().check()
        return Answer('must not save', [])
    monkeypatch.setattr(state.answer_service, 'answer', answer)
    body = TurnCommand(question=QUESTION)
    rid, _ = session_runs.prepare_run(sid, body, state)
    stream = session_runs.run_stream(sid, body, state, rid)
    assert entered.wait(3)
    stream.close()
    session_runs.cancel_run(sid, rid, state)
    assert not state.research_runs.update(rid, 'running', expected={'running'})
    release.set()
    state.run_workers.close()
    assert state.research_runs.get(rid, sid)['status'] == 'cancelled'
    assert state.conversations.get(sid)['turns'] == []


def test_worker_capacity_is_shared_with_sync_and_shutdown_waits_for_work():
    workers = session_runs.RunWorkers(max_active=1)
    entered, release, finished = Event(), Event(), Event()
    token = CancellationToken()
    def work():
        entered.set()
        release.wait(3)
        assert token.event.is_set()
        finished.set()
    workers.start(work, token)
    assert entered.wait(1)
    with pytest.raises(ApplicationError):
        with workers.track(CancellationToken()):
            pass
    with pytest.raises(ApplicationError):
        workers.start(lambda: None, CancellationToken())
    closer = Thread(target=workers.close)
    closer.start()
    assert token.event.wait(1)
    assert closer.is_alive()
    release.set()
    closer.join(2)
    assert not closer.is_alive()
    assert finished.is_set()
    with pytest.raises(ApplicationError):
        with workers.track(CancellationToken()):
            pass
