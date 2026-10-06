import json

from fastapi.testclient import TestClient

from deepsearch_agent.api import create_app
from deepsearch_agent.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
from deepsearch_agent.image_embeddings import ImageEmbeddingSettings, SiliconFlowImageEmbeddingClient
from deepsearch_agent.llm_config import LLMSettings
from deepsearch_agent.llm_gateway import ModelServiceError, OpenAICompatibleClient
from deepsearch_agent.vision_config import VisionSettings
from test_multimodal_api import pdf, finished, upload


def app(tmp_path, monkeypatch, *, images=True, vision=True):
    monkeypatch.setattr(RemoteEmbeddingClient, 'embed', lambda self, texts: [[1., 0.] for _ in texts])
    return create_app(index_path=tmp_path / 'index.json', llm_settings=LLMSettings(),
        embedding_settings=EmbeddingSettings(backend='openai_compatible', api_key='test',
                                            base_url='https://example.test/v1', dimensions=2),
        image_embedding_settings=ImageEmbeddingSettings(backend='siliconflow', api_key='test',
            base_url='https://example.test/v1', dimensions=2) if images else ImageEmbeddingSettings(),
        vision_settings=VisionSettings(backend='openai_compatible', llm=LLMSettings(
            backend='openai_compatible', api_key='test', base_url='https://example.test/v1',
            model='vision', max_output_tokens=8192, context_window=32768)) if vision else VisionSettings())


def fake_vision(self, prompt, images, *, structured=False):
    assert images
    if prompt.startswith('读取这张论文'):
        return json.dumps({'ocr_text': '', 'items': [], 'warnings': []})
    return 'The plotted voltage is 42 V. [1]'


def test_existing_document_image_backfill_answer_restart_and_failure_atomicity(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAICompatibleClient, 'complete_images', fake_vision)
    monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_images', lambda self, images: [[0., 1.] for _ in images])
    monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_query', lambda self, text: [0., 1.])
    with TestClient(app(tmp_path, monkeypatch, images=False, vision=False)) as client:
        ident = upload(client, pdf(scanned=False))
        assert finished(client, ident)['status'] == 'ready'
        assert client.post(f'/api/documents/{ident}/index-images').status_code == 409
    application = app(tmp_path, monkeypatch)
    with TestClient(application) as client:
        assert client.get('/api/health').json()['image_retrieval_enabled']
        assert client.post(f'/api/documents/{ident}/index-images').status_code == 202
        record = finished(client, ident)
        assert record['status'] == 'ready' and record['image_indexed_pages'] == 1
        assert record['chunk_count'] == 1 and record['analysis_mode'] == 'text'
        before = application.state.index_path.read_bytes()
        assert client.post(f'/api/documents/{ident}/index-images').status_code == 202
        assert finished(client, ident)['image_indexed_pages'] == 1
        answer = client.post('/api/ask', json={'question': 'plotted voltage', 'document_ids': [ident], 'top_k': 2})
        assert answer.status_code == 200, answer.text
        assert any(s['evidence_kind'] == 'image' for s in answer.json()['sources'])
        def fail(self, images):
            raise ModelServiceError('Image provider unavailable')
        monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_images', fail)
        client.post(f'/api/documents/{ident}/analyze').raise_for_status()
        assert finished(client, ident)['status'] == 'failed'
        assert application.state.index_path.read_bytes() == before
        assert len(application.state.kb.image_index.chunks) == 1
    with TestClient(app(tmp_path, monkeypatch)) as client:
        assert client.get(f'/api/documents/{ident}').json()['image_indexed_pages'] == 1


def test_new_upload_indexes_images_without_visual_descriptions(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAICompatibleClient, 'complete_images', fake_vision)
    monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_images', lambda self, images: [[0., 1.] for _ in images])
    monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_query', lambda self, text: [0., 1.])
    with TestClient(app(tmp_path, monkeypatch)) as client:
        ident = upload(client, pdf(scanned=False))
        record = finished(client, ident)
        assert record['status'] == 'ready' and record['image_indexed_pages'] == 1
        assert record['chunk_count'] == 1


def test_scanned_pdf_can_be_indexed_and_queried_without_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_images', lambda self, images: [[0., 1.] for _ in images])
    monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_query', lambda self, text: [0., 1.])
    with TestClient(app(tmp_path, monkeypatch, vision=False)) as client:
        ident = upload(client, pdf(scanned=True))
        record = finished(client, ident)
        assert record['status'] == 'ready', record
        assert record['chunk_count'] == 0 and record['image_indexed_pages'] == 1
        assert client.get(f'/api/documents/{ident}/pages/1').json()['text'] == ''
    monkeypatch.setattr(OpenAICompatibleClient, 'complete_images', fake_vision)
    with TestClient(app(tmp_path, monkeypatch)) as client:
        answer = client.post('/api/ask', json={'question': 'voltage', 'document_ids': [ident]})
        assert answer.status_code == 200, answer.text
        assert answer.json()['sources'][0]['evidence_kind'] == 'image'


def test_image_only_scanned_pdf_does_not_require_generated_descriptions(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAICompatibleClient, 'complete_images', fake_vision)
    monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_images', lambda self, images: [[0., 1.] for _ in images])
    with TestClient(app(tmp_path, monkeypatch)) as client:
        ident = upload(client, pdf(scanned=True))
        record = finished(client, ident)
        assert record['status'] == 'ready', record
        assert record['chunk_count'] == 0 and record['image_indexed_pages'] == 1
