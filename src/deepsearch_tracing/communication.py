"""Read-only projections of handoff events; no model calls or evaluation labels."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from deepsearch_tracing.store import TraceStore, validate_run
from deepsearch_tracing.outcomes import classify_run

VERSION = 1


def handoffs(runs):
    """Preserve failed/incomplete attempts; absence is unknown, never success."""
    rows = []
    seen = set()
    for run in sorted(runs, key=lambda r: r['run_id']):
        validate_run(run)
        if run['run_id'] in seen:
            raise ValueError('Duplicate trace run')
        seen.add(run['run_id'])
        events = run['events']
        task_ids = {e['payload']['task_id'] for e in events if e['payload'].get('task_id')}
        for event in events:
            if event['kind'] == 'communication.parent_delivery':
                task_ids.update(r['task_id'] for r in event['payload']['source_results'])
            elif event['kind'] == 'communication.parent_decision':
                task_ids.update(event['payload'].get('task_ids', []))
        tasks = sorted(task_ids)
        config = next((e['payload'] for e in events if e['kind'] == 'retrieval.config'), None)
        outcome = next((e['payload'] for e in reversed(events) if e['kind'] == 'rag.outcome'), None)
        answer = next((e['payload'] for e in reversed(events) if e['kind'] == 'answer.final'), None)
        for task_id in tasks:
            scoped = [e for e in events if e['payload'].get('task_id') == task_id]
            select = lambda kind: [e for e in scoped if e['kind'] == kind]
            dispatches = select('communication.task_dispatched')
            results = select('subagent.result')
            receipts = []
            related_ids = set()
            for event in events:
                payload = event['payload']
                if event['kind'] != 'communication.parent_delivery':
                    continue
                source = next((r for r in payload['source_results'] if r['task_id'] == task_id), None)
                if source is None:
                    continue
                delivered = next((r for r in payload['delivered_results'] if r['task_id'] == task_id), None)
                delivery_id = payload['delivery_id']
                inputs = [e for e in events if e['kind'] == 'communication.model_input'
                          and e['payload'].get('parent_delivery_id') == delivery_id]
                request_ids = {e['payload']['call_id'] for e in inputs}
                responses = [e for e in events if e['kind'] == 'rag.response'
                             and e['payload'].get('call_id') in request_ids]
                requests = [e for e in events if e['kind'] == 'rag.request'
                            and e['payload'].get('call_id') in request_ids]
                decisions = [e for e in events if e['kind'] == 'communication.parent_decision'
                             and e['payload'].get('delivery_id') == delivery_id]
                related_ids.update(e['seq'] for e in [event, *inputs, *requests, *responses, *decisions])
                receipts.append({'delivery_id': delivery_id, 'source_result': source,
                    'received_result': delivered, 'omitted': delivered is None,
                    'budget_estimated_tokens': payload['budget_estimated_tokens'],
                    'source_estimated_tokens': payload['source_estimated_tokens'],
                    'delivered_estimated_tokens': payload['delivered_estimated_tokens'],
                    'compression_version': payload['compression_version'],
                    'model_inputs': inputs, 'model_requests': requests,
                    'model_responses': responses, 'decisions': decisions})
            guard_decisions = [e for e in events if e['kind'] == 'communication.parent_decision'
                               and not e['payload'].get('delivery_id')
                               and task_id in e['payload'].get('task_ids', [])]
            parent_call_id = dispatches[0]['payload'].get('parent_call_id') if dispatches else None
            parent_origin = [e for e in events if parent_call_id is not None
                             and e['payload'].get('call_id') == parent_call_id]
            related_ids.update(e['seq'] for e in [*scoped, *guard_decisions, *parent_origin])
            rows.append({'schema_version': VERSION, 'trace_run_id': run['run_id'],
                'research_run_id': run['metadata'].get('research_run_id'),
                'conversation_id': run['metadata'].get('conversation_id'),
                'resume': run['metadata'].get('resume', False),
                'task_id': task_id, 'parent_call_id': parent_call_id,
                'recording_status': run['status'], 'retrieval_config': config,
                'classification': classify_run(run),
                'dispatch': dispatches[0]['payload'] if dispatches else None,
                'child_raw_results': [e['payload'] for e in select('communication.child_raw_result')],
                'child_result': results[-1]['payload'] if results else None,
                'child_model_inputs': select('communication.model_input'),
                'tool_observations': select('communication.tool_observation'),
                'evidence_snapshots': select('communication.evidence_snapshot'),
                'failures': select('communication.task_failed'),
                'parent_receipts': receipts, 'guard_decisions': guard_decisions,
                'stage_presence': {'dispatch': bool(dispatches), 'result': bool(results),
                    'parent_input_attempted': any(r['model_inputs'] for r in receipts),
                    'parent_response_observed': any(r['model_responses'] for r in receipts),
                    'parent_decision_observed': bool(guard_decisions) or any(r['decisions'] for r in receipts)},
                'run_outcome': outcome, 'final_answer': answer,
                'evaluation_labels': None,
                'source_events': [e for e in events if e['seq'] in related_ids]})
    return rows


def export_jsonl(rows, path):
    """Exclusive creation protects prior human annotations and frozen exports."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    with path.open('xb') as stream:
        for row in rows:
            line = (json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
            stream.write(line)
            hasher.update(line)
    return {'rows': len(rows), 'sha256': hasher.hexdigest()}


def export_split(rows, path, runs):
    """Separate success, failure and unknown records, including runs without a handoff."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    manifest = {'schema_version': VERSION, 'files': {}}
    for group in ('success', 'failure', 'unknown'):
        folder = path / group
        folder.mkdir()
        selected_runs = [r for r in runs if classify_run(r)['group'] == group]
        manifest['files'][f'{group}/runs.jsonl'] = export_jsonl(selected_runs, folder / 'runs.jsonl')
        manifest['files'][f'{group}/handoffs.jsonl'] = export_jsonl(
            [r for r in rows if r['classification']['group'] == group], folder / 'handoffs.jsonl')
    (path / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description='Inspect/export parent-child handoff traces without model calls')
    parser.add_argument('command', choices=['inspect', 'export', 'export-split'])
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--run-id')
    parser.add_argument('--task-id')
    parser.add_argument('--group', choices=['success', 'failure', 'unknown'])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command in {'export', 'export-split'} and args.output is None:
        parser.error('export/export-split requires --output')
    try:
        if not args.db.is_file():
            raise ValueError('Trace database does not exist')
        runs = [r for r in TraceStore(args.db, read_only=True).read_runs()
                if (args.run_id is None or r['run_id'] == args.run_id)
                and (args.group is None or classify_run(r)['group'] == args.group)]
        rows = handoffs(runs)
        rows = [r for r in rows if args.task_id is None or r['task_id'] == args.task_id]
        if args.task_id is not None:
            selected_run_ids = {r['trace_run_id'] for r in rows}
            runs = [r for r in runs if r['run_id'] in selected_run_ids]
        result = {'runs': len(runs), 'handoffs': len(rows),
                  'run_groups': dict(Counter(classify_run(r)['group'] for r in runs)),
                  'recording_statuses': dict(Counter(r['recording_status'] for r in rows)),
                  'observed_results': sum(r['stage_presence']['result'] for r in rows),
                  'parent_receipts': sum(len(r['parent_receipts']) for r in rows),
                  'failure_handoffs': sum(r['classification']['group'] == 'failure' for r in rows),
                  'task_exceptions': sum(bool(r['failures']) for r in rows)}
        if args.command == 'export':
            result['export'] = export_jsonl(rows, args.output)
        elif args.command == 'export-split':
            result['export'] = export_split(rows, args.output, runs)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(2, f'{type(error).__name__}: trace operation failed; check input integrity and output paths.\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
