import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from deepsearch_agent.workspace.image_assets import normalize_image
from deepsearch_agent.adapters.image_embeddings import SiliconFlowImageEmbeddingClient
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from test_image_retrieval_api import app, fake_vision
from test_multimodal_api import finished


def raster(format='PNG', size=(80, 60)):
    stream = io.BytesIO()
    Image.new('RGB', size, 'blue').save(stream, format=format)
    return stream.getvalue()


@pytest.mark.parametrize('format', ['PNG', 'JPEG', 'WEBP'])
def test_supported_images_normalized_to_png(format):
    data, width, height = normalize_image(raster(format))
    assert data.startswith(b'\x89PNG') and (width, height) == (80, 60)


@pytest.mark.parametrize('content', [b'', b'%PDF-test', b'<svg/>', raster('GIF')])
def test_non_raster_or_disallowed_formats_rejected(content):
    with pytest.raises(ValueError):
        normalize_image(content)


def test_image_pixel_limit_orientation_and_metadata(monkeypatch):
    import deepsearch_agent.workspace.image_assets as assets
    image = Image.new('RGB', (80, 60), 'blue')
    exif = image.getexif()
    exif[274] = 6
    exif[270] = 'Private metadata'
    stream = io.BytesIO()
    image.save(stream, format='JPEG', exif=exif)
    normalized, width, height = normalize_image(stream.getvalue())
    assert (width, height) == (60, 80)
    assert not Image.open(io.BytesIO(normalized)).getexif()
    monkeypatch.setattr(assets, 'MAX_IMAGE_PIXELS', 100)
    with pytest.raises(ValueError):
        normalize_image(raster())


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAICompatibleClient, 'complete_images', fake_vision)
    monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_images', lambda self, images: [[0., 1.] for _ in images])
    monkeypatch.setattr(SiliconFlowImageEmbeddingClient, 'embed_query', lambda self, text: [0., 1.])
    return app(tmp_path, monkeypatch)


def attach(client, session, content=None):
    response = client.post(f'/api/sessions/{session}/attachments?filename=chart.png',
                           content=content or raster(), headers={'Content-Type': 'image/png'})
    assert response.status_code == 201, response.text
    return response.json()


def test_image_library_upload_retrieval_duplicate_and_restart(tmp_path, monkeypatch):
    application = setup(tmp_path, monkeypatch)
    with TestClient(application) as client:
        kwargs = dict(content=raster(), headers={'Content-Type': 'image/png'})
        response = client.post('/api/documents/upload-image?filename=chart.png', **kwargs)
        assert response.status_code == 202, response.text
        ident = response.json()['id']
        doc = finished(client, ident)
        assert doc['status'] == 'ready' and doc['image_indexed_pages'] == 1, doc
        assert doc['source_path'].startswith('image://')
        assert doc['media_type'] == 'image/png'
        assert client.get(f'/api/documents/{ident}/file').headers['content-type'] == 'image/png'
        assert client.get(f'/api/documents/{ident}/pages/1/image').status_code == 200
        assert client.post('/api/documents/upload-image?filename=chart.png', **kwargs).json()['id'] == ident
        assert len(application.state.kb.image_index.chunks) == 1
        answer = client.post('/api/ask', json={'question': 'chart', 'document_ids': [ident]})
        assert answer.status_code == 200, answer.text
        assert answer.json()['sources'][0]['source_path'] == doc['source_path']
    with TestClient(setup(tmp_path, monkeypatch)) as client:
        assert client.get(f'/api/documents/{ident}').json()['status'] == 'ready'


