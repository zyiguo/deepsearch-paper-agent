import json
import asyncio

from fastapi.testclient import TestClient

from deepsearch_agent.agents.agent_config import AgentSettings
from deepsearch_agent.api import create_app
from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.core.answers import Answer
from deepsearch_agent.application.answer_service import AnswerService
from deepsearch_agent.agents.tool_service import PaperToolSession


class Library:
    image_index = None
    def __init__(self):
        self.chunks = [TextChunk('verified local fact', 'a.pdf', 1, 0)]
    def search(self, query, top_k=5):
        return self.chunks


class Planner:
    def __init__(self):
        self.outputs = iter([{'tool': 'search_evidence', 'arguments': {'query': 'fact'}},
                             {'tool': '', 'arguments': {}},
                             {'claims': [{'text': 'verified local fact', 'citations': [1]}]},
                             {'supported': [0], 'queries': []}])
        self.prompts = []
    def complete(self, prompt):
        self.prompts.append(prompt)
        if prompt.startswith('你是 Supervisor。'):
            state = json.loads(prompt.split('\ndata: ', 1)[1])
            action = 'research' if not state['research_rounds'] else 'draft' if not state['verified'] else 'finish'
            return json.dumps({'action': action, 'tasks': [], 'instruction': 'verify'})
        return json.dumps(next(self.outputs))


def test_agentic_answer_still_uses_verifier_and_original_ids():
    library = Library()
    client = Planner()
    result = AnswerService(client, library, use_graph=True,
        agent_settings=AgentSettings(enabled=True),
        tool_session_factory=lambda kb, **kwargs: PaperToolSession(kb, **kwargs)).answer('fact?')
    assert result.text == 'verified local fact [1]'
    assert result.sources[0].chunk_id == library.chunks[0].chunk_id
    assert any(p.startswith('你是 Verifier。') for p in client.prompts)


def test_answer_service_passes_bound_memory_to_tool_factory(tmp_path):
    from uuid import uuid4
    from deepsearch_agent.workspace.working_memory import WorkingMemoryStore
    memory = WorkingMemoryStore(tmp_path).bind(str(uuid4()))
    seen = []
    def factory(kb, **kwargs):
        seen.append(kwargs.pop('working_memory'))
        return PaperToolSession(kb, working_memory=seen[-1], **kwargs)
    library, client = Library(), Planner()
    result = AnswerService(client, library, use_graph=True,
        agent_settings=AgentSettings(enabled=True), tool_session_factory=factory).answer(
            'fact?', working_memory=memory)
    assert seen == [memory]
    assert result.sources[0].chunk_id == library.chunks[0].chunk_id
    assert any('working_memory' in prompt for prompt in client.prompts)


def test_recovered_notes_reach_answerer_but_not_verifier(tmp_path):
    from uuid import uuid4
    from deepsearch_agent.workspace.working_memory import WorkingMemoryStore
    store, session_id = WorkingMemoryStore(tmp_path), str(uuid4())
    previous = store.bind(session_id)
    revision = previous.read('history')['revision']
    previous.replace('history', '# User Preferences\n- User explicitly requested concise answers.', revision)
    client = Planner()
    client.outputs = iter([
        {'tool': 'working_memory', 'arguments': {'action': 'read', 'document': 'history'}},
        {'tool': 'search_evidence', 'arguments': {'query': 'fact'}},
        {'tool': '', 'arguments': {}},
        {'claims': [{'text': 'verified local fact', 'citations': [1]}]},
        {'supported': [0], 'queries': []},
    ])
    result = AnswerService(client, Library(), use_graph=True,
        agent_settings=AgentSettings(enabled=True)).answer('fact?', working_memory=store.bind(session_id))
    assert result.text == 'verified local fact [1]'
    for prompt in client.prompts:
        if prompt.startswith('你是 Answerer。'):
            data = json.loads(prompt.split('\ndata: ', 1)[1])
            assert 'concise answers' in data['working_notes']['history']
        if prompt.startswith('你是 Verifier。'):
            data = json.loads(prompt.split('\ndata: ', 1)[1])
            assert 'working_notes' not in data


