import json

import httpx
import pytest

from deepsearch_agent.llm_config import LLMSettings
from deepsearch_agent.llm_gateway import ModelServiceError, OpenAICompatibleClient
from deepsearch_agent.search_intent import SearchIntent, parse_decision
from deepsearch_agent.qa import AnswerService
from test_research_graph import Client, Library


class PolicyClient:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def complete_messages(self, messages):
        self.calls.append(messages)
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


@pytest.mark.parametrize('raw', [
    '{}', '[]', '{"search":""}', '{"search":2}',
    '{"search":"a","answer":"b"}', '{"search":"a","search":"b"}',
    '```json\n{"search":"a"}\n```', '{"other":"a"}',
    json.dumps({'search': 'a' * 2001}),
])
def test_reject_invalid_actions(raw):
    with pytest.raises(ValueError):
        parse_decision(raw)


def test_answer_text_is_discarded():
    assert parse_decision('{"answer":"invented claim"}').query is None


def test_policy_preserves_system_and_question_and_labels_history():
    client = PolicyClient('{"search":" 灯塔周期 "}')
    decision = SearchIntent(client).decide('它多久闪一次？', [{'role': 'user', 'content': '灯塔'}])
    assert decision.query == '灯塔周期'
    messages = client.calls[0]
    assert messages[0]['role'] == 'system'
    assert 'not evidence' in messages[1]['content']
    assert messages[-1]['content'] == '它多久闪一次？'


@pytest.mark.parametrize('output', ['bad json', '{"answer":"UNKNOWN"}', ModelServiceError('offline')])
def test_fallback_still_retrieves_and_verifies(output):
    policy = SearchIntent(PolicyClient(output))
    main = Client([{'queries': ['灯塔']},
                   {'claims': [{'text': '白光', 'citations': [1]}]},
                   {'supported': [0], 'queries': []}])
    answer = AnswerService(main, Library(), use_graph=True, search_intent=policy).answer('灯塔信号？')
    assert answer.text == '白光 [1]'
    assert len(answer.sources) == 1


def test_policy_only_changes_first_query_and_keeps_verifier():
    class RecordingLibrary(Library):
        def __init__(self):
            self.queries = []

        def search(self, query, top_k=5):
            self.queries.append(query)
            return super().search(query, top_k)

    client = PolicyClient('{"search":"自训检索词"}')
    library = RecordingLibrary()
    main = Client([
        {'claims': [{'text': '错误', 'citations': [1]}]},
        {'supported': [], 'queries': ['补查']},
        {'queries': ['原研究器补查']},
        {'claims': [{'text': '白光', 'citations': [1]}]},
        {'supported': [0], 'queries': []},
    ])
    answer = AnswerService(main, library, use_graph=True,
                           search_intent=SearchIntent(client)).answer('灯塔信号？')
    assert library.queries == ['自训检索词', '原研究器补查']
    assert len(client.calls) == 1
    assert answer.text == '白光 [1]'
    assert '错误' not in answer.text


def test_gateway_preserves_message_roles_without_network():
    messages = [{'role': 'system', 'content': 'protocol'}, {'role': 'user', 'content': 'question'}]

    def respond(request):
        assert json.loads(request.content)['messages'] == messages
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"search":"q"}'}}]})

    client = OpenAICompatibleClient(LLMSettings(backend='openai_compatible', api_key='test',
        base_url='https://model.example/v1', model='test'), transport=httpx.MockTransport(respond))
    try:
        assert client.complete_messages(messages) == '{"search":"q"}'
    finally:
        client.close()
