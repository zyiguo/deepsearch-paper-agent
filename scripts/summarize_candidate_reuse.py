"""Audit actual provider usage and observable reuse, without counterfactual cost claims."""
from collections import Counter
import json
from pathlib import Path
from evaluate_multicandidate_paths import write


def summarize(root, baseline='v3', optimized='v4'):
    cases = json.loads((root / 'per-case.json').read_text(encoding='utf-8'))
    pairs = []
    for identity in sorted({r['id'] for r in cases}):
        rows = {r['method'].rsplit('-', 1)[-1]: r for r in cases if r['id'] == identity}
        before, after = rows[baseline], rows[optimized]
        a, b = (r['usage']['reported_total_tokens'] for r in (before, after))
        pairs.append({'id': identity, 'baseline_tokens': a, 'optimized_tokens': b,
            'token_delta': b - a if a is not None and b is not None else None,
            'token_reduction_fraction': (a-b)/a if a and b is not None else None,
            'baseline_answer': before['answer'], 'optimized_answer': after['answer'],
            'baseline_em': before['score']['answer_em'], 'optimized_em': after['score']['answer_em'],
            'gold_excluded': before['gold_excluded'],
            'baseline_role_cost': before['role_cost'], 'optimized_role_cost': after['role_cost']})
    mechanisms = []
    for path in sorted((root / 'runs').glob('*/attempts.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            attempt = json.loads(line)
            events = attempt['events']
            proposed = [e for e in events if e['kind'] == 'mainline.candidates.proposed']
            reviewed = [e for e in events if e['kind'] == 'mainline.candidates.reviewed']
            retained = True
            old = set()
            for event in proposed:
                current = {c['candidate_id'] for c in event['candidates']}
                retained &= old <= current
                old = current
            mechanisms.append({'id': attempt['id'], 'run': path.parent.name,
                'gap_deferred_paths': sum(len(e['paths']) for e in events if e['kind'] == 'mainline.candidates.gap_review_deferred'),
                'schema_repairs': sum(e['kind'] == 'mainline.rag.schema_repair' for e in events),
                'generation_skips': sum(e['kind'] == 'mainline.candidates.generation_skipped' for e in events),
                'verification_skips_by_reason': dict(Counter(e['reason'] for e in events
                    if e['kind'] == 'mainline.candidates.verification_skipped')),
                'explicit_id_update_proposals': sum(bool(p.get('existing_candidate_id'))
                    for e in proposed for p in e.get('raw_proposals', [])),
                'rejected_updates': sum(len(e.get('rejected_updates', [])) for e in proposed),
                'all_previous_candidate_ids_retained': retained,
                'final_states': [c['status'] for c in reviewed[-1]['candidates']] if reviewed else [],
                'proposal_rounds': len(proposed), 'review_events': len(reviewed)})
    result = {'pairs': pairs, 'mechanisms': mechanisms,
        'cost_basis': 'Actual provider reported total tokens; missing stays null. Independent stochastic runs, no seed.',
        'limitations': 'Four outcome-selected development cases; different search/model outputs confound mechanism attribution. '
                       'Retained candidate IDs prove no eviction, not semantic completeness or zero false merges. '
                       'True false-prune rate requires independent path labels.'}
    write(root / 'reuse-analysis.json', result)
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('output/candidate-reuse-20261019'))
    print(json.dumps(summarize(parser.parse_args().output), ensure_ascii=False, indent=2))
