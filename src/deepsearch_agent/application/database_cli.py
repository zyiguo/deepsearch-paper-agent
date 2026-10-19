"""Explicit schema upgrades and non-overwriting, read-only SQLite import reports."""
import argparse
import json
from contextlib import ExitStack
from pathlib import Path

from deepsearch_agent.adapters.database_config import DatabaseSettings
from deepsearch_agent.core.errors import ApplicationError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['upgrade','status','import-identity','import-workspace'])
    parser.add_argument('--source',type=Path)
    parser.add_argument('--runs',type=Path)
    parser.add_argument('--workspace-id')
    parser.add_argument('--apply',action='store_true',help='Without this option, imports only validate and report')
    parser.add_argument('--output',type=Path,help='New report file; existing files are never replaced')
    args = parser.parse_args(argv)
    importing = args.command.startswith('import-')
    if importing and (args.source is None or args.output is None):
        parser.error('Imports require --source and --output')
    if args.command=='import-workspace' and args.workspace_id is None:
        parser.error('Select an explicit --workspace-id (local or an existing team ID)')
    # Reserve the report before any remote mutation. No data or credentials are echoed.
    report_file = None
    database = None
    operation_started = False
    commit_confirmed = False
    resources = ExitStack()
    try:
        if args.output is not None:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            report_file = args.output.open('x',encoding='utf-8')
        settings = DatabaseSettings.load()
        if settings.backend!='postgresql':
            raise ValueError('Set DATABASE_BACKEND=postgresql before database administration')
        if args.command != 'status':
            from deepsearch_agent.core.paths import PROJECT_ROOT
            from deepsearch_agent.adapters.persistence.deployment_lock import DeploymentLock
            resources.enter_context(DeploymentLock(PROJECT_ROOT / 'data/runtime/deployment.lock'))
        from deepsearch_agent.adapters.persistence.postgres import PostgresDatabase
        from deepsearch_agent.adapters.persistence.postgres_schema import upgrade, VERSION
        database = PostgresDatabase(settings,check_schema=args.command!='upgrade')
        operation_started = True
        if args.command=='upgrade':
            report = {'status':'passed','version':VERSION,'changed':upgrade(database)}
        elif args.command=='status':
            report = {'status':'passed','version':VERSION,'database':'available'}
        else:
            from deepsearch_agent.adapters.persistence.database_import import import_identity, import_workspace
            if args.command=='import-identity':
                result = import_identity(database,args.source,apply=args.apply)
            else:
                result = import_workspace(database,args.source,args.workspace_id,runs_path=args.runs,apply=args.apply)
            report = {'status':'passed',**result}
        commit_confirmed = args.command=='upgrade' or (importing and args.apply)
        if report_file:
            json.dump(report,report_file,ensure_ascii=False,indent=2)
        print(json.dumps(report,ensure_ascii=False))
        return 0
    except Exception as error:
        report = {'status':'failed','error_type':type(error).__name__,
                  'write_outcome':'committed' if commit_confirmed else ('unknown' if operation_started and (args.apply or args.command=='upgrade') else 'not_started'),
                  'message':'Database operation failed; check settings, schema, source integrity and empty destination. No automatic fallback.'}
        if report_file:
            json.dump(report,report_file,indent=2)
        print(json.dumps(report))
        return 1
    finally:
        if database is not None:
            database.close()
        if report_file is not None:
            report_file.close()
        resources.close()


if __name__=='__main__':
    raise SystemExit(main())
