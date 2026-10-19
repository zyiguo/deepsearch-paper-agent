"""Scoped run metadata; workers/checkpoints remain owned by a single API process."""
import json
from pathlib import Path
from uuid import uuid4
from psycopg.types.json import Jsonb

from deepsearch_agent.adapters.persistence.research_runs import utc_now


def public_run(row):
    if row is None:
        return None
    return {key:value for key,value in {**row,'request':row['request_json'],
            'request_json':json.dumps(row['request_json'],ensure_ascii=False)}.items() if key!='workspace_id'}


class PostgresResearchRunStore:
    def __init__(self, database, workspace, path):
        self.database, self.workspace, self.path = database, workspace, Path(path)
        with self.transaction() as db:
            db.execute('INSERT INTO business_scopes VALUES(%s) ON CONFLICT DO NOTHING', (workspace,))
            db.execute("UPDATE research_runs SET status='interrupted' WHERE workspace_id=%s AND status IN ('queued','running')", (workspace,))
            db.execute("UPDATE research_runs SET status='cancelled' WHERE workspace_id=%s AND status='cancel_requested'", (workspace,))

    def transaction(self):
        return self.database.transaction(self.workspace)

    def create(self, session_id, request, *, run_id=None):
        ident,now = run_id or uuid4().hex,utc_now()
        with self.transaction() as db:
            db.execute('INSERT INTO research_runs VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,id) DO NOTHING',
                       (self.workspace,ident,session_id,Jsonb(request),'queued','queued',None,now,now))
        return ident

    def get(self, run_id, session_id):
        with self.transaction() as db:
            return public_run(db.execute('SELECT * FROM research_runs WHERE workspace_id=%s AND id=%s AND session_id=%s',
                                         (self.workspace,run_id,session_id)).fetchone())

    def list(self, session_id):
        with self.transaction() as db:
            return [public_run(row) for row in db.execute('SELECT * FROM research_runs WHERE workspace_id=%s AND session_id=%s '
                                                        'ORDER BY created_at DESC,id', (self.workspace,session_id))]

    def update(self, run_id, status, *, stage=None, turn_id=None, expected=None, execution=None):
        with self.transaction() as db:
            row = db.execute('SELECT request_json,status FROM research_runs WHERE workspace_id=%s AND id=%s FOR UPDATE',
                             (self.workspace,run_id)).fetchone()
            if row is None or (expected is not None and row['status'] not in expected):
                return False
            request = row['request_json']
            if execution is not None:
                previous = request.get('_execution', {})
                request['_execution'] = {**previous, **execution}
                if execution.get('new_attempt'):
                    request['_execution']['attempts'] = previous.get('attempts', 0) + 1
                    request['_execution'].pop('new_attempt', None)
            db.execute('UPDATE research_runs SET status=%s,stage=COALESCE(%s,stage),turn_id=COALESCE(%s,turn_id),updated_at=%s,request_json=%s '
                       'WHERE workspace_id=%s AND id=%s', (status,stage,turn_id,utc_now(),Jsonb(request),self.workspace,run_id))
        return True

    def claim_resume(self, run_id, session_id):
        with self.transaction() as db:
            result = db.execute("UPDATE research_runs SET status='queued',updated_at=%s WHERE workspace_id=%s AND id=%s "
                                "AND session_id=%s AND status='interrupted'", (utc_now(),self.workspace,run_id,session_id))
        return result.rowcount==1

    def delete_session(self, session_id):
        with self.transaction() as db:
            db.execute('DELETE FROM research_runs WHERE workspace_id=%s AND session_id=%s', (self.workspace,session_id))
