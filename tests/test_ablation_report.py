import hashlib
import json

import pytest

from deepsearch_experiments.benchmarks.ablation_metrics import summarize_ablation
from deepsearch_experiments.benchmarks.ablation_report import write_report
from deepsearch_experiments.benchmarks.badcases import attribute_cases


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def fixture_run(tmp_path):
    prepared, output = tmp_path / 'prepared', tmp_path / 'output'
    variants = ['fixed_workflow', 'agent_full', 'agent_no_rerank', 'agent_no_ocr', 'agent_no_visual', 'oracle']
    cases = [
        {'id': 'chart', 'dataset': 'chartqa', 'task': 'chartqa', 'group': 'human',
         'question': 'How many | bars?', 'answers': ['10'], 'gold_element_ids': [],
         'gold_pages': ['p1'], 'scope': ['p1']},
        {'id': 'pdf-a', 'dataset': 'pdfvqa', 'task': 'A', 'group': 'existence',
         'question': 'Any table?', 'answers': ['yes'], 'gold_element_ids': [],
         'gold_pages': ['p2'], 'scope': ['p2']},
        {'id': 'pdf-b', 'dataset': 'pdfvqa', 'task': 'B', 'group': 'object_recognition',
         'question': 'Which elements?', 'answers': [], 'gold_element_ids': ['e1', 'e2'],
         'gold_pages': ['p2'], 'scope': ['p2']},
    ]
    hashes = {}
    for dataset in ('chartqa', 'pdfvqa'):
        path = prepared / dataset / 'manifest.json'
        write_json(path, {'dataset': dataset, 'split': 'validation', 'pages': [],
                         'cases': [c for c in cases if c['dataset'] == dataset],
                         'provenance': {'status': 'unit-fixture'}})
        hashes[dataset] = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = []
    for variant in variants:
        for case in cases:
            if variant == 'agent_no_ocr' and case['id'] == 'chart':
                continue
            failed = variant == 'agent_no_visual' and case['id'] == 'pdf-a'
            rows.append({'id': case['id'], 'variant': variant, 'status': 'error' if failed else 'ok',
                'error_type': 'TimeoutError' if failed else None,
                'answer': '' if failed or case['task'] == 'B' else case['answers'][0],
                'element_ids': ['e1'] if case['task'] == 'B' else [],
                'prediction_status': 'valid', 'ranking': case['gold_pages'],
                'evidence_ranking': case['gold_pages'], 'latency_ms': 1000,
                'trace': {'model_calls': [{'stage': 'answerer', 'role': 'Answerer', 'status': 'ok',
                    'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12, 'latency_ms': 900}],
                    'retrieval_calls': [{'status': 'ok', 'ranking': case['gold_pages']}],
                    'rerank_calls': [], 'tool_calls': [], 'events': []}})
    write_json(output / 'run.json', {'schema_version': 1, 'code_sha256': 'f' * 64,
        'manifests': hashes, 'indices': {'chartqa': 'a' * 64, 'pdfvqa': 'b' * 64},
        'case_ids': [c['id'] for c in cases], 'variants': variants,
        'configuration': {'model': 'test-model', 'max_model_calls': 18, 'k': 3,
            'agent': {'max_tool_calls': 6, 'max_ocr_calls': 1},
            'context_window': 32768, 'max_output_tokens': 2048,
            'disabled': ['memory', 'skills', 'external_MCP'],
            'no_visual': 'disable role images only', 'ocr_cache': 'none'}})
    (output / 'predictions.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    summary = summarize_ablation(cases, rows, variants=variants)
    write_json(output / 'summary.json', summary)
    badcases = attribute_cases(cases, rows, oracle_rows=[r for r in rows if r['variant'] == 'oracle'])
    (output / 'badcases.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in badcases), encoding='utf-8')
    return prepared, output


def test_report_uses_actual_denominators_separates_oracle_and_links_traces(tmp_path):
    prepared, output = fixture_run(tmp_path)
    destination = tmp_path / 'docs' / 'ABLATION.md'
    write_report(prepared, output, destination)
    text = destination.read_text(encoding='utf-8')
    assert 'ChartQA 1' in text and 'PDFVQA A 1' in text and 'PDFVQA B 1' in text
    assert 'agent_full' in text and 'agent_no_visual' in text
    assert '100.0% (1/1)' in text and '0.6667 (n=1)' in text
    assert 'oracle' in text and '不保证是上界' in text
    assert '缺失' in text and 'abstention' in text
    assert 'predictions.jsonl#L' in text and '/trace/' in text
    assert 'generation_reading_or_reasoning' in text
    assert '人工复核' in text and '图像嵌入' in text
    assert '显著提升' not in text
    assert 'How many \\| bars?' in text
    assert 'f' * 64 in text
    assert '最后回答上下文金页覆盖' in text
    assert 'PDFVQA A 金标分布' in text
    assert 'ChartQA human' in text
    assert '(&lt;' not in text


def test_report_rejects_changed_manifest_and_stale_scores(tmp_path):
    prepared, output = fixture_run(tmp_path)
    path = output / 'summary.json'
    summary = json.loads(path.read_text(encoding='utf-8'))
    summary['variants']['agent_full']['by_task']['chartqa']['answer_metrics']['relaxed_accuracy']['value'] = .2
    write_json(path, summary)
    with pytest.raises(ValueError, match='summary'):
        write_report(prepared, output, tmp_path / 'report.md')
    write_json(prepared / 'chartqa' / 'manifest.json', {'cases': []})
    with pytest.raises(ValueError, match='manifest'):
        write_report(prepared, output, tmp_path / 'report.md')


def test_no_preparation_trace_is_unknown_and_partial_run_is_explicit(tmp_path):
    prepared, output = fixture_run(tmp_path)
    target = tmp_path / 'report.md'
    write_report(prepared, output, target)
    text = target.read_text(encoding='utf-8')
    assert '17/18' in text and '部分完成' in text
    assert '预处理调用未观测' in text
    assert '未上报' in text


def test_stale_badcase_annotations_rejected(tmp_path):
    prepared, output = fixture_run(tmp_path)
    path = output / 'badcases.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    rows[0]['automatic']['primary_code'] = 'invented'
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    with pytest.raises(ValueError, match='attribution'):
        write_report(prepared, output, tmp_path / 'report.md')


def test_report_supports_frozen_subset_and_does_not_claim_fresh_402(tmp_path):
    prepared, output = fixture_run(tmp_path)
    run = json.loads((output / 'run.json').read_text(encoding='utf-8'))
    run['case_ids'] = ['pdf-a']
    run['variants'] = ['agent_full']
    run['configuration'].update(retrieval={'pdfvqa': 'bm25'},
                                retrieval_fallback_reason='image_embedding_http_402')
    write_json(output / 'run.json', run)
    cases = json.loads((prepared / 'pdfvqa' / 'manifest.json').read_text(encoding='utf-8'))['cases'][:1]
    rows = [json.loads(line) for line in (output / 'predictions.jsonl').read_text(encoding='utf-8').splitlines()]
    rows = [r for r in rows if r['id'] == 'pdf-a' and r['variant'] == 'agent_full']
    (output / 'predictions.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    write_json(output / 'summary.json', summarize_ablation(cases, rows, variants=run['variants']))
    badcases = attribute_cases(cases, rows)
    (output / 'badcases.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in badcases), encoding='utf-8')
    target = tmp_path / 'report.md'
    write_report(prepared, output, target)
    text = target.read_text(encoding='utf-8')
    assert '1/1' in text and '这 1 道题' in text
    assert 'Wilson 95%' in text
    assert '本轮图像 embedding 服务 HTTP 402' not in text
    assert '本轮未包含 oracle' in text
    assert 'chart |' not in text
    run['case_ids'] = ['pdf-a', 'pdf-a']
    write_json(output / 'run.json', run)
    with pytest.raises(ValueError, match='case IDs'):
        write_report(prepared, output, target)


def test_report_links_matching_dag_and_rejects_stale_nodes(tmp_path):
    from deepsearch_experiments.benchmarks.dag import default_task, score_rows
    from deepsearch_experiments.benchmarks.dag_schema import DAGSuite

    prepared, output = fixture_run(tmp_path)
    run = json.loads((output / 'run.json').read_text(encoding='utf-8'))
    cases = [c for dataset in ('chartqa', 'pdfvqa')
             for c in json.loads((prepared / dataset / 'manifest.json').read_text(encoding='utf-8'))['cases']]
    rows = [json.loads(line) for line in (output / 'predictions.jsonl').read_text(encoding='utf-8').splitlines()]
    suite = DAGSuite(name='fixture', manifests=run['manifests'], tasks=[default_task(c) for c in cases])
    summary, results = score_rows(suite, cases, rows, run['variants'])
    summary['provenance'] = {key: hashlib.sha256((output / filename).read_bytes()).hexdigest()
                             for key, filename in [('predictions_sha256', 'predictions.jsonl'), ('run_sha256', 'run.json')]}
    dag_output = tmp_path / 'dag'
    write_json(dag_output / 'summary.json', summary)
    write_json(dag_output / 'suite.json', suite.model_dump())
    nodes = dag_output / 'node-results.jsonl'
    nodes.write_text(''.join(json.dumps(r) + '\n' for r in results), encoding='utf-8')
    target = tmp_path / 'report.md'
    write_report(prepared, output, target, dag_output=dag_output)
    text = target.read_text(encoding='utf-8')
    assert 'DAG 依赖' in text and 'stage_proxy' in text and 'node-results.jsonl' in text
    results[0]['score'] = 99
    nodes.write_text(''.join(json.dumps(r) + '\n' for r in results), encoding='utf-8')
    with pytest.raises(ValueError, match='DAG report scores'):
        write_report(prepared, output, target, dag_output=dag_output)
    summary['provenance']['predictions_sha256'] = 'wrong'
    write_json(dag_output / 'summary.json', summary)
    with pytest.raises(ValueError, match='DAG report provenance'):
        write_report(prepared, output, target, dag_output=dag_output)
