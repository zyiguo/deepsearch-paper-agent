from dataclasses import replace
import json

import httpx
import pytest

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_agent.adapters.vision_config import VisionSettings


def settings():
    return LLMSettings(
        backend='openai_compatible', api_key='test',
        base_url='https://api.xiaomimimo.com/v1', model='mimo-v2.6-pro',
        context_window=1048576, max_output_tokens=131072, timeout_seconds=300,
    )


def test_official_mimo_limits_are_accepted():
    config = settings()
    assert config.context_window == 1048576
    assert config.max_output_tokens == 131072
    for change in ({'context_window': 1048577}, {'max_output_tokens': 131073}):
        with pytest.raises(ValueError):
            replace(config, **change)


def test_text_and_vision_settings_load_large_budgets(tmp_path):
    values = {
        'LLM_BACKEND': 'openai_compatible', 'LLM_API_KEY': 'test',
        'LLM_BASE_URL': 'https://api.xiaomimimo.com/v1', 'LLM_MODEL': 'mimo-v2.6-pro',
        'LLM_CONTEXT_WINDOW': '1048576', 'LLM_MAX_OUTPUT_TOKENS': '32768',
    }
    llm = LLMSettings.load(tmp_path / 'absent', environ=values)
    vision = VisionSettings.load(tmp_path / 'absent', shared_llm=llm, environ={
        'VISION_BACKEND': 'openai_compatible', 'VISION_USE_LLM_CREDENTIALS': 'true',
        'VISION_MODEL': llm.model, 'VISION_CONTEXT_WINDOW': '1048576',
        'VISION_MAX_OUTPUT_TOKENS': '32768',
    })
    assert vision.llm.model == llm.model
    assert vision.llm.context_window == llm.context_window == 1048576
    assert vision.llm.max_output_tokens == llm.max_output_tokens == 32768


@pytest.mark.parametrize('with_image', [False, True])
def test_large_budget_reaches_mimo_wire_request(with_image):
    def server(request):
        payload = json.loads(request.content)
        assert payload['model'] == 'mimo-v2.6-pro'
        assert payload['max_completion_tokens'] == 131072
        assert 'max_tokens' not in payload
        assert payload['thinking'] == {'type': 'disabled'}
        assert payload['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}],
        })
    content = [{'type': 'text', 'text': 'Return JSON.'}]
    if with_image:
        content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,dGVzdA=='}})
    client = OpenAICompatibleClient(settings(), transport=httpx.MockTransport(server))
    try:
        assert client.generate_messages([{'role': 'user', 'content': content}], structured=True).text == '{"ok":true}'
    finally:
        client.close()
