import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from deepsearch_agent.agents.contracts import ReviewFinding
from deepsearch_agent.agents.evidence_policy import EvidencePolicy
from deepsearch_agent.agents.review_control import (apply_assumption_updates, classify_findings,
                                                   review_input_id)
from deepsearch_agent.agents.runtime import ResearchWorkflow
from deepsearch_experiments.benchmarks.multihop.mainline import run_mainline
from test_evidence_policy import tape
from test_multihop_mainline import runtime


def finding(kind='note', message='No earlier spouse is documented.', **changes):
    return {'kind': kind, 'message': message, 'requirement_indices': [0], 'claim_indices': [0],
            'blocking': kind != 'note', 'action': {'note': 'finish', 'contradiction': 'revise',
            'citation_error': 'revise', 'missing_evidence': 'research', 'evidence_conflict': 'research'}[kind],
            **changes}


def actionable_tape(findings=None):
    responses = tape()
    verdict = json.loads(responses[4])
    verdict['findings'] = findings if findings is not None else [finding()]
    responses[4] = json.dumps(verdict)
    return responses


def test_nonblocking_note_finishes_without_repeated_drafts():
    rt = runtime(actionable_tape())
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v2')
    assert result['answer'] == 'Paris' and result['outcome']['status'] == 'verified'
    assert rt.model_calls == 6
    assert '资料说明：' in result['verified_answer']
    assert result['outcome']['issues'] == []


def test_actual_conflict_does_not_become_a_note_or_pass_coverage():
    responses = actionable_tape([finding('evidence_conflict', 'Paris and Berlin conflict.', action='stop')])[:-1]
    rt = runtime(responses)
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v2')
    assert result['answer'] == 'UNKNOWN' and result['outcome']['status'] == 'incomplete'
    assert rt.model_calls == 5
    assert not any(e['kind'] == 'mainline.answer_projection' for e in rt.events)


@pytest.mark.parametrize('changes', [{'blocking': False}, {'action': 'finish'}])
def test_conflict_cannot_be_downgraded_by_inconsistent_fields(changes):
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(finding('evidence_conflict', **changes))


def test_unclassified_free_text_and_invalid_targets_remain_blocking():
    blockers, notes, action = classify_findings([finding()], ['Unresolved publisher conflict'], 1, 1)
    assert blockers == ['Unresolved publisher conflict'] and notes and action == 'stop'
    blockers, notes, action = classify_findings([finding(requirement_indices=[9])], [], 1, 1)
    assert blockers and not notes and action == 'stop'


def test_exact_repeated_draft_reuses_verifier_then_stops():
    responses = actionable_tape([finding('contradiction', 'Correct the birthplace.')])
    responses[-1] = responses[3]  # The attempted revision returns the exact same draft.
    rt = runtime(responses)
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v2')
    assert result['answer'] == 'UNKNOWN'
    assert result['outcome']['stop_reason'] == 'unchanged_review_input'
    assert rt.model_calls == 6
    verifiers = [e for e in rt.events if e['kind'] == 'mainline.rag.request' and e['role'] == 'Verifier']
    assert len(verifiers) == 1
    assert any(e['kind'] == 'mainline.verification.reused' for e in rt.events)


@pytest.mark.parametrize('changed', ['question', 'claims', 'evidence', 'requirements', 'assumptions', 'policy'])
def test_review_cache_invalidated_by_every_actual_input(changed):
    args = {'question': 'q', 'claims': [{'text': 'Paris', 'citations': [1]}],
            'evidence': [{'id': 1, 'text': 'Paris'}], 'requirements': ['birthplace'],
            'assumptions': [], 'policy': {'version': 'v2'}}
    before = review_input_id(**args)
    args[changed] = 'different' if changed == 'question' else ['different']
    assert review_input_id(**args) != before


def test_assumption_update_requires_authorized_verbatim_evidence():
    original = [{'key': 'entity_type', 'statement': 'X is a city', 'status': 'unconfirmed',
                 'citations': [], 'quote': ''}]
    update = {'key': 'entity_type', 'statement': 'X is a radio station', 'status': 'supported',
              'citations': [1], 'quote': 'X is a radio station'}
    evidence = [{'id': 1, 'text': 'X is a radio station in Los Alamos.'}]
    new, changes, rejected = apply_assumption_updates(original, [update], evidence)
    assert new == [update] and changes[0]['old'] == original[0] and not rejected
    assert original[0]['statement'] == 'X is a city'
    for invalid in ({**update, 'citations': [999]}, {**update, 'quote': 'X is in Canada'}):
        new, changes, rejected = apply_assumption_updates(original, [invalid], evidence)
        assert new == original and not changes and rejected == ['entity_type']


