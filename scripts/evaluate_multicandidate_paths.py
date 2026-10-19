"""Frozen same-build, matched-budget, no-retry evaluation on existing development cases.

Use prepare, run, finalize in order. Writes only to a new experiment directory.
"""
import argparse
from collections import Counter, defaultdict
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_experiments.architecture import inspect
from deepsearch_experiments.benchmarks.multihop.compare import compare_runs
from deepsearch_experiments.benchmarks.multihop.datasets import load_manifest
from deepsearch_experiments.benchmarks.multihop.pruning_audit import audit_pruning
from deepsearch_experiments.benchmarks.multihop.runner import implementation_identity, read_attempts, score_run


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, data):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)


def original_hashes():
    root = Path('output/expanded-evaluation-20261018-100x2')
    prior = Path('output/evidence-controlled-20261018/canary')
    records = {str(root / x['path']): x['sha256'] for x in json.loads((root / 'trace-manifest.json').read_text())}
    attribution = json.loads(Path('output/badcase-attribution-20261018/audit-v2/summary.json').read_text())
    records.update({str(root / key): value for key, value in attribution['input_sha256'].items()})
    records.update({str(prior / x['path']): x['sha256'] for x in json.loads((prior / 'artifact-manifest.json').read_text())})
    assert all(digest(Path(path)) == expected for path, expected in records.items())
    return records


def prepare(root, methods=None):
    prior = Path('output/evidence-controlled-20261018/canary')
    old = json.loads((prior / 'plan.json').read_text())
    settings = LLMSettings.load(Path('.env'))
    for key in ('backend', 'base_url', 'model', 'timeout_seconds'):
        assert getattr(settings, key) == old['model'][key], 'Public model identity changed'
    tests = (root / 'pytest-final.log').read_text(encoding='utf-8')
    assert 'failed' not in tests and 'passed' in tests
    architecture = inspect(Path('src'))
    assert not architecture['cycles'] and not architecture['violations']
    datasets = {}
    for name, selection in old['datasets'].items():
        source = Path(selection['manifest'])
        assert digest(source) == selection['sha256']
        destination = root / (name + '.json')
        with destination.open('xb') as handle:
            handle.write(source.read_bytes())
        datasets[name] = {**selection, 'manifest': str(destination.resolve())}
    plan = {'purpose': 'outcome-selected development mechanism check; not an unbiased accuracy estimate',
        'methods': methods or ['mainline-evidence-controlled-v2', 'mainline-evidence-controlled-v3-shadow',
                    'mainline-evidence-controlled-v3'],
        'implementation': implementation_identity(), 'limits': old['limits'], 'model': old['model'],
        'datasets': datasets, 'max_workers': 4, 'retry_errors': False,
        'gold_exclusions': {'2hop__128342_375952': 'Gold bridge conflates Los Alamos New Mexico and California; quarantine, not a human correctness label.'},
        'original_hashes': original_hashes(), 'architecture': architecture,
        'test_summary': tests.splitlines()[-1], 'test_log_sha256': digest(root / 'pytest-final.log'),
        'selection': 'Reuse the original 4 canary cases: 3 historical badcases + 1 success control.',
        'sampling': 'Provider defaults shared; no reproducible provider seed, single run per case/method.'}
    write(root / 'plan.json', plan)
    print(json.dumps({'prepared': True, 'cases': 4, 'predictions': 4 * len(plan['methods']), 'implementation': plan['implementation']['sha256']}))


