import importlib.util
from pathlib import Path
import sys


def module():
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location('gap_repeat_analysis', scripts / 'evaluate_gap_repeats.py')
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        return result
    finally:
        sys.path.remove(str(scripts))


def pairs():
    return [{'dataset': 'test', 'id': str(i), 'repeat': r, 'v4_tokens': 100, 'v5_tokens': 80}
            for i in range(2) for r in range(3)]


def test_cluster_bootstrap_keeps_three_repeats_as_one_question():
    result = module().stability(pairs())
    assert result['paired_count'] == 6 and result['complete_question_clusters'] == 2
    assert result['question_cluster_bootstrap_95_interval'] == [0.2, 0.2]
    assert all(r['reduction_fraction'] == 0.2 for r in result['per_repeat'])


def test_unknown_usage_is_not_zero_and_excludes_incomplete_cost_cluster():
    rows = pairs(); rows[0]['v5_tokens'] = None
    result = module().stability(rows)
    assert result['known_usage_pairs'] == 5 and result['complete_question_clusters'] == 1
    assert result['per_repeat'][0]['v5_tokens'] is None
    assert result['per_repeat'][0]['reduction_fraction'] is None


def test_empty_sample_has_no_false_stability_claim():
    result = module().stability([])
    assert result['paired_cost_win_fraction'] is None
    assert result['question_cluster_bootstrap_95_interval'] is None


def test_quality_excludes_quarantined_gold_and_separates_risk_from_causality():
    rows=[{'dataset':'test','id':str(i),'repeat':r,'gold_excluded':i==0,
           'v4_em':1.0,'v5_em':0.0,'v5_deferred_paths':int(i==1)}
          for i in range(2) for r in range(3)]
    result=module().quality(rows)
    assert result['eligible_pairs']==3 and result['question_clusters']==1
    assert result['correct_to_wrong_with_defer']==3
    assert result['accuracy_delta_cluster_bootstrap_95_interval']==[-1.0,-1.0]
