"""Offline audit of mainline pilot results against immutable historical runs."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from deepsearch_experiments.benchmarks.multihop.compare import compare_runs, markdown_table
from deepsearch_experiments.benchmarks.multihop.datasets import load_manifest
from deepsearch_experiments.benchmarks.multihop.runner import implementation_identity, read_attempts, score_run
from deepsearch_agent.adapters.persistence.local_files import atomic_write


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--mainline', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Audit output exists; choose a new path')
    before = json.loads(args.snapshot.read_text(encoding='utf-8'))
    unchanged = all(sha(path) == digest for path, digest in before['baseline_artifacts'].items())
    if not unchanged:
        raise ValueError('Historical baseline artifacts changed')
    changed = [path for path, digest in before['files'].items() if sha(path) != digest]
    allowed = {'src/deepsearch_agent/benchmarks/multihop/' + name
               for name in ('runner.py', 'compare.py', '__main__.py')}
    if set(changed) - allowed:
        raise ValueError('Shared baseline algorithm/runtime/scoring changed; inspect before comparison')
    result = {'baseline_artifacts_unchanged': unchanged, 'changed_existing_files': changed,
              'prechange_implementation': before['implementation'],
              'implementation': implementation_identity(), 'datasets': {},
              'limitations': [
                  'Historical references, not a same-build or concurrent rerun.',
                  'Small previously inspected development population; no significance claim.',
                  'Support recall is document-level presence, not causal evidence use.',
                  'V2 MuSiQue IRCoT recovery has unknown pre-interruption unpersisted usage.',
              ]}
    for dataset in ('2wikimultihopqa', 'musique'):
        manifest = load_manifest(args.prepared / dataset / 'pilot-10.json')
        current = args.mainline / (dataset + '-mainline')
        paths = [args.references / (dataset + '-' + method) for method in ('ircot', 'logicrag')]
        comparison = compare_runs(manifest, [*paths, current], historical_reference=True)
        report = score_run(manifest, current)
        if report['run']['implementation'] != result['implementation']:
            raise ValueError('Mainline run does not match current implementation')
        if json.loads((current / 'report.json').read_text(encoding='utf-8')) != report:
            raise ValueError('Saved report differs from offline rescore')
        attempts, latest = read_attempts(current / 'attempts.jsonl', manifest, report['run'])
        if len(attempts) != len(manifest.cases):
            raise ValueError('Pilot has retries or missing attempts; explicit review needed')
        gold = {case.id: case for case in manifest.gold}
        cases = []
        for metric in report['per_case']:
            row = latest[metric['id']]
            details = row['details']
            outcome = details.get('outcome', {})
            roles, tools, role_tokens = Counter(), Counter(), Counter()
            role = 'unknown'
            for event in row['events']:
                kind = event['kind']
                if kind == 'mainline.rag.request':
                    role = event['role']
                elif kind == 'mainline.answer_projection':
                    role = 'AnswerProjection'
                elif kind == 'model':
                    roles[role] += 1
                    role_tokens[role] += event.get('reported_total_tokens') or 0
                elif kind == 'mainline.tool.request':
                    tools[event['name']] += 1
                elif kind == 'mainline.rag.outcome':
                    outcome = event
            support = set(gold[metric['id']].support_document_ids)
            admitted = {chunk['source_path'] for chunk in outcome.get('evidence', [])}
            if metric['status'] != 'ok':
                category = details.get('error_code') or details.get('failure_code') or details.get('termination_reason') or metric['status']
            elif metric['answer_em']:
                category = 'correct'
            elif not row['prediction']['answer']:
                category = 'abstained_without_verified_claims'
            elif metric['document_support_recall_proxy'] < 1:
                category = 'incorrect_with_annotated_retrieval_gap'
            else:
                category = 'incorrect_despite_retrieved_support'
            cases.append({**metric, 'observed_category': category,
                'answer': row['prediction']['answer'], 'gold_answers': gold[metric['id']].answers,
                'outcome_status': outcome.get('status'), 'stop_reason': outcome.get('stop_reason'),
                'admitted_support_recall_proxy': len(admitted & support) / len(support) if outcome else None,
                'verified_answer': details.get('verified_answer'),
                'role_calls': dict(roles), 'role_known_tokens': dict(role_tokens), 'tool_calls': dict(tools),
                'usage': row['usage']})
        result['datasets'][dataset] = {
            'comparison': comparison, 'cases': cases,
            'observed_categories': dict(Counter(case['observed_category'] for case in cases)),
            'artifact_sha256': {str(path): sha(path) for path in current.glob('*.json*')},
        }
    atomic_write(args.output, json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))
    for dataset, data in result['datasets'].items():
        print(dataset)
        print(markdown_table(data['comparison']))
        print(json.dumps(data['observed_categories']))


if __name__ == '__main__':
    main()