def test_duplicate_updates_cannot_choose_arbitrary_entity_interpretation():
    update = {'key': 'type', 'statement': 'X is a station', 'status': 'supported',
              'citations': [1], 'quote': 'station'}
    assert apply_assumption_updates([], [update, update], [{'id': 1, 'text': 'station'}]) == (
        [], [], ['type', 'type'])


def state_for(workflow):
    from deepsearch_agent.core.evidence import TextChunk
    return {'question': 'Where was Alice born?', 'claims': [{'text': 'Paris', 'citations': [1]}],
            'evidence': [TextChunk('Alice was born in Paris.', 'a.pdf', 1, 0)],
            'requirements': ['birthplace'], 'plan_assumptions': [], 'reviews': 0,
            'history': [], 'tasks': [], 'instruction': '', 'issues': []}


def test_changed_claim_calls_verifier_again_instead_of_reusing_cached_result():
    outputs = iter([json.dumps({'supported': [0], 'queries': [], 'findings': [], 'coverage': [
        {'requirement_index': 0, 'status': 'supported', 'citations': [1]}]})] * 2)
    calls = []
    wf = ResearchWorkflow(SimpleNamespace(complete=lambda p: calls.append(p) or next(outputs)), None,
                          evidence_policy=EvidencePolicy.v2(), context_window=131072)
    state = state_for(wf)
    first = wf.verify(state)
    state.update(first)
    state['claims'] = [{'text': 'Alice was born in Paris', 'citations': [1]}]
    assert wf.verify(state)['verified'] and len(calls) == 2


def test_memory_context_change_invalidates_review_cache():
    response = json.dumps({'supported': [0], 'queries': [], 'findings': [], 'coverage': [
        {'requirement_index': 0, 'status': 'supported', 'citations': [1]}]})
    calls = []
    wf = ResearchWorkflow(SimpleNamespace(complete=lambda p: calls.append(p) or response), None,
                          evidence_policy=EvidencePolicy.v2(), context_window=131072)
    state = state_for(wf)
    state.update(wf.verify(state))
    wf.memory_context = {'language': 'English'}
    wf.verify(state)
    assert len(calls) == 2


def test_visual_evidence_does_not_reuse_text_only_fingerprint():
    from deepsearch_agent.agents.contracts import ActionableVerdict
    response = ActionableVerdict.model_validate({'supported': [0], 'queries': [], 'findings': [],
        'coverage': [{'requirement_index': 0, 'status': 'supported', 'citations': [1]}]})
    wf = ResearchWorkflow(None, None, evidence_policy=EvidencePolicy.v2())
    calls = []
    wf.call = lambda *args, **kwargs: calls.append(args) or response
    wf.context = lambda _: [{'id': 1, 'text': 'Paris', 'visual_pages': [1]}]
    state = state_for(wf)
    state.update(wf.verify(state))
    wf.verify(state)
    assert len(calls) == 2


def test_repeated_invalid_citations_stop_without_repeated_repairs():
    responses = actionable_tape()[:3]
    bad_draft = '{"claims":[{"text":"Paris","citations":[999]}]}'
    responses += [bad_draft, bad_draft]
    rt = runtime(responses)
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v2')
    assert result['outcome']['stop_reason'] == 'unchanged_review_input'
    assert not any(e['kind'] == 'mainline.rag.request' and e['role'] == 'Verifier' for e in rt.events)
    assert rt.model_calls == 5


def test_checkpoint_resume_preserves_review_cache_and_stops_repetition():
    from langgraph.checkpoint.memory import InMemorySaver
    events = []
    saver = InMemorySaver()
    wf = ResearchWorkflow(SimpleNamespace(complete=lambda _: pytest.fail('Unexpected model call')), None,
        evidence_policy=EvidencePolicy.v2(), context_window=131072, checkpointer=saver, run_id='review-recovery')
    state = state_for(wf)
    state.update(top_k=5, rounds=1, supported=[0], verified=True, stopped=False, dispatches=2,
        reviews=1, queries=[], coverage_complete=False, coverage=[], missing_answerable=[],
        review_stalled=True, review_action='revise', review_cache={'input_id': 'saved-input', 'result': {}})
    wf.graph.update_state({'configurable': {'thread_id': 'review-recovery'}}, state, as_node='verifier')
    resumed = ResearchWorkflow(SimpleNamespace(complete=lambda _: pytest.fail('Unexpected model call')), None,
        evidence_policy=EvidencePolicy.v2(), context_window=131072, checkpointer=saver, run_id='review-recovery',
        observer=lambda k, p: events.append((k, p)))
    answer = resumed.answer(state['question'], 5, [], resume=True)
    assert '无法根据资料确认' in answer.text
    assert any(k == 'review.stopped' and p['input_id'] == 'saved-input' for k, p in events)


