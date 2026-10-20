"""Cache protection contracts, synchronized at observable registration/dispatch points.

The backend here is deliberately in memory. Real Redis acceptance is a separate
runner; these tests isolate singleflight, cancellation and durable billing rules.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import math
from threading import Barrier, Condition, Event, Lock

import pytest

from deepsearch_agent.adapters.governance_config import GovernanceSettings
from deepsearch_agent.adapters.infrastructure import CachedEmbeddingClient, InfrastructureSettings
from deepsearch_agent.adapters.persistence.governance import GovernanceStore
from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger
from deepsearch_agent.application.governance import Governance
from deepsearch_agent.application.tool_execution import ToolExecutor
from deepsearch_agent.core.cancellation import CancellationToken, ResearchCancelled, current_token
from deepsearch_agent.core.governance import GovernanceDenied, report_provider_usage, usage_context
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked, ToolKind, ToolNotExecuted


VECTOR = [0.6, 0.8]
OTHER_VECTOR = [1.0, 0.0]
DEADLINE = 5


class MemoryBackend:
    """Thread-safe cache with a controllable expiry clock, without Redis I/O."""
    def __init__(self, settings=None, *, clock=lambda: 0):
        self.settings = settings or InfrastructureSettings(cache_ttl_jitter_ratio=0)
        self.clock = clock
        self.lock = Lock()
        self.data, self.expiry, self.ttls = {}, {}, []
        self.reads = Counter()

    def call(self, operation, key, value=None, *, ex=None):
        with self.lock:
            if operation == 'get':
                self.reads[key] += 1
                if key in self.expiry and self.expiry[key] <= self.clock():
                    self.data.pop(key, None)
                    self.expiry.pop(key, None)
                return self.data.get(key)
            assert operation == 'set'
            self.data[key] = value
            self.expiry[key] = self.clock() + ex
            self.ttls.append(ex)
            return True


class Observer:
    """Registration metrics are the public synchronization point for followers."""
    def __init__(self):
        self.condition = Condition()
        self.records = Counter()
        self.durations = []

    def record(self, name, **labels):
        with self.condition:
            self.records[(name, tuple(sorted(labels.items())))] += 1
            self.condition.notify_all()

    def duration(self, name, seconds, **labels):
        with self.condition:
            self.durations.append((name, seconds, labels))
            self.condition.notify_all()

    def followers(self, count=1):
        key = ('embedding_singleflight', (('outcome', 'follower'),))
        with self.condition:
            assert self.condition.wait_for(lambda: self.records[key] >= count, timeout=DEADLINE), 'follower did not register'


class BlockingProvider:
    def __init__(self, *, error=None, result=None):
        self.lock, self.entered, self.release = Lock(), Event(), Event()
        self.calls = []
        self.error, self.result = error, result

    def embed(self, texts):
        with self.lock:
            self.calls.append(list(texts))
        self.entered.set()
        assert self.release.wait(timeout=DEADLINE), 'test did not release provider'
        if self.error is not None:
            raise self.error
        return self.result if self.result is not None else [list(VECTOR) for _ in texts]


def client_for(provider, *, backend=None, identity='model', scope='account', **settings):
    backend = backend or MemoryBackend(InfrastructureSettings(cache_ttl_jitter_ratio=0, **settings))
    return CachedEmbeddingClient(provider, backend, identity, 2, scope)


def request(client, texts, observer=None, token=None, **scope):
    marker = current_token.set(token)
    try:
        with usage_context(observer, **scope):
            return client.embed(texts)
    finally:
        current_token.reset(marker)


@contextmanager
def held_provider(provider, workers=4):
    # Always release before executor.shutdown, including assertion failures.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            yield pool
        finally:
            provider.release.set()


def cost_rows(governance):
    with governance.store.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM cost_reservations ORDER BY created_at,id')]


def observe_real_governance(governance, observer, monkeypatch):
    original = governance.record
    def record(name, **labels):
        original(name, **labels)
        observer.record(name, **labels)
    monkeypatch.setattr(governance, 'record', record)


@pytest.mark.parametrize('base,ratio', [(100, .1), (3, .1), (1, .99), (5, .99), (101, .25)])
def test_downward_jitter_has_exact_integer_bounds(base, ratio):
    settings = InfrastructureSettings(cache_ttl=base, cache_ttl_jitter_ratio=ratio)
    expected_low = max(1, base - math.floor(base * ratio))
    observed = []
    for end in ('low', 'high'):
        backend = MemoryBackend(settings)
        class Provider:
            def embed(self, texts):
                return [list(VECTOR) for _ in texts]
        def randint(low, high):
            assert (low, high) == (0, math.floor(base * ratio))
            return low if end == 'low' else high
        client = CachedEmbeddingClient(Provider(), backend, 'model', 2, randint=randint)
        client.embed(['a'])
        assert len(backend.ttls) == 1 and type(backend.ttls[0]) is int
        observed.extend(backend.ttls)
    assert sorted(observed) == [expected_low, base]


def test_zero_jitter_reproduces_fixed_ttl_without_sampling():
    provider = BlockingProvider()
    provider.release.set()
    backend = MemoryBackend(InfrastructureSettings(cache_ttl=123, cache_ttl_jitter_ratio=0))
    def no_random(*args):
        pytest.fail('fixed TTL should not sample randomness')
    client = CachedEmbeddingClient(provider, backend, 'model', 2, randint=no_random)
    client.embed(['a', 'b'])
    assert backend.ttls == [123, 123]


@pytest.mark.parametrize('kwargs', [
    {'cache_ttl_jitter_ratio': -.1}, {'cache_ttl_jitter_ratio': 1}, {'cache_ttl_jitter_ratio': 1.01},
    {'cache_ttl_jitter_ratio': float('nan')}, {'cache_ttl_jitter_ratio': float('inf')},
    {'singleflight_wait_seconds': 0}, {'singleflight_wait_seconds': float('nan')},
    {'singleflight_wait_seconds': float('inf')}, {'singleflight_max_keys': 0},
    {'singleflight_max_keys': 1.5}, {'singleflight_max_keys': True},
    {'singleflight_enabled': 'false'},
])
def test_protection_settings_reject_invalid_values(kwargs):
    with pytest.raises(ValueError):
        InfrastructureSettings(**kwargs)


def test_protection_defaults_match_the_deployment_contract():
    settings = InfrastructureSettings()
    assert settings.cache_ttl_jitter_ratio == .1
    assert settings.singleflight_enabled is True
    assert settings.singleflight_wait_seconds == 65
    assert settings.singleflight_max_keys == 4096


def test_protection_environment_loading_and_file_precedence(tmp_path, monkeypatch):
    path = tmp_path / '.env.infrastructure'
    monkeypatch.setenv('EMBEDDING_CACHE_TTL_JITTER_RATIO', '0.8')
    monkeypatch.setenv('EMBEDDING_SINGLEFLIGHT_ENABLED', 'true')
    monkeypatch.setenv('EMBEDDING_SINGLEFLIGHT_WAIT_SECONDS', '1')
    monkeypatch.setenv('EMBEDDING_SINGLEFLIGHT_MAX_KEYS', '2')
    path.write_text('EMBEDDING_CACHE_TTL_JITTER_RATIO=0\nEMBEDDING_SINGLEFLIGHT_ENABLED=false\n'
                    'EMBEDDING_SINGLEFLIGHT_WAIT_SECONDS=2.5\nEMBEDDING_SINGLEFLIGHT_MAX_KEYS=9\n', encoding='utf-8')
    settings = InfrastructureSettings.load(path)
    assert settings.cache_ttl_jitter_ratio == 0
    assert settings.singleflight_enabled is False
    assert settings.singleflight_wait_seconds == 2.5
    assert settings.singleflight_max_keys == 9
    path.write_text('EMBEDDING_SINGLEFLIGHT_ENABLED=secret-invalid-value\n', encoding='utf-8')
    with pytest.raises(ValueError) as raised:
        InfrastructureSettings.load(path)
    assert 'secret' not in str(raised.value)


def test_expired_hot_key_is_computed_once_across_request_snapshots():
    now = [0]
    backend = MemoryBackend(clock=lambda: now[0])
    provider, observer = BlockingProvider(), Observer()
    client = client_for(provider, backend=backend)
    backend.call('set', client.key('hot'), json.dumps(OTHER_VECTOR), ex=1)
    now[0] = 2
    with held_provider(provider) as pool:
        owner = pool.submit(request, client, ['hot', 'hot'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        followers = [pool.submit(request, deepcopy(client), ['hot'], observer) for _ in range(3)]
        observer.followers(3)
        assert provider.calls == [['hot']] and client.inflight_count == 1
        provider.release.set()
        first = owner.result(timeout=DEADLINE)
        results = [future.result(timeout=DEADLINE) for future in followers]
    assert first == [VECTOR, VECTOR] and results == [[VECTOR]] * 3
    first[0][0] = 99
    results[0][0][0] = 88
    assert first[1] == VECTOR and results[1] == [VECTOR]
    assert client.embed(['hot']) == [VECTOR]
    assert provider.calls == [['hot']] and client.inflight_count == 0


@pytest.mark.parametrize('owner_texts,follower_texts', [(['a', 'b'], ['b', 'a']), (['b', 'a'], ['a', 'b'])])
def test_reverse_batches_share_atomic_registration_and_keep_output_order(owner_texts, follower_texts):
    provider, observer = BlockingProvider(), Observer()
    provider.result = [VECTOR if text == 'a' else OTHER_VECTOR for text in owner_texts]
    client = client_for(provider)
    with held_provider(provider) as pool:
        first = pool.submit(request, client, owner_texts, observer)
        assert provider.entered.wait(timeout=DEADLINE)
        second = pool.submit(request, client, follower_texts, observer)
        observer.followers()
        assert provider.calls == [owner_texts]
        provider.release.set()
        assert first.result(timeout=DEADLINE) == provider.result
        assert second.result(timeout=DEADLINE) == list(reversed(provider.result))
    assert client.inflight_count == 0


def test_overlapping_batch_computes_all_owned_keys_before_waiting_for_overlap():
    provider, observer = BlockingProvider(), Observer()
    owned_dispatched = Event()
    original = provider.embed
    def embed(texts):
        if texts == ['a']:
            return original(texts)
        with provider.lock:
            provider.calls.append(list(texts))
        assert texts == ['b', 'c'], 'newly owned misses must keep their batch dispatch'
        owned_dispatched.set()
        return [list(OTHER_VECTOR), list(VECTOR)]
    provider.embed = embed
    client = client_for(provider)
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        second = pool.submit(request, client, ['b', 'a', 'c', 'b'], observer)
        observer.followers()
        assert owned_dispatched.wait(timeout=DEADLINE), 'owned work waited on another owner'
        assert not second.done() and client.inflight_count == 1
        provider.release.set()
        assert first.result(timeout=DEADLINE) == [VECTOR]
        assert second.result(timeout=DEADLINE) == [OTHER_VECTOR, VECTOR, VECTOR, OTHER_VECTOR]
    assert provider.calls == [['a'], ['b', 'c']] and client.inflight_count == 0


def test_simultaneous_overlapping_batches_keep_dispatch_parallel_and_each_key_unique():
    registered, dispatched = Barrier(2), Barrier(3)
    provider = BlockingProvider()
    class RegisterTogether(Observer):
        def record(self, name, **labels):
            super().record(name, **labels)
            if name == 'embedding_singleflight' and labels == {'outcome': 'leader'}:
                registered.wait(timeout=DEADLINE)
    observer = RegisterTogether()
    def embed(texts):
        with provider.lock:
            provider.calls.append(list(texts))
        dispatched.wait(timeout=DEADLINE)
        assert provider.release.wait(timeout=DEADLINE)
        return [list(OTHER_VECTOR if text == 'shared' else VECTOR) for text in texts]
    provider.embed = embed
    client = client_for(provider)
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a', 'shared'], observer)
        second = pool.submit(request, client, ['shared', 'b'], observer)
        dispatched.wait(timeout=DEADLINE)
        observer.followers()
        assert client.inflight_count == 3
        assert Counter(text for batch in provider.calls for text in batch) == {'a': 1, 'shared': 1, 'b': 1}
        assert sorted(map(len, provider.calls)) == [1, 2]
        provider.release.set()
        assert first.result(timeout=DEADLINE) == [VECTOR, OTHER_VECTOR]
        assert second.result(timeout=DEADLINE) == [OTHER_VECTOR, VECTOR]
    assert client.inflight_count == 0


def test_independent_keys_compute_in_parallel():
    provider = BlockingProvider()
    entered = Barrier(3)
    original = provider.embed
    def embed(texts):
        entered.wait(timeout=DEADLINE)
        return original(texts)
    provider.embed = embed
    client = client_for(provider)
    with held_provider(provider) as pool:
        futures = [pool.submit(client.embed, [text]) for text in ('a', 'b')]
        entered.wait(timeout=DEADLINE)
        assert client.inflight_count == 2
        provider.release.set()
        assert [future.result(timeout=DEADLINE) for future in futures] == [[VECTOR], [VECTOR]]
    assert sorted(provider.calls) == [['a'], ['b']] and client.inflight_count == 0


@pytest.mark.parametrize('identity,scope', [('other-model', 'account'), ('model', 'other-account'), ('model', 'account')])
def test_inflight_registry_is_isolated_by_model_scope_and_client(identity, scope):
    provider = BlockingProvider()
    entered = Barrier(3)
    original = provider.embed
    def embed(texts):
        entered.wait(timeout=DEADLINE)
        return original(texts)
    provider.embed = embed
    backend = MemoryBackend()
    first = client_for(provider, backend=backend)
    second = client_for(provider, backend=backend, identity=identity, scope=scope)
    with held_provider(provider) as pool:
        futures = [pool.submit(client.embed, ['same']) for client in (first, second)]
        entered.wait(timeout=DEADLINE)
        assert first.inflight_count == second.inflight_count == 1
        provider.release.set()
        assert [future.result(timeout=DEADLINE) for future in futures] == [[VECTOR], [VECTOR]]
    assert provider.calls == [['same'], ['same']]
    assert first.inflight_count == second.inflight_count == 0


def test_second_cache_lookup_avoids_dispatch_after_an_initial_miss():
    class FilledBetweenLookups(MemoryBackend):
        def call(self, operation, key, value=None, *, ex=None):
            result = super().call(operation, key, value, ex=ex)
            if operation == 'get' and self.reads[key] == 1:
                super().call('set', key, json.dumps(OTHER_VECTOR), ex=60)
            return result
    provider = BlockingProvider()
    provider.release.set()
    backend = FilledBetweenLookups()
    client = client_for(provider, backend=backend)
    assert client.embed(['a']) == [OTHER_VECTOR]
    assert provider.calls == [] and backend.reads[client.key('a')] >= 2
    assert client.inflight_count == 0


@pytest.mark.parametrize('corrupt', ['invalid-json', '[0,0]', '[true,1]', '[NaN,0]', '{}', '[1]'])
def test_corrupt_cached_values_are_never_published_to_followers(corrupt):
    provider, observer = BlockingProvider(), Observer()
    client = client_for(provider)
    client.backend.data[client.key('a')] = corrupt
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        second = pool.submit(request, client, ['a'], observer)
        observer.followers()
        provider.release.set()
        assert first.result(timeout=DEADLINE) == second.result(timeout=DEADLINE) == [VECTOR]
    assert provider.calls == [['a']] and json.loads(client.backend.data[client.key('a')]) == VECTOR


@pytest.mark.parametrize('vectors', [[VECTOR], [VECTOR, [0, 0]], [VECTOR, [True, 0]], [VECTOR, [float('nan'), 0]]])
def test_invalid_provider_batch_is_not_partially_cached_and_all_waiters_finish(vectors):
    provider, observer = BlockingProvider(result=vectors), Observer()
    client = client_for(provider)
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a', 'b'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        second = pool.submit(request, client, ['b'], observer)
        observer.followers()
        provider.release.set()
        with pytest.raises(ValueError):
            first.result(timeout=DEADLINE)
        with pytest.raises(RuntimeError, match='embedding_singleflight_leader_failed'):
            second.result(timeout=DEADLINE)
    assert client.backend.data == {} and client.inflight_count == 0
    provider.result = [VECTOR, OTHER_VECTOR]
    assert client.embed(['a', 'b']) == [VECTOR, OTHER_VECTOR]
    assert provider.calls == [['a', 'b'], ['a', 'b']]


@pytest.mark.parametrize('error', [
    RuntimeError('secret upstream endpoint'),
    ModelServiceError('secret response timeout', 504),
    ToolNotExecuted('secret leader quota'),
    ToolExecutionBlocked('quota_settlement_unavailable', 'secret-operation-id'),
])
def test_leader_errors_fan_out_safely_without_automatic_retry_and_cleanup(error):
    provider, observer = BlockingProvider(error=error), Observer()
    client = client_for(provider)
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        followers = [pool.submit(request, client, ['a'], observer) for _ in range(2)]
        observer.followers(2)
        provider.release.set()
        with pytest.raises(type(error)) as owner_error:
            first.result(timeout=DEADLINE)
        assert owner_error.value is error
        for future in followers:
            with pytest.raises(RuntimeError, match='embedding_singleflight_leader_failed') as follower_error:
                future.result(timeout=DEADLINE)
            assert type(follower_error.value) is RuntimeError and 'secret' not in str(follower_error.value)
    assert provider.calls == [['a']] and client.backend.data == {} and client.inflight_count == 0
    provider.error = None
    assert client.embed(['a']) == [VECTOR]
    assert provider.calls == [['a'], ['a']]


def test_follower_timeout_neither_takes_over_nor_releases_live_owner():
    provider, observer = BlockingProvider(), Observer()
    client = client_for(provider, singleflight_wait_seconds=.03)
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        second = pool.submit(request, client, ['a'], observer)
        observer.followers()
        with pytest.raises(RuntimeError, match='embedding_singleflight_timeout'):
            second.result(timeout=DEADLINE)
        assert client.inflight_count == 1 and not first.done() and provider.calls == [['a']]
        third = pool.submit(request, client, ['a'], observer)
        observer.followers(2)
        provider.release.set()
        assert first.result(timeout=DEADLINE) == third.result(timeout=DEADLINE) == [VECTOR]
    assert client.inflight_count == 0 and provider.calls == [['a']]


def test_follower_cancellation_preserves_owner_and_other_followers():
    provider, observer, token = BlockingProvider(), Observer(), CancellationToken()
    client = client_for(provider)
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        cancelled = pool.submit(request, client, ['a'], observer, token)
        remaining = pool.submit(request, client, ['a'], observer)
        observer.followers(2)
        token.cancel()
        with pytest.raises(ResearchCancelled):
            cancelled.result(timeout=DEADLINE)
        assert client.inflight_count == 1 and not first.done() and not remaining.done()
        provider.release.set()
        assert first.result(timeout=DEADLINE) == remaining.result(timeout=DEADLINE) == [VECTOR]
    assert provider.calls == [['a']] and client.inflight_count == 0


def test_cancelled_leader_notifies_followers_and_cleans_all_owned_keys():
    provider, observer = BlockingProvider(error=ResearchCancelled('research_cancelled')), Observer()
    client = client_for(provider)
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a', 'b'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        second = pool.submit(request, client, ['b', 'a'], observer)
        observer.followers()
        provider.release.set()
        with pytest.raises(ResearchCancelled):
            first.result(timeout=DEADLINE)
        with pytest.raises(RuntimeError, match='embedding_singleflight_leader_failed'):
            second.result(timeout=DEADLINE)
    assert client.inflight_count == 0 and client.backend.data == {} and provider.calls == [['a', 'b']]


def test_leader_token_cancellation_retains_ownership_until_provider_actually_finishes():
    provider, observer, token = BlockingProvider(), Observer(), CancellationToken()
    client = client_for(provider)
    with held_provider(provider) as pool:
        owner = pool.submit(request, client, ['a'], observer, token)
        assert provider.entered.wait(timeout=DEADLINE)
        follower = pool.submit(request, client, ['a'], observer)
        observer.followers()
        token.cancel()
        assert client.inflight_count == 1 and not owner.done() and not follower.done()
        assert provider.calls == [['a']]
        provider.release.set()
        with pytest.raises(ResearchCancelled):
            owner.result(timeout=DEADLINE)
        with pytest.raises(RuntimeError, match='embedding_singleflight_leader_failed'):
            follower.result(timeout=DEADLINE)
    assert client.inflight_count == 0 and provider.calls == [['a']]


def test_metrics_failures_cannot_change_a_successful_coalesced_result():
    provider = BlockingProvider()
    joined = Event()
    class BrokenObserver:
        def record(self, name, **labels):
            if name == 'embedding_singleflight' and labels == {'outcome': 'follower'}:
                joined.set()
            raise RuntimeError('metric backend unavailable')
        def duration(self, *args, **kwargs):
            raise RuntimeError('metric backend unavailable')
    observer = BrokenObserver()
    client = client_for(provider)
    with held_provider(provider) as pool:
        owner = pool.submit(request, client, ['a'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        follower = pool.submit(request, client, ['a'], observer)
        assert joined.wait(timeout=DEADLINE)
        provider.release.set()
        assert owner.result(timeout=DEADLINE) == follower.result(timeout=DEADLINE) == [VECTOR]
    assert client.inflight_count == 0 and provider.calls == [['a']]


def test_pre_cancelled_request_never_registers_or_dispatches():
    provider, token = BlockingProvider(), CancellationToken()
    client = client_for(provider)
    token.cancel()
    with pytest.raises(ResearchCancelled):
        request(client, ['a', 'b'], token=token)
    assert provider.calls == [] and client.inflight_count == 0


def test_capacity_rejects_entire_batch_without_partial_registrations():
    provider, observer = BlockingProvider(), Observer()
    client = client_for(provider, singleflight_max_keys=2)
    with pytest.raises(GovernanceDenied, match='embedding_singleflight_capacity'):
        client.embed(['a', 'b', 'c'])
    assert client.inflight_count == 0 and provider.calls == []
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a'], observer)
        assert provider.entered.wait(timeout=DEADLINE)
        with pytest.raises(GovernanceDenied, match='embedding_singleflight_capacity'):
            client.embed(['a', 'b', 'c'])
        assert client.inflight_count == 1 and provider.calls == [['a']]
        second = pool.submit(request, client, ['a'], observer)
        observer.followers()
        provider.release.set()
        assert first.result(timeout=DEADLINE) == second.result(timeout=DEADLINE) == [VECTOR]
    assert client.inflight_count == 0
    assert client.embed(['b', 'c']) == [VECTOR, VECTOR]


def test_disabling_singleflight_preserves_independent_provider_dispatch():
    provider, entered = BlockingProvider(), Barrier(3)
    original = provider.embed
    def embed(texts):
        entered.wait(timeout=DEADLINE)
        return original(texts)
    provider.embed = embed
    client = client_for(provider, singleflight_enabled=False)
    with held_provider(provider) as pool:
        futures = [pool.submit(client.embed, ['a']) for _ in range(2)]
        entered.wait(timeout=DEADLINE)
        assert client.inflight_count == 0
        provider.release.set()
        assert [future.result(timeout=DEADLINE) for future in futures] == [[VECTOR], [VECTOR]]
    assert provider.calls == [['a'], ['a']]


def test_paid_timeout_coalescing_keeps_one_unknown_cost_and_stable_tool_recovery(tmp_path, monkeypatch):
    governance = Governance(GovernanceStore(tmp_path / 'governance.sqlite3'), GovernanceSettings())
    ledger = SQLiteToolLedger(tmp_path / 'tools.sqlite3')
    observer, provider = Observer(), BlockingProvider(error=ModelServiceError('private provider timed out after charge', 504))
    observe_real_governance(governance, observer, monkeypatch)
    effects = []
    class PaidProvider:
        def embed(self, texts):
            def effect():
                effects.append('charge')
                return provider.embed(texts)
            return governance.invoke('embedding', 'synthetic-private-model', 100, effect)
    client = client_for(PaidProvider())
    runs = ['owner-run', 'follower-run-1', 'follower-run-2']
    for run in runs:
        ledger.begin_run(run)
    def execute(run):
        with usage_context(governance, user='private-' + run, workspace='private-workspace', run_id=run):
            return ToolExecutor(ledger, run).execute('embed-slot', 'embedding', ToolKind.PAID,
                {'text': 'private-query'}, lambda _: client.embed(['private-query']))
    with held_provider(provider) as pool:
        first = pool.submit(execute, runs[0])
        assert provider.entered.wait(timeout=DEADLINE)
        rest = [pool.submit(execute, run) for run in runs[1:]]
        observer.followers(2)
        assert len(cost_rows(governance)) == 1 and effects == ['charge']
        provider.release.set()
        for run, future in zip(runs, [first, *rest]):
            with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown') as raised:
                future.result(timeout=DEADLINE)
            operation = ledger.list_run(run)[0]
            assert raised.value.operation_id == operation['id']
            assert operation['kind'] == ToolKind.PAID and operation['status'] == 'unknown' and operation['attempt'] == 1
    costs = cost_rows(governance)
    assert len(costs) == 1
    cost = costs[0]
    assert cost['user_id'] == 'private-owner-run' and cost['run_id'] == runs[0]
    assert cost['operation_id'] == ledger.list_run(runs[0])[0]['id']
    assert cost['reserved'] == 100 and cost['actual'] is None and cost['status'] == 'unknown'
    assert client.inflight_count == 0 and client.backend.data == {}
    restarted = SQLiteToolLedger(ledger.path)
    restarted.recover_interrupted()
    for run in runs:
        with usage_context(governance, run_id=run), pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown'):
            ToolExecutor(restarted, run).execute('embed-slot', 'embedding', ToolKind.PAID,
                {'text': 'private-query'}, lambda _: pytest.fail('unknown paid operation must not resend'))
        assert restarted.list_run(run)[0]['attempt'] == 1
    assert effects == ['charge'] and len(cost_rows(governance)) == 1
    metrics = governance.render()
    assert 'deepsearch_embedding_singleflight_total{outcome="leader"} 1' in metrics
    assert 'deepsearch_embedding_singleflight_total{outcome="follower"} 2' in metrics
    assert 'deepsearch_embedding_singleflight_wait_seconds_count' in metrics
    assert all(secret not in metrics for secret in ('private-', 'private-query', 'owner-run', 'synthetic-private-model', cost['operation_id']))


def test_follower_quota_context_cannot_replace_the_leaders_billing_identity(tmp_path, monkeypatch):
    governance = Governance(GovernanceStore(tmp_path / 'governance.sqlite3'), GovernanceSettings())
    governance.store.set_limit('user', 'bob', 0, 'test-admin')
    observer, provider = Observer(), BlockingProvider()
    observe_real_governance(governance, observer, monkeypatch)
    class PaidProvider:
        def embed(self, texts):
            def effect():
                result = provider.embed(texts)
                report_provider_usage({'total_tokens': 7})
                return result
            return governance.invoke('embedding', 'synthetic', 100, effect)
    client = client_for(PaidProvider())
    with held_provider(provider) as pool:
        first = pool.submit(request, client, ['a'], governance, None,
                            user='alice', workspace='alpha', run_id='alice-run', operation_id='alice-operation')
        assert provider.entered.wait(timeout=DEADLINE)
        second = pool.submit(request, client, ['a'], governance, None,
                             user='bob', workspace='beta', run_id='bob-run', operation_id='bob-operation')
        observer.followers()
        provider.release.set()
        assert first.result(timeout=DEADLINE) == second.result(timeout=DEADLINE) == [VECTOR]
    costs = cost_rows(governance)
    assert len(costs) == 1
    assert (costs[0]['user_id'], costs[0]['workspace_id'], costs[0]['run_id'], costs[0]['operation_id']) == (
        'alice', 'alpha', 'alice-run', 'alice-operation')
    assert costs[0]['actual'] == 7 and costs[0]['status'] == 'settled'
    assert governance.store.usage('user', 'bob')['used_or_held'] == 0
    with usage_context(governance, user='bob', workspace='beta'), pytest.raises(GovernanceDenied, match='user_quota_exceeded'):
        client.embed(['independent'])
    assert len(cost_rows(governance)) == 1 and provider.calls == [['a']]
    assert client.inflight_count == 0


def test_mixed_paid_batch_keeps_unknown_after_owned_cost_settles_and_overlap_is_rejected(tmp_path, monkeypatch):
    governance = Governance(GovernanceStore(tmp_path / 'governance.sqlite3'), GovernanceSettings())
    ledger = SQLiteToolLedger(tmp_path / 'tools.sqlite3')
    observer = Observer()
    observe_real_governance(governance, observer, monkeypatch)
    shared = BlockingProvider(error=ToolNotExecuted('shared_precondition_rejected'))
    owned_settled, calls_lock = Event(), Lock()
    calls, effects, mixed_errors = [], [], []
    class MixedProvider:
        def embed(self, texts):
            with calls_lock:
                calls.append(list(texts))
            if texts == ['shared']:
                return shared.embed(texts)
            assert texts == ['owned'], 'the mixed request must dispatch only its newly owned key'
            def effect():
                effects.append('owned-charge')
                report_provider_usage({'total_tokens': 7})
                return [list(VECTOR)]
            result = governance.invoke('embedding', 'synthetic', 100, effect)
            # Set only after Governance has durably settled this provider call.
            owned_settled.set()
            return result
    client = client_for(MixedProvider())
    for run in ('shared-run', 'mixed-run'):
        ledger.begin_run(run)
    def execute(run, texts):
        def invoke(_):
            try:
                return client.embed(texts)
            except BaseException as error:
                if run == 'mixed-run':
                    mixed_errors.append(error)
                raise
        with usage_context(governance, user=run, workspace='workspace', run_id=run):
            return ToolExecutor(ledger, run).execute('embed-slot', 'embedding', ToolKind.PAID,
                {'texts': texts}, invoke)
    with held_provider(shared) as pool:
        first = pool.submit(execute, 'shared-run', ['shared'])
        assert shared.entered.wait(timeout=DEADLINE)
        mixed = pool.submit(execute, 'mixed-run', ['owned', 'shared'])
        observer.followers()
        assert owned_settled.wait(timeout=DEADLINE), 'owned paid work must finish before waiting on overlap'
        assert effects == ['owned-charge'] and not first.done() and not mixed.done()
        costs = cost_rows(governance)
        assert len(costs) == 1 and costs[0]['status'] == 'settled' and costs[0]['actual'] == 7
        assert costs[0]['run_id'] == 'mixed-run'
        shared.release.set()
        with pytest.raises(ToolNotExecuted, match='shared_precondition_rejected'):
            first.result(timeout=DEADLINE)
        with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown') as blocked:
            mixed.result(timeout=DEADLINE)
    assert len(mixed_errors) == 1 and type(mixed_errors[0]) is RuntimeError
    assert str(mixed_errors[0]) == 'embedding_singleflight_leader_failed'
    shared_row = ledger.list_run('shared-run')[0]
    mixed_row = ledger.list_run('mixed-run')[0]
    assert shared_row['status'] == 'not_executed' and shared_row['attempt'] == 1
    assert mixed_row['kind'] == ToolKind.PAID and mixed_row['status'] == 'unknown' and mixed_row['attempt'] == 1
    assert blocked.value.operation_id == mixed_row['id']
    assert costs[0]['operation_id'] == mixed_row['id']
    assert client.inflight_count == 0 and calls == [['shared'], ['owned']]
    assert json.loads(client.backend.data[client.key('owned')]) == VECTOR
    assert client.key('shared') not in client.backend.data
    restarted = SQLiteToolLedger(ledger.path)
    restarted.recover_interrupted()
    with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown') as replay:
        ToolExecutor(restarted, 'mixed-run').execute('embed-slot', 'embedding', ToolKind.PAID,
            {'texts': ['owned', 'shared']}, lambda _: pytest.fail('mixed paid operation must not resend'))
    assert replay.value.operation_id == mixed_row['id']
    with pytest.raises(ToolNotExecuted, match='shared_precondition_rejected') as rejected_replay:
        ToolExecutor(restarted, 'shared-run').execute('embed-slot', 'embedding', ToolKind.PAID,
            {'texts': ['shared']}, lambda _: pytest.fail('rejected operation must replay its rejection'))
    assert rejected_replay.value.replayed
    assert restarted.list_run('mixed-run')[0]['attempt'] == restarted.list_run('shared-run')[0]['attempt'] == 1
    assert effects == ['owned-charge'] and cost_rows(governance) == costs
