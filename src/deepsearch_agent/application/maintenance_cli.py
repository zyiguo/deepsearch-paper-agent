"""Offline backup, hash validation and isolated restore; never overwrite a deployment."""
import argparse
import json
from pathlib import Path

from deepsearch_agent.core.paths import PROJECT_ROOT
from deepsearch_agent.adapters.database_config import DatabaseSettings
from deepsearch_agent.adapters.access_config import AccessSettings
from deepsearch_agent.adapters.persistence.deployment_lock import DeploymentLock
from deepsearch_agent.adapters.persistence.recovery import (
    DockerPostgres, create_bundle, verify_bundle, restore_drill, file_hash,
)


def check_restored_application(url, schema, files, manifest):
    """Use the real public repositories as a non-superuser against the restored DB."""
    from deepsearch_agent.adapters.persistence.postgres import PostgresDatabase
    from deepsearch_agent.adapters.persistence.postgres_access import PostgresAccessStore
    from deepsearch_agent.adapters.persistence.postgres_conversations import PostgresConversationStore
    from deepsearch_agent.adapters.persistence.postgres_runs import PostgresResearchRunStore
    from deepsearch_agent.workspace.memory import MemoryManager
    database = PostgresDatabase(DatabaseSettings(backend='postgresql', url=url, schema=schema))
    try:
        access = PostgresAccessStore(database)
        users = access.users()
        with database.transaction() as db:
            workspaces = db.execute('SELECT id FROM access_workspaces ORDER BY id').fetchall()
            assert db.execute('SELECT count(*) AS n FROM access_sessions').fetchone()['n'] == 0
        result = {'users': len(users), 'scopes': {}, 'login_sessions_revoked': True}
        totals = {'sessions': 0, 'turns': 0, 'session_memories': 0, 'research_runs': 0}
        for ident in ['local', *(row['id'] for row in workspaces)]:
            with database.transaction(ident) as db:
                if not db.execute('SELECT workspace_id FROM business_scopes').fetchone():
                    continue
            path = files / ('indexes' if ident == 'local' else 'team/workspaces/' + ident)
            conversations = PostgresConversationStore(database, ident, path / 'conversations.sqlite3')
            # The run repository applies the same interruption policy used at real API startup.
            runs = PostgresResearchRunStore(database, ident, path / 'research_runs.sqlite3')
            sessions = conversations.list_sessions()
            count = {'sessions': len(sessions), 'turns': 0, 'memories': 0, 'runs': 0}
            for row in sessions:
                detail = conversations.get(row['id'])
                count['turns'] += len(detail['turns'])
                with database.transaction(ident) as db:
                    saved = db.execute('SELECT payload FROM session_memories WHERE session_id=%s', (row['id'],)).fetchone()
                if saved:
                    assert MemoryManager(conversations).get(row['id']).model_dump() == saved['payload']
                    count['memories'] += 1
                count['runs'] += len(runs.list(row['id']))
            result['scopes'][ident] = count
            for field, key in [('sessions', 'sessions'), ('turns', 'turns'), ('session_memories', 'memories'), ('research_runs', 'runs')]:
                totals[field] += count[key]
        for table, total in totals.items():
            assert total == manifest['tables'][table]['rows'], 'Restored public repository count mismatch'
        result['all_public_repository_counts_equal'] = True
        return result
    finally:
        database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['backup', 'verify', 'restore-drill'])
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--postgres-container', help='Exact configured local source PostgreSQL container')
    parser.add_argument('--postgres-image', default='postgres:17-alpine')
    parser.add_argument('--docker-host')
    parser.add_argument('--docker-config', type=Path)
    args = parser.parse_args(argv)
    if args.command != 'verify' and args.output is None:
        parser.error('Use a new --output directory')
    if args.command != 'backup' and args.bundle is None:
        parser.error('Select --bundle')
    docker = DockerPostgres(host=args.docker_host, config=args.docker_config)
    try:
        if args.command == 'backup':
            with DeploymentLock(PROJECT_ROOT / 'data/runtime/deployment.lock'):
                settings = DatabaseSettings.load()
                access = AccessSettings.load()
                roots = {'indexes': PROJECT_ROOT / 'data/indexes', 'team': access.root,
                         'runtime': PROJECT_ROOT / 'data/runtime', 'skills': PROJECT_ROOT / 'skills'}
                config_hashes = {p.name: file_hash(p) for p in [PROJECT_ROOT / name for name in (
                    '.env', '.env.embedding', '.env.vision', '.env.image_embedding', '.env.agent',
                    '.env.access', '.env.database', '.env.mcp', '.env.infrastructure', '.env.tracing', '.env.governance')] if p.is_file()}
                report = create_bundle(args.output, roots, settings, docker=docker,
                                       container=args.postgres_container, config_hashes=config_hashes)
        elif args.command == 'verify':
            manifest = verify_bundle(args.bundle)
            report = {'status': 'passed', 'files': len(manifest['files']),
                      'manifest_sha256': file_hash(args.bundle / 'manifest.json')}
        else:
            report = restore_drill(args.bundle, args.output, docker=docker, image=args.postgres_image,
                                   application_check=check_restored_application)
        print(json.dumps(report, ensure_ascii=True))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
                          'message': 'Maintenance failed; check stopped writers, selected source, bundle integrity and new destination. Existing deployment was not used as a restore target.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
