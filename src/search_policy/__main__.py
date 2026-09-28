"""python -m search_policy --help (no RAG or ML dependencies)."""
import argparse
import json
import sqlite3
from pathlib import Path

from .dataset import (candidates, export_dataset, read_jsonl, review_template,
                      verify_dataset, write_new_jsonl)
from .store import TraceStore


def main():
    parser = argparse.ArgumentParser(description='Local search-policy trace and action-SFT pipeline')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'raw', 'candidates', 'export'):
        command = sub.add_parser(name)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument('--db', type=Path)
        source.add_argument('--traces', type=Path, help='Frozen raw JSONL snapshot')
        if name != 'inspect':
            command.add_argument('--output', required=True, type=Path)
        if name == 'candidates':
            command.add_argument('--review-template', type=Path)
        if name == 'export':
            command.add_argument('--reviews', required=True, type=Path)
            command.add_argument('--val-ratio', type=float, default=0.2)
            command.add_argument('--seed', default='42')
    verify = sub.add_parser('verify')
    verify.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'verify':
            result = verify_dataset(args.directory)
        else:
            if args.db is not None:
                if not args.db.is_file():
                    raise ValueError('Trace database does not exist')
                runs = TraceStore(args.db).read_runs()
            else:
                runs = read_jsonl(args.traces)
            rows = candidates(runs)
            result = {'runs': len(runs), 'candidates': len(rows),
                      'eligible': sum(r['eligible'] for r in rows)}
            if args.command == 'raw':
                write_new_jsonl(args.output, runs)
            elif args.command == 'candidates':
                write_new_jsonl(args.output, rows)
                if args.review_template:
                    write_new_jsonl(args.review_template, review_template(rows))
            elif args.command == 'export':
                result = export_dataset(runs, read_jsonl(args.reviews), args.output,
                                        val_ratio=args.val_ratio, seed=args.seed)
    except ValueError as error:
        parser.exit(2, f'Validation failed: {error}\n')
    except (OSError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(2, f'{type(error).__name__}: pipeline operation failed; check input schema and paths.\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
