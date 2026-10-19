"""Synthetic trajectories test the workflow; these are not real answer quality scores."""
import json
import pytest
from deepsearch_tracing.store import TraceStore, digest
from deepsearch_tracing.quality import freeze, review, compare, read_snapshot


def snapshot(tmp_path, name, *, corpus='corpus-v1', answer=True, identity='matched-runtime'):
    db = tmp_path / (name + '.sqlite3')
    store = TraceStore(db)
    rid = store.start({'question': 'fixture question', 'research_run_id': 'business-' + name,
                       'runtime_identity': identity, 'corpus_identity': corpus})
    store.append(rid, 'rag.outcome', {'status': 'verified'})
    if answer:
        store.append(rid, 'answer.final', {'text': 'fixture', 'sources': []})
    store.finish(rid)
    frozen = freeze(db, tmp_path / name)
    return tmp_path / name / 'snapshot.json', frozen['runs'][0]


def label(row, verdict='incorrect', **kwargs):
    return {'trace_run_id': row['trace_run_id'], 'trace_sha256': row['trace_sha256'],
            'case_id': 'case-001', 'verdict': verdict, 'root_causes': ['hallucination'] if verdict == 'incorrect' else [],
            'expected': 'expected answer', 'evidence': 'reviewed fixture page 1', **kwargs}


def test_internal_verification_never_becomes_human_correctness(tmp_path):
    path, row = snapshot(tmp_path, 'a')
    result = review(path, [label(row)], 'fixture-reviewer', tmp_path / 'review.json')
    assert result['metrics']['internal_outcomes'] == {'internally_verified': 1}
    assert result['metrics']['human_accuracy'] == 0
    assert result['metrics']['badcase_root_causes'] == {'hallucination': 1}


def test_unknown_and_unreviewed_are_excluded_from_accuracy(tmp_path):
    path, row = snapshot(tmp_path, 'a')
    result = review(path, [label(row, 'unknown')], 'fixture-reviewer', tmp_path / 'unknown.json')
    assert result['metrics']['human_accuracy'] is None
    assert result['metrics']['correctness_denominator'] == 0
    assert result['metrics']['review_coverage'] == 1
    result = review(path, [], 'fixture-reviewer', tmp_path / 'empty.json')
    assert result['metrics']['unreviewed_runs'] == 1
    assert result['metrics']['human_accuracy'] is None


def test_matched_reviewed_fix_closes_badcase(tmp_path):
    a, old = snapshot(tmp_path, 'a')
    b, new = snapshot(tmp_path, 'b')
    review(a, [label(old)], 'reviewer', tmp_path / 'a.json')
    review(b, [label(new, 'correct', fix_id='fix-123')], 'reviewer', tmp_path / 'b.json')
    result = compare(tmp_path / 'a.json', tmp_path / 'b.json', tmp_path / 'comparison.json')
    assert result['counts'] == {'resolved': 1}
    assert result['cases'][0]['fix_id'] == 'fix-123'


@pytest.mark.parametrize('options,verdict,fix,state', [
    ({'corpus': 'other-corpus'}, 'correct', 'fix-1', 'incomparable'),
    ({'identity': None}, 'correct', 'fix-1', 'incomparable'),
    ({}, 'unknown', 'fix-1', 'awaiting_review'),
    ({}, 'correct', None, 'awaiting_fix_reference'),
    ({}, 'incorrect', 'fix-1', 'open'),
])
def test_incomplete_or_unmatched_candidates_do_not_close_cases(tmp_path, options, verdict, fix, state):
    a, old = snapshot(tmp_path, 'a')
    b, new = snapshot(tmp_path, 'b', **options)
    review(a, [label(old)], 'reviewer', tmp_path / 'a.json')
    review(b, [label(new, verdict, fix_id=fix)], 'reviewer', tmp_path / 'b.json')
    result = compare(tmp_path / 'a.json', tmp_path / 'b.json', tmp_path / 'comparison.json')
    assert result['cases'][0]['state'] == state


def test_tampering_wrong_binding_and_existing_human_file_are_rejected(tmp_path):
    path, row = snapshot(tmp_path, 'a')
    with pytest.raises(ValueError):
        review(path, [label(row, trace_sha256='not-this-trace')], 'reviewer', tmp_path / 'invalid.json')
    output = tmp_path / 'review.json'
    review(path, [label(row)], 'reviewer', output)
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        review(path, [label(row)], 'reviewer', output)
    assert output.read_bytes() == original
    runs_path = path.with_name('runs.json')
    runs = json.loads(runs_path.read_text(encoding='utf-8'))
    runs[0]['metadata']['question'] = 'changed'
    runs_path.write_text(json.dumps(runs), encoding='utf-8')
    with pytest.raises(ValueError):
        read_snapshot(path)


def test_missing_answer_cannot_be_marked_correct(tmp_path):
    path, row = snapshot(tmp_path, 'a', answer=False)
    with pytest.raises(ValueError):
        review(path, [label(row, 'correct')], 'reviewer', tmp_path / 'invalid.json')


def test_relabelling_same_old_run_cannot_close_a_badcase(tmp_path):
    path, row = snapshot(tmp_path, 'a')
    review(path, [label(row)], 'reviewer', tmp_path / 'before.json')
    review(path, [label(row, 'correct', fix_id='fix-1')], 'reviewer', tmp_path / 'after.json')
    result = compare(tmp_path / 'before.json', tmp_path / 'after.json', tmp_path / 'comparison.json')
    assert result['counts'] == {'awaiting_new_run': 1}
