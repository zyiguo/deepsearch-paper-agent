"""Local operator account provisioning; passwords never appear in command arguments."""
import argparse
from contextlib import ExitStack
from getpass import getpass

from deepsearch_agent.adapters.access_config import AccessSettings
from deepsearch_agent.adapters.passwords import hash_password
from deepsearch_agent.adapters.persistence.access import AccessStore
from deepsearch_agent.application.access import AccessService, normalized_username
from deepsearch_agent.core.errors import ApplicationError
from deepsearch_agent.adapters.database_config import DatabaseSettings
from deepsearch_agent.adapters.persistence.business import build_database, access_store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['create-user', 'reset-password'])
    parser.add_argument('--username', required=True)
    parser.add_argument('--display-name')
    parser.add_argument('--admin', action='store_true')
    args = parser.parse_args(argv)
    settings = AccessSettings.load()
    if settings.mode != 'team':
        parser.error('Set ACCESS_MODE=team in .env.access before provisioning team accounts')
    password = getpass('Password (12–128 characters): ')
    if password != getpass('Confirm password: '):
        parser.error('Passwords do not match')
    try:
        with ExitStack() as resources:
            from deepsearch_agent.core.paths import PROJECT_ROOT
            from deepsearch_agent.adapters.persistence.deployment_lock import DeploymentLock
            resources.enter_context(DeploymentLock(PROJECT_ROOT / 'data/runtime/deployment.lock'))
            database = build_database(DatabaseSettings.load())
            if database is not None:
                resources.callback(database.close)
            store = access_store(database, settings.root / 'identity.sqlite3')
            if args.command == 'create-user':
                user = AccessService(store, settings).create_user(args.username, args.display_name or args.username,
                                                                password, admin=args.admin)
                print('Created user: ' + user['username'])
            else:
                store.reset_password(normalized_username(args.username), hash_password(password))
                print('Password changed; all sessions revoked')
    except (ApplicationError, ValueError, KeyError):
        parser.exit(1, 'Account operation failed; check username, password policy and existing accounts.\n')


if __name__ == '__main__':
    main()
