import json
from threading import RLock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from deepsearch_agent.conversation_api import router
from deepsearch_agent.conversations import ConversationStore
from deepsearch_agent.qa import Answer, AnswerService
from test_supervisor import Script, Library, decision


def test_graph_progress_emits_stages_without_draft_contents():
    events = []
    steps = [('Supervisor', decision('research')), ('Researcher', {'queries': ['signal']}),
             ('Supervisor', decision('draft')), ('Answerer', {'claims': [{'text': '白光', 'citations': [1]}]}),
             ('Verifier', {'supported': [0], 'queries': []}), ('Supervisor', decision('finish'))]
    result = AnswerService(Script(steps), Library(), use_graph=True).answer('信号', on_progress=events.append)
    assert result.text == '白光 [1]'
    assert events == ['planning', 'retrieving', 'planning', 'drafting', 'verifying', 'planning']


def app_for(tmp_path, broken=False):
    app = FastAPI()
    app.include_router(router)
    app.state.lock = RLock()
    app.state.demo_retrieval = False
    from types import SimpleNamespace
    from deepsearch_agent.chunking import TextChunk
    app.state.kb = SimpleNamespace(chunks=[TextChunk(text='fixture', source_path='pdf://' + 'a' * 64 + '/fixture.pdf', page_number=1, chunk_index=0)])
    app.state.conversations = ConversationStore(tmp_path / 'sessions.sqlite')
    class Service:
        def answer(self, **kwargs):
            kwargs['on_progress']('retrieving')
            if broken:
                raise RuntimeError('SECRET_DO_NOT_SEND')
            return Answer('第一项。\n\n第二项。', [])
    app.state.answer_service = Service()
    return app


def test_stream_saves_once_and_emits_terminal_done(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app) as client:
        sid = client.post('/api/sessions', json={'title': 'test'}).json()['id']
        response = client.post(f'/api/sessions/{sid}/turns/stream', json={'question': '问题'})
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        assert 'event: status' in response.text
        frames = [f for f in response.text.split('\n\n') if f.startswith('event:')]
        assert frames[-1].startswith('event: done')
        saved = client.get(f'/api/sessions/{sid}').json()['turns']
        assert len(saved) == 1
        done = json.loads(frames[-1].split('data: ', 1)[1])
        assert done == saved[0]


def test_stream_error_is_sanitized_and_not_saved(tmp_path):
    with TestClient(app_for(tmp_path, broken=True)) as client:
        sid = client.post('/api/sessions', json={'title': 'test'}).json()['id']
        response = client.post(f'/api/sessions/{sid}/turns/stream', json={'question': '问题'})
        assert 'event: error' in response.text
        assert 'SECRET' not in response.text
        assert 'event: done' not in response.text
        assert client.get(f'/api/sessions/{sid}').json()['turns'] == []


def test_stream_missing_session_returns_404_before_opening(tmp_path):
    with TestClient(app_for(tmp_path)) as client:
        assert client.post('/api/sessions/missing/turns/stream', json={'question': '问题'}).status_code == 404


def test_progress_arrives_while_answer_is_still_blocked(tmp_path):
    import asyncio
    from threading import Event
    from types import SimpleNamespace
    from deepsearch_agent.conversation_api import stream_turn, SendTurnRequest

    release = Event()
    app = app_for(tmp_path)
    sid = app.state.conversations.create('test')['id']
    class BlockingService:
        def answer(self, **kwargs):
            kwargs['on_progress']('verifying')
            assert release.wait(timeout=5)
            return Answer('核验后答案', [])
    app.state.answer_service = BlockingService()

    async def consume():
        response = stream_turn(sid, SendTurnRequest(question='问题'), SimpleNamespace(app=app))
        iterator = response.body_iterator
        try:
            queued = await anext(iterator)
            assert 'queued' in queued
            progress = await asyncio.wait_for(anext(iterator), timeout=2)
            assert 'verifying' in progress
            assert 'event: answer' not in progress
            assert app.state.conversations.get(sid)['turns'] == []
        finally:
            release.set()
        frames = [frame async for frame in iterator]
        assert any('event: answer' in frame for frame in frames)
        assert 'event: done' in frames[-1]
    asyncio.run(consume())
