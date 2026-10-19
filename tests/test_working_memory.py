from deepsearch_agent.adapters.tool_adapters import langgraph_tools
from uuid import uuid4

import pytest

from deepsearch_agent.workspace.working_memory import WorkingMemoryError, WorkingMemoryStore


def test_two_markdown_files_persist_and_isolate(tmp_path):
    store = WorkingMemoryStore(tmp_path)
    session_id = str(uuid4())
    memory = store.bind(session_id)
    old = memory.read('plan')
    memory.replace('plan', '# Plan\n- Read chart', old['revision'])
    assert {p.name for p in (tmp_path / session_id).iterdir()} == {'PLAN.md', 'HISTORY.md'}
    restored = WorkingMemoryStore(tmp_path).bind(session_id)
    assert 'Read chart' in restored.read('plan')['content']
    assert 'Read chart' not in store.bind(str(uuid4())).read('plan')['content']
    assert 'Read chart' not in restored.read('history')['content']


def test_replace_requires_read_and_current_revision(tmp_path):
    store = WorkingMemoryStore(tmp_path)
    session_id = str(uuid4())
    first, second = store.bind(session_id), store.bind(session_id)
    before = first.read('plan')
    with pytest.raises(WorkingMemoryError, match='memory_read_required'):
        second.replace('plan', 'bad', before['revision'])
    second.read('plan')
    first.replace('plan', 'new', before['revision'])
    with pytest.raises(WorkingMemoryError, match='memory_conflict'):
        second.replace('plan', 'stale', before['revision'])
    assert second.read('plan')['content'] == 'new'


def test_clear_revokes_inflight_handles(tmp_path):
    store = WorkingMemoryStore(tmp_path)
    session_id = str(uuid4())
    memory = store.bind(session_id)
    before = memory.read('history')
    store.clear(session_id)
    with pytest.raises(WorkingMemoryError, match='memory_scope_expired'):
        memory.replace('history', 'late write', before['revision'])
    assert 'late write' not in store.bind(session_id).read('history')['content']


def test_deleted_session_cannot_recreate_files(tmp_path):
    existing = {str(uuid4())}
    session_id = next(iter(existing))
    store = WorkingMemoryStore(tmp_path, session_exists=lambda sid: sid in existing)
    memory = store.bind(session_id)
    existing.clear()
    store.clear(session_id)
    with pytest.raises(WorkingMemoryError, match='memory_scope_expired'):
        memory.read('plan')
    with pytest.raises(WorkingMemoryError, match='memory_scope_expired'):
        store.bind(session_id)
    assert not (tmp_path / session_id).exists()


@pytest.mark.parametrize('session_id', ['../outside', '', '/tmp/private', 'PLAN.md'])
def test_scope_cannot_choose_paths(tmp_path, session_id):
    with pytest.raises(WorkingMemoryError, match='memory_invalid_scope'):
        WorkingMemoryStore(tmp_path).bind(session_id)


def test_document_and_size_limits(tmp_path):
    memory = WorkingMemoryStore(tmp_path).bind(str(uuid4()))
    with pytest.raises(WorkingMemoryError, match='memory_invalid_document'):
        memory.read('../secret')
    revision = memory.read('plan')['revision']
    with pytest.raises(WorkingMemoryError, match='memory_too_large'):
        memory.replace('plan', 'x' * 2001, revision)


def test_concurrent_writers_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store, session_id = WorkingMemoryStore(tmp_path), str(uuid4())
    barrier = Barrier(2)
    def write(content):
        memory = store.bind(session_id)
        revision = memory.read('plan')['revision']
        barrier.wait(timeout=5)
        try:
            memory.replace('plan', content, revision)
            return 'ok'
        except WorkingMemoryError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ['first', 'second']))
    assert sorted(results) == ['memory_conflict', 'ok']


def test_chinese_memory_fits_tool_action_budget():
    from deepsearch_agent.agents.agentic_research import ToolAction
    action = ToolAction(tool='working_memory', arguments={
        'action': 'replace', 'document': 'history', 'content': '中文' * 1000,
        'expected_revision': 'a' * 64})
    assert len(action.arguments['content']) == 2000


def test_tool_is_bound_optional_and_never_evidence(tmp_path):
    from deepsearch_agent.agents.agent_config import AgentSettings
    from deepsearch_agent.agents.tool_service import PaperToolSession, ToolError
    from types import SimpleNamespace

    kb = SimpleNamespace(chunks=[], image_index=None)
    unbound = PaperToolSession(kb)
    assert 'working_memory' not in {s['name'] for s in unbound.specs()}
    with pytest.raises(ToolError, match='unknown_tool'):
        unbound.invoke('working_memory', {'action': 'read', 'document': 'plan'})
    memory = WorkingMemoryStore(tmp_path).bind(str(uuid4()))
    session = PaperToolSession(kb, working_memory=memory, settings=AgentSettings(max_tool_calls=8))
    assert 'working_memory' in {t.name for t in langgraph_tools(session)}
    old = session.invoke('working_memory', {'action': 'read', 'document': 'history'})
    session.invoke('working_memory', {'action': 'replace', 'document': 'history',
        'content': '# Completed\n- Read chart\n# Preferences\n- User requested Chinese',
        'expected_revision': old.data['revision']})
    assert session.evidence == []
    assert session.registry == {}
    with pytest.raises(ToolError, match='invalid_arguments'):
        session.invoke('working_memory', {'action': 'read', 'document': 'plan', 'session_id': 'other'})
    with pytest.raises(ToolError, match='invalid_arguments'):
        session.invoke('working_memory', {'action': 'replace', 'document': 'plan'})


