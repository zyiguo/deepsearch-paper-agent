"""One bounded PostgreSQL pool per API process, with transaction-local scope."""
from contextlib import contextmanager
import re

from deepsearch_agent.core.errors import ApplicationError


class PostgresDatabase:
    def __init__(self, settings, *, check_schema=True):
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError:
            raise RuntimeError('PostgreSQL enabled; install pip install -e ".[database]"') from None
        self.settings = settings
        self.errors = psycopg.Error
        self.closed = False
        self.pool = ConnectionPool(settings.url, min_size=1, max_size=settings.pool_size,
            timeout=settings.timeout, max_waiting=64, open=False, name='deepsearch-business',
            kwargs={'row_factory':dict_row, 'connect_timeout':settings.timeout,
                    'options':f'-c statement_timeout={settings.statement_timeout_ms} -c lock_timeout={settings.timeout*1000}'})
        try:
            self.pool.open(wait=True, timeout=settings.timeout)
            with self.transaction() as db:
                role = db.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
                if role['rolsuper'] or role['rolbypassrls']:
                    raise RuntimeError('Use a non-superuser PostgreSQL role without BYPASSRLS')
            if check_schema:
                from deepsearch_agent.adapters.persistence.postgres_schema import validate_schema
                validate_schema(self)
        except Exception:
            self.close()
            raise

    @contextmanager
    def transaction(self, workspace=None):
        if workspace is not None and workspace != 'local' and not re.fullmatch(r'[a-f0-9]{32}', workspace):
            raise ValueError('Invalid database workspace scope')
        if self.closed:
            raise ApplicationError(503, '业务数据库已关闭')
        from psycopg_pool import PoolTimeout, TooManyRequests
        try:
            with self.pool.connection(timeout=self.settings.timeout) as db:
                with db.transaction():
                    db.execute("SELECT set_config('search_path', %s, true)", (self.settings.schema + ',pg_catalog',))
                    db.execute("SELECT set_config('deepsearch.workspace_id', %s, true)", (workspace or '',))
                    yield db
        except (self.errors, PoolTimeout, TooManyRequests):
            # Never include the driver exception, query text, DSN or server messages.
            raise ApplicationError(503, '业务数据库暂时不可用，请稍后重试') from None

    def ping(self):
        try:
            with self.transaction() as db:
                return db.execute('SELECT 1 AS ready').fetchone()['ready'] == 1
        except ApplicationError:
            return False

    def close(self):
        if not self.closed:
            self.closed = True
            self.pool.close(timeout=self.settings.timeout)
