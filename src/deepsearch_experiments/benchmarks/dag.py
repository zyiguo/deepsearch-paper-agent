"""Offline task-DAG adaptation and scoring for recorded Agentic RAG experiments."""
import argparse
from collections import Counter
import json
from pathlib import Path

from deepsearch_experiments.benchmarks.ablation_metrics import summarize_ablation
from deepsearch_experiments.benchmarks.badcases import attribute_case
from deepsearch_experiments.benchmarks.dag_metrics import evaluate_dag, summarize_dags
from deepsearch_experiments.benchmarks.dag_schema import DAGSuite, TaskDAG, fingerprint
from deepsearch_experiments.benchmarks.schema import load_manifest, sha256
from deepsearch_agent.adapters.persistence.local_files import atomic_write


def default_task(case):
    """A transparent migration proxy, not invented intermediate-task labels."""
    if not case['gold_pages']:
        raise ValueError('Stage-proxy DAG requires annotated gold pages')
    nodes = [
        {'id': 'evidence', 'description': 'Collect annotated source pages (page overlap proxy).',
         'rule': {'kind': 'set', 'field': 'evidence_ranking', 'expected': case['gold_pages']},
         'weight': .2, 'failure_tag': 'evidence_collection'},
        {'id': 'context', 'description': 'Include annotated pages in the final answer context (page proxy).',
         'depends_on': ['evidence'],
         'rule': {'kind': 'set', 'field': 'context_ranking', 'expected': case['gold_pages']},
         'weight': .2, 'failure_tag': 'context_assembly'},
        {'id': 'answer', 'description': 'Pass the original dataset answer metric.',
         'depends_on': ['context'], 'rule': {'kind': 'answer'},
         'weight': .5, 'failure_tag': 'answer_correctness'},
        {'id': 'citations', 'description': 'Cited pages overlap annotated pages (not semantic faithfulness).',
         'depends_on': ['context'],
         'rule': {'kind': 'set', 'field': 'ranking', 'expected': case['gold_pages'], 'metric': 'precision'},
         'weight': .1, 'failure_tag': 'citation_page_precision'},
        {'id': 'tool_result', 'description': 'At least one successful evidence tool returned a gold page (diagnostic).',
         'rule': {'kind': 'tool', 'names': ['search_evidence', 'read_page', 'ocr_page', 'get_evidence'],
                  'expected_pages': case['gold_pages']},
         'weight': 0., 'required': False, 'failure_tag': 'tool_result'},
    ]
    if case['gold_element_ids']:
        nodes.append({'id': 'element_text', 'description': 'Gold element IDs appear in context text (diagnostic; image reading is allowed).',
                      'rule': {'kind': 'set', 'field': 'context_element_ids', 'expected': case['gold_element_ids']},
                      'weight': 0., 'required': False, 'failure_tag': 'context_element_coverage'})
    return TaskDAG(case_id=case['id'], case_sha256=fingerprint(case),
                   annotation_level='stage_proxy', annotation_status='generated', nodes=nodes)


def load_population(prepared, run_path):
    prepared, run_path = Path(prepared), Path(run_path)
    manifests = [load_manifest(prepared / name / 'manifest.json') for name in ('chartqa', 'pdfvqa')]
    identities = {m['dataset']: m['_manifest_sha256'] for m in manifests}
    run = json.loads((run_path / 'run.json').read_text(encoding='utf-8'))
    if run.get('manifests') != identities:
        raise ValueError('Run/manifest fingerprint mismatch')
    indices = {name: sha256(prepared / name / 'index.json') for name in identities}
    if run.get('indices') != indices:
        raise ValueError('Run/index fingerprint mismatch')
    case_map = {c['id']: c for m in manifests for c in m['cases']}
    ids, variants = run.get('case_ids', []), run.get('variants', [])
    if (not ids or len(set(ids)) != len(ids) or not set(ids) <= case_map.keys()
            or not variants or not all(isinstance(v, str) and v for v in variants) or len(set(variants)) != len(variants)):
        raise ValueError('Invalid planned population')
    return [case_map[key] for key in ids], variants, identities, run


def score_rows(suite, cases, rows, variants):
    case_map, tasks = {c['id']: c for c in cases}, {task.case_id: task for task in suite.tasks}
    if set(case_map) != set(tasks) or len(case_map) != len(cases):
        raise ValueError('DAG suite must match the planned case population exactly')
    if not variants or len(set(variants)) != len(variants):
        raise ValueError('Empty or duplicate planned variants')
    by_key, locators = {}, {}
    for line, row in enumerate(rows, 1):
        key = row.get('id'), row.get('variant')
        if key in by_key or key[0] not in case_map or key[1] not in variants:
            raise ValueError('Duplicate or unplanned prediction row')
        by_key[key], locators[key] = row, f'predictions.jsonl#L{line}'
    results = []
    for variant in variants:
        if variant in suite.excluded_variants:
            continue
        for case in cases:
            key = case['id'], variant
            row = by_key.get(key)
            result = evaluate_dag(tasks[case['id']], case, row)
            result.update(variant=variant, trace_locator=locators.get(key))
            tag = attribute_case(case, row)
            result['legacy_badcase'] = {name: tag[name] for name in ('primary_code', 'evidence_level', 'manual_review_required')}
            results.append(result)
    if not results:
        raise ValueError('No DAG-eligible variants')
    summary = {'schema_version': 1, 'suite_sha256': fingerprint(suite.model_dump()),
               'population': 'all_planned_cases_including_missing_predictions',
               'annotation_levels': dict(Counter(t.annotation_level for t in suite.tasks)),
               'excluded_variants': [v for v in variants if v in suite.excluded_variants],
               'score_policy': 'node_weighted_per_case; unknown_and_blocked_credit_zero; task_macro_average',
               'attribution': 'dependency_failure_frontier_not_proven_causal_root',
               'variants': summarize_dags(results),
               'legacy_metrics': summarize_ablation(cases, rows, variants=variants)}
    return summary, results


