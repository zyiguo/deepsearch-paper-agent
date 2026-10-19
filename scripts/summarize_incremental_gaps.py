"""Actual incremental-gap events and diagnostic lengths, without inferred token savings."""
import argparse
import json
from pathlib import Path
from collections import Counter
from evaluate_multicandidate_paths import write
from summarize_candidate_reuse import summarize


def main(root):
    summarize(root, 'v4', 'v5')
    rows = []
    for source in sorted((root / 'runs').glob('*/attempts.jsonl')):
        for line in source.read_text(encoding='utf-8').splitlines():
            attempt = json.loads(line)
            events = attempt['events']
            raw_lengths, parsed_lengths = [], []
            unparsable = 0
            for event in events:
                if event.get('role') != 'Verifier':
                    continue
                if event['kind'] == 'mainline.rag.response':
                    try:
                        value = json.loads(event['raw'])
                        raw_lengths.extend(len(p['reason']) for p in value.get('paths', [])
                                           if isinstance(p.get('reason'), str))
                    except (ValueError, TypeError, KeyError):
                        unparsable += 1
                elif event['kind'] == 'mainline.rag.parsed':
                    parsed_lengths.extend(len(p['reason']) for p in event['value'].get('paths', []))
            rows.append({'id': attempt['id'], 'run': source.parent.name,
                'gap_deferred_paths': sum(len(e['paths']) for e in events
                                         if e['kind'] == 'mainline.candidates.gap_review_deferred'),
                'verification_skips': dict(Counter(e['reason'] for e in events
                    if e['kind'] == 'mainline.candidates.verification_skipped')),
                'verifier_model_calls': sum(e['kind'] == 'mainline.rag.request' and e.get('role') == 'Verifier'
                                            for e in events),
                'verifier_repairs': sum(e['kind'] == 'mainline.rag.schema_repair' and e.get('role') == 'Verifier'
                                       for e in events),
                'raw_reason_lengths': raw_lengths, 'parsed_reason_lengths': parsed_lengths,
                'raw_reasons_over_280': sum(n > 280 for n in raw_lengths),
                'unparsed_raw_verifier_responses': unparsable,
                'true_false_defer_rate': None,
                'truth_boundary': 'No independent semantic labels for deferred paths; retained unknown is not proof of irrelevance.'})
    write(root / 'gap-analysis.json', rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('output/incremental-gaps-20261019'))
    main(parser.parse_args().output)
