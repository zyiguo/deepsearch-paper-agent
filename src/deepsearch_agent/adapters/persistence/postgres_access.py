"""PostgreSQL identity repository; authorization remains in application.access."""
import hashlib
import secrets
import time
from uuid import uuid4

from deepsearch_agent.adapters.persistence.access import public_row, USER_FIELDS
from deepsearch_agent.core.errors import PersistenceConflict


class PostgresAccessStore:
    def __init__(self, database, *, clock=time.time):
        self.database, self.clock = database, clock

    def connect(self):
        return self.database.transaction()

    def _audit(self, db, action, actor=None, target=None, workspace=None):
        db.execute('INSERT INTO access_audit(created_at,actor_id,action,target_id,workspace_id) VALUES(%s,%s,%s,%s,%s)',
                   (self.clock(), actor, action, target, workspace))

    def audit(self, action, actor=None, target=None, workspace=None):
        with self.connect() as db:
            self._audit(db, action, actor, target, workspace)

    def user_by_name(self, username):
        with self.connect() as db:
            return db.execute('SELECT * FROM access_users WHERE username=%s', (username,)).fetchone()

    def create_user(self, username, display_name, password_hash, *, admin=False, actor=None):
        ident = uuid4().hex
        with self.connect() as db:
            inserted = db.execute('INSERT INTO access_users(id,username,display_name,password_hash,is_admin,disabled) '
                'VALUES(%s,%s,%s,%s,%s,0) ON CONFLICT(username) DO NOTHING RETURNING id',
                (ident,username,display_name,password_hash,int(admin))).fetchone()
            if not inserted:
                raise PersistenceConflict('Username already exists')
            self._audit(db,'user.created',actor,ident)
        return ident

    def users(self):
        with self.connect() as db:
            return [public_row(r) for r in db.execute(f'SELECT {USER_FIELDS} FROM access_users ORDER BY username')]

    def login_allowed(self, username):
        key = hashlib.sha256(username.encode()).hexdigest()
        now = self.clock()
        with self.connect() as db:
            # A keyed transaction lock covers both a new and an existing attempt row.
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('login:'+self.database.settings.schema+':'+key,))
            db.execute('DELETE FROM access_login_attempts WHERE expires_at<=%s', (now,))
            row = db.execute('SELECT count FROM access_login_attempts WHERE account_hash=%s', (key,)).fetchone()
            if row is None:
                db.execute('INSERT INTO access_login_attempts VALUES(%s,1,%s)', (key,now+60))
                return True
            if row['count'] >= 5:
                return False
            db.execute('UPDATE access_login_attempts SET count=count+1 WHERE account_hash=%s', (key,))
            return True

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    def new_session(self, user_id, ttl, old_token=None, *, expected_hash=None):
        token = secrets.token_hex(32)
        with self.connect() as db:
            row = db.execute('SELECT password_hash,disabled FROM access_users WHERE id=%s FOR UPDATE', (user_id,)).fetchone()
            if not row or row['disabled'] or (expected_hash is not None and row['password_hash'] != expected_hash):
                raise ValueError('Credentials changed during login')
            db.execute('DELETE FROM access_sessions WHERE expires_at<=%s', (self.clock(),))
            if isinstance(old_token,str) and len(old_token)==64 and all(c in '0123456789abcdef' for c in old_token):
                db.execute('DELETE FROM access_sessions WHERE token_hash=%s', (self.token_hash(old_token),))
            db.execute('INSERT INTO access_sessions VALUES(%s,%s,%s)', (self.token_hash(token),user_id,self.clock()+ttl))
        return token

    def authenticate(self, token):
        if not isinstance(token,str) or len(token)!=64 or any(c not in '0123456789abcdef' for c in token):
            return None
        with self.connect() as db:
            row = db.execute('SELECT u.id,u.username,u.display_name,u.is_admin,u.disabled FROM access_sessions s '
                'JOIN access_users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires_at>%s AND u.disabled=0',
                (self.token_hash(token),self.clock())).fetchone()
        return public_row(row) if row else None

    def logout(self, token, actor):
        with self.connect() as db:
            db.execute('DELETE FROM access_sessions WHERE token_hash=%s', (self.token_hash(token),))
            self._audit(db,'session.logout',actor)

    def reset_password(self, username, password_hash, *, expected_hash=None):
        with self.connect() as db:
            row = db.execute('SELECT id,password_hash FROM access_users WHERE username=%s FOR UPDATE', (username,)).fetchone()
            if not row:
                raise KeyError(username)
            if expected_hash is not None and row['password_hash'] != expected_hash:
                raise ValueError('Credentials changed during password update')
            db.execute('UPDATE access_users SET password_hash=%s WHERE id=%s', (password_hash,row['id']))
            db.execute('DELETE FROM access_sessions WHERE user_id=%s', (row['id'],))
            self._audit(db,'user.password_reset',target=row['id'])

    def disable_user(self, ident, actor):
        with self.connect() as db:
            db.execute('UPDATE access_users SET disabled=1 WHERE id=%s AND is_admin=0', (ident,))
            db.execute('DELETE FROM access_sessions WHERE user_id=%s AND user_id IN '
                       '(SELECT id FROM access_users WHERE disabled=1)', (ident,))
            self._audit(db,'user.disabled',actor,ident)

    def enable_user(self, ident, actor):
        with self.connect() as db:
            db.execute('UPDATE access_users SET disabled=0 WHERE id=%s AND is_admin=0', (ident,))
            self._audit(db,'user.enabled',actor,ident)

    def workspaces(self, user_id):
        with self.connect() as db:
            return db.execute('SELECT w.*,m.role FROM access_workspaces w JOIN access_members m ON m.workspace_id=w.id '
                              'WHERE m.user_id=%s ORDER BY w.created_at,w.id', (user_id,)).fetchall()

    def create_workspace(self, user_id, name):
        ident = uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO access_workspaces VALUES(%s,%s,%s)', (ident,name,self.clock()))
            db.execute("INSERT INTO access_members VALUES(%s,%s,'owner')", (ident,user_id))
            self._audit(db,'workspace.created',user_id,workspace=ident)
        return ident

    def role(self, user_id, workspace):
        with self.connect() as db:
            row = db.execute('SELECT m.role FROM access_members m JOIN access_users u ON u.id=m.user_id '
                'WHERE m.workspace_id=%s AND m.user_id=%s AND u.disabled=0', (workspace,user_id)).fetchone()
        return row['role'] if row else None

    def members(self, workspace):
        with self.connect() as db:
            return [public_row(r) for r in db.execute('SELECT u.id,u.username,u.display_name,u.disabled,m.role '
                'FROM access_members m JOIN access_users u ON u.id=m.user_id WHERE m.workspace_id=%s ORDER BY u.username', (workspace,))]

    def set_member(self, workspace, user_id, role, actor):
        with self.connect() as db:
            db.execute('INSERT INTO access_members VALUES(%s,%s,%s) ON CONFLICT(workspace_id,user_id) '
                       'DO UPDATE SET role=excluded.role', (workspace,user_id,role))
            self._audit(db,'member.'+role,actor,user_id,workspace)

    def remove_member(self, workspace, user_id, actor):
        with self.connect() as db:
            db.execute("DELETE FROM access_members WHERE workspace_id=%s AND user_id=%s AND role!='owner'", (workspace,user_id))
            self._audit(db,'member.removed',actor,user_id,workspace)
