"""Behavioral checks for offline, immutable reuse of local benchmark records."""
import hashlib
import json
from pathlib import Path
import socket

from PIL import Image
import pytest

from deepsearch_experiments.existing_datasets import rescore_catalog, main


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')


def catalog(root, kind='multimodal'):
    run = root / 'run'
    records = run / ('predictions.jsonl' if kind == 'multimodal' else 'attempts.jsonl')
    entry = {'id': 'fixture-run', 'kind': kind, 'manifest': 'manifest.json',
             'run': 'run', 'manifest_sha256': digest(root / 'manifest.json'),
             'run_sha256': digest(run / 'run.json'), 'records_sha256': digest(records),
             'scope': 'Synthetic test fixture; no model quality claim.'}
    path = root / 'catalog.json'
    save(path, {'schema_version': 1, 'entries': [entry]})
    return path


@pytest.fixture
def visual(tmp_path):
    Image.new('RGB', (10, 10), 'white').save(tmp_path / 'page.png')
    cases = [{'id': ident, 'dataset': 'chartqa', 'split': 'val', 'task': 'chartqa',
              'group': 'human', 'question': ident, 'answers': ['42'],
              'gold_element_ids': [], 'scope': ['p1'], 'gold_pages': ['p1']}
             for ident in ('q1', 'q2', 'q3')]
    save(tmp_path / 'manifest.json', {'schema_version': 1, 'dataset': 'chartqa', 'split': 'val',
         'provenance': {'status': 'unit-fixture'}, 'cases': cases,
         'pages': [{'id': 'p1', 'source': 'benchmark://fixture/1', 'page_number': 1,
                    'image': 'page.png', 'image_sha256': digest(tmp_path / 'page.png'),
                    'text': '', 'elements': []}]})
    run = tmp_path / 'run'
    run.mkdir()
    save(run / 'run.json', {'manifest_sha256': digest(tmp_path / 'manifest.json'),
         'code_sha256': 'historical-code', 'case_ids': ['q1', 'q2'],
         'configuration': {'mode': 'oracle', 'k': 3, 'vision_model': 'historical-model'}})
    (run / 'predictions.jsonl').write_text('\n'.join(json.dumps(row) for row in (
        {'id': 'q1', 'status': 'ok', 'mode': 'oracle', 'answer': '42'},
        {'id': 'q2', 'status': 'error', 'mode': 'oracle', 'error_type': 'TimeoutError'},
    )), encoding='utf-8')
    return tmp_path


def test_exact_subset_denominator_failure_separation_and_no_network(visual, monkeypatch):
    source = catalog(visual)
    before = {path: digest(path) for path in visual.rglob('*') if path.is_file()}

    def no_network(*args, **kwargs):
        pytest.fail('Offline rescoring must never connect to a network')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    report = rescore_catalog(source, visual / 'out', root=visual)
    assert report['status'] == 'scored'
    assert report['model_calls_this_run'] == 0
    summary = report['entries'][0]['summary']
    assert summary['count'] == 2  # q3 was never in this historical run.
    assert summary['error_count'] == 1
    assert summary['relaxed_accuracy'] == .5
    assert report['entries'][0]['historical_model'] == 'historical-model'
    assert report['inputs_unchanged']
    assert before == {path: digest(path) for path in before}
    success = json.loads((visual / 'out/success.jsonl').read_text())
    failure = json.loads((visual / 'out/failure.jsonl').read_text())
    assert success['case_id'] == 'q1' and failure['case_id'] == 'q2'
    assert failure['execution_status'] == 'error'
    assert failure['human_verdict'] is failure['root_causes'] is failure['internal_verifier'] is None
    assert failure['records_sha256'] == digest(visual / 'run/predictions.jsonl')
    assert (visual / 'out/unknown.jsonl').read_bytes() == b''
    frozen = (visual / 'out/report.json').read_bytes()
    with pytest.raises(FileExistsError):
        rescore_catalog(source, visual / 'out', root=visual)
    assert (visual / 'out/report.json').read_bytes() == frozen


