"""Expand local multi-hop evaluation without altering production code or data.

Preparation and scoring are offline. ``run`` explicitly invokes existing remote
benchmark CLIs, with bounded concurrency and the prior per-question budgets.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys

from deepsearch_experiments.benchmarks.multihop.datasets import import_logicrag, load_manifest, write_manifest
from deepsearch_experiments.benchmarks.multihop.metrics import evaluate_predictions
from deepsearch_experiments.benchmarks.multihop.runner import read_attempts, score_run
from deepsearch_experiments.identity import source_fingerprint


DATASETS = ('musique', '2wikimultihopqa')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write('\n')


def question_key(question):
    return ' '.join(question.casefold().split())


def select_cases(cases, strata, excluded, count, seed):
    """Largest-remainder proportional sampling, independent of gold labels."""
    if type(count) is not int or count < 1:
        raise ValueError('count must be a positive integer')
    old_ids = {case.id for case in excluded}
    seen = {question_key(case.question) for case in excluded}
    grouped = defaultdict(list)
    for case in sorted(cases, key=lambda case: case.id):
        key = question_key(case.question)
        if case.id in old_ids or key in seen:
            continue
        seen.add(key)
        grouped[strata[case.id]].append(case.id)
    available = sum(map(len, grouped.values()))
    if available < count:
        raise ValueError('Not enough novel unique questions')
    quotas = {key: len(values) * count // available for key, values in grouped.items()}
    remainders = sorted(grouped, key=lambda key: (-(len(grouped[key]) * count % available), key))
    for key in remainders[:count - sum(quotas.values())]:
        quotas[key] += 1
    rng = random.Random(seed)
    ids = sorted(ident for key in sorted(grouped) for ident in rng.sample(grouped[key], quotas[key]))
    return ids, dict(sorted(quotas.items())), available


def subset(manifest, ids, seed):
    selected = set(ids)
    if len(selected) != len(ids) or not selected <= {case.id for case in manifest.cases}:
        raise ValueError('Invalid subset population')
    result = manifest.model_copy(update={
        'cases': [case for case in manifest.cases if case.id in selected],
        'gold': [gold for gold in manifest.gold if gold.id in selected],
        'limit': len(ids), 'seed': seed})
    return type(manifest).model_validate(result.model_dump())


def prepare(root, reference, *, count=100, seed=20261018, shard_size=5, workers=8):
    if not 1 <= workers <= 8 or shard_size < 1:
        raise ValueError('Invalid shard size or worker limit')
    root, reference = Path(root).resolve(), Path(reference).resolve()
    if root.exists():
        raise FileExistsError(root)
    plan = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'protocol': 'stratified-novel-questions-v1', 'count_per_dataset': count,
            'seed': seed, 'workers': workers, 'shard_size': shard_size,
            'source_sha256': source_fingerprint(), 'reference': str(reference),
            'project_root': str(Path.cwd().resolve()), 'inputs': {}, 'datasets': {}, 'jobs': []}
    # Validate every source before creating the output directory.
    prepared = []
    for dataset in DATASETS:
        old_path = Path('data/benchmarks/multihop/prepared') / dataset / 'pilot-10.json'
        old = load_manifest(old_path)
        source = Path('data/benchmarks/multihop/upstream/logicrag/dataset')
        questions, corpus = source / f'{dataset}.json', source / f'{dataset}_corpus.json'
        full = import_logicrag(questions, corpus, dataset=dataset, split=old.split,
                              source_revision=old.source_revision)
        if {s.kind: s.sha256 for s in full.sources} != {s.kind: s.sha256 for s in old.sources}:
            raise ValueError('Upstream source changed relative to prior pilot')
        raw = read(questions)
        strata = {row['id']: row['id'].split('__')[0] for row in raw} if dataset == 'musique' else {
            row['_id']: row['type'] for row in raw}
        ids, allocation, available = select_cases(full.cases, strata, old.cases, count, seed)
        selected = subset(full, ids, seed)
        reference_run = reference / f'{dataset}-mainline'
        runtime = read(reference_run / 'run.json')
        if runtime['method']['name'] != 'mainline' or runtime['implementation']['sha256'] != plan['source_sha256']:
            raise ValueError('Reference code or method differs; do not silently change experimental conditions')
        for path in (questions, corpus, old_path, reference_run / 'run.json', reference_run / 'attempts.jsonl'):
            plan['inputs'][str(path.resolve())] = sha(path)
        plan['datasets'][dataset] = {'manifest': f'manifests/{dataset}/all.json',
            'strata': {ident: strata[ident] for ident in ids}, 'allocation': allocation,
            'novel_unique_available': available, 'excluded_pilot_ids': [case.id for case in old.cases],
            'case_ids': ids, 'expected_runtime': {k: v for k, v in runtime.items() if k != 'manifest_sha256'}}
        prepared.append((dataset, selected, ids))
    root.mkdir(parents=True, exist_ok=False)
    # Interleaved small shards prevent long MuSiQue cases from holding all workers.
    jobs_by_dataset = defaultdict(list)
    for dataset, selected, ids in prepared:
        write_manifest(selected, root / plan['datasets'][dataset]['manifest'])
        for start in range(0, len(ids), shard_size):
            index = start // shard_size
            rel = f'manifests/{dataset}/shard-{index:03d}.json'
            shard = subset(selected, ids[start:start + shard_size], seed)
            write_manifest(shard, root / rel)
            jobs_by_dataset[dataset].append({'id': f'{dataset}-{index:03d}', 'dataset': dataset,
                'manifest': rel, 'run': f'runs/{dataset}-{index:03d}',
                'manifest_sha256': sha(root / rel), 'case_ids': [case.id for case in shard.cases]})
    for index in range(max(map(len, jobs_by_dataset.values()))):
        for dataset in DATASETS:
            if index < len(jobs_by_dataset[dataset]):
                plan['jobs'].append(jobs_by_dataset[dataset][index])
    for info in plan['datasets'].values():
        info['manifest_sha256'] = sha(root / info['manifest'])
    write(root / 'plan.json', plan)
    return plan


def check_plan(root):
    root = Path(root).resolve()
    plan = read(root / 'plan.json')
    if source_fingerprint() != plan['source_sha256']:
        raise ValueError('Source identity changed; use a new experiment')
    if any(not Path(p).is_file() or sha(p) != expected for p, expected in plan['inputs'].items()):
        raise ValueError('Source data or reference changed')
    for dataset, info in plan['datasets'].items():
        if sha(root / info['manifest']) != info['manifest_sha256']:
            raise ValueError('Frozen evaluation manifest changed')
        assigned = [ident for job in plan['jobs'] if job['dataset'] == dataset for ident in job['case_ids']]
        if len(assigned) != len(set(assigned)) or set(assigned) != set(info['case_ids']):
            raise ValueError('Shard population overlaps or omits cases')
    for job in plan['jobs']:
        if sha(root / job['manifest']) != job['manifest_sha256']:
            raise ValueError('Frozen shard manifest changed')
        if [case.id for case in load_manifest(root / job['manifest']).cases] != job['case_ids']:
            raise ValueError('Shard assignment mismatch')
    return plan


def run_batch(root):
    root = Path(root).resolve()
    plan = check_plan(root)
    if Path.cwd().resolve() != Path(plan['project_root']):
        raise ValueError('Run from the original project root')
    from dataclasses import replace
    from deepsearch_agent.adapters.llm_config import LLMSettings
    settings = LLMSettings.load(Path('.env'))
    for info in plan['datasets'].values():
        expected = info['expected_runtime']['model']
        limits = info['expected_runtime']['limits']
        current = replace(settings, context_window=limits['context_window'],
                          max_output_tokens=limits['max_output_tokens'],
                          timeout_seconds=min(settings.timeout_seconds, limits['deadline_seconds']))
        actual = {key: getattr(current, key) for key in expected if key != 'proxy_policy'}
        actual['proxy_policy'] = 'direct'
        if actual != expected:
            raise ValueError('Current model configuration differs from frozen reference')
    lock = root / 'batch.lock'
    with lock.open('x', encoding='utf-8') as handle:
        handle.write(str(os.getpid()))
    def execute(job):
        if source_fingerprint() != plan['source_sha256']:
            raise ValueError('Source changed during evaluation; refusing further dispatch')
        run = root / job['run']
        runtime = plan['datasets'][job['dataset']]['expected_runtime']
        if (run / 'run.lock').exists():
            raise ValueError('Existing shard writer lock; inspect its process before recovery')
        if (run / 'run.json').exists():
            previous = score_run(load_manifest(root / job['manifest']), run)
            if {k: v for k, v in previous['run'].items() if k != 'manifest_sha256'} != runtime:
                raise ValueError('Existing shard runtime identity differs')
            if not previous['summary']['missing_predictions']:
                return {'job': job['id'], 'status': 'already_recorded'}
        limits = runtime['limits']
        args = [sys.executable, '-u', '-m', 'deepsearch_experiments.benchmarks.multihop', 'run',
                '--manifest', str(root / job['manifest']), '--method', 'mainline',
                '--output', str(run), '--allow-remote', '--direct', '--llm-env', str(Path('.env').resolve()),
                '--max-steps', str(runtime['max_steps'])]
        for option, field in (('--k', 'k'), ('--max-model-calls', 'model_calls'),
                              ('--max-search-calls', 'search_calls'), ('--token-budget', 'token_budget'),
                              ('--max-output-tokens', 'max_output_tokens'), ('--context-window', 'context_window'),
                              ('--deadline-seconds', 'deadline_seconds')):
            args.extend((option, str(limits[field])))
        if (run / 'run.json').exists():
            args.append('--resume')  # Never --retry-errors: failed attempts remain saved.
        with (root / (job['id'] + '.log')).open('ab') as log:
            child = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            status = child.wait()
        return {'job': job['id'], 'status': 'finished' if status == 0 else 'process_error', 'exit_code': status}
    try:
        with ThreadPoolExecutor(max_workers=plan['workers']) as pool:
            futures = [pool.submit(execute, job) for job in plan['jobs']]
            for future in as_completed(futures):
                result = future.result()
                with (root / 'progress.jsonl').open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(result) + '\n')
                print(json.dumps(result), flush=True)
    except BaseException:
        # Leave the coordinator lock for deliberate recovery if children may still run.
        raise
    else:
        lock.unlink()


def summarize(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    plan = check_plan(root)
    collected, all_cases = {}, []
    for dataset, info in plan['datasets'].items():
        manifest = load_manifest(root / info['manifest'])
        predictions, attempts, origins = {}, [], {}
        for job in (job for job in plan['jobs'] if job['dataset'] == dataset):
            run = root / job['run']
            if not (run / 'run.json').exists():
                continue
            shard = load_manifest(root / job['manifest'])
            score_run(shard, run)  # Validates manifest/run/prediction identities and schemas.
            identity = read(run / 'run.json')
            if {k: v for k, v in identity.items() if k != 'manifest_sha256'} != info['expected_runtime']:
                raise ValueError('Shard code/model/budget/retrieval conditions differ')
            raw, latest = read_attempts(run / 'attempts.jsonl', shard, identity)
            if set(latest) & set(predictions):
                raise ValueError('Duplicate case across shard runs')
            predictions.update({key: row['prediction'] for key, row in latest.items()})
            attempts.extend(raw)
            origins.update({key: {'run': job['run'], 'attempts_sha256': sha(run / 'attempts.jsonl'),
                                  'run_sha256': sha(run / 'run.json'), 'attempt': row['attempt']}
                            for key, row in latest.items()})
        report = evaluate_predictions(manifest, predictions)
        report.update(attempts=len(attempts), total_model_calls=sum(row['usage']['model_calls'] for row in attempts),
                      total_search_calls=sum(row['usage']['search_calls'] for row in attempts),
                      known_reported_tokens=sum(row['usage']['known_reported_tokens'] for row in attempts),
                      usage_missing_calls=sum(row['usage']['usage_missing_calls'] for row in attempts),
                      corpus_scope=manifest.corpus_scope, strata=info['allocation'])
        report['reported_total_tokens'] = None if report['usage_missing_calls'] else report['known_reported_tokens']
        report['by_stratum'] = {}
        for stratum in info['allocation']:
            ids = [ident for ident, group in info['strata'].items() if group == stratum]
            if ids:
                selected = subset(manifest, ids, plan['seed'])
                report['by_stratum'][stratum] = evaluate_predictions(selected, {
                    ident: predictions[ident] for ident in ids if ident in predictions})['summary']
        for row in report['per_case']:
            missing = row['status'] == 'missing'
            all_cases.append({**row, 'dataset': dataset, 'source': origins.get(row['id']),
                'benchmark_outcome': 'unknown' if missing else 'success' if row['answer_em'] == 1 else 'failure',
                'human_verdict': None, 'root_causes': None})
        collected[dataset] = report
    output.mkdir(parents=True, exist_ok=False)
    for dataset, report in collected.items():
        write(output / f'{dataset}.json', report)
    counts = dict(Counter(row['benchmark_outcome'] for row in all_cases))
    result = {'schema_version': 1, 'status': 'incomplete' if counts.get('unknown') else 'completed',
              'plan_sha256': sha(root / 'plan.json'), 'source_sha256': plan['source_sha256'],
              'population': len(all_cases), 'automatic_outcome_counts': counts,
              'human_reviewed': 0, 'semantic_distortion_rate': None, 'inputs_unchanged': True,
              'datasets': {key: {**value['summary'], 'total_model_calls': value['total_model_calls'],
                                'reported_total_tokens': value['reported_total_tokens'],
                                'known_reported_tokens': value['known_reported_tokens'],
                                'usage_missing_calls': value['usage_missing_calls']}
                           for key, value in collected.items()},
              'interpretation': 'Novel stratified holdout; failures stay in denominator; no aggregate accuracy or human correctness inference.'}
    for group in ('success', 'failure', 'unknown'):
        with (output / (group + '.jsonl')).open('x', encoding='utf-8') as handle:
            for row in all_cases:
                if row['benchmark_outcome'] == group:
                    handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    write(output / 'summary.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--reference', type=Path, required=True)
    prep.add_argument('--count', type=int, default=100)
    prep.add_argument('--seed', type=int, default=20261018)
    prep.add_argument('--workers', type=int, default=8)
    prep.add_argument('--shard-size', type=int, default=5)
    run = sub.add_parser('run')
    run.add_argument('--root', type=Path, required=True)
    run.add_argument('--allow-remote', action='store_true')
    score = sub.add_parser('score')
    score.add_argument('--root', type=Path, required=True)
    score.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        plan = prepare(args.output, args.reference, count=args.count, seed=args.seed,
                       workers=args.workers, shard_size=args.shard_size)
        print(json.dumps({'prepared_cases': sum(len(d['case_ids']) for d in plan['datasets'].values()),
                          'shards': len(plan['jobs']), 'allocation': {
                              key: value['allocation'] for key, value in plan['datasets'].items()}}))
    elif args.command == 'run':
        if not args.allow_remote:
            parser.error('Real model evaluation requires --allow-remote')
        run_batch(args.root)
    else:
        print(json.dumps(summarize(args.root, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
