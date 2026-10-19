"""Exercise quality closure with labelled synthetic fixtures, without model/network calls."""
import argparse
import json
from pathlib import Path

from deepsearch_tracing.store import TraceStore
from deepsearch_tracing.quality import freeze, review, compare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    scenarios = {
        'fixed': ('incorrect', 'correct'), 'regressed': ('correct', 'incorrect'),
        'unreviewed': ('unknown', 'unknown'), 'missing': ('incorrect', None),
        'changed-corpus': ('incorrect', 'correct'),
    }
    for phase in ['baseline', 'candidate']:
        store = TraceStore(args.output / (phase + '.sqlite3'))
        bindings = {}
        for case_id, pair in scenarios.items():
            verdict = pair[0 if phase == 'baseline' else 1]
            if verdict is None:
                continue
            corpus = 'synthetic-corpus-v2' if phase == 'candidate' and case_id == 'changed-corpus' else 'synthetic-corpus-v1'
            rid = store.start({'question': 'synthetic ' + case_id, 'synthetic': True,
                'research_run_id': phase + '-' + case_id, 'runtime_identity': 'synthetic-runtime-v1',
                'corpus_identity': corpus})
            store.append(rid, 'rag.outcome', {'status': 'verified'})
            store.append(rid, 'answer.final', {'text': 'fixture blue' if verdict == 'correct' else 'fixture orange', 'sources': []})
            store.finish(rid)
            bindings[rid] = (case_id, verdict)
        frozen = freeze(store.path, args.output / phase)
        labels = []
        for row in frozen['runs']:
            case_id, verdict = bindings[row['trace_run_id']]
            labels.append({'trace_run_id': row['trace_run_id'], 'trace_sha256': row['trace_sha256'],
                'case_id': case_id, 'verdict': verdict,
                'root_causes': ['hallucination'] if verdict == 'incorrect' else [],
                'expected': 'fixture blue', 'evidence': 'Synthetic oracle: fixture expected color is blue.',
                'notes': 'Synthetic acceptance label; not a real human judgement of a business answer.',
                'fix_id': 'synthetic-fix-001' if phase == 'candidate' else None})
        review(args.output / phase / 'snapshot.json', labels, 'synthetic-fixture-oracle', args.output / (phase + '-review.json'))
    result = compare(args.output / 'baseline-review.json', args.output / 'candidate-review.json', args.output / 'comparison.json')
    expected = {'resolved': 1, 'regression': 1, 'awaiting_review': 1, 'awaiting_regression': 1, 'incomparable': 1}
    assert result['counts'] == expected
    summary = {'status': 'passed', 'synthetic_inputs': True, 'cloud_model_calls': False,
        'real_business_quality_improvement_established': False, 'comparison_counts': result['counts']}
    with (args.output / 'report.json').open('x', encoding='utf-8') as stream:
        json.dump(summary, stream, indent=2)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
