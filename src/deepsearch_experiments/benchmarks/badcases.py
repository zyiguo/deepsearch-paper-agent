"""Deterministic stage attribution from benchmark labels and recorded observations."""
from copy import deepcopy
import json

from deepsearch_experiments.benchmarks.metrics import answer_scores
from deepsearch_experiments.benchmarks.schema import validate_prediction


SCHEMA_VERSION = 1
TAXONOMY_VERSION = '1.1'
PRIMARY_STAGES = {
    'success': 'complete', 'missing_result': 'evaluation',
    'schema_data_failure': 'data', 'asset_failure': 'asset',
    'transport_failure': 'runtime', 'budget_exhausted': 'budget',
    'model_format_error': 'output_format', 'tool_argument_invalid': 'tool',
    'tool_execution_failure': 'tool', 'retrieval_miss': 'retrieval',
    'rerank_drop': 'rerank', 'evidence_selection_loss': 'evidence_selection',
    'context_selection_loss': 'context_selection',
    'generation_reading_or_reasoning': 'generation',
    'verifier_rejected_correct': 'verification', 'abstention': 'abstention',
    'unknown': 'unknown',
}


def _list(value):
    return value if isinstance(value, list) else []


def _pages(value):
    return list(dict.fromkeys(p for p in _list(value) if isinstance(p, str)))


def _prediction_score(case, row):
    metrics = {'chartqa': 'relaxed_accuracy', 'A': 'accuracy',
               'B': 'element_exact_match', 'C': 'element_exact_match'}
    metric = metrics.get(case.get('task'))
    if metric is None:
        return {}, False, False, None
    try:
        answer_scores(case, {})
        prediction = validate_prediction(case, {key: row[key] for key in ('answer', 'element_ids') if key in row})
        scores = answer_scores(case, prediction.model_dump())
        valid = row.get('status', 'ok') == 'ok' and row.get('prediction_status', 'ok') in {'ok', 'valid', 'success'}
        if not valid:
            scores = {key: 0. for key in scores}
        return scores, valid and scores[metric] == 1., valid, metric
    except (KeyError, TypeError, ValueError, AttributeError):
        try:
            empty = answer_scores(case, {})
        except (KeyError, TypeError, ValueError, AttributeError):
            empty = {metric: 0.}
        return {key: 0. for key in empty}, False, False, metric


def _claim_results(case, claims):
    result = {}
    for index, claim in enumerate(_list(claims)):
        if not isinstance(claim, dict) or not isinstance(claim.get('text'), str):
            continue
        try:
            payload = json.loads(claim['text'])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            _, correct, valid, _ = _prediction_score(case, payload)
            if valid:
                result[index] = correct
    return result


def _last_event(events, kind):
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if isinstance(event, dict) and event.get('kind') == kind and isinstance(event.get('payload'), dict):
            return index, event['payload']
    return None, {}


def _error_kind(error):
    text = str(error or '').lower()
    if any(code in text for code in ('manifestvalidation', 'datasetvalidation', 'schemadata', 'invalid_manifest')):
        return 'schema_data_failure'
    if any(code in text for code in ('filenotfound', 'asset_missing', 'asset_checksum', 'assetvalidation', 'unidentifiedimage')):
        return 'asset_failure'
    if any(code in text for code in ('timeout', 'connecterror', 'connectionerror', 'httpstatuserror',
                                    'transporterror', 'ratelimit', 'remoteprotocol', 'networkerror')):
        return 'transport_failure'
    if 'budget' in text or 'recursionlimit' in text or 'graphrecursion' in text:
        return 'budget_exhausted'
    return None