def test_atomic_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    import deepsearch_agent.workspace.working_memory as module
    memory = WorkingMemoryStore(tmp_path).bind(str(uuid4()))
    before = memory.read('plan')
    def fail(*args):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(module.os, 'replace', fail)
    with pytest.raises(OSError):
        memory.replace('plan', 'lost', before['revision'])
    assert memory.read('plan') == before
    assert not list(tmp_path.rglob('*.tmp'))


def test_symlink_document_is_rejected(tmp_path):
    session_id = str(uuid4())
    memory = WorkingMemoryStore(tmp_path).bind(session_id)
    memory.read('plan')
    path = tmp_path / session_id / 'PLAN.md'
    path.unlink()
    target = tmp_path / 'private.md'
    target.write_text('private', encoding='utf-8')
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip('Symlink creation is unavailable for this Windows account')
    with pytest.raises(WorkingMemoryError, match='memory_unsafe_path'):
        memory.read('plan')
    assert target.read_text(encoding='utf-8') == 'private'


def test_model_chosen_memory_tools_roundtrip_through_langgraph(tmp_path):
    import json
    from types import SimpleNamespace
    from deepsearch_agent.agents.agent_config import AgentSettings
    from deepsearch_agent.agents.agentic_research import AgenticResearcher
    from deepsearch_agent.agents.tool_service import PaperToolSession

    kb = SimpleNamespace(chunks=[], image_index=None, search=lambda *args, **kwargs: [])
    session_id = str(uuid4())
    store = WorkingMemoryStore(tmp_path)
    session = PaperToolSession(kb, working_memory=store.bind(session_id),
                               settings=AgentSettings(max_tool_calls=8))
    calls = []
    def planner(instruction, schema, data):
        calls.append(data)
        assert 'not evidence' in instruction
        if len(calls) == 1:
            return {'tool': 'working_memory', 'arguments': {'action': 'read', 'document': 'history'}}
        if len(calls) == 2:
            previous = json.loads(data['observations'][-1]['result'])
            return {'tool': 'working_memory', 'arguments': {'action': 'replace', 'document': 'history',
                'expected_revision': previous['revision'], 'content': '# User Preferences\n- Answer in Chinese'}}
        return {'tool': '', 'arguments': {}}
    result = AgenticResearcher(session, planner, tools_factory=langgraph_tools).run('Continue', {}, [])
    assert result == []
    assert session.calls == 3  # Two memory calls and the mandatory evidence search.
    assert 'Answer in Chinese' in store.bind(session_id).read('history')['content']


def test_api_binds_next_turn_and_cleans_up(tmp_path):
    from fastapi.testclient import TestClient
    from deepsearch_agent.api import create_app
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.core.answers import Answer

    app = create_app(tmp_path / 'index.json', llm_settings=LLMSettings(backend='demo'))
    with TestClient(app) as client:
        session_id = client.post('/api/sessions', json={'title': 'memory'}).json()['id']
        base = f'/api/sessions/{session_id}'
        app.state.agentic_rag_enabled = True
        calls, handles = [], []
        def answer(**kwargs):
            memory = kwargs['working_memory']
            handles.append(memory)
            old = memory.read('history')
            calls.append(old['content'])
            memory.replace('history', '# Completed\n- Step A', old['revision'])
            return Answer('ok', [])
        app.state.answer_service.answer = answer
        for _ in range(2):
            assert client.post(base + '/turns', json={'question': '模型怎么训练？'}).status_code == 201
        assert 'Step A' not in calls[0]
        assert 'Step A' in calls[1]
        assert 'Step A' in client.get(base + '/working-memory').json()['history']['content']
        assert client.delete(base + '/working-memory').status_code == 200
        with pytest.raises(WorkingMemoryError, match='memory_scope_expired'):
            handles[-1].read('plan')
        assert 'Step A' not in client.get(base + '/working-memory').json()['history']['content']
        assert client.delete(base + '/memory').status_code == 200
        assert not (app.state.working_memory.root / session_id).exists()
        client.get(base + '/working-memory')
        assert client.delete(base).status_code == 200
        assert not (app.state.working_memory.root / session_id).exists()
        assert client.get(base + '/working-memory').status_code == 404
