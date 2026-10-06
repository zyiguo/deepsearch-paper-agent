import json

import pytest

from deepsearch_agent.benchmarks.badcases import attribute_case, attribute_cases


def case(**changes):
    return {'id': 'q1', 'dataset': 'chartqa', 'task': 'chartqa', 'answers': ['42'],
            'gold_element_ids': [], 'scope': ['p1', 'p2'], 'gold_pages': ['p1'], **changes}


def row(**changes):
    return {'id': 'q1', 'variant': 'agentic', 'status': 'ok', 'prediction_status': 'ok',
            'answer': '41', 'element_ids': [], 'ranking': ['p1'],
            'evidence_ranking': ['p1'], 'trace': {}, **changes}


def outcome(claim_answer, supported):
    claims = [{'text': json.dumps({'answer': claim_answer}), 'citations': [1]}]
    return [{'kind': 'rag.parsed', 'payload': {'role': 'Answerer', 'value': {'claims': claims}}},
            {'kind': 'rag.outcome', 'payload': {'status': 'verified' if supported else 'incomplete',
                                             'claims': claims, 'supported': supported}}]


def test_dataset_metric_and_annotation_are_explicit():
    result = attribute_case(case(), row(answer='43'))
    assert result['is_correct'] is True  # ChartQA 5% relative tolerance.
    assert result['primary_code'] == 'success'
    assert result['score']['relaxed_accuracy'] == 1
    assert result['automatic']['primary_code'] == 'success'
    assert result['expert_override'] is None


def test_task_b_uses_element_exact_match_not_partial_f1():
    result = attribute_case(case(task='B', dataset='pdfvqa', answers=[], gold_element_ids=['a', 'b']),
                            row(answer='', element_ids=['a']))
    assert result['is_correct'] is False
    assert result['score']['element_f1'] == pytest.approx(2 / 3)


def test_recovered_tool_failure_does_not_become_primary():
    result = attribute_case(case(), row(answer='1', trace={'tool_calls': [
        {'name': 'search_evidence', 'status': 'error', 'error_code': 'invalid_arguments'},
        {'name': 'search_evidence', 'status': 'ok', 'ranking': ['p1']}]}))
    assert result['primary_code'] == 'generation_reading_or_reasoning'
    assert 'recovered_tool_failure' in result['secondary_codes']


def test_success_preserves_recovered_failures_without_counting_as_badcase():
    result = attribute_case(case(), row(answer='42', trace={'model_calls': [
        {'role': 'Researcher', 'status': 'error', 'error_type': 'TimeoutError'},
        {'role': 'Researcher', 'status': 'ok'}]}))
    assert result['primary_code'] == 'success'
    assert 'recovered_model_failure' in result['secondary_codes']


def test_retrieval_miss_requires_observed_search_and_no_gold_recall():
    result = attribute_case(case(), row(answer='1', ranking=['p2'], evidence_ranking=['p2'],
        trace={'retrieval_calls': [{'query': 'q', 'ranking': ['p2'], 'status': 'ok'}]}))
    assert result['primary_code'] == 'retrieval_miss'
    assert result['evidence_level'] == 'confirmed'


def test_rerank_drop_is_not_reported_as_retrieval_miss():
    result = attribute_case(case(), row(answer='1', ranking=['p2'], evidence_ranking=['p2'], trace={
        'retrieval_calls': [{'ranking': ['p1', 'p2'], 'status': 'ok'}],
        'rerank_calls': [{'initial_ranking': ['p1', 'p2'], 'ranking': ['p2'], 'status': 'ok'}]}))
    assert result['primary_code'] == 'rerank_drop'


def test_recovered_rerank_drop_is_only_secondary():
    result = attribute_case(case(), row(answer='1', trace={
        'rerank_calls': [{'initial_ranking': ['p1', 'p2'], 'ranking': ['p2'], 'status': 'ok'}],
        'tool_calls': [{'name': 'read_page', 'status': 'ok', 'ranking': ['p1']}]}))
    assert result['primary_code'] == 'generation_reading_or_reasoning'
    assert 'recovered_rerank_drop' in result['secondary_codes']


def test_retrieved_gold_not_in_final_evidence_is_selection_loss():
    result = attribute_case(case(), row(answer='1', ranking=['p2'], evidence_ranking=['p1', 'p2']))
    assert result['primary_code'] == 'evidence_selection_loss'


def test_partial_recall_is_not_a_retrieval_miss():
    result = attribute_case(case(gold_pages=['p1', 'p2']), row(answer='1'))
    assert result['primary_code'] == 'generation_reading_or_reasoning'
    assert 'partial_gold_coverage' in result['secondary_codes']
    assert result['manual_review_required'] is True


