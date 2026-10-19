"""python -m deepsearch_experiments.benchmarks.multihop --help"""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
from deepsearch_agent.adapters.persistence.local_files import atomic_write

from .baselines import prepare_ircot_prompt
from .compare import compare_runs, markdown_table
from .datasets import import_ircot, import_logicrag, load_manifest, write_manifest
from .runner import ALL_METHODS, run_experiment, score_run
from .runtime import RuntimeLimits, ScriptedClient
from .schema import manifest_sha256
from .upstream import DATASETS, LOGICRAG_REVISION, fetch_logicrag_dataset, fetch_reference_assets


def parser():
    p = argparse.ArgumentParser(description='Author-data multi-hop baseline evaluation; no application-state writes')
    sub = p.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('fetch', help='Download pinned author assets, never execute upstream scripts')
    fetch.add_argument('--dataset', choices=['references', *DATASETS], required=True)
    fetch.add_argument('--output', type=Path, required=True)
    fetch.add_argument('--allow-network', action='store_true')
    fetch.add_argument('--proxy')
    for name in ('import-ircot', 'import-logicrag'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--dataset', choices=DATASETS, required=True)
        cmd.add_argument('--questions', type=Path, required=True)
        cmd.add_argument('--corpus', type=Path, required=True)
        cmd.add_argument('--source-revision', required=True)
        cmd.add_argument('--split', required=True)
        cmd.add_argument('--limit', type=int)
        cmd.add_argument('--seed', type=int, default=42)
        cmd.add_argument('--output', type=Path, required=True)
        if name == 'import-ircot':
            cmd.add_argument('--corpus-scope', choices=['full', 'external', 'sampled_pool'], required=True)
            cmd.add_argument('--aliases', type=Path)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--manifest', type=Path, required=True)
    score = sub.add_parser('score', help='Offline rescoring from a saved run')
    score.add_argument('--manifest', type=Path, required=True)
    score.add_argument('--run', type=Path, required=True)
    score.add_argument('--output', type=Path, required=True)
    compare = sub.add_parser('compare')
    compare.add_argument('--manifest', type=Path, required=True)
    compare.add_argument('--runs', type=Path, nargs='+', required=True)
    compare.add_argument('--output', type=Path, required=True, help='New JSON report; sibling Markdown also written')
    compare.add_argument('--historical-reference', action='store_true',
                         help='Explicit cross-code reference; data/model/retrieval/budgets still must match')
    run = sub.add_parser('run')
    run.add_argument('--manifest', type=Path, required=True)
    run.add_argument('--method', choices=list(ALL_METHODS), required=True)
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--llm-env', type=Path, default=Path('.env'))
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument('--allow-remote', action='store_true')
    source.add_argument('--responses', type=Path, help='Offline tape JSON {question_id:[responses]}; not model evaluation')
    proxy = run.add_mutually_exclusive_group()
    proxy.add_argument('--direct', action='store_true')
    proxy.add_argument('--proxy')
    run.add_argument('--prompt-file', type=Path)
    run.add_argument('--demonstration-ids', type=Path, help='Frozen JSON list of training qids, in chosen order')
    run.add_argument('--demo-count', type=int, help='Use first N examples in supplied prompt; explicit adaptation')
    run.add_argument('--max-steps', type=int, default=6)
    run.add_argument('--k', type=int, default=5)
    run.add_argument('--max-model-calls', type=int, default=24)
    run.add_argument('--max-search-calls', type=int, default=8)
    run.add_argument('--token-budget', type=int, default=128000)
    run.add_argument('--max-output-tokens', type=int, default=1024)
    run.add_argument('--context-window', type=int, default=65536)
    run.add_argument('--deadline-seconds', type=float, default=300)
    run.add_argument('--resume', action='store_true')
    run.add_argument('--retry-errors', action='store_true')
    return p


def _network(args):
    if args.direct:
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            os.environ.pop(key, None)
        os.environ['NO_PROXY'] = '*'
    elif args.proxy:
        parsed = urlsplit(args.proxy)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Proxy must be an HTTP(S) URL without credentials')
        os.environ['HTTPS_PROXY'] = os.environ['HTTP_PROXY'] = args.proxy
        os.environ['NO_PROXY'] = 'localhost,127.0.0.1'


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == 'fetch':
            kwargs = dict(allow_network=args.allow_network, proxy=args.proxy)
            result = (fetch_reference_assets(args.output, **kwargs) if args.dataset == 'references'
                      else fetch_logicrag_dataset(args.output, args.dataset, **kwargs))
            print(json.dumps({'downloaded_files': len(result['assets']), 'output': str(args.output)}))
            return
        if args.command.startswith('import-'):
            kwargs = dict(dataset=args.dataset, split=args.split, source_revision=args.source_revision,
                          seed=args.seed, limit=args.limit)
            if args.command == 'import-ircot':
                manifest = import_ircot(args.questions, args.corpus, corpus_scope=args.corpus_scope,
                                        aliases_path=args.aliases, **kwargs)
            else:
                if args.source_revision != LOGICRAG_REVISION:
                    raise ValueError('LogicRAG adapter has only been checked against the pinned release')
                manifest = import_logicrag(args.questions, args.corpus, **kwargs)
            write_manifest(manifest, args.output)
        else:
            manifest = load_manifest(args.manifest)
        if args.command.startswith('import-') or args.command == 'inspect':
            print(json.dumps({'dataset': manifest.dataset, 'protocol': manifest.protocol,
                              'questions': len(manifest.cases), 'available_questions': manifest.available_cases,
                              'documents': len(manifest.documents), 'corpus_scope': manifest.corpus_scope,
                              'manifest_sha256': manifest_sha256(manifest),
                              'annotation_warnings': len(manifest.annotation_warnings)}))
            return
        if args.command == 'compare':
            markdown = args.output.with_suffix('.md')
            if args.output.suffix.lower() != '.json' or args.output.exists() or markdown.exists():
                raise ValueError('Comparison needs new .json and .md output paths')
            comparison = compare_runs(manifest, args.runs, historical_reference=args.historical_reference)
            atomic_write(args.output, json.dumps(comparison, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))
            atomic_write(markdown, markdown_table(comparison).encode('utf-8'))
            print(json.dumps({'methods': len(comparison['rows']), 'result_kind': comparison['result_kind']}))
            return
        if args.command == 'score':
            if args.output.exists():
                raise ValueError('Score output exists; choose a new output')
            report = score_run(manifest, args.run)
            atomic_write(args.output, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))
        else:
            limits = RuntimeLimits(k=args.k, model_calls=args.max_model_calls, search_calls=args.max_search_calls,
                                   token_budget=args.token_budget, max_output_tokens=args.max_output_tokens,
                                   context_window=args.context_window, deadline_seconds=args.deadline_seconds)
            prompt = args.prompt_file.read_text(encoding='utf-8-sig') if args.prompt_file else None
            ids = json.loads(args.demonstration_ids.read_text(encoding='utf-8')) if args.demonstration_ids else None
            if ids is not None and (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)):
                raise ValueError('Demonstration IDs must be a JSON string list')
            if args.demo_count is not None:
                if ids is not None or args.demo_count < 1:
                    raise ValueError('Use positive demo-count OR a frozen ID list')
                _, available = prepare_ircot_prompt(prompt or '')
                if not available or args.demo_count > len(available):
                    raise ValueError('Demo-count exceeds metadata-identified examples')
                ids = available[:args.demo_count]
            if args.responses:
                raw = args.responses.read_bytes()
                tape = json.loads(raw)
                if not isinstance(tape, dict) or set(tape) != {case.id for case in manifest.cases}:
                    raise ValueError('Offline tape must match the exact question population')
                for responses in tape.values():
                    ScriptedClient(responses)
                factory = lambda ident: ScriptedClient(tape[ident])
                identity = {'backend': 'scripted', 'model': 'scripted-offline',
                            'responses_sha256': hashlib.sha256(raw).hexdigest()}
            else:
                settings = LLMSettings.load(args.llm_env)
                if settings.backend != 'openai_compatible':
                    raise ValueError('Live evaluation requires configured cloud model')
                if limits.context_window > settings.context_window:
                    raise ValueError('Evaluation context window exceeds configured model capability')
                settings = replace(settings, max_output_tokens=limits.max_output_tokens,
                                   context_window=limits.context_window,
                                   timeout_seconds=min(settings.timeout_seconds, limits.deadline_seconds))
                _network(args)
                factory = lambda ident: OpenAICompatibleClient(settings)
                identity = {key: getattr(settings, key) for key in ('backend', 'model', 'base_url', 'timeout_seconds',
                                                                  'max_output_tokens', 'context_window')}
                identity['proxy_policy'] = 'direct' if args.direct else args.proxy or 'inherited'
            report = run_experiment(manifest, args.method, factory, args.output, limits, identity,
                                    max_steps=args.max_steps, prompt=prompt, demonstration_ids=ids,
                                    resume=args.resume, retry_errors=args.retry_errors)
        print(json.dumps(report['summary'], ensure_ascii=False))
    except (ValueError, OSError) as exc:
        p.error(str(exc))


if __name__ == '__main__':
    main()