def test_verified_assumption_correction_invalidates_previous_answer():
    update = {'key': 'entity_type', 'statement': 'Alice is a person', 'status': 'supported',
              'citations': [1], 'quote': 'Alice was born in Paris.'}
    verdict = {'supported': [0], 'queries': [], 'findings': [], 'coverage': [
        {'requirement_index': 0, 'status': 'supported', 'citations': [1]}], 'assumption_updates': [update]}
    wf = ResearchWorkflow(SimpleNamespace(complete=lambda _: json.dumps(verdict)), None,
                          evidence_policy=EvidencePolicy.v2(), context_window=131072)
    state = state_for(wf)
    state['plan_assumptions'] = [{'key': 'entity_type', 'statement': 'Alice is a city',
                                'status': 'unconfirmed', 'citations': [], 'quote': ''}]
    result = wf.verify(state)
    assert not result['verified'] and not result['coverage_complete'] and not result['supported']
    assert result['plan_revision_pending'] and result['review_action'] == 'research'
    assert state['requirements'] == ['birthplace']


def test_corrected_assumption_reaches_fresh_child_and_requires_new_verification():
    responses = actionable_tape(findings=[])
    initial = json.loads(responses[0])
    initial['assumptions'] = [{'key': 'entity_type', 'statement': 'Alice is a city'}]
    responses[0] = json.dumps(initial)
    first_verdict = json.loads(responses[4])
    first_verdict['assumption_updates'] = [{'key': 'entity_type', 'statement': 'Alice is a person',
        'status': 'supported', 'citations': [1], 'quote': 'Alice was born in Paris.'}]
    responses[4] = json.dumps(first_verdict)
    responses = responses[:-1] + responses[1:4] + [actionable_tape(findings=[])[4], 'Paris']
    rt = runtime(responses)
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v2')
    assert result['answer'] == 'Paris' and rt.model_calls == 10
    verifiers = [e for e in rt.events if e['kind'] == 'mainline.rag.request' and e['role'] == 'Verifier']
    assert len(verifiers) == 2
    children = [e for e in rt.events if e['kind'] == 'mainline.communication.task_dispatched']
    assert len(children) == 2
    assert 'Alice is a person (supported)' in ' '.join(children[1]['task']['constraints'])
    assert result['coverage']['requirements'] == ['The birthplace of Alice']
    assert any(e['kind'] == 'mainline.plan.assumptions_updated' for e in rt.events)


def test_existing_v1_still_rejects_untyped_issues_without_downgrade():
    responses = tape()
    verdict = json.loads(responses[4]); verdict['issues'] = ['scope caveat']
    responses[4] = json.dumps(verdict)
    responses[-1] = json.dumps({'action': 'stop', 'tasks': [], 'instruction': '',
                               'requirements': ['The birthplace of Alice'], 'answer_type': 'entity'})
    rt = runtime(responses)
    assert run_mainline('Where was Alice born?', rt, evidence_controlled=True)['answer'] == 'UNKNOWN'


def test_v2_configuration_is_explicit_and_disabled_by_default(tmp_path):
    from deepsearch_agent.agents.agent_config import AgentSettings
    settings = AgentSettings.load(tmp_path / 'missing', environ={'AGENT_EVIDENCE_POLICY_VERSION': 'v2'})
    assert settings.evidence_policy_version == 'v2' and not settings.evidence_control_enabled
    with pytest.raises(ValueError):
        AgentSettings(evidence_policy_version='v99')


@pytest.mark.parametrize('conflict', [False, True])
def test_saved_north_star_input_notes_and_conflicts_take_distinct_paths(conflict):
    fixture = json.loads((Path(__file__).parent / 'fixtures/review_v2/north_star.json').read_text(encoding='utf-8'))
    findings = [finding(message=fixture['legacy_issues'][1], requirement_indices=[1], claim_indices=[1])]
    if conflict:
        findings.append(finding('evidence_conflict', fixture['legacy_issues'][0]))
    evidence = [e for e in fixture['evidence'] if conflict or e['id'] != 2]
    response = {'supported': [0, 1], 'queries': [], 'findings': findings, 'coverage': [
        {'requirement_index': 0, 'status': 'supported', 'citations': [1]},
        {'requirement_index': 1, 'status': 'supported', 'citations': [6]}]}
    wf = ResearchWorkflow(SimpleNamespace(complete=lambda _: json.dumps(response)), None,
                          evidence_policy=EvidencePolicy.v2())
    wf.context = lambda _: evidence
    state = {'question': fixture['question'], 'claims': fixture['claims'],
        'requirements': fixture['requirements'], 'plan_assumptions': [], 'reviews': 0,
        'evidence': evidence, 'rounds': 1, 'dispatches': 2, 'verified': False,
        'issues': [], 'queries': [], 'supported': []}
    result = wf.verify(state)
    state.update(result)
    decision = wf.supervise(state)
    assert decision['action'] == ('research' if conflict else 'finish')
    assert result['review_notes'] == [fixture['legacy_issues'][1]]
    assert bool(result['issues']) == conflict
