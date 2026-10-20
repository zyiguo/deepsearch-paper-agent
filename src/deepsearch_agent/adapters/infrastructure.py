"""Optional Redis infrastructure; application data remains in local durable stores."""
from dataclasses import dataclass, field
from collections import OrderedDict
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import random
import re
from threading import Lock, BoundedSemaphore, Thread
from concurrent.futures import Future, TimeoutError as FutureTimeout
import time
from urllib.parse import urlsplit

from dotenv import dotenv_values
from deepsearch_agent.core.governance import GovernanceDenied, current_governance
from deepsearch_agent.core.cancellation import current_token, ResearchCancelled

log = logging.getLogger(__name__)


def _finite_number(value):
    if type(value) not in (float, int):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class InfrastructureSettings:
    redis_url: str = field(default='', repr=False)
    namespace: str = 'deepsearch'
    cache_ttl: int = 86400
    rate_limit: int = 0
    rate_window: int = 60
    socket_timeout: float = 0.3
    cooldown: float = 5.0
    cache_ttl_jitter_ratio: float = 0.1
    singleflight_enabled: bool = True
    singleflight_wait_seconds: float = 65.0
    singleflight_max_keys: int = 4096

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.namespace):
            raise ValueError('INFRA_NAMESPACE must contain 1-64 letters, digits, underscores or hyphens')
        if (self.cache_ttl <= 0 or self.rate_limit < 0 or self.rate_window <= 0
                or not math.isfinite(self.socket_timeout) or not 0 < self.socket_timeout <= 5
                or not math.isfinite(self.cooldown) or self.cooldown <= 0):
            raise ValueError('Invalid infrastructure limits or timeouts')
        if (not _finite_number(self.cache_ttl_jitter_ratio)
                or not 0 <= self.cache_ttl_jitter_ratio < 1
                or type(self.singleflight_enabled) is not bool
                or not _finite_number(self.singleflight_wait_seconds)
                or self.singleflight_wait_seconds <= 0
                or type(self.singleflight_max_keys) is not int
                or self.singleflight_max_keys <= 0):
            raise ValueError('Invalid embedding cache protection limits')
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
            enabled = values.get('EMBEDDING_SINGLEFLIGHT_ENABLED', 'true').strip().lower()
            if enabled not in {'true', 'false'}:
                raise ValueError('Invalid embedding singleflight boolean')
            return cls(redis_url=values.get('REDIS_URL', '').strip(),
                       namespace=values.get('INFRA_NAMESPACE', 'deepsearch'),
                       cache_ttl=int(values.get('EMBEDDING_CACHE_TTL_SECONDS', '86400')),
                       rate_limit=int(values.get('API_RATE_LIMIT', '0')),
                       rate_window=int(values.get('API_RATE_WINDOW_SECONDS', '60')),
                       socket_timeout=float(values.get('REDIS_TIMEOUT_SECONDS', '0.3')),
                       cooldown=float(values.get('REDIS_COOLDOWN_SECONDS', '5')),
                       cache_ttl_jitter_ratio=float(values.get('EMBEDDING_CACHE_TTL_JITTER_RATIO', '0.1')),
                       singleflight_enabled=enabled == 'true',
                       singleflight_wait_seconds=float(values.get('EMBEDDING_SINGLEFLIGHT_WAIT_SECONDS', '65')),
                       singleflight_max_keys=int(values.get('EMBEDDING_SINGLEFLIGHT_MAX_KEYS', '4096')))
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
            try:
                if current_governance.get() is not None:
                    current_governance.get().event('redis','unavailable')
            except Exception:
                pass
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


class _EmbeddingSingleflightTimeout(RuntimeError):
    pass


