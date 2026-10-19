"""Export an unlabeled path review queue and same-input pruning size counterfactual.

No model calls. Does not modify a run or infer semantic truth from exact-answer labels.
"""
import argparse
import hashlib
import json
from pathlib import Path

from deepsearch_agent.core.evidence_paths import replay_certificate
from deepsearch_agent.core.tokens import estimate_tokens


def write(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def score_labels(queue, labels, output):
    """Only explicit independent labels supply a semantic false-prune denominator."""
    originals = [json.loads(s) for s in queue.read_text(encoding='utf-8').splitlines()]
    reviewed = [json.loads(s) for s in labels.read_text(encoding='utf-8').splitlines()]
    editable = {'reviewed_path_correct', 'reviewed_prune_error', 'reviewer', 'root_cause'}
    def key(row):
        return tuple(row[k] for k in ('id', 'source', 'source_sha256', 'source_event_index',
                                     'candidate_index', 'path_index'))
    by_key = {key(row): row for row in originals}
    seen, judged = set(), []
    for row in reviewed:
        ident = key(row)
        if ident in seen or ident not in by_key:
            raise ValueError('Duplicate or unknown review identity')
        seen.add(ident)
        original = by_key[ident]
        if {k: v for k, v in row.items() if k not in editable} != {
                k: v for k, v in original.items() if k not in editable}:
            raise ValueError('Review changed frozen path, evidence, or pruning metadata')
        correct = row['reviewed_path_correct']
        if correct is None:
            if row['reviewed_prune_error'] is not None:
                raise ValueError('Prune-error label requires an independent path correctness label')
            continue
        if type(correct) is not bool or not isinstance(row['reviewer'], str) or not row['reviewer'].strip():
            raise ValueError('Explicit boolean correctness and independent reviewer are required')
        error = correct and row['actually_pruned']
        if row['reviewed_prune_error'] is not None and (
                type(row['reviewed_prune_error']) is not bool or row['reviewed_prune_error'] != error):
            raise ValueError('Path correctness and prune-error labels disagree')
        judged.append(row)
    correct = [r for r in judged if r['reviewed_path_correct']]
    pruned = [r for r in judged if r['actually_pruned']]
    errors = sum(r['actually_pruned'] for r in correct)
    result = {'total_paths': len(originals), 'reviewed_paths': len(judged),
        'reviewed_correct_path_denominator': len(correct), 'false_pruned_correct_paths': errors,
        'false_prune_rate': errors / len(correct) if correct else None,
        'reviewed_pruned_path_denominator': len(pruned),
        'wrong_prune_fraction_among_reviewed_prunes': errors / len(pruned) if pruned else None,
        'review_coverage': len(judged) / len(originals) if originals else None,
        'queue_sha256': hashlib.sha256(queue.read_bytes()).hexdigest(),
        'labels_sha256': hashlib.sha256(labels.read_bytes()).hexdigest(),
        'truth_boundary': 'Reviewer-supplied path labels; reviewer identity is recorded, not independently authenticated.'}
    write(output, result)
    return result


def main(root, destination):
    destination.mkdir(parents=True, exist_ok=False)
    rows, sizes = [], []
    for source in sorted((root / 'runs').glob('*/attempts.jsonl')):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        for attempt in [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines()]:
            for event_index, event in enumerate(attempt['events']):
                if event['kind'] != 'mainline.candidates.proposed':
                    continue
                before, after = [], []
                for ci, candidate in enumerate(event['candidates']):
                    shell = {'candidate_index': ci, 'answer': candidate['answer'],
                             'interpretation': candidate['interpretation']}
                    all_paths, valid_paths = [], []
                    for pi, path in enumerate(candidate['paths']):
                        raw = {'steps': path['steps']}
                        assert replay_certificate(path['certificate'], raw, event['evidence'])
                        entry = {'path_index': pi, **raw}
                        all_paths.append(entry)
                        would_prune = path['certificate']['status'] == 'invalid_witness'
                        if not would_prune:
                            valid_paths.append(entry)
                        ids = {s['citation'] for s in path['steps']}
                        rows.append({'id': attempt['id'], 'run': source.parent.name,
                            'round': event['round'], 'candidate_index': ci, 'path_index': pi,
                            'answer': candidate['answer'], 'interpretation': candidate['interpretation'],
                            'path': raw, 'certificate': path['certificate'], 'actually_pruned': path['pruned'],
                            'would_prune': would_prune,
                            'evidence_for_review': [e for e in event['evidence'] if e['id'] in ids],
                            'source': str(source.relative_to(root)), 'source_sha256': digest,
                            'source_event_index': event_index,
                            'reviewed_path_correct': None, 'reviewed_prune_error': None,
                            'reviewer': None, 'root_cause': None})
                    before.append({**shell, 'paths': all_paths})
                    after.append({**shell, 'paths': valid_paths})
                before_text, after_text = (json.dumps(payload, ensure_ascii=False) for payload in (before, after))
                sizes.append({'id': attempt['id'], 'run': source.parent.name, 'round': event['round'],
                    'input_utf8_bytes_removed': len(before_text.encode('utf-8')) - len(after_text.encode('utf-8')),
                    'estimated_input_tokens_removed': estimate_tokens(before_text) - estimate_tokens(after_text),
                    'measurement': 'same-candidate verifier payload only; estimate, not provider tokenizer or realized total-token savings'})
    with (destination / 'path-review.jsonl').open('x', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    write(destination / 'same-input-payload-size.json', sizes)
    write(destination / 'summary.json', {'paths': len(rows),
        'actually_pruned': sum(r['actually_pruned'] for r in rows),
        'would_prune': sum(r['would_prune'] for r in rows),
        'human_labels_assigned': 0, 'true_false_prune_rate': None,
        'input_utf8_bytes_removed_counterfactual': sum(r['input_utf8_bytes_removed'] for r in sizes),
        'estimated_input_tokens_removed_counterfactual': sum(r['estimated_input_tokens_removed'] for r in sizes),
        'note': 'Separate stochastic runs cannot isolate total-token savings; this counterfactual holds candidate payload fixed.'})
    print(json.dumps({'review_paths': len(rows), 'destination': str(destination)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, default=Path('output/multicandidate-paths-20261019'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--queue', type=Path, help='Frozen path-review.jsonl, required when scoring labels')
    parser.add_argument('--labels', type=Path, help='Separate reviewed copy; never overwrite the frozen queue')
    args = parser.parse_args()
    if args.labels:
        if not args.queue:
            parser.error('--labels requires --queue')
        print(json.dumps(score_labels(args.queue, args.labels, args.output)))
    else:
        main(args.run_root, args.output)
