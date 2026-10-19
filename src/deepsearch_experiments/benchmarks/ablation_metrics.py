"""Paired, stage-level diagnostics for small Agentic RAG ablation runs."""
from collections import Counter
import json
import math
import random

from deepsearch_experiments.benchmarks.metrics import answer_scores, ranking_scores
from deepsearch_experiments.benchmarks.schema import validate_prediction


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _ratio(values, *, binary=False):
    count, total = len(values), sum(values)
    result = {'value': total / count if count else None,
              'numerator': total, 'denominator': count}
    if binary:
        if count:
            p, z = total / count, 1.959963984540054
            center = (p + z * z / (2 * count)) / (1 + z * z / count)
            radius = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / (1 + z * z / count)
            result['wilson95'] = [max(0., center - radius), min(1., center + radius)]
        else:
            result['wilson95'] = None
    return result


def _quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * q
    lower = math.floor(index)
    upper = math.ceil(index)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def _latencies(items):
    values = [item['latency_ms'] for item in items if _number(item.get('latency_ms'))]
    return {'mean': sum(values) / len(values) if values else None,
            'p50': _quantile(values, .5), 'p95': _quantile(values, .95),
            'sum': sum(values) if values else None,
            'observed_count': len(values), 'missing_count': len(items) - len(values)}


def _ranking(value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(page, str) for page in value):
        raise ValueError('Ranking must contain page IDs')
    return list(dict.fromkeys(value))


def _calls(row, kind):
    return (row or {}).get('trace', {}).get(kind, [])


def _success(call):
    return call.get('status') in {'ok', 'success', 'ok_multimodal'}


def _status(case, row):
    if row is None:
        return 'missing'
    if row.get('status') != 'ok':
        return row.get('status', 'unknown')
    try:
        value = {key: row[key] for key in ('answer', 'element_ids') if key in row}
        validate_prediction(case, value)
    except (ValueError, TypeError):
        return 'invalid_output'
    if row.get('prediction_status') in {'invalid_output', 'error'}:
        return 'invalid_output'
    return 'ok'


def _prediction(case, row):
    result = answer_scores(case, {})
    if _status(case, row) != 'ok' or row.get('prediction_status') == 'abstained':
        return {key: 0. for key in result}
    return answer_scores(case, row)


def _answer_metrics(cases, by_id):
    values = {}
    for case in cases:
        for metric, score in _prediction(case, by_id.get(case['id'])).items():
            values.setdefault(metric, []).append(score)
    return {metric: _ratio(scores, binary=metric != 'element_f1')
            for metric, scores in values.items()}


def _gold_available(case, row):
    return bool(set(case['gold_pages']) & set(_ranking((row or {}).get('evidence_ranking'))))


def _task_group(cases, by_id):
    available = [case for case in cases if _gold_available(case, by_id.get(case['id']))]
    conditional = _answer_metrics(available, by_id)
    # Expose an empty denominator for each expected metric when no gold was found.
    for name in _answer_metrics(cases, by_id):
        conditional.setdefault(name, _ratio([], binary=name != 'element_f1'))
    return {'count': len(cases), 'answer_metrics': _answer_metrics(cases, by_id),
            'answer_when_gold_available': conditional, 'gold_available_cases': len(available),
            'element_text_coverage': _element_coverage(cases, by_id)}


def _element_coverage(cases, by_id):
    eligible = [case for case in cases if case['task'] in {'B', 'C'} and case['gold_element_ids']]
    result = {'eligible_cases': len(eligible),
              'interpretation': 'catalog_ids_in_text_not_image_readability',
              'denominator_policy': 'observed_id_lists_with_nonempty_gold; absent_fields_are_unknown'}
    for stage in ('retrieved', 'evidence'):
        recalls, hits = [], []
        for case in eligible:
            row = by_id.get(case['id'], {})
            if f'{stage}_element_ids' not in row or row[f'{stage}_element_ids'] is None:
                continue
            present = set(_ranking(row[f'{stage}_element_ids']))
            gold = set(case['gold_element_ids'])
            recalls.append(len(present & gold) / len(gold))
            hits.append(int(bool(present & gold)))
        result[f'{stage}_recall'] = _ratio(recalls)
        result[f'{stage}_gold_hit'] = _ratio(hits, binary=True)
        result[f'{stage}_missing_count'] = len(eligible) - len(recalls)
    return result


