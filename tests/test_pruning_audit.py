from deepsearch_agent.core.evidence_paths import inspect_candidates
from deepsearch_experiments.benchmarks.multihop.pruning_audit import audit_pruning
from test_evidence_paths import candidate, path, EVIDENCE


def test_gold_proxy_cannot_masquerade_as_true_false_prune_rate():
    event = {'kind': 'mainline.candidates.proposed', 'evidence': EVIDENCE,
             'candidates': inspect_candidates([candidate([path(citation=999)]),
                                               candidate([], answer='Berlin')], EVIDENCE, hard_pruning=True)}
    result = audit_pruning({'events': [event]}, ['Paris'], 'musique')
    assert result['candidate_oracle_em_proxy'] == 1
    assert result['gold_candidate_all_paths_pruned_risk_proxy'] == 1
    assert result['true_false_prune_rate'] is None and result['certificate_replay_failures'] == 0
    result = audit_pruning({'events': [event]}, ['Paris'], 'musique', exclude_gold=True)
    assert result['candidate_oracle_em_proxy'] is None
    assert result['gold_candidate_all_paths_pruned_risk_proxy'] is None


def test_empty_denominators_are_unknown_not_zero_success():
    result = audit_pruning({'events': []}, ['Paris'], 'musique')
    assert result['candidate_oracle_em_proxy'] is None
    assert result['gold_candidate_all_paths_pruned_risk_proxy'] is None