def run(root, plan):
    assert implementation_identity() == plan['implementation'], 'Code changed after freeze'
    limits = plan['limits']
    def worker(dataset, method):
        args = [sys.executable, '-m', 'deepsearch_experiments.benchmarks.multihop', 'run',
            '--manifest', plan['datasets'][dataset]['manifest'], '--method', method,
            '--output', str(root / 'runs' / (dataset + '-' + method)),
            '--llm-env', '.env', '--allow-remote', '--direct', '--max-steps', '6']
        for flag, key in [('k', 'k'), ('max-model-calls', 'model_calls'), ('max-search-calls', 'search_calls'),
            ('token-budget', 'token_budget'), ('max-output-tokens', 'max_output_tokens'),
            ('context-window', 'context_window'), ('deadline-seconds', 'deadline_seconds')]:
            args += ['--' + flag, str(limits[key])]
        with (root / (dataset + '-' + method + '.log')).open('x', encoding='utf-8') as handle:
            process = subprocess.run(args, stdout=handle, stderr=subprocess.STDOUT)
        result = {'dataset': dataset, 'method': method, 'exit_code': process.returncode}
        print(json.dumps(result), flush=True)
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=plan['max_workers']) as pool:
        jobs = [pool.submit(worker, dataset, method) for dataset in plan['datasets'] for method in plan['methods']]
        results = [job.result() for job in concurrent.futures.as_completed(jobs)]
    assert implementation_identity() == plan['implementation'], 'Code changed during inference'
    write(root / 'batch-result.json', results)
    assert all(r['exit_code'] == 0 for r in results)


def role_cost(events):
    totals = defaultdict(lambda: {'calls': 0, 'known_tokens': 0, 'usage_missing_calls': 0})
    role = None
    for event in events:
        if event['kind'] == 'mainline.rag.request':
            role = event['role']
        elif event['kind'] == 'mainline.answer_projection':
            role = 'Projection'
        elif event['kind'] == 'model':
            cost = totals[role or 'Unattributed']
            cost['calls'] += 1
            tokens = event['reported_total_tokens']
            if tokens is None:
                cost['usage_missing_calls'] += 1
            else:
                cost['known_tokens'] += tokens
            role = None
    return dict(totals)