def _retrieval(cases, by_id, k):
    values = {key: [] for key in (
        'any_search_gold_hit', 'candidate_recall', 'first_search_recall_at_k',
        'first_search_mrr_at_k', 'post_tool_gold_hit', 'collected_evidence_gold_hit',
        'final_citation_page_precision', 'final_citation_page_coverage',
        'precision_when_citations_present')}
    observed = {'search_cases': 0, 'tool_cases': 0, 'citation_cases': 0}
    for case in cases:
        gold = set(case['gold_pages'])
        if not gold:
            continue
        row = by_id.get(case['id'], {})
        searches = _calls(row, 'retrieval_calls')
        observed['search_cases'] += bool(searches)
        candidates = {page for call in searches if _success(call) for page in _ranking(call.get('ranking'))}
        first = _ranking(searches[0].get('ranking')) if searches and _success(searches[0]) else []
        scores = ranking_scores(first, gold, k)
        tools = _calls(row, 'tool_calls')
        observed['tool_cases'] += bool(tools)
        tool_pages = {page for call in tools if _success(call) for page in _ranking(call.get('ranking'))}
        final = set(_ranking(row.get('ranking')))
        observed['citation_cases'] += bool(final)
        precision = len(final & gold) / len(final) if final else 0.
        values['any_search_gold_hit'].append(int(bool(candidates & gold)))
        values['candidate_recall'].append(len(candidates & gold) / len(gold))
        values['first_search_recall_at_k'].append(scores['recall'])
        values['first_search_mrr_at_k'].append(scores['mrr'])
        values['post_tool_gold_hit'].append(int(bool(tool_pages & gold)))
        values['collected_evidence_gold_hit'].append(int(_gold_available(case, row)))
        values['final_citation_page_precision'].append(precision)
        values['final_citation_page_coverage'].append(len(final & gold) / len(gold))
        if final:
            values['precision_when_citations_present'].append(precision)
    return {**{name: _ratio(v, binary=name.endswith('gold_hit')) for name, v in values.items()},
            'observed': observed,
            'citation_interpretation': 'gold_page_overlap_proxy_not_semantic_faithfulness',
            'denominator_policy': 'all_cases_with_gold_pages; missing_stages_score_zero'}


def _rerank(cases, by_id, k):
    before, after, calls = [], [], []
    for case in cases:
        for call in _calls(by_id.get(case['id']), 'rerank_calls'):
            calls.append(call)
            if case['gold_pages']:
                before.append(ranking_scores(_ranking(call.get('initial_ranking')), case['gold_pages'], k))
                after.append(ranking_scores(_ranking(call.get('ranking')), case['gold_pages'], k))
    result = {'aggregation': 'call_weighted_diagnostic', 'attempts': len(calls),
              'success_count': sum(_success(call) for call in calls),
              'status_counts': dict(Counter(call.get('status', 'unknown') for call in calls)),
              'latency_ms': _latencies(calls)}
    for metric in ('recall', 'mrr', 'ndcg'):
        result[f'before_{metric}_at_k'] = _ratio([value[metric] for value in before])
        result[f'after_{metric}_at_k'] = _ratio([value[metric] for value in after])
        result[f'delta_{metric}_at_k'] = _ratio([right[metric] - left[metric] for left, right in zip(before, after)])
    return result


def _tools(cases, by_id):
    by_name = {}
    for case in cases:
        seen = set()
        for call in _calls(by_id.get(case['id']), 'tool_calls'):
            name = call.get('name', 'unknown')
            item = by_name.setdefault(name, {'calls': [], 'hits': [], 'repeats': 0, 'new': 0, 'invalid': 0})
            item['calls'].append(call)
            identity = (name, json.dumps(call.get('arguments', {}), sort_keys=True, ensure_ascii=True, allow_nan=False))
            item['repeats'] += identity in seen
            seen.add(identity)
            item['new'] += bool(_success(call) and call.get('new_chunk_ids'))
            invalid = ('invalid' in str(call.get('error_code', '')).lower()
                       or call.get('error_code') == 'unknown_tool'
                       or str(call.get('status', '')).startswith('invalid'))
            item['invalid'] += bool(invalid)
            if name != 'list_documents' and 'ranking' in call and case['gold_pages']:
                hit = _success(call) and bool(set(_ranking(call['ranking'])) & set(case['gold_pages']))
                item['hits'].append(int(hit))
    result = {}
    for name, item in by_name.items():
        calls = item['calls']
        succeeded = sum(_success(call) for call in calls)
        result[name] = {'attempts': len(calls), 'success_count': succeeded,
                        'error_count': len(calls) - succeeded, 'invalid_count': item['invalid'],
                        'status_counts': dict(Counter(call.get('status', 'unknown') for call in calls)),
                        'error_code_counts': dict(Counter(call['error_code'] for call in calls if call.get('error_code'))),
                        'repeated_identical_count': item['repeats'], 'new_evidence_calls': item['new'],
                        'gold_page_hit': _ratio(item['hits'], binary=True),
                        'ranking_observed_count': len(item['hits']), 'latency_ms': _latencies(calls)}
    return result


