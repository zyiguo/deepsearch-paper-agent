import json
import httpx
import pytest
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_agent.core.models import ModelServiceError


def test_mimo_structured_request_disables_thinking_and_requests_json():
    def server(request):
        body = json.loads(request.content)
        assert body['thinking'] == {'type':'disabled'}
        assert body['response_format'] == {'type':'json_object'}
        assert body['max_completion_tokens'] == 1024
        assert 'max_tokens' not in body
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})
    settings = LLMSettings(backend='openai_compatible', api_key='test', base_url='https://example.test/v1', model='mimo-v2.5-pro')
    client = OpenAICompatibleClient(settings, transport=httpx.MockTransport(server))
    try:
        assert client.complete_structured('JSON please') == '{"ok":true}'
    finally:
        client.close()


def test_truncated_response_is_not_accepted_as_complete():
    settings = LLMSettings(backend='openai_compatible', api_key='test', base_url='https://example.test/v1', model='model')
    client = OpenAICompatibleClient(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200,
        json={'choices':[{'finish_reason':'length','message':{'content':'partial'}}]})))
    try:
        with pytest.raises(ModelServiceError, match='截断'):
            client.complete('prompt')
    finally:
        client.close()
