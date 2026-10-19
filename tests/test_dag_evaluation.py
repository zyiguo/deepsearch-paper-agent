from copy import deepcopy
import json

import pytest

from deepsearch_experiments.benchmarks.dag import default_task, score_rows, score_run
from deepsearch_experiments.benchmarks.dag_metrics import evaluate_dag, evaluate_rule
from deepsearch_experiments.benchmarks.dag_schema import BudgetRule, DAGSuite, TaskDAG, ToolRule, ValueRule, fingerprint
from deepsearch_experiments.benchmarks.schema import sha256


def case():
    return {'id': 'q1', 'dataset': 'chartqa', 'split': 'val', 'task': 'chartqa', 'group': 'human',
            'question': 'What is the result?', 'answers': ['10'], 'gold_element_ids': [],
            'scope': ['p1', 'p2'], 'gold_pages': ['p1']}


def row(**changes):
    return {'id': 'q1', 'variant': 'agent_full', 'status': 'ok', 'answer': '10', 'element_ids': [],
            'evidence_ranking': ['p1'], 'context_ranking': ['p1'], 'ranking': ['p1'],
            'trace': {'tool_calls': [], 'model_calls': []}, **changes}


def task(nodes):
    return TaskDAG(case_id='q1', case_sha256=fingerprint(case()), annotation_level='task_rubric', nodes=nodes)


def value_node(ident, expected, **changes):
    return {'id': ident, 'description': ident, 'rule': {'kind': 'value', 'artifact': ident,
            'metric': 'numeric', 'expected': float(expected)}, **changes}


def test_proxy_keeps_correct_raw_answer_but_blocks_unsubstantiated_completion():
    result = evaluate_dag(default_task(case()), case(), row(evidence_ranking=['p2']))
    nodes = {n['id']: n for n in result['nodes']}
    assert result['failure_frontier'] == ['evidence']
    assert nodes['answer']['raw_score'] == 1
    assert nodes['answer']['status'] == 'blocked'
    assert nodes['answer']['blocked_by'] == ['evidence']
    assert not result['success'] and result['score'] == 0
    assert result['raw_score'] == pytest.approx(.8)
    assert nodes['tool_result']['status'] == 'failed'
    assert 'tool_result' not in result['failure_frontier']


def test_unknown_context_is_distinct_from_empty_context_and_does_not_shrink_denominator():
    missing = row()
    del missing['context_ranking']
    suite = DAGSuite(name='test', manifests={}, tasks=[default_task(case())])
    summary, results = score_rows(suite, [case()], [missing], ['agent_full', 'absent'])
    assert results[0]['unknown_nodes'] == ['context']
    assert results[0]['failure_frontier'] == []
    assert 'answer' in results[0]['blocked_nodes']
    assert summary['variants']['absent']['chartqa']['count'] == 1
    assert summary['variants']['absent']['chartqa']['mean_score'] == 0
    assert summary['legacy_metrics']['variants']['agent_full']['by_task']['chartqa']['answer_metrics']['relaxed_accuracy']['value'] == 1
    empty = evaluate_dag(default_task(case()), case(), row(context_ranking=[]))
    assert empty['failure_frontier'] == ['context']


def test_diamond_dag_and_alternative_paths_do_not_require_a_fixed_tool_sequence():
    graph = task([
        value_node('ocr', 10, required=False, weight=0.),
        value_node('vision', 10, required=False, weight=0.),
        {'id': 'read_value', 'description': 'One valid reading route', 'depends_on': ['ocr', 'vision'],
         'dependency_mode': 'any', 'rule': {'kind': 'gate'}, 'weight': .5},
        value_node('other_value', 5, weight=.5),
        value_node('difference', 5, depends_on=['read_value', 'other_value'], weight=1.),
    ])
    result = evaluate_dag(graph, case(), row(artifacts={
        'vision': {'value': 10}, 'other_value': {'value': 5}, 'difference': {'value': 5}}))
    assert result['success'] and result['score'] == 1
    assert result['failure_frontier'] == [] and result['unknown_nodes'] == ['ocr']
    # A plausible final answer cannot bypass a wrong prerequisite.
    bad = evaluate_dag(graph, case(), row(artifacts={
        'vision': {'value': 10}, 'other_value': {'value': 6}, 'difference': {'value': 5}}))
    assert bad['failure_frontier'] == ['other_value']
    assert bad['nodes'][-1]['status'] == 'blocked'


