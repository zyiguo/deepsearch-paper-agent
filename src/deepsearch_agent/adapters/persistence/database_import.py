"""Explicit read-only SQLite imports into empty PostgreSQL destinations."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from psycopg import sql
from psycopg.types.json import Jsonb


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def read_source(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Source SQLite file does not exist')
    for suffix in ('-wal','-journal'):
        sidecar = Path(str(path)+suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('Stop source writers and checkpoint SQLite before importing')
    before = checksum(path)
    db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Source SQLite integrity check failed')
        yield db,before
    finally:
        db.close()
        if checksum(path)!=before:
            raise ValueError('Source changed during import; stop all source writers')


def rows(source, table):
    # All table identifiers are internal constants, not caller input.
    allowed = {'access_users','access_workspaces','access_members','access_audit',
               'sessions','turns','session_memories','research_runs'}
    if table not in allowed:
        raise ValueError('Unsupported source table')
    exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
    return [dict(r) for r in source.execute('SELECT * FROM '+table)] if exists else []


def advance_sequence(db, table, largest):
    if table not in {'turns','access_audit'}:
        raise ValueError('Unsupported identity sequence')
    if largest:
        sequence = table+'_id_seq'
        # Migration callers serialize these advances; never rewind a global sequence.
        db.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s,'id'), "
            "GREATEST((SELECT last_value FROM {}),%s),true)").format(sql.Identifier(sequence)),(table,largest))


def import_identity(database, source_path, *, apply=False):
    with read_source(source_path) as (source,digest):
        users = rows(source,'access_users')
        workspaces = rows(source,'access_workspaces')
        members = rows(source,'access_members')
        audit = rows(source,'access_audit')
        if not users:
            raise ValueError('Source has no identity users')
        report = {'kind':'identity','applied':apply,'source_sha256':digest,
                  'counts':{'users':len(users),'workspaces':len(workspaces),'members':len(members),'audit':len(audit)},
                  'login_sessions_imported':0,'source_unchanged':True}
        with database.transaction() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('identity-import:'+database.settings.schema,))
            tables = ('access_users','access_workspaces','access_members','access_sessions','access_audit')
            db.execute(sql.SQL('LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE').format(sql.SQL(',').join(map(sql.Identifier,tables))))
            for table in tables:
                if db.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n']:
                    raise ValueError('Target identity store is not empty; refusing overwrite')
            if apply:
                for row in users:
                    db.execute('INSERT INTO access_users(id,username,display_name,password_hash,is_admin,disabled) VALUES(%s,%s,%s,%s,%s,%s)',
                        tuple(row[k] for k in ('id','username','display_name','password_hash','is_admin','disabled')))
                for row in workspaces:
                    db.execute('INSERT INTO access_workspaces VALUES(%s,%s,%s)', tuple(row[k] for k in ('id','name','created_at')))
                for row in members:
                    db.execute('INSERT INTO access_members VALUES(%s,%s,%s)', tuple(row[k] for k in ('workspace_id','user_id','role')))
                for row in audit:
                    db.execute('INSERT INTO access_audit(id,created_at,actor_id,action,target_id,workspace_id) VALUES(%s,%s,%s,%s,%s,%s)',
                        tuple(row[k] for k in ('id','created_at','actor_id','action','target_id','workspace_id')))
                advance_sequence(db,'access_audit',max((r['id'] for r in audit),default=0))
                if checksum(source_path)!=digest:
                    raise ValueError('Source changed; migration rolled back')
        return report


def import_workspace(database, source_path, workspace, *, runs_path=None, apply=False):
    if workspace!='local' and not re.fullmatch(r'[a-f0-9]{32}',workspace):
        raise ValueError('Select local or an existing team workspace ID')
    with read_source(source_path) as (source,digest):
        sessions,turns,memories = (rows(source,t) for t in ('sessions','turns','session_memories'))
        runs = []
        run_digest = None
        if runs_path is not None:
            with read_source(runs_path) as (run_source,run_digest):
                runs = rows(run_source,'research_runs')
        session_ids = {s['id'] for s in sessions}
        turn_ids = {t['id'] for t in turns}
        if any(t['session_id'] not in session_ids for t in [*turns,*memories,*runs]):
            raise ValueError('Source has orphaned session references')
        if any(r['turn_id'] is not None and r['turn_id'] not in turn_ids for r in runs):
            raise ValueError('Source run points to a missing turn')
        report = {'kind':'workspace','workspace_id':workspace,'applied':apply,'source_sha256':digest,
                  'runs_sha256':run_digest,'source_unchanged':True,
                  'counts':{'sessions':len(sessions),'turns':len(turns),'memories':len(memories),'runs':len(runs)}}
        # Validate JSON before opening the destination transaction, including dry-run.
        for row in turns:
            row['answer_json'] = json.loads(row['answer_json'])
            row['attachments_json'] = json.loads(row.get('attachments_json','[]'))
        for row in memories:
            row['payload'] = json.loads(row['payload'])
        for row in runs:
            row['request_json'] = json.loads(row['request_json'])
        with database.transaction(workspace) as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('workspace-import:'+database.settings.schema,))
            # Fence concurrent ordinary writers between the empty check and commit.
            tables = ('business_scopes','sessions','turns','session_memories','research_runs')
            db.execute(sql.SQL('LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE').format(sql.SQL(',').join(map(sql.Identifier,tables))))
            if workspace!='local' and not db.execute('SELECT id FROM access_workspaces WHERE id=%s',(workspace,)).fetchone():
                raise ValueError('Import team identities before importing their workspace data')
            for table in ('sessions','turns','session_memories','research_runs'):
                if db.execute(sql.SQL('SELECT count(*) AS n FROM {} WHERE workspace_id=%s').format(sql.Identifier(table)),(workspace,)).fetchone()['n']:
                    raise ValueError('Destination workspace is not empty; refusing overwrite')
            if apply:
                db.execute('INSERT INTO business_scopes VALUES(%s) ON CONFLICT DO NOTHING',(workspace,))
                for row in sessions:
                    db.execute('INSERT INTO sessions VALUES(%s,%s,%s,%s,%s)',
                               (workspace,*(row[k] for k in ('id','title','created_at','updated_at'))))
                for row in turns:
                    db.execute('INSERT INTO turns(workspace_id,id,session_id,question,answer_json,created_at,attachments_json,run_id) '
                        'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', (workspace,row['id'],row['session_id'],row['question'],
                        Jsonb(row['answer_json']),row['created_at'],Jsonb(row['attachments_json']),row.get('run_id')))
                for row in memories:
                    db.execute('INSERT INTO session_memories VALUES(%s,%s,%s,%s)', (workspace,row['session_id'],row['revision'],Jsonb(row['payload'])))
                for row in runs:
                    db.execute('INSERT INTO research_runs VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (workspace,row['id'],row['session_id'],Jsonb(row['request_json']),row['status'],row['stage'],
                         row['turn_id'],row['created_at'],row['updated_at']))
                advance_sequence(db,'turns',max(turn_ids,default=0))
                if checksum(source_path)!=digest or (runs_path and checksum(runs_path)!=run_digest):
                    raise ValueError('Source changed; migration rolled back')
        return report