def _tokens(calls):
    result = {}
    for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        values = [call[name] for call in calls if _number(call.get(name))]
        result[name] = {'sum': sum(values) if values else None, 'reported_calls': len(values),
                        'missing_calls': len(calls) - len(values)}
    return result


def _model_cost(calls):
    requested = [call for call in calls if call.get('status') != 'budget_exceeded']
    images = [call['image_count'] for call in requested if _number(call.get('image_count'))]
    return {'model_attempts': len(calls), 'model_calls': len(requested),
            'budget_blocked_calls': len(calls) - len(requested),
            'success_count': sum(_success(call) for call in calls),
            'status_counts': dict(Counter(call.get('status', 'unknown') for call in calls)),
            'tokens': _tokens(requested), 'latency_ms': _latencies(requested),
            'images': {'sum': sum(images) if images else None,
                       'reported_calls': len(images), 'missing_calls': len(requested) - len(images)}}


def _costs(cases, by_id):
    rows = [by_id.get(case['id'], {}) for case in cases]
    calls = [call for row in rows for call in _calls(row, 'model_calls')]
    result = _model_cost(calls)
    result['model_latency_ms'] = result.pop('latency_ms')
    result['latency_ms'] = _latencies(rows)
    stages = sorted({call.get('stage') or call.get('role') or 'unknown' for call in calls})
    result['by_stage'] = {stage: _model_cost([call for call in calls
                         if (call.get('stage') or call.get('role') or 'unknown') == stage]) for stage in stages}
    result['token_policy'] = 'provider_reported_only; missing_usage_is_not_zero'
    return result


def _claim_prediction(case, claim):
    try:
        text = claim.get('text', '').strip()
        if text.startswith('```') and text.endswith('```'):
            text = text[text.find('\n') + 1:-3].strip()
        value = json.loads(text)
        validate_prediction(case, value)
        return answer_scores(case, value)
    except (ValueError, TypeError, AttributeError):
        return None


def _primary_score(case, scores):
    metric = {'chartqa': 'relaxed_accuracy', 'A': 'accuracy', 'B': 'element_exact_match',
              'C': 'element_exact_match'}[case['task']]
    return scores[metric]


def _verifier(cases, by_id):
    result = {'interpretation': 'model_self_check_not_ground_truth', 'observed_cases': 0,
              'outcome_counts': {}, 'parseable_final_drafts': 0,
              'correct_draft_final_incorrect': 0, 'incorrect_draft_final_correct': 0,
              'parseable_json_claims': 0, 'rejected_correct_json_claims': 0}
    outcomes, accepted = Counter(), []
    for case in cases:
        row = by_id.get(case['id'])
        events = [event['payload'] for event in _calls(row, 'events')
                  if event.get('kind') == 'rag.outcome' and isinstance(event.get('payload'), dict)]
        if not events:
            continue
        event = events[-1]
        result['observed_cases'] += 1
        outcomes[event.get('status', 'unknown')] += 1
        claims = event.get('claims', [])
        supported = set(index for index in event.get('supported', [])
                        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(claims))
        accepted.extend(int(index in supported) for index in range(len(claims)))
        parsed = [_claim_prediction(case, claim) for claim in claims]
        result['parseable_json_claims'] += sum(value is not None for value in parsed)
        result['rejected_correct_json_claims'] += sum(value is not None and _primary_score(case, value) == 1
                                                     and index not in supported for index, value in enumerate(parsed))
        # A single structured claim can be compared without inventing an answer extraction rule.
        if len(parsed) == 1 and parsed[0] is not None:
            result['parseable_final_drafts'] += 1
            before = _primary_score(case, parsed[0])
            after = _primary_score(case, _prediction(case, row))
            result['correct_draft_final_incorrect'] += before == 1 and after == 0
            result['incorrect_draft_final_correct'] += before == 0 and after == 1
    result['outcome_counts'] = dict(outcomes)
    result['accepted_claims'] = _ratio(accepted, binary=True)
    result['transition_interpretation'] = 'descriptive_last_draft_to_final_transition_not_causal_effect'
    return result


