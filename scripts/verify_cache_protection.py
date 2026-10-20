"""Isolated, real-Redis cache protection acceptance runner.

No credentials, provider messages, submitted input, or absolute paths are included in
the public report. All embedding inputs and provider results here are synthetic.
An unavailable fault facility produces UNVERIFIED, never a passing fault result.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import secrets
import subprocess
import sys
from threading import Barrier, Condition, Event, Lock, get_ident
import time
from uuid import uuid4
from urllib.parse import urlsplit

from deepsearch_agent.adapters.governance_config import GovernanceSettings
from deepsearch_agent.adapters.infrastructure import (
    CachedEmbeddingClient, InfrastructureSettings, RedisBackend, build_redis,
)
from deepsearch_agent.adapters.persistence.governance import GovernanceStore
from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger
from deepsearch_agent.application.governance import Governance
from deepsearch_agent.application.tool_execution import ToolExecutor
from deepsearch_agent.core.cancellation import CancellationToken, ResearchCancelled, current_token
from deepsearch_agent.core.governance import (
    GovernanceDenied, current_usage_scope, report_provider_usage, usage_context,
)
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked, ToolKind


class VerificationFailure(RuntimeError):
    """Only fixed, non-sensitive assertion codes cross the report boundary."""


def require(condition, code):
    if not condition:
        raise VerificationFailure(code)


def wait_until(predicate, *, timeout=5.0, code='condition_timeout'):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    raise VerificationFailure(code)


def vector(text):
    index = hashlib.sha256(text.encode()).digest()[0] % 4
    return [float(index == i) for i in range(4)]


class CountedProvider:
    """Counts actual synthetic dispatches; an Event holds owners until observed."""

    def __init__(self, *, governed=None, fail=False, effect_path=None):
        self.lock = Condition()
        self.gate = Event()
        self.gate.set()
        self.calls = self.active = self.peak = 0
        self.texts = Counter()
        self.governed, self.fail, self.effect_path = governed, fail, effect_path

    def embed(self, texts):
        if self.governed is not None:
            return self.governed.invoke('embedding', 'synthetic-counted', 7,
                                        lambda: self._dispatch(texts))
        return self._dispatch(texts)

    def _dispatch(self, texts):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.texts.update(texts)
            self.lock.notify_all()
        try:
            if self.effect_path is not None:
                import sqlite3
                with sqlite3.connect(self.effect_path) as db:
                    db.execute('CREATE TABLE IF NOT EXISTS effects (id INTEGER PRIMARY KEY)')
                    db.execute('INSERT INTO effects DEFAULT VALUES')
            require(self.gate.wait(12), 'synthetic_provider_gate_timeout')
            if self.fail:
                raise RuntimeError('synthetic_provider_failure')
            report_provider_usage({'total_tokens': 3})
            return [vector(text) for text in texts]
        finally:
            with self.lock:
                self.active -= 1


class ObservedBackend:
    """Observe completed real GETs without substituting Redis responses."""

    def __init__(self, backend):
        self.backend, self.settings = backend, backend.settings
        self.lock = Lock()
        self.gets = 0
        self.get_threads = set()

    def call(self, operation, *args, **kwargs):
        result = self.backend.call(operation, *args, **kwargs)
        if operation == 'get':
            with self.lock:
                self.gets += 1
                self.get_threads.add(get_ident())
        return result


def create_output(path):
    """Refuse an existing file, directory or symlink, including interrupted runs."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()


def cleanup_namespace(client, namespace):
    """Delete only keys returned by this run's unpredictable literal prefix."""
    require(bool(re.fullmatch(r'cacheverify_[a-f0-9]{32}', namespace)),
            'cleanup_namespace_invalid')
    prefix = namespace + ':'
    removed = 0
    for key in client.scan_iter(match=prefix + '*', count=200):
        decoded = key.decode() if isinstance(key, bytes) else key
        require(isinstance(decoded, str) and decoded.startswith(prefix),
                'cleanup_key_outside_namespace')
        removed += client.delete(key)
    return removed


class OwnedDocker:
    """Every mutation rechecks the explicit isolated-service ownership label."""

    facility = 'docker-owned-container'

    def __init__(self, container, host, run_id):
        require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container)),
                'docker_container_invalid')
        self.container, self.run_id = container, run_id
        self.prefix = ['docker', *(['--host', host] if host else [])]
        self.paused = self.stopped = False
        self.verify()

    def command(self, args, timeout=20):
        result = subprocess.run([*self.prefix, *args], capture_output=True,
                                text=True, encoding='utf-8', timeout=timeout)
        require(result.returncode == 0, 'owned_docker_command_failed')
        return result.stdout

    def verify(self):
        data = json.loads(self.command(['inspect', self.container]))
        require(len(data) == 1, 'docker_inspect_invalid')
        labels = data[0].get('Config', {}).get('Labels') or {}
        require(labels.get('deepsearch.cache-test') == self.run_id,
                'docker_ownership_label_mismatch')

    def action(self, operation):
        require(operation in {'pause', 'unpause', 'stop', 'start'}, 'docker_action_invalid')
        self.verify()
        # A command timeout has an unknown effect; still attempt restoration.
        if operation == 'pause':
            self.paused = True
        elif operation == 'stop':
            self.stopped = True
        self.command([operation, *(['--time', '1'] if operation == 'stop' else []), self.container])
        if operation == 'pause':
            self.paused = True
        elif operation == 'unpause':
            self.paused = False
        elif operation == 'stop':
            self.stopped = True
        elif operation == 'start':
            self.stopped = False

    def verify_endpoint(self, client):
        self.verify()
        output = self.command(['exec', self.container, 'redis-cli', '--raw', 'INFO', 'server'])
        fields = dict(line.split(':', 1) for line in output.splitlines()
                      if ':' in line and not line.startswith('#'))
        require(bool(fields.get('run_id')) and fields['run_id'] == client.info('server').get('run_id'),
                'redis_endpoint_not_owned_container')

    def restore(self):
        if self.paused:
            self.action('unpause')
        if self.stopped:
            self.action('start')


