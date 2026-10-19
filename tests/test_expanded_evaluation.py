import importlib.util
import json
from pathlib import Path
import socket

import pytest

from deepsearch_experiments.benchmarks.multihop.datasets import write_manifest
from deepsearch_experiments.benchmarks.multihop.runner import run_experiment
from deepsearch_experiments.benchmarks.multihop.runtime import RuntimeLimits, ScriptedClient
from deepsearch_experiments.benchmarks.multihop.schema import (
    AgentCase, BenchmarkManifest, Document, GoldCase, SourceFile, case_fingerprint, document_id,
)
from deepsearch_experiments.identity import source_fingerprint


spec = importlib.util.spec_from_file_location('expanded_eval', Path(__file__).resolve().parents[1] / 'scripts/evaluate_expanded_multihop.py')
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


def test_sampling_is_novel_deduplicated_proportional_and_order_independent():
    cases = [AgentCase(id=f'q{i}', question=question) for i, question in enumerate(
        ['Already tested', 'Alpha?', 'ALPHA? ', 'Beta?', 'Gamma?', 'Delta?', 'Epsilon?'])]
    strata = {case.id: 'A' if int(case.id[1:]) < 4 else 'B' for case in cases}
    first = batch.select_cases(cases, strata, cases[:1], 4, 18)
    assert first == batch.select_cases(list(reversed(cases)), strata, cases[:1], 4, 18)
    ids, allocation, available = first
    assert len(ids) == 4 and available == 5
    assert 'q0' not in ids and 'q2' not in ids
    assert allocation == {'A': 2, 'B': 2}
    assert len({batch.question_key(case.question) for case in cases if case.id in ids}) == 4
    with pytest.raises(ValueError, match='Not enough'):
        batch.select_cases(cases, strata, cases[:1], 6, 18)
    with pytest.raises(ValueError, match='positive'):
        batch.select_cases(cases, strata, [], 0, 18)


def make_batch(tmp_path, *, complete=True, different_limits=False):
    doc = Document(id=document_id('Alpha', 'Alpha is in France.'), title='Alpha', text='Alpha is in France.')
    cases = [AgentCase(id=f'q{i}', question=f'Where is Alpha number {i}?') for i in range(4)]
    gold = [GoldCase(id=case.id, answers=['France'], support_document_ids=[doc.id],
                    case_sha256=case_fingerprint('musique', 'fixture', case, ['France'], [doc.id])) for case in cases]
    manifest = BenchmarkManifest(dataset='musique', split='fixture', source_revision='a'*40,
        corpus_scope='sampled_pool', available_cases=4, cases=cases, gold=gold, documents=[doc],
        sources=[SourceFile(kind=kind, filename=kind+'.json', sha256='a'*64) for kind in ('questions', 'corpus')])
    write_manifest(manifest, tmp_path / 'all.json')
    info = {'manifest': 'all.json', 'manifest_sha256': batch.sha(tmp_path / 'all.json'),
            'case_ids': [case.id for case in cases], 'allocation': {'A': 2, 'B': 2},
            'strata': {'q0':'A', 'q1':'A', 'q2':'B', 'q3':'B'}}
    plan = {'source_sha256': source_fingerprint(), 'seed': 18, 'inputs': {},
            'datasets': {'musique': info}, 'jobs': []}
    for index in range(2):
        ids = [case.id for case in cases[index*2:index*2+2]]
        shard = batch.subset(manifest, ids, 18)
        path = tmp_path / f'shard{index}.json'
        write_manifest(shard, path)
        job = {'id':f'shard{index}', 'dataset':'musique', 'manifest':path.name,
               'manifest_sha256':batch.sha(path), 'case_ids':ids, 'run':f'run{index}'}
        plan['jobs'].append(job)
        if index == 1 and not complete:
            continue
        factory = lambda ident: ScriptedClient(['Alpha is in France.', '{"can_answer":true}', 'France']
                                               if ident in {'q0','q2'} else [])
        run_experiment(shard, 'logicrag', factory, tmp_path/job['run'],
                       RuntimeLimits(model_calls=3 if index and different_limits else 24),
                       {'backend':'scripted','model':'fixture'})
        if index == 0:
            info['expected_runtime'] = {key:value for key,value in batch.read(tmp_path/job['run']/'run.json').items()
                                        if key != 'manifest_sha256'}
    batch.write(tmp_path/'plan.json', plan)
    return plan


def test_complete_batch_preserves_failures_costs_and_unknown_human_labels(tmp_path, monkeypatch):
    make_batch(tmp_path)
    monkeypatch.setattr(socket.socket, 'connect', lambda *args: pytest.fail('No network during score'))
    result = batch.summarize(tmp_path, tmp_path/'scored')
    summary = result['datasets']['musique']
    assert result['status'] == 'completed' and result['population'] == 4
    assert summary['answer_em'] == .5 and summary['failed_predictions'] == 2
    assert summary['total_model_calls'] == 8 and summary['reported_total_tokens'] is None
    assert result['automatic_outcome_counts'] == {'success':2,'failure':2}
    assert result['human_reviewed'] == 0 and result['semantic_distortion_rate'] is None
    failed = [json.loads(line) for line in (tmp_path/'scored/failure.jsonl').read_text().splitlines()]
    assert all(row['human_verdict'] is None and row['root_causes'] is None for row in failed)
    frozen = (tmp_path/'scored/summary.json').read_bytes()
    with pytest.raises(FileExistsError):
        batch.summarize(tmp_path, tmp_path/'scored')
    assert frozen == (tmp_path/'scored/summary.json').read_bytes()


def test_missing_shard_stays_unknown_and_in_full_population_denominator(tmp_path):
    make_batch(tmp_path, complete=False)
    result = batch.summarize(tmp_path, tmp_path/'scored')
    assert result['status'] == 'incomplete'
    assert result['datasets']['musique']['count'] == 4
    assert result['datasets']['musique']['answer_em'] == .25
    assert result['datasets']['musique']['missing_predictions'] == 2
    assert len((tmp_path/'scored/unknown.jsonl').read_text().splitlines()) == 2


@pytest.mark.parametrize('tamper', ['assignment', 'manifest', 'source'])
def test_changed_input_or_overlapping_population_rejected(tmp_path, tamper):
    plan = make_batch(tmp_path)
    if tamper == 'assignment':
        plan['jobs'][1]['case_ids'] = plan['jobs'][0]['case_ids']
    elif tamper == 'manifest':
        with (tmp_path/'all.json').open('a') as handle:
            handle.write(' ')
    else:
        source = tmp_path/'source.json'
        source.write_text('original')
        plan['inputs'][str(source)] = batch.sha(source)
        source.write_text('changed')
    (tmp_path/'plan.json').write_text(json.dumps(plan))
    with pytest.raises(ValueError):
        batch.summarize(tmp_path, tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_different_shard_conditions_rejected(tmp_path):
    make_batch(tmp_path, different_limits=True)
    with pytest.raises(ValueError, match='conditions differ'):
        batch.summarize(tmp_path, tmp_path/'out')


def test_live_execution_requires_explicit_flag(tmp_path):
    with pytest.raises(SystemExit):
        batch.main(['run', '--root', str(tmp_path)])
