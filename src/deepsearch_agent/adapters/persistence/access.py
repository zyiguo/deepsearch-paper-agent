"""Identity database, independent of document and Agent persistence formats."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import secrets
import sqlite3
import time
from uuid import uuid4
from deepsearch_agent.core.errors import PersistenceConflict


USER_FIELDS = 'id, username, display_name, is_admin, disabled'


def public_row(row):
    result = dict(row)
    for key in ('is_admin', 'disabled'):
        if key in result:
            result[key] = bool(result[key])
    return result


class AccessStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS access_users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS access_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES access_users(id),
                    expires_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS access_sessions_user ON access_sessions(user_id);
                CREATE TABLE IF NOT EXISTS access_workspaces (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS access_members (
                    workspace_id TEXT NOT NULL REFERENCES access_workspaces(id),
                    user_id TEXT NOT NULL REFERENCES access_users(id),
                    role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),
                    PRIMARY KEY(workspace_id, user_id));
                CREATE TABLE IF NOT EXISTS access_login_attempts (
                    account_hash TEXT PRIMARY KEY, count INTEGER NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS access_audit (
                    id INTEGER PRIMARY KEY, created_at REAL NOT NULL, actor_id TEXT,
                    action TEXT NOT NULL, target_id TEXT, workspace_id TEXT);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, action, actor=None, target=None, workspace=None):
        with self.connect() as db:
            self._audit(db, action, actor, target, workspace)

    def _audit(self, db, action, actor=None, target=None, workspace=None):
        db.execute('INSERT INTO access_audit(created_at,actor_id,action,target_id,workspace_id) VALUES(?,?,?,?,?)',
                   (self.clock(), actor, action, target, workspace))

    def user_by_name(self, username):
        with self.connect() as db:
            row = db.execute('SELECT * FROM access_users WHERE username=?', (username,)).fetchone()
        return dict(row) if row else None

    def create_user(self, username, display_name, password_hash, *, admin=False, actor=None):
        ident = uuid4().hex
        with self.connect() as db:
            inserted = db.execute('INSERT INTO access_users VALUES(?,?,?,?,?,0) ON CONFLICT(username) DO NOTHING',
                                  (ident, username, display_name, password_hash, int(admin)))
            if inserted.rowcount == 0:
                raise PersistenceConflict('Username already exists')
            self._audit(db, 'user.created', actor, ident)
        return ident

    def users(self):
        with self.connect() as db:
            return [public_row(r) for r in db.execute(f'SELECT {USER_FIELDS} FROM access_users ORDER BY username')]

    def login_allowed(self, username):
        # Count unknown names too; never store supplied passwords or login names in attempts.
        key = hashlib.sha256(username.encode('utf-8')).hexdigest()
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM access_login_attempts WHERE expires_at<=?', (now,))
            row = db.execute('SELECT count FROM access_login_attempts WHERE account_hash=?', (key,)).fetchone()
            if row is None:
                db.execute('INSERT INTO access_login_attempts VALUES(?,1,?)', (key, now + 60))
                return True
            if row['count'] >= 5:
                return False
            db.execute('UPDATE access_login_attempts SET count=count+1 WHERE account_hash=?', (key,))
            return True

    def new_session(self, user_id, ttl, old_token=None, *, expected_hash=None):
        token = secrets.token_hex(32)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT password_hash,disabled FROM access_users WHERE id=?', (user_id,)).fetchone()
            if not row or row['disabled'] or (expected_hash is not None and row['password_hash'] != expected_hash):
                raise ValueError('Credentials changed during login')
            db.execute('DELETE FROM access_sessions WHERE expires_at<=?', (self.clock(),))
            if (isinstance(old_token, str) and len(old_token) == 64
                    and all(c in '0123456789abcdef' for c in old_token)):
                db.execute('DELETE FROM access_sessions WHERE token_hash=?', (self.token_hash(old_token),))
            db.execute('INSERT INTO access_sessions VALUES(?,?,?)',
                       (self.token_hash(token), user_id, self.clock() + ttl))
        return token

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    def authenticate(self, token):
        if not isinstance(token, str) or len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
            return None
        with self.connect() as db:
            row = db.execute('SELECT u.id,u.username,u.display_name,u.is_admin,u.disabled FROM access_sessions s '
                             'JOIN access_users u ON u.id=s.user_id '
                             'WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0',
                             (self.token_hash(token), self.clock())).fetchone()
        return public_row(row) if row else None

    def logout(self, token, actor):
        with self.connect() as db:
            db.execute('DELETE FROM access_sessions WHERE token_hash=?', (self.token_hash(token),))
            self._audit(db, 'session.logout', actor)

    def reset_password(self, username, password_hash, *, expected_hash=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id,password_hash FROM access_users WHERE username=?', (username,)).fetchone()
            if not row:
                raise KeyError(username)
            if expected_hash is not None and row['password_hash'] != expected_hash:
                raise ValueError('Credentials changed during password update')
            db.execute('UPDATE access_users SET password_hash=? WHERE id=?', (password_hash, row['id']))
            db.execute('DELETE FROM access_sessions WHERE user_id=?', (row['id'],))
            self._audit(db, 'user.password_reset', target=row['id'])

    def disable_user(self, ident, actor):
        with self.connect() as db:
            db.execute('UPDATE access_users SET disabled=1 WHERE id=? AND is_admin=0', (ident,))
            db.execute('DELETE FROM access_sessions WHERE user_id=? AND user_id IN '
                       '(SELECT id FROM access_users WHERE disabled=1)', (ident,))
            self._audit(db, 'user.disabled', actor, ident)

    def enable_user(self, ident, actor):
        with self.connect() as db:
            db.execute('UPDATE access_users SET disabled=0 WHERE id=? AND is_admin=0', (ident,))
            self._audit(db, 'user.enabled', actor, ident)

    def workspaces(self, user_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT w.*,m.role FROM access_workspaces w '
                'JOIN access_members m ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.created_at,w.id', (user_id,))]

    def create_workspace(self, user_id, name):
        ident = uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO access_workspaces VALUES(?,?,?)', (ident, name, self.clock()))
            db.execute("INSERT INTO access_members VALUES(?,?,'owner')", (ident, user_id))
            self._audit(db, 'workspace.created', user_id, workspace=ident)
        return ident

    def role(self, user_id, workspace):
        with self.connect() as db:
            row = db.execute('SELECT m.role FROM access_members m JOIN access_users u ON u.id=m.user_id '
                             'WHERE m.workspace_id=? AND m.user_id=? AND u.disabled=0', (workspace, user_id)).fetchone()
        return row['role'] if row else None

    def members(self, workspace):
        with self.connect() as db:
            return [public_row(r) for r in db.execute('SELECT u.id,u.username,u.display_name,u.disabled,m.role '
                'FROM access_members m JOIN access_users u ON u.id=m.user_id WHERE m.workspace_id=? '
                'ORDER BY u.username', (workspace,))]

    def set_member(self, workspace, user_id, role, actor):
        with self.connect() as db:
            db.execute('INSERT INTO access_members VALUES(?,?,?) ON CONFLICT(workspace_id,user_id) '
                       'DO UPDATE SET role=excluded.role', (workspace, user_id, role))
            self._audit(db, 'member.' + role, actor, user_id, workspace)

    def remove_member(self, workspace, user_id, actor):
        with self.connect() as db:
            db.execute("DELETE FROM access_members WHERE workspace_id=? AND user_id=? AND role!='owner'", (workspace, user_id))
            self._audit(db, 'member.removed', actor, user_id, workspace)