class CachedEmbeddingClient:
    def __init__(self, delegate, backend, identity, dimensions, scope='', *,
                 clock=time.monotonic, randint=random.randint):
        self.delegate, self.backend = delegate, backend
        self.identity, self.dimensions, self.scope = identity, dimensions, scope
        self.clock, self.randint = clock, randint
        self._flight_lock = Lock()
        self._flights = {}

    def __deepcopy__(self, memo):
        # Snapshots share the provider, connection pool and this client's flights.
        return self

    @property
    def inflight_count(self):
        with self._flight_lock:
            return len(self._flights)

    def key(self, text):
        digest = hashlib.sha256(json.dumps([self.identity, self.scope, text], ensure_ascii=False).encode()).hexdigest()
        return f'{self.backend.settings.namespace}:embedding:v1:{digest}'

    def valid(self, vector):
        return (isinstance(vector, list) and len(vector) == self.dimensions
                and all(type(v) in (float, int) and math.isfinite(v) for v in vector)
                and math.isclose(sum(v * v for v in vector), 1.0, rel_tol=1e-5, abs_tol=1e-5))

    @staticmethod
    def _record(outcome, name='embedding_singleflight'):
        try:
            governance = current_governance.get()
            if governance is not None:
                governance.record(name, outcome=outcome)
        except Exception:
            # Observability cannot turn a completed provider call into a failure.
            pass

    def _read(self, text):
        raw = self.backend.call('get', self.key(text))
        try:
            vector = json.loads(raw) if raw is not None else None
            return vector if self.valid(vector) else None
        except (ValueError, TypeError, OverflowError):
            return None

    def _ttl(self):
        base = self.backend.settings.cache_ttl
        jitter = math.floor(base * self.backend.settings.cache_ttl_jitter_ratio)
        return max(1, base - self.randint(0, jitter)) if jitter else base

    def _compute(self, missing, found):
        vectors = self.delegate.embed(missing)
        if len(vectors) != len(missing) or not all(self.valid(v) for v in vectors):
            raise ValueError('Embedding provider returned invalid vectors')
        for text, vector in zip(missing, vectors):
            # Immutable values also isolate results from mutable provider buffers.
            found[text] = tuple(vector)
            self.backend.call('set', self.key(text), json.dumps(vector, allow_nan=False),
                              ex=self._ttl())

    @staticmethod
    def _check_cancelled():
        token = current_token.get()
        if token is not None:
            token.check()

    def _register(self, missing):
        owned, followed = {}, {}
        keys = {text: self.key(text) for text in missing}
        with self._flight_lock:
            new_count = sum(key not in self._flights for key in keys.values())
            if len(self._flights) + new_count > self.backend.settings.singleflight_max_keys:
                # Nothing is registered or dispatched until the whole batch fits.
                raise GovernanceDenied('embedding_singleflight_capacity', 503)
            for text, key in keys.items():
                if key in self._flights:
                    followed[text] = self._flights[key]
                else:
                    future = self._flights[key] = Future()
                    owned[text] = future
        return owned, followed

    def _complete_owned(self, owned, found):
        try:
            self._check_cancelled()
            missing = []
            # Another owner may have filled Redis between our first read and join.
            for text in owned:
                vector = self._read(text)
                if vector is None:
                    missing.append(text)
                else:
                    found[text] = tuple(vector)
            if missing:
                self._check_cancelled()
                # Keep provider work in the caller and its original billing context.
                self._compute(missing, found)
            self._check_cancelled()
        except BaseException:
            # Never pass another request's operation ID, provider message or a
            # ToolNotExecuted classification through the shared Future.
            for future in owned.values():
                future.set_exception(RuntimeError('embedding_singleflight_leader_failed'))
            raise
        else:
            # Future callbacks can run arbitrary code, so publish outside the lock.
            for text, future in owned.items():
                future.set_result(tuple(found[text]))
        finally:
            # Work and all publications have finished before ownership is released.
            with self._flight_lock:
                for text, future in owned.items():
                    key = self.key(text)
                    if self._flights.get(key) is future:
                        del self._flights[key]

    def _wait(self, followed, found):
        started = self.clock()
        deadline = started + self.backend.settings.singleflight_wait_seconds
        outcome = 'completed'
        try:
            for text, future in followed.items():
                while True:
                    self._check_cancelled()
                    if future.done():
                        found[text] = future.result()
                        break
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise _EmbeddingSingleflightTimeout('embedding_singleflight_timeout')
                    try:
                        found[text] = future.result(timeout=min(0.1, remaining))
                        break
                    except FutureTimeout:
                        # Do not cancel, relinquish or replace the owner's future.
                        continue
            self._check_cancelled()
        except _EmbeddingSingleflightTimeout:
            outcome = 'timeout'
            raise
        except ResearchCancelled:
            outcome = 'cancelled'
            raise
        except BaseException:
            outcome = 'error'
            raise
        finally:
            try:
                governance = current_governance.get()
                if governance is not None:
                    governance.duration('embedding_singleflight_wait', self.clock() - started,
                                        outcome=outcome)
            except Exception:
                pass

    def embed(self, texts):
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError('Embedding 输入不能为空')
        if not texts:
            return []
        found, missing = {}, []
        for text in dict.fromkeys(texts):
            vector = self._read(text)
            if vector is None:
                missing.append(text)
            else:
                found[text] = vector
            self._record('miss' if vector is None else 'hit', 'embedding_cache')
        if missing:
            if not self.backend.settings.singleflight_enabled:
                self._compute(missing, found)
            else:
                try:
                    owned, followed = self._register(missing)
                except GovernanceDenied:
                    self._record('capacity_rejected')
                    raise
                if owned:
                    self._record('leader')
                if followed:
                    self._record('follower')
                try:
                    # Compute all newly owned keys before waiting for overlaps.
                    # Reverse batches therefore cannot wait on each other's work.
                    if owned:
                        self._complete_owned(owned, found)
                    if followed:
                        self._wait(followed, found)
                except _EmbeddingSingleflightTimeout:
                    self._record('timeout')
                    raise
                except ResearchCancelled:
                    self._record('cancelled')
                    raise
                except BaseException:
                    self._record('error')
                    raise
                self._record('completed')
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
