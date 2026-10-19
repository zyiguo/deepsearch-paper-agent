"""Offline audit of the frozen 2026-10-15 paired budget pilot."""
import hashlib
import json
from collections import Counter
from pathlib import Path

from deepsearch_experiments.benchmarks.multihop.compare import compare_runs, markdown_table
from deepsearch_experiments.benchmarks.multihop.datasets import load_manifest
from deepsearch_experiments.benchmarks.multihop.runner import implementation_identity, score_run


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root = Path('data/benchmarks/multihop/runs/cloud-20261015-budget-pilot10')
    identity = implementation_identity()
    result = {'implementation': identity, 'runs': [], 'paired_cases': {},
              'scope': '20 development questions, 60 saved outcomes; descriptive only',
              'possible_unpersisted_cloud_calls': True, 'unpersisted_usage': None}
    for dataset in ('2wikimultihopqa', 'musique'):
        manifest = load_manifest(Path('data/benchmarks/multihop/prepared') / dataset / 'pilot-10.json')
        paths = [root / f'{dataset}-{method}' for method in
                 ('mainline', 'mainline-budget-visible', 'mainline-budget-bounded')]
        comparison = compare_runs(manifest, paths)
        paired = {}
        for path in paths:
            assert not (path / 'run.lock').exists(), f'Active lock: {path}'
            report = score_run(manifest, path)
            saved = json.loads((path / 'report.json').read_text(encoding='utf-8'))
            assert report == saved, f'Rescore mismatch: {path}'
            assert report['run']['implementation'] == identity
            attempts = [json.loads(line) for line in
                        (path / 'attempts.jsonl').read_text(encoding='utf-8').splitlines()]
            assert len(attempts) == 10 and len({a['id'] for a in attempts}) == 10
            assert all(a['attempt'] == 1 for a in attempts)
            method = report['run']['method']['name']
            scores = {case['id']: case for case in report['per_case']}
            cases = []
            for attempt in attempts:
                outcome = attempt.get('details', {}).get('outcome', {})
                events = [e for e in attempt['events'] if e['kind'].startswith('mainline.budget.')]
                case = {'id': attempt['id'], 'prediction': attempt['prediction'],
                        'score': scores[attempt['id']], 'usage': attempt['usage'],
                        'outcome_status': outcome.get('status', 'unavailable'),
                        'termination_reason': attempt.get('details', {}).get('termination_reason'),
                        'error_code': attempt.get('details', {}).get('error_code'),
                        'failure_code': attempt.get('details', {}).get('failure_code'),
                        'budget_events': events}
                cases.append(case)
                paired.setdefault(attempt['id'], {})[method] = case
            result['runs'].append({
                'dataset': dataset, 'method': method, 'summary': report['summary'],
                'statuses': dict(Counter(a['prediction']['status'] for a in attempts)),
                'outcomes': dict(Counter(c['outcome_status'] for c in cases)),
                'explicit_abstentions': sum(a['prediction']['status'] == 'ok' and
                    a['prediction']['answer'].strip().upper() in ('', 'UNKNOWN') for a in attempts),
                'stop_triggered_cases': [c['id'] for c in cases if c['budget_events']],
                'known_reported_tokens': sum(a['usage']['known_reported_tokens'] for a in attempts),
                'usage_missing_calls': report['usage_missing_calls'],
                'total_model_calls': report['total_model_calls'],
                'total_search_calls': report['total_search_calls'],
                'hashes': {name: digest(path / name) for name in
                           ('run.json', 'attempts.jsonl', 'report.json')}})
        result['paired_cases'][dataset] = paired
        (root / f'{dataset}-comparison.json').write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (root / f'{dataset}-comparison.md').write_text(markdown_table(comparison), encoding='utf-8')
    recovered = root / 'musique-mainline-budget-visible'
    record = json.loads((recovered / 'recovery-20261015.json').read_text(encoding='utf-8'))
    assert digest(recovered / 'run.json') == record['run_sha256']
    prefix = (recovered / 'attempts.jsonl').read_bytes()[:record['prefix_length']]
    assert hashlib.sha256(prefix).hexdigest() == record['attempts_prefix_sha256']
    result['recovery_prefix_preserved'] = True
    canary = json.loads((root.parent / 'cloud-20261015-budget-canary' / 'audit.json').read_text(encoding='utf-8'))
    for run in canary['runs']:
        for name, expected in run['artifact_sha256'].items():
            assert digest(Path(name)) == expected, f'Canary changed: {name}'
    result['original_canaries_preserved'] = True
    result['saved_outcomes'] = 60
    (root / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'paired_cases'}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
