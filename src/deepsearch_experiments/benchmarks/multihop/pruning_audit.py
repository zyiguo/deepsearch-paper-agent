"""Post-run path audit; labels never enter generation, retrieval, or pruning."""
from deepsearch_agent.core.evidence_paths import replay_certificate
from .metrics import answer_scores


def audit_pruning(attempt, answers, dataset, *, exclude_gold=False):
    proposed = [e for e in attempt['events'] if e['kind'] == 'mainline.candidates.proposed']
    reviewed = [e for e in attempt['events'] if e['kind'] == 'mainline.candidates.reviewed']
    paths = pruned = replay_failures = missing = candidate_observations = 0
    gold_candidates = gold_all_paths_pruned = 0
    oracle = False
    for event in proposed:
        for candidate in event['candidates']:
            candidate_observations += 1
            match = not exclude_gold and answer_scores(candidate['answer'], answers, dataset)['answer_em'] == 1
            oracle |= match
            if match and candidate['paths']:
                gold_candidates += 1
                gold_all_paths_pruned += int(all(p['pruned'] for p in candidate['paths']))
            for path in candidate['paths']:
                paths += 1
                missing += int(path['certificate']['status'] == 'unknown')
                pruned += int(path['pruned'])
                replay_failures += int(not replay_certificate(path['certificate'],
                                            {'steps': path['steps']}, event['evidence']))
    return {'candidate_round_observations': candidate_observations, 'path_round_observations': paths,
            'pruned_paths': pruned, 'missing_witness_paths_retained': missing,
            'certificate_replay_failures': replay_failures,
            'gold_excluded': exclude_gold, 'candidate_oracle_em_proxy': None if exclude_gold or not proposed else float(oracle),
            'gold_candidate_with_path_observations': gold_candidates,
            'gold_candidate_all_paths_pruned_observations': gold_all_paths_pruned,
            'gold_candidate_all_paths_pruned_risk_proxy': gold_all_paths_pruned / gold_candidates if gold_candidates else None,
            'true_false_prune_rate': None, 'reviewed_correct_path_denominator': 0,
            'false_prune_unavailable_reason': 'No independent human labels of path correctness; exact answer match is not path truth.',
            'final_candidate_states': [c['status'] for c in reviewed[-1]['candidates']] if reviewed else [],
            'human_verdict': None, 'human_root_causes': None}
