"""Offline, evidence-linked observations; never replaces benchmark or human labels."""
import argparse
import collections
import hashlib
import json
import pathlib
import re


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root, destination):
    manifest = read(root / 'trace-manifest.json')
    source_hashes = {item['path']: item['sha256'] for item in manifest}
    for name, expected in source_hashes.items():
        if digest(root / name) != expected:
            raise ValueError('Trace integrity failure: ' + name)
    datasets = {d: read(root / 'manifests' / d / 'all.json')
                for d in ('musique', '2wikimultihopqa')}
    input_paths = [root / 'trace-manifest.json'] + [root / 'manifests' / d / 'all.json' for d in datasets]
    input_hashes = {str(p.relative_to(root)): digest(p) for p in input_paths}
    from deepsearch_agent.agents.contracts import Decision, Draft, Verdict
    from deepsearch_agent.agents.agentic_research import ToolAction
    from pydantic import ValidationError
    schemas = {'Supervisor': Decision, 'Researcher': ToolAction,
               'Answerer': Draft, 'Verifier': Verdict}
    records = []
    for relative in source_hashes:
        trace = read(root / relative)
        attempt = trace['attempts'][-1]
        events = attempt['events']
        dataset = datasets[trace['dataset']]
        gold = next(g for g in dataset['gold'] if g['id'] == trace['case_id'])
        question = next(q['question'] for q in dataset['cases'] if q['id'] == trace['case_id'])
        support = set(gold['support_document_ids'])
        retrieved = set(attempt['prediction']['retrieved_document_ids'])
        details = attempt['details']
        observations = []

        def add(label, indices, **facts):
            observations.append({'label': label, 'confidence': 'confirmed_observation',
                                 'event_indices': indices, 'facts': facts})

        search = [(i, e) for i, e in enumerate(events) if e['kind'] == 'search']
        seen = set()
        no_novel = []
        for i, e in search:
            ids = set(e['document_ids'])
            if not ids - seen:
                no_novel.append(i)
            seen |= ids
        if no_novel:
            add('search_returned_no_new_documents', no_novel, count=len(no_novel))
        if support <= retrieved:
            add('all_annotated_support_retrieved', [], support_document_ids=sorted(support))
        inputs = [(i, e) for i, e in enumerate(events)
                  if e['kind'] == 'mainline.communication.model_input']
        role_coverage = {}
        for role in schemas:
            full = []
            for i, e in inputs:
                if e['role'] != role:
                    continue
                evidence = e['data'].get('evidence', [])
                ids = {x.get('source_path', x.get('source')) for x in evidence if isinstance(x, dict)}
                if support <= ids:
                    full.append(i)
            role_coverage[role] = full
        for role in ('Answerer', 'Verifier'):
            if role_coverage[role]:
                add('all_annotated_support_in_' + role.lower() + '_input', role_coverage[role])

        reason = details.get('termination_reason')
        if attempt['prediction']['status'] == 'budget_exhausted':
            add('budget_exhausted_' + str(reason), [],
                model_calls=attempt['usage']['model_calls'], search_calls=attempt['usage']['search_calls'])
            if not any(e['role'] == 'Answerer' for _, e in inputs):
                add('budget_stopped_before_first_draft', [])
        failures = []
        parsed_calls = {e.get('call_id') for e in events if e['kind'] == 'mainline.rag.parsed'}
        for i, e in enumerate(events):
            if e['kind'] != 'mainline.rag.response':
                continue
            raw = e['raw'].strip()
            if raw.startswith('```') and raw.endswith('```'):
                raw = re.sub(r'^```(?:json)?\s*', '', raw)[:-3].strip()
            try:
                schemas[e['role']].model_validate_json(raw)
            except ValidationError as error:
                failures.append({'event_index': i, 'role': e['role'], 'call_id': e['call_id'],
                    'errors': [{'field': list(x['loc']), 'type': x['type']}
                               for x in error.errors(include_input=False, include_url=False)],
                    'parsed_same_call': e['call_id'] in parsed_calls})
        if attempt['prediction']['status'] == 'invalid_output' and failures:
            last = failures[-1]
            add('terminal_schema_rejection_replayed', [last['event_index']], **last)
            if last['errors'] == [{'field': ['research_rounds'], 'type': 'extra_forbidden'}]:
                candidate = json.loads(events[last['event_index']]['raw'])
                candidate.pop('research_rounds')
                schemas[last['role']].model_validate(candidate)
                add('offline_remove_extra_research_rounds_validates', [last['event_index']],
                    limitation='Static validation only; not a completed run or answer correctness proof.')
        for i, e in enumerate(events):
            if e['kind'] == 'model' and e.get('status') != 'ok':
                add('model_service_error', [i], diagnostic={k: v for k, v in e.items()
                    if k not in {'response', 'prompt', 'kind'}})
        deliveries = [(i, e) for i, e in enumerate(events)
                      if e['kind'] == 'mainline.communication.parent_delivery']
        omitted = [(i, e) for i, e in deliveries if e.get('omitted_task_ids')
                   or any(x.get('omitted_findings', 0) for x in e.get('delivered_results', []))]
        if omitted:
            add('parent_delivery_explicit_omission', [i for i, _ in omitted],
                deliveries=[{'event_index': i, 'omitted_task_ids': e.get('omitted_task_ids'),
                             'omitted_findings': [x.get('omitted_findings') for x in e['delivered_results']]}
                            for i, e in omitted])
        outcome = details.get('outcome', {})
        supported = [outcome.get('claims', [])[i]['text'] for i in outcome.get('supported', [])]
        if attempt['prediction']['status'] == 'ok' and not supported:
            add('no_verified_claims_empty_answer', [], termination_reason=reason)
        records.append({'dataset': trace['dataset'], 'case_id': trace['case_id'],
            'question': question, 'gold_answers': gold['answers'],
            'prediction': attempt['prediction'], 'benchmark_outcome': trace['benchmark_outcome'],
            'trace': relative, 'trace_sha256': source_hashes[relative],
            'attempt': attempt['attempt'], 'observations': observations,
            'schema_validation_replays': failures, 'verified_claims': supported,
            'internal_verifier': outcome.get('status'), 'verifier_issues': outcome.get('issues'),
            'human_verdict': None, 'human_root_causes': None,
            'causal_status': 'not_established_by_observational_audit'})
    counts = {}
    for group in ('success', 'failure'):
        subset = [x for x in records if x['benchmark_outcome'] == group]
        counts[group] = {'cases': len(subset), 'observations': dict(collections.Counter(
            label for x in subset for label in {o['label'] for o in x['observations']}))}
    for name, expected in source_hashes.items():
        assert digest(root / name) == expected
    for name, expected in input_hashes.items():
        assert digest(root / name) == expected
    destination.mkdir(parents=True, exist_ok=False)
    for group in ('success', 'failure', 'unknown'):
        with (destination / (group + '.jsonl')).open('x', encoding='utf-8') as handle:
            for record in records:
                if record['benchmark_outcome'] == group:
                    handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    summary = {'schema_version': 1, 'source': str(root.resolve()), 'traces_verified': len(manifest),
               'original_trace_hashes_unchanged': True, 'new_model_calls': 0,
               'counts': counts, 'human_labels_assigned': 0,
               'input_sha256': input_hashes,
               'interpretation': 'Non-exclusive observations; support presence is not proof of sufficiency or use.'}
    (destination / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=pathlib.Path)
    parser.add_argument('destination', type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.source, args.destination), ensure_ascii=False, indent=2))
