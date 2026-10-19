"""Deterministic review routing and provenance checks; never certifies semantic truth."""
import hashlib
import json


def review_input_id(question, claims, evidence, requirements, assumptions, policy=None, context=None):
    data = {'question': question, 'claims': claims, 'evidence': evidence,
            'requirements': requirements, 'assumptions': assumptions, 'policy': policy, 'context': context}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def classify_findings(findings, legacy_issues, requirement_count, claim_count):
    blockers, notes, accepted = [], [], []
    for item in findings:
        valid = (all(0 <= i < requirement_count for i in item['requirement_indices'])
                 and all(0 <= i < claim_count for i in item['claim_indices']))
        if not valid:
            blockers.append('Invalid review target: ' + item['message'])
            continue
        accepted.append(item)
        (blockers if item['blocking'] else notes).append(item['message'])
    # Old free text never becomes a harmless note just because the new list omits it.
    blockers.extend(legacy_issues)
    actions = {f['action'] for f in accepted if f['blocking']}
    action = next((a for a in ('stop', 'research', 'revise') if a in actions), '')
    if blockers and not action:
        action = 'stop'
    return blockers, notes, action


def apply_assumption_updates(assumptions, updates, evidence):
    """Check quotation provenance; the independent verifier judges entailment."""
    current = {item['key']: dict(item) for item in assumptions}
    by_id = {item['id']: item for item in evidence}
    changes, rejected = [], []
    keys = [item['key'] for item in updates]
    duplicates = {key for key in keys if keys.count(key) > 1}
    for item in updates:
        key = item['key']
        valid = (key not in duplicates and item['citations'] and item['quote'].strip()
                 and all(ref in by_id and item['quote'] in by_id[ref].get('text', '')
                         for ref in item['citations']))
        if not valid or (key not in current and len(current) >= 8):
            rejected.append(key)
            continue
        old = current.get(key)
        if old != item:
            current[key] = dict(item)
            changes.append({'key': key, 'old': old, 'new': dict(item)})
    return list(current.values()), changes, rejected
