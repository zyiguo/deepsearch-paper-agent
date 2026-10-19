"""Read-only rescoring of pinned local benchmarks using their existing scorers.

This module never runs a model. Benchmark outcomes are automatic label matches,
not human reviews, causal attribution, or production trajectory classifications.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _inside(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError('Catalog paths must be relative to --root')
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Catalog path escapes --root')
    return path


def _write(path, data):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write('\n')


def _case_result(case_id, question, expected, prediction, scores, metric, execution_status,
                 case_sha256, *, missing=False):
    return {'case_id': case_id, 'case_sha256': case_sha256, 'question': question,
            'expected': expected, 'prediction': prediction, 'scores': scores,
            'benchmark_metric': metric,
            'benchmark_outcome': ('unknown' if missing else
                                  'success' if scores[metric] == 1 else 'failure'),
            'execution_status': execution_status, 'internal_verifier': None,
            'human_verdict': None, 'root_causes': None}


def _multimodal(manifest_path, run_path, inputs):
    from .benchmarks.schema import load_manifest, inside
    from .benchmarks.runner import read_predictions, summarize

    manifest = load_manifest(manifest_path)
    inputs.update({inside(manifest_path.parent, page['image']): page['image_sha256']
                   for page in manifest['pages']})
    identity = _read(run_path / 'run.json')
    if identity['manifest_sha256'] != manifest['_manifest_sha256']:
        raise ValueError('Manifest/run identity mismatch')
    ids = identity['case_ids']
    all_cases = {case['id']: case for case in manifest['cases']}
    if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
            or not set(ids) <= all_cases.keys()):
        raise ValueError('Invalid run case population')
    cases = [all_cases[ident] for ident in ids]
    rows = read_predictions(run_path / 'predictions.jsonl')
    config = identity['configuration']
    mode, k = config['mode'], config['k']
    if mode not in {'oracle', 'rerank', 'rag'} or type(k) is not int or k < 1:
        raise ValueError('Invalid scoring configuration')
    if any(row.get('mode') != mode for row in rows):
        raise ValueError('Prediction/run mode mismatch')

    def score(selected):
        selected_ids = {case['id'] for case in selected}
        return summarize(selected, [row for row in rows if row['id'] in selected_ids],
                         k=k, mode=mode, pages=manifest['pages'])

    # Validate the entire prediction population before filtering by task/group.
    summary = summarize(cases, rows, k=k, mode=mode, pages=manifest['pages'])
    by_id = {row['id']: row for row in rows}
    per_case = []
    primary = {'chartqa': 'relaxed_accuracy', 'A': 'accuracy',
               'B': 'element_exact_match', 'C': 'element_exact_match'}
    for case in cases:
        row = by_id.get(case['id'])
        scores = score([case])
        result = _case_result(case['id'], case['question'],
                              {'answers': case['answers'], 'element_ids': case['gold_element_ids'],
                               'gold_pages': case['gold_pages']},
                              {key: row[key] for key in ('answer', 'element_ids', 'ranking', 'error_type')
                               if row is not None and key in row},
                              scores, f'recall@{k}' if mode == 'rerank' else primary[case['task']],
                              'missing' if row is None else
                              row.get('status', 'unknown') if not scores['error_count'] else 'error',
                              _canonical(case), missing=row is None)
        result.update(task=case['task'], group=case['group'], input_pages=case['scope'])
        per_case.append(result)
    return {'dataset': manifest['dataset'], 'split': manifest['split'],
            'historical_model': config.get('vision_model'), 'configuration': config,
            'population': ids, 'summary': summary,
            'by_task': {key: score([case for case in cases if case['task'] == key])
                        for key in sorted({case['task'] for case in cases})},
            'by_group': {key: score([case for case in cases if case['group'] == key])
                         for key in sorted({case['group'] for case in cases})},
            'per_case': per_case,
            'coverage': 'Component evaluation; does not execute the parent/child Agent graph.'}


def _multihop(manifest_path, run_path, inputs):
    from .benchmarks.multihop.datasets import load_manifest
    from .benchmarks.multihop.runner import score_run, read_attempts

    manifest = load_manifest(manifest_path)
    report = score_run(manifest, run_path)
    _, latest = read_attempts(run_path / 'attempts.jsonl', manifest, report['run'])
    cases = {case.id: case for case in manifest.cases}
    gold = {row.id: row for row in manifest.gold}
    per_case = []
    for row in report['per_case']:
        ident = row['id']
        result = _case_result(ident, cases[ident].question, {'answers': gold[ident].answers,
                              'support_document_ids': gold[ident].support_document_ids},
                              latest[ident]['prediction'] if ident in latest else {},
                              {key: row[key] for key in ('answer_em', 'answer_f1',
                                                        'document_support_recall_proxy')},
                              'answer_em', row['status'], row['case_sha256'],
                              missing=ident not in latest)
        result.update(diagnostic_observation=row['diagnostic_tag'],
                      latest_attempt=latest[ident]['attempt'] if ident in latest else None)
        per_case.append(result)
    return {**report, 'historical_model': report['run']['model']['model'],
            'population': [case.id for case in manifest.cases], 'per_case': per_case,
            'corpus_scope': manifest.corpus_scope,
            'coverage': ('Historical Agent graph with fixed BM25 corpus; '
                         'does not evaluate HTTP, database recovery, image embeddings or human correctness.')}


def rescore_catalog(catalog_path, output, *, root=Path('.')):
    """Validate pinned inputs and rescore into a new directory; never overwrite.

    A rejected entry remains in report.json and makes the CLI exit nonzero.
    Missing predictions stay in metric denominators and have unknown outcomes.
    """
    root, catalog_path, output = Path(root).resolve(), Path(catalog_path).resolve(), Path(output).resolve()
    catalog_hash = _digest(catalog_path)
    catalog = _read(catalog_path)
    entries = catalog.get('entries')
    if catalog.get('schema_version') != 1 or not isinstance(entries, list) or not entries:
        raise ValueError('Invalid dataset catalog')
    ids = [entry.get('id') for entry in entries]
    if any(not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9-]+', ident) for ident in ids) or len(set(ids)) != len(ids):
        raise ValueError('Invalid or duplicate catalog entry IDs')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'reports').mkdir()
    report = {'schema_version': 1, 'kind': 'existing-local-dataset-rescore',
              'created_at': datetime.now(timezone.utc).isoformat(),
              'catalog_sha256': catalog_hash, 'model_calls_this_run': 0,
              'result_scope': 'Historical predictions rescored offline; no new model evaluation.',
              'outcome_policy': ('Automatic primary-metric match: success=1, failure<1, '
                                 'unknown=missing prediction. Human/verifier/root-cause labels are independent.'),
              'entries': [], 'inputs_unchanged': True}
    from .identity import source_fingerprint
    report['scorer_source_sha256'] = source_fingerprint()
    grouped = {key: [] for key in ('success', 'failure', 'unknown')}
    all_inputs = {catalog_path: catalog_hash}
    for entry in entries:
        inputs = {catalog_path: catalog_hash}
        try:
            kind = entry['kind']
            if kind not in {'multimodal', 'multihop'}:
                raise ValueError('Unsupported catalog kind')
            manifest_path = _inside(root, entry['manifest'])
            run_path = _inside(root, entry['run'])
            records_name = 'predictions.jsonl' if kind == 'multimodal' else 'attempts.jsonl'
            for path, field in ((manifest_path, 'manifest_sha256'),
                                (run_path / 'run.json', 'run_sha256'),
                                (run_path / records_name, 'records_sha256')):
                expected = entry[field]
                if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
                    raise ValueError(f'Invalid catalog fingerprint: {field}')
                inputs[path] = _digest(path)
                if inputs[path] != expected:
                    raise ValueError(f'Pinned input identity mismatch: {field}')
            scored = (_multimodal if kind == 'multimodal' else _multihop)(manifest_path, run_path, inputs)
            all_inputs.update(inputs)
            if any(not path.is_file() or _digest(path) != expected for path, expected in inputs.items()):
                report['inputs_unchanged'] = False
                raise ValueError('Input changed while scoring')
            input_hashes = {path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path): value
                            for path, value in inputs.items()}
            scored.update(catalog_entry=entry, source_files=input_hashes)
            _write(output / 'reports' / f"{entry['id']}.json", scored)
            for case in scored['per_case']:
                grouped[case['benchmark_outcome']].append({**case, 'entry_id': entry['id'],
                    'manifest_sha256': entry['manifest_sha256'], 'run_sha256': entry['run_sha256'],
                    'records_sha256': entry['records_sha256'],
                    'source_run': entry['run'], 'source_report': f"reports/{entry['id']}.json"})
            report['entries'].append({'id': entry['id'], 'status': 'scored',
                                      'dataset': scored['dataset'], 'scope': entry.get('scope'),
                                      'historical_model': scored['historical_model'],
                                      'summary': scored['summary'],
                                      'automatic_outcomes': dict(Counter(case['benchmark_outcome']
                                                                        for case in scored['per_case'])),
                                      'report': f"reports/{entry['id']}.json"})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            report['entries'].append({'id': entry['id'], 'status': 'rejected',
                                      'error_type': type(exc).__name__, 'error': str(exc)})
    # Also recheck inputs after subsequent entries; shared source mutation is a rejection.
    report['inputs_unchanged'] &= all(path.is_file() and _digest(path) == expected
                                      for path, expected in all_inputs.items())
    report['catalog_unchanged'] = catalog_path.is_file() and _digest(catalog_path) == catalog_hash
    report['status'] = ('scored' if report['catalog_unchanged'] and report['inputs_unchanged']
                        and all(entry['status'] == 'scored' for entry in report['entries']) else 'rejected')
    for group, rows in grouped.items():
        with (output / f'{group}.jsonl').open('x', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    _write(output / 'report.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=Path('eval/existing-datasets-v1.json'))
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = rescore_catalog(args.catalog, args.output, root=args.root)
    print(json.dumps({'status': report['status'], 'entries': len(report['entries']),
                      'model_calls': 0, 'report': str(args.output / 'report.json')}, ensure_ascii=False))
    return 0 if report['status'] == 'scored' else 1


if __name__ == '__main__':
    raise SystemExit(main())
