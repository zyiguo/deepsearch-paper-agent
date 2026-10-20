"""Destructive-runner boundaries; these do not claim real Redis acceptance."""
import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    'cache_protection_runner', Path(__file__).resolve().parents[1] / 'scripts/verify_cache_protection.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_output_refuses_existing_directory_and_preserves_previous_evidence(tmp_path):
    output = tmp_path / 'run'
    runner.create_output(output)
    evidence = output / 'report.json'
    evidence.write_text('previous-evidence', encoding='utf-8')
    with pytest.raises(FileExistsError):
        runner.create_output(output)
    assert evidence.read_text(encoding='utf-8') == 'previous-evidence'


def test_cleanup_uses_only_exact_random_namespace_and_never_flushes():
    namespace = 'cacheverify_' + 'a' * 32
    class Client:
        def __init__(self):
            self.deleted = []
        def scan_iter(self, *, match, count):
            assert match == namespace + ':*'
            return iter([namespace + ':embedding:v1:synthetic'])
        def delete(self, key):
            self.deleted.append(key)
            return 1
    client = Client()
    assert runner.cleanup_namespace(client, namespace) == 1
    assert client.deleted == [namespace + ':embedding:v1:synthetic']


def test_cleanup_rejects_unowned_key_even_if_server_scan_returns_it():
    class Client:
        def scan_iter(self, **kwargs):
            return iter(['other-user:data'])
        def delete(self, key):
            pytest.fail('must never delete a key outside this run namespace')
    with pytest.raises(runner.VerificationFailure, match='cleanup_key_outside_namespace'):
        runner.cleanup_namespace(Client(), 'cacheverify_' + 'a' * 32)


@pytest.mark.parametrize('namespace', ['deepsearch', '*', 'cacheverify_' + 'a' * 31, 'cacheverify_../'])
def test_cleanup_rejects_nonrandom_or_glob_namespaces(namespace):
    with pytest.raises(runner.VerificationFailure, match='cleanup_namespace_invalid'):
        runner.cleanup_namespace(object(), namespace)


def test_docker_refuses_mutation_when_ownership_label_does_not_match(monkeypatch):
    calls = []
    def command(self, args, timeout=20):
        calls.append(args[0])
        return '[{"Config":{"Labels":{"deepsearch.cache-test":"another-run"}}}]'
    monkeypatch.setattr(runner.OwnedDocker, 'command', command)
    with pytest.raises(runner.VerificationFailure, match='docker_ownership_label_mismatch'):
        runner.OwnedDocker('synthetic-redis', '', 'a' * 32)
    assert calls == ['inspect']


def test_owned_container_label_cannot_authorize_an_unrelated_redis_endpoint(monkeypatch):
    calls = []
    def command(self, args, timeout=20):
        calls.append(args[0])
        if args[0] == 'inspect':
            return '[{"Config":{"Labels":{"deepsearch.cache-test":"' + 'a' * 32 + '"}}}]'
        assert args == ['exec', 'synthetic-redis', 'redis-cli', '--raw', 'INFO', 'server']
        return '# Server\r\nrun_id:owned-server\r\n'
    monkeypatch.setattr(runner.OwnedDocker, 'command', command)
    docker = runner.OwnedDocker('synthetic-redis', '', 'a' * 32)
    client = type('Client', (), {'info': lambda self, section: {'run_id': 'unrelated-server'}})()
    with pytest.raises(runner.VerificationFailure, match='redis_endpoint_not_owned_container'):
        docker.verify_endpoint(client)
    assert calls == ['inspect', 'inspect', 'exec']


def test_case_report_preserves_failure_and_omits_exception_message(tmp_path, monkeypatch):
    harness = object.__new__(runner.Harness)
    harness.output, harness.rows = tmp_path, []
    harness.args = type('Args', (), {'run_id': 'a' * 32, 'docker_container': None})()
    monkeypatch.setattr(harness, 'source_hashes', lambda: {})
    def fail():
        raise RuntimeError('redis://sensitive-user:sensitive-password@example.invalid')
    harness.case('failure', fail)
    harness.case('later-success', lambda: {'synthetic_count': 1})
    data = (tmp_path / 'report.json').read_text(encoding='utf-8')
    assert 'sensitive-password' not in data and 'example.invalid' not in data
    assert 'RuntimeError' in data
    assert [row['status'] for row in harness.rows] == ['failed', 'passed']


def test_partial_run_cannot_report_complete_acceptance(tmp_path, monkeypatch):
    import json
    harness = object.__new__(runner.Harness)
    harness.output, harness.rows = tmp_path, []
    harness.args = type('Args', (), {'run_id': 'a' * 32, 'docker_container': None})()
    monkeypatch.setattr(harness, 'source_hashes', lambda: {})
    harness.case('one-passing-case-before-interruption', lambda: {})
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert report['summary']['passed'] == 1
    assert report['summary']['all_required_verified'] is False


def wsl_spec():
    root = '/mnt/c/Users/root/Desktop/deepsearch-paper-agent/tmp'
    return root, {
        'distro': 'Ubuntu-24.04', 'run_id': 'a' * 32,
        'binary': root + '/redis-source/src/redis-server',
        'config': root + '/redis-runtime/redis.conf',
        'pid_file': root + '/redis-runtime/redis.pid',
        'port': 16389, 'redis_run_id': 'b' * 40,
    }


def test_wsl_spec_accepts_only_owned_normalized_workspace_paths():
    root, spec = wsl_spec()
    assert runner.validate_wsl_spec(spec, 'a' * 32, 'redis://127.0.0.1:16389/0', root) == spec


