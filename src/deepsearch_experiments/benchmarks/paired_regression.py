"""Compare recorded runs without confusing config changes with proven causes."""
import argparse
from collections import Counter
import json
from pathlib import Path

from deepsearch_experiments.benchmarks.ablation import read_rows, validate_rows, write_json
from deepsearch_experiments.benchmarks.badcases import attribute_case
from deepsearch_experiments.benchmarks.schema import load_manifest


def _flatten(value, prefix=''):
    result = {}
    for key, item in value.items():
        name = f'{prefix}.{key}' if prefix else key
        if isinstance(item, dict):
            result.update(_flatten(item, name))
        else:
            result[name] = item
    return result


def _tokens(row):
    calls = [c for c in (row or {}).get('trace', {}).get('model_calls', [])
             if c.get('status') != 'budget_exceeded']
    values = [c.get('total_tokens') for c in calls]
    return sum(values) if values and all(type(v) is int and v >= 0 for v in values) else None


def _outcome(row):
    outcomes = [event.get('payload', {}) for event in (row or {}).get('trace', {}).get('events', [])
                if event.get('kind') == 'rag.outcome']
    return outcomes[-1] if outcomes else {}


def compare_runs(cases, before, before_rows, after, after_rows):
    for field in ('manifests', 'indices'):
        if not before.get(field) or before[field] != after.get(field):
            raise ValueError(f'Cannot pair different or unrecorded {field}')
    case_map = {case['id']: case for case in cases}
    for run, rows in ((before, before_rows), (after, after_rows)):
        ids, variants = run['case_ids'], run['variants']
        if (not ids or not variants or len(ids) != len(set(ids))
                or len(variants) != len(set(variants)) or not set(ids) <= case_map.keys()):
            raise ValueError('Invalid comparison population')
        validate_rows(rows, [case_map[i] for i in ids], variants)
    if (not set(after['case_ids']) <= set(before['case_ids'])
            or not set(after['variants']) <= set(before['variants'])):
        raise ValueError('New run population was not planned in baseline')
    conditions = [{**_flatten({'configuration': r['configuration']}), 'code_sha256': r['code_sha256']}
                  for r in (before, after)]
    changes = {key: {'before': conditions[0].get(key), 'after': conditions[1].get(key)}
               for key in sorted(conditions[0].keys() | conditions[1].keys())
               if conditions[0].get(key) != conditions[1].get(key)}
    maps = [{(r['id'], r['variant']): r for r in rows} for rows in (before_rows, after_rows)]
    transitions, tasks, pairs = Counter(), {}, []
    complete = 0
    for ident in after['case_ids']:
        for variant in after['variants']:
            rows = [m.get((ident, variant)) for m in maps]
            tags = [attribute_case(case_map[ident], r) for r in rows]
            correct = [tag['is_correct'] for tag in tags]
            if rows[0] is None or rows[1] is None:
                transition = 'missing_both' if rows == [None, None] else (
                    'missing_before' if rows[0] is None else 'missing_after')
            else:
                complete += 1
                transition = {(False, True): 'improved', (True, False): 'regressed',
                              (True, True): 'still_correct', (False, False): 'still_wrong'}[tuple(correct)]
            transitions[transition] += 1
            task = tasks.setdefault(case_map[ident]['task'], {'count': 0, 'before_correct': 0, 'after_correct': 0})
            task['count'] += 1
            task['before_correct'] += int(correct[0])
            task['after_correct'] += int(correct[1])
            pair = {'id': ident, 'variant': variant, 'transition': transition}
            for side, row, tag in zip(('before', 'after'), rows, tags):
                pair.update({f'{side}_code': tag['primary_code'], f'{side}_score': tag['score'],
                             f'{side}_total_tokens': _tokens(row),
                             f'{side}_latency_ms': (row or {}).get('latency_ms'),
                             f'{side}_graph_status': _outcome(row).get('status'),
                             f'{side}_stop_reason': _outcome(row).get('stop_reason'),
                             f'{side}_secondary_codes': tag['secondary_codes']})
            pairs.append(pair)
    return {'schema_version': 1, 'population': 'after_run_planned_cases_and_variants',
            'causal_claim_supported': False, 'changed_conditions': changes,
            'complete_pairs': complete, 'planned_pairs': len(pairs),
            'transitions': dict(transitions), 'tasks': tasks, 'pairs': pairs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; preserve it and choose a new file')
    manifests = [load_manifest(args.prepared / name / 'manifest.json') for name in ('chartqa', 'pdfvqa')]
    identities = [json.loads((root / 'run.json').read_text(encoding='utf-8')) for root in (args.before, args.after)]
    expected = {m['dataset']: m['_manifest_sha256'] for m in manifests}
    if any(run['manifests'] != expected for run in identities):
        parser.error('Prepared manifest differs from recorded run')
    rows = [read_rows(root / 'predictions.jsonl') for root in (args.before, args.after)]
    result = compare_runs([c for m in manifests for c in m['cases']], identities[0], rows[0], identities[1], rows[1])
    result['inputs'] = {'before': str(args.before), 'after': str(args.after)}
    write_json(args.output, result)
    print(json.dumps({'complete_pairs': result['complete_pairs'], 'transitions': result['transitions']}))


if __name__ == '__main__':
    main()
