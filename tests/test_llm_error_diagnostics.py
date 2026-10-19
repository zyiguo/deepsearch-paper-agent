import json

import httpx
import pytest

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient


SECRET = 'fixture-secret-never-log'
USAGE = {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}


def client(handler):
    settings = LLMSettings(backend='openai_compatible', api_key=SECRET,
                           base_url='https://fixture.invalid/v1', model='fixture')
    return OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('status,code,retryable', [
    (400, 'request_rejected', False), (401, 'authentication_error', False),
    (403, 'permission_denied', False), (429, 'rate_limit', True),
    (500, 'server_error', True), (503, 'server_error', True),
])
def test_http_errors_are_classified_without_leaking_provider_body(status, code, retryable):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'message': SECRET}, 'usage': USAGE})
    c = client(handler)
    try:
        with pytest.raises(ModelServiceError) as caught:
            c.generate('Question')
        e = caught.value
        assert e.error_code == code
        assert e.provider_status_code == status
        assert e.retryable is retryable
        assert e.total_tokens == 30
        assert SECRET not in str(e) + json.dumps(e.diagnostics())
        assert len(calls) == 1
    finally:
        c.close()


@pytest.mark.parametrize('exception,code', [(httpx.ReadTimeout, 'timeout'),
                                          (httpx.ConnectTimeout, 'timeout'),
                                          (httpx.ConnectError, 'connection_error')])
def test_transport_error_classification(exception, code):
    def handler(request):
        raise exception(SECRET, request=request)
    c = client(handler)
    try:
        with pytest.raises(ModelServiceError) as caught:
            c.generate('Question')
        assert caught.value.error_code == code
        assert caught.value.provider_status_code is None
        assert caught.value.total_tokens is None
        assert SECRET not in str(caught.value) + json.dumps(caught.value.diagnostics())
    finally:
        c.close()


@pytest.mark.parametrize('body,code', [
    ({'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]}, 'output_truncated'),
    ({'choices': [{'finish_reason': 'content_filter', 'message': {'content': None}}]}, 'content_filtered'),
    ({'choices': [{'message': {'content': ''}}]}, 'empty_response'),
    ({'choices': [{'message': {'content': None}}]}, 'empty_response'),
    ({'choices': [{'message': {'content': []}}]}, 'invalid_response'),
    ({'choices': []}, 'invalid_response'),
    ({'choices': [None]}, 'invalid_response'),
    ({'choices': [{'message': None}]}, 'invalid_response'),
    ({}, 'invalid_response'),
])
def test_response_errors_preserve_usage(body, code):
    c = client(lambda request: httpx.Response(200, json={**body, 'usage': USAGE}))
    try:
        with pytest.raises(ModelServiceError) as caught:
            c.generate('Question')
        e = caught.value
        assert e.error_code == code
        assert e.provider_status_code == 200
        assert e.total_tokens == 30
        assert e.prompt_tokens == 10
        assert e.completion_tokens == 20
    finally:
        c.close()


@pytest.mark.parametrize('body,code', [('not json', 'invalid_json'), ('null', 'invalid_response'), ('[]', 'invalid_response')])
def test_non_object_provider_body_is_safe(body, code):
    c = client(lambda request: httpx.Response(200, text=body))
    try:
        with pytest.raises(ModelServiceError) as caught:
            c.generate('Question')
        assert caught.value.error_code == code
    finally:
        c.close()


def test_usage_and_error_metadata_are_whitelisted():
    e = ModelServiceError(SECRET, error_code=SECRET, provider_status_code=True,
                          usage={'prompt_tokens': True, 'completion_tokens': -1, 'total_tokens': '30', 'secret': SECRET},
                          finish_reason=SECRET)
    assert e.error_code == 'model_service_error'
    assert e.total_tokens is e.prompt_tokens is e.completion_tokens is None
    assert e.provider_status_code is e.finish_reason is None
    assert SECRET not in json.dumps(e.diagnostics())
