import json

import httpx
import pytest
from PIL import Image

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_agent.adapters.vision import VisionReader
from deepsearch_agent.adapters.vision_config import VisionSettings
from deepsearch_agent.adapters.visual_evidence import VisualEvidence


def settings(**kwargs):
    return VisionSettings(backend='openai_compatible', llm=LLMSettings(backend='openai_compatible',
                          api_key='secret-test', base_url='https://example.test/v1', model='vision-model',
                          context_window=32768, max_output_tokens=8192), **kwargs)


def test_config_is_independent_from_main_model_and_hides_credentials(tmp_path):
    loaded = VisionSettings.load(tmp_path / '.env.vision', environ={
        'LLM_API_KEY': 'wrong-key', 'VISION_BACKEND': 'openai_compatible', 'VISION_API_KEY': 'right-key',
        'VISION_BASE_URL': 'https://example.test/v1', 'VISION_MODEL': 'vision-model',
    })
    assert loaded.llm.api_key == 'right-key'
    assert 'right-key' not in repr(loaded)
    assert VisionSettings.load(tmp_path / '.env.vision', environ={}).backend == 'disabled'


def test_adapter_sends_real_png_and_parses_page_schema(tmp_path):
    image = tmp_path / 'page.png'
    Image.new('RGB', (40, 60), 'white').save(image)
    def server(request):
        data = json.loads(request.content)
        parts = data['messages'][0]['content']
        assert '逐字转录全文' in parts[0]['text']
        assert parts[1]['image_url']['url'].startswith('data:image/png;base64,iVBOR')
        assert parts[1]['image_url']['detail'] == 'high'
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'ocr_text': '42 V', 'items': [{'kind': 'table', 'label': 'Table 1', 'description': 'Voltage 42 V'}],
            'warnings': []})}}]})
    client = OpenAICompatibleClient(settings().llm, transport=httpx.MockTransport(server))
    try:
        result = VisionReader(client, settings()).analyze(image, '')
        assert result.ocr_text == '42 V' and result.items[0].kind == 'table'
    finally:
        client.close()


def test_reuses_injected_credentials_without_inheriting_text_model_limits(tmp_path):
    main = LLMSettings(backend='openai_compatible', api_key='shared-secret',
                       base_url='https://example.test/v1', model='mimo-v2.5-pro')
    loaded = VisionSettings.load(tmp_path / '.env.vision', shared_llm=main, environ={
        'VISION_BACKEND': 'openai_compatible', 'VISION_USE_LLM_CREDENTIALS': 'true',
        'VISION_MODEL': 'mimo-v2.5',
    })
    assert loaded.llm.api_key == main.api_key and loaded.llm.base_url == main.base_url
    assert loaded.llm.model == 'mimo-v2.5'
    assert loaded.llm.max_output_tokens == 8192 and loaded.llm.context_window == 32768
    assert 'shared-secret' not in repr(loaded)
    with pytest.raises(ValueError, match='视觉配置无效'):
        VisionSettings.load(tmp_path / '.env.vision', environ={
            'VISION_BACKEND': 'openai_compatible', 'VISION_USE_LLM_CREDENTIALS': 'true',
            'VISION_MODEL': 'mimo-v2.5',
        })


def test_invalid_visual_schema_fails_without_accepting_prose():
    class BadClient:
        def complete_images(self, *args, **kwargs):
            return 'A guessed summary'
    with pytest.raises(ModelServiceError, match='页面结构无效'):
        VisionReader(BadClient(), settings()).analyze(None, '')


def test_original_page_resolution_deduplicates_and_keeps_source_order():
    class Client:
        def complete_images(self, prompt, images, *, structured):
            assert images == ['paper-a-page-2', 'paper-b-page-1']
            assert '以原页图片为准' in prompt
            return 'ok'
    provider = VisualEvidence(Client(), settings(), lambda source, page: f'{source}-page-{page}')
    assert provider.complete('Check claims', [{'source': 'paper-a', 'visual_pages': [2]},
                             {'source': 'paper-a', 'visual_pages': [2]}, {'source': 'paper-b', 'visual_pages': [1]}]) == 'ok'


def test_missing_images_and_over_budget_never_fall_back_to_descriptions():
    class Client:
        def complete_images(self, *args, **kwargs):
            pytest.fail('Must not issue an incomplete request')
    provider = VisualEvidence(Client(), settings(max_images=1), lambda source, page: None)
    with pytest.raises(ModelServiceError, match='页数超过'):
        provider.complete('Check', [{'source': 'a', 'visual_pages': [1, 2]}])
    with pytest.raises(ModelServiceError, match='上下文预算'):
        provider.complete('Long prompt ' * 40000, [{'source': 'a', 'visual_pages': [1]}])
    def missing(source, page):
        raise KeyError('missing')
    provider.resolve_image = missing
    with pytest.raises(ModelServiceError, match='原页图片不可用'):
        provider.complete('Check', [{'source': 'a', 'visual_pages': [1]}])
