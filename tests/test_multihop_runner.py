from dataclasses import replace
import json

import pytest

from deepsearch_experiments.benchmarks.multihop.datasets import import_ircot, write_manifest
from deepsearch_experiments.benchmarks.multihop.runner import run_experiment, score_run
from deepsearch_experiments.benchmarks.multihop.runtime import RuntimeLimits, ScriptedClient
from deepsearch_experiments.benchmarks.multihop.__main__ import main
from deepsearch_experiments.benchmarks.multihop.compare import compare_runs, markdown_table
from deepsearch_agent.core.models import ModelServiceError


@pytest.fixture
def manifest(tmp_path):
    questions = tmp_path / 'questions.jsonl'
    corpus = tmp_path / 'corpus.jsonl'
    questions.write_text(json.dumps({
        'question_id': 'q1', 'question_text': 'Where is Alpha?',
        'answers_objects': [{'number': '', 'date': {'day': '', 'month': '', 'year': ''}, 'spans': ['France']}],
        'contexts': [{'idx': 0, 'title': 'Alpha', 'paragraph_text': 'Alpha is in France.', 'is_supporting': True}],
        'reasoning_steps': ['EVALUATOR_ONLY_CANARY'],
    }), encoding='utf-8')
    corpus.write_text(json.dumps({'title': 'Alpha', 'text': 'Alpha is in France.'}), encoding='utf-8')
    return import_ircot(questions, corpus, dataset='musique', split='fixture',
                        corpus_scope='sampled_pool', source_revision='a' * 40)


def factory(ident):
    return ScriptedClient(['Alpha is in France.', '{"can_answer": true}', 'France'])


MODEL = {'backend': 'scripted', 'model': 'scripted-offline'}


def test_run_score_resume_and_label_isolation(manifest, tmp_path):
    path = tmp_path / 'run'
    report = run_experiment(manifest, 'logicrag', factory, path, RuntimeLimits(), MODEL)
    assert report['summary']['answer_em'] == 1
    assert report['total_model_calls'] == 3
    assert report['result_kind'] == 'offline-plumbing-only'
    assert score_run(manifest, path)['summary'] == report['summary']
    before = (path / 'attempts.jsonl').read_bytes()
    resumed = run_experiment(manifest, 'logicrag', lambda _: pytest.fail('must not rerun'),
                             path, RuntimeLimits(), MODEL, resume=True)
    assert resumed['attempts'] == 1
    assert before == (path / 'attempts.jsonl').read_bytes()
    assert 'EVALUATOR_ONLY_CANARY' not in before.decode()
    assert not (path / 'run.lock').exists()


def test_resume_rejects_configuration_changes(manifest, tmp_path):
    path = tmp_path / 'run'
    run_experiment(manifest, 'logicrag', factory, path, RuntimeLimits(), MODEL)
    with pytest.raises(ValueError, match='identity mismatch'):
        run_experiment(manifest, 'logicrag', factory, path, RuntimeLimits(k=2), MODEL, resume=True)
    with pytest.raises(ValueError, match='output exists'):
        run_experiment(manifest, 'logicrag', factory, path, RuntimeLimits(), MODEL)
    assert not (path / 'run.lock').exists()


def test_retry_keeps_failed_attempt_and_cost(manifest, tmp_path):
    path = tmp_path / 'run'
    failed = run_experiment(manifest, 'logicrag', lambda _: ScriptedClient([]), path, RuntimeLimits(), MODEL)
    assert failed['summary']['failed_predictions'] == 1
    assert failed['summary']['answer_em'] == 0
    report = run_experiment(manifest, 'logicrag', factory, path, RuntimeLimits(), MODEL, resume=True, retry_errors=True)
    assert report['attempts'] == 2
    assert report['summary']['answer_em'] == 1
    assert report['total_model_calls'] == 4
    assert report['reported_total_tokens'] is None


def test_hard_budget_is_not_silent_final_answer(manifest, tmp_path):
    report = run_experiment(manifest, 'logicrag', factory, tmp_path / 'run', RuntimeLimits(model_calls=1), MODEL)
    assert report['per_case'][0]['status'] == 'budget_exhausted'
    assert report['total_model_calls'] == 1
    assert report['summary']['answer_em'] == 0
    assert report['summary']['document_support_recall_proxy'] == 1


def test_interrupted_attempt_saved_and_requires_explicit_retry(manifest, tmp_path):
    class Interrupted:
        def generate(self, prompt):
            raise KeyboardInterrupt
    path = tmp_path / 'run'
    with pytest.raises(KeyboardInterrupt):
        run_experiment(manifest, 'logicrag', lambda _: Interrupted(), path, RuntimeLimits(), MODEL)
    assert score_run(manifest, path)['per_case'][0]['status'] == 'interrupted'
    assert not (path / 'run.lock').exists()


def test_duplicate_attempts_and_tampered_manifest_rejected(manifest, tmp_path):
    path = tmp_path / 'run'
    run_experiment(manifest, 'logicrag', factory, path, RuntimeLimits(), MODEL)
    altered = manifest.model_copy(update={'seed': 123})
    with pytest.raises(ValueError, match='Manifest/run'):
        score_run(altered, path)
    raw = (path / 'attempts.jsonl').read_text()
    (path / 'attempts.jsonl').write_text(raw + raw)
    with pytest.raises(ValueError, match='duplicate attempt'):
        score_run(manifest, path)


