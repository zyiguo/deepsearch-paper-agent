import json
from dataclasses import asdict

import httpx
import pytest

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.demo import build_demo_knowledge_base
from deepsearch_agent.image_embeddings import ImageEmbeddingSettings, SiliconFlowImageEmbeddingClient
from deepsearch_agent.image_index import ImageVectorIndex
from deepsearch_agent.llm_gateway import ModelServiceError


class Images:
    def __init__(self):
        self.queries = []

    def embed_images(self, images):
        return [[1., 0.], [0., 1.]][:len(images)]

    def embed_query(self, text):
        self.queries.append(text)
        return [0., 1.]


def index():
    client = Images()
    chunks = [TextChunk(text='Raw page; no caption.', source_path=source, page_number=1,
                        chunk_index=0, evidence_kind='image', visual_pages=[1])
              for source in ['a.pdf', 'b.pdf']]
    return ImageVectorIndex.restore({'identity': 'vl-test', 'entries': [
        {'chunk': asdict(c), 'vector': v} for c, v in zip(chunks, [[1., 0.], [0., 1.]])]}, client)


def test_raw_images_retrievable_without_ocr_or_text_and_scoped_before_search():
    kb = build_demo_knowledge_base()
    kb.image_index = index()
    assert kb.search('rising blue curve', 1)[0].source_path == 'b.pdf'
    assert kb.image_index.client.queries == ['rising blue curve']
    scoped = kb.restrict_sources({'a.pdf'})
    assert scoped.search('rising blue curve', 1)[0].source_path == 'a.pdf'
    assert len(kb.image_index.chunks) == 2
    before = len(kb.image_index.client.queries)
    assert kb.restrict_sources(set()).search('curve') == []
    assert len(kb.image_index.client.queries) == before


def test_image_snapshot_roundtrip_and_failed_load_is_atomic(tmp_path):
    kb = build_demo_knowledge_base()
    kb.image_index = index()
    path = tmp_path / 'index.json'
    kb.save(path)
    restored = build_demo_knowledge_base()
    restored.image_index = ImageVectorIndex('vl-test', Images())
    restored.load(path)
    assert restored.search('curve', 1)[0].source_path == 'b.pdf'
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['image_index']['entries'][1]['vector'] = [1., 0., 0.]
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError):
        restored.load(path)
    assert restored.search('curve', 1)[0].source_path == 'b.pdf'
    restored.image_index.identity = 'other-model'
    with pytest.raises(ValueError, match='identity'):
        restored.load(path)


def test_cloud_images_and_queries_share_model_but_not_text_embeddings(tmp_path):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['model'] == 'Qwen/Qwen3-VL-Embedding-8B'
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [3., 4.]}]})
    client = SiliconFlowImageEmbeddingClient(ImageEmbeddingSettings(
        backend='siliconflow', api_key='secret', base_url='https://example.test/v1', dimensions=2),
        transport=httpx.MockTransport(handler))
    image = tmp_path / 'page.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\nimage')
    assert client.embed_images([image]) == [[.6, .8]]
    assert client.embed_query('curve') == [.6, .8]
    assert requests[0]['input'][0]['image'].startswith('data:image/png;base64,')
    assert requests[1]['input'] == [{'text': 'curve'}]
    assert 'secret' not in repr(client.settings)


@pytest.mark.parametrize('vector', [[True, 1.], [float('nan'), 1.], [0., 0.], [1.]])
def test_invalid_cloud_vectors_rejected(vector):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(
        {'data': [{'index': 0, 'embedding': vector}]}).encode(), headers={'content-type': 'application/json'}))
    client = SiliconFlowImageEmbeddingClient(ImageEmbeddingSettings(
        backend='siliconflow', api_key='test', base_url='https://example.test/v1', dimensions=2), transport)
    with pytest.raises(ModelServiceError):
        client.embed_query('curve')


def test_image_reranking_uses_original_images_not_placeholder_text():
    from deepsearch_agent.reranker import LLMReranker
    class TextModel:
        def complete(self, prompt):
            pytest.fail('Raw images must not be ranked by a text-only model')
    class Visual:
        def complete(self, prompt, evidence, *, structured=True):
            assert [item['visual_pages'] for item in evidence] == [[1], [1]]
            assert [item['source'] for item in evidence] == ['a.pdf', 'b.pdf']
            assert '[2] source=b.pdf page=1' in prompt
            return '{"ranked_ids": [2, 1]}'
    reranker = LLMReranker(TextModel(), visual_evidence=Visual())
    assert reranker.rerank('curve', index().chunks, 1)[0].source_path == 'b.pdf'


def test_config_reuses_injected_credentials_without_coupling_models(tmp_path):
    from deepsearch_agent.embedding_runtime import EmbeddingSettings
    path = tmp_path / '.env.image_embedding'
    path.write_text('IMAGE_EMBEDDING_BACKEND=siliconflow\nIMAGE_EMBEDDING_USE_EMBEDDING_CREDENTIALS=true\n', encoding='utf-8')
    shared = EmbeddingSettings(backend='openai_compatible', api_key='test-key',
        base_url='https://example.test/v1', model='BAAI/bge-m3')
    settings = ImageEmbeddingSettings.load(path, shared_embeddings=shared, environ={})
    assert settings.api_key == shared.api_key and settings.base_url == shared.base_url
    assert settings.model == 'Qwen/Qwen3-VL-Embedding-8B' and settings.model != shared.model
    with pytest.raises(ValueError):
        ImageEmbeddingSettings.load(path, environ={})


def test_deepsearch_answers_and_verifies_raw_image_hits():
    from deepsearch_agent.qa import AnswerService
    from test_visual_research import TextClient
    calls = []
    class Visual:
        def complete(self, prompt, evidence, *, structured=True):
            calls.append(prompt)
            assert all(e['evidence_kind'] == 'image' for e in evidence)
            if prompt.startswith('你是 Answerer。'):
                return json.dumps({'claims': [{'text': 'A rising curve', 'citations': [1]}]})
            return json.dumps({'supported': [0], 'queries': []})
    kb = build_demo_knowledge_base()
    kb.image_index = index()
    result = AnswerService(TextClient(), kb, use_graph=True, visual_evidence=Visual()).answer('curve', 1)
    assert len(calls) == 2 and result.sources[0].source_path == 'b.pdf'
    assert result.sources[0].evidence_kind == 'image'


def test_visual_reranker_bounds_page_pool_before_calling_provider():
    from deepsearch_agent.reranker import LLMReranker
    from deepsearch_agent.vision_config import VisionSettings
    from deepsearch_agent.llm_config import LLMSettings
    class TextModel:
        def complete(self, prompt):
            pytest.fail('Wrong reranker model')
    class Visual:
        settings = VisionSettings(backend='openai_compatible', max_images=2,
            llm=LLMSettings(backend='openai_compatible', api_key='test', base_url='https://example.test/v1',
                            model='vision', context_window=32768, max_output_tokens=8192))
        def complete(self, prompt, evidence, *, structured=True):
            assert len(evidence) == 2
            return '{"ranked_ids": [2, 1]}'
    chunks = [TextChunk('Page image', 'a.pdf', i, i - 1, evidence_kind='image', visual_pages=[i])
              for i in range(1, 6)]
    result = LLMReranker(TextModel(), visual_evidence=Visual()).rerank('curve', chunks, 1)
    assert result[0].page_number == 2