def render_report(summary, results, suite):
    lines = ['# Task DAG evaluation', '',
             'Offline re-scoring of recorded trajectories; no new model calls.', '',
             f"Suite SHA256: `{summary['suite_sha256']}`", '',
             'Generated graphs are stage proxies. Page overlap does not establish semantic support. '
             'The dependency failure frontier is a diagnostic, not a causal claim.', '',
             'Unknown observations and blocked nodes receive zero credit; neither is counted as an observed failure. '
             'Original dataset metrics remain in summary.json under legacy_metrics.', '',
             '| Variant | Task | N | DAG success | Weighted score | Raw score | Observation coverage |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', '')
    for variant, tasks in summary['variants'].items():
        for name, item in tasks.items():
            lines.append(f"| {cell(variant)} | {cell(name)} | {item['count']} | {item['success_count']}/{item['count']} | "
                         f"{item['mean_score']:.3f} | {item['mean_raw_score']:.3f} | {item['observation_coverage']:.1%} |")
    lines += ['', '## Dependency graph', '', 'The first case is shown below; every per-case graph is stored in suite.json.', '', '```mermaid', 'flowchart LR']
    for node in suite.tasks[0].nodes:
        lines.append(f'    {node.id}["{node.id} ({node.dependency_mode})"]')
        lines.extend(f'    {parent} --> {node.id}' for parent in node.depends_on)
    lines += ['```', '', '## Cases requiring review', '',
              '| Case | Variant | Failure frontier | Blocked | Unknown | Existing badcase tag |',
              '| --- | --- | --- | --- | --- | --- |']
    for result in results:
        if not result['success']:
            values = [result['id'], result['variant'], ', '.join(result['failure_frontier']),
                      ', '.join(result['blocked_nodes']), ', '.join(result['unknown_nodes']),
                      result['legacy_badcase']['primary_code']]
            lines.append('| ' + ' | '.join(cell(v) for v in values) + ' |')
    return '\n'.join(lines) + '\n'


def write_json(path, value):
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))


def score_run(prepared, run_path, suite_path, output):
    output = Path(output)
    if output.exists():
        raise ValueError('Output exists; choose a new directory')
    suite = DAGSuite.model_validate_json(Path(suite_path).read_text(encoding='utf-8'))
    cases, variants, identities, run = load_population(prepared, run_path)
    if suite.manifests != identities:
        raise ValueError('DAG/manifest fingerprint mismatch')
    predictions = Path(run_path) / 'predictions.jsonl'
    rows = [json.loads(line) for line in predictions.read_text(encoding='utf-8').splitlines() if line.strip()]
    summary, results = score_rows(suite, cases, rows, variants)
    evaluator_files = ['dag.py', 'dag_schema.py', 'dag_metrics.py', 'metrics.py', 'schema.py', 'badcases.py', 'ablation_metrics.py']
    summary['provenance'] = {'run_sha256': sha256(Path(run_path) / 'run.json'),
                             'predictions_sha256': sha256(predictions), 'source_code_sha256': run.get('code_sha256'),
                             'evaluator_sha256': fingerprint({name: sha256(Path(__file__).with_name(name)) for name in evaluator_files}),
                             'suite_file_sha256': sha256(suite_path), 'manifests': identities,
                             'indices': run['indices'], 'source_run': str(Path(run_path).resolve())}
    write_json(output / 'summary.json', summary)
    write_json(output / 'suite.json', suite.model_dump())
    atomic_write(output / 'node-results.jsonl', ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n'
                                                      for r in results).encode('utf-8'))
    atomic_write(output / 'report.md', render_report(summary, results, suite).encode('utf-8'))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'validate', 'score'])
    parser.add_argument('--prepared', type=Path)
    parser.add_argument('--run', type=Path)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'validate':
        suite = DAGSuite.model_validate_json(args.suite.read_text(encoding='utf-8'))
        print(json.dumps({'valid': True, 'tasks': len(suite.tasks), 'suite_sha256': fingerprint(suite.model_dump())}))
        return
    if args.prepared is None or args.run is None:
        parser.error('--prepared and --run are required')
    if args.command == 'init':
        if args.suite.exists():
            parser.error('Suite already exists; choose a new file')
        cases, _, identities, _ = load_population(args.prepared, args.run)
        suite = DAGSuite(name='public-multimodal-stage-dag-v1', manifests=identities,
                         tasks=[default_task(case) for case in cases])
        write_json(args.suite, suite.model_dump())
        print(json.dumps({'suite': str(args.suite), 'tasks': len(suite.tasks), 'annotation_level': 'stage_proxy'}))
    else:
        if args.output is None:
            parser.error('--output required')
        result = score_run(args.prepared, args.run, args.suite, args.output)
        print(json.dumps({'output': str(args.output), 'variants': list(result['variants']), 'cloud_calls': 0}))


if __name__ == '__main__':
    main()
