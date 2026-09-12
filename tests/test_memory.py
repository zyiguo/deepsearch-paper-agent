import json
from deepsearch_agent.conversations import ConversationStore
from deepsearch_agent.qa import Answer


def setup(tmp_path):
    from deepsearch_agent.memory import MemoryManager
    store = ConversationStore(tmp_path / 'chat.db')
    sid = store.create('research')['id']
    return store, sid, MemoryManager(store)


def test_scope_change_and_clear_keep_history_but_stop_injection(tmp_path):
    store, sid, manager = setup(tmp_path)
    manager.set_scope(sid, ['a'])
    manager.edit(sid, research_goal='比较方法', language='zh', answer_style='简洁')
    store.append_turn(sid, 'old question', Answer('old answer', []))
    assert len(manager.context(sid)[1]) == 2
    manager.set_scope(sid, ['b'])
    memory, recent = manager.context(sid)
    assert recent == [] and memory['summary'] == ''
    assert memory['research_goal'] == '比较方法'
    assert len(store.get(sid)['turns']) == 1
    store.append_turn(sid, 'new question', Answer('new answer', []))
    manager.clear(sid)
    assert manager.context(sid)[1] == []
    assert manager.get(sid).document_ids == ['b']


def test_summary_excludes_recent_three_and_preserves_protected_fields(tmp_path):
    store, sid, manager = setup(tmp_path)
    manager.set_scope(sid, ['a'])
    for i in range(7):
        store.append_turn(sid, f'q{i}', Answer(f'a{i}', []))
    class Client:
        def complete(self, prompt):
            data = json.loads(prompt.split('\ndata: ', 1)[1])
            assert [t['question'] for t in data['turns']] == ['q0', 'q1', 'q2', 'q3']
            return json.dumps({'summary': '讨论了方法', 'active_entities': ['AX-247'], 'open_questions': ['成本？']})
    manager.compress(sid, Client())
    memory, recent = manager.context(sid)
    assert memory['summary'] == '讨论了方法'
    assert [r['content'] for r in recent] == ['q4', 'a4', 'q5', 'a5', 'q6', 'a6']
    assert manager.get(sid).document_ids == ['a']
    restored = type(manager)(ConversationStore(store.path))
    assert restored.get(sid).summary == '讨论了方法'


def test_failed_summary_does_not_retry_same_batch(tmp_path):
    store, sid, manager = setup(tmp_path)
    manager.set_scope(sid, ['a'])
    for i in range(7): store.append_turn(sid, str(i), Answer('answer', []))
    class Broken:
        calls = 0
        def complete(self, prompt):
            self.calls += 1
            raise RuntimeError('private error')
    client = Broken()
    manager.compress(sid, client); manager.compress(sid, client)
    assert client.calls == 1
    assert manager.get(sid).summary == ''
    assert manager.get(sid).compression_status == 'failed'


def test_clear_during_compression_rejects_stale_write(tmp_path):
    store, sid, manager = setup(tmp_path)
    manager.set_scope(sid, ['a'])
    for i in range(7): store.append_turn(sid, str(i), Answer('answer', []))
    class Client:
        def complete(self, prompt):
            manager.clear(sid)
            return '{"summary":"stale","active_entities":[],"open_questions":[]}'
    manager.compress(sid, Client())
    assert manager.get(sid).summary == ''


def test_delete_session_removes_memory(tmp_path):
    store, sid, manager = setup(tmp_path)
    manager.get(sid)
    store.delete(sid)
    with store._db() as db:
        assert db.execute('SELECT count(*) FROM session_memories').fetchone()[0] == 0


def test_no_compression_at_six_turns_and_model_cannot_change_scope(tmp_path):
    store, sid, manager = setup(tmp_path)
    manager.set_scope(sid, ['a'])
    for i in range(6): store.append_turn(sid, str(i), Answer('answer', []))
    class Client:
        calls = 0
        def complete(self, prompt):
            self.calls += 1
            return '{"summary":"bad","active_entities":[],"open_questions":[],"document_ids":["b"]}'
    client = Client()
    manager.compress(sid, client)
    assert client.calls == 0
    store.append_turn(sid, '7', Answer('answer', []))
    manager.compress(sid, client)
    assert manager.get(sid).document_ids == ['a']
    assert manager.get(sid).compression_status == 'failed'
    assert manager.get(sid).summary == ''


def test_memory_routes_preserve_history_and_restore_scope(tmp_path):
    from fastapi.testclient import TestClient
    from test_streaming import app_for
    app = app_for(tmp_path)
    with TestClient(app) as client:
        sid = client.post('/api/sessions', json={'title': 'test'}).json()['id']
        base = f'/api/sessions/{sid}'
        assert client.put(base + '/memory/scope', json={'document_ids': ['a'*64]}).status_code == 200
        client.patch(base + '/memory', json={'research_goal': '对比', 'language': 'zh', 'answer_style': '简洁'}).raise_for_status()
        client.post(base + '/turns/stream', json={'question': 'question'}).raise_for_status()
        detail = client.get(base).json()
        assert detail['memory']['document_ids'] == ['a'*64]
        assert len(detail['turns']) == 1
        client.delete(base + '/memory').raise_for_status()
        assert len(client.get(base).json()['turns']) == 1
        assert client.get(base + '/memory').json()['research_goal'] == '对比'
        client.put(base + '/memory/scope', json={'document_ids': []}).raise_for_status()
        assert client.post(base + '/turns', json={'question': 'question'}).status_code == 422


def test_summary_failure_does_not_lose_seventh_answer(tmp_path):
    from fastapi.testclient import TestClient
    from test_streaming import app_for
    app = app_for(tmp_path)
    class Broken:
        def complete(self, prompt):
            raise RuntimeError('PRIVATE')
    class Service:
        client = Broken()
        def answer(self, **kwargs):
            return Answer('saved answer', [])
    app.state.answer_service = Service()
    with TestClient(app) as client:
        sid = client.post('/api/sessions', json={'title': 'test'}).json()['id']
        for i in range(7):
            result = client.post(f'/api/sessions/{sid}/turns', json={'question': str(i)})
            assert result.status_code == 201
        detail = client.get(f'/api/sessions/{sid}').json()
        assert len(detail['turns']) == 7
        assert detail['memory']['compression_status'] == 'failed'