@pytest.mark.parametrize('nodes', [
    [value_node('a', 1, depends_on=['b']), value_node('b', 1, depends_on=['a'])],
    [value_node('a', 1, depends_on=['missing'])],
    [value_node('a', 1), value_node('a', 2)],
    [value_node('a', 1, depends_on=['a'])],
    [{'id': 'gate', 'description': 'invalid', 'rule': {'kind': 'gate'}}],
])
def test_invalid_graphs_rejected(nodes):
    with pytest.raises(ValueError):
        task(nodes)


def test_invalid_weights_thresholds_and_unreviewed_provenance_rejected():
    for changes in ({'weight': float('nan')}, {'weight': -1}, {'threshold': 0}, {'weight': True}):
        with pytest.raises(ValueError):
            task([value_node('a', 1, **changes)])
    graph = task([value_node('a', 1)]).model_dump()
    with pytest.raises(ValueError):
        TaskDAG.model_validate({**graph, 'annotation_status': 'reviewed'})


def test_tool_success_needs_relevant_result_and_token_missingness_is_unknown():
    rule = ToolRule(names=['read_page'], expected_pages=['p1'])
    wrong = {'trace': {'tool_calls': [{'name': 'read_page', 'status': 'ok', 'ranking': ['p2']}]}}
    assert evaluate_rule(rule, case(), wrong)['score'] == 0
    wrong['trace']['tool_calls'][0]['ranking'] = ['p1']
    assert evaluate_rule(rule, case(), wrong)['score'] == 1
    budget = BudgetRule(metric='total_tokens', limit=100.)
    assert evaluate_rule(budget, case(), row(trace={'model_calls': [{'status': 'ok'}]}))['score'] is None
    counted = row(trace={'model_calls': [{'status': 'ok', 'total_tokens': 50}, {'status': 'budget_exceeded'}]})
    assert evaluate_rule(budget, case(), counted)['score'] == 1


def test_intermediate_values_are_scored_by_rules_not_agent_claimed_scores():
    rule = ValueRule(artifact='number', metric='numeric', expected=10., relative_tolerance=.05)
    assert evaluate_rule(rule, case(), row(artifacts={'number': {'value': 10.4, 'score': 0}}))['score'] == 1
    assert evaluate_rule(rule, case(), row(artifacts={'number': {'value': 20, 'score': 1}}))['score'] == 0
    assert evaluate_rule(rule, case(), row(artifacts={'number': {'value': True}}))['score'] == 0
    assert evaluate_rule(rule, case(), row())['reason'] == 'missing_artifact'


def test_wrong_case_fingerprint_duplicate_rows_and_incompatible_metrics_fail_closed():
    graph = default_task(case())
    with pytest.raises(ValueError, match='identity'):
        evaluate_dag(graph, {**case(), 'answers': ['11']}, row())
    suite = DAGSuite(name='test', manifests={}, tasks=[graph])
    with pytest.raises(ValueError, match='Duplicate'):
        score_rows(suite, [case()], [row(), row()], ['agent_full'])
    with pytest.raises(ValueError, match='unplanned'):
        score_rows(suite, [case()], [row(id='other')], ['agent_full'])
    incompatible = task([{'id': 'answer', 'description': 'bad metric',
                          'rule': {'kind': 'answer', 'metric': 'element_f1'}}])
    with pytest.raises(ValueError, match='metric'):
        evaluate_dag(incompatible, case(), None)


def test_offline_run_writes_auditable_results_without_modifying_source(tmp_path, monkeypatch):
    import deepsearch_experiments.benchmarks.dag as module
    run = tmp_path / 'source'
    run.mkdir()
    (run / 'run.json').write_text('{}', encoding='utf-8')
    (run / 'predictions.jsonl').write_text(json.dumps(row()) + '\n', encoding='utf-8')
    before = sha256(run / 'predictions.jsonl')
    suite = DAGSuite(name='test', manifests={}, tasks=[default_task(case())])
    suite_path = tmp_path / 'suite.json'
    suite_path.write_text(suite.model_dump_json(), encoding='utf-8')
    monkeypatch.setattr(module, 'load_population', lambda *args: ([case()], ['agent_full'], {}, {'indices': {}}))
    result = score_run(tmp_path, run, suite_path, tmp_path / 'scored')
    assert result['variants']['agent_full']['chartqa']['success_count'] == 1
    assert sha256(run / 'predictions.jsonl') == before
    assert 'flowchart LR' in (tmp_path / 'scored/report.md').read_text(encoding='utf-8')
    record = json.loads((tmp_path / 'scored/node-results.jsonl').read_text(encoding='utf-8'))
    assert record['trace_locator'] == 'predictions.jsonl#L1'
    assert len(result['provenance']['evaluator_sha256']) == 64
    with pytest.raises(ValueError, match='Output exists'):
        score_run(tmp_path, run, suite_path, tmp_path / 'scored')