def test_ask_releases_index_lock_and_uses_request_snapshot(tmp_path):
    app = create_app(tmp_path / 'demo.json', llm_settings=LLMSettings(backend='demo'),
                     embedding_settings=EmbeddingSettings(backend='demo'))
    with TestClient(app) as client:
        client.post('/api/demo/ingest')
        service = app.state.answer_service
        original = service.knowledge_base
        calls = []
        def answer(question, **kwargs):
            acquired = app.state.lock.acquire(blocking=False)
            if acquired:
                app.state.lock.release()
            calls.append(acquired)
            return Answer('ok', [])
        service.answer = answer
        assert client.post('/api/ask', json={'question': '模型怎么训练？'}).status_code == 200
        assert calls == [True]
        assert service.knowledge_base is original


def test_conversation_releases_index_lock_during_tools(tmp_path):
    app = create_app(tmp_path / 'demo.json', llm_settings=LLMSettings(backend='demo'),
                     embedding_settings=EmbeddingSettings(backend='demo'))
    with TestClient(app) as client:
        client.post('/api/demo/ingest')
        session = client.post('/api/sessions', json={'title': 'tools'}).json()
        calls = []
        def answer(**kwargs):
            acquired = app.state.lock.acquire(blocking=False)
            if acquired:
                app.state.lock.release()
            calls.append(acquired)
            return Answer('ok', [])
        app.state.answer_service.answer = answer
        response = client.post(f"/api/sessions/{session['id']}/turns", json={'question': '模型怎么训练？'})
        assert response.status_code == 201
        assert calls == [True]


def test_mounted_mcp_uses_app_lifespan_and_scoped_snapshot(tmp_path):
    import pytest
    mcp = pytest.importorskip('mcp')
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from deepsearch_agent.adapters.mcp_config import MCPSettings
    from deepsearch_agent.adapters.demo import DEMO_SOURCE
    app = create_app(tmp_path / 'demo.json', llm_settings=LLMSettings(backend='demo'),
        embedding_settings=EmbeddingSettings(backend='demo'),
        mcp_settings=MCPSettings(server_enabled=True, server_sources=(DEMO_SOURCE,)))

    async def run():
        async with app.router.lifespan_context(app):
            app.state.kb.ingest_pdf(DEMO_SOURCE)
            original = list(app.state.kb.chunks)
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                          base_url='http://127.0.0.1:8000') as http:
                transport = streamable_http_client('http://127.0.0.1:8000/mcp/', http_client=http)
                async with mcp.Client(transport) as client:
                    result = await client.call_tool('list_documents', {})
                    assert not result.is_error, str(result)
                    docs = result.structured_content['data']['documents']
                    assert {d['source_path'] for d in docs} == {DEMO_SOURCE}
                    forbidden = await client.call_tool('read_page', {'source': 'C:/private.pdf', 'page': 1})
                    assert forbidden.is_error
                    denied = await client.call_tool('ocr_page', {'source': DEMO_SOURCE, 'page': 1})
                    assert denied.is_error
                invalid_origin = await http.post('/mcp/', headers={
                    'Origin': 'https://untrusted.example', 'Content-Type': 'application/json'},
                    json={'jsonrpc': '2.0', 'id': 'bad-origin', 'method': 'tools/list'})
                assert invalid_origin.status_code == 403
            assert app.state.kb.chunks == original
    asyncio.run(run())


def test_snapshot_does_not_follow_later_index_mutation(tmp_path):
    from deepsearch_agent.application.snapshots import snapshot_answer_service
    app = create_app(tmp_path / 'demo.json', llm_settings=LLMSettings(backend='demo'),
                     embedding_settings=EmbeddingSettings(backend='demo'))
    with TestClient(app) as client:
        client.post('/api/demo/ingest')
        with app.state.lock:
            request_service = snapshot_answer_service(app.state)
            original = list(request_service.knowledge_base.chunks)
            app.state.kb.chunks.clear()
        assert request_service.knowledge_base.chunks == original


def test_uploaded_image_is_visible_to_tool_planner():
    from types import SimpleNamespace
    library, client = Library(), Planner()
    seen = []
    class Visual:
        settings = SimpleNamespace(max_images=8)
        def complete(self, prompt, evidence, **kwargs):
            if prompt.startswith('你是 Researcher。'):
                seen.extend(evidence)
            return client.complete(prompt)
    image = TextChunk('User attached image: chart.png', 'attachment://session/' + 'a' * 64 + '/chart.png',
                      1, 0, evidence_kind='figure', visual_pages=[1])
    AnswerService(client, library, use_graph=True, visual_evidence=Visual(),
        agent_settings=AgentSettings(enabled=True)).answer('What is plotted?', input_evidence=[image])
    assert seen
    assert all(item['source'] == image.source_path for item in seen)