def attribute_case(case, row, *, oracle_row=None, k=3):
    """Return one versioned annotation without guessing hidden OCR/causal errors.

    ``ranking`` is final cited pages; ``evidence_ranking`` is collected pages.
    Actual Answerer text context and image inputs are independent observations.
    Retrieval misses use all observed candidates, not an arbitrary top-k cutoff.
    ``k`` controls the auxiliary final-page coverage diagnostic only.
    """
    if type(k) is not int or k < 1:
        raise ValueError('k must be a positive integer')
    row_missing = row is None
    row = row or {}
    score, correct, valid_prediction, metric = _prediction_score(case, row)
    oracle_correct = _prediction_score(case, oracle_row)[1] if oracle_row is not None else None
    trace = row.get('trace') if isinstance(row.get('trace'), dict) else {}
    events = _list(trace.get('events'))
    models = _list(trace.get('model_calls'))
    tools = _list(trace.get('tool_calls'))
    retrieval = _list(trace.get('retrieval_calls'))
    reranks = _list(trace.get('rerank_calls'))
    gold = set(_pages(case.get('gold_pages')))
    admitted = set(_pages(row.get('evidence_ranking')))
    final = set(_pages(row.get('ranking')))
    context = set(_pages(row.get('context_ranking')))
    original_images = set(_pages(row.get('original_image_pages')))
    context_known = isinstance(row.get('context_ranking'), list)
    images_known = isinstance(row.get('original_image_pages'), list)
    gathered_known = isinstance(row.get('evidence_ranking'), list)
    final_known = isinstance(row.get('ranking'), list)
    retrieved, retrieval_known = set(), False
    secondary, details, pointers = [], [], []

    def note(code, pointer, evidence_level='confirmed'):
        if code not in secondary:
            secondary.append(code)
        details.append({'code': code, 'evidence_level': evidence_level, 'evidence': [pointer]})

    for index, call in enumerate(retrieval):
        if isinstance(call, dict) and call.get('status', 'ok') == 'ok' and isinstance(call.get('ranking'), list):
            retrieval_known = True
            retrieved.update(_pages(call['ranking']))
    for index, call in enumerate(tools):
        if not isinstance(call, dict):
            continue
        if call.get('status') == 'ok':
            admitted.update(_pages(call.get('ranking')))
            if call.get('name') == 'search_evidence' and isinstance(call.get('ranking'), list):
                retrieval_known = True
                retrieved.update(_pages(call['ranking']))
            continue
        if call.get('status') != 'error':
            continue
        recovered = correct or any(isinstance(later, dict) and later.get('name') == call.get('name')
                                   and later.get('status') == 'ok' for later in tools[index + 1:])
        note('recovered_tool_failure' if recovered else 'tool_failure_observed',
             f'/trace/tool_calls/{index}/status=error', 'confirmed' if recovered else 'needs_review')
    for index, call in enumerate(models):
        if not isinstance(call, dict) or call.get('status') != 'error':
            continue
        recovered = correct or any(isinstance(later, dict) and later.get('status') == 'ok'
            and (later.get('role') or later.get('stage')) == (call.get('role') or call.get('stage'))
            for later in models[index + 1:])
        note('recovered_model_failure' if recovered else 'model_failure_observed',
             f'/trace/model_calls/{index}/status=error', 'confirmed' if recovered else 'needs_review')

    drops = []
    for index, call in enumerate(reranks):
        if not isinstance(call, dict) or call.get('status', 'ok') not in {'ok', 'success', 'reranked', 'ok_multimodal'}:
            continue
        if isinstance(call.get('initial_ranking'), list):
            retrieval_known = True
            retrieved.update(_pages(call['initial_ranking']))
        if gold.intersection(_pages(call.get('initial_ranking'))) and isinstance(call.get('ranking'), list) \
                and not gold.intersection(_pages(call['ranking'])):
            drops.append(index)
    observed_evidence = admitted | final | context | original_images
    if drops and gold.intersection(observed_evidence):
        for index in drops:
            note('recovered_rerank_drop', f'/trace/rerank_calls/{index}/ranking')
    elif drops and gathered_known:
        for index in drops:
            note('rerank_drop_observed', f'/trace/rerank_calls/{index}/ranking')
    if gold and retrieval_known and not gold.intersection(retrieved | observed_evidence):
        note('retrieval_miss_observed', '/trace/retrieval_calls')
    if row.get('variant') == 'agent_no_visual' and images_known and not original_images:
        note('visual_input_disabled', '/original_image_pages=[]; /variant=agent_no_visual')

    outcome_index, outcome = _last_event(events, 'rag.outcome')
    outcome_pointer = f'/trace/events/{outcome_index}/payload'
    supported = {i for i in _list(outcome.get('supported')) if type(i) is int}
    claims = outcome.get('claims')
    if not isinstance(claims, list):
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            payload = event.get('payload', {}) if isinstance(event, dict) else {}
            if isinstance(event, dict) and event.get('kind') == 'rag.parsed' and payload.get('role') == 'Answerer':
                value = payload.get('value')
                claims = value.get('claims', []) if isinstance(value, dict) else []
                break
    claim_results = _claim_results(case, claims)
    if any(index in supported and not matches for index, matches in claim_results.items()):
        note('verifier_accepted_wrong', outcome_pointer + '/supported')
    if gold and 0 < len(gold & admitted) < len(gold):
        note('partial_gold_coverage', '/evidence_ranking')
    if oracle_correct is True and not correct:
        note('oracle_correct_rag_failed', '/oracle_row', 'needs_review')
    reason = str(outcome.get('stop_reason') or row.get('stop_reason') or '')
    budget_stop = _error_kind(reason) == 'budget_exhausted'
    if budget_stop:
        note('budget_limit_observed', outcome_pointer + '/stop_reason', 'needs_review')

    primary, level = 'unknown', 'needs_review'
    if row_missing:
        primary, level, pointers = 'missing_result', 'confirmed', ['/row=missing']
    elif not metric or (row.get('id') is not None and row.get('id') != case.get('id')):
        primary, level, pointers = 'schema_data_failure', 'confirmed', ['/id', '/case/task']
    elif correct:
        primary, level, pointers = 'success', 'confirmed', [f'/score/{metric}=1']
    elif row.get('status') == 'error':
        primary = _error_kind(row.get('error_type')) or _error_kind(row.get('error_code')) or 'unknown'
        pointers = ['/status=error', '/error_type']
        failed_tools = [(i, c) for i, c in enumerate(tools) if isinstance(c, dict) and c.get('status') == 'error']
        if primary == 'unknown' and failed_tools and str(row.get('error_type', '')).lower() == 'toolerror':
            index, call = failed_tools[-1]
            if index == len(tools) - 1:
                primary = _error_kind(call.get('error_code')) or ('tool_argument_invalid' if call.get('error_code') in
                           {'invalid_arguments', 'invalid_tool_action', 'unknown_tool'} else 'tool_execution_failure')
                pointers.append(f'/trace/tool_calls/{index}/error_code')
        level = 'needs_review' if primary == 'unknown' else 'confirmed'
    elif (outcome_index is not None and 'supported' in outcome and len(claim_results) == 1
          and any(matches and index not in supported for index, matches in claim_results.items())):
        primary, level = 'verifier_rejected_correct', 'confirmed'
        pointers = [outcome_pointer + '/claims', outcome_pointer + '/supported', f'/score/{metric}=0']
    elif budget_stop and not valid_prediction:
        primary, level, pointers = 'budget_exhausted', 'confirmed', [outcome_pointer + '/stop_reason']
    elif outcome.get('status') == 'incomplete' and not valid_prediction and not supported \
            and isinstance(outcome.get('claims'), list):
        primary, level, pointers = 'abstention', 'confirmed', [outcome_pointer + '/claims', outcome_pointer + '/supported']
    elif row.get('prediction_status') == 'abstained':
        primary, level, pointers = 'abstention', 'confirmed', ['/prediction_status=abstained']
    elif outcome.get('status') == 'schema_validation_failed' or not valid_prediction:
        primary, level, pointers = 'model_format_error', 'confirmed', ['/prediction_status', '/status']
    elif gold:
        if context_known and images_known and gold.intersection(admitted) and not gold.intersection(context | original_images):
            primary, level = 'context_selection_loss', 'confirmed'
            pointers = ['/evidence_ranking', '/context_ranking', '/original_image_pages']
        elif drops and gathered_known and not gold.intersection(observed_evidence):
            primary, level = 'rerank_drop', 'confirmed'
            pointers = [f'/trace/rerank_calls/{i}' for i in drops] + ['/evidence_ranking', '/ranking']
        elif retrieval_known and not gold.intersection(retrieved | observed_evidence):
            primary, level = 'retrieval_miss', 'confirmed'
            pointers = ['/trace/retrieval_calls', '/trace/tool_calls', '/case/gold_pages']
        elif final_known and gold.intersection(admitted | retrieved) and not gold.intersection(final):
            primary, level = 'evidence_selection_loss', 'confirmed'
            pointers = ['/evidence_ranking', '/trace/retrieval_calls', '/ranking']
        elif gold.intersection(context | original_images if context_known else admitted | final | original_images):
            primary, level = 'generation_reading_or_reasoning', 'needs_review'
            pointers = (['/context_ranking', '/original_image_pages'] if context_known else
                        ['/evidence_ranking', '/ranking']) + [f'/score/{metric}=0']
    if primary == 'unknown' and not pointers:
        pointers = ['/trace=insufficient', f'/score/{metric}=0']
    gold_elements = set(_pages(case.get('gold_element_ids')))
    element_coverage = {
        key: len(gold_elements.intersection(_pages(row.get(field)))) / len(gold_elements)
        if gold_elements and isinstance(row.get(field), list) else None
        for key, field in (('retrieved', 'retrieved_element_ids'), ('admitted', 'evidence_element_ids'),
                           ('context', 'context_element_ids'))
    }
    if element_coverage['admitted'] is not None and element_coverage['admitted'] < 1.:
        note('incomplete_text_element_coverage', '/evidence_element_ids', 'needs_review')
    if element_coverage['context'] is not None and element_coverage['context'] < 1.:
        note('incomplete_context_element_coverage', '/context_element_ids', 'needs_review')
    automatic = {
        'is_correct': correct, 'primary_code': primary, 'stage': PRIMARY_STAGES[primary],
        'evidence_level': level, 'evidence': pointers, 'secondary_codes': secondary,
        'secondary_details': details,
        'manual_review_required': level == 'needs_review' or primary == 'verifier_rejected_correct',
        'oracle_correct': oracle_correct, 'score': score, 'score_metric': metric,
        'element_coverage': element_coverage,
        'gold_coverage': {'admitted': len(gold & admitted) / len(gold) if gold and gathered_known else None,
                          'collected': len(gold & admitted) / len(gold) if gold and gathered_known else None,
                          'context': len(gold & context) / len(gold) if gold and context_known else None,
                          'original_images': len(gold & original_images) / len(gold) if gold and images_known else None,
                          'final': len(gold & final) / len(gold) if gold and final_known else None,
                          f'final@{k}': len(gold.intersection(_pages(row.get('ranking'))[:k])) / len(gold)
                          if gold and final_known else None},
    }
    return {'schema_version': SCHEMA_VERSION, 'taxonomy_version': TAXONOMY_VERSION,
            'id': case.get('id'), 'variant': row.get('variant'), **automatic,
            'automatic': deepcopy(automatic), 'expert_override': None}


