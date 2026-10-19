"""Real process exits against a synthetic provider; no network or real charges."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger
from deepsearch_agent.application.tool_execution import ToolExecutor
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked, ToolKind


def worker(root, kind, stage, phase, idempotent):
    ledger = SQLiteToolLedger(root / 'ledger.sqlite3')
    ledger.begin_run('synthetic-run')
    if phase == 'resume':
        ledger.recover_interrupted('synthetic-run')
    provider = root / 'synthetic-provider.sqlite3'
    with sqlite3.connect(provider) as db:
        db.executescript('CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, operation_id TEXT);'
                         'CREATE TABLE IF NOT EXISTS effects(id INTEGER PRIMARY KEY, operation_id TEXT, kind TEXT);')
    def invoke(ident):
        with sqlite3.connect(provider) as db:
            db.execute('INSERT INTO requests(operation_id) VALUES (?)', (ident,))
            if kind != ToolKind.READ_ONLY and (not idempotent or not db.execute(
                    'SELECT 1 FROM effects WHERE operation_id=?', (ident,)).fetchone()):
                db.execute('INSERT INTO effects(operation_id,kind) VALUES (?,?)', (ident, kind))
        return {'receipt': ident, 'fixture': 'synthetic'}
    def fault(point, ident):
        if phase == 'crash' and point == stage:
            os._exit(73)
    execution = ToolExecutor(ledger, 'synthetic-run', fault=fault)
    try:
        result = execution.execute('round:0/step:0', 'synthetic_tool', ToolKind(kind), {}, invoke,
                                   idempotency_contract='synthetic-provider-v1' if idempotent else '')
        value = {'status': 'succeeded', 'replayed': result.replayed}
    except ToolExecutionBlocked as error:
        value = {'status': 'blocked', 'code': error.code}
    print(json.dumps(value))


def verify(output):
    output.mkdir(parents=True, exist_ok=False)
    cases = [(kind, stage, False) for kind in ToolKind for stage in
             ['after_claim', 'after_dispatch', 'after_effect', 'after_commit']]
    cases += [(kind, 'after_effect', True) for kind in [ToolKind.PAID, ToolKind.EFFECTFUL]]
    results = []
    for kind, stage, idempotent in cases:
        name = f'{kind}-{stage}' + ('-idempotent' if idempotent else '')
        root = output / name
        root.mkdir()
        args = [sys.executable, __file__, '--worker', '--output', str(root), '--kind', kind,
                '--stage', stage, *(['--idempotent'] if idempotent else [])]
        crashed = subprocess.run(args + ['--phase', 'crash'], capture_output=True, text=True, timeout=30)
        assert crashed.returncode == 73, (name, crashed.stderr)
        resumed = subprocess.run(args + ['--phase', 'resume'], capture_output=True, text=True, timeout=30)
        assert resumed.returncode == 0, (name, resumed.stderr)
        response = json.loads(resumed.stdout)
        ledger = SQLiteToolLedger(root / 'ledger.sqlite3')
        with sqlite3.connect(root / 'synthetic-provider.sqlite3') as db:
            requests = db.execute('SELECT count(*) FROM requests').fetchone()[0]
            effects = db.execute("SELECT count(*) FROM effects WHERE kind='effectful'").fetchone()[0]
            charges = db.execute("SELECT count(*) FROM effects WHERE kind='paid'").fetchone()[0]
        unknown = kind != ToolKind.READ_ONLY and stage in {'after_dispatch', 'after_effect'} and not idempotent
        assert ledger.list_run('synthetic-run')[0]['status'] == ('unknown' if unknown else 'succeeded')
        expected_requests = (0 if stage == 'after_dispatch' and unknown else
                             2 if stage == 'after_effect' and (kind == ToolKind.READ_ONLY or idempotent) else 1)
        assert requests == expected_requests and effects <= 1 and charges <= 1
        results.append({'case': name, 'kind': kind, 'crash_exit': crashed.returncode,
                        'resume': response, 'requests': requests, 'business_effects': effects,
                        'synthetic_charges': charges, 'ledger': ledger.list_run('synthetic-run')[0]})
    with (output / 'report.json').open('x', encoding='utf-8') as handle:
        json.dump({'scope': 'Actual process exit and SQLite restart against synthetic provider; no real API, business effects or charges.',
                   'cases': len(results), 'all_assertions_passed': True, 'results': results}, handle, indent=2)
    print(json.dumps({'cases': len(results), 'all_assertions_passed': True, 'report': str(output / 'report.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--kind')
    parser.add_argument('--stage')
    parser.add_argument('--phase')
    parser.add_argument('--idempotent', action='store_true')
    args = parser.parse_args()
    if args.worker:
        worker(args.output, args.kind, args.stage, args.phase, args.idempotent)
    else:
        verify(args.output)