# Constant code and JSON data are separate argv entries; no shell interpolation.
WSL_PROCESS_CONTROL = r'''
import json, os, shlex, signal, subprocess, sys, time
from pathlib import Path

def need(value, code):
    if not value:
        raise RuntimeError(code)

def main(data):
    root = os.path.realpath(data['root'])
    need(root == data['root'], 'wsl_root_not_canonical')
    for key in ('binary', 'config', 'pid_file'):
        path = data[key]
        need(os.path.isabs(path) and os.path.normpath(path) == path,
             'wsl_path_not_canonical')
        need(os.path.realpath(path) == path and os.path.commonpath([root, path]) == root,
             'wsl_path_outside_workspace_tmp')
    need(os.path.isfile(data['binary']) and os.path.isfile(data['config']),
         'wsl_owned_files_missing')
    settings = {}
    for line in Path(data['config']).read_text(encoding='utf-8').splitlines():
        parts = shlex.split(line, comments=True)
        if parts:
            need(parts[0].lower() != 'include', 'wsl_config_include_disallowed')
            settings[parts[0].lower()] = parts[1:]
    need(settings.get('daemonize') == ['yes'], 'wsl_config_daemonize_required')
    need(settings.get('set-proc-title') == ['no'], 'wsl_config_process_title_required')
    need(settings.get('pidfile') == [data['pid_file']], 'wsl_config_pidfile_mismatch')
    need(settings.get('port') == [str(data['port'])], 'wsl_config_port_mismatch')

    def active(pid):
        try:
            text = Path('/proc/' + str(pid) + '/stat').read_text()
            return text[text.rfind(')') + 2:].split()[0] != 'Z'
        except FileNotFoundError:
            return False

    def verify():
        pid = int(Path(data['pid_file']).read_text().strip())
        need(pid > 1 and active(pid), 'wsl_owned_pid_missing')
        if data.get('expected_pid') is not None:
            need(pid == data['expected_pid'], 'wsl_owned_pid_changed')
        base = '/proc/' + str(pid)
        need(os.path.realpath(base + '/exe') == data['binary'], 'wsl_owned_executable_mismatch')
        args = Path(base + '/cmdline').read_bytes().split(b'\0')
        need(os.fsencode(data['config']) in args, 'wsl_owned_config_not_in_cmdline')
        return pid

    operation = data['operation']
    need(operation in ('verify', 'pause', 'unpause', 'stop', 'start'), 'wsl_action_invalid')
    if operation == 'start':
        old = data.get('expected_pid')
        need(old is not None and not active(old), 'wsl_previous_process_still_active')
        if Path(data['pid_file']).exists():
            prior = int(Path(data['pid_file']).read_text().strip())
            need(prior > 1 and not active(prior), 'wsl_pidfile_process_still_active')
        child = subprocess.Popen([data['binary'], data['config']],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
        need(child.wait(timeout=5) == 0, 'wsl_redis_start_failed')
        until = time.monotonic() + 7
        data['expected_pid'] = None
        while time.monotonic() < until:
            try:
                pid = verify()
                need(pid != old, 'wsl_start_pid_not_changed')
                return {'ok': True, 'pid': pid}
            except (FileNotFoundError, ProcessLookupError):
                time.sleep(.03)
            except RuntimeError as error:
                if str(error) != 'wsl_owned_pid_missing':
                    raise
                time.sleep(.03)
        raise RuntimeError('wsl_start_pid_not_ready')
    pid = verify()
    if operation != 'verify':
        signum = {'pause': signal.SIGSTOP, 'unpause': signal.SIGCONT, 'stop': signal.SIGTERM}[operation]
        # pidfd fences PID reuse between identity validation and signal delivery.
        fd = os.pidfd_open(pid)
        try:
            need(verify() == pid, 'wsl_owned_pid_changed')
            signal.pidfd_send_signal(fd, signum)
        finally:
            os.close(fd)
        if operation == 'stop':
            until = time.monotonic() + 8
            while active(pid) and time.monotonic() < until:
                time.sleep(.03)
            need(not active(pid), 'wsl_owned_process_stop_timeout')
    return {'ok': True, 'pid': pid}

try:
    result = main(json.loads(sys.argv[1]))
except Exception as error:
    code = str(error) if type(error) is RuntimeError and str(error).startswith('wsl_') else 'wsl_control_failed'
    print(json.dumps({'ok': False, 'code': code, 'error_type': type(error).__name__}))
    sys.exit(1)
print(json.dumps(result))
'''


def workspace_wsl_tmp(workspace):
    windows = Path(workspace).resolve().as_posix()
    require(bool(re.match(r'^[A-Za-z]:/', windows)), 'wsl_requires_windows_workspace')
    return '/mnt/' + windows[0].lower() + windows[2:] + '/tmp'


