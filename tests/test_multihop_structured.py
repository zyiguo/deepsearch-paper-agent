import json

import pytest

from deepsearch_experiments.benchmarks.multihop.structured import parse_object
from deepsearch_experiments.benchmarks.multihop.baselines import run_baseline


@pytest.mark.parametrize('text,envelope', [
    ('{"can_answer":true}', 'plain'),
    ('```json\n{"can_answer":true}\n```', 'fenced'),
    ('```\n{"can_answer":true}\n```', 'fenced'),
    ('Analysis of [0] and [1].\n```JSON\r\n{"can_answer":true}\r\n```\nDone.', 'fenced_with_prose'),
    ('Result: {"can_answer":true} End.', 'prose'),
    ('Result: {"can_answer":true,"nested":{"text":"brace } and { and \\\" quote"}} End.', 'prose'),
])
def test_single_object_envelopes(text, envelope):
    result = parse_object(text)
    assert result.value['can_answer'] is True
    assert result.envelope == envelope
    assert result.error_code is None


@pytest.mark.parametrize('text', [
    '', 'No JSON', '[]', '[{"can_answer":true}]',
    '{"can_answer":true} {"can_answer":false}',
    'First {"can_answer":true} then {"can_answer":false}',
    '```json\n{}\n```\n```json\n{}\n```',
    '{"draft":true}\n```json\n{}\n```',
    '```json\n{}\n```\n{"second":true}',
    '```json\n{}', '```python\n{}\n```',
    '```json\n[]\n```', '```json\n{} {}\n```',
    '{"outer":{"can_answer":true}',
    'Result: [{"can_answer":true}]',
    '{"can_answer": True}', '{"can_answer":true,}',
    '{"can_answer":true,"can_answer":false}',
    '{"nested":{"x":1,"x":2}}', '{"number":NaN}',
    '{"number":Infinity}', '{"number":1e999}',
    pytest.param('x' * 131073, id='oversized'),
    pytest.param('{"nested":' + '[' * 1500 + '0' + ']' * 1500 + '}', id='deeply-nested'),
])
def test_unsafe_or_ambiguous_envelopes_fail_closed(text):
    result = parse_object(text)
    assert result.value is None
    assert result.error_code


class Runtime:
    def __init__(self, graph):
        self.responses = iter(['Summary', '{"can_answer":false,"dependencies":["A","B"]}',
                               graph, 'Updated summary', '{"can_answer":true}', 'Answer'])
        self.calls = 0
        self.queries = []

    def generate(self, prompt, **kwargs):
        self.calls += 1
        return next(self.responses)

    def search(self, query):
        self.queries.append(query)
        return [{'id': 'p1', 'title': 'Example', 'text': 'Fact'}]


def test_wrapped_graph_continues_to_retrieval_without_repair_call():
    runtime = Runtime('Dependencies [1] requires [0].\n```json\n{"dependency_pairs":[[1,0]]}\n```')
    result = run_baseline('logicrag', 'Question', runtime)
    assert result['answer'] == 'Answer'
    assert result['stop_reason'] == 'dependencies_sufficient'
    assert runtime.calls == 6
    assert runtime.queries == ['Question', 'A']
    event = next(t for t in result['trace'] if t.get('stage') == 'dependency_graph')
    assert event['envelope'] == 'fenced_with_prose'


@pytest.mark.parametrize('pairs', [[[0, 1], [1, 0]], [[0, 9]], [[True, 0]], [[0, 0]]])
def test_envelope_adaptation_does_not_relax_graph_validation(pairs):
    runtime = Runtime('Explanation\n```json\n' + json.dumps({'dependency_pairs': pairs}) + '\n```')
    result = run_baseline('logicrag', 'Question', runtime)
    assert result['failure_stage'] == 'dependency_graph'
    assert result['failure_code'] == 'invalid_dependency_graph'
    assert runtime.calls == 3


def test_parse_failure_has_distinct_diagnostic():
    runtime = Runtime('```json\n{}\n```\n```json\n{}\n```')
    result = run_baseline('logicrag', 'Question', runtime)
    assert result['failure_code'] == 'ambiguous_or_invalid_fence'
    assert result['status'] == 'invalid_output'