def test_missing_trace_does_not_invent_retrieval_failure():
    result = attribute_case(case(), {'id': 'q1', 'status': 'ok', 'answer': '1'})
    assert result['primary_code'] == 'unknown'
    assert result['evidence_level'] == 'needs_review'


def test_invalid_final_json_is_format_failure_when_not_abstention():
    result = attribute_case(case(), row(status='invalid_output', prediction_status='invalid_json', answer=''))
    assert result['primary_code'] == 'model_format_error'


def test_last_correct_draft_rejected_by_verifier_is_identified():
    result = attribute_case(case(), row(status='invalid_output', prediction_status='no_prediction', answer='',
        ranking=[], trace={'events': outcome('42', [])}))
    assert result['primary_code'] == 'verifier_rejected_correct'
    assert result['evidence_level'] == 'confirmed'
    assert result['manual_review_required'] is True
    assert any('/trace/events/1' in p for p in result['evidence'])


def test_accepted_wrong_draft_is_secondary_not_truth_verification():
    result = attribute_case(case(), row(answer='1', trace={'events': outcome('1', [0])}))
    assert result['primary_code'] == 'generation_reading_or_reasoning'
    assert 'verifier_accepted_wrong' in result['secondary_codes']


def test_observed_empty_draft_is_abstention_not_format_error():
    result = attribute_case(case(), row(status='invalid_output', answer='', ranking=[], trace={'events': [
        {'kind': 'rag.outcome', 'payload': {'status': 'incomplete', 'claims': [], 'supported': []}}]}))
    assert result['primary_code'] == 'abstention'


def test_earlier_correct_draft_does_not_blame_later_verifier():
    events = outcome('42', [])[:1] + outcome('1', [])
    result = attribute_case(case(), row(answer='', status='invalid_output', ranking=[], trace={'events': events}))
    assert result['primary_code'] == 'abstention'
    assert result['primary_code'] != 'verifier_rejected_correct'


@pytest.mark.parametrize('error_type,primary', [('TimeoutError', 'transport_failure'),
    ('FileNotFoundError', 'asset_failure'), ('ManifestValidationError', 'schema_data_failure')])
def test_terminal_runtime_errors_have_explicit_categories(error_type, primary):
    result = attribute_case(case(), row(status='error', answer='', error_type=error_type))
    assert result['primary_code'] == primary


def test_tool_argument_error_requires_terminal_evidence():
    trace = {'tool_calls': [{'name': 'read_page', 'status': 'error', 'error_code': 'invalid_arguments'}]}
    result = attribute_case(case(), row(answer='1', trace=trace))
    assert result['primary_code'] == 'generation_reading_or_reasoning'
    terminal = attribute_case(case(), row(status='error', answer='', error_type='ToolError', trace=trace))
    assert terminal['primary_code'] == 'tool_argument_invalid'


def test_budget_exhaustion_requires_a_stopped_answer_not_just_observed_limit():
    trace = {'events': [{'kind': 'rag.outcome', 'payload': {'status': 'incomplete',
        'stop_reason': 'call_budget', 'claims': [], 'supported': []}}]}
    assert attribute_case(case(), row(status='invalid_output', answer='', trace=trace))['primary_code'] == 'budget_exhausted'
    assert attribute_case(case(), row(answer='42', trace=trace))['primary_code'] == 'success'


def test_oracle_success_does_not_prove_ocr_failure():
    result = attribute_case(case(), row(answer='1'), oracle_row=row(answer='42'))
    assert result['oracle_correct'] is True
    assert result['primary_code'] == 'generation_reading_or_reasoning'
    assert 'oracle_correct_rag_failed' in result['secondary_codes']
    assert result['manual_review_required'] is True


def test_empty_gold_element_set_is_not_scored_correct_on_runtime_error():
    result = attribute_case(case(task='B', answers=[], gold_element_ids=[]), row(status='error', answer='', element_ids=[]))
    assert result['is_correct'] is False
    assert result['score']['element_exact_match'] == 0


def test_case_list_preserves_variants_and_joins_oracle_by_id():
    rows = [row(variant='fixed', answer='42'), row(variant='agentic', answer='1')]
    results = attribute_cases([case()], rows, oracle_rows=[row(answer='42')])
    assert [r['variant'] for r in results] == ['fixed', 'agentic']
    assert all(r['oracle_correct'] is True for r in results)
    with pytest.raises(ValueError, match='Duplicate'):
        attribute_cases([case()], [row(), row()])
    with pytest.raises(ValueError, match='Unknown'):
        attribute_cases([case()], [row(id='other')])


