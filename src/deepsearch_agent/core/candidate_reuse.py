"""Conservative identity reuse; never infer semantic equivalence from similar prose."""
from copy import deepcopy
from .evidence_paths import fingerprint


def registry(candidates):
    return [{k: c[k] for k in ('candidate_id', 'answer', 'interpretation', 'status')}
            for c in candidates]


def merge_updates(previous, proposals, capacity=4):
    # Clear current proofs; historical snapshots retain them. A changed evidence
    # snapshot must never inherit old citation numbering or semantic support.
    pool = [{k: c[k] for k in ('answer', 'interpretation')} | {'paths': []} for c in previous]
    ids = {c['candidate_id']: i for i, c in enumerate(previous)}
    rejected = []
    def key(c):
        return tuple(' '.join(c[k].split()) for k in ('answer', 'interpretation'))
    for raw in proposals:
        proposal = deepcopy(raw)
        identity = proposal.pop('existing_candidate_id', None)
        if identity is not None and identity not in ids:
            rejected.append({'reason': 'unknown_candidate_id', 'proposal': raw})
            continue
        index = ids.get(identity) if identity is not None else next(
            (i for i, c in enumerate(pool) if key(c) == key(proposal)), None)
        if index is None:
            if len(pool) >= capacity:
                rejected.append({'reason': 'fixed_candidate_capacity', 'proposal': raw})
                continue
            index = len(pool)
            pool.append({'answer': proposal['answer'], 'interpretation': proposal['interpretation'], 'paths': []})
        # Explicit ID means adding proofs to the immutable old proposition, not
        # changing its meaning. The verifier sees that old proposition.
        paths = pool[index]['paths']
        seen = {fingerprint(p) for p in paths}
        for path in proposal['paths']:
            if fingerprint(path) not in seen and len(paths) < 3:
                paths.append(path)
                seen.add(fingerprint(path))
    return pool, rejected
