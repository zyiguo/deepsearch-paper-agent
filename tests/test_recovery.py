from pathlib import Path
import json
import sqlite3
import subprocess
import sys

import pytest
from deepsearch_agent.adapters.database_config import DatabaseSettings
from deepsearch_agent.adapters.persistence.deployment_lock import DeploymentLock, DeploymentBusy
from deepsearch_agent.adapters.persistence.recovery import create_bundle, verify_bundle, restore_drill, file_hash
from deepsearch_agent.core.filesystem import native_path


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'business'
    root.mkdir()
    (root / 'original.pdf').write_bytes(b'%PDF-fixture')
    db = sqlite3.connect(root / 'conversations.sqlite3')
    db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,value TEXT)')
    db.execute("INSERT INTO records VALUES(1,'preserve')")
    db.commit()
    db.close()
    return root


def test_file_bundle_restore_and_sqlite_integrity_are_real(source, tmp_path):
    before = {p.name: file_hash(p) for p in source.iterdir()}
    output = tmp_path / 'backup'
    create_bundle(output, {'indexes': source}, DatabaseSettings())
    assert verify_bundle(output)['backend'] == 'sqlite'
    restored = tmp_path / 'restored'
    report = restore_drill(output, restored)
    assert report['status'] == 'passed' and report['sqlite_integrity_checked'] == 1
    assert {p.name: file_hash(p) for p in source.iterdir()} == before
    assert {p.name: file_hash(p) for p in (restored / 'files/indexes').iterdir()} == before
    with pytest.raises(FileExistsError):
        restore_drill(output, restored)
    with pytest.raises(FileExistsError):
        create_bundle(output, {'indexes': source}, DatabaseSettings())


def test_tampered_backup_cannot_create_restore_destination(source, tmp_path):
    output = tmp_path / 'backup'
    create_bundle(output, {'indexes': source}, DatabaseSettings())
    (output / 'files/indexes/original.pdf').write_bytes(b'corrupted')
    with pytest.raises(ValueError):
        restore_drill(output, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'C:/windows/file', 'files/../../escape', 'files\\escape'])
def test_manifest_path_traversal_is_rejected_before_copy(source, tmp_path, path):
    output = tmp_path / 'backup'
    create_bundle(output, {'indexes': source}, DatabaseSettings())
    manifest = json.loads((output / 'manifest.json').read_text())
    manifest['files'][0]['path'] = path
    (output / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        restore_drill(output, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_live_wal_and_nested_destination_refuse_backup(source, tmp_path):
    with pytest.raises(ValueError):
        create_bundle(source / 'backup', {'indexes': source}, DatabaseSettings())
    (source / 'conversations.sqlite3-wal').write_bytes(b'pending transaction')
    with pytest.raises(ValueError):
        create_bundle(tmp_path / 'backup', {'indexes': source}, DatabaseSettings())
    assert not (tmp_path / 'backup/manifest.json').exists()


def test_process_lock_rejects_second_process_and_releases_after_exit(tmp_path):
    path = tmp_path / 'deployment.lock'
    script = ('from deepsearch_agent.adapters.persistence.deployment_lock import DeploymentLock\n'
              f'with DeploymentLock({str(path)!r}): print("acquired")')
    with DeploymentLock(path):
        with pytest.raises(DeploymentBusy):
            with DeploymentLock(path):
                pass
        attempt = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        assert attempt.returncode != 0 and 'acquired' not in attempt.stdout
    attempt = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert attempt.returncode == 0 and 'acquired' in attempt.stdout


def test_deep_workspace_paths_survive_backup_and_restore(source, tmp_path):
    deep = native_path(source / 'workspaces' / ('a' * 32) / 'documents' / ('b' * 64) / ('c' * 64 + '.json'))
    deep.parent.mkdir(parents=True)
    deep.write_text('preserve long workspace asset', encoding='utf-8')
    bundle = tmp_path / 'deep-backup'
    create_bundle(bundle, {'team': source}, DatabaseSettings())
    output = tmp_path / 'deep-restored'
    assert restore_drill(bundle, output)['status'] == 'passed'
    restored = native_path(output / 'files/team' / deep.relative_to(native_path(source)))
    assert restored.read_bytes() == deep.read_bytes()


@pytest.mark.parametrize('wrong_label', [False, True])
def test_failed_restore_preserves_report_and_only_cleans_owned_resource(source, tmp_path, wrong_label):
    class Docker:
        removed = False
        label = None
        def validate_source(self, *args):
            return 'deepsearch'
        def fingerprints(self, *args):
            return {'sessions': {'rows': 1, 'sha256': 'fixture'}}
        def call(self, args, **kwargs):
            if args[0] == 'run':
                self.label = args[args.index('--label') + 1].split('=', 1)[1]
                return b'fixture-container'
            if 'pg_dump' in args:
                return b'PGDMP-fixture'
            if 'pg_isready' in args:
                return b'accepting connections'
            if 'pg_restore' in args:
                raise RuntimeError('injected restore failure')
            if args[0] == 'inspect':
                return b'not-our-resource' if wrong_label else self.label.encode()
            if args[0] == 'rm':
                self.removed = True
                return b''
            raise AssertionError('Unexpected command')
    docker = Docker()
    bundle = tmp_path / 'backup'
    create_bundle(bundle, {'indexes': source}, DatabaseSettings(backend='postgresql', url='postgresql://fixture@127.0.0.1/deepsearch'), docker=docker, container='fixture')
    output = tmp_path / 'restored'
    with pytest.raises(RuntimeError):
        restore_drill(bundle, output, docker=docker)
    report = json.loads((output / 'restore-report.json').read_text())
    assert report['status'] == 'failed'
    assert docker.removed is not wrong_label
    assert report['isolated_resource_cleaned'] is not wrong_label
