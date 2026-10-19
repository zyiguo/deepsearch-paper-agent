"""Read-only identity checks, then exclusive validation and delivery manifests."""
import json
from collections import Counter
from pathlib import Path

from evaluate_multicandidate_paths import digest, write
from deepsearch_experiments.benchmarks.multihop.runner import implementation_identity
from deepsearch_experiments.benchmarks.multihop.datasets import load_manifest
from deepsearch_experiments.benchmarks.multihop.schema import manifest_sha256


def validate(root):
    plan = json.loads((root / 'plan.json').read_text(encoding='utf-8'))
    assert implementation_identity() == plan['implementation']
    assert all(digest(Path(p)) == h for p, h in plan['historical_hashes'].items())
    for item in plan['selection'].values():
        assert digest(Path(item['source'])) == item['source_sha256']
    for item in plan['shards'].values():
        assert digest(Path(item['path'])) == item['sha256']
    rows = json.loads((root / 'per-attempt.json').read_text(encoding='utf-8'))
    assert len(rows) == 120
    assert len({(x['dataset'], x['id']) for x in rows}) == 20
    assert set(Counter((x['dataset'], x['id'], x['version']) for x in rows).values()) == {3}
    retrievers, total_calls, total_tokens, retained = set(), 0, 0, 0
    for job in plan['jobs']:
        path = root / 'runs' / job['name']
        run = json.loads((path / 'run.json').read_text(encoding='utf-8'))
        assert run['implementation'] == plan['implementation']
        assert run['model'] == plan['model'] and run['limits'] == plan['limits']
        assert run['max_steps'] == 6
        # Run identity hashes the normalized manifest; plan hashes file bytes.
        assert run['manifest_sha256'] == manifest_sha256(load_manifest(plan['shards'][job['shard']]['path']))
        retrievers.add(json.dumps(run['retriever'], sort_keys=True))
        attempts = [json.loads(s) for s in (path / 'attempts.jsonl').read_text(encoding='utf-8').splitlines()]
        assert len(attempts) == 2 and all(x['attempt'] == 1 for x in attempts)
        for a in attempts:
            total_calls += a['usage']['model_calls']
            assert a['usage']['usage_missing_calls'] == 0
            total_tokens += a['usage']['reported_total_tokens']
            proposed = []
            for e in a['events']:
                if e['kind'] == 'mainline.candidates.proposed':
                    proposed = e['candidates']
                if e['kind'] == 'mainline.candidates.gap_review_deferred':
                    for d in e['paths']:
                        assert d['pruned'] is False and d['status'] == 'unknown'
                        assert proposed[d['candidate_index']]['candidate_id'] == d['candidate_id']
                        retained += 1
    assert len(retrievers) == 1 and retained == 9
    assert not list(root.rglob('run.lock'))
    for x in rows:
        assert digest(root / x['source']) == x['source_sha256']
    queue = root / 'deferred-review.jsonl'
    audit = json.loads((root / 'blinded-audit/summary.json').read_text())
    assert audit['queue_sha256'] == digest(queue)
    assert audit['natural_deferred_paths'] == retained
    replay = json.loads((root / 'rule-replay.json').read_text())
    assert replay['all_exact'] and replay['count'] == retained
    result = {'implementation': plan['implementation'], 'historical_files_unchanged': len(plan['historical_hashes']),
              'source_manifests_unchanged': len(plan['selection']), 'shards_unchanged': len(plan['shards']),
              'jobs': len(plan['jobs']), 'predictions': len(rows), 'unique_questions': 20,
              'all_attempts_first_attempt': True, 'shared_model_limits_retriever': True,
              'model_calls': total_calls, 'reported_total_tokens': total_tokens,
              'deferred_paths_retained_unknown_at_decision': retained, 'exact_rule_replays': retained,
              'missing_usage_calls': 0, 'remaining_run_locks': 0,
              'audit_cost_outside_main_experiment': True,
              'code_and_document_hashes': {str(p): digest(p) for pattern in
                  ['scripts/*gap*py', 'tests/test_gap_repeat_statistics.py', 'tests/test_deferred_gap_audit.py',
                   'docs/REPEATED_GAP_EVALUATION_20261019.md', 'docs/plans/2026-10-19-repeated-gap-evaluation.md', 'README.md']
                  for p in Path('.').glob(pattern)}}
    write(root / 'validation-final.json', result)
    entries = [{'path': str(p.relative_to(root)), 'sha256': digest(p)} for p in sorted(root.rglob('*')) if p.is_file()]
    write(root / 'delivery-manifest.json', entries)
    assert all(digest(root / e['path']) == e['sha256'] for e in entries)
    print(json.dumps({**result, 'code_and_document_hashes': 'recorded', 'delivery_files': len(entries)}, indent=2))


if __name__ == '__main__':
    validate(Path('output/gap-repeats-20261019'))
