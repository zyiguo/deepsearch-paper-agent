"""Deterministic DAG scoring with raw outcomes and dependency-aware credit."""
from collections import Counter
import math

from deepsearch_experiments.benchmarks.dag_schema import (
    AnswerRule, BudgetRule, GateRule, SetRule, ToolRule, ValueRule, fingerprint,
)
from deepsearch_experiments.benchmarks.metrics import answer_scores, element_scores, normalized_text
from deepsearch_experiments.benchmarks.schema import validate_prediction


PRIMARY = {'chartqa': 'relaxed_accuracy', 'A': 'accuracy', 'B': 'element_exact_match', 'C': 'element_exact_match'}
SUCCESS = {'ok', 'success', 'ok_multimodal'}


def observation(score=None, *, reason='observed', paths=(), value=None):
    return {'score': score, 'reason': reason, 'paths': list(paths), 'observed_value': value}


def finite_number(value):
    return type(value) in {int, float} and math.isfinite(value)


def evaluate_rule(rule, case, row):
    if row is None:
        return observation(reason='missing_prediction')
    if isinstance(rule, AnswerRule):
        if row.get('status') != 'ok' or row.get('prediction_status') in {'error', 'invalid_output', 'abstained'}:
            return observation(0., reason='unsuccessful_prediction', paths=['status', 'prediction_status'])
        try:
            validate_prediction(case, {key: row[key] for key in ('answer', 'element_ids') if key in row})
        except (TypeError, ValueError):
            return observation(0., reason='invalid_prediction', paths=['answer', 'element_ids'])
        scores = answer_scores(case, row)
        metric = PRIMARY[case['task']] if rule.metric == 'primary' else rule.metric
        if metric not in scores:
            raise ValueError(f'Metric {metric} does not apply to task {case["task"]}')
        return observation(scores[metric], paths=['answer', 'element_ids'], value={'metric': metric})
    if isinstance(rule, SetRule):
        actual = row.get(rule.field)
        if not isinstance(actual, list) or not all(isinstance(x, str) for x in actual):
            return observation(reason='missing_or_invalid_observation', paths=[rule.field])
        # An interrupted trace can prove presence, but cannot prove absence.
        gold, actual = set(rule.expected), set(actual)
        overlap = len(gold & actual)
        if rule.field.startswith('evidence_') and row.get('evidence_complete_observation') is False and not gold <= actual:
            return observation(reason='incomplete_evidence_observation', paths=[rule.field])
        denominator = len(gold) if rule.metric == 'recall' else len(actual)
        return observation(overlap / denominator if denominator else 0., paths=[rule.field],
                           value={'matched': sorted(actual & gold), 'observed_count': len(actual)})
    if isinstance(rule, ValueRule):
        artifacts = row.get('artifacts', {})
        path = f'artifacts.{rule.artifact}.value'
        item = artifacts.get(rule.artifact) if isinstance(artifacts, dict) else None
        if not isinstance(item, dict) or 'value' not in item:
            return observation(reason='missing_artifact', paths=[path])
        actual = item['value']
        if rule.metric == 'exact':
            score = float(isinstance(actual, str) and normalized_text(actual) == normalized_text(rule.expected))
        elif rule.metric == 'numeric':
            score = float(finite_number(actual) and math.isclose(actual, rule.expected,
                          rel_tol=rule.relative_tolerance, abs_tol=rule.absolute_tolerance))
        elif isinstance(actual, list) and all(isinstance(x, str) for x in actual):
            score = element_scores(rule.expected, actual)['element_f1' if rule.metric == 'set_f1' else 'element_exact_match']
        else:
            score = 0.
        return observation(score, paths=[path])
    if isinstance(rule, GateRule):
        return observation(1., reason='dependency_gate')
    trace = row.get('trace', {})
    if isinstance(rule, ToolRule):
        calls = trace.get('tool_calls') if isinstance(trace, dict) else None
        if not isinstance(calls, list):
            return observation(reason='missing_tool_trace', paths=['trace.tool_calls'])
        matches = []
        for index, call in enumerate(calls):
            if not isinstance(call, dict) or call.get('name') not in rule.names or call.get('status') not in SUCCESS:
                continue
            pages = call.get('ranking')
            if rule.expected_pages and (not isinstance(pages, list) or not all(isinstance(p, str) for p in pages)
                                        or not set(rule.expected_pages).intersection(pages)):
                continue
            matches.append(index)
        return observation(min(1., len(matches) / rule.min_successes),
                           paths=[f'trace.tool_calls[{i}]' for i in matches] or ['trace.tool_calls'],
                           value={'successful_matching_calls': len(matches)})
    if isinstance(rule, BudgetRule):
        path = 'latency_ms' if rule.metric == 'latency_ms' else 'trace.' + ('model_calls' if rule.metric == 'total_tokens' else rule.metric)
        if rule.metric == 'latency_ms':
            value = row.get('latency_ms')
        else:
            calls = trace.get('model_calls' if rule.metric == 'total_tokens' else rule.metric) if isinstance(trace, dict) else None
            if not isinstance(calls, list) or any(not isinstance(c, dict) for c in calls):
                return observation(reason='missing_budget_observation', paths=[path])
            sent = [c for c in calls if c.get('status') != 'budget_exceeded']
            if rule.metric == 'total_tokens':
                values = [c.get('total_tokens') for c in sent]
                value = sum(values) if all(finite_number(v) and v >= 0 for v in values) else None
            else:
                value = len(sent) if rule.metric == 'model_calls' else len(calls)
        if not finite_number(value) or value < 0:
            return observation(reason='missing_budget_observation', paths=[path])
        return observation(float(value <= rule.limit), paths=[path], value=value)
    raise TypeError('Unsupported evaluator rule')