def _variant(cases, by_id, k):
    task_names = sorted({case['task'] for case in cases})
    group_names = sorted({(case['dataset'], case['task'], case['group']) for case in cases})
    statuses = Counter(_status(case, by_id[case['id']]) for case in cases if case['id'] in by_id)
    return {'count': len(cases), 'ok_count': statuses.get('ok', 0),
            'error_count': sum(count for status, count in statuses.items() if status != 'ok'),
            'invalid_output_count': statuses.get('invalid_output', 0),
            'abstained_count': sum(row.get('prediction_status') == 'abstained' for row in by_id.values()),
            'missing_count': len(cases) - len(by_id), 'status_counts': dict(statuses),
            'by_task': {task: _task_group([case for case in cases if case['task'] == task], by_id) for task in task_names},
            'by_group': {'/'.join(group): _task_group([case for case in cases
                          if (case['dataset'], case['task'], case['group']) == group], by_id) for group in group_names},
            'retrieval': _retrieval(cases, by_id, k), 'rerank': _rerank(cases, by_id, k),
            'element_text_coverage': _element_coverage(cases, by_id),
            'tools': _tools(cases, by_id), 'verifier': _verifier(cases, by_id), 'costs': _costs(cases, by_id)}


def _paired_metric(cases, full, other, metric):
    left = [_prediction(case, full.get(case['id']))[metric] for case in cases]
    right = [_prediction(case, other.get(case['id']))[metric] for case in cases]
    differences = [b - a for a, b in zip(left, right)]
    improved, worse = sum(value > 0 for value in differences), sum(value < 0 for value in differences)
    discordant = improved + worse
    probability = (min(1., 2 * sum(math.comb(discordant, i) for i in range(min(improved, worse) + 1))
                       / 2 ** discordant) if discordant else 1.)
    rng = random.Random(20261005)
    bootstrap = [sum(rng.choices(differences, k=len(differences))) / len(differences) for _ in range(2000)]
    return {'case_count': len(cases), 'observed_both_count': sum(case['id'] in full and case['id'] in other for case in cases),
            'agent_full': sum(left) / len(left), 'variant': sum(right) / len(right),
            'difference': sum(differences) / len(differences), 'improved': improved, 'worse': worse,
            'tied': len(cases) - discordant, 'exact_sign_test_p': probability,
            'bootstrap95': [_quantile(bootstrap, .025), _quantile(bootstrap, .975)],
            'bootstrap_replicates': 2000, 'bootstrap_seed': 20261005}


def summarize_ablation(cases, rows, k=3, *, variants=None):
    """Summarize fixed paired cases, with zero scores for absent/failed predictions.

    Pass ``variants`` to include wholly missing experimental arms. Tool and model
    counts describe observed traces only; page-overlap scores are not entailment.
    """
    if not cases or not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError('Ablation requires cases and a positive integer k')
    case_ids = {case['id'] for case in cases}
    if len(case_ids) != len(cases):
        raise ValueError('Duplicate case IDs')
    names = list(variants) if variants is not None else list({row.get('variant', '') for row in rows})
    if any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != len(names):
        raise ValueError('Invalid or duplicate variant names')
    if variants is None:
        names.sort()
    grouped = {name: {} for name in names}
    for row in rows:
        name, ident = row.get('variant'), row.get('id')
        if name not in grouped or ident not in case_ids or ident in grouped[name]:
            raise ValueError('Duplicate or unknown prediction ID/variant')
        grouped[name][ident] = row
    result = {'schema_version': 1, 'k': k,
              'metadata': {'answer_denominator': 'all_fixed_cases_per_task; missing_and_failed_zero',
                           'stage_denominator': 'case_weighted_except_explicit_call_weighted_rerank_and_tool_tables',
                           'confidence_intervals': 'descriptive_small_sample; no_multiple_comparison_adjustment',
                           'page_metrics': 'gold_page_overlap; not_semantic_answer_or_citation_faithfulness',
                           'pair_direction': 'variant_minus_agent_full; positive_is_better',
                           'trace_missingness': 'unobserved_calls_cannot_be_inferred'},
              'variants': {name: _variant(cases, by_id, k) for name, by_id in grouped.items()},
              'paired_vs_agent_full': {}}
    if 'agent_full' in grouped:
        for name, by_id in grouped.items():
            if name == 'agent_full':
                continue
            tasks = {}
            for task in sorted({case['task'] for case in cases}):
                selected = [case for case in cases if case['task'] == task]
                tasks[task] = {metric: _paired_metric(selected, grouped['agent_full'], by_id, metric)
                               for metric in _prediction(selected[0], None)}
            result['paired_vs_agent_full'][name] = {'by_task': tasks}
    return result
