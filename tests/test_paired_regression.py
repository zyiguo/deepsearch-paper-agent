from copy import deepcopy

import pytest

from deepsearch_experiments.benchmarks import ablation
from deepsearch_experiments.benchmarks.paired_regression import compare_runs


def case():
    return {'id': 'q1', 'dataset': 'pdfvqa', 'task': 'A', 'answers': ['yes'],
            'gold_pages': ['p1'], 'scope': ['p1'], 'gold_element_ids': []}


def identity():
    return {'code_sha256': 'code1', 'manifests': {'pdfvqa': 'data1'},
            'indices': {'pdfvqa': 'index1'}, 'case_ids': ['q1'],
            'variants': ['agent_full'], 'configuration': {'model': 'old', 'max_output_tokens': 2048}}


def row(answer='yes', **changes):
    return {'id': 'q1', 'variant': 'agent_full', 'answer': answer, 'status': 'ok',
            'prediction_status': 'valid', 'element_ids': [], 'trace': {}, **changes}


def test_comparison_separates_improvements_regressions_and_missing():
    a = identity()
    report = compare_runs([case()], a, [row('no')], a, [row()])
    assert report['transitions'] == {'improved': 1}
    assert report['tasks']['A']['before_correct'] == 0
    assert report['tasks']['A']['after_correct'] == 1
    assert report['pairs'][0]['before_code'] != 'success'
    assert compare_runs([case()], a, [row()], a, [row('no')])['transitions'] == {'regressed': 1}
    missing = compare_runs([case()], a, [], a, [row()])
    assert missing['transitions'] == {'missing_before': 1}
    assert missing['complete_pairs'] == 0


def test_changed_conditions_are_explicit_and_data_mismatch_is_rejected():
    before, after = identity(), identity()
    after['configuration']['model'] = 'new'
    after['configuration']['max_output_tokens'] = 32768
    after['code_sha256'] = 'code2'
    result = compare_runs([case()], before, [row()], after, [row()])
    assert set(result['changed_conditions']) == {'code_sha256', 'configuration.model',
                                                'configuration.max_output_tokens'}
    assert result['causal_claim_supported'] is False
    for key in ('manifests', 'indices'):
        changed = deepcopy(after)
        changed[key]['pdfvqa'] = 'different'
        with pytest.raises(ValueError):
            compare_runs([case()], before, [row()], changed, [row()])


def test_duplicate_predictions_and_unplanned_population_rejected():
    a = identity()
    with pytest.raises(ValueError):
        compare_runs([case()], a, [row(), row()], a, [row()])
    b = deepcopy(a)
    b['case_ids'] = ['missing']
    with pytest.raises(ValueError):
        compare_runs([case()], a, [row()], b, [])


def test_error_empty_gold_is_not_improvement_and_unknown_usage_is_null():
    c = {**case(), 'task': 'B', 'answers': [], 'gold_element_ids': []}
    a = identity()
    report = compare_runs([c], a, [row(status='error')], a, [row(status='error')])
    assert report['transitions'] == {'still_wrong': 1}
    assert report['pairs'][0]['after_total_tokens'] is None


def test_subset_selection_validates_and_preserves_manifest_order():
    c = case()
    selected, variants = ablation.select_run([c, {**c, 'id': 'q2'}], ['q2'], ['agent_full'])
    assert [item['id'] for item in selected] == ['q2']
    assert variants == ['agent_full']
    for ids, variants in [(['bad'], None), (['q1', 'q1'], None), (None, ['bad'])]:
        with pytest.raises(ValueError):
            ablation.select_run([c], ids, variants)


def test_score_uses_saved_subset_not_entire_manifest(monkeypatch, tmp_path):
    import json
    from deepsearch_experiments.benchmarks import ablation_metrics
    c = case()
    manifest = {'cases': [c, {**c, 'id': 'q2'}]}
    monkeypatch.setattr(ablation, 'load_manifest', lambda path: manifest if 'pdfvqa' in str(path) else {'cases': []})
    (tmp_path / 'run.json').write_text(json.dumps(identity()), encoding='utf-8')
    ablation.write_rows(tmp_path / 'predictions.jsonl', [row()])
    def summarize(cases, rows, *, k, variants):
        assert [c['id'] for c in cases] == ['q1']
        assert variants == ['agent_full']
        return {'selected_count': len(cases)}
    monkeypatch.setattr(ablation_metrics, 'summarize_ablation', summarize)
    report = ablation.score(tmp_path, tmp_path)
    assert report == {'selected_count': 1}


def test_matched_runs_report_only_complete_token_usage():
    a = identity()
    trace = {'model_calls': [{'status': 'ok', 'total_tokens': 10},
                             {'status': 'budget_exceeded', 'total_tokens': None}]}
    report = compare_runs([case()], a, [row(trace=trace)], a, [row(trace=trace)])
    assert report['pairs'][0]['after_total_tokens'] == 10
    trace['model_calls'].append({'status': 'error', 'total_tokens': None})
    report = compare_runs([case()], a, [row(trace=trace)], a, [row(trace=trace)])
    assert report['pairs'][0]['after_total_tokens'] is None


def test_benchmark_correct_does_not_erase_incomplete_graph_status():
    a = identity()
    trace = {'events': [{'kind': 'rag.outcome', 'payload': {'status': 'incomplete', 'stop_reason': 'review_limit'}}]}
    report = compare_runs([case()], a, [row()], a, [row(trace=trace)])
    assert report['pairs'][0]['transition'] == 'still_correct'
    assert report['pairs'][0]['after_graph_status'] == 'incomplete'
    assert report['pairs'][0]['after_stop_reason'] == 'review_limit'
