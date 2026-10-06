import json

import pytest

from deepsearch_agent.benchmarks.ablation_metrics import summarize_ablation


def case(ident='q1', task='chartqa', gold=None):
    return {'id': ident, 'dataset': 'chartqa' if task == 'chartqa' else 'pdfvqa',
            'task': task, 'group': 'human' if task == 'chartqa' else 'structural',
            'answers': ['10'] if task == 'chartqa' else ['yes'],
            'gold_element_ids': ['e1', 'e2'] if task == 'B' else [],
            'gold_pages': gold or ['p1'], 'scope': ['p1', 'p2']}


def row(ident='q1', variant='agent_full', **changes):
    return {'id': ident, 'variant': variant, 'status': 'ok', 'answer': '10',
            'element_ids': [], 'ranking': ['p1'], 'evidence_ranking': ['p1'],
            'trace': {}, **changes}


def test_missing_and_failed_answers_stay_in_denominator_without_mixing_tasks():
    cases = [case('q1'), case('q2'), case('q3'), case('a', 'A'), case('b', 'B')]
    rows = [row(), row('q2', status='error'), row('a', answer='yes'),
            row('b', answer='', element_ids=['e1'])]
    report = summarize_ablation(cases, rows, variants=['agent_full', 'absent'])
    full = report['variants']['agent_full']
    score = full['by_task']['chartqa']['answer_metrics']['relaxed_accuracy']
    assert score['value'] == 1 / 3 and score['denominator'] == 3
    assert full['by_task']['A']['answer_metrics']['accuracy']['value'] == 1
    assert full['by_task']['B']['answer_metrics']['element_f1']['value'] == 2 / 3
    assert 'answer_metrics' not in full
    assert full['missing_count'] == 1 and full['error_count'] == 1
    assert report['variants']['absent']['missing_count'] == 5
    assert report['variants']['absent']['by_task']['chartqa']['answer_metrics']['relaxed_accuracy']['value'] == 0


@pytest.mark.parametrize('rows', [
    [row(), row()], [row('unknown')], [row(variant='')], [row(), row('q2', variant=None)],
])
def test_reject_duplicate_unknown_and_empty_variant_rows(rows):
    with pytest.raises(ValueError):
        summarize_ablation([case()], rows)


def test_reject_duplicate_cases_and_unknown_variants():
    with pytest.raises(ValueError):
        summarize_ablation([case(), case()], [])
    with pytest.raises(ValueError):
        summarize_ablation([case()], [row()], variants=['other'])


def test_stage_metrics_distinguish_candidates_ranks_tools_and_citation_proxies():
    trace = {
        'retrieval_calls': [{'status': 'ok', 'ranking': ['p2', 'p1', 'p1']}],
        'rerank_calls': [{'status': 'ok', 'initial_ranking': ['p2', 'p1'], 'ranking': ['p1', 'p2']}],
        'tool_calls': [{'name': 'read_page', 'arguments': {'page': 1}, 'status': 'ok',
                        'ranking': ['p1'], 'new_chunk_ids': ['c1']}],
    }
    result = summarize_ablation([case()], [row(ranking=['p1', 'p2'], trace=trace)], k=1)['variants']['agent_full']
    retrieval = result['retrieval']
    assert retrieval['candidate_recall']['value'] == 1
    assert retrieval['first_search_recall_at_k']['value'] == 0
    assert retrieval['post_tool_gold_hit']['value'] == 1
    assert retrieval['final_citation_page_precision']['value'] == .5
    assert retrieval['final_citation_page_coverage']['value'] == 1
    assert result['rerank']['before_recall_at_k']['value'] == 0
    assert result['rerank']['after_recall_at_k']['value'] == 1
    assert result['rerank']['aggregation'] == 'call_weighted_diagnostic'


def test_tool_metrics_count_repeats_with_canonical_arguments_and_unknown_rankings():
    calls = [
        {'name': 'search_evidence', 'arguments': {'query': 'x', 'k': 3}, 'status': 'ok',
         'ranking': ['p1'], 'new_chunk_ids': ['c1']},
        {'name': 'search_evidence', 'arguments': {'k': 3, 'query': 'x'}, 'status': 'ok',
         'ranking': [], 'new_chunk_ids': []},
        {'name': 'search_evidence', 'arguments': {'query': 'y'}, 'status': 'error',
         'error_code': 'invalid_arguments'},
        {'name': 'list_documents', 'arguments': {}, 'status': 'ok', 'ranking': []},
    ]
    result = summarize_ablation([case()], [row(trace={'tool_calls': calls})])['variants']['agent_full']['tools']
    search = result['search_evidence']
    assert search['attempts'] == 3 and search['success_count'] == 2
    assert search['error_count'] == 1 and search['invalid_count'] == 1
    assert search['repeated_identical_count'] == 1 and search['new_evidence_calls'] == 1
    assert search['gold_page_hit']['denominator'] == 2 and search['gold_page_hit']['value'] == .5
    assert result['list_documents']['gold_page_hit']['value'] is None


