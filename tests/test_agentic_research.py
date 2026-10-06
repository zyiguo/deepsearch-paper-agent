import json
from contextlib import asynccontextmanager

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.tokens import estimate_tokens


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str
    top_k: int = Field(default=5, ge=1, le=20)


class Page(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    document_id: str
    page: int = Field(ge=1)


class Empty(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ToolSession:
    def __init__(self, budget=8):
        self.remaining_calls = budget
        self.evidence = []
        self.executed = []
        self.chunk = TextChunk('The reported value is 12.', 'paper.pdf', 1, 0)

    def record(self, name):
        self.remaining_calls -= 1
        self.executed.append(name)

    def tools(self):
        def list_documents():
            self.record('list_documents')
            return json.dumps({'documents': [{'id': 'paper', 'pages': 2}]}), None

        def search_evidence(query, top_k=5):
            self.record('search_evidence')
            self.evidence.append(self.chunk)
            return json.dumps({'hits': [{'chunk_id': self.chunk.chunk_id, 'text': self.chunk.text}]}), [self.chunk]

        def read_page(document_id, page):
            self.record('read_page')
            return json.dumps({'document_id': document_id, 'page': page, 'text': 'Page 2 explains the chart.'}), None

        return [
            StructuredTool(name='list_documents', description='List authorized documents', args_schema=Empty,
                           func=list_documents, response_format='content_and_artifact'),
            StructuredTool(name='search_evidence', description='Search local evidence', args_schema=Query,
                           func=search_evidence, response_format='content_and_artifact'),
            StructuredTool(name='read_page', description='Read an authorized page', args_schema=Page,
                           func=read_page, response_format='content_and_artifact'),
        ]


class Planner:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.contexts = []

    def __call__(self, instruction, schema, data):
        self.contexts.append(data)
        return schema.model_validate(next(self.actions))


def researcher(session, planner, **kwargs):
    from deepsearch_agent.agentic_research import AgenticResearcher
    return AgenticResearcher(session, planner, **kwargs)


def test_model_selected_tools_receive_observation_feedback():
    session = ToolSession()
    planner = Planner([
        {'tool': 'list_documents', 'arguments': {}},
        {'tool': 'search_evidence', 'arguments': {'query': 'reported value'}},
        {'tool': 'read_page', 'arguments': {'document_id': 'paper', 'page': 2}},
        {'tool': '', 'arguments': {}},
    ])
    evidence = researcher(session, planner).run('What value is reported?', {'top_k': 3}, [])

    assert evidence == [session.chunk]
    assert session.executed == ['list_documents', 'search_evidence', 'read_page']
    assert 'paper' in json.dumps(planner.contexts[1]['observations'])
    assert session.chunk.chunk_id in json.dumps(planner.contexts[2]['observations'])
    assert 'Page 2 explains the chart.' in json.dumps(planner.contexts[3]['observations'])


def test_early_finish_still_searches_this_turn_question():
    session = ToolSession()
    planner = Planner([{'tool': '', 'arguments': {}}, {'tool': '', 'arguments': {}}])
    evidence = researcher(session, planner).run('What value is reported?', {'top_k': 2}, [])
    assert evidence == [session.chunk]
    assert session.executed == ['search_evidence']


def test_unknown_tool_is_feedback_not_arbitrary_execution():
    session = ToolSession()
    planner = Planner([
        {'tool': 'run_shell', 'arguments': {'command': 'bad'}},
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': '', 'arguments': {}},
    ])
    evidence = researcher(session, planner).run('Value?', {}, [])
    assert evidence == [session.chunk]
    assert session.executed == ['search_evidence']
    assert 'unknown_tool' in json.dumps(planner.contexts[1]['observations'])
    assert 'command' not in json.dumps(planner.contexts[1]['observations'])


def test_tool_error_is_redacted_from_planner_and_traces():
    class BrokenSession(ToolSession):
        def tools(self):
            def fail(query, top_k=5):
                self.record('search_evidence')
                raise RuntimeError('private-api-key-and-private-path')
            return [StructuredTool(name='search_evidence', description='Search', args_schema=Query, func=fail)]

    session = BrokenSession()
    planner = Planner([
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': '', 'arguments': {}},
    ])
    events = []
    assert researcher(session, planner, observer=lambda kind, payload: events.append((kind, payload))).run('Value?', {}, []) == []
    serialized = json.dumps({'contexts': planner.contexts, 'events': events})
    assert 'tool_failed' in serialized
    assert 'private-api-key' not in serialized


def test_invalid_arguments_do_not_reach_tool_implementation():
    session = ToolSession()
    planner = Planner([
        {'tool': 'search_evidence', 'arguments': {'query': 'value', 'arbitrary_path': '/secret'}},
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': '', 'arguments': {}},
    ])
    assert researcher(session, planner).run('Value?', {}, []) == [session.chunk]
    assert session.executed == ['search_evidence']
    assert 'tool_failed' in json.dumps(planner.contexts[1]['observations'])
    assert '/secret' not in json.dumps(planner.contexts[1]['observations'])


def test_shared_call_budget_stops_before_another_model_request():
    session = ToolSession(budget=1)
    planner = Planner([{'tool': 'search_evidence', 'arguments': {'query': 'value'}}])
    assert researcher(session, planner).run('Value?', {}, []) == [session.chunk]
    assert len(planner.contexts) == 1
    assert session.remaining_calls == 0


def test_step_budget_bounds_invalid_model_decisions():
    session = ToolSession()
    planner = Planner([{'tool': 'unknown', 'arguments': {}}] * 3)
    assert researcher(session, planner, max_steps=3).run('Value?', {}, []) == []
    assert len(planner.contexts) == 3
    assert session.executed == []


def test_identical_tool_calls_allow_only_one_retry():
    session = ToolSession()
    planner = Planner([
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': '', 'arguments': {}},
    ])
    assert researcher(session, planner).run('Value?', {}, []) == [session.chunk]
    assert session.executed == ['search_evidence', 'search_evidence']
    assert 'repeated_tool_call' in json.dumps(planner.contexts[3]['observations'])


def test_external_async_artifact_cannot_create_local_citation_evidence():
    class ExternalSession(ToolSession):
        def tools(self):
            async def external_lookup():
                self.record('external_lookup')
                return 'External server claims a value of 99.', [TextChunk('99', 'fabricated.pdf', 1, 0)]
            return super().tools() + [StructuredTool(
                name='external_lookup', description='External untrusted observation', args_schema=Empty,
                coroutine=external_lookup, response_format='content_and_artifact')]

    session = ExternalSession()
    planner = Planner([
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': 'external_lookup', 'arguments': {}},
        {'tool': '', 'arguments': {}},
    ])
    evidence = researcher(session, planner).run('Value?', {}, [])
    assert [chunk.source_path for chunk in evidence] == ['paper.pdf']
    assert 'External server claims' in json.dumps(planner.contexts[2]['observations'])


def test_existing_and_duplicate_evidence_are_not_returned_as_new():
    session = ToolSession()
    planner = Planner([
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': '', 'arguments': {}},
    ])
    assert researcher(session, planner).run('Value?', {}, [session.chunk]) == []


def test_oversized_tool_observation_is_bounded_before_next_plan():
    class LargeSession(ToolSession):
        def tools(self):
            def list_documents():
                self.record('list_documents')
                return 'X' * 200000
            return super().tools() + [StructuredTool(
                name='large_listing', description='Huge external result', args_schema=Empty, func=list_documents)]

    session = LargeSession()
    planner = Planner([
        {'tool': 'large_listing', 'arguments': {}},
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': '', 'arguments': {}},
    ])
    researcher(session, planner).run('Value?', {'large_context': 'Y' * 200000}, [])
    assert len(json.dumps(planner.contexts[1])) < 40000
    assert len(json.dumps(planner.contexts[1]['observations'])) < 6000


def test_no_remaining_budget_never_invokes_planner():
    session = ToolSession(budget=0)
    planner = Planner([])
    assert researcher(session, planner).run('Value?', {}, []) == []
    assert planner.contexts == []


def test_exhausted_budget_never_connects_external_servers():
    @asynccontextmanager
    async def external_tools():
        raise AssertionError('A server was contacted with no call budget')
        yield []

    events = []
    worker = researcher(ToolSession(budget=0), Planner([]), external_tools=external_tools,
                        observer=lambda kind, payload: events.append((kind, payload)))
    assert worker.run('Value?', {}, []) == []
    assert events == []
    assert worker.stop_reason == 'call_budget'


def test_async_external_clients_remain_open_until_the_graph_finishes():
    lifecycle = []

    class SessionWithExternal(ToolSession):
        def tools(self, extra_tools=None):
            return super().tools() + (extra_tools or [])

    session = SessionWithExternal()

    @asynccontextmanager
    async def external_tools():
        lifecycle.append('open')

        async def external_lookup():
            assert lifecycle == ['open']
            session.record('external_lookup')
            return 'Untrusted external observation'

        try:
            yield [StructuredTool(name='external_lookup', description='External', args_schema=Empty,
                                  coroutine=external_lookup)]
        finally:
            lifecycle.append('close')

    planner = Planner([
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': 'external_lookup', 'arguments': {}},
        {'tool': '', 'arguments': {}},
    ])
    assert researcher(session, planner, external_tools=external_tools).run('Value?', {}, []) == [session.chunk]
    assert lifecycle == ['open', 'close']
    assert session.executed == ['search_evidence', 'external_lookup']


def test_external_connection_failure_falls_back_without_leaking_credentials():
    @asynccontextmanager
    async def unavailable():
        raise RuntimeError('secret-bearer-token')
        yield []

    session = ToolSession()
    events = []
    planner = Planner([
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': '', 'arguments': {}},
    ])
    assert researcher(session, planner, external_tools=unavailable,
                      observer=lambda kind, payload: events.append((kind, payload))).run('Value?', {}, []) == [session.chunk]
    assert 'unavailable' in json.dumps(events)
    assert 'secret-bearer-token' not in json.dumps(events)


def test_researcher_disables_hosted_tracing_for_planner_and_tools(monkeypatch):
    from langsmith import utils
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    session = ToolSession()
    checked = []

    def planner(instruction, schema, data):
        checked.append(utils.tracing_is_enabled())
        return schema.model_validate({'tool': '', 'arguments': {}})

    assert researcher(session, planner).run('Value?', {}, []) == [session.chunk]
    assert checked == [False, False]


def test_small_model_context_preserves_usable_tool_specs_within_budget():
    session = ToolSession()
    planner = Planner([
        {'tool': 'search_evidence', 'arguments': {'query': 'value'}},
        {'tool': '', 'arguments': {}},
    ])
    researcher(session, planner, context_budget_tokens=1400).run(
        'Value?', {'evidence': [{'text': 'Large irrelevant evidence. ' * 1000}]}, [])
    data = planner.contexts[0]
    assert estimate_tokens(json.dumps(data, ensure_ascii=False)) <= 600
    assert isinstance(data['available_tools'], list)
    assert any(tool['name'] == 'search_evidence' and 'query' in tool['arguments']['properties']
               for tool in data['available_tools'])
    assert 'evidence' in data['context']


def test_initial_search_uses_service_query_and_top_k_limits():
    class LimitedSession(ToolSession):
        def tools(self):
            class LimitedQuery(Query):
                query: str = Field(max_length=2000)
                top_k: int = Field(default=5, ge=1, le=10)

            def search_evidence(query, top_k=5):
                self.record('search_evidence')
                self.evidence.append(self.chunk)
                return {'query_length': len(query), 'top_k': top_k}
            return [StructuredTool(name='search_evidence', description='Search', args_schema=LimitedQuery,
                                   func=search_evidence)]

    session = LimitedSession()
    planner = Planner([{'tool': '', 'arguments': {}}, {'tool': '', 'arguments': {}}])
    assert researcher(session, planner).run('Q' * 3000, {'top_k': 20}, []) == [session.chunk]
    assert '2000' in json.dumps(planner.contexts[1]['observations'])


@pytest.mark.parametrize('budget', [0, -1, 500, True])
def test_invalid_context_budget_is_rejected(budget):
    with pytest.raises(ValueError):
        researcher(ToolSession(), Planner([]), context_budget_tokens=budget)


@pytest.mark.parametrize('steps', [0, -1, 33, True])
def test_invalid_step_budget_is_rejected(steps):
    with pytest.raises(ValueError):
        researcher(ToolSession(), Planner([]), max_steps=steps)