def evaluate_dag(task, case, row):
    if task.case_id != case['id'] or task.case_sha256 != fingerprint(case):
        raise ValueError('DAG case identity mismatch')
    results = {}
    for node in task.ordered_nodes():
        if isinstance(node.rule, AnswerRule) and node.rule.metric != 'primary' and node.rule.metric not in answer_scores(case, {}):
            raise ValueError('Answer metric does not apply to this dataset task')
        value = evaluate_rule(node.rule, case, row)
        raw = 'unknown' if value['score'] is None else 'passed' if value['score'] >= node.threshold else 'failed'
        parents = [results[key] for key in node.depends_on]
        ready = not parents or (all(p['status'] == 'passed' for p in parents) if node.dependency_mode == 'all'
                                else any(p['status'] == 'passed' for p in parents))
        blockers = sorted({key for parent in parents if parent['status'] != 'passed'
                           for key in (parent['blocked_by'] or [parent['id']])}) if not ready else []
        results[node.id] = {'id': node.id, 'description': node.description, 'raw_status': raw,
                            'status': raw if ready else 'blocked', 'raw_score': value['score'],
                            'score': (value['score'] or 0.) if ready else 0.,
                            'weight': node.weight, 'required': node.required, 'blocked_by': blockers,
                            'failure_tag': node.failure_tag, 'observation': value}
    values = list(results.values())
    denominator = sum(n.weight for n in task.nodes)
    goal_blockers = {key for n in values if n['required'] for key in [n['id'], *n['blocked_by']]}
    return {'id': case['id'], 'dataset': case['dataset'], 'task': case['task'],
            'variant': row.get('variant') if row else None,
            'annotation_level': task.annotation_level, 'annotation_status': task.annotation_status,
            'dag_sha256': fingerprint(task.model_dump()), 'nodes': values,
            'success': all(n['status'] == 'passed' for n in values if n['required']),
            'raw_score': sum((n['raw_score'] or 0.) * n['weight'] for n in values) / denominator,
            'score': sum(n['score'] * n['weight'] for n in values) / denominator,
            'observation_coverage': sum(n['weight'] for n in values if n['raw_score'] is not None) / denominator,
            'failure_frontier': [n['id'] for n in values if n['status'] == 'failed' and n['id'] in goal_blockers],
            'unknown_nodes': [n['id'] for n in values if n['raw_status'] == 'unknown'],
            'blocked_nodes': [n['id'] for n in values if n['status'] == 'blocked'],
            'attribution': 'dependency_failure_frontier_not_proven_causal_root'}


def summarize_dags(results):
    summary = {}
    for variant in sorted({r['variant'] for r in results}):
        groups = {}
        for task in sorted({r['task'] for r in results if r['variant'] == variant}):
            rows = [r for r in results if r['variant'] == variant and r['task'] == task]
            groups[task] = {'count': len(rows), 'success_count': sum(r['success'] for r in rows),
                            'success_rate': sum(r['success'] for r in rows) / len(rows),
                            'mean_score': sum(r['score'] for r in rows) / len(rows),
                            'mean_raw_score': sum(r['raw_score'] for r in rows) / len(rows),
                            'observation_coverage': sum(r['observation_coverage'] for r in rows) / len(rows),
                            'node_status_counts': dict(Counter(n['status'] for r in rows for n in r['nodes'])),
                            'failure_frontier_counts': dict(Counter(n for r in rows for n in r['failure_frontier']))}
        summary[variant] = groups
    return summary
