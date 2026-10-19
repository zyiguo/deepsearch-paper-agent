from fastapi.testclient import TestClient
from deepsearch_agent.api import create_app
from deepsearch_agent.adapters.llm_config import LLMSettings


def test_delete_session_removes_turns_and_preserves_other_data(tmp_path):
    path = tmp_path/'index.json'
    with TestClient(create_app(index_path=path, llm_settings=LLMSettings())) as client:
        client.post('/api/demo/ingest')
        first = client.post('/api/sessions', json={'title':'删除目标'}).json()['id']
        other = client.post('/api/sessions', json={'title':'保留'}).json()['id']
        client.post(f'/api/sessions/{first}/turns', json={'question':'模型怎么训练？'})
        response = client.delete(f'/api/sessions/{first}')
        assert response.status_code == 200
        assert client.get(f'/api/sessions/{first}').status_code == 404
        assert client.get(f'/api/sessions/{other}').status_code == 200
        assert len(client.get('/api/documents').json()) == 1
        assert client.delete(f'/api/sessions/{first}').status_code == 404
    with TestClient(create_app(index_path=path, llm_settings=LLMSettings())) as client:
        assert [s['id'] for s in client.get('/api/sessions').json()] == [other]