def attribute_cases(cases, rows, oracle_rows=None, k=3):
    """Join by case ID and expose missing cases for each observed variant."""
    case_map = {case['id']: case for case in cases}
    if len(case_map) != len(cases):
        raise ValueError('Duplicate case IDs')
    oracle = oracle_rows or []
    if isinstance(oracle, dict):
        oracle = list(oracle.values())
    oracle_map = {row['id']: row for row in oracle}
    if len(oracle_map) != len(oracle):
        raise ValueError('Duplicate oracle IDs')
    if not oracle_map.keys() <= case_map.keys():
        raise ValueError('Unknown oracle IDs')
    seen, results, variants = set(), [], []
    for row in rows:
        ident = row['id']
        if ident not in case_map:
            raise ValueError('Unknown prediction ID')
        key = (ident, row.get('variant'))
        if key in seen:
            raise ValueError('Duplicate prediction ID and variant')
        seen.add(key)
        if key[1] not in variants:
            variants.append(key[1])
        results.append(attribute_case(case_map[ident], row, oracle_row=oracle_map.get(ident), k=k))
    for variant in variants or [None]:
        for case in cases:
            if (case['id'], variant) not in seen:
                missing = attribute_case(case, None, oracle_row=oracle_map.get(case['id']), k=k)
                missing['variant'] = variant
                results.append(missing)
    return results
