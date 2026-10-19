"""Bounded runs, strict resume identities, item-level errors and offline scoring."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from deepsearch_experiments.benchmarks.metrics import answer_scores, ranking_scores
from deepsearch_experiments.benchmarks.schema import validate_prediction
from deepsearch_agent.adapters.persistence.local_files import atomic_write


def read_predictions(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate prediction IDs')
    return rows


def summarize(cases, rows, k=5, mode=None, *, pages=None):
    if not cases:
        raise ValueError('No evaluation cases')
    by_id = {row['id']: row for row in rows}
    if len(by_id) != len(rows) or not by_id.keys() <= {c['id'] for c in cases}:
        raise ValueError('Duplicate or unknown prediction IDs')
    mode = mode or (rows[0].get('mode') if rows else None)
    page_map = {p['id']: p for p in pages} if pages is not None else None
    values, errors, missing = {}, 0, 0
    rankings, initial = [], []
    for case in cases:
        row = by_id.get(case['id'])
        failed = row is None or row.get('status') != 'ok'
        if not failed:
            try:
                if mode in {'rerank', 'rag'}:
                    for key in ('ranking', *(['initial_ranking'] if 'initial_ranking' in row else [])):
                        if not isinstance(row.get(key), list) or not all(isinstance(p, str) for p in row[key]):
                            raise ValueError('Ranking must be a page ID list')
                        if page_map is not None and not set(row[key]) <= page_map.keys():
                            raise ValueError('Ranking includes unknown input pages')
                    if mode == 'rag' and case['dataset'] == 'pdfvqa' and not set(row['ranking']) <= set(case['scope']):
                        raise ValueError('Ranking exceeds PDF input scope')
                if mode != 'rerank':
                    allowed_ids = None
                    if page_map is not None:
                        selected = row['ranking'] if mode == 'rag' else case['scope']
                        if not set(selected) <= page_map.keys():
                            raise ValueError('Prediction context includes unknown pages')
                        allowed_ids = {e['id'] for p in selected for e in page_map[p]['elements']}
                    validate_prediction(case, {key: row[key] for key in ('answer', 'element_ids') if key in row}, allowed_ids)
            except ValueError:
                failed = True
        missing += row is None
        errors += row is not None and failed
        scores = answer_scores(case, row if not failed else {}) if mode != 'rerank' else {}
        if failed:
            scores = {key: 0. for key in scores}
        for metric, value in scores.items():
            values.setdefault(metric, []).append(value)
        if mode in {'rerank', 'rag'} and case['gold_pages']:
            ranks = ranking_scores(row.get('ranking', []) if not failed else [], case['gold_pages'], k)
            rankings.append(ranks)
            if mode == 'rerank':
                initial.append(ranking_scores(row.get('initial_ranking', []) if not failed else [], case['gold_pages'], k))
    result = {'count': len(cases), 'error_count': errors, 'missing_count': missing,
              **{key: sum(scores) / len(scores) for key, scores in values.items()}}
    if rankings:
        result.update({f'{name}@{k}': sum(r[name] for r in rankings) / len(rankings)
                       for name in ('recall', 'mrr', 'ndcg')})
    if initial:
        result.update({f'initial_{name}@{k}': sum(r[name] for r in initial) / len(initial)
                       for name in ('recall', 'mrr', 'ndcg')})
    result['rerank_fallback_count'] = sum(str(r.get('rerank_status', '')).startswith('fallback') for r in rows)
    result['mean_latency_ms'] = sum(r.get('latency_ms', 0) for r in rows) / len(rows) if rows else None
    return result


def run_cases(manifest, predict, output, configuration, *, workers=1, resume=False, retry_errors=False):
    if not 1 <= workers <= 4:
        raise ValueError('workers must be 1 to 4')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    meta_path, rows_path = output / 'run.json', output / 'predictions.jsonl'
    from deepsearch_experiments.identity import source_fingerprint
    code_hash = source_fingerprint()
    identity = {'manifest_sha256': manifest['_manifest_sha256'], 'code_sha256': code_hash,
                'configuration': configuration, 'case_ids': [c['id'] for c in manifest['cases']]}
    rows = []
    if meta_path.exists() or rows_path.exists():
        if not resume or not meta_path.exists() or json.loads(meta_path.read_text(encoding='utf-8')) != identity:
            raise ValueError('Run identity mismatch or output exists; choose new output or matching --resume')
        rows = read_predictions(rows_path) if rows_path.exists() else []
    else:
        atomic_write(meta_path, json.dumps(identity, ensure_ascii=False, indent=2).encode('utf-8'))
    summarize(manifest['cases'], rows, pages=manifest['pages'])
    if retry_errors:
        rows = [r for r in rows if r['status'] == 'ok']
    done = {r['id'] for r in rows}
    def work(case):
        start = time.perf_counter()
        try:
            result = predict(case)
            row = {**result, 'status': result.get('status', 'ok')}
        except Exception as error:
            row = {'status': 'error', 'error_type': type(error).__name__}
        return {**row, 'id': case['id'], 'mode': configuration['mode'],
                'latency_ms': (time.perf_counter() - start) * 1000}
    def save():
        # One writer; atomic replacement also preserves completed work on interruption.
        content = ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False)+'\n' for r in rows)
        atomic_write(rows_path, content.encode('utf-8'))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, c) for c in manifest['cases'] if c['id'] not in done]
        for future in as_completed(futures):
            rows.append(future.result())
            save()
            print(f"Completed {len(rows)}/{len(manifest['cases'])}", flush=True)
    rows.sort(key=lambda r: identity['case_ids'].index(r['id']))
    save()
    cases = manifest['cases']
    report = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              **identity, 'provenance': manifest['provenance'], 'summary': summarize(cases, rows, configuration.get('k', 5), configuration['mode'], pages=manifest['pages']),
              'by_group': {group: summarize([c for c in cases if c['group'] == group],
                            [r for r in rows if r['id'] in {c['id'] for c in cases if c['group'] == group}], configuration.get('k', 5), configuration['mode'], pages=manifest['pages'])
                           for group in sorted({c['group'] for c in cases})},
              'by_task': {task: summarize([c for c in cases if c['task'] == task],
                           [r for r in rows if r['id'] in {c['id'] for c in cases if c['task'] == task}], configuration.get('k', 5), configuration['mode'], pages=manifest['pages'])
                          for task in sorted({c['task'] for c in cases})},
              'pipeline': 'Production retrieval/rerank adapters + structured benchmark generation; not LangGraph/skills/memory'}
    atomic_write(output / 'report.json', json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8'))
    return report
