from deepsearch_agent.adapters.tool_adapters import langgraph_tools
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.prebuilt import ToolNode
from langgraph.graph import END, START, StateGraph

from deepsearch_agent.agents.agent_config import AgentSettings
from deepsearch_experiments.benchmarks.instrumentation import (
    MeasuredClient, RunTrace, TracedKnowledgeBase, TracedReranker, TracedToolSession,
)
from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.agents.tool_service import ToolError


SOURCE = 'pdf://' + 'a' * 64 + '/paper.pdf'
OTHER = 'pdf://' + 'b' * 64 + '/other.pdf'


class KB:
    image_index = None

    def __init__(self, chunks):
        self.chunks = chunks
        self.searches = 0

    def search(self, query, top_k=5):
        self.searches += 1
        return self.chunks[:top_k]

    def restrict_sources(self, sources):
        return KB([c for c in self.chunks if c.source_path in sources])


def settings():
    return LLMSettings(backend='openai_compatible', api_key='secret-api-key',
                       base_url='https://model.example/v1', model='model-test')


def response(request):
    return httpx.Response(200, json={
        'id': 'request-one', 'model': 'served-model',
        'choices': [{'message': {'content': '{"ok": true}'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 21, 'completion_tokens': 8, 'total_tokens': 29},
    })


def test_gateway_usage_safe_prompts_and_shared_budget():
    trace = RunTrace({}, max_model_calls=1)
    client = MeasuredClient(settings(), trace, transport=httpx.MockTransport(response))
    other = MeasuredClient(settings(), trace, transport=httpx.MockTransport(response))
    try:
        with trace.stage('answer'):
            value = client.generate_messages([{'role': 'user', 'content': [
                {'type': 'text', 'text': '\u4f60\u662f Answerer secret-api-key'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,SECRETPIXELS'}},
            ]}])
        assert value.total_tokens == 29
        record = trace.model_calls[0]
        assert record['status'] == 'ok'
        assert record['stage'] == 'answer'
        assert record['role'] == 'Answerer'
        assert record['image_count'] == 1
        assert record['prompt_tokens'] == 21
        assert record['completion_tokens'] == 8
        assert record['total_tokens'] == 29
        assert record['model'] == 'served-model'
        assert record['request_id'] == 'request-one'
        assert record['raw_output'] == '{"ok": true}'
        with pytest.raises(ModelServiceError, match='model_call_budget'):
            other.complete('another request')
        assert trace.model_calls[-1]['status'] == 'budget_exceeded'
        encoded = json.dumps(trace.to_dict())
        assert 'secret-api-key' not in encoded
        assert 'SECRETPIXELS' not in encoded
    finally:
        client.close()
        other.close()


def test_parallel_clients_share_atomic_budget():
    trace = RunTrace({}, max_model_calls=3)
    client = MeasuredClient(settings(), trace, transport=httpx.MockTransport(response))
    def call(_):
        try:
            client.complete('query')
            return True
        except ModelServiceError:
            return False
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert sum(pool.map(call, range(8))) == 3
        assert sum(row['status'] == 'ok' for row in trace.model_calls) == 3
    finally:
        client.close()


def test_scoped_retrieval_and_rerank_records_original_ids():
    chunks = [TextChunk('first', SOURCE, 1, 0), TextChunk('second', SOURCE, 2, 0),
              TextChunk('private', OTHER, 1, 0)]
    original = KB(chunks)
    trace = RunTrace({(SOURCE, 1): 'page1', (SOURCE, 2): 'page2', (OTHER, 1): 'private'})
    view = TracedKnowledgeBase(original, trace).restrict_sources({SOURCE})
    result = view.search('query', top_k=5)
    assert original.chunks == chunks
    assert original.searches == 0
    assert trace.retrieval_calls[0]['page_ids'] == ['page1', 'page2']
    assert trace.retrieval_calls[0]['ranking'] == ['page1', 'page2']
    assert trace.retrieval_calls[0]['requested_k'] == 5
    assert trace.retrieval_calls[0]['chunk_ids'] == [c.chunk_id for c in chunks[:2]]
    class Reverse:
        last_status = 'ok'
        last_scores = [{'chunk_id': chunks[1].chunk_id, 'score': 95}]
        def rerank(self, query, chunks, top_k):
            return list(reversed(chunks))[:top_k]
    reranker = TracedReranker(Reverse(), trace)
    assert reranker.rerank('query', result, 1) == [chunks[1]]
    row = trace.rerank_calls[0]
    assert row['initial_page_ids'] == ['page1', 'page2']
    assert row['initial_ranking'] == ['page1', 'page2']
    assert row['page_ids'] == ['page2']
    assert row['scores'] == Reverse.last_scores
    assert reranker.last_status == 'ok'


def test_tools_record_invalid_arguments_budgets_and_safe_errors():
    trace = RunTrace({(SOURCE, 1): 'page1'})
    kb = KB([TextChunk('evidence', SOURCE, 1, 0)])
    def broken(source, page):
        raise RuntimeError('credential-canary')
    session = TracedToolSession(kb, trace=trace,
        settings=AgentSettings(max_tool_calls=1), page_reader=broken)
    with pytest.raises(ToolError, match='invalid_arguments'):
        session.invoke('read_page', {'source': SOURCE, 'page': True})
    with pytest.raises(ToolError, match='tool_failed'):
        session.invoke('read_page', {'source': SOURCE, 'page': 1})
    with pytest.raises(ToolError, match='tool_budget'):
        session.invoke('list_documents', {})
    assert [r['error_code'] for r in trace.tool_calls] == [
        'invalid_arguments', 'tool_failed', 'tool_budget']
    assert 'credential-canary' not in json.dumps(trace.to_dict())


def test_toolnode_context_and_new_evidence_without_double_execution():
    trace = RunTrace({(SOURCE, 1): 'page1'})
    kb = KB([TextChunk('evidence', SOURCE, 1, 0)])
    session = TracedToolSession(TracedKnowledgeBase(kb, trace), trace=trace)
    class State(TypedDict):
        messages: list
    async def run():
        with trace.stage('research'):
            builder = StateGraph(State)
            builder.add_node('tools', ToolNode(langgraph_tools(session)))
            builder.add_edge(START, 'tools')
            builder.add_edge('tools', END)
            graph = builder.compile()
            await graph.ainvoke({'messages': [AIMessage(content='', tool_calls=[{
                'name': 'search_evidence', 'args': {'query': 'evidence'},
                'id': 'one', 'type': 'tool_call',
            }])]})
    asyncio.run(run())
    assert kb.searches == 1
    assert trace.tool_calls[0]['stage'] == 'research'
    assert trace.retrieval_calls[0]['stage'] == 'tool:search_evidence'
    assert trace.tool_calls[0]['new_chunk_ids'] == [kb.chunks[0].chunk_id]
    assert trace.tool_calls[0]['data']['evidence'][0]['chunk_id'] == kb.chunks[0].chunk_id
    session.invoke('get_evidence', {'chunk_id': kb.chunks[0].chunk_id})
    assert trace.tool_calls[1]['new_chunk_ids'] == []


def test_observer_is_bounded_and_discards_images_and_credentials():
    trace = RunTrace({})
    trace.observer('rag.parsed', {'role': 'Verifier', 'value': {'supported': [0]}})
    trace.observer('rag.outcome', {'status': 'verified'})
    trace.observer('rag.request', {'prompt': 'x' * 200000,
        'authorization': 'credential-canary', 'image': {'base64': 'PIXEL-CANARY'}})
    result = trace.to_dict()
    assert result['events'][0]['payload']['value']['supported'] == [0]
    assert result['events'][1]['payload']['status'] == 'verified'
    encoded = json.dumps(result)
    assert 'credential-canary' not in encoded
    assert 'PIXEL-CANARY' not in encoded
    assert len(result['events'][2]['payload']['prompt']) <= 150000
