"""Export an explicit public allowlist without prompts, source corpora or credentials."""
import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(source, destination):
    destination.mkdir(parents=True, exist_ok=False)
    provenance = {}

    def read(name):
        path = source / name
        provenance[name] = digest(path)
        return json.loads(path.read_text(encoding='utf-8'))

    def write(name, value):
        with (destination / name).open('x', encoding='utf-8', newline='\n') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')

    for name in ['summary.json', 'paired.json', 'cost-breakdown.json',
                 'synthetic-safety-counterexample.json', 'audit-validation-diagnostics.json']:
        write(name, read(name))
    plan = read('plan.json')
    write('protocol.json', {k: plan[k] for k in ['implementation', 'limits', 'model',
          'repeats', 'max_workers', 'gold_exclusions', 'selection_rule', 'sampling', 'retry_errors']} |
          {'selection': {k: {field: value[field] for field in ['ids', 'anchors', 'source_sha256', 'corpus_documents']}
                         for k, value in plan['selection'].items()},
           'jobs': plan['jobs']})
    records = read('per-attempt.json')
    fields = ['dataset', 'id', 'repeat', 'version', 'anchor', 'gold_excluded',
              'score', 'group', 'usage', 'role_cost', 'gap_deferred_paths',
              'verification_skips', 'schema_repairs', 'source_sha256']
    public_records = [{k: row[k] for k in fields} | {'prediction_status': row['prediction']['status']}
                      for row in records]
    write('per-attempt.json', public_records)
    for group in ['success', 'failure', 'unknown']:
        write(group + '.json', [row for row in public_records if row['group'] == group])
    write('audit-summary.json', read('blinded-audit/summary.json'))
    audits = []
    for path in sorted((source / 'blinded-audit').glob('[0-9][0-9][0-9][0-9].json')):
        row = read(str(path.relative_to(source)))
        cf = row['counterfactual']
        result = {k: row[k] for k in ['queue_index', 'id', 'job', 'source_sha256',
                  'event_index', 'decision', 'judge_kind', 'reported_total_tokens', 'status', 'retries']}
        result['judgment'] = ({k: row['judgment'][k] for k in
             ['candidate_supported', 'path_sufficient', 'missing_requirements']} if 'judgment' in row else None)
        result['counterfactual'] = {k: cf.get(k) for k in ['status', 'gap_state_removed', 'regeneration',
              'selected_candidate', 'accepts_deferred_candidate', 'deferred_path_status',
              'reported_total_tokens', 'known_reported_tokens']}
        result['counterfactual']['calls'] = len(cf['calls'])
        audits.append(result)
    write('audit-paths.json', audits)
    replay = read('rule-replay.json')
    write('rule-replay.json', replay)
    validation = read('validation-final.json')
    write('validation.json', {k: v for k, v in validation.items() if k != 'code_and_document_hashes'})
    write('provenance.json', {'scope': 'Sanitized statistics, identifiers, judgments and hashes only.',
          'excluded': ['private configuration', 'credentials', 'source corpora', 'full model prompts',
                       'raw model responses', 'natural evidence quotes', 'user workspace data', 'absolute local paths'],
          'source_sha256': provenance,
          'limitation': 'The public export supports statistical recalculation, not full evidence re-audit. Raw evidence remains local.'})
    entries = [{'path': p.name, 'sha256': digest(p), 'bytes': p.stat().st_size}
               for p in sorted(destination.iterdir()) if p.is_file()]
    write('checksums.json', entries)
    assert all(digest(destination / entry['path']) == entry['sha256'] for entry in entries)
    print(json.dumps({'files': len(entries) + 1, 'destination': str(destination)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('output/gap-repeats-20261019'))
    parser.add_argument('--destination', type=Path, default=Path('eval/results/gap-repeats-20261019'))
    args = parser.parse_args()
    export(args.source, args.destination)