def finalize(root, plan):
    assert implementation_identity() == plan['implementation']
    assert all(digest(Path(p)) == h for p, h in plan['original_hashes'].items())
    batch = json.loads((root / 'batch-result.json').read_text())
    assert len(batch) == len(plan['datasets']) * len(plan['methods']) and all(x['exit_code'] == 0 for x in batch)
    records = []
    for dataset, selection in plan['datasets'].items():
        manifest_path = Path(selection['manifest'])
        assert digest(manifest_path) == selection['sha256']
        manifest = load_manifest(manifest_path)
        runs = [root / 'runs' / (dataset + '-' + method) for method in plan['methods']]
        write(root / (dataset + '-comparison.json'), compare_runs(manifest, runs))
        gold = {g.id: g for g in manifest.gold}
        for method, path in zip(plan['methods'], runs):
            report = score_run(manifest, path)
            assert report['run']['model'] == plan['model'] and report['run']['limits'] == plan['limits']
            attempts, latest = read_attempts(path / 'attempts.jsonl', manifest, report['run'])
            assert len(attempts) == 2 and all(a['attempt'] == 1 for a in attempts)
            scores = {s['id']: s for s in report['per_case']}
            for attempt in attempts:
                excluded = attempt['id'] in plan['gold_exclusions']
                audit = audit_pruning(attempt, gold[attempt['id']].answers, dataset, exclude_gold=excluded)
                score, prediction = scores[attempt['id']], attempt['prediction']
                group = ('failure' if prediction['status'] != 'ok' else
                         'unknown' if excluded or
                         prediction['answer'].strip().upper() in {'', 'UNKNOWN'} else
                         'success' if score['answer_em'] == 1 else 'failure')
                costs = role_cost(attempt['events'])
                assert sum(c['calls'] for c in costs.values()) == attempt['usage']['model_calls']
                records.append({'id': attempt['id'], 'dataset': dataset, 'method': method,
                    'outcome_group': group, 'gold_excluded': excluded,
                    'score': score, 'pruning': audit, 'role_cost': costs, 'attempt': attempt,
                    'source_sha256': digest(path / 'attempts.jsonl')})
    assert len(records) == 4 * len(plan['methods'])
    for group in ('success', 'failure', 'unknown'):
        with (root / (group + '.jsonl')).open('x', encoding='utf-8') as handle:
            for record in records:
                if record['outcome_group'] == group:
                    handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    totals = {}
    for method in plan['methods']:
        subset = [r for r in records if r['method'] == method]
        eligible = [r for r in subset if not r['gold_excluded']]
        usage = [r['attempt']['usage'] for r in subset]
        candidate_cases = [r for r in eligible if r['pruning']['candidate_oracle_em_proxy'] is not None]
        denominator = sum(r['pruning']['gold_candidate_with_path_observations'] for r in subset)
        numerator = sum(r['pruning']['gold_candidate_all_paths_pruned_observations'] for r in subset)
        totals[method] = {'count': len(subset), 'eligible_gold_count': len(eligible),
            'raw_answer_em': sum(r['score']['answer_em'] for r in subset) / len(subset),
            'eligible_answer_em': sum(r['score']['answer_em'] for r in eligible) / len(eligible),
            'eligible_answer_f1': sum(r['score']['answer_f1'] for r in eligible) / len(eligible),
            'execution_statuses': dict(Counter(r['attempt']['prediction']['status'] for r in subset)),
            'outcome_groups': dict(Counter(r['outcome_group'] for r in subset)),
            'model_calls': sum(u['model_calls'] for u in usage), 'search_calls': sum(u['search_calls'] for u in usage),
            'reported_total_tokens': sum(u['reported_total_tokens'] for u in usage)
                if all(u['reported_total_tokens'] is not None for u in usage) else None,
            'known_reported_tokens': sum(u['known_reported_tokens'] for u in usage),
            'usage_missing_calls': sum(u['usage_missing_calls'] for u in usage),
            'candidate_oracle_eligible_cases': len(candidate_cases),
            'candidate_oracle_em_proxy': sum(r['pruning']['candidate_oracle_em_proxy'] for r in candidate_cases) / len(candidate_cases)
                if candidate_cases else None,
            'pruned_paths': sum(r['pruning']['pruned_paths'] for r in subset),
            'certificate_replay_failures': sum(r['pruning']['certificate_replay_failures'] for r in subset),
            'gold_candidate_all_paths_pruned_risk_proxy': numerator / denominator if denominator else None,
            'gold_candidate_path_observation_denominator': denominator,
            'true_false_prune_rate': None, 'reviewed_correct_path_denominator': 0}
    per_case = [{'id': r['id'], 'dataset': r['dataset'], 'method': r['method'],
        'answer': r['attempt']['prediction']['answer'], 'status': r['attempt']['prediction']['status'],
        'gold_excluded': r['gold_excluded'], 'score': r['score'], 'pruning': r['pruning'],
        'usage': r['attempt']['usage'], 'role_cost': r['role_cost']} for r in records]
    write(root / 'per-case.json', per_case)
    write(root / 'summary.json', {'purpose': plan['purpose'], 'results': totals,
        'gold_exclusions': plan['gold_exclusions'], 'retries': 0,
        'human_labels_assigned': 0, 'true_false_prune_rate': None,
        'interpretation': 'Single stochastic run on 4 selected development cases; no causal gain or semantic false-prune guarantee.'})
    write(root / 'validation.json', {'implementation': implementation_identity(), 'paired_real_predictions': len(records),
        'test_summary': plan['test_summary'], 'test_log_sha256': plan['test_log_sha256'],
        'architecture': plan['architecture'], 'historical_files_verified_unchanged': len(plan['original_hashes']),
        'same_conditions_checked': ['manifest', 'model', 'retriever', 'code', 'limits', 'max_steps'],
        'production_default_enabled': False, 'private_configuration_modified': False,
        'human_labels_assigned': 0})
    write(root / 'artifact-manifest.json', [{'path': str(p.relative_to(root)), 'sha256': digest(p)}
        for p in sorted(root.rglob('*')) if p.is_file() and 'test-temp' not in p.parts])
    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'finalize'])
    parser.add_argument('--output', type=Path, default=Path('output/multicandidate-paths-20261019'))
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.output)
    else:
        plan = json.loads((args.output / 'plan.json').read_text(encoding='utf-8'))
        (run if args.command == 'run' else finalize)(args.output, plan)
