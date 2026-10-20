"""Optional Redis infrastructure; application data remains in local durable stores."""
from dataclasses import dataclass, field
from collections import OrderedDict
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
from threading import Lock, BoundedSemaphore, Thread
from concurrent.futures import Future
import time
from urllib.parse import urlsplit

from dotenv import dotenv_values
from deepsearch_agent.core.governance import current_governance

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InfrastructureSettings:
    redis_url: str = field(default='', repr=False)
    namespace: str = 'deepsearch'
    cache_ttl: int = 86400
    rate_limit: int = 0
    rate_window: int = 60
    socket_timeout: float = 0.3
    cooldown: float = 5.0

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.namespace):
            raise ValueError('INFRA_NAMESPACE must contain 1-64 letters, digits, underscores or hyphens')
        if (self.cache_ttl <= 0 or self.rate_limit < 0 or self.rate_window <= 0
                or not math.isfinite(self.socket_timeout) or not 0 < self.socket_timeout <= 5
                or not math.isfinite(self.cooldown) or self.cooldown <= 0):
            raise ValueError('Invalid infrastructure limits or timeouts')
        if self.redis_url:
            try:
                url = urlsplit(self.redis_url)
                valid = (url.scheme in {'redis', 'rediss'} and bool(url.hostname)
                         and not url.query and not url.fragment
                         and (url.path in {'', '/'} or re.fullmatch(r'/[0-9]+', url.path)))
                _ = url.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('REDIS_URL must be a redis:// or rediss:// URL without query or fragment')

    @classmethod
    def load(cls, path: Path):
        values = dict(os.environ)
        if path.exists():
            values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
        try:
            return cls(redis_url=values.get('REDIS_URL', '').strip(),
                       namespace=values.get('INFRA_NAMESPACE', 'deepsearch'),
                       cache_ttl=int(values.get('EMBEDDING_CACHE_TTL_SECONDS', '86400')),
                       rate_limit=int(values.get('API_RATE_LIMIT', '0')),
                       rate_window=int(values.get('API_RATE_WINDOW_SECONDS', '60')),
                       socket_timeout=float(values.get('REDIS_TIMEOUT_SECONDS', '0.3')),
                       cooldown=float(values.get('REDIS_COOLDOWN_SECONDS', '5')))
        except (ValueError, TypeError):
            raise ValueError('Infrastructure configuration invalid; check .env.infrastructure') from None


