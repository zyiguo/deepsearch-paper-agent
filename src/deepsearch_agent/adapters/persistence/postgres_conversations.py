"""Scoped PostgreSQL conversations and optimistic memory updates."""
import json
from pathlib import Path
from uuid import uuid4
from psycopg.types.json import Jsonb

from deepsearch_agent.workspace.conversations import utc_now, serialize_answer, stored_turn


class PostgresConversationStore:
    def __init__(self, database, workspace, path):
        self.database, self.workspace, self.path = database, workspace, Path(path)
        # path only locates local working memory; it is not a SQLite database.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute('INSERT INTO business_scopes VALUES(%s) ON CONFLICT DO NOTHING', (workspace,))

    def transaction(self):
        return self.database.transaction(self.workspace)

    def create(self, title):
        title = title.strip()
        if not title or len(title)>100:
            raise ValueError('会话名称须为 1 到 100 个字符')
        now = utc_now()
        session = dict(id=str(uuid4()),title=title,created_at=now,updated_at=now)
        with self.transaction() as db:
            db.execute('INSERT INTO sessions VALUES(%s,%s,%s,%s,%s)', (self.workspace,session['id'],title,now,now))
        return session

    def list_sessions(self):
        with self.transaction() as db:
            return db.execute('SELECT id,title,created_at,updated_at FROM sessions WHERE workspace_id=%s '
                              'ORDER BY updated_at DESC,id', (self.workspace,)).fetchall()

    def get(self, session_id):
        with self.transaction() as db:
            session = db.execute('SELECT id,title,created_at,updated_at FROM sessions WHERE workspace_id=%s AND id=%s',
                                 (self.workspace,session_id)).fetchone()
            if session is None:
                raise KeyError('会话不存在')
            turns = db.execute('SELECT id,question,answer_json,created_at,attachments_json FROM turns '
                               'WHERE workspace_id=%s AND session_id=%s ORDER BY id', (self.workspace,session_id)).fetchall()
        return {**session,'turns':[stored_turn(row) for row in turns]}

    def find_turn(self, run_id, session_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM turns WHERE workspace_id=%s AND run_id=%s AND session_id=%s',
                             (self.workspace, run_id, session_id)).fetchone()
        return stored_turn(row) if row else None

    def delete(self, session_id):
        with self.transaction() as db:
            result = db.execute('DELETE FROM sessions WHERE workspace_id=%s AND id=%s', (self.workspace,session_id))
            if not result.rowcount:
                raise KeyError('会话不存在')

    def append_turn(self, session_id, question, answer, attachments=None, run_id=None):
        answer_data = serialize_answer(answer)
        with self.transaction() as db:
            # Serialize idempotent commits by run, and chronological writes by session.
            if run_id is not None:
                db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                           ('turn:'+self.database.settings.schema+':'+self.workspace+':'+run_id,))
                previous = db.execute('SELECT * FROM turns WHERE workspace_id=%s AND run_id=%s',
                                      (self.workspace,run_id)).fetchone()
                if previous is not None:
                    if previous['session_id']!=session_id:
                        raise ValueError('run_id belongs to another session')
                    return stored_turn(previous)
            exists = db.execute('SELECT id FROM sessions WHERE workspace_id=%s AND id=%s FOR UPDATE',
                                (self.workspace,session_id)).fetchone()
            if not exists:
                raise KeyError('会话不存在')
            now = utc_now()
            row = db.execute('INSERT INTO turns(workspace_id,session_id,question,answer_json,created_at,attachments_json,run_id) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,question,answer_json,created_at,attachments_json',
                (self.workspace,session_id,question,Jsonb(answer_data),now,Jsonb(attachments or []),run_id)).fetchone()
            db.execute('UPDATE sessions SET updated_at=%s WHERE workspace_id=%s AND id=%s', (now,self.workspace,session_id))
        return stored_turn(row)

    def initialize_memory(self, session_id, payload):
        with self.transaction() as db:
            db.execute('INSERT INTO session_memories VALUES(%s,%s,0,%s) ON CONFLICT DO NOTHING',
                       (self.workspace,session_id,Jsonb(json.loads(payload))))
            row = db.execute('SELECT payload FROM session_memories WHERE workspace_id=%s AND session_id=%s',
                             (self.workspace,session_id)).fetchone()
        return json.dumps(row['payload'],ensure_ascii=False)

    def compare_and_set_memory(self, session_id, revision, payload, expected):
        with self.transaction() as db:
            result = db.execute('UPDATE session_memories SET revision=%s,payload=%s '
                'WHERE workspace_id=%s AND session_id=%s AND revision=%s',
                (revision,Jsonb(json.loads(payload)),self.workspace,session_id,expected))
        return result.rowcount==1