def test_missing_prediction_remains_in_denominator_and_unknown(visual):
    path = visual / 'run/predictions.jsonl'
    path.write_text(path.read_text().splitlines()[0])
    report = rescore_catalog(catalog(visual), visual / 'out', root=visual)
    assert report['entries'][0]['summary']['relaxed_accuracy'] == .5
    assert report['entries'][0]['summary']['missing_count'] == 1
    unknown = json.loads((visual / 'out/unknown.jsonl').read_text())
    assert unknown['case_id'] == 'q2' and unknown['execution_status'] == 'missing'
    assert unknown['scores']['relaxed_accuracy'] == 0


@pytest.mark.parametrize('change', ['pin', 'run-manifest', 'duplicate-population', 'unknown-row', 'mode', 'asset'])
def test_invalid_identity_population_or_assets_rejected_without_scoring(visual, change):
    source = catalog(visual)
    meta = visual / 'run/run.json'
    data = json.loads(meta.read_text())
    if change in {'pin', 'run-manifest'}:
        data['manifest_sha256'] = '0' * 64
        save(meta, data)
    elif change == 'duplicate-population':
        data['case_ids'] = ['q1', 'q1']
        save(meta, data)
    elif change in {'unknown-row', 'mode'}:
        row = {'id': 'q3' if change == 'unknown-row' else 'q1', 'status': 'ok',
               'mode': 'rag' if change == 'mode' else 'oracle', 'answer': '42'}
        (visual / 'run/predictions.jsonl').write_text(json.dumps(row))
    else:
        (visual / 'page.png').write_bytes(b'tampered')
    if change != 'pin':
        source = catalog(visual)
    assert main(['--root', str(visual), '--catalog', str(source), '--output', str(visual / 'out')]) == 1
    report = json.loads((visual / 'out/report.json').read_text())
    assert report['status'] == report['entries'][0]['status'] == 'rejected'
    assert not list((visual / 'out/reports').iterdir())
    assert not (visual / 'out/success.jsonl').read_text()


def test_multihop_uses_existing_scorer_and_preserves_all_attempt_costs(tmp_path):
    from deepsearch_experiments.benchmarks.multihop.datasets import import_ircot, write_manifest
    from deepsearch_experiments.benchmarks.multihop.runner import run_experiment
    from deepsearch_experiments.benchmarks.multihop.runtime import RuntimeLimits, ScriptedClient

    questions, corpus = tmp_path / 'questions.jsonl', tmp_path / 'corpus.jsonl'
    save(questions, {'question_id': 'q1', 'question_text': 'Where is Alpha?',
         'answers_objects': [{'number': '', 'date': {'day': '', 'month': '', 'year': ''}, 'spans': ['France']}],
         'contexts': [{'idx': 0, 'title': 'Alpha', 'paragraph_text': 'Alpha is in France.', 'is_supporting': True}]})
    save(corpus, {'title': 'Alpha', 'text': 'Alpha is in France.'})
    manifest = import_ircot(questions, corpus, dataset='musique', split='fixture',
                            corpus_scope='sampled_pool', source_revision='a' * 40)
    write_manifest(manifest, tmp_path / 'manifest.json')
    run = tmp_path / 'run'
    model = {'backend': 'scripted', 'model': 'scripted-offline'}
    run_experiment(manifest, 'logicrag', lambda _: ScriptedClient([]), run, RuntimeLimits(), model)
    expected = run_experiment(manifest, 'logicrag', lambda _: ScriptedClient(
        ['Alpha is in France.', '{"can_answer": true}', 'France']), run, RuntimeLimits(), model,
        resume=True, retry_errors=True)
    before = digest(run / 'attempts.jsonl')
    result = rescore_catalog(catalog(tmp_path, 'multihop'), tmp_path / 'out', root=tmp_path)
    assert result['status'] == 'scored'
    scored = json.loads((tmp_path / 'out/reports/fixture-run.json').read_text())
    assert scored['attempts'] == 2 and scored['total_model_calls'] == expected['total_model_calls'] == 4
    assert scored['reported_total_tokens'] is None
    assert scored['summary'] == expected['summary']
    assert scored['per_case'][0]['latest_attempt'] == 2
    assert scored['per_case'][0]['human_verdict'] is None
    assert before == digest(run / 'attempts.jsonl')
