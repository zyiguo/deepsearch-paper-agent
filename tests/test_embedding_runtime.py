import httpx
import pytest
from fastapi.testclient import TestClient
from deepsearch_agent.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
from deepsearch_agent.llm_gateway import ModelServiceError
from deepsearch_agent.llm_config import LLMSettings
from deepsearch_agent.api import create_app


def settings(**kwargs):
    return EmbeddingSettings(backend='openai_compatible', api_key='test-only',
                             base_url='https://example.test/v1', dimensions=2, **kwargs)


def test_batch_order_and_dimensions():
    calls = []
    def handler(request):
        import json
        body = json.loads(request.content)
        calls.append(body)
        assert body['dimensions'] == 2
        return httpx.Response(200, json={'data': [
            {'index': i, 'embedding': [3, 4]} for i in reversed(range(len(body['input'])))
        ]})
    result = RemoteEmbeddingClient(settings(batch_size=2), httpx.MockTransport(handler)).embed(['a', 'b', 'c'])
    assert result == [[.6, .8]] * 3
    assert [len(c['input']) for c in calls] == [2, 1]


@pytest.mark.parametrize('vector', [[1], [0, 0], [True, 1], ['1', 2]])
def test_rejects_invalid_vectors(vector):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'data': [{'index': 0, 'embedding': vector}]}))
    with pytest.raises(ModelServiceError):
        RemoteEmbeddingClient(settings(), transport).embed(['text'])


def test_safe_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(401, text='secret-response'))
    with pytest.raises(ModelServiceError, match='认证失败') as caught:
        RemoteEmbeddingClient(settings(), transport).embed(['text'])
    assert 'secret-response' not in str(caught.value)


def test_index_isolation_free_question_and_restore(tmp_path, monkeypatch):
    # No .env or network reads: inject both providers explicitly.
    monkeypatch.setattr(RemoteEmbeddingClient, 'embed', lambda self, texts: [[1., 0.] for _ in texts])
    original = tmp_path / 'demo.json'
    original.write_text('old snapshot stays untouched', encoding='utf-8')
    args = dict(index_path=original, llm_settings=LLMSettings(), embedding_settings=settings())
    with TestClient(create_app(**args)) as client:
        assert client.post('/api/demo/ingest').status_code == 200
        assert client.post('/api/ask', json={'question': '自由输入的新问题'}).status_code == 200
        session = client.post('/api/sessions', json={'title': '真实向量测试'}).json()
        assert client.post(f"/api/sessions/{session['id']}/turns", json={'question': '另一个新问题'}).status_code == 201
    assert original.read_text(encoding='utf-8') == 'old snapshot stays untouched'
    assert len(list(tmp_path.glob('embedding-*.json'))) == 1
    with TestClient(create_app(**args)) as client:
        assert client.get('/api/knowledge-base/status').json()['chunk_count'] == 2


def test_identity_changes_with_model_not_credentials():
    assert settings().identity != settings(model='other').identity
    assert 'test-only' not in repr(settings())