def validate_wsl_spec(spec, run_id, redis_url, root):
    require(isinstance(spec, dict), 'wsl_control_spec_invalid')
    require(set(spec) == {'distro', 'run_id', 'binary', 'config', 'pid_file', 'port', 'redis_run_id'},
            'wsl_control_fields_invalid')
    require(spec['distro'] == 'Ubuntu-24.04', 'wsl_distribution_not_allowed')
    require(spec['run_id'] == run_id, 'wsl_ownership_run_id_mismatch')
    require(type(spec['port']) is int and 1024 <= spec['port'] <= 65535, 'wsl_port_invalid')
    require((urlsplit(redis_url).port or 6379) == spec['port'], 'wsl_redis_url_port_mismatch')
    require(bool(re.fullmatch(r'[a-f0-9]{40}', str(spec['redis_run_id']))), 'wsl_server_run_id_invalid')
    for field in ('binary', 'config', 'pid_file'):
        value = spec[field]
        require(isinstance(value, str) and PurePosixPath(value).is_absolute()
                and posixpath.normpath(value) == value, 'wsl_path_not_canonical')
        require(PurePosixPath(value).is_relative_to(PurePosixPath(root)) and value != root,
                'wsl_path_outside_workspace_tmp')
    require(len({spec['binary'], spec['config'], spec['pid_file']}) == 3, 'wsl_owned_paths_overlap')
    return spec


class OwnedWSL:
    """Control one explicitly identified Redis process, never an entire distro."""

    facility = 'wsl-owned-process'

    def __init__(self, control_path, run_id, redis_url, client):
        workspace = Path(__file__).resolve().parents[1]
        tmp = (workspace / 'tmp').resolve()
        path = Path(control_path)
        require(path.is_absolute() and path == path.resolve() and path.resolve().is_relative_to(tmp),
                'wsl_control_file_outside_workspace_tmp')
        require(not path.is_symlink() and path.is_file(), 'wsl_control_file_invalid')
        self.root = workspace_wsl_tmp(workspace)
        self.spec = validate_wsl_spec(json.loads(path.read_text(encoding='utf-8-sig')),
                                      run_id, redis_url, self.root)
        self.client, self.pid = client, None
        self.redis_run_id = self.spec['redis_run_id']
        self.paused = self.stopped = False
        self.verify_endpoint(client)

    def command(self, operation):
        data = {**self.spec, 'root': self.root, 'operation': operation, 'expected_pid': self.pid}
        result = subprocess.run(['wsl.exe', '--distribution', self.spec['distro'], '--exec',
            'python3', '-c', WSL_PROCESS_CONTROL, json.dumps(data)],
            capture_output=True, text=False, timeout=25)
        # wsl.exe can mix UTF-16 host warnings into stderr. Keep it opaque and
        # private; only the controller's UTF-8 stdout JSON is protocol data.
        require(isinstance(result.stdout, bytes), 'owned_wsl_control_invalid_response')
        try:
            payload = json.loads(result.stdout.decode('utf-8', errors='strict'))
        except (ValueError, TypeError):
            raise VerificationFailure('owned_wsl_control_invalid_response') from None
        require(isinstance(payload, dict), 'owned_wsl_control_invalid_response')
        if result.returncode != 0 or payload.get('ok') is not True:
            code = payload.get('code')
            require(isinstance(code, str) and bool(re.fullmatch(r'wsl_[a-z_]{1,80}', code)),
                    'owned_wsl_control_failed')
            raise VerificationFailure(code)
        require(type(payload.get('pid')) is int and payload['pid'] > 1, 'owned_wsl_control_invalid_pid')
        return payload

    def verify(self):
        result = self.command('verify')
        require(self.pid is None or result['pid'] == self.pid, 'wsl_owned_pid_changed')
        self.pid = result['pid']

    def verify_endpoint(self, client):
        self.verify()
        info = client.info('server')
        require(info.get('run_id') == self.redis_run_id and info.get('process_id') == self.pid
                and info.get('tcp_port') == self.spec['port'], 'redis_endpoint_not_owned_wsl_process')

    def action(self, operation):
        require(operation in {'pause', 'unpause', 'stop', 'start'}, 'wsl_action_invalid')
        if operation == 'start':
            require(self.stopped and self.pid is not None, 'wsl_start_without_owned_stop')
        else:
            self.verify()
        if operation == 'pause':
            self.paused = True
        elif operation == 'stop':
            self.stopped = True
        result = self.command(operation)
        if operation == 'unpause':
            self.paused = False
        elif operation == 'start':
            self.pid = result['pid']
            def ready():
                try:
                    info = self.client.info('server')
                    if info.get('process_id') != self.pid or info.get('tcp_port') != self.spec['port']:
                        return False
                    self.redis_run_id = info['run_id']
                    return bool(re.fullmatch(r'[a-f0-9]{40}', self.redis_run_id))
                except Exception:
                    return False
            wait_until(ready, timeout=8, code='wsl_restarted_endpoint_not_ready')
            self.verify_endpoint(self.client)
            self.stopped = False

    def restore(self):
        if self.paused:
            self.action('unpause')
        if self.stopped:
            self.action('start')


