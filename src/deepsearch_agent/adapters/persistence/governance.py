"""Atomic user + workspace quota admission and durable unknown expenses."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from deepsearch_agent.core.governance import GovernanceDenied


class GovernanceStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS governance_limits (
                    scope TEXT NOT NULL, subject TEXT NOT NULL, daily_units INTEGER,
                    PRIMARY KEY(scope,subject));
                CREATE TABLE IF NOT EXISTS cost_reservations (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    day TEXT NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL, rate INTEGER NOT NULL,
                    reserved INTEGER NOT NULL, actual INTEGER, status TEXT NOT NULL,
                    run_id TEXT, operation_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS costs_user ON cost_reservations(user_id,day,status);
                CREATE INDEX IF NOT EXISTS costs_workspace ON cost_reservations(workspace_id,day,status);
                CREATE TABLE IF NOT EXISTS governance_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
                    target TEXT NOT NULL, value TEXT NOT NULL, proof_reference TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS governance_alerts (
                    name TEXT PRIMARY KEY, state TEXT NOT NULL, since REAL NOT NULL, updated_at REAL NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def day(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()

    @staticmethod
    def _limit(db, scope, subject, default):
        row = db.execute('SELECT daily_units FROM governance_limits WHERE scope=? AND subject=?', (scope, subject)).fetchone()
        return row['daily_units'] if row is not None else default

    @staticmethod
    def _used(db, scope, subject, day):
        column = 'user_id' if scope == 'user' else 'workspace_id'
        row = db.execute(f'''SELECT COALESCE(sum(CASE WHEN status='settled' THEN actual ELSE reserved END),0) AS n
            FROM cost_reservations WHERE {column}=? AND
            ((day=? AND status='settled') OR status IN ('reserved','running','unknown'))''', (subject, day)).fetchone()
        return row['n']

    def reserve(self, scope, kind, model, tokens, rate, defaults):
        amount, ident, now, day = tokens * rate, uuid4().hex, self.clock(), self.day()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for name, subject, default in [('user', scope.user, defaults.user_daily_units),
                                           ('workspace', scope.workspace, defaults.workspace_daily_units)]:
                limit = self._limit(db, name, subject, default)
                if limit is not None and self._used(db, name, subject, day) + amount > limit:
                    raise GovernanceDenied(f'{name}_quota_exceeded')
            db.execute('INSERT INTO cost_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, scope.user, scope.workspace, day, kind, model[:120], rate, amount, None,
                        'reserved', scope.run_id, scope.operation_id, now, now))
        return ident

    def dispatch(self, ident):
        with self.connect() as db:
            if db.execute("UPDATE cost_reservations SET status='running',updated_at=? WHERE id=? AND status='reserved'", (self.clock(), ident)).rowcount != 1:
                raise RuntimeError('quota_dispatch_conflict')

    def finish(self, ident, *, tokens=None, not_executed=False):
        with self.connect() as db:
            row = db.execute('SELECT rate FROM cost_reservations WHERE id=?', (ident,)).fetchone()
            actual = tokens * row['rate'] if tokens is not None else None
            status = 'released' if not_executed else 'settled' if tokens is not None else 'unknown'
            if db.execute('UPDATE cost_reservations SET status=?,actual=?,updated_at=? WHERE id=? AND status IN (\'reserved\',\'running\')',
                          (status, actual, self.clock(), ident)).rowcount != 1:
                raise RuntimeError('quota_settlement_conflict')
        return status

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE cost_reservations SET status=CASE WHEN status='reserved' THEN 'released' ELSE 'unknown' END,updated_at=? WHERE status IN ('reserved','running')", (self.clock(),))

    def usage(self, scope, subject, default=None):
        if scope not in {'user', 'workspace'}:
            raise ValueError('Invalid quota scope')
        with self.connect() as db:
            limit = self._limit(db, scope, subject, default)
            used = self._used(db, scope, subject, self.day())
            column = 'user_id' if scope == 'user' else 'workspace_id'
            rows = db.execute(f'''SELECT status,count(*) AS count,COALESCE(sum(reserved),0) AS reserved,
                sum(actual) AS actual FROM cost_reservations WHERE {column}=? AND
                (day=? OR status IN ('reserved','running','unknown')) GROUP BY status''', (subject,self.day())).fetchall()
        return dict(scope=scope, subject=subject, day=self.day(), daily_units=limit, used_or_held=used,
                    remaining=None if limit is None else max(0,limit-used), breakdown=[dict(r) for r in rows])

    def set_limit(self, scope, subject, amount, actor):
        if scope not in {'user','workspace'} or not subject or len(subject)>128:
            raise ValueError('Invalid quota scope')
        if amount is not None and (type(amount) is not int or not 0<=amount<=10**12):
            raise ValueError('Invalid quota amount')
        with self.connect() as db:
            db.execute('INSERT INTO governance_limits VALUES (?,?,?) ON CONFLICT(scope,subject) DO UPDATE SET daily_units=excluded.daily_units', (scope,subject,amount))
            db.execute('INSERT INTO governance_audit(actor,action,target,value,created_at) VALUES (?,?,?,?,?)',
                       (actor,'set_limit',scope+':'+subject,str(amount),self.clock()))

    def unsettled(self, *, workspace=None, limit=100):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM cost_reservations WHERE status='unknown'" +
                (' AND workspace_id=?' if workspace else '') + ' ORDER BY created_at LIMIT ?',
                (workspace,limit) if workspace else (limit,))]

    def settle_unknown(self, ident, actual_units, actor, proof):
        if type(actual_units) is not int or not 0<=actual_units<=10**12 or not proof.strip():
            raise ValueError('Verified cost and proof reference required')
        with self.connect() as db:
            if db.execute("UPDATE cost_reservations SET status='settled',actual=?,updated_at=? WHERE id=? AND status='unknown'", (actual_units,self.clock(),ident)).rowcount != 1:
                raise GovernanceDenied('quota_settlement_conflict',409)
            db.execute('INSERT INTO governance_audit(actor,action,target,value,proof_reference,created_at) VALUES (?,?,?,?,?,?)',
                       (actor,'settle_unknown',ident,str(actual_units),proof,self.clock()))

    def summary(self):
        with self.connect() as db:
            row = db.execute("SELECT count(*) AS unknown_count,min(created_at) AS oldest FROM cost_reservations WHERE status='unknown'").fetchone()
            over = db.execute("SELECT count(*) FROM cost_reservations WHERE day=? AND status='settled' AND actual>reserved",(self.day(),)).fetchone()[0]
        return {'unknown_count': row['unknown_count'], 'oldest_unknown_seconds': max(0,self.clock()-row['oldest']) if row['oldest'] is not None else 0, 'reservation_overruns':over}

    def audit(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM governance_audit ORDER BY sequence DESC LIMIT 100')]

    def alerts(self, conditions, *, hold_seconds):
        now, changed = self.clock(), []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for name, active in conditions.items():
                row = db.execute('SELECT * FROM governance_alerts WHERE name=?',(name,)).fetchone()
                since = row['since'] if row and row['state'] in {'pending','firing'} and active else now
                state = ('firing' if now-since>=hold_seconds else 'pending') if active else 'resolved'
                if row is None or row['state']!=state:
                    changed.append({'name':name,'state':state})
                db.execute('INSERT INTO governance_alerts VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,since=excluded.since,updated_at=excluded.updated_at',(name,state,since,now))
            rows = [dict(r) for r in db.execute('SELECT * FROM governance_alerts ORDER BY name')]
        return rows, changed
