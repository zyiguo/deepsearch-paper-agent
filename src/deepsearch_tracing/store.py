"""Append-only SQLite event log with per-event commits and snapshot reads."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock, RLock
from weakref import WeakValueDictionary
from uuid import uuid4

SCHEMA_VERSION = 1
_write_locks = WeakValueDictionary()
_lock_guard = Lock()


def write_lock(path):
    # Serialize short commits in one process; SQLite still arbitrates other processes.
    key = str(Path(path).resolve()).removeprefix('\\\\?\\')
    with _lock_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = RLock()
            _write_locks[key] = lock
        return lock


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_run(run):
    if run.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Unsupported trace schema')
    if run.get('status') not in {'running', 'completed', 'error', 'recording_error'}:
        raise ValueError('Invalid run status')
    events = run.get('events', [])
    previous = ''
    for seq, event in enumerate(events):
        if event['seq'] != seq or event['previous_hash'] != previous:
            raise ValueError('Trace sequence or hash chain is broken')
        body = {k: v for k, v in event.items() if k != 'hash'}
        if event['hash'] != digest(body) or event['run_id'] != run['run_id']:
            raise ValueError('Trace content hash mismatch')
        previous = event['hash']
    if not events or events[0]['kind'] != 'run.started' or events[0]['payload'] != run['metadata']:
        raise ValueError('Missing run start')
    if run['status'] != 'running':
        if events[-1]['kind'] != 'run.finished' or events[-1]['payload']['status'] != run['status']:
            raise ValueError('Missing run finish')
    return run


class TraceStore:
    def __init__(self, path, *, read_only=False):
        self.path = Path(path)
        self.read_only = read_only
        self._write_lock = write_lock(path)
        if read_only:
            with closing(self._connect()) as db:
                if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                    raise ValueError('Unsupported trace database version')
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, closing(self._connect()) as db, db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version not in (0, SCHEMA_VERSION):
                raise ValueError('Unsupported trace database version')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id), seq INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(run_id, seq));
            ''')
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def _connect(self):
        if self.read_only:
            db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
            db.execute('PRAGMA query_only=ON')
        else:
            db = sqlite3.connect(str(self.path), timeout=1)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def start(self, metadata):
        run_id = uuid4().hex
        with self._write_lock, closing(self._connect()) as db, db:
            db.execute('INSERT INTO runs VALUES (?, ?, ?)', (run_id, canonical(metadata), 'running'))
            self._append(db, run_id, 'run.started', metadata)
        return run_id

    def _append(self, db, run_id, kind, payload):
        last = db.execute('SELECT body FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1', (run_id,)).fetchone()
        prev = json.loads(last[0]) if last else None
        event = {'run_id': run_id, 'seq': prev['seq'] + 1 if prev else 0,
                 'timestamp': now(), 'kind': kind, 'payload': payload,
                 'previous_hash': prev['hash'] if prev else ''}
        event['hash'] = digest(event)
        db.execute('INSERT INTO events VALUES (?, ?, ?)', (run_id, event['seq'], canonical(event)))

    def append(self, run_id, kind, payload):
        if kind in {'run.started', 'run.finished'}:
            raise ValueError('Reserved lifecycle event')
        with self._write_lock, closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            status = db.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if status != ('running',):
                raise ValueError('Run is not open')
            self._append(db, run_id, kind, payload)

    def finish(self, run_id, status='completed'):
        if status not in {'completed', 'error', 'recording_error'}:
            raise ValueError('Invalid terminal status')
        with self._write_lock, closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone() != ('running',):
                raise ValueError('Run is not open')
            self._append(db, run_id, 'run.finished', {'status': status})
            db.execute('UPDATE runs SET status=? WHERE run_id=?', (status, run_id))

    def read_runs(self):
        # One transaction prevents readers from observing half a lifecycle update.
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            result = []
            for run_id, metadata, status in db.execute('SELECT * FROM runs ORDER BY run_id'):
                events = [json.loads(row[0]) for row in db.execute(
                    'SELECT body FROM events WHERE run_id=? ORDER BY seq', (run_id,))]
                result.append(validate_run({'schema_version': SCHEMA_VERSION, 'run_id': run_id,
                    'metadata': json.loads(metadata), 'status': status, 'events': events}))
            return result
