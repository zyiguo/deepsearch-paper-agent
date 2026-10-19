import json
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from deepsearch_agent.core.evidence_paths import (check_path, inspect_candidates, merge_candidate_pool,
                                                replay_certificate, resolve_candidates)
from deepsearch_agent.agents.evidence_policy import EvidencePolicy
from deepsearch_agent.agents.runtime import ResearchWorkflow
from deepsearch_agent.agents.agent_config import AgentSettings
from deepsearch_experiments.benchmarks.multihop.mainline import run_mainline
from deepsearch_experiments.benchmarks.multihop.runtime import BudgetExceeded
from test_evidence_policy import tape
from test_multihop_mainline import runtime


EVIDENCE = [{'id': 1, 'text': 'Alice was born in Paris.', 'chunk_id': 'a'}]


def path(**changes):
    return {'steps': [{'requirement_index': 0, 'claim': 'Alice birthplace is Paris',
                       'citation': 1, 'quote': 'Alice was born in Paris.', **changes}]}


def candidate(paths=None, answer='Paris'):
    return {'answer': answer, 'interpretation': 'Alice birthplace',
            'paths': [path()] if paths is None else paths}


def review(paths=None, **changes):
    return {'paths': [{'candidate_index': 0, 'path_index': 0, 'verdict': 'supported',
                       'supported_requirements': [0], 'reason': 'The cited sentence supports birthplace.'}]
            if paths is None else paths, 'selected_candidate': 0, 'answer_resolved': True,
            'issues': [], 'queries': [], **changes}


@pytest.mark.parametrize('changes,expected', [({}, 'witness_valid'), ({'citation': 99}, 'invalid_witness'),
    ({'quote': 'Alice was born in Berlin.'}, 'invalid_witness'), ({'quote': ''}, 'unknown'),
    ({'quote': '   '}, 'unknown')])
def test_integrity_is_not_entailment(changes, expected):
    result = check_path(path(**changes), EVIDENCE)
    assert result['status'] == expected and not result['semantic_entailment_checked']
    assert replay_certificate(result, path(**changes), EVIDENCE)


def test_certificate_expiration_and_forgery():
    p = path()
    certificate = check_path(p, EVIDENCE)
    assert not replay_certificate(certificate, p, [{'id': 1, 'text': 'Different'}])
    assert not replay_certificate({**certificate, 'status': 'invalid_witness'}, p, EVIDENCE)
    assert not replay_certificate(certificate, path(claim='Changed claim'), EVIDENCE)


@pytest.mark.parametrize('paths', [[], [{'steps': []}], [path(quote='')]])
def test_insufficient_evidence_kept_as_unknown(paths):
    records = inspect_candidates([candidate(paths)], EVIDENCE, hard_pruning=True)
    result, selected = resolve_candidates(records, review(paths=[]), 1)
    assert result[0]['status'] == 'unknown' and selected is None
    assert not any(p['pruned'] for p in result[0]['paths'])


def test_invalid_path_does_not_delete_candidate_and_alternative_can_succeed():
    records = inspect_candidates([candidate([path(citation=999), path()])], EVIDENCE, hard_pruning=True)
    result, selected = resolve_candidates(records, review(paths=[{
        **review()['paths'][0], 'path_index': 1}]), 1)
    assert selected == 0 and result[0]['paths'][0]['pruned'] and result[0]['status'] == 'supported'
    records = inspect_candidates([candidate([path(citation=999)])], EVIDENCE, hard_pruning=True)
    result, selected = resolve_candidates(records, review(paths=[]), 1)
    assert len(result) == 1 and result[0]['status'] == 'unknown' and selected is None


def test_shadow_never_accepts_bad_witness_even_if_model_passes_it():
    records = inspect_candidates([candidate([path(citation=999)])], EVIDENCE, hard_pruning=False)
    result, selected = resolve_candidates(records, review(), 1)
    assert not result[0]['paths'][0]['pruned'] and selected is None


def test_semantic_refutation_is_contested_and_not_pruned():
    records = inspect_candidates([candidate()], EVIDENCE, hard_pruning=True)
    result, selected = resolve_candidates(records, review(paths=[{
        **review()['paths'][0], 'verdict': 'contradicted'}]), 1)
    assert result[0]['status'] == 'contested' and not result[0]['paths'][0]['pruned'] and selected is None


@pytest.mark.parametrize('change', [
    {'paths': [review()['paths'][0]] * 2},
    {'paths': [{**review()['paths'][0], 'candidate_index': 3}]},
    {'paths': [{**review()['paths'][0], 'supported_requirements': []}]},
    {'issues': ['Unresolved entity conflict']}, {'queries': ['Check identity']},
    {'selected_candidate': 3}, {'answer_resolved': False}])
