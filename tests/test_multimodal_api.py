import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from deepsearch_agent.api import create_app
from deepsearch_agent.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
from deepsearch_agent.llm_config import LLMSettings
from deepsearch_agent.llm_gateway import ModelServiceError, OpenAICompatibleClient
from deepsearch_agent.vision_config import VisionSettings


def pdf(scanned=True):
    stream = io.BytesIO()
    writer = canvas.Canvas(stream)
    if scanned:
        image = Image.new('RGB', (500, 700), 'white')
        ImageDraw.Draw(image).text((30, 30), 'Table 1: voltage = 42 V', fill='black')
        writer.drawImage(ImageReader(image), 0, 0, width=500, height=700)
    else:
        writer.drawString(30, 700, 'The experiment measures voltage.')
    writer.showPage()
    writer.save()
    return stream.getvalue()


def app(tmp_path, monkeypatch, *, enabled=True):
    monkeypatch.setattr(RemoteEmbeddingClient, 'embed', lambda self, texts: [[1., 0.] for _ in texts])
    return create_app(index_path=tmp_path / 'index.json', llm_settings=LLMSettings(),
        embedding_settings=EmbeddingSettings(backend='openai_compatible', api_key='test',
                                            base_url='https://example.test/v1', dimensions=2),
        vision_settings=VisionSettings(backend='openai_compatible', llm=LLMSettings(
            backend='openai_compatible', api_key='test', base_url='https://example.test/v1',
            model='vision-test', max_output_tokens=8192, context_window=32768)) if enabled else VisionSettings())


def upload(client, content):
    response = client.post('/api/documents/upload?filename=scan.pdf', content=content,
                           headers={'Content-Type': 'application/pdf'})
    assert response.status_code == 202, response.text
    return response.json()['id']


def finished(client, ident):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        document = client.get(f'/api/documents/{ident}').json()
        if document['status'] not in {'queued', 'parsing', 'analyzing', 'embedding'}:
            return document
        time.sleep(.02)
    pytest.fail('PDF processing timeout')


def fake_images(self, prompt, images, *, structured=False):
    assert images and all(path.read_bytes().startswith(b'\x89PNG') for path in images)
    if prompt.startswith('读取这张论文'):
        return json.dumps({'ocr_text': 'Voltage is 42 volts.' if '本页需要 OCR' in prompt else '',
                           'items': [{'kind': 'table', 'label': 'Table 1', 'description': 'Voltage is 42 V.'}],
                           'warnings': []})
    return 'The voltage is 42 V. [1]'


def test_scanned_upload_answer_and_restart_preserve_visual_citations(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAICompatibleClient, 'complete_images', fake_images)
    application = app(tmp_path, monkeypatch)
    with TestClient(application) as client:
        assert client.get('/api/health').json()['vision_enabled']
        ident = upload(client, pdf())
        document = finished(client, ident)
        assert document['status'] == 'ready', document
        assert document['analysis_mode'] == 'vision'
        assert client.get(f'/api/documents/{ident}/pages/1').json()['text'] == 'Voltage is 42 volts.'
        answer = client.post('/api/ask', json={'question': 'Voltage', 'document_ids': [ident]}).json()
        assert answer['text'] == 'The voltage is 42 V. [1]'
        assert answer['sources'][0]['visual_pages'] == [1]
        assert {s['evidence_kind'] for s in answer['sources']} == {'ocr', 'table'}
        sid = client.post('/api/sessions', json={'title': 'Scan'}).json()['id']
        response = client.post(f'/api/sessions/{sid}/turns', json={'question': 'Voltage', 'document_ids': [ident]})
        assert response.status_code == 201, response.text
    with TestClient(app(tmp_path, monkeypatch, enabled=False)) as client:
        assert client.get(f'/api/documents/{ident}').json()['analysis_mode'] == 'vision'
        saved = client.get(f'/api/sessions/{sid}').json()['turns'][0]['answer']['sources']
        assert saved[0]['visual_pages'] == [1]
        response = client.post('/api/ask', json={'question': 'Voltage', 'document_ids': [ident]})
        assert response.status_code == 502 and '视觉模型' in response.json()['detail']


def test_upgrading_existing_document_replaces_index_without_duplicates(tmp_path, monkeypatch):
    content = pdf(scanned=False)
    with TestClient(app(tmp_path, monkeypatch, enabled=False)) as client:
        ident = upload(client, content)
        assert finished(client, ident)['chunk_count'] == 1
        assert client.post(f'/api/documents/{ident}/analyze').status_code == 409
    calls = []
    def counting(self, prompt, images, *, structured=False):
        calls.append(prompt)
        return fake_images(self, prompt, images, structured=structured)
    monkeypatch.setattr(OpenAICompatibleClient, 'complete_images', counting)
    with TestClient(app(tmp_path, monkeypatch)) as client:
        assert client.post(f'/api/documents/{ident}/analyze').status_code == 202
        upgraded = finished(client, ident)
        assert upgraded['status'] == 'ready' and upgraded['chunk_count'] == 2
        assert client.post(f'/api/documents/{ident}/analyze').status_code == 202
        assert finished(client, ident)['chunk_count'] == 2
        assert len(calls) == 1
        assert client.get(f'/api/documents/{ident}/pages/1').json()['text'].strip() == 'The experiment measures voltage.'


def test_vision_failure_retains_previous_index_and_retry_can_recover(tmp_path, monkeypatch):
    with TestClient(app(tmp_path, monkeypatch, enabled=False)) as client:
        ident = upload(client, pdf(scanned=False))
        assert finished(client, ident)['status'] == 'ready'
    def fail(self, *args, **kwargs):
        raise ModelServiceError('Vision unavailable')
    monkeypatch.setattr(OpenAICompatibleClient, 'complete_images', fail)
    application = app(tmp_path, monkeypatch)
    with TestClient(application) as client:
        client.post(f'/api/documents/{ident}/analyze').raise_for_status()
        assert finished(client, ident)['status'] == 'failed'
        assert len(application.state.kb.chunks) == 1
        assert application.state.kb.chunks[0].evidence_kind == 'text'
        monkeypatch.setattr(OpenAICompatibleClient, 'complete_images', fake_images)
        client.post(f'/api/documents/{ident}/retry').raise_for_status()
        assert finished(client, ident)['status'] == 'ready'
        assert len(application.state.kb.chunks) == 2
