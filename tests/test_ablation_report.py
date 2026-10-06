import hashlib
import json

import pytest

from deepsearch_agent.benchmarks.ablation_metrics import summarize_ablation
from deepsearch_agent.benchmarks.ablation_report import write_report
from deepsearch_agent.benchmarks.badcases import attribute_cases


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