class Harness:
    def __init__(self, args, output):
        self.args, self.output = args, output
        self.namespace = 'cacheverify_' + uuid4().hex
        self.rows, self.backends = [], []
        self.completed = False
        self.controller = None
        self.settings = InfrastructureSettings(
            redis_url=args.redis_url, namespace=self.namespace, cache_ttl=120,
            cache_ttl_jitter_ratio=.1, singleflight_enabled=True,
            singleflight_wait_seconds=65, singleflight_max_keys=4096,
            socket_timeout=.15, cooldown=.2)
        self.control = build_redis(self.settings)
        self.client = self.control.client

    def cache(self, name, provider, *, settings=None, backend=None):
        if backend is None:
            backend = build_redis(settings or self.settings)
            self.backends.append(backend)
        observed = ObservedBackend(backend)
        return CachedEmbeddingClient(provider, observed, 'synthetic-v1', 4, scope=name), observed

    def case(self, name, invoke):
        started = time.monotonic()
        try:
            details = invoke() or {}
            self.rows.append({'case': name, 'status': 'passed', 'details': details,
                              'duration_seconds': round(time.monotonic() - started, 3)})
        except Exception as error:
            code = str(error) if isinstance(error, VerificationFailure) else 'unexpected_exception'
            self.rows.append({'case': name, 'status': 'failed', 'error_type': type(error).__name__,
                              'error_code': code,
                              'duration_seconds': round(time.monotonic() - started, 3)})
        # A result survives subsequent failures or interruption; never replace earlier rows.
        self.write_report()

    def unverified(self, name, reason):
        self.rows.append({'case': name, 'status': 'unverified', 'reason': reason})
        self.write_report()

    def connection(self):
        require(self.client.ping() is True, 'real_redis_ping_failed')
        info = self.client.info('server')
        version = str(info.get('redis_version', ''))
        require(bool(re.fullmatch(r'[0-9.]+', version)), 'real_redis_version_missing')
        return {'service': 'real_redis', 'redis_version': version,
                'model_provider': 'synthetic_counted', 'real_model_used': False}

    def burst(self, cache, backend, provider, batches, *, expected_calls, invoke=None, while_held=None):
        initial_calls, initial_threads = provider.calls, set(backend.get_threads)
        barrier = Barrier(len(batches) + 1)
        provider.gate.clear()
        def run(index, texts):
            barrier.wait(timeout=6)
            return invoke(index, texts) if invoke else cache.embed(texts)
        with ThreadPoolExecutor(max_workers=len(batches)) as pool:
            futures = [pool.submit(run, i, texts) for i, texts in enumerate(batches)]
            try:
                barrier.wait(timeout=6)
                wait_until(lambda: len(backend.get_threads - initial_threads) >= len(batches),
                           code='all_concurrent_gets_not_observed')
                wait_until(lambda: provider.calls >= initial_calls + expected_calls,
                           code='expected_dispatches_not_observed')
                # All GETs completed while the owner is held; followers have time to register.
                time.sleep(.1)
                require(provider.calls == initial_calls + expected_calls, 'dispatch_count_mismatch')
                if while_held is not None:
                    while_held()
            finally:
                provider.gate.set()
            return [future.result(timeout=8) for future in futures]

    def hot_key(self, enabled):
        provider = CountedProvider()
        cache, backend = self.cache('hot_' + str(enabled), provider,
                                    settings=replace(self.settings, singleflight_enabled=enabled))
        n = self.args.requests
        results = self.burst(cache, backend, provider, [['synthetic-hot']] * n,
                             expected_calls=1 if enabled else n)
        require(all(result == [vector('synthetic-hot')] for result in results), 'hot_vectors_invalid')
        require(len({id(result[0]) for result in results}) == n, 'returned_vectors_share_lists')
        require(cache.inflight_count == 0, 'hot_inflight_leak')
        require(self.client.get(cache.key('synthetic-hot')) is not None, 'hot_writeback_missing')
        before = provider.calls
        cache.embed(['synthetic-hot'])
        require(provider.calls == before, 'hot_second_call_not_hit')
        return {'requests': n, 'provider_dispatches': provider.calls,
                'singleflight_enabled': enabled, 'provider_peak': provider.peak,
                'cache_hit_after_write': True, 'inflight_after': cache.inflight_count}

    def expiry(self):
        provider = CountedProvider()
        cache, backend = self.cache('expiry', provider, settings=replace(
            self.settings, cache_ttl=1, cache_ttl_jitter_ratio=0))
        cache.embed(['synthetic-expiry'])
        require(self.client.pttl(cache.key('synthetic-expiry')) > 0, 'expiry_seed_missing')
        wait_until(lambda: self.client.pttl(cache.key('synthetic-expiry')) == -2,
                   timeout=4, code='real_key_did_not_expire')
        self.burst(cache, backend, provider, [['synthetic-expiry']] * self.args.requests,
                   expected_calls=1)
        require(provider.calls == 2 and cache.inflight_count == 0, 'expiry_rebuild_not_single')
        return {'expired_on_real_server': True, 'requests_after_expiry': self.args.requests,
                'initial_dispatches': 1, 'rebuild_dispatches': provider.calls - 1}

    def overlap(self):
        provider = CountedProvider()
        cache, backend = self.cache('overlap', provider)
        batches = [['synthetic-a', 'synthetic-b'], ['synthetic-b', 'synthetic-c'],
                   ['synthetic-c', 'synthetic-a'], ['synthetic-a', 'synthetic-c', 'synthetic-d']]
        barrier = Barrier(len(batches) + 1)
        provider.gate.clear()
        with ThreadPoolExecutor(max_workers=4) as pool:
            def run(texts):
                barrier.wait(timeout=5)
                return cache.embed(texts)
            futures = [pool.submit(run, batch) for batch in batches]
            try:
                barrier.wait(timeout=5)
                wait_until(lambda: len(backend.get_threads) == 4, code='overlap_gets_incomplete')
                wait_until(lambda: sum(provider.texts.values()) == 4, code='overlap_owners_incomplete')
            finally:
                provider.gate.set()
            results = [future.result(timeout=8) for future in futures]
        require(all(results[i] == [vector(t) for t in batch] for i, batch in enumerate(batches)),
                'overlap_order_invalid')
        require(set(provider.texts.values()) == {1}, 'overlap_text_recomputed')
        require(cache.inflight_count == 0, 'overlap_inflight_leak')
        return {'batches': 4, 'unique_texts': 4, 'computed_texts': sum(provider.texts.values()),
                'provider_dispatches': provider.calls, 'completed_without_deadlock': True}

    def follower_timeout(self):
        provider = CountedProvider()
        cache, backend = self.cache('timeout', provider, settings=replace(
            self.settings, singleflight_wait_seconds=.2))
        provider.gate.clear()
        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(cache.embed, ['synthetic-timeout'])
            try:
                wait_until(lambda: provider.calls == 1)
                follower = pool.submit(cache.embed, ['synthetic-timeout'])
                try:
                    follower.result(timeout=3)
                except RuntimeError as error:
                    require(str(error) == 'embedding_singleflight_timeout', 'follower_wrong_timeout_error')
                    timeout_type = type(error).__name__
                else:
                    raise VerificationFailure('follower_timeout_missing')
                require(provider.calls == 1 and cache.inflight_count == 1,
                        'timeout_transferred_ownership')
            finally:
                provider.gate.set()
            leader.result(timeout=5)
        cache.embed(['synthetic-timeout'])
        require(provider.calls == 1 and cache.inflight_count == 0, 'timeout_owner_not_preserved')
        return {'follower_error_type': timeout_type, 'follower_code': 'embedding_singleflight_timeout',
                'provider_dispatches': 1,
                'leader_completed': True, 'subsequent_cache_hit': True}

    def follower_cancel(self):
        provider, token = CountedProvider(), CancellationToken()
        cache, backend = self.cache('cancel', provider)
        provider.gate.clear()
        def follow():
            marker = current_token.set(token)
            try:
                return cache.embed(['synthetic-cancel'])
            finally:
                current_token.reset(marker)
        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(cache.embed, ['synthetic-cancel'])
            try:
                wait_until(lambda: provider.calls == 1)
                follower = pool.submit(follow)
                wait_until(lambda: len(backend.get_threads) == 2)
                token.cancel()
                try:
                    follower.result(timeout=3)
                except ResearchCancelled:
                    pass
                else:
                    raise VerificationFailure('follower_cancellation_missing')
                require(provider.calls == 1 and cache.inflight_count == 1,
                        'cancel_transferred_ownership')
            finally:
                provider.gate.set()
            leader.result(timeout=5)
        require(cache.inflight_count == 0, 'cancel_inflight_leak')
        return {'follower': 'ResearchCancelled', 'provider_dispatches': 1, 'leader_completed': True}

    def ttl_distribution(self):
        rows = []
        for base in (60, 120):
            provider = CountedProvider()
            cache, _ = self.cache('ttl_' + str(base), provider, settings=replace(
                self.settings, cache_ttl=base, cache_ttl_jitter_ratio=.2))
            texts = ['synthetic-ttl-' + str(i) for i in range(128)]
            started = time.monotonic()
            cache.embed(texts)
            pipe = self.client.pipeline(transaction=False)
            for text in texts:
                pipe.pttl(cache.key(text))
            ttls = pipe.execute()
            elapsed_ms = math.ceil((time.monotonic() - started) * 1000)
            lower = max(1, base - math.floor(base * .2))
            require(all(lower * 1000 - elapsed_ms - 250 <= ttl <= base * 1000 for ttl in ttls),
                    'real_ttl_outside_downward_bounds')
            bins = Counter(math.ceil(ttl / 1000) for ttl in ttls)
            require(len(bins) >= 2, 'ttl_jitter_distribution_degenerate')
            rows.append({'base_seconds': base, 'ratio': .2, 'keys': len(ttls),
                         'expected_min_seconds': lower, 'actual_min_ms': min(ttls),
                         'actual_max_ms': max(ttls), 'unique_second_buckets': len(bins),
                         'actual_second_bucket_counts': {str(k): v for k, v in sorted(bins.items())}})
        return {'real_server_pttl': rows}

    def corrupt_cache(self):
        provider = CountedProvider()
        cache, _ = self.cache('corrupt', provider)
        self.client.set(cache.key('synthetic-corrupt-json'), '{', ex=60)
        self.client.set(cache.key('synthetic-corrupt-vector'), '[2,0,0,0]', ex=60)
        texts = ['synthetic-corrupt-json', 'synthetic-corrupt-vector']
        result = cache.embed(texts)
        require(result == [vector(text) for text in texts], 'corrupt_not_recomputed')
        require(all(cache.valid(json.loads(self.client.get(cache.key(t)))) for t in texts),
                'corrupt_not_repaired_on_server')
        require(provider.calls == 1, 'corrupt_unexpected_dispatches')
        return {'corrupt_entries': 2, 'provider_dispatches': 1, 'repaired_on_real_server': True}

    def governance(self, name, *, quota=1000, limit=1):
        root = self.output / name
        root.mkdir()
        settings = GovernanceSettings(user_daily_units=quota, workspace_daily_units=quota,
                                      global_provider=limit, workspace_provider=limit, user_provider=limit)
        return Governance(GovernanceStore(root / 'cost.sqlite3'), settings), root

    def outage(self, operation):
        require(self.controller is not None, 'owned_fault_facility_required')
        governance, _ = self.governance('outage_' + operation, quota=7)
        provider = CountedProvider(governed=governance)
        cache, backend = self.cache('outage_' + operation, provider)
        def invoke(index, texts):
            with usage_context(governance, user='synthetic-user', workspace='synthetic-workspace'):
                return cache.embed(texts)
        def admission_probe():
            try:
                invoke(0, ['synthetic-concurrency-probe'])
            except GovernanceDenied as error:
                require(error.code == 'capacity_global_provider', 'outage_wrong_capacity_error')
            else:
                raise VerificationFailure('outage_concurrency_not_enforced')
        restored = 'unpause' if operation == 'pause' else 'start'
        try:
            self.controller.action(operation)
            # A fresh backend actually attempts the unreachable server before bypassing.
            self.burst(cache, backend, provider, [['synthetic-outage']] * self.args.requests,
                       expected_calls=1, invoke=invoke, while_held=admission_probe)
            require(backend.backend.status == 'unavailable', 'outage_not_seen_by_backend')
            require(provider.calls == 1, 'outage_singleflight_failed')
            require(governance.store.usage('user', 'synthetic-user', 7)['used_or_held'] == 3,
                    'outage_cost_not_settled')
            # Cache misses cannot bypass the persistent daily quota.
            try:
                invoke(0, ['synthetic-quota-probe'])
            except GovernanceDenied as error:
                require(error.code == 'user_quota_exceeded', 'outage_wrong_quota_error')
            else:
                raise VerificationFailure('outage_quota_not_enforced')
            require(provider.calls == 1, 'outage_quota_dispatched_provider')
            require(governance.peaks[('provider', 'global', 'all')] <= 1,
                    'outage_provider_concurrency_exceeded')
        finally:
            self.controller.action(restored)
        wait_until(lambda: self._ping(), timeout=8, code='redis_not_recovered')
        time.sleep(self.settings.cooldown + .05)
        # Reuse the SAME cache and backend after its cooldown. A second synthetic
        # identity has fresh quota; the outage identity's settled cost stays intact.
        with usage_context(governance, user='synthetic-recovery-user',
                           workspace='synthetic-recovery-workspace'):
            cache.embed(['synthetic-outage'])
            cache.embed(['synthetic-outage'])
        require(provider.calls == 2, 'recovery_cache_hit_missing')
        require(self.client.exists(cache.key('synthetic-outage')) == 1,
                'recovery_writeback_missing')
        return {'fault': operation, 'requests': self.args.requests, 'provider_dispatches': 1,
                'governance_provider_peak': governance.peaks[('provider', 'global', 'all')],
                'reported_synthetic_tokens': 3, 'used_or_held_units': 3,
                'quota_rejection_before_dispatch': True,
                'concurrency_rejection_before_dispatch': True,
                'same_backend_recovery_dispatches': provider.calls - 1,
                'server_recovered': True, 'writeback_and_hit_after_recovery': True}

    def _ping(self):
        try:
            return self.client.ping() is True
        except Exception:
            return False

    def write_denied(self):
        require(self.controller is not None, 'owned_fault_facility_required')
        self.controller.verify_endpoint(self.client)
        username, password = 'cacheverify_' + uuid4().hex, secrets.token_hex(24)
        restricted = None
        try:
            # ACL is the only server mutation besides this namespace's disposable keys.
            # It is allowed only after validating the isolated service's ownership.
            self.client.execute_command('ACL', 'SETUSER', username, 'reset', 'on',
                '>' + password, '~' + self.namespace + ':*', '+get', '+ping', '+pttl', '+select')
            from redis import ConnectionPool, Redis
            from redis.exceptions import ResponseError
            kwargs = dict(self.client.connection_pool.connection_kwargs)
            kwargs.update(username=username, password=password)
            restricted = Redis(connection_pool=ConnectionPool(**kwargs))
            require(restricted.ping() is True, 'restricted_redis_ping_failed')
            key = self.namespace + ':acl-sentinel'
            self.client.set(key, 'synthetic', ex=60)
            require(restricted.get(key) == 'synthetic', 'restricted_real_get_failed')
            try:
                restricted.set(key, 'synthetic', ex=60)
            except ResponseError as error:
                rejection_type = type(error).__name__
            else:
                raise VerificationFailure('real_server_write_not_denied')
            settings = replace(self.settings, namespace=self.namespace)
            backend = RedisBackend(restricted, settings)
            self.backends.append(backend)
            provider = CountedProvider()
            cache, _ = self.cache('write-denied', provider, backend=backend)
            result = cache.embed(['synthetic-write-denied'])
            require(result == [vector('synthetic-write-denied')], 'write_denial_did_not_bypass')
            require(backend.status == 'unavailable', 'write_denial_not_observed')
            require(self.client.get(cache.key('synthetic-write-denied')) is None,
                    'write_denial_unexpected_cache_entry')
            return {'real_redis_server_error': rejection_type,
                    'restriction': 'temporary_acl_user_set_denied', 'provider_dispatches': provider.calls,
                    'valid_result_returned': True, 'cache_write_rejected': True}
        finally:
            if restricted is not None:
                restricted.close()
            self.controller.verify()
            self.client.execute_command('ACL', 'DELUSER', username)

    def paid_unknown(self):
        governance, root = self.governance('paid_unknown', quota=1000)
        provider = CountedProvider(governed=governance, fail=True,
                                   effect_path=root / 'provider.sqlite3')
        cache, backend = self.cache('paid_unknown', provider)
        ledger = SQLiteToolLedger(root / 'tool.sqlite3')
        run_id = 'synthetic-paid-run'
        ledger.begin_run(run_id)
        executor = ToolExecutor(ledger, run_id)
        n = self.args.requests
        def invoke(index, texts):
            with usage_context(governance, user='synthetic-user', workspace='synthetic-workspace',
                               run_id=run_id):
                try:
                    executor.execute('synthetic-slot-' + str(index), 'synthetic-paid-embedding',
                        ToolKind.PAID, {'synthetic': True}, lambda ident: cache.embed(texts))
                except ToolExecutionBlocked as error:
                    require(error.code == 'tool_outcome_unknown', 'paid_wrong_block_code')
                    expected_id = ledger.prepare(run_id, 'synthetic-slot-' + str(index),
                        'synthetic-paid-embedding', ToolKind.PAID, {'synthetic': True}, '')
                    require(error.operation_id == expected_id, 'follower_operation_id_crossed')
                    return 'unknown'
            raise VerificationFailure('paid_failure_was_not_blocked')
        outcomes = self.burst(cache, backend, provider, [['synthetic-paid-failure']] * n,
                              expected_calls=1, invoke=invoke)
        require(outcomes == ['unknown'] * n and provider.calls == 1, 'paid_failure_fanout_invalid')
        rows = ledger.list_run(run_id)
        require(len(rows) == n and all(row['status'] == 'unknown' and row['attempt'] == 1 for row in rows),
                'paid_ledger_not_unknown_once')
        costs = governance.store.unsettled()
        require(len(costs) == 1 and costs[0]['actual'] is None and costs[0]['reserved'] == 7,
                'paid_unknown_cost_reservation_lost')
        require(costs[0]['operation_id'] in {row['id'] for row in rows}, 'paid_cost_operation_unlinked')
        require(cache.inflight_count == 0, 'paid_failure_inflight_leak')
        # Reopen these durable stores in a fresh process and retry the exact original
        # PAID run/slots. The worker dispatch callback records any forbidden resend.
        state = {'run_id': run_id, 'slots': n}
        (root / 'recovery_state.json').write_text(json.dumps(state), encoding='utf-8')
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--internal-recovery', str(root)]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=25)
        require(result.returncode == 0, 'paid_recovery_process_failed')
        resumed = json.loads(result.stdout)
        require(resumed['blocked_original_slots'] == n and resumed['provider_effects'] == 1,
                'paid_recovery_redispatched')
        require(resumed['unknown_costs'] == 1 and resumed['used_or_held_units'] == 7,
                'paid_recovery_unknown_cost_lost')
        return {'requests': n, 'provider_dispatches': provider.calls, 'unknown_tool_slots': n,
                'actual_tokens': None, 'unknown_cost_reservations': 1, 'held_units': 7,
                'original_paid_slots_blocked_after_process_reopen': resumed['blocked_original_slots'],
                'durable_provider_effects_after_recovery': resumed['provider_effects'],
                'operation_ids_isolated': True}

    def source_hashes(self):
        root = Path(__file__).resolve().parents[1]
        paths = [Path(__file__).resolve(), root / 'src/deepsearch_agent/adapters/infrastructure.py',
                 root / 'src/deepsearch_agent/application/governance.py',
                 root / 'src/deepsearch_agent/application/tool_execution.py',
                 root / 'src/deepsearch_agent/adapters/persistence/governance.py',
                 root / 'src/deepsearch_agent/adapters/persistence/tool_ledger.py']
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in paths}

    def write_report(self):
        counts = Counter(row['status'] for row in self.rows)
        report = {'schema_version': 1, 'kind': 'real_redis_synthetic_cache_protection',
                  'run_id': self.args.run_id, 'inputs': 'fixed_synthetic_only',
                  'real_model_used': False, 'namespace_is_random': True,
                  'service_faults_requested': bool(self.args.docker_container or getattr(self.args, 'wsl_control', None)),
                  'facility': getattr(getattr(self, 'controller', None), 'facility', 'unavailable'),
                  'source_sha256': self.source_hashes(), 'cases': self.rows,
                  'summary': {'passed': counts['passed'], 'failed': counts['failed'],
                              'unverified': counts['unverified'],
                              'all_required_verified': bool(getattr(self, 'completed', False))
                                                       and bool(self.rows) and not counts['failed']
                                                       and not counts['unverified']}}
        temporary = self.output / 'report.pending.json'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.output / 'report.json')

    def run(self):
        self.case('real_redis_connection', self.connection)
        self.case('hot_key_singleflight_off', lambda: self.hot_key(False))
        self.case('hot_key_singleflight_on', lambda: self.hot_key(True))
        self.case('real_expiry_hot_key_rebuild', self.expiry)
        self.case('overlapping_batches', self.overlap)
        self.case('follower_timeout_preserves_owner', self.follower_timeout)
        self.case('follower_cancel_preserves_owner', self.follower_cancel)
        self.case('real_ttl_jitter_distribution', self.ttl_distribution)
        self.case('corrupt_cache_recompute', self.corrupt_cache)
        self.case('paid_unknown_and_fresh_process_recovery', self.paid_unknown)
        if self.args.docker_container:
            self.case('isolated_container_ownership', self._owned_docker)
        elif self.args.wsl_control:
            self.case('isolated_wsl_process_ownership', self._owned_wsl)
        if self.controller is not None:
            self.case('real_redis_pause_and_recovery', lambda: self.outage('pause'))
            self.case('real_redis_stop_and_recovery', lambda: self.outage('stop'))
            self.case('real_redis_server_write_denied', self.write_denied)
        else:
            for name in ('real_redis_pause_and_recovery', 'real_redis_stop_and_recovery',
                         'real_redis_server_write_denied'):
                self.unverified(name, 'isolated_owned_fault_facility_not_available')

    def _owned_docker(self):
        docker = OwnedDocker(self.args.docker_container, self.args.docker_host, self.args.run_id)
        docker.verify_endpoint(self.client)
        self.controller = docker
        return {'ownership_label_checked': True}

    def _owned_wsl(self):
        self.controller = OwnedWSL(self.args.wsl_control, self.args.run_id,
                                   self.args.redis_url, self.client)
        return {'process_identity_checked': True, 'endpoint_identity_checked': True,
                'facility': self.controller.facility}

    def close(self):
        if self.controller is not None:
            try:
                self.controller.restore()
            except Exception as error:
                self.rows.append({'case': 'owned_service_restore', 'status': 'failed',
                                  'error_code': 'owned_service_restore_failed',
                                  'error_type': type(error).__name__})
        for backend in self.backends:
            backend.close()
        try:
            removed = cleanup_namespace(self.client, self.namespace)
            self.rows.append({'case': 'namespace_cleanup', 'status': 'passed',
                              'details': {'keys_removed': removed, 'flush_used': False}})
        except Exception as error:
            self.rows.append({'case': 'namespace_cleanup', 'status': 'failed',
                              'error_code': 'namespace_cleanup_failed',
                              'error_type': type(error).__name__})
        self.control.close()
        self.write_report()


