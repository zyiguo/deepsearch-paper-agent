"""Workspace-local durable operation journal, independent of optional tracing."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from deepsearch_agent.core.identity import fingerprint
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked


def read_backlog(root, *, workspaces=False):
    """Read inactive workspace journals without starting/migrating their runtimes."""
    root = Path(root).resolve()
    paths = root.glob('*/tool_executions.sqlite3') if workspaces else [root/'tool_executions.sqlite3']
    total, oldest = 0, 0
    for path in paths:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
        try:
            row=db.execute("SELECT count(*),min(strftime('%s',updated_at)) FROM tool_operations WHERE status='unknown'").fetchone()
            total += row[0]
            if row[1] is not None:
                oldest=max(oldest,max(0,time.time()-int(row[1])))
        finally:
            db.close()
    return {'unknown_count':total,'oldest_unknown_seconds':oldest}


class SQLiteToolLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tool_ledger_runs (run_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS tool_operations (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, slot TEXT NOT NULL,
                    tool TEXT NOT NULL, kind TEXT NOT NULL, identity TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT, owner TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(run_id, slot));
                CREATE TABLE IF NOT EXISTS tool_operation_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(tool_operations)')}
            for name in ('rejection_code', 'arguments_json'):
                if name not in columns:
                    db.execute(f'ALTER TABLE tool_operations ADD COLUMN {name} TEXT')
            db.execute('''CREATE TABLE IF NOT EXISTS tool_reconciliations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
                actor TEXT NOT NULL, proof_reference TEXT NOT NULL, result_digest TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event(db, ident, status, reason):
        db.execute('INSERT INTO tool_operation_events(operation_id,status,reason) VALUES (?,?,?)',
                   (ident, status, reason))

    def begin_run(self, run_id):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO tool_ledger_runs VALUES (?)', (run_id,))

    def has_run(self, run_id):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM tool_ledger_runs WHERE run_id=?', (run_id,)).fetchone() is not None

    def prepare(self, run_id, slot, tool, kind, arguments, capability):
        ident = fingerprint(['tool-operation-v1', run_id, slot])
        identity = fingerprint([tool, str(kind), arguments, capability])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM tool_ledger_runs WHERE run_id=?', (run_id,)).fetchone():
                raise ToolExecutionBlocked('tool_ledger_run_missing', ident)
            row = db.execute('SELECT * FROM tool_operations WHERE id=?', (ident,)).fetchone()
            if row is None:
                db.execute('INSERT INTO tool_operations(id,run_id,slot,tool,kind,identity,status,arguments_json) VALUES (?,?,?,?,?,?,?,?)',
                           (ident, run_id, slot, tool, str(kind), identity, 'pending',
                            json.dumps(arguments, ensure_ascii=False, allow_nan=False)))
                self.event(db, ident, 'pending', 'intent_persisted')
            elif row['identity'] != identity:
                raise ToolExecutionBlocked('tool_operation_conflict', ident)
        return ident

    def get(self, operation_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tool_operations WHERE id=?', (operation_id,)).fetchone()
        if row is None:
            raise ToolExecutionBlocked('tool_operation_missing', operation_id)
        return dict(row)

    def claim(self, operation_id, *, allow_unknown=False):
        owner = uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            allowed = ('pending', 'unknown') if allow_unknown else ('pending',)
            row = db.execute('SELECT status FROM tool_operations WHERE id=?', (operation_id,)).fetchone()
            if row is None or row['status'] not in allowed:
                return None
            db.execute("UPDATE tool_operations SET status='running',phase='claimed',owner=?,attempt=attempt+1,"
                       'updated_at=CURRENT_TIMESTAMP WHERE id=?', (owner, operation_id))
            self.event(db, operation_id, 'running', 'execution_claimed')
        return owner

    def dispatch(self, operation_id, owner):
        with self.connect() as db:
            changed = db.execute("UPDATE tool_operations SET phase='dispatched',updated_at=CURRENT_TIMESTAMP "
                                 "WHERE id=? AND owner=? AND status='running' AND phase='claimed'",
                                 (operation_id, owner)).rowcount
            if changed != 1:
                raise ToolExecutionBlocked('tool_execution_fenced', operation_id)
            self.event(db, operation_id, 'running', 'dispatch_started')

    def succeed(self, operation_id, owner, result):
        # Serialize before opening the transaction; serialization failure remains uncertain.
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            changed = db.execute("UPDATE tool_operations SET status='succeeded',result_json=?,phase=NULL,"
                                 'owner=NULL,updated_at=CURRENT_TIMESTAMP '
                                 "WHERE id=? AND owner=? AND status='running'",
                                 (encoded, operation_id, owner)).rowcount
            if changed != 1:
                raise ToolExecutionBlocked('tool_execution_fenced', operation_id)
            self.event(db, operation_id, 'succeeded', 'result_committed')

    def uncertain(self, operation_id, owner):
        with self.connect() as db:
            changed = db.execute("UPDATE tool_operations SET status='unknown',phase=NULL,owner=NULL,"
                                 "updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner=? AND status='running'",
                                 (operation_id, owner)).rowcount
            if changed:
                self.event(db, operation_id, 'unknown', 'outcome_unconfirmed')

    def reject(self, operation_id, owner, code):
        with self.connect() as db:
            changed = db.execute("UPDATE tool_operations SET status='not_executed',rejection_code=?,"
                "phase=NULL,owner=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner=? AND status='running'",
                (code, operation_id, owner)).rowcount
            if changed != 1:
                raise ToolExecutionBlocked('tool_execution_fenced', operation_id)
            self.event(db, operation_id, 'not_executed', code)

    def recover_interrupted(self, run_id=None):
        """Only after exclusive startup or an interrupted run is atomically claimed."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            sql = "SELECT id,phase FROM tool_operations WHERE status='running'"
            rows = db.execute(sql + (' AND run_id=?' if run_id else ''), (run_id,) if run_id else ()).fetchall()
            for row in rows:
                status = 'pending' if row['phase'] == 'claimed' else 'unknown'
                db.execute('UPDATE tool_operations SET status=?,owner=NULL,phase=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                           (status, row['id']))
                self.event(db, row['id'], status, 'interrupted_before_dispatch' if status == 'pending' else 'interrupted_after_dispatch')
        return len(rows)

    def list_run(self, run_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT id,slot,tool,kind,status,attempt,updated_at,rejection_code '
                                                   'FROM tool_operations WHERE run_id=? ORDER BY slot', (run_id,))]

    def resolve(self, operation_id, result, *, actor='provider-adapter', proof_reference='provider-status-query'):
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            changed = db.execute("UPDATE tool_operations SET status='succeeded',result_json=?,"
                                 "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='unknown'",
                                 (encoded, operation_id)).rowcount
            if changed != 1:
                raise ToolExecutionBlocked('tool_reconciliation_conflict', operation_id)
            self.event(db, operation_id, 'succeeded', 'provider_result_confirmed')
            db.execute('INSERT INTO tool_reconciliations(operation_id,actor,proof_reference,result_digest) VALUES (?,?,?,?)',
                       (operation_id, actor, proof_reference, fingerprint(result)))

    def audit(self, operation_id):
        with self.connect() as db:
            events = [dict(r) for r in db.execute('SELECT status,reason,created_at FROM tool_operation_events WHERE operation_id=? ORDER BY sequence', (operation_id,))]
            resolutions = [dict(r) for r in db.execute('SELECT actor,proof_reference,result_digest,created_at FROM tool_reconciliations WHERE operation_id=? ORDER BY sequence', (operation_id,))]
        return {'events': events, 'reconciliations': resolutions}

    def summary(self):
        with self.connect() as db:
            row = db.execute("SELECT count(*) AS n,min(strftime('%s',updated_at)) AS oldest FROM tool_operations WHERE status='unknown'").fetchone()
        return {'unknown_count': row['n'], 'oldest_unknown_seconds': max(0,time.time()-int(row['oldest'])) if row['oldest'] is not None else 0}

    def delete_run(self, run_id):
        with self.connect() as db:
            db.execute('DELETE FROM tool_reconciliations WHERE operation_id IN (SELECT id FROM tool_operations WHERE run_id=?)', (run_id,))
            db.execute('DELETE FROM tool_operation_events WHERE operation_id IN (SELECT id FROM tool_operations WHERE run_id=?)', (run_id,))
            db.execute('DELETE FROM tool_operations WHERE run_id=?', (run_id,))
            db.execute('DELETE FROM tool_ledger_runs WHERE run_id=?', (run_id,))
