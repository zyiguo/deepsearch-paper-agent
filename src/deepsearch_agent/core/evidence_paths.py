"""Replayable proof-path integrity checks. No semantic entailment or closed-world claims."""
from copy import deepcopy
import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def check_path(path, evidence):
    """Check the exact prompt snapshot, never a larger unseen document.

    A bad witness invalidates this submitted proof, not the candidate proposition.
    Missing witnesses are unknown. Semantic contradictions require independent review.
    """
    by_id = {item['id']: item for item in evidence}
    failures, missing = [], []
    for index, step in enumerate(path['steps']):
        citation, quote = step['citation'], step['quote']
        if citation not in by_id:
            failures.append({'step': index, 'rule': 'unauthorized_citation', 'citation': citation})
        elif not quote.strip():
            missing.append(index)
        elif quote not in by_id[citation]['text']:
            failures.append({'step': index, 'rule': 'non_verbatim_witness', 'citation': citation})
    return {'checker': 'witness-integrity-v1', 'path_id': fingerprint(path),
            'evidence_id': fingerprint(evidence), 'failures': failures,
            'missing_steps': missing, 'status': 'invalid_witness' if failures else
            'unknown' if missing or not path['steps'] else 'witness_valid',
            'scope': 'submitted_proof_only', 'semantic_entailment_checked': False}


def replay_certificate(certificate, path, evidence):
    """False for stale evidence, modified paths, or forged certificate fields."""
    return certificate == check_path(path, evidence)


def inspect_candidates(candidates, evidence, *, hard_pruning):
    records = []
    for candidate in candidates:
        record = deepcopy(candidate)
        record['candidate_id'] = fingerprint({'answer': candidate['answer'],
                                              'interpretation': candidate['interpretation']})
        record['status'] = 'unknown'
        for path in record['paths']:
            certificate = check_path(path, evidence)
            path.update(certificate=certificate, status='unknown',
                        pruned=hard_pruning and certificate['status'] == 'invalid_witness')
        records.append(record)
    return records


def merge_candidate_pool(previous, proposals, *, capacity=4):
    """Never evict an unresolved candidate to make a new guess fit a fixed budget.

    Old proof snapshots remain in history; an old answer needs a fresh path against
    the current prompt before it can be supported again.
    """
    pool = [{'answer': c['answer'], 'interpretation': c['interpretation'], 'paths': []} for c in previous]
    overflow = []
    for proposal in proposals:
        key = (proposal['answer'], proposal['interpretation'])
        index = next((i for i, c in enumerate(pool) if (c['answer'], c['interpretation']) == key), None)
        if index is not None:
            pool[index] = deepcopy(proposal)
        elif len(pool) < capacity:
            pool.append(deepcopy(proposal))
        else:
            overflow.append(deepcopy(proposal))
    return pool, overflow


def resolve_candidates(records, review, requirement_count):
    """Fail closed on malformed reviews; preserve unknown and contested candidates.

    The supported state is explicitly model-reported, not a formal truth certificate.
    """
    records = deepcopy(records)
    by_pair = {}
    malformed = False
    for item in review['paths']:
        ci, pi = item['candidate_index'], item['path_index']
        if (not 0 <= ci < len(records) or not 0 <= pi < len(records[ci]['paths'])
                or (ci, pi) in by_pair):
            malformed = True
        else:
            by_pair[ci, pi] = item
    for ci, candidate in enumerate(records):
        for pi, path in enumerate(candidate['paths']):
            item = by_pair.get((ci, pi))
            path['review'] = item
            if path['pruned']:
                continue
            if item and item['verdict'] == 'contradicted':
                path['status'] = 'contested'  # A semantic judge cannot hard-prune.
            elif (item and item['verdict'] == 'supported'
                  and path['certificate']['status'] == 'witness_valid'
                  and {s['requirement_index'] for s in path['steps']} == set(range(requirement_count))
                  and set(item['supported_requirements']) == set(range(requirement_count))
                  and requirement_count > 0):
                path['status'] = 'supported'
        if any(p['status'] == 'supported' for p in candidate['paths']):
            candidate['status'] = 'supported'
        elif any(p['status'] == 'contested' for p in candidate['paths']):
            candidate['status'] = 'contested'
    selected = review['selected_candidate']
    supported_answers = {' '.join(c['answer'].casefold().split()) for c in records
                         if c['status'] == 'supported'}
    accepted = (not malformed and review['answer_resolved'] and not review['issues']
                and not review['queries'] and len(supported_answers) == 1
                and selected is not None and 0 <= selected < len(records)
                and records[selected]['status'] == 'supported')
    return records, selected if accepted else None
