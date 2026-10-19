"""Opt-in business database configuration; connection secrets never enter repr."""
from dataclasses import dataclass, field
from pathlib import Path
import os
import re
from urllib.parse import urlsplit, parse_qs

from dotenv import dotenv_values
from deepsearch_agent.core.paths import PROJECT_ROOT


@dataclass(frozen=True)
class DatabaseSettings:
    backend: str = 'sqlite'
    url: str = field(default='', repr=False)
    schema: str = 'deepsearch'
    pool_size: int = 8
    timeout: int = 3
    statement_timeout_ms: int = 5000

    def __post_init__(self):
        if self.backend not in {'sqlite', 'postgresql'}:
            raise ValueError('DATABASE_BACKEND must be sqlite or postgresql')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', self.schema) or self.schema.startswith(('pg_', 'information_')):
            raise ValueError('DATABASE_SCHEMA must be a short application schema name')
        if not 1 <= self.pool_size <= 32 or not 1 <= self.timeout <= 30 or not 100 <= self.statement_timeout_ms <= 60000:
            raise ValueError('Invalid database pool or timeout setting')
        if self.backend == 'postgresql':
            try:
                value = urlsplit(self.url)
                params = parse_qs(value.query, strict_parsing=True)
                valid = (value.scheme == 'postgresql' and bool(value.hostname) and bool(value.username)
                         and bool(value.path.strip('/')) and not value.fragment
                         and set(params) <= {'sslmode'}
                         and all(len(v) == 1 for v in params.values())
                         and params.get('sslmode', ['prefer'])[0] in {'disable','allow','prefer','require','verify-ca','verify-full'})
                _ = value.port
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise ValueError('Invalid DATABASE_URL; use postgresql:// with an optional sslmode only')
        elif self.url:
            raise ValueError('DATABASE_URL requires DATABASE_BACKEND=postgresql')

    @classmethod
    def load(cls, path=PROJECT_ROOT / '.env.database'):
        values = dict(os.environ)
        if Path(path).exists():
            values.update({k:v for k,v in dotenv_values(path, interpolate=False).items() if v is not None})
        try:
            return cls(backend=values.get('DATABASE_BACKEND', 'sqlite'), url=values.get('DATABASE_URL', ''),
                       schema=values.get('DATABASE_SCHEMA', 'deepsearch'),
                       pool_size=int(values.get('DATABASE_POOL_SIZE', '8')),
                       timeout=int(values.get('DATABASE_TIMEOUT_SECONDS', '3')),
                       statement_timeout_ms=int(values.get('DATABASE_STATEMENT_TIMEOUT_MS', '5000')))
        except (TypeError, ValueError):
            raise ValueError('Database configuration invalid; check .env.database') from None
