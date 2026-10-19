"""Compare completed runs only when their shared experimental conditions match."""
from pathlib import Path

from .runner import score_run


def compare_runs(manifest, runs, *, historical_reference=False):
    if len(runs) < 2:
        raise ValueError('At least two runs are required')
    reports = [score_run(manifest, path) for path in runs]
    reference = reports[0]['run']
    fields = ('manifest_sha256', 'model', 'limits', 'retriever', 'implementation', 'max_steps')
    if historical_reference:
        fields = tuple(key for key in fields if key != 'implementation')
    for report in reports:
        if any(report['run'][key] != reference[key] for key in fields):
            raise ValueError('Cannot compare runs with different model, data, retrieval, code, or budgets')
        if report['summary']['missing_predictions']:
            raise ValueError('Incomplete run: missing predictions; finish it before comparison')
    names = [report['run']['method']['name'] for report in reports]
    if len(set(names)) != len(names):
        raise ValueError('Comparison requires distinct methods')
    rows = []
    for path, report in zip(runs, reports, strict=True):
        rows.append({'method': report['run']['method']['name'], 'run': str(Path(path)),
                     'implementation': report['run']['implementation'],
                     **report['summary'], 'total_model_calls': report['total_model_calls'],
                     'total_search_calls': report['total_search_calls'],
                     'reported_total_tokens': report['reported_total_tokens'],
                     'total_accounted_tokens': report['total_accounted_tokens'],
                     'attempts': report['attempts']})
    return {'schema_version': 1, 'manifest_sha256': reference['manifest_sha256'],
            'result_kind': ('historical-code-version-comparison' if historical_reference
                            else reports[0]['result_kind']), 'rows': rows,
            'code_identity_checked': not historical_reference,
            'code_warning': ('Historical reference: code identities may differ; this is not a same-build experiment.'
                             if historical_reference else ''),
            'interpretation': 'Paired population, descriptive results only; no statistical significance claim.',
            'cost_scope': 'All attempts including failures and retries; unknown provider usage remains null.'}


def markdown_table(comparison):
    lines = ['# Multi-hop Baseline Comparison', '', f"Result kind: `{comparison['result_kind']}`", '',
             '| Method | N | EM | F1 | Document support recall (proxy) | Failed | Model calls | Searches | Reported tokens |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in comparison['rows']:
        tokens = str(row['reported_total_tokens']) if row['reported_total_tokens'] is not None else 'unknown'
        lines.append(f"| {row['method']} | {row['count']} | {row['answer_em']:.4f} | {row['answer_f1']:.4f} | "
                     f"{row['document_support_recall_proxy']:.4f} | {row['failed_predictions']} | "
                     f"{row['total_model_calls']} | {row['total_search_calls']} | {tokens} |")
    lines.extend(['', comparison.get('code_warning', ''), '', comparison['interpretation'], '', comparison['cost_scope'], ''])
    return '\n'.join(lines)
