"""Fallible verifier-reported gaps; reuse only unknown, never positive support."""
from .evidence_paths import fingerprint


def witness(step, evidence):
    item = next((e for e in evidence if e['id'] == step['citation']), None)
    if not item or not step['quote'].strip() or step['quote'] not in item['text']:
        return None
    # Ignore presentation citation numbering; include complete source identity and
    # content so modified documents and visual evidence always reopen review.
    source = {k: v for k, v in item.items() if k != 'id'}
    return fingerprint({'source': source, 'quote': step['quote'],
                        'requirement': step['requirement_index']})


def record_gaps(records, review, evidence, count):
    gaps = {}
    pairs = [(p['candidate_index'], p['path_index']) for p in review['paths']]
    if len(set(pairs)) != len(pairs) or any(
        not 0 <= ci < len(records) or not 0 <= pi < len(records[ci]['paths']) for ci, pi in pairs):
        return gaps
    for ci, candidate in enumerate(records):
        if candidate['status'] != 'unknown':
            continue
        paths = [(pi, p) for pi, p in enumerate(candidate['paths']) if not p['pruned']]
        reports = [r for r in review['paths'] if r['candidate_index'] == ci]
        if (not paths or {r['path_index'] for r in reports} != {pi for pi, _ in paths}
                or any(r['verdict'] != 'unknown' for r in reports)):
            continue
        supported = {i for r in reports for i in r['supported_requirements']}
        if not supported <= set(range(count)):
            continue
        missing = sorted(set(range(count)) - supported)
        if missing:
            gaps[candidate['candidate_id']] = {
                'missing_requirements': missing,
                'witnesses': sorted({w for _, p in paths for s in p['steps']
                                     if (w := witness(s, evidence)) is not None}),
                'review_id': fingerprint(review), 'evidence_id': fingerprint(evidence),
                'status': 'verifier_reported_unknown', 'semantic_truth_certified': False}
    return gaps


def filter_review_paths(payload, records, evidence, gaps):
    filtered, deferred = [], []
    for item in payload:
        candidate = records[item['candidate_index']]
        gap = gaps.get(candidate['candidate_id'])
        paths = []
        for path in item['paths']:
            witnesses = {w for s in path['steps'] if (w := witness(s, evidence)) is not None}
            if gap and witnesses <= set(gap['witnesses']):
                deferred.append({'candidate_id': candidate['candidate_id'],
                    'candidate_index': item['candidate_index'], 'path_index': path['path_index'],
                    'missing_requirements': gap['missing_requirements'], 'review_id': gap['review_id'],
                    'reason': 'no_new_witness_after_unknown_review',
                    'path_id': fingerprint(path), 'status': 'unknown', 'pruned': False})
            else:
                paths.append(path)
        filtered.append({**item, 'paths': paths})
    return filtered, deferred
