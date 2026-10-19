"""Account and workspace authorization policies, independent of HTTP transport."""
import re

from deepsearch_agent.adapters.passwords import hash_password, verify_password
from deepsearch_agent.core.errors import ApplicationError, PersistenceConflict


def normalized_username(value):
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}', value):
        raise ApplicationError(422, '用户名须为 3–64 位字母、数字、点、下划线或连字符')
    return value


class AccessService:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        # Match the work factor for unknown users without creating any default account.
        self.dummy_hash = hash_password('dummy-credential-never-valid')

    def create_user(self, username, display_name, password, *, admin=False, actor=None):
        name = normalized_username(username)
        if not display_name.strip() or len(display_name) > 100:
            raise ApplicationError(422, '显示名称须为 1–100 个字符')
        try:
            encoded = hash_password(password)
        except ValueError as error:
            raise ApplicationError(422, str(error)) from None
        try:
            ident = self.store.create_user(name, display_name.strip(), encoded, admin=admin, actor=actor)
        except PersistenceConflict:
            raise ApplicationError(409, '用户名已存在') from None
        return {'id': ident, 'username': name, 'display_name': display_name.strip(), 'is_admin': admin, 'disabled': False}

    def login(self, username, password, old_token=None):
        name = username.strip().lower()
        if not self.store.login_allowed(name):
            raise ApplicationError(429, '登录尝试过多，请一分钟后重试')
        user = self.store.user_by_name(name)
        valid = verify_password(password, user['password_hash'] if user else self.dummy_hash)
        if not valid or not user or user['disabled']:
            self.store.audit('session.login_failed')
            raise ApplicationError(401, '用户名或密码错误')
        try:
            token = self.store.new_session(user['id'], self.settings.session_seconds, old_token,
                                           expected_hash=user['password_hash'])
        except ValueError:
            raise ApplicationError(401, '账号状态已改变，请重新登录') from None
        self.store.audit('session.login', user['id'])
        return token

    def require_user(self, token):
        user = self.store.authenticate(token)
        if user is None:
            raise ApplicationError(401, '请先登录，或登录已过期')
        return user

    def require_admin(self, user):
        if not user['is_admin']:
            raise ApplicationError(403, '需要系统管理员权限')

    def change_password(self, user, current, new):
        record = self.store.user_by_name(user['username'])
        if not record or not verify_password(current, record['password_hash']):
            raise ApplicationError(403, '当前密码错误')
        try:
            encoded = hash_password(new)
        except ValueError as error:
            raise ApplicationError(422, str(error)) from None
        try:
            self.store.reset_password(user['username'], encoded, expected_hash=record['password_hash'])
        except ValueError:
            raise ApplicationError(409, '密码已改变，请重新登录后重试') from None
        self.store.audit('user.password_changed', user['id'], user['id'])

    def authorize(self, user, workspace, *, write=False, owner=False):
        role = self.store.role(user['id'], workspace)
        if role is None:
            raise ApplicationError(404, '工作空间不存在或无权访问')
        if (owner and role != 'owner') or (write and role == 'viewer'):
            raise ApplicationError(403, '当前成员没有此操作权限')
        return role

    def create_workspace(self, user, name):
        if not name.strip() or len(name) > 100:
            raise ApplicationError(422, '工作空间名称须为 1–100 个字符')
        ident = self.store.create_workspace(user['id'], name.strip())
        return next(w for w in self.store.workspaces(user['id']) if w['id'] == ident)

    def set_member(self, user, workspace, username, role):
        self.authorize(user, workspace, owner=True)
        if role not in {'editor', 'viewer'}:
            raise ApplicationError(422, '可分配角色为 editor 或 viewer')
        target = self.store.user_by_name(normalized_username(username))
        if not target or target['disabled']:
            raise ApplicationError(404, '用户不存在或已停用')
        if self.store.role(target['id'], workspace) == 'owner':
            raise ApplicationError(409, '不能修改工作空间所有者')
        self.store.set_member(workspace, target['id'], role, user['id'])

    def remove_member(self, user, workspace, target):
        self.authorize(user, workspace, owner=True)
        if self.store.role(target, workspace) == 'owner':
            raise ApplicationError(409, '不能移除工作空间所有者')
        self.store.remove_member(workspace, target, user['id'])