@pytest.mark.parametrize('field,value,code', [
    ('binary', '/usr/bin/redis-server', 'wsl_path_outside_workspace_tmp'),
    ('config', '/mnt/c/Users/root/Desktop/deepsearch-paper-agent/tmp/../private.conf', 'wsl_path_not_canonical'),
    ('pid_file', 'relative.pid', 'wsl_path_not_canonical'),
    ('run_id', 'c' * 32, 'wsl_ownership_run_id_mismatch'),
    ('port', 16390, 'wsl_redis_url_port_mismatch'),
    ('distro', 'unrelated-distro', 'wsl_distribution_not_allowed'),
    ('redis_run_id', 'not-a-server-run-id', 'wsl_server_run_id_invalid'),
])
def test_wsl_spec_refuses_unowned_or_ambiguous_control(field, value, code):
    root, spec = wsl_spec()
    spec[field] = value
    with pytest.raises(runner.VerificationFailure, match=code):
        runner.validate_wsl_spec(spec, 'a' * 32, 'redis://127.0.0.1:16389/0', root)


def test_wsl_endpoint_must_match_both_redis_run_id_and_owned_pid(monkeypatch):
    owned = object.__new__(runner.OwnedWSL)
    _, owned.spec = wsl_spec()
    owned.pid, owned.redis_run_id = 12345, 'b' * 40
    commands = []
    def command(operation):
        commands.append(operation)
        return {'ok': True, 'pid': 12345}
    monkeypatch.setattr(owned, 'command', command)
    client = type('Client', (), {'info': lambda self, section:
        {'run_id': 'b' * 40, 'process_id': 54321, 'tcp_port': 16389}})()
    with pytest.raises(runner.VerificationFailure, match='redis_endpoint_not_owned_wsl_process'):
        owned.verify_endpoint(client)
    assert commands == ['verify']


def test_wsl_control_rejects_pid_change_before_sending_a_signal(monkeypatch):
    owned = object.__new__(runner.OwnedWSL)
    owned.pid, owned.paused, owned.stopped = 12345, False, False
    commands = []
    def command(operation):
        commands.append(operation)
        return {'ok': True, 'pid': 54321}
    monkeypatch.setattr(owned, 'command', command)
    with pytest.raises(runner.VerificationFailure, match='wsl_owned_pid_changed'):
        owned.action('pause')
    assert commands == ['verify']


def test_wsl_start_without_owned_stop_never_calls_wsl(monkeypatch):
    owned = object.__new__(runner.OwnedWSL)
    owned.pid, owned.paused, owned.stopped = 12345, False, False
    monkeypatch.setattr(owned, 'command', lambda operation: pytest.fail('no WSL command authorized'))
    with pytest.raises(runner.VerificationFailure, match='wsl_start_without_owned_stop'):
        owned.action('start')


def test_wsl_report_labels_process_facility_and_never_claims_container_acceptance(tmp_path, monkeypatch):
    import json
    harness = object.__new__(runner.Harness)
    harness.output, harness.rows = tmp_path, []
    harness.args = type('Args', (), {'run_id': 'a' * 32, 'docker_container': None,
                                   'wsl_control': Path('/private/control.json')})()
    harness.controller = type('Controller', (), {'facility': 'wsl-owned-process'})()
    monkeypatch.setattr(harness, 'source_hashes', lambda: {})
    harness.case('isolated_wsl_process_ownership', lambda: {})
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert report['facility'] == 'wsl-owned-process'
    assert report['service_faults_requested'] is True
    assert '/private/control.json' not in json.dumps(report)


def test_wsl_command_keeps_mixed_encoding_stderr_opaque(monkeypatch, capsys):
    from types import SimpleNamespace
    owned = object.__new__(runner.OwnedWSL)
    owned.root, owned.spec = wsl_spec()
    owned.pid = 12345
    def run(command, **kwargs):
        assert command[:5] == ['wsl.exe', '--distribution', 'Ubuntu-24.04', '--exec', 'python3']
        assert kwargs == {'capture_output': True, 'text': False, 'timeout': 25}
        return SimpleNamespace(returncode=0, stdout=b'{"ok": true, "pid": 12345}',
            stderr='WSL\u8b66\u544a'.encode('utf-16') + b'\xff\xfeprivate-warning')
    monkeypatch.setattr(runner.subprocess, 'run', run)
    assert owned.command('verify') == {'ok': True, 'pid': 12345}
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


@pytest.mark.parametrize('returncode,stdout,code', [
    (1, b'{"ok": true, "pid": 12345}', 'owned_wsl_control_failed'),
    (1, b'{"ok": false, "code": "wsl_owned_pid_changed"}', 'wsl_owned_pid_changed'),
    (0, b'\xff\xfe{"ok":true}', 'owned_wsl_control_invalid_response'),
    (0, b'[]', 'owned_wsl_control_invalid_response'),
    (0, '{"ok": true, "pid": 12345}', 'owned_wsl_control_invalid_response'),
])
def test_wsl_command_rejects_failed_exit_or_invalid_stdout_protocol(monkeypatch, returncode, stdout, code):
    from types import SimpleNamespace
    owned = object.__new__(runner.OwnedWSL)
    owned.root, owned.spec = wsl_spec()
    owned.pid = 12345
    monkeypatch.setattr(runner.subprocess, 'run', lambda *args, **kwargs:
        SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b'\xff\xfeopaque-warning'))
    with pytest.raises(runner.VerificationFailure, match=code):
        owned.command('verify')
