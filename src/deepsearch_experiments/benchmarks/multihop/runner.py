"""Immutable experiment identities and durable per-question attempt records."""
from dataclasses import asdict
import hashlib
from importlib.metadata import version
import json
from pathlib import Path

from deepsearch_agent.adapters.persistence.local_files import atomic_write
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.core.cancellation import ResearchCancelled

from .baselines import METHODS, prepare_ircot_prompt, run_baseline
from .metrics import evaluate_predictions
from .runtime import BM25Retriever, BudgetExceeded, EvaluationRuntime
from .schema import Prediction, canonical_sha256, manifest_sha256

from .mainline import METHOD as MAINLINE_METHOD, BUDGET_METHODS, run_mainline

ALL_METHODS = {**METHODS, 'mainline': MAINLINE_METHOD, **BUDGET_METHODS}


def implementation_identity():
    from deepsearch_experiments.identity import source_fingerprint
    return {'sha256': source_fingerprint(),
            'dependencies': {name: version(name) for name in (
                'pydantic', 'httpx', 'jieba', 'pysbd', 'langgraph', 'langchain-core', 'langsmith')}}


def _save_json(path, data):
    atomic_write(Path(path), json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode('utf-8'))


def _save_attempts(path, attempts):
    atomic_write(Path(path), ''.join(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n' for row in attempts).encode('utf-8'))


def read_attempts(path, manifest, identity):
    path = Path(path)
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []
    latest = {}
    allowed_ids = {case.id for case in manifest.cases}
    identity_hash = canonical_sha256(identity)
    for row in rows:
        ident = row.get('id')
        expected = latest[ident]['attempt'] + 1 if ident in latest else 1
        if ident not in allowed_ids or type(row.get('attempt')) is not int or row['attempt'] != expected:
            raise ValueError('Unknown question or nonsequential/duplicate attempt')
        if row.get('run_sha256') != identity_hash:
            raise ValueError('Prediction/run identity mismatch')
        Prediction.model_validate(row['prediction'])
        latest[ident] = row
    evaluate_predictions(manifest, {key: row['prediction'] for key, row in latest.items()})
    return rows, latest


def _report(manifest, identity, attempts, latest):
    report = evaluate_predictions(manifest, {key: row['prediction'] for key, row in latest.items()})
    stats = [row['usage'] for row in attempts]
    report.update(run=identity, attempts=len(attempts),
                  annotation_warnings=manifest.annotation_warnings,
                  result_kind='offline-plumbing-only' if identity['model']['backend'] == 'scripted' else 'adapted-baseline-evaluation',
                  cost_scope='all attempts, including failed/retried attempts',
                  total_model_calls=sum(s['model_calls'] for s in stats),
                  total_search_calls=sum(s['search_calls'] for s in stats),
                  total_accounted_tokens=sum(s['accounted_tokens'] for s in stats),
                  reported_total_tokens=(sum(s['reported_total_tokens'] for s in stats)
                                         if all(s['reported_total_tokens'] is not None for s in stats) else None),
                  usage_missing_calls=sum(s['usage_missing_calls'] for s in stats))
    return report


def score_run(manifest, output):
    output = Path(output)
    identity = json.loads((output / 'run.json').read_text(encoding='utf-8'))
    if identity['manifest_sha256'] != manifest_sha256(manifest):
        raise ValueError('Manifest/run identity mismatch')
    attempts, latest = read_attempts(output / 'attempts.jsonl', manifest, identity)
    return _report(manifest, identity, attempts, latest)


def run_experiment(manifest, method, client_factory, output, limits, model_identity, *,
                   max_steps=6, prompt=None, demonstration_ids=None, resume=False, retry_errors=False):
    if method not in ALL_METHODS:
        raise ValueError('Unknown baseline')
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError('max_steps must be a positive integer')
    allowed_model_fields = {'backend', 'model', 'base_url', 'timeout_seconds', 'max_output_tokens',
                            'context_window', 'responses_sha256', 'proxy_policy'}
    if set(model_identity) - allowed_model_fields or model_identity.get('backend') not in {'scripted', 'openai_compatible'}:
        raise ValueError('Model identity must contain only non-secret approved fields')
    prompt_ids = []
    if method == 'ircot':
        _, prompt_ids = prepare_ircot_prompt(prompt or '', demonstration_ids)
        if set(prompt_ids) & {case.id for case in manifest.cases}:
            raise ValueError('Demonstration/evaluation question ID overlap')
    elif prompt is not None or demonstration_ids is not None:
        raise ValueError('LogicRAG does not use IRCoT demonstration prompts')
    if retry_errors and not resume:
        raise ValueError('Retrying errors requires explicit resume')
    identity = {'schema_version': 1, 'manifest_sha256': manifest_sha256(manifest),
                'implementation': implementation_identity(), 'method': ALL_METHODS[method],
                'model': model_identity, 'limits': asdict(limits), 'retriever': BM25Retriever.identity,
                'max_steps': max_steps, 'demonstration_ids': prompt_ids,
                'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest() if prompt is not None else None,
                'prompt_selection': 'selected-qids' if demonstration_ids is not None else 'provided-file-order'}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / 'run.lock'
    # Exclusive lock rejects concurrent writers; stale locks require deliberate recovery.
    with lock_path.open('x', encoding='utf-8') as lock:
        lock.write('Active experiment writer. Remove only after confirming its process has stopped.\n')
    try:
        meta, rows_path = output / 'run.json', output / 'attempts.jsonl'
        if meta.exists() or rows_path.exists():
            if not resume or not meta.exists() or json.loads(meta.read_text(encoding='utf-8')) != identity:
                raise ValueError('Run identity mismatch or output exists; use matching --resume or a new output')
        elif resume:
            raise ValueError('Cannot resume a nonexistent run')
        else:
            _save_json(meta, identity)
        attempts, latest = read_attempts(rows_path, manifest, identity)
        retriever = BM25Retriever([document.model_dump() for document in manifest.documents])
        for case in manifest.cases:
            previous = latest.get(case.id)
            if previous and (not retry_errors or previous['prediction']['status'] == 'ok'):
                continue
            client = client_factory(case.id)
            runtime = EvaluationRuntime(client, retriever, limits)
            details, interrupted = {}, False
            try:
                with runtime.cancellation_scope():
                    if method == 'mainline' or method in BUDGET_METHODS:
                        details = run_mainline(case.question, runtime, max_steps=max_steps,
                                               budget_mode=BUDGET_METHODS.get(method, {}).get('budget_mode'),
                                               evidence_controlled=method.startswith('mainline-evidence-controlled-'),
                                               policy_version=BUDGET_METHODS.get(method, {}).get('policy_version', 'v1'))
                    else:
                        details = run_baseline(method, case.question, runtime, max_steps=max_steps,
                                               few_shot_examples=prompt, demonstration_ids=demonstration_ids)
                prediction = Prediction(answer=details['answer'], status=details['status'],
                                        retrieved_document_ids=runtime.retrieved_document_ids)
            except BudgetExceeded as exc:
                prediction = Prediction(status='budget_exhausted', retrieved_document_ids=runtime.retrieved_document_ids)
                details = {'termination_reason': str(exc)}
            except ResearchCancelled:
                prediction = Prediction(status='budget_exhausted', retrieved_document_ids=runtime.retrieved_document_ids)
                details = {'termination_reason': 'deadline_cancelled_inflight_request'}
            except KeyboardInterrupt:
                interrupted = True
                prediction = Prediction(status='interrupted', retrieved_document_ids=runtime.retrieved_document_ids)
            except ModelServiceError as exc:
                prediction = Prediction(status='error', retrieved_document_ids=runtime.retrieved_document_ids)
                details = {'error_type': type(exc).__name__, **exc.diagnostics()}
            except Exception as exc:
                prediction = Prediction(status='error', retrieved_document_ids=runtime.retrieved_document_ids)
                details = {'error_type': type(exc).__name__}
            finally:
                if hasattr(client, 'close'):
                    client.close()
            row = {'id': case.id, 'attempt': previous['attempt'] + 1 if previous else 1,
                   'run_sha256': canonical_sha256(identity), 'prediction': prediction.model_dump(),
                   'usage': runtime.statistics(), 'events': runtime.events, 'details': details}
            attempts.append(row)
            latest[case.id] = row
            _save_attempts(rows_path, attempts)
            _save_json(output / 'report.json', _report(manifest, identity, attempts, latest))
            print(f'{method}: {len(latest)}/{len(manifest.cases)} {prediction.status}', flush=True)
            if interrupted:
                raise KeyboardInterrupt
        report = _report(manifest, identity, attempts, latest)
        _save_json(output / 'report.json', report)
        return report
    finally:
        lock_path.unlink(missing_ok=True)
