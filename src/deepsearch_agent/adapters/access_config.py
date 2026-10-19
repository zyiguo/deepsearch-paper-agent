"""Explicit opt-in team access settings; never infer an existing data owner."""
from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import urlsplit

from dotenv import dotenv_values
from deepsearch_agent.core.paths import PROJECT_ROOT


@dataclass(frozen=True)
class AccessSettings:
    mode: str = 'local'
    root: Path = PROJECT_ROOT / 'data' / 'team'
    cookie_secure: bool = True
    session_seconds: int = 28800
    max_open_workspaces: int = 8
    origins: tuple[str, ...] = ('http://127.0.0.1:5173', 'http://localhost:5173',
                               'http://127.0.0.1:8000', 'http://localhost:8000')

    def __post_init__(self):
        if self.mode not in {'local', 'team'}:
            raise ValueError('ACCESS_MODE must be local or team')
        if not 300 <= self.session_seconds <= 86400 or not 1 <= self.max_open_workspaces <= 32:
            raise ValueError('Invalid access session or workspace limits')
        for origin in self.origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {'https', 'http'} or not parsed.netloc or parsed.path
                    or parsed.query or parsed.fragment or parsed.username or parsed.password):
                raise ValueError('ACCESS_ORIGINS must contain exact HTTP(S) origins without paths')
        object.__setattr__(self, 'root', Path(self.root).resolve())

    @property
    def cookie_name(self):
        return '__Host-deepsearch_session' if self.cookie_secure else 'deepsearch_session'

    @classmethod
    def load(cls, path=PROJECT_ROOT / '.env.access'):
        values = dict(os.environ)
        if Path(path).exists():
            values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
        secure = values.get('ACCESS_COOKIE_SECURE', 'true').lower()
        if secure not in {'true', 'false'}:
            raise ValueError('ACCESS_COOKIE_SECURE must be true or false')
        root = Path(values.get('ACCESS_DATA_ROOT', str(PROJECT_ROOT / 'data' / 'team')))
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        return cls(mode=values.get('ACCESS_MODE', 'local'), root=root, cookie_secure=secure == 'true',
                   session_seconds=int(values.get('ACCESS_SESSION_SECONDS', '28800')),
                   max_open_workspaces=int(values.get('ACCESS_MAX_OPEN_WORKSPACES', '8')),
                   origins=tuple(o.strip() for o in values.get('ACCESS_ORIGINS', ','.join(cls.origins)).split(',') if o.strip()))
