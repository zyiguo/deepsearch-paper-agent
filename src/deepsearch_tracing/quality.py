"""Immutable trace snapshots, human reviews and matched-case regression closure; stdlib only."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from deepsearch_tracing.outcomes import classify_run
from deepsearch_tracing.store import TraceStore, canonical, digest, validate_run

ROOT_CAUSES = frozenset({'retrieval_miss', 'evidence_loss', 'constraint_loss', 'hallucination',
    'verifier_false_accept', 'verification_reject', 'tool_failure', 'model_service',
    'persistence', 'cancellation', 'budget', 'unknown'})


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def load(path, kind):
    result = json.loads(Path(path).read_text(encoding='utf-8'))
    checksum = result.pop('sha256', None)
    if result.get('version') != 1 or result.get('kind') != kind or digest(result) != checksum:
        raise ValueError('Quality artifact type/version/hash mismatch')
    result['sha256'] = checksum
    return result


def seal(value):
    return {**value, 'sha256': digest(value)}


def freeze(db_path, output):
    runs = TraceStore(db_path, read_only=True).read_runs()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for run in runs:
        metadata = run['metadata']
        result = classify_run(run)
        rows.append({'trace_run_id': run['run_id'], 'trace_sha256': digest(run),
            'research_run_id': metadata.get('research_run_id'),
            'question_sha256': digest(metadata.get('question')) if metadata.get('question') else None,
            'runtime_identity': metadata.get('runtime_identity'), 'corpus_identity': metadata.get('corpus_identity'),
            'execution_group': result['group'], 'internal_outcome': result['outcome'],
            'answer_observed': any(e['kind'] == 'answer.final' for e in run['events'])})
    # Full observations stay in the immutable snapshot, never in parent-agent context.
    write(output / 'runs.json', runs)
    snapshot = seal({'version': 1, 'kind': 'snapshot', 'created_at': datetime.now(timezone.utc).isoformat(),
                     'runs_sha256': digest(runs), 'runs': rows})
    write(output / 'snapshot.json', snapshot)
    return snapshot


def read_snapshot(path):
    path = Path(path)
    snapshot = load(path, 'snapshot')
    runs = json.loads(path.with_name('runs.json').read_text(encoding='utf-8'))
    if digest(runs) != snapshot['runs_sha256']:
        raise ValueError('Snapshot trajectories have changed')
    actual = {r['run_id']: digest(validate_run(r)) for r in runs}
    if actual != {r['trace_run_id']: r['trace_sha256'] for r in snapshot['runs']}:
        raise ValueError('Snapshot index differs from trajectories')
    return snapshot


def review(snapshot_path, labels, reviewer, output):
    snapshot = read_snapshot(snapshot_path)
    if not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 100:
        raise ValueError('Identify the human reviewer')
    by_id = {r['trace_run_id']: r for r in snapshot['runs']}
    seen, cases, rows = set(), set(), []
    for label in labels:
        run_id = label['trace_run_id']
        case_id = label['case_id']
        if run_id not in by_id or run_id in seen or case_id in cases or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', case_id):
            raise ValueError('Unknown/duplicate run or invalid case ID')
        if label['trace_sha256'] != by_id[run_id]['trace_sha256']:
            raise ValueError('Review is not bound to this exact trajectory')
        verdict = label['verdict']
        causes = label.get('root_causes', [])
        if verdict not in {'correct', 'incorrect', 'unknown'} or not isinstance(causes, list) or not set(causes) <= ROOT_CAUSES:
            raise ValueError('Unsupported human verdict or root cause')
        expected, evidence = label.get('expected', ''), label.get('evidence', '')
        fix_id = label.get('fix_id')
        if fix_id is not None and (not isinstance(fix_id, str) or not 1 <= len(fix_id.strip()) <= 256
                                   or any(ord(c) < 32 for c in fix_id)):
            raise ValueError('Invalid fix reference')
        if any(not isinstance(v, str) or len(v) > 4000 for v in [expected, evidence, label.get('notes', '')]):
            raise ValueError('Review evidence/notes exceeds limits')
        if verdict != 'unknown' and (not expected.strip() or not evidence.strip()):
            raise ValueError('A known correctness label requires expected behavior and supporting evidence')
        if verdict == 'incorrect' and not causes:
            raise ValueError('A badcase requires a root-cause label, including explicit unknown')
        if verdict == 'correct' and not by_id[run_id]['answer_observed']:
            raise ValueError('Cannot mark a missing answer as correct')
        seen.add(run_id)
        cases.add(case_id)
        rows.append({**by_id[run_id], 'case_id': case_id, 'verdict': verdict, 'root_causes': sorted(set(causes)),
            'expected': expected, 'evidence': evidence, 'notes': label.get('notes', ''),
            'expectation_sha256': digest([expected, evidence]), 'fix_id': label.get('fix_id')})
    counts = Counter(r['verdict'] for r in rows)
    total, known = len(by_id), counts['correct'] + counts['incorrect']
    result = seal({'version': 1, 'kind': 'review', 'reviewer': reviewer.strip(),
        'created_at': datetime.now(timezone.utc).isoformat(), 'snapshot_sha256': snapshot['sha256'],
        'rows': rows, 'metrics': {'total_runs': total, 'reviewed_runs': len(rows),
            'unreviewed_runs': total - len(rows), 'unknown_labels': counts['unknown'],
            'correct': counts['correct'], 'incorrect': counts['incorrect'], 'correctness_denominator': known,
            'human_accuracy': counts['correct'] / known if known else None,
            'review_coverage': len(rows) / total if total else None,
            'known_correctness_coverage': known / total if total else None,
            'execution_groups': dict(Counter(r['execution_group'] for r in by_id.values())),
            'internal_outcomes': dict(Counter(r['internal_outcome'] for r in by_id.values())),
            'badcase_root_causes': dict(Counter(c for r in rows if r['verdict'] == 'incorrect' for c in r['root_causes']))}})
    write(output, result)
    return result


def compare(baseline_path, candidate_path, output):
    baseline, candidate = load(baseline_path, 'review'), load(candidate_path, 'review')
    after = {r['case_id']: r for r in candidate['rows']}
    rows = []
    for before in baseline['rows']:
        new = after.get(before['case_id'])
        state, reason = 'awaiting_regression', 'candidate_case_missing'
        if new is not None:
            fields = ['question_sha256', 'runtime_identity', 'corpus_identity', 'expectation_sha256']
            mismatch = [key for key in fields if before.get(key) is None or new.get(key) is None or before[key] != new[key]]
            if mismatch:
                state, reason = 'incomparable', ','.join(mismatch)
            elif new['verdict'] == 'unknown' or before['verdict'] == 'unknown':
                state, reason = 'awaiting_review', 'human_correctness_unknown'
            elif before['verdict'] == 'incorrect' and new['verdict'] == 'correct':
                if (new['trace_sha256'] == before['trace_sha256'] or not new.get('research_run_id')
                        or new['research_run_id'] == before.get('research_run_id')):
                    state, reason = 'awaiting_new_run', 'a_relabelled_old_run_is_not_a_regression_execution'
                else:
                    state, reason = ('resolved', 'human_reviewed_regression_pass') if new.get('fix_id') else ('awaiting_fix_reference', 'fix_id_missing')
            elif before['verdict'] == 'correct' and new['verdict'] == 'incorrect':
                state, reason = 'regression', 'previously_correct_now_incorrect'
            elif new['verdict'] == 'incorrect':
                state, reason = 'open', 'still_incorrect'
            else:
                state, reason = 'stable', 'still_correct'
        rows.append({'case_id': before['case_id'], 'state': state, 'reason': reason,
            'baseline_trace_sha256': before['trace_sha256'],
            'candidate_trace_sha256': new['trace_sha256'] if new else None,
            'fix_id': new.get('fix_id') if new else None})
    result = seal({'version': 1, 'kind': 'comparison', 'baseline_review_sha256': baseline['sha256'],
        'candidate_review_sha256': candidate['sha256'], 'cases': rows,
        'counts': dict(Counter(r['state'] for r in rows)),
        'new_candidate_cases': sorted(set(after) - {r['case_id'] for r in baseline['rows']})})
    write(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['freeze', 'review', 'compare'])
    parser.add_argument('--db', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--labels', type=Path)
    parser.add_argument('--reviewer')
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'freeze':
            result = freeze(args.db, args.output)
        elif args.command == 'review':
            result = review(args.snapshot, json.loads(args.labels.read_text(encoding='utf-8')), args.reviewer, args.output)
        else:
            result = compare(args.baseline, args.candidate, args.output)
        print(json.dumps({'status': 'passed', 'artifact_sha256': result['sha256'],
                          'metrics': result.get('metrics'), 'counts': result.get('counts')}))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
                          'message': 'Invalid, changed or already existing quality artifact; source traces and reviews were not overwritten.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