def test_missing_prediction_is_visible():
    result = attribute_cases([case()], [])
    assert result[0]['primary_code'] == 'missing_result'


def test_missing_case_variant_pair_is_visible():
    result = attribute_cases([case(), case(id='q2')],
                             [row(variant='fixed'), row(variant='agentic'), row(id='q2', variant='fixed')])
    assert len(result) == 4
    assert result[-1]['id'] == 'q2' and result[-1]['variant'] == 'agentic'
    assert result[-1]['primary_code'] == 'missing_result'


def test_element_coverage_does_not_claim_ocr_error_when_raw_gold_page_present():
    result = attribute_case(case(task='B', answers=[], gold_element_ids=['a', 'b']),
        row(answer='', element_ids=['a'], retrieved_element_ids=['a', 'b'], evidence_element_ids=['a']))
    assert result['element_coverage'] == {'retrieved': 1., 'admitted': .5, 'context': None}
    assert result['primary_code'] == 'generation_reading_or_reasoning'
    assert 'incomplete_text_element_coverage' in result['secondary_codes']


def test_explicit_abstention_without_outcome_is_not_format_failure():
    result = attribute_case(case(), row(answer='', prediction_status='abstained'))
    assert result['primary_code'] == 'abstention'


def test_unrelated_terminal_exception_does_not_reuse_earlier_model_error():
    result = attribute_case(case(), row(status='error', error_type='RuntimeError', answer='', trace={
        'model_calls': [{'status': 'error', 'error_type': 'TimeoutError'}]}))
    assert result['primary_code'] == 'unknown'


def test_abstention_keeps_observed_retrieval_miss_secondary():
    result = attribute_case(case(), row(answer='', prediction_status='abstained', ranking=[],
        evidence_ranking=['p2'], trace={'retrieval_calls': [{'ranking': ['p2'], 'status': 'ok'}]}))
    assert result['primary_code'] == 'abstention'
    assert 'retrieval_miss_observed' in result['secondary_codes']


def test_abstention_keeps_observed_unrecovered_rerank_drop_secondary():
    result = attribute_case(case(), row(answer='', prediction_status='abstained', ranking=[],
        evidence_ranking=['p2'], trace={'rerank_calls': [
            {'initial_ranking': ['p1', 'p2'], 'ranking': ['p2'], 'status': 'ok_multimodal'}]}))
    assert result['primary_code'] == 'abstention'
    assert 'rerank_drop_observed' in result['secondary_codes']
    assert 'retrieval_miss_observed' not in result['secondary_codes']


def test_collected_gold_not_in_observed_actual_context_is_selection_loss():
    result = attribute_case(case(), row(answer='1', context_ranking=['p2'], original_image_pages=['p2']))
    assert result['primary_code'] == 'context_selection_loss'
    assert result['gold_coverage']['collected'] == 1.
    assert result['gold_coverage']['context'] == 0.
    assert result['gold_coverage']['original_images'] == 0.
    assert result['taxonomy_version'] == '1.1'


def test_context_loss_requires_actual_context_observation():
    result = attribute_case(case(), row(answer='1'))
    assert result['primary_code'] == 'generation_reading_or_reasoning'
    assert result['gold_coverage']['context'] is None


def test_context_loss_does_not_assume_unobserved_images_are_absent():
    result = attribute_case(case(), row(answer='1', context_ranking=[]))
    assert result['primary_code'] != 'context_selection_loss'
    assert result['gold_coverage']['original_images'] is None


def test_gold_original_image_prevents_false_context_selection_loss():
    result = attribute_case(case(), row(answer='1', context_ranking=[], original_image_pages=['p1']))
    assert result['primary_code'] == 'generation_reading_or_reasoning'


def test_no_visual_intervention_is_observed_not_an_ocr_causal_claim():
    result = attribute_case(case(), row(answer='1', variant='agent_no_visual', context_ranking=['p1'],
                                       original_image_pages=[]))
    assert result['primary_code'] == 'generation_reading_or_reasoning'
    assert 'visual_input_disabled' in result['secondary_codes']
    assert result['manual_review_required'] is True


def test_actual_text_element_coverage_is_distinct_from_collected_elements():
    result = attribute_case(case(task='B', answers=[], gold_element_ids=['a', 'b']),
        row(answer='', element_ids=['a'], evidence_element_ids=['a', 'b'], context_element_ids=['a']))
    assert result['element_coverage']['admitted'] == 1.
    assert result['element_coverage']['context'] == .5
    assert 'incomplete_context_element_coverage' in result['secondary_codes']