def test_malformed_or_incomplete_review_cannot_select(change):
    records = inspect_candidates([candidate()], EVIDENCE, hard_pruning=True)
    assert resolve_candidates(records, review(**change), 1)[1] is None


def test_all_bridge_requirements_needed_and_conflicting_supported_answers_abstain():
    records = inspect_candidates([candidate()], EVIDENCE, hard_pruning=True)
    assert resolve_candidates(records, review(), 2)[1] is None
    records = inspect_candidates([candidate(), candidate(answer='Berlin')], EVIDENCE, hard_pruning=True)
    r = review(paths=review()['paths'] + [{**review()['paths'][0], 'candidate_index': 1}])
    assert resolve_candidates(records, r, 1)[1] is None


def test_candidate_capacity_never_evicts_unknown_and_refresh_clears_stale_proof():
    previous = [candidate(answer=str(i)) for i in range(4)]
    pool, overflow = merge_candidate_pool(previous, [candidate(answer='new')])
    assert [c['answer'] for c in pool] == ['0', '1', '2', '3'] and len(overflow) == 1
    assert all(c['paths'] == [] for c in pool) and previous[0]['paths']


def candidate_tape(proposals=None, verdict=None):
    responses = tape()
    responses[3] = json.dumps({'candidates': proposals if proposals is not None else [candidate()]})
    responses[4] = json.dumps(verdict if verdict is not None else review())
    return responses


@pytest.mark.parametrize('version', ['v3', 'v3-shadow'])
def test_real_graph_batch_candidates_use_same_budgeted_model_path(version):
    rt = runtime(candidate_tape([candidate(), candidate([], answer='Berlin')]))
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version=version)
    assert result['answer'] == 'Paris' and rt.model_calls == 6 and rt.search_calls == 1
    event = next(e for e in rt.events if e['kind'] == 'mainline.candidates.reviewed')
    assert event['candidates'][1]['status'] == 'unknown'
    assert rt.statistics()['usage_missing_calls'] == 6  # Scripted outputs have no provider usage.


def test_pruned_paths_omitted_from_independent_prompt_and_replayable_from_trace():
    rt = runtime(candidate_tape([candidate([path(citation=999), path()])], review(paths=[{
        **review()['paths'][0], 'path_index': 1}])))
    run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v3')
    event = next(e for e in rt.events if e['kind'] == 'mainline.candidates.proposed')
    p = event['candidates'][0]['paths'][0]
    assert replay_certificate(p['certificate'], {'steps': p['steps']}, event['evidence'])
    verifier = next(e for e in rt.events if e['kind'] == 'mainline.communication.model_input'
                    and e['role'] == 'Verifier')
    assert [p['path_index'] for p in verifier['data']['candidates'][0]['paths']] == [1]


def test_no_candidate_proof_abstains_without_projection_and_obeys_budget():
    rt = runtime(candidate_tape([candidate([])], review(paths=[], answer_resolved=False))[:-1])
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v3')
    assert not result['answer'] and rt.model_calls == 5
    rt = runtime(candidate_tape(), model_calls=5)
    guarded = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v3')
    assert not guarded['answer'] and rt.model_calls <= 5


def test_checkpoint_preserves_unknown_candidates_and_path_snapshots():
    from deepsearch_agent.core.evidence import TextChunk
    saver = InMemorySaver()
    responses = iter(candidate_tape([candidate(), candidate([], answer='Berlin')])[3:5])
    wf = ResearchWorkflow(SimpleNamespace(complete=lambda _: next(responses)), None,
        evidence_policy=EvidencePolicy.v3(), context_window=131072, checkpointer=saver, run_id='paths')
    initial = {'question': 'Where?', 'history': [], 'rounds': 1, 'reviews': 0, 'dispatches': 1,
        'evidence': [TextChunk('Alice was born in Paris.', 'a', 1, 0)], 'claims': [], 'supported': [],
        'queries': [], 'issues': [], 'tasks': [], 'instruction': '', 'verified': False,
        'requirements': ['birthplace'], 'answer_type': 'entity'}
    config = {'configurable': {'thread_id': 'paths'}}
    wf.graph.update_state(config, initial, as_node='researcher')
    wf.graph.invoke(None, config)
    restored = ResearchWorkflow(None, None, evidence_policy=EvidencePolicy.v3(), checkpointer=saver)
    state = restored.graph.get_state(config).values
    assert state['candidates'][1]['status'] == 'unknown'
    assert len(state['candidate_history']) == 1 and state['selected_candidate'] == 0


def test_optional_config_and_flags():
    assert AgentSettings(evidence_policy_version='v3').evidence_control_enabled is False
    with pytest.raises(ValueError):
        EvidencePolicy(hard_pruning=True)
