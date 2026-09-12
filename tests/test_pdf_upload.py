import io
import time

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from deepsearch_agent.api import create_app
from deepsearch_agent.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
from deepsearch_agent.llm_config import LLMSettings
from deepsearch_agent.llm_gateway import ModelServiceError


def pdf_bytes(blank=False):
    stream = io.BytesIO()
    writer = canvas.Canvas(stream)
    for text in ['The lighthouse flashes white every twelve seconds.',
                 'The library opens Wednesday and Saturday from two until six.']:
        if not blank:
            writer.drawString(50, 700, text)
        writer.showPage()
    writer.save()
    return stream.getvalue()


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Only replace the paid external dependency, keep parser/storage/index real.
    monkeypatch.setattr(RemoteEmbeddingClient, 'embed', lambda self, texts: [[1., 0.] for _ in texts])
    return create_app(index_path=tmp_path / 'index.json', llm_settings=LLMSettings(),
                      embedding_settings=EmbeddingSettings(backend='openai_compatible',
                        api_key='test', base_url='https://example.test/v1', dimensions=2))


def upload(client, content, filename='story.pdf'):
    return client.post('/api/documents/upload', params={'filename': filename},
                       content=content, headers={'Content-Type': 'application/pdf'})


def finished(client, ident):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get(f'/api/documents/{ident}')
        assert response.status_code == 200
        state = response.json()
        if state['status'] not in {'queued', 'parsing', 'embedding'}:
            return state
        time.sleep(.05)
    pytest.fail('PDF job did not finish')


def test_upload_indexes_pages_deduplicates_and_survives_restart(app):
    content = pdf_bytes()
    with TestClient(app) as client:
        response = upload(client, content, '../../original.pdf')
        assert response.status_code == 202
        ident = response.json()['id']
        doc = finished(client, ident)
        assert doc['status'] == 'ready', doc
        assert doc['page_count'] == 2
        assert doc['title'] == 'original.pdf'
        assert doc['chunk_count'] == 2
        again = upload(client, content)
        assert again.json()['id'] == ident
        assert finished(client, ident)['chunk_count'] == 2
        answer = client.post('/api/ask', json={'question': 'What color is the lighthouse?', 'top_k': 1}).json()
        assert answer['sources'][0]['source_path'].startswith(f'pdf://{ident}/')
        page = client.get(f'/api/documents/{ident}/pages/2')
        assert page.status_code == 200
        assert 'Wednesday' in page.json()['text']
        image = client.get(f'/api/documents/{ident}/pages/2/image')
        assert image.status_code == 200
        assert image.content.startswith(b'\x89PNG')
        assert client.get(f'/api/documents/{ident}/pages/3').status_code == 404
    with TestClient(app) as client:
        assert client.get(f'/api/documents/{ident}').json()['status'] == 'ready'
        assert len(client.get('/api/documents').json()) == 1


@pytest.mark.parametrize('payload,name,status', [(b'not pdf', 'x.pdf', 415), (b'%PDF-fake', 'x.exe', 415), (b'', 'x.pdf', 400)])
def test_rejects_bad_upload_without_creating_document(app, payload, name, status):
    with TestClient(app) as client:
        assert upload(client, payload, name).status_code == status
        assert client.get('/api/documents').json() == []


def test_blank_pdf_fails_without_polluting_index(app):
    with TestClient(app) as client:
        response = upload(client, pdf_bytes(blank=True))
        assert response.status_code == 202
        doc = finished(client, response.json()['id'])
        assert doc['status'] == 'failed'
        assert 'OCR' in doc['error']
        assert client.get('/api/knowledge-base/status').json()['chunk_count'] == 0


def test_embedding_failure_keeps_old_index_and_can_retry(app, monkeypatch):
    with TestClient(app) as client:
        client.post('/api/demo/ingest')
        def fail(self, texts):
            raise ModelServiceError('Embedding unavailable')
        monkeypatch.setattr(RemoteEmbeddingClient, 'embed', fail)
        response = upload(client, pdf_bytes())
        ident = response.json()['id']
        assert finished(client, ident)['status'] == 'failed'
        assert client.get('/api/knowledge-base/status').json()['chunk_count'] == 2
        monkeypatch.setattr(RemoteEmbeddingClient, 'embed', lambda self, texts: [[1., 0.] for _ in texts])
        assert client.post(f'/api/documents/{ident}/retry').status_code == 202
        assert finished(client, ident)['status'] == 'ready'
        assert client.get('/api/knowledge-base/status').json()['chunk_count'] == 4


def test_upload_limits_and_foreign_origin(app):
    with TestClient(app) as client:
        assert client.post('/api/documents/upload?filename=a.pdf', content=b'%PDF-x',
                           headers={'Content-Type': 'application/pdf', 'Content-Length': str(11 * 1024 * 1024)}).status_code == 413
        assert client.post('/api/documents/upload?filename=a.pdf', content=pdf_bytes(),
                           headers={'Content-Type': 'application/pdf', 'Origin': 'https://untrusted.example'}).status_code == 403


def test_demo_mode_rejects_real_upload(tmp_path):
    with TestClient(create_app(index_path=tmp_path/'demo.json', llm_settings=LLMSettings())) as client:
        assert upload(client, pdf_bytes()).status_code == 409


def test_uploaded_sections_keep_cross_page_citations_after_restart(app):
    stream = io.BytesIO()
    writer = canvas.Canvas(stream)
    writer.drawString(50, 720, '2 Methods')
    writer.drawString(50, 700, 'The experiment uses')
    writer.showPage()
    writer.drawString(50, 720, 'two GPUs and contrastive learning.')
    writer.drawString(50, 680, '3 Results')
    writer.drawString(50, 660, 'Accuracy increases.')
    writer.showPage()
    writer.save()
    with TestClient(app) as client:
        ident = upload(client, stream.getvalue(), 'sections.pdf').json()['id']
        assert finished(client, ident)['status'] == 'ready'
        assert len(app.state.kb.chunks) == 2
        assert app.state.kb.chunks[0].chunking['strategy'] == 'section-rules-v1'
        assert app.state.kb.chunks[0].page_end == 2
    with TestClient(app) as client:
        answer = client.post('/api/ask', json={'question':'GPUs contrastive learning', 'top_k':1}).json()
        source = answer['sources'][0]
        assert source['section_path'] == ['2 Methods']
        assert source['page_number'] == 1 and source['page_end'] == 2
        assert source['pages'][1]['page_number'] == 2
        assert 'two GPUs' in source['pages'][1]['text']
        assert '3 Results' not in source['text']
        sid = client.post('/api/sessions', json={'title':'章节引用'}).json()['id']
        client.post(f'/api/sessions/{sid}/turns', json={'question':'GPUs contrastive learning', 'top_k':1}).raise_for_status()
        saved = client.get(f'/api/sessions/{sid}').json()['turns'][0]['answer']['sources'][0]
        assert saved['pages'] == source['pages']
