import json

import pytest

from deepsearch_agent.agents.budget_control import ResearchBudgetControl
from deepsearch_experiments.benchmarks.multihop.mainline import run_mainline
from test_multihop_mainline import runtime, happy_tape, decision


def snapshot(**changes):
    return {**dict(model_calls_remaining=24, search_calls_remaining=8,
                tokens_remaining=256000, seconds_remaining=600,
                max_output_tokens=8192), **changes}


@pytest.mark.parametrize('mode', ['visible', 'bounded'])
def test_budget_visible_to_planners_but_not_verifier(mode):
    rt = runtime(happy_tape())
    result = run_mainline('Where was Alice born?', rt, budget_mode=mode)
    assert result['answer'] == 'Paris' and result['outcome']['status'] == 'verified'
    for event in rt.events:
        if event['kind'] == 'mainline.rag.request':
            data = json.loads(event['prompt'].split('\ndata: ', 1)[1])
            assert ('resource_budget' in data) == (event['role'] in {'Supervisor', 'Researcher'})
    assert rt.model_calls == 8


def test_one_search_budget_finalizes_without_ninth_search_or_skipped_verifier():
    tape = [decision('research'), '{"tool":"search_evidence","arguments":{"query":"Alice"}}',
            '{"claims":[{"text":"Paris","citations":[1]}]}',
            '{"supported":[0],"queries":[],"issues":[],"missing_answerable":[]}', 'Paris']
    rt = runtime(tape, search_calls=1)
    result = run_mainline('Where was Alice born?', rt, budget_mode='bounded')
    assert result['answer'] == 'Paris' and result['outcome']['status'] == 'verified'
    assert rt.search_calls == 1 and rt.model_calls == 5
    assert any(e['kind'] == 'mainline.budget.finalize' for e in rt.events)


def test_rejected_claims_do_not_become_success_on_stopping():
    tape = [decision('research'), '{"tool":"search_evidence","arguments":{"query":"Alice"}}',
            '{"claims":[{"text":"London","citations":[1]}]}',
            '{"supported":[],"queries":[],"issues":["unsupported"],"missing_answerable":[]}']
    rt = runtime(tape, search_calls=1)
    result = run_mainline('Where was Alice born?', rt, budget_mode='bounded')
    assert result['answer'] == '' and result['outcome']['status'] == 'incomplete'
    assert result['termination_reason'] == 'no_verified_claims'
    assert rt.model_calls == 4


def test_no_evidence_stops_without_fabricating_answer():
    rt = runtime([decision('research'),
                  '{"tool":"search_evidence","arguments":{"query":"nonexistentword"}}'], search_calls=1)
    result = run_mainline('Where?', rt, budget_mode='bounded')
    assert result['answer'] == '' and rt.model_calls == 2


def test_no_progress_counts_admitted_ids_not_gold_and_latches():
    control = ResearchBudgetControl(snapshot, bounded=True)
    control.observe_search(['a'])
    control.observe_search(['a'])
    assert not control.research_stop()
    control.observe_search(['a', 'b'])
    assert control.stalled_searches == 0
    control.observe_search(['a'])
    control.observe_search([])
    assert control.research_stop() == 'no_new_admitted_chunks'
    control.observe_search(['c'])
    assert control.research_stop() == 'no_new_admitted_chunks'


def test_repeated_admitted_results_stop_real_graph():
    tape = [decision('research'), *[
        json.dumps({'tool': 'search_evidence', 'arguments': {'query': query}})
        for query in ['Alice', 'Alice Paris', 'Paris']],
        '{"claims":[{"text":"Paris","citations":[1]}]}',
        '{"supported":[0],"queries":[],"issues":[],"missing_answerable":[]}', 'Paris']
    rt = runtime(tape)
    result = run_mainline('Where was Alice born?', rt, budget_mode='bounded')
    assert rt.search_calls == 3 and rt.model_calls == 7
    assert result['answer'] == 'Paris'
    assert result['outcome']['stop_reason'] == 'no_new_admitted_chunks'


@pytest.mark.parametrize('changes,reason', [
    ({'model_calls_remaining': 5}, 'model_calls_reserved'),
    ({'search_calls_remaining': 0}, 'search_budget_reserved'),
    ({'tokens_remaining': 40960}, 'token_reserve_heuristic'),
    ({'seconds_remaining': 30}, 'time_reserve_heuristic'),
])
def test_bounded_thresholds_and_visible_does_not_force_stop(changes, reason):
    reader = lambda: snapshot(**changes)
    assert ResearchBudgetControl(reader, bounded=True).research_stop() == reason
    assert not ResearchBudgetControl(reader).research_stop()


def test_model_reserve_is_checked_after_planning_before_tool_dispatch():
    rt = runtime([decision('research'),
                  '{"tool":"search_evidence","arguments":{"query":"Alice"}}'], model_calls=7)
    result = run_mainline('Where?', rt, budget_mode='bounded')
    assert rt.model_calls == 2 and rt.search_calls == 0
    assert result['answer'] == ''
    assert any(e.get('stage') == 'before_tool' for e in rt.events)


def test_reject_nonfinite_counters():
    with pytest.raises(ValueError, match='Invalid budget'):
        ResearchBudgetControl(lambda: snapshot(tokens_remaining=float('nan'))).snapshot()
