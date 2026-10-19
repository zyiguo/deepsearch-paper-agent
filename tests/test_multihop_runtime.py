import json

import pytest

from deepsearch_experiments.benchmarks.multihop.runtime import (
    BM25Retriever, BudgetExceeded, EvaluationRuntime, RuntimeLimits, ScriptedClient,
)
from deepsearch_agent.core.models import LLMResponse, ModelServiceError


def docs():
    return [{'id': 'd1', 'title': 'Alpha', 'text': 'Alpha is in France.'},
            {'id': 'd2', 'title': 'Beta', 'text': 'Beta is in Canada.'}]


def test_retrieval_is_label_free_and_records_all_searches():
    runtime = EvaluationRuntime(ScriptedClient([]), BM25Retriever(docs()), RuntimeLimits(k=1))
    first = runtime.search('Alpha')
    assert first[0]['id'] == 'd1'
    first[0]['text'] = 'mutation'
    assert runtime.search('Alpha')[0]['text'] != 'mutation'
    runtime.search('Beta')
    assert runtime.retrieved_document_ids == ['d1', 'd2']
    assert runtime.statistics()['search_calls'] == 3
    assert 'gold' not in json.dumps(runtime.events)


def test_call_and_search_budget_prevent_extra_requests():
    client = ScriptedClient(['first', 'second'])
    runtime = EvaluationRuntime(client, BM25Retriever(docs()), RuntimeLimits(model_calls=1, search_calls=1))
    runtime.generate('Question')
    with pytest.raises(BudgetExceeded, match='model_calls'):
        runtime.generate('Question')
    assert client.position == 1
    runtime.search('Alpha')
    with pytest.raises(BudgetExceeded, match='search_calls'):
        runtime.search('Beta')


def test_unknown_usage_is_not_reported_as_zero_and_stop_is_accounted():
    runtime = EvaluationRuntime(ScriptedClient(['answer\nNEXT extra']), BM25Retriever(docs()), RuntimeLimits())
    assert runtime.generate('Question', stop=['\nNEXT']) == 'answer'
    stats = runtime.statistics()
    assert stats['reported_total_tokens'] is None
    assert stats['usage_missing_calls'] == 1
    assert stats['accounted_tokens'] > 0
    assert runtime.events[0]['stop_mode'] == 'client_side_truncation'


def test_token_reservation_and_context_limit_block_calls():
    client = ScriptedClient(['unused'])
    runtime = EvaluationRuntime(client, BM25Retriever(docs()), RuntimeLimits(token_budget=50))
    with pytest.raises(BudgetExceeded, match='token_budget'):
        runtime.generate('Question')
    assert client.position == 0
    runtime = EvaluationRuntime(client, BM25Retriever(docs()), RuntimeLimits(context_window=1024))
    with pytest.raises(BudgetExceeded, match='context_window'):
        runtime.generate('x' * 2000)


def test_failure_attempt_counts_and_unknown_usage():
    class Broken:
        def generate(self, prompt):
            raise RuntimeError('a secret must not enter trace')
    runtime = EvaluationRuntime(Broken(), BM25Retriever(docs()), RuntimeLimits())
    with pytest.raises(RuntimeError):
        runtime.generate('Question')
    assert runtime.statistics()['model_calls'] == 1
    assert runtime.statistics()['usage_missing_calls'] == 1
    assert 'a secret' not in json.dumps(runtime.events)


def test_actual_usage_and_deadline():
    class Client:
        def generate(self, prompt):
            return LLMResponse('yes', total_tokens=12, prompt_tokens=10, completion_tokens=2)
    now = [0.0]
    runtime = EvaluationRuntime(Client(), BM25Retriever(docs()), RuntimeLimits(deadline_seconds=1), clock=lambda: now[0])
    runtime.generate('Question')
    assert runtime.statistics()['reported_total_tokens'] == 12
    now[0] = 2
    with pytest.raises(BudgetExceeded, match='deadline'):
        runtime.search('Alpha')


@pytest.mark.parametrize('kwargs', [{'k': 0}, {'model_calls': True}, {'deadline_seconds': float('nan')}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        RuntimeLimits(**kwargs)


def test_deadline_cancels_inflight_gateway_request():
    import asyncio
    import httpx
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
    from deepsearch_agent.core.cancellation import ResearchCancelled

    started = []
    async def handler(request):
        started.append(True)
        await asyncio.sleep(30)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'late'}}]})

    settings = LLMSettings(backend='openai_compatible', api_key='fixture-key',
                           base_url='https://fixture.invalid/v1', model='fixture')
    client = OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler))
    runtime = EvaluationRuntime(client, BM25Retriever(docs()), RuntimeLimits(deadline_seconds=.3))
    try:
        with runtime.cancellation_scope(), pytest.raises(ResearchCancelled):
            runtime.generate('Question')
    finally:
        client.close()
    assert started
    assert runtime.statistics()['latency_ms'] < 5000
    assert runtime.statistics()['usage_missing_calls'] == 1
    assert runtime.events[-1]['error_code'] == 'cancelled'


@pytest.mark.parametrize('total', [30, None])
def test_failed_model_usage_is_reconciled_without_fabrication(total):
    class Broken:
        def generate(self, prompt):
            raise ModelServiceError('secret body', error_code='output_truncated', provider_status_code=200,
                                    finish_reason='length', usage={'prompt_tokens': 10, 'completion_tokens': 20,
                                                                    'total_tokens': total})
    runtime = EvaluationRuntime(Broken(), BM25Retriever(docs()), RuntimeLimits())
    with pytest.raises(ModelServiceError):
        runtime.generate('Question')
    stats = runtime.statistics()
    assert stats['model_calls'] == 1
    assert stats['reported_total_tokens'] == total
    assert stats['usage_missing_calls'] == int(total is None)
    assert stats['accounted_tokens'] == (30 if total else len('Question') + 512 + 1024)
    event = runtime.events[0]
    assert event['error_code'] == 'output_truncated'
    assert event['finish_reason'] == 'length'
    assert event['prompt_tokens'] == 10
    assert event['latency_ms'] >= 0
    assert 'secret body' not in json.dumps(event)


@pytest.mark.parametrize('status,body,code', [
    (200, {'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]}, 'output_truncated'),
    (429, {'error': {'message': 'secret'}}, 'rate_limit'),
])
def test_cancellable_gateway_errors_reach_runtime_with_usage(status, body, code):
    import httpx
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient

    calls = []
    async def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert 'thinking' not in payload and 'response_format' not in payload
        return httpx.Response(status, json={**body, 'usage': {'total_tokens': 30}})
    settings = LLMSettings(backend='openai_compatible', api_key='fixture-key',
                           base_url='https://fixture.invalid/v1', model='mimo-v2.6-pro')
    client = OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler))
    runtime = EvaluationRuntime(client, BM25Retriever(docs()), RuntimeLimits())
    try:
        with runtime.cancellation_scope(), pytest.raises(ModelServiceError):
            runtime.generate('Question')
    finally:
        client.close()
    assert len(calls) == 1
    assert runtime.statistics()['reported_total_tokens'] == 30
    assert runtime.events[-1]['error_code'] == code
    assert 'secret' not in json.dumps(runtime.events)