def test_token_and_latency_missingness_is_not_silent_zero_and_json_is_finite():
    rows = [row(trace={'model_calls': [
        {'stage': 'planner', 'status': 'ok', 'latency_ms': 12., 'prompt_tokens': 10,
         'completion_tokens': 4, 'total_tokens': 14, 'image_count': 1},
        {'stage': 'answer', 'status': 'error', 'total_tokens': None, 'image_count': 0},
    ]}, latency_ms=20), row('q2', latency_ms=None)]
    cost = summarize_ablation([case(), case('q2')], rows)['variants']['agent_full']['costs']
    assert cost['model_calls'] == 2
    assert cost['tokens']['total_tokens'] == {'sum': 14, 'reported_calls': 1, 'missing_calls': 1}
    assert cost['latency_ms']['observed_count'] == 1 and cost['latency_ms']['mean'] == 20
    assert cost['by_stage']['answer']['tokens']['total_tokens']['sum'] is None
    json.dumps(cost, allow_nan=False)


def test_conditional_answer_scores_and_verifier_do_not_claim_ground_truth_faithfulness():
    outcome = {'kind': 'rag.outcome', 'payload': {'status': 'incomplete',
               'claims': [{'text': '{"answer":"10","element_ids":[]}', 'citations': [1]}],
               'supported': []}}
    result = summarize_ablation([case(), case('q2')], [
        row(answer='', trace={'events': [outcome]}),
        row('q2', answer='10', evidence_ranking=['p2']),
    ])['variants']['agent_full']
    conditional = result['by_task']['chartqa']['answer_when_gold_available']['relaxed_accuracy']
    assert conditional['denominator'] == 1 and conditional['value'] == 0
    verifier = result['verifier']
    assert verifier['accepted_claims']['denominator'] == 1
    assert verifier['accepted_claims']['value'] == 0
    assert verifier['correct_draft_final_incorrect'] == 1
    assert verifier['rejected_correct_json_claims'] == 1
    assert verifier['interpretation'] == 'model_self_check_not_ground_truth'


def test_paired_differences_use_fixed_cases_and_keep_missing_zero():
    cases = [case('q1'), case('q2'), case('a', 'A')]
    rows = [row(), row('q2', answer='wrong'), row('a', answer='yes'),
            row('q1', variant='agent_no_ocr', answer='wrong'),
            row('q2', variant='agent_no_ocr', answer='10')]
    report = summarize_ablation(cases, rows)
    chart = report['paired_vs_agent_full']['agent_no_ocr']['by_task']['chartqa']['relaxed_accuracy']
    assert chart['improved'] == 1 and chart['worse'] == 1 and chart['tied'] == 0
    assert chart['difference'] == 0 and chart['exact_sign_test_p'] == 1
    task_a = report['paired_vs_agent_full']['agent_no_ocr']['by_task']['A']['accuracy']
    assert task_a['difference'] == -1 and task_a['observed_both_count'] == 0
    assert task_a['case_count'] == 1


def test_invalid_prediction_does_not_score_true_from_unvalidated_output():
    result = summarize_ablation([case()], [row(status='invalid_output', answer='10')])['variants']['agent_full']
    assert result['by_task']['chartqa']['answer_metrics']['relaxed_accuracy']['value'] == 0
    assert result['invalid_output_count'] == 1


def test_abstention_never_gets_empty_element_set_credit():
    value = case('b', 'B')
    value['gold_element_ids'] = []
    result = summarize_ablation([value], [row('b', answer='', prediction_status='abstained')])['variants']['agent_full']
    assert result['by_task']['B']['answer_metrics']['element_exact_match']['value'] == 0
    assert result['by_task']['B']['answer_metrics']['element_f1']['value'] == 0
    assert result['abstained_count'] == 1


def test_element_text_coverage_excludes_unobserved_rows_and_scalar_tasks():
    cases = [case('b1', 'B'), case('b2', 'B'), case('b3', 'B'), case('a', 'A')]
    rows = [row('b1', answer='', element_ids=['e1'], retrieved_element_ids=['e1'],
                evidence_element_ids=['e1', 'e2']),
            row('b2', answer='', element_ids=[], retrieved_element_ids=[], evidence_element_ids=[]),
            row('b3', answer='', element_ids=[]),
            row('a', answer='yes', retrieved_element_ids=['e1'], evidence_element_ids=['e1'])]
    report = summarize_ablation(cases, rows)['variants']['agent_full']
    coverage = report['element_text_coverage']
    assert coverage['eligible_cases'] == 3
    assert coverage['retrieved_recall']['value'] == .25 and coverage['retrieved_recall']['denominator'] == 2
    assert coverage['evidence_recall']['value'] == .5 and coverage['evidence_missing_count'] == 1
    assert coverage['interpretation'] == 'catalog_ids_in_text_not_image_readability'
    assert report['by_task']['A']['element_text_coverage']['eligible_cases'] == 0


def test_malformed_success_rows_are_reported_as_invalid_not_success():
    result = summarize_ablation([case()], [row(answer=['10'])])['variants']['agent_full']
    assert result['ok_count'] == 0 and result['invalid_output_count'] == 1


def test_budget_rejection_is_attempt_not_cloud_request_or_unknown_billed_usage():
    trace = {'model_calls': [{'stage': 'planner', 'status': 'ok', 'total_tokens': 12},
                             {'stage': 'planner', 'status': 'budget_exceeded', 'total_tokens': None}]}
    costs = summarize_ablation([case()], [row(trace=trace)])['variants']['agent_full']['costs']
    assert costs['model_attempts'] == 2 and costs['model_calls'] == 1 and costs['budget_blocked_calls'] == 1
    assert costs['tokens']['total_tokens'] == {'sum': 12, 'reported_calls': 1, 'missing_calls': 0}