def test_no_secret_identity_or_demo_label_overlap(manifest, tmp_path):
    with pytest.raises(ValueError, match='non-secret'):
        run_experiment(manifest, 'logicrag', factory, tmp_path / 'run', RuntimeLimits(), {**MODEL, 'api_key': 'secret'})
    prompt = '# METADATA: {"qid":"q1"}\nQ: Example\nA: Example'
    with pytest.raises(ValueError, match='overlap'):
        run_experiment(manifest, 'ircot', factory, tmp_path / 'run', RuntimeLimits(), MODEL, prompt=prompt)


def test_cli_offline_tape_and_remote_opt_in(manifest, tmp_path):
    source = tmp_path / 'manifest.json'
    write_manifest(manifest, source)
    responses = tmp_path / 'tape.json'
    responses.write_text(json.dumps({'q1': factory('q1').responses}))
    output = tmp_path / 'run'
    main(['run', '--manifest', str(source), '--method', 'logicrag', '--output', str(output), '--responses', str(responses)])
    assert score_run(manifest, output)['summary']['answer_em'] == 1
    with pytest.raises(SystemExit):
        main(['run', '--manifest', str(source), '--method', 'logicrag', '--output', str(tmp_path / 'forbidden')])


def test_existing_writer_lock_not_removed(manifest, tmp_path):
    path = tmp_path / 'run'
    path.mkdir()
    lock = path / 'run.lock'
    lock.write_text('another writer')
    with pytest.raises(FileExistsError):
        run_experiment(manifest, 'logicrag', factory, path, RuntimeLimits(), MODEL)
    assert lock.read_text() == 'another writer'


def test_comparison_requires_matching_conditions_and_marks_mock(manifest, tmp_path):
    logic = tmp_path / 'logic'
    ircot = tmp_path / 'ircot'
    run_experiment(manifest, 'logicrag', factory, logic, RuntimeLimits(), MODEL)
    run_experiment(manifest, 'ircot', lambda _: ScriptedClient(['The answer is France.', 'The answer is France.']),
                   ircot, RuntimeLimits(), MODEL, prompt='Q: Demo?\nA: The answer is demo.')
    compared = compare_runs(manifest, [logic, ircot])
    assert compared['result_kind'] == 'offline-plumbing-only'
    assert 'unknown' in markdown_table(compared)
    assert all(row['answer_em'] == 1 for row in compared['rows'])
    with pytest.raises(ValueError, match='distinct'):
        compare_runs(manifest, [logic, logic])
    altered = tmp_path / 'different'
    run_experiment(manifest, 'ircot', lambda _: ScriptedClient(['The answer is France.', 'France']),
                   altered, RuntimeLimits(k=1), MODEL, prompt='Q: Demo?\nA: Demo.')
    with pytest.raises(ValueError, match='different'):
        compare_runs(manifest, [logic, altered])
    with pytest.raises(ValueError, match='different'):
        compare_runs(manifest, [logic, altered], historical_reference=True)


def test_historical_reference_must_be_explicit(manifest, tmp_path, monkeypatch):
    import deepsearch_experiments.benchmarks.multihop.runner as runner
    logic, ircot = tmp_path / 'logic', tmp_path / 'ircot'
    run_experiment(manifest, 'logicrag', factory, logic, RuntimeLimits(), MODEL)
    monkeypatch.setattr(runner, 'implementation_identity', lambda: {'sha256': 'different'})
    run_experiment(manifest, 'ircot', lambda _: ScriptedClient(['The answer is France.', 'France']),
                   ircot, RuntimeLimits(), MODEL, prompt='Q: Demo?\nA: Demo.')
    with pytest.raises(ValueError, match='different'):
        compare_runs(manifest, [logic, ircot])
    report = compare_runs(manifest, [logic, ircot], historical_reference=True)
    assert report['result_kind'] == 'historical-code-version-comparison'
    assert not report['code_identity_checked']
    assert report['rows'][0]['implementation'] != report['rows'][1]['implementation']


def test_mainline_runner_label_isolation(manifest, tmp_path):
    from test_multihop_mainline import happy_tape
    path = tmp_path / 'mainline'
    tape = [value.replace('Alice', 'Alpha').replace('Paris', 'France') for value in happy_tape()]
    report = run_experiment(manifest, 'mainline', lambda _: ScriptedClient(tape),
                            path, RuntimeLimits(context_window=131072), MODEL)
    assert report['summary']['failed_predictions'] == 0
    assert report['total_model_calls'] == 8
    assert 'EVALUATOR_ONLY_CANARY' not in (path / 'attempts.jsonl').read_text(encoding='utf-8')


def test_service_error_details_and_usage_survive_persistence(manifest, tmp_path):
    class Failed:
        def generate(self, prompt):
            raise ModelServiceError('secret provider body', error_code='output_truncated',
                                    provider_status_code=200, finish_reason='length', usage={'total_tokens': 12})
    path = tmp_path / 'failed'
    report = run_experiment(manifest, 'logicrag', lambda _: Failed(), path, RuntimeLimits(), MODEL)
    assert report['summary']['failed_predictions'] == 1
    assert report['summary']['answer_em'] == 0
    assert report['reported_total_tokens'] == 12
    assert report['usage_missing_calls'] == 0
    row = json.loads((path / 'attempts.jsonl').read_text(encoding='utf-8'))
    assert row['details']['error_code'] == 'output_truncated'
    assert row['details']['provider_status_code'] == 200
    assert row['events'][-1]['reported_total_tokens'] == 12
    assert 'secret provider body' not in json.dumps(row)
    assert score_run(manifest, path) == report
