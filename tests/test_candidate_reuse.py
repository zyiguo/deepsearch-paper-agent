from copy import deepcopy
from types import SimpleNamespace
import pytest

from deepsearch_agent.core.candidate_reuse import merge_updates, registry
from deepsearch_agent.core.evidence_paths import inspect_candidates
from deepsearch_agent.agents.evidence_policy import EvidencePolicy
from deepsearch_agent.agents.path_answering import PathAnsweringMixin
from deepsearch_agent.agents.path_contracts import CandidateUpdates
from test_evidence_paths import candidate, path, EVIDENCE, candidate_tape, runtime, run_mainline


def test_identity_update_dedup_preserves_unknown_and_distinct_interpretations():
    old = inspect_candidates([candidate([], answer='Paris')], EVIDENCE, hard_pruning=True)
    update = candidate([path(), path()], answer='A paraphrase') | {'existing_candidate_id': old[0]['candidate_id']}
    pool, rejected = merge_updates(old, [update, update, candidate([], answer='Paris  '),
        candidate([], answer='Paris') | {'interpretation': 'Different city with same name'}])
    assert not rejected and len(pool) == 2 and len(pool[0]['paths']) == 1
    assert pool[0]['answer'] == 'Paris' and pool[1]['paths'] == []
    assert 'paths' not in registry(old)[0]
    assert old[0]['status'] == 'unknown'


def test_unknown_id_and_capacity_do_not_replace_unknowns():
    old = inspect_candidates([candidate([], answer=str(i)) for i in range(4)], EVIDENCE, hard_pruning=True)
    pool, rejected = merge_updates(old, [candidate() | {'existing_candidate_id': 'invalid'}, candidate()])
    assert len(pool) == 4 and len(rejected) == 2
    assert [c['answer'] for c in pool] == ['0', '1', '2', '3']


class Harness(PathAnsweringMixin):
    evidence_policy = EvidencePolicy.v4()
    observer = None
    skills = None
    client = object()
    def __init__(self):
        self.calls = []
    def context(self, state):
        return state['evidence']
    def working_notes(self):
        return {}
    def call(self, role, instruction, schema, data):
        self.calls.append(role)
        assert role == 'Answerer'
        return CandidateUpdates(candidates=[candidate([])])


def test_empty_paths_skip_verifier_unchanged_stops_changed_evidence_reopens():
    h = Harness()
    state = {'evidence': deepcopy(EVIDENCE), 'question': 'Where?', 'instruction': '',
             'requirements': ['birthplace'], 'reviews': 0}
    state.update(h.draft_paths(state))
    state.update(h.verify_paths(state))
    assert h.calls == ['Answerer'] and state['candidates'][0]['status'] == 'unknown'
    assert state['review_action'] == 'research' and not state['coverage_complete']
    state.update(h.draft_paths(state))
    state.update(h.verify_paths(state))
    assert h.calls == ['Answerer'] and state['review_stalled'] and state['review_action'] == 'stop'
    state['evidence'][0]['text'] += ' New evidence.'
    state.update(h.draft_paths(state))
    assert h.calls == ['Answerer', 'Answerer'] and not state['candidate_generation_reused']


def test_v4_real_graph_still_requires_independent_review_for_proof():
    rt = runtime(candidate_tape())
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v4')
    assert result['answer'] == 'Paris' and rt.model_calls == 6
    assert any(e['kind'] == 'mainline.rag.request' and e['role'] == 'Verifier' for e in rt.events)


@pytest.mark.parametrize('changed', ['question', 'requirements', 'instruction', 'memory', 'client', 'resume'])
def test_reuse_does_not_cross_changed_context_or_restored_workflow(changed):
    h = Harness()
    state = {'evidence': deepcopy(EVIDENCE), 'question': 'Where?', 'instruction': '',
             'requirements': ['birthplace'], 'reviews': 0}
    state.update(h.draft_paths(state))
    state.update(h.verify_paths(state))
    if changed == 'memory':
        h.memory_context = {'language': 'English'}
    elif changed == 'client':
        h.client = object()
    elif changed == 'resume':
        h = Harness()
    else:
        state[changed] = ['new requirement'] if changed == 'requirements' else 'Changed'
    state.update(h.draft_paths(state))
    assert not state['candidate_generation_reused']


def test_partial_paths_are_not_removed_or_locally_accepted():
    h = Harness()
    records = inspect_candidates([candidate([path(quote='')])], EVIDENCE, hard_pruning=True)
    def call(role, *args):
        assert role == 'Verifier'
        raise RuntimeError('independent verifier reached')
    h.call = call
    with pytest.raises(RuntimeError, match='independent verifier reached'):
        h.verify_paths({'candidate_evidence': EVIDENCE, 'candidates': records,
                        'question': 'Where?', 'requirements': ['birthplace'], 'reviews': 0})