def recover_worker(root):
    """Private subprocess: the same PAID logical slots must remain blocked."""
    import sqlite3
    root = Path(root)
    state = json.loads((root / 'recovery_state.json').read_text(encoding='utf-8'))
    store = GovernanceStore(root / 'cost.sqlite3')
    governance = Governance(store, GovernanceSettings(user_daily_units=1000, workspace_daily_units=1000))
    ledger = SQLiteToolLedger(root / 'tool.sqlite3')
    ledger.recover_interrupted(state['run_id'])
    executor = ToolExecutor(ledger, state['run_id'])
    def forbidden_dispatch(ident):
        with sqlite3.connect(root / 'provider.sqlite3') as db:
            db.execute('INSERT INTO effects DEFAULT VALUES')
        raise VerificationFailure('paid_recovery_forbidden_dispatch')
    blocked = 0
    for index in range(state['slots']):
        with usage_context(governance, user='synthetic-user', workspace='synthetic-workspace',
                           run_id=state['run_id']):
            try:
                executor.execute('synthetic-slot-' + str(index), 'synthetic-paid-embedding',
                    ToolKind.PAID, {'synthetic': True}, forbidden_dispatch)
            except ToolExecutionBlocked as error:
                require(error.code == 'tool_outcome_unknown', 'recovery_wrong_block_code')
                blocked += 1
    with sqlite3.connect(root / 'provider.sqlite3') as db:
        effects = db.execute('SELECT count(*) FROM effects').fetchone()[0]
    print(json.dumps({'blocked_original_slots': blocked, 'provider_effects': effects,
                      'unknown_costs': len(store.unsettled()),
                      'used_or_held_units': store.usage('user', 'synthetic-user', 1000)['used_or_held']}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--redis-url', default=os.environ.get('TEST_REDIS_URL', ''), help='Prefer TEST_REDIS_URL to keep credentials out of shell history')
    parser.add_argument('--requests', type=int, choices=(16, 32), default=16)
    parser.add_argument('--run-id', default=uuid4().hex, help='Must equal the isolated container deepsearch.cache-test label')
    parser.add_argument('--docker-container')
    parser.add_argument('--docker-host', default=os.environ.get('DOCKER_HOST', ''))
    parser.add_argument('--wsl-control', type=Path, help='Absolute path to owned Redis process JSON under workspace/tmp')
    parser.add_argument('--internal-recovery', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # Redis adapter warnings use fixed codes, but the report is the runner's only public log.
    logging.disable(logging.CRITICAL)
    if args.internal_recovery is not None:
        recover_worker(args.internal_recovery)
        return 0
    if args.output is None:
        parser.error('--output is required')
    if not args.redis_url:
        parser.error('--redis-url or TEST_REDIS_URL is required')
    if args.docker_container and args.wsl_control:
        parser.error('--docker-container and --wsl-control are mutually exclusive')
    if not re.fullmatch(r'[a-fA-F0-9]{16,64}', args.run_id):
        parser.error('--run-id must contain 16-64 hexadecimal characters')
    try:
        output = create_output(args.output)
    except OSError as error:
        print(json.dumps({'status': 'failed', 'error_code': 'output_must_be_new_directory',
                          'error_type': type(error).__name__}))
        return 2
    harness = None
    try:
        harness = Harness(args, output)
        frozen = harness.source_hashes()
        harness.run()
        harness.case('source_freeze', lambda: require(harness.source_hashes() == frozen, 'source_changed_during_run'))
        harness.completed = True
    except BaseException as error:
        report = {'schema_version': 1, 'kind': 'real_redis_synthetic_cache_protection',
                  'cases': [{'case': 'runner_initialization' if harness is None else 'runner_execution',
                             'status': 'failed',
                             'error_code': 'runner_interrupted' if isinstance(error, KeyboardInterrupt)
                                           else 'runner_execution_failed',
                             'error_type': type(error).__name__}],
                  'summary': {'passed': 0, 'failed': 1, 'unverified': 0, 'all_required_verified': False}}
        if harness is None:
            (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        else:
            harness.rows.extend(report['cases'])
    finally:
        if harness is not None:
            harness.close()
    report = json.loads((output / 'report.json').read_text(encoding='utf-8'))
    print(json.dumps({'report': 'report.json', **report['summary']}))
    return 1 if report['summary']['failed'] else 0 if report['summary']['all_required_verified'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
