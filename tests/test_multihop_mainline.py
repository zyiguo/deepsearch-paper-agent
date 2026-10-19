import json

import pytest

from deepsearch_experiments.benchmarks.multihop.mainline import CorpusBridge, RetrievedOnlySession, run_mainline
from deepsearch_experiments.benchmarks.multihop.runtime import (
    BM25Retriever, BudgetExceeded, EvaluationRuntime, RuntimeLimits, ScriptedClient,
)
from deepsearch_agent.agents.tool_service import ToolError


def runtime(responses, **limits):
    return EvaluationRuntime(ScriptedClient(responses), BM25Retriever([
        {'id': 'doc_a', 'title': 'Alice', 'text': 'Alice was born in Paris.'},
        {'id': 'doc_b', 'title': 'Bob', 'text': 'Bob was born in Berlin.'},
    ]), RuntimeLimits(context_window=131072, token_budget=256000, **limits))


def decision(action):
    return json.dumps({'action': action, 'tasks': [], 'instruction': '', 'skill_ids': []})


def happy_tape():
    return [decision('research'),
            '{"tool":"search_evidence","arguments":{"query":"Alice","top_k":1}}',
            '{"tool":"","arguments":{}}', decision('draft'),
            '{"claims":[{"text":"Paris","citations":[1]}]}',
            '{"supported":[0],"queries":[],"issues":[],"missing_answerable":[]}',
            decision('finish'), 'Paris']


def test_real_graph_roles_tools_and_projection_are_budgeted():
    rt = runtime(happy_tape())
    result = run_mainline('Where was Alice born?', rt)
    assert result['answer'] == 'Paris'
    assert result['status'] == 'ok'
    assert result['outcome']['status'] == 'verified'
    assert result['verified_answer'] == 'Paris [1]'
    assert rt.model_calls == 8 and rt.search_calls == 1
    roles = {event['role'] for event in rt.events if event['kind'] == 'mainline.rag.request'}
    assert roles == {'Supervisor', 'Researcher', 'Answerer', 'Verifier'}
    assert result['tool_calls'] == 1


def test_projection_cannot_bypass_model_call_budget():
    rt = runtime(happy_tape(), model_calls=7)
    with pytest.raises(BudgetExceeded, match='model_calls'):
        run_mainline('Where was Alice born?', rt)
    assert rt.model_calls == 7


def test_corpus_bridge_caps_k_and_prevents_unretrieved_reads():
    rt = runtime([], k=1)
    kb = CorpusBridge(rt)
    session = RetrievedOnlySession(kb)
    with pytest.raises(ToolError, match='evidence_not_retrieved'):
        session.get_evidence(kb.by_id['doc_b'].chunk_id)
    assert len(kb.search('Alice', 10)) == 1
    assert session.get_evidence(kb.by_id['doc_a'].chunk_id).chunks
    assert not kb.restrict_sources({'doc_b'}).search('Alice')


def test_search_budget_failure_survives_tool_error_wrapping():
    rt = runtime([], search_calls=1)
    kb = CorpusBridge(rt)
    session = RetrievedOnlySession(kb)
    session.invoke('search_evidence', {'query': 'Alice'})
    with pytest.raises(ToolError):
        session.invoke('search_evidence', {'query': 'Bob'})
    with pytest.raises(BudgetExceeded, match='search_calls'):
        kb.check()


def test_schema_failure_is_not_a_successful_chinese_answer():
    rt = runtime(['invalid', 'invalid'])
    result = run_mainline('Where?', rt)
    assert result['status'] == 'invalid_output'
    assert result['answer'] == ''
    assert rt.model_calls == 2


def test_no_verified_claims_abstains_without_projection():
    rt = runtime([decision('research'),
        '{"tool":"search_evidence","arguments":{"query":"Alice"}}',
        '{"tool":"","arguments":{}}', decision('stop')])
    result = run_mainline('Where?', rt)
    assert result['answer'] == '' and result['status'] == 'ok'
    assert result['termination_reason'] == 'no_verified_claims'
    assert rt.model_calls == 4
