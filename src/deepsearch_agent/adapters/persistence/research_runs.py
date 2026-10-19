"""Durable metadata and cooperative cancellation for conversation research runs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ResearchRunStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS research_runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_json TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL, turn_id INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute("UPDATE research_runs SET status='interrupted' WHERE status IN ('queued','running')")
            db.execute("UPDATE research_runs SET status='cancelled' WHERE status='cancel_requested'")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def create(self, session_id, request, *, run_id=None):
        run_id = run_id or uuid4().hex
        now = utc_now()
        with self.connect() as db:
            db.execute('INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                       (run_id, session_id, json.dumps(request, ensure_ascii=False), 'queued',
                        'queued', None, now, now))
        return run_id

    def get(self, run_id, session_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM research_runs WHERE id=? AND session_id=?', (run_id, session_id)).fetchone()
        return ({**dict(row), 'request': json.loads(row['request_json'])} if row else None)

    def list(self, session_id):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM research_runs WHERE session_id=? ORDER BY created_at DESC',
                              (session_id,)).fetchall()
        return [{**dict(row), 'request': json.loads(row['request_json'])} for row in rows]

    def update(self, run_id, status, *, stage=None, turn_id=None, expected=None, execution=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT request_json,status FROM research_runs WHERE id=?', (run_id,)).fetchone()
            if row is None or (expected is not None and row['status'] not in expected):
                return False
            request = json.loads(row['request_json'])
            if execution is not None:
                previous = request.get('_execution', {})
                request['_execution'] = {**previous, **execution}
                if execution.get('new_attempt'):
                    request['_execution']['attempts'] = previous.get('attempts', 0) + 1
                    request['_execution'].pop('new_attempt', None)
            db.execute('UPDATE research_runs SET status=?, stage=COALESCE(?, stage), '
                       'turn_id=COALESCE(?, turn_id), updated_at=?, request_json=? WHERE id=?',
                       (status, stage, turn_id, utc_now(), json.dumps(request, ensure_ascii=False), run_id))
        return True

    def claim_resume(self, run_id, session_id):
        with self.connect() as db:
            result = db.execute("UPDATE research_runs SET status='queued', updated_at=? WHERE id=? AND session_id=? AND status='interrupted'",
                                (utc_now(), run_id, session_id))
        return result.rowcount == 1

    def delete_session(self, session_id):
        with self.connect() as db:
            db.execute('DELETE FROM research_runs WHERE session_id=?', (session_id,))