# Increment + expiration are atomic, including when the first request is interrupted.
RATE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {count, redis.call('TTL', KEYS[1])}
"""


class RedisBackend:
    def __init__(self, client, settings, *, clock=time.monotonic):
        self.client, self.settings, self.clock = client, settings, clock
        self.lock = Lock()
        self.retry_at = 0.0
        self.status = 'unchecked'
        self.closed = False
        # Match the connection pool bound; slow DNS must not accumulate threads.
        self.in_flight = BoundedSemaphore(16)

    def call(self, operation, *args, **kwargs):
        with self.lock:
            if self.closed or self.clock() < self.retry_at:
                return None
        future = None
        try:
            if not self.in_flight.acquire(blocking=False):
                raise TimeoutError('Redis operation capacity reached')
            future = Future()
            def execute():
                try:
                    if future.set_running_or_notify_cancel():
                        if self.closed:
                            raise RuntimeError('Redis backend closed')
                        future.set_result(getattr(self.client, operation)(*args, **kwargs))
                except BaseException as error:
                    if not future.done():
                        future.set_exception(error)
                finally:
                    self.in_flight.release()
                    # A DNS lookup may finish after close; discard late connections.
                    if self.closed:
                        self._close_client()
            try:
                # Daemon workers let shutdown finish even if OS DNS remains blocked.
                Thread(target=execute, daemon=True, name='deepsearch-redis-io').start()
            except BaseException:
                self.in_flight.release()
                raise
            # Socket connect/read limits exclude DNS. Bound their total wall time too.
            result = future.result(timeout=2 * self.settings.socket_timeout)
        except Exception:
            if future is not None:
                future.cancel()
            # Boundary around Redis only: never expose server messages or connection URLs.
            with self.lock:
                first = self.status != 'unavailable'
                self.status = 'unavailable'
                self.retry_at = self.clock() + self.settings.cooldown
            if first:
                log.warning('redis_unavailable: cache bypassed; rate limiter uses local fallback')
            if current_governance.get() is not None:
                current_governance.get().event('redis','unavailable')
            return None
        with self.lock:
            self.status = 'available'
            self.retry_at = 0.0
        return result

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self._close_client()

    def _close_client(self):
        try:
            self.client.close()
        except Exception:
            log.warning('redis_close_failed')


def build_redis(settings):
    if not settings.redis_url:
        return None
    try:
        from redis import Redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:
        raise RuntimeError('Redis is enabled; install the infrastructure extra: pip install -e ".[infrastructure]"') from None
    client = Redis.from_url(settings.redis_url, decode_responses=True,
                            socket_connect_timeout=settings.socket_timeout,
                            socket_timeout=settings.socket_timeout,
                            retry=Retry(NoBackoff(), 0), max_connections=16)
    return RedisBackend(client, settings)


class CachedEmbeddingClient:
    def __init__(self, delegate, backend, identity, dimensions, scope=''):
        self.delegate, self.backend = delegate, backend
        self.identity, self.dimensions, self.scope = identity, dimensions, scope

    def __deepcopy__(self, memo):
        # Knowledge-base snapshots share stateless providers and their connection pool.
        return self

    def key(self, text):
        digest = hashlib.sha256(json.dumps([self.identity, self.scope, text], ensure_ascii=False).encode()).hexdigest()
        return f'{self.backend.settings.namespace}:embedding:v1:{digest}'

    def valid(self, vector):
        return (isinstance(vector, list) and len(vector) == self.dimensions
                and all(type(v) in (float, int) and math.isfinite(v) for v in vector)
                and math.isclose(sum(v * v for v in vector), 1.0, rel_tol=1e-5, abs_tol=1e-5))

    def embed(self, texts):
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError('Embedding 输入不能为空')
        if not texts:
            return []
        found, missing = {}, []
        for text in dict.fromkeys(texts):
            raw = self.backend.call('get', self.key(text))
            try:
                vector = json.loads(raw) if raw is not None else None
                if not self.valid(vector):
                    vector = None
            except (ValueError, TypeError, OverflowError):
                vector = None
            if vector is None:
                missing.append(text)
            else:
                found[text] = vector
            if current_governance.get() is not None:
                current_governance.get().record('embedding_cache',outcome='miss' if vector is None else 'hit')
        if missing:
            vectors = self.delegate.embed(missing)
            if len(vectors) != len(missing) or not all(self.valid(v) for v in vectors):
                raise ValueError('Embedding provider returned invalid vectors')
            for text, vector in zip(missing, vectors):
                found[text] = vector
                self.backend.call('set', self.key(text), json.dumps(vector, allow_nan=False),
                                  ex=self.backend.settings.cache_ttl)
        return [list(found[text]) for text in texts]


class RequestLimiter:
    """Fixed window per socket peer; bounded local fallback, no forwarded-header trust."""
    def __init__(self, settings, backend=None, *, clock=time.monotonic):
        self.settings, self.backend, self.clock = settings, backend, clock
        self.lock = Lock()
        self.local = OrderedDict()
        self.max_peers = 10000

    def check(self, peer):
        if not self.settings.rate_limit:
            return True, 0
        digest = hashlib.sha256(peer.encode()).hexdigest()
        if self.backend:
            result = self.backend.call('eval', RATE_SCRIPT, 1,
                f'{self.settings.namespace}:rate:v1:{digest}', self.settings.rate_window)
            if result is not None:
                count, ttl = result
                return count <= self.settings.rate_limit, max(1, int(ttl))
        now = self.clock()
        with self.lock:
            while self.local and next(iter(self.local.values()))[1] <= now:
                self.local.popitem(last=False)
            if digest not in self.local:
                if len(self.local) >= self.max_peers:
                    # Do not evict active peers, which could bypass the limit under churn.
                    return False, self.settings.rate_window
                self.local[digest] = [0, now + self.settings.rate_window]
            entry = self.local[digest]
            entry[0] += 1
            return entry[0] <= self.settings.rate_limit, max(1, math.ceil(entry[1] - now))
