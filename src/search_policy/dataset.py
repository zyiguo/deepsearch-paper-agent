"""Auditable action-SFT export. Labels are external, never inferred from RAG."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import unicodedata
from uuid import uuid4

from .intent import parse_decision
from .store import canonical, digest, validate_run


def normalized_question(question):
    return ' '.join(unicodedata.normalize('NFKC', question).casefold().split())


def candidates(runs):
    rows = []
    seen_runs = set()
    for run in runs:
        validate_run(run)
        if run['run_id'] in seen_runs:
            raise ValueError('Duplicate trace run')
        seen_runs.add(run['run_id'])
        requests, responses, searches, results = {}, {}, {}, {}
        for event in run['events']:
            kind, payload = event['kind'], event['payload']
            if kind == 'policy.request':
                if payload['call_id'] in requests:
                    raise ValueError('Duplicate policy request')
                requests[payload['call_id']] = event
            elif kind == 'policy.response':
                if payload['call_id'] in responses:
                    raise ValueError('Duplicate policy response')
                responses[payload['call_id']] = event
            elif kind == 'retrieval.request':
                searches[payload['search_id']] = event
            elif kind == 'retrieval.result':
                results[payload['search_id']] = event
        for call_id, request in requests.items():
            response = responses.get(call_id)
            data = response['payload'] if response else {}
            reason = 'eligible'
            decision = None
            try:
                if data.get('status') == 'parsed':
                    decision = parse_decision(data.get('raw'))
            except (ValueError, TypeError):
                pass
            executed = [event for event in searches.values()
                        if event['payload'].get('policy_call_id') == call_id
                        and event['payload'].get('origin') == 'policy'
                        and decision is not None and decision.action == 'search'
                        and event['payload']['query'] == decision.query
                        and event['payload']['search_id'] in results]
            if run['status'] != 'completed':
                reason = 'run_not_completed'
            elif decision is None:
                reason = 'invalid_or_missing_action'
            elif decision.action != 'search':
                reason = 'answer_not_executed'
            elif not executed:
                reason = 'search_not_executed'
            messages = request['payload']['messages']
            if (not isinstance(messages, list) or not messages
                or any(not isinstance(m, dict) or m.get('role') not in {'system', 'user', 'assistant'}
                       or not isinstance(m.get('content'), str) or not m['content'].strip() for m in messages)):
                raise ValueError('Invalid recorded messages')
            row = {'schema_version': 1, 'sample_id': digest([run['run_id'], call_id]),
                   'run_id': run['run_id'], 'call_id': call_id,
                   'question': run['metadata']['question'],
                   'conversation_id': run['metadata'].get('conversation_id'),
                   'messages': messages, 'text': data.get('raw'),
                   'model_id': request['payload'].get('model_id', 'unspecified'),
                   'model_revision': request['payload'].get('model_revision', 'unspecified'),
                   'protocol_version': request['payload'].get('protocol_version'),
                   'eligible': reason == 'eligible', 'reason': reason,
                   'request_hash': request['hash'], 'response_hash': response['hash'] if response else None,
                   'run_hash': run['events'][-1]['hash'],
                   'observations': [results[e['payload']['search_id']]['payload'] for e in executed]}
            row['sample_hash'] = digest(row)
            rows.append(row)
    return sorted(rows, key=lambda row: row['sample_id'])


def read_jsonl(path):
    rows = []
    with Path(path).open(encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise ValueError(f'Invalid JSON at line {number}') from None
            if not isinstance(row, dict):
                raise ValueError(f'Expected object at line {number}')
            rows.append(row)
    return rows


def jsonl_bytes(rows):
    return ''.join(canonical(row) + '\n' for row in rows).encode('utf-8')


def write_new_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(jsonl_bytes(rows))


def review_template(rows):
    return [{'sample_id': row['sample_id'], 'sample_hash': row['sample_hash'],
             'decision': 'pending', 'reviewer': '', 'note': ''}
            for row in rows if row['eligible']]


def group_keys(runs):
    """Union questions AND conversations, including unapproved runs."""
    parent = {}

    def root(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    keys = {}
    for run in runs:
        meta = run['metadata']
        q = 'q:' + digest(normalized_question(meta['question']))
        root(q)
        conv = meta.get('conversation_id')
        if conv:
            c = 'c:' + str(conv)
            a, b = root(q), root(c)
            parent[max(a, b)] = min(a, b)
        keys[run['run_id']] = q
    return {run_id: root(key) for run_id, key in keys.items()}


def export_dataset(runs, reviews, output, *, val_ratio=0.2, seed='42'):
    if not 0 <= val_ratio < 1:
        raise ValueError('val_ratio must be in [0, 1)')
    rows = candidates(runs)
    by_id = {row['sample_id']: row for row in rows}
    approved, seen = [], set()
    for review in reviews:
        sample_id = review.get('sample_id')
        if sample_id not in by_id or sample_id in seen:
            raise ValueError('Unknown or duplicate reviewed sample')
        seen.add(sample_id)
        row = by_id[sample_id]
        if review.get('sample_hash') != row['sample_hash']:
            raise ValueError('Review is stale or sample has changed')
        if review.get('decision') not in {'pending', 'approve', 'reject'}:
            raise ValueError('Invalid review decision')
        if review['decision'] == 'approve':
            if not row['eligible']:
                raise ValueError('Cannot approve an ineligible sample')
            if not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
                raise ValueError('Approved samples require a reviewer')
            approved.append((row, review))
    groups = group_keys(runs)
    prompts, unique = {}, {}
    for row, review in sorted(approved, key=lambda pair: pair[0]['sample_id']):
        prompt_key = digest(row['messages'])
        # Equivalent whitespace in action JSON should not create duplicate labels.
        target_key = digest(json.loads(row['text']))
        if prompt_key in prompts and prompts[prompt_key] != target_key:
            raise ValueError('Conflicting approved targets for identical messages')
        prompts[prompt_key] = target_key
        key = (prompt_key, target_key)
        if key in unique:
            unique[key]['provenance'].append({'sample_id': row['sample_id'], 'run_id': row['run_id'],
                'sample_hash': row['sample_hash'], 'model_id': row['model_id'],
                'model_revision': row['model_revision'], 'review': review})
            continue
        unique[key] = {'id': digest(key), 'messages': row['messages'], 'text': row['text'],
            'loss_scope': 'completion_only', 'group_id': digest(groups[row['run_id']]),
            'model_id': row['model_id'], 'model_revision': row['model_revision'],
            'protocol_version': row['protocol_version'],
            'provenance': [{'sample_id': row['sample_id'], 'run_id': row['run_id'],
                            'sample_hash': row['sample_hash'], 'model_id': row['model_id'],
                            'model_revision': row['model_revision'], 'review': review}]}
    if not unique:
        raise ValueError('No approved eligible samples; no dataset created')
    splits = {'train': [], 'val': []}
    for row in sorted(unique.values(), key=lambda r: r['id']):
        fraction = int(digest([str(seed), row['group_id']])[:16], 16) / 2**64
        split = 'val' if fraction < val_ratio else 'train'
        splits[split].append(row)
    files = {name + '.jsonl': jsonl_bytes(items) for name, items in splits.items()}
    manifest = {'schema_version': 1, 'format': 'search-policy-action-sft-v1',
        'seed': str(seed), 'val_ratio': val_ratio, 'split_strategy': 'question-conversation-components-hash',
        'trace_snapshot_hash': digest(sorted((r['run_id'], r['events'][-1]['hash']) for r in runs)),
        'reviews_hash': digest(sorted(reviews, key=lambda r: r['sample_id'])),
        'counts': {**{k: len(v) for k, v in splits.items()}, 'runs': len(runs),
                   'candidates': len(rows), 'approved': len(approved), 'deduplicated': len(approved) - len(unique)},
        'candidate_reasons': dict(Counter(r['reason'] for r in rows)),
        'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        'notes': ['No automatic reward or ground-truth labels.',
                  'Single action samples; not a complete Search-R1 replay bundle.',
                  'Hash splitting can produce an empty split for small datasets.']}
    output = Path(output)
    if output.exists():
        raise FileExistsError('Dataset output already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(output.name + '.partial-' + uuid4().hex)
    staging.mkdir()
    for name, data in files.items():
        (staging / name).write_bytes(data)
    (staging / 'manifest.json').write_text(canonical(manifest) + '\n', encoding='utf-8')
    staging.rename(output)
    return manifest


def verify_dataset(path):
    path = Path(path)
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 'search-policy-action-sft-v1' or manifest.get('schema_version') != 1:
        raise ValueError('Unsupported dataset format')
    seen_ids, seen_groups = set(), set()
    for split in ('train', 'val'):
        name = split + '.jsonl'
        if hashlib.sha256((path / name).read_bytes()).hexdigest() != manifest['sha256'][name]:
            raise ValueError('Dataset file hash mismatch')
        rows = read_jsonl(path / name)
        if len(rows) != manifest['counts'][split]:
            raise ValueError('Dataset count mismatch')
        groups = set()
        for row in rows:
            if row['id'] in seen_ids or row['group_id'] in seen_groups:
                raise ValueError('Duplicate sample or cross-split group leakage')
            if parse_decision(row['text']).action != 'search' or row['loss_scope'] != 'completion_only':
                raise ValueError('Invalid SFT target')
            seen_ids.add(row['id'])
            groups.add(row['group_id'])
        seen_groups.update(groups)
    return manifest