def test_attachment_only_question_persistence_isolation_and_promotion(tmp_path, monkeypatch):
    application = setup(tmp_path, monkeypatch)
    with TestClient(application) as client:
        session = client.post('/api/sessions', json={'title': 'Images'}).json()['id']
        other = client.post('/api/sessions', json={'title': 'Other'}).json()['id']
        image = attach(client, session)
        assert client.get(image['image_url']).status_code == 200
        assert not application.state.kb.image_index.chunks
        body = {'question': 'Describe this plot', 'document_ids': [], 'attachment_ids': [image['id']]}
        answer = client.post(f'/api/sessions/{session}/turns', json=body)
        assert answer.status_code == 201, answer.text
        assert answer.json()['attachments'] == [image]
        assert answer.json()['answer']['sources'][0]['source_path'] == image['source_path']
        assert client.post(f'/api/sessions/{other}/turns', json=body).status_code == 404
        assert client.post(f'/api/sessions/{other}/turns/stream', json=body).status_code == 404
        assert client.get(image['image_url'].replace(session, other)).status_code == 404
        stream = client.post(f'/api/sessions/{session}/turns/stream', json=body)
        assert stream.status_code == 200 and 'event: done' in stream.text
        assert 'attachment://' in stream.text
        promoted = client.post(f'/api/sessions/{session}/attachments/{image["id"]}/index')
        assert promoted.status_code == 202, promoted.text
        assert finished(client, promoted.json()['id'])['status'] == 'ready'
    with TestClient(setup(tmp_path, monkeypatch)) as client:
        detail = client.get(f'/api/sessions/{session}').json()
        assert detail['turns'][0]['attachments'] == [image]
        assert client.get(image['image_url']).status_code == 200
        assert client.delete(f'/api/sessions/{session}').status_code == 200
        assert client.get(image['image_url']).status_code == 404
        assert not (tmp_path / 'documents' / 'attachments' / session).exists()
        assert client.get(f'/api/documents/{promoted.json()["id"]}/file').status_code == 200


def test_attachment_limits_invalid_files_and_preflight(tmp_path, monkeypatch):
    with TestClient(setup(tmp_path, monkeypatch)) as client:
        session = client.post('/api/sessions', json={'title': 'Images'}).json()['id']
        base = f'/api/sessions/{session}'
        assert client.post(base + '/attachments?filename=bad.png', content=b'not an image',
                           headers={'Content-Type': 'image/png'}).status_code == 422
        assert client.post(base + '/attachments?filename=bad.svg', content=b'<svg/>',
                           headers={'Content-Type': 'image/svg+xml'}).status_code == 415
        assert client.post(base + '/attachments?filename=chart.png', content=raster(),
                           headers={'Content-Type': 'image/png', 'Origin': 'https://untrusted.test'}).status_code == 403
        assert client.post(base + '/turns', json={'question': 'q', 'document_ids': []}).status_code == 422
        assert client.post(base + '/turns/stream', json={'question': 'q', 'attachment_ids': ['a'] * 5}).status_code == 422
        assert client.post(base + '/turns/stream', json={'question': 'q', 'attachment_ids': ['../bad']}).status_code == 404


def test_attachment_drives_visual_search_queries_and_combines_scoped_evidence():
    from deepsearch_agent.core.evidence import TextChunk
    from deepsearch_experiments.legacy.qa import AnswerService
    from test_visual_research import TextClient
    calls = []
    class Library:
        def restrict_sources(self, sources):
            assert sources == {'paper.pdf'}
            return self
        def search(self, query, top_k=5):
            assert query == 'blue curve calibration'
            return [TextChunk('Calibration is 12 seconds.', 'paper.pdf', 2, 0)]
    class Vision:
        def complete(self, prompt, evidence, *, structured=True):
            calls.append(prompt)
            assert evidence[0]['source'].startswith('attachment://')
            if prompt.startswith('你是 Researcher。'):
                return json.dumps({'queries': ['blue curve calibration']})
            assert len(evidence) == 2
            if prompt.startswith('你是 Answerer。'):
                return json.dumps({'claims': [{'text': 'Calibration is 12 seconds.', 'citations': [1, 2]}]})
            return json.dumps({'supported': [0], 'queries': []})
    image = TextChunk('User image', 'attachment://session/image/chart.png', 1, 0,
                      evidence_kind='image', visual_pages=[1])
    result = AnswerService(TextClient(), Library(), use_graph=True, visual_evidence=Vision()).answer(
        'How does this chart relate?', allowed_sources={'paper.pdf'}, input_evidence=[image])
    assert len(calls) == 3 and len(result.sources) == 2
