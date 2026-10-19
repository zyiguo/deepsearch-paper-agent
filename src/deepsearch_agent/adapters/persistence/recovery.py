"""Offline file bundles and isolated PostgreSQL restore drills; never restore in place."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit, unquote
from uuid import uuid4
from deepsearch_agent.core.filesystem import native_path

VERSION = 1


def file_hash(path):
    value = hashlib.sha256()
    with native_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    with native_path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def inventory(root):
    root = native_path(root)
    if linked(root):
        raise ValueError('Linked backup roots are not supported')
    result = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            if linked(Path(parent) / name):
                raise ValueError('Linked business files/directories are not supported')
        for name in files:
            path = Path(parent) / name
            if path.name == 'deployment.lock':
                continue
            if path.name.endswith(('-wal', '-journal')) and path.stat().st_size:
                raise ValueError('Uncheckpointed SQLite sidecar; stop writers and checkpoint first')
            result.append(path)
    return sorted(result)


def relative_path(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ValueError('Invalid bundle path')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {'.', '..'} for part in value.split('/')):
        raise ValueError('Bundle path escapes its root')
    return Path(*path.parts)


def verify_bundle(bundle):
    bundle = native_path(bundle)
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != VERSION or manifest.get('status') != 'complete':
        raise ValueError('Incomplete or unsupported backup')
    seen = set()
    for row in manifest['files']:
        relative = relative_path(row['path'])
        if row['path'] in seen:
            raise ValueError('Duplicate bundle entry')
        seen.add(row['path'])
        path = bundle / relative
        if not path.is_file() or path.stat().st_size != row['bytes'] or file_hash(path) != row['sha256']:
            raise ValueError('Backup content hash/size mismatch')
    actual = {p.relative_to(bundle).as_posix() for p in inventory(bundle)}
    if actual != seen | {'manifest.json'}:
        raise ValueError('Unexpected or missing backup files')
    return manifest


class DockerPostgres:
    def __init__(self, *, host=None, config=None):
        self.command = ['docker']
        if config:
            self.command += ['--config', str(config)]
        if host:
            self.command += ['--host', host]

    def call(self, args, *, data=None, env=None, timeout=120):
        result = subprocess.run(self.command + list(args), input=data, capture_output=True,
                                env=env, timeout=timeout)
        if result.returncode:
            # Server output can contain SQL, passwords or user content.
            raise RuntimeError('PostgreSQL container operation failed')
        return result.stdout

    def query(self, container, database, statement):
        return self.call(['exec', '-i', container, 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                          '-U', 'postgres', '-d', database], data=statement.encode('utf-8')).decode('utf-8').strip()

    def fingerprints(self, container, database, schema):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', schema):
            raise ValueError('Invalid application schema')
        names = self.query(container, database,
            f"SELECT tablename FROM pg_tables WHERE schemaname='{schema}' ORDER BY tablename").splitlines()
        if not names or any(not re.fullmatch(r'[a-z_][a-z0-9_]*', name) for name in names):
            raise ValueError('Missing or unsupported application tables')
        result = {}
        for name in names:
            content = self.query(container, database, f'SELECT row_to_json(t) FROM "{schema}"."{name}" t')
            rows = [json.dumps(json.loads(line), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
                    for line in content.splitlines() if line]
            result[name] = {'rows': len(rows), 'sha256': hashlib.sha256('\n'.join(sorted(rows)).encode()).hexdigest()}
        return result

    def validate_source(self, container, settings):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container):
            raise ValueError('Select an explicit source container name')
        url = urlsplit(settings.url)
        if url.hostname not in {'localhost', '127.0.0.1'}:
            raise ValueError('This adapter supports an explicitly selected local Docker PostgreSQL only')
        ports = self.call(['port', container, '5432/tcp']).decode().splitlines()
        if f'127.0.0.1:{url.port or 5432}' not in ports:
            raise ValueError('Selected PostgreSQL container does not match the configured database endpoint')
        database = unquote(url.path[1:])
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', database):
            raise ValueError('Unsupported database name')
        return database


def create_bundle(output, roots, settings, *, docker=None, container=None, config_hashes=None):
    """Caller holds DeploymentLock and has stopped all other database/file writers."""
    output = native_path(output)
    roots = {name: native_path(path) for name, path in roots.items()}
    for path in roots.values():
        source = native_path(path)
        if output == source or output.is_relative_to(source):
            raise ValueError('Backup destination must be outside all business roots')
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'version': VERSION, 'status': 'complete', 'backend': settings.backend,
        'schema': settings.schema, 'created_at': datetime.now(timezone.utc).isoformat(),
        'config_sha256': config_hashes or {}, 'files': [], 'tables': None}
    try:
        for name, source in roots.items():
            if not re.fullmatch(r'[a-z][a-z0-9_-]*', name):
                raise ValueError('Invalid logical backup root')
            source = Path(source)
            for path in inventory(source):
                relative = Path('files') / name / path.relative_to(source)
                target = output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = file_hash(path)
                with path.open('rb') as incoming, target.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                if file_hash(path) != digest or file_hash(target) != digest:
                    raise ValueError('Source changed during backup')
                manifest['files'].append({'path': relative.as_posix(), 'bytes': target.stat().st_size, 'sha256': digest})
        if settings.backend == 'postgresql':
            if docker is None or not container:
                raise ValueError('PostgreSQL backup requires its explicit local source container')
            database = docker.validate_source(container, settings)
            before = docker.fingerprints(container, database, settings.schema)
            dump = docker.call(['exec', container, 'pg_dump', '-U', 'postgres', '-d', database,
                                '--schema', settings.schema, '--format=custom', '--no-acl'], timeout=300)
            if not dump.startswith(b'PGDMP'):
                raise ValueError('Invalid PostgreSQL archive')
            with (output / 'database.dump').open('xb') as stream:
                stream.write(dump)
            after = docker.fingerprints(container, database, settings.schema)
            if before != after:
                raise ValueError('Database changed during offline backup')
            manifest['tables'] = before
            manifest['files'].append({'path': 'database.dump', 'bytes': len(dump), 'sha256': file_hash(output / 'database.dump')})
        # Cross-store consistency depends on stopped writers; recheck all files before sealing.
        for name, source in roots.items():
            current = inventory(source)
            expected_paths = {row['path'] for row in manifest['files'] if row['path'].startswith('files/' + name + '/')}
            actual_paths = {(Path('files') / name / path.relative_to(source)).as_posix() for path in current}
            if actual_paths != expected_paths:
                raise ValueError('Business file set changed before sealing backup')
            for path in current:
                relative = (Path('files') / name / path.relative_to(source)).as_posix()
                expected = next((r['sha256'] for r in manifest['files'] if r['path'] == relative), None)
                if expected is None or file_hash(path) != expected:
                    raise ValueError('Business files changed before sealing backup')
        write_json(output / 'manifest.json', manifest)
        verify_bundle(output)
        return {'status': 'passed', 'backup': str(output), 'manifest_sha256': file_hash(output / 'manifest.json'),
                'files': len(manifest['files']), 'backend': settings.backend}
    except Exception as error:
        write_json(output / 'failure.json', {'status': 'failed', 'error_type': type(error).__name__})
        raise


def restore_files(bundle, output, manifest):
    bundle = native_path(bundle)
    output = native_path(output)
    output.mkdir(parents=True, exist_ok=False)
    for row in manifest['files']:
        if not row['path'].startswith('files/'):
            continue
        relative = relative_path(row['path'])
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with (Path(bundle) / relative).open('rb') as incoming, target.open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        if file_hash(target) != row['sha256']:
            raise ValueError('Restored business file hash mismatch')


def restore_drill(bundle, output, *, docker=None, image='postgres:17-alpine', application_check=None):
    """Restore only to a fresh container and directory; source deployment is never a target."""
    bundle = native_path(bundle)
    manifest = verify_bundle(bundle)
    output = native_path(output)
    # Reserve the destination before creating any external resource.
    restore_files(bundle, output, manifest)
    started = time.monotonic()
    name = 'deepsearch-restore-' + uuid4().hex[:16]
    label = uuid4().hex
    created = False
    report = {'status': 'failed', 'backend': manifest['backend'], 'files_sha256_equal': True,
              'source_manifest_sha256': file_hash(Path(bundle) / 'manifest.json'), 'resource': name,
              'isolated_resource_cleaned': False, 'cloud_model_calls': False,
              'recovery_point': manifest['created_at'],
              'backup_age_seconds': round((datetime.now(timezone.utc) - datetime.fromisoformat(manifest['created_at'])).total_seconds(), 3)}
    try:
        if manifest['backend'] == 'postgresql':
            if docker is None or not re.fullmatch(r'postgres:[0-9]+(?:\.[0-9]+)?-alpine', image):
                raise ValueError('Select an official PostgreSQL Alpine major image')
            password = secrets.token_hex(32)
            env = dict(os.environ, POSTGRES_PASSWORD=password)
            docker.call(['run', '-d', '--name', name, '--label', 'deepsearch.restore=' + label,
                '--mount', 'type=tmpfs,destination=/var/lib/postgresql/data',
                '-p', '127.0.0.1::5432', '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_DB=deepsearch', image], env=env)
            created = True
            for _ in range(50):
                try:
                    docker.call(['exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'deepsearch'], timeout=5)
                    break
                except RuntimeError:
                    time.sleep(0.2)
            else:
                raise RuntimeError('Restore database did not become ready')
            docker.call(['exec', '-i', name, 'pg_restore', '-U', 'postgres', '-d', 'deepsearch',
                '--no-owner', '--no-acl', '--single-transaction', '--exit-on-error'],
                data=(Path(bundle) / 'database.dump').read_bytes(), timeout=300)
            actual = docker.fingerprints(name, 'deepsearch', manifest['schema'])
            if actual != manifest['tables']:
                raise ValueError('Restored database tables differ from backup')
            report['all_database_tables_sha256_equal'] = True
            report['tables'] = {key: value['rows'] for key, value in actual.items()}
            schema = manifest['schema']
            role_password = secrets.token_hex(32)
            docker.query(name, 'deepsearch', f"CREATE ROLE recovery_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{role_password}';"
                f'GRANT USAGE ON SCHEMA "{schema}" TO recovery_app;'
                f'GRANT ALL ON ALL TABLES IN SCHEMA "{schema}" TO recovery_app;'
                f'GRANT ALL ON ALL SEQUENCES IN SCHEMA "{schema}" TO recovery_app;'
                f'DELETE FROM "{schema}".access_sessions;')
            port_text = docker.call(['port', name, '5432/tcp']).decode().strip()
            port = int(next(line.rsplit(':', 1)[1] for line in port_text.splitlines() if line.startswith('127.0.0.1:')))
            if application_check is not None:
                report['application'] = application_check(
                    f'postgresql://recovery_app:{role_password}@127.0.0.1:{port}/deepsearch',
                    schema, output / 'files', manifest)
            report['restored_login_sessions_revoked'] = True
        else:
            checked = 0
            for path in (output / 'files').rglob('*'):
                if path.suffix not in {'.sqlite3', '.sqlite', '.db'} or not path.stat().st_size:
                    continue
                value = str(path)
                if value.startswith('\\\\?\\UNC\\'):
                    value = '\\\\' + value[8:]
                elif value.startswith('\\\\?\\'):
                    value = value[4:]
                connection = sqlite3.connect(Path(value).as_uri() + '?mode=ro', uri=True)
                try:
                    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchone():
                        raise ValueError('Restored SQLite integrity failure')
                finally:
                    connection.close()
                checked += 1
            report['sqlite_integrity_checked'] = checked
        report['status'] = 'passed'
    except Exception as error:
        report['error_type'] = type(error).__name__
        raise
    finally:
        if created:
            try:
                actual_label = docker.call(['inspect', '--format', '{{index .Config.Labels "deepsearch.restore"}}', name]).decode().strip()
                if actual_label != label:
                    raise RuntimeError('Restore resource ownership mismatch; refusing cleanup')
                docker.call(['rm', '-f', name])
                report['isolated_resource_cleaned'] = True
            except Exception:
                report['status'] = 'failed'
                report['cleanup_error'] = True
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        write_json(output / 'restore-report.json', report)
    if report['status'] != 'passed':
        raise RuntimeError('Restore drill cleanup failed; inspect restore-report.json')
    return report
