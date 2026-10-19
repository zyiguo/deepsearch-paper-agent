from copy import deepcopy
import json
import pytest
from pydantic import ValidationError

from deepsearch_agent.core.evidence_paths import inspect_candidates, resolve_candidates
from deepsearch_agent.core.verification_gaps import record_gaps, filter_review_paths
from deepsearch_agent.agents.path_contracts import GapVerdict
from deepsearch_agent.agents.evidence_policy import EvidencePolicy
from test_candidate_reuse import Harness
from test_evidence_paths import candidate, path, EVIDENCE, review, candidate_tape, runtime, run_mainline


def fixture():
    records = inspect_candidates([candidate()], EVIDENCE, hard_pruning=True)
    verdict = review(paths=[{**review()['paths'][0], 'verdict': 'unknown', 'supported_requirements': []}],
                     selected_candidate=None, answer_resolved=False, issues=['birthplace missing'], queries=['birthplace'])
    records, _ = resolve_candidates(records, verdict, 1)
    gaps = record_gaps(records, verdict, EVIDENCE, 1)
    payload = [{'candidate_index': 0, 'answer': records[0]['answer'], 'interpretation': records[0]['interpretation'],
                'paths': [{'path_index': 0, 'steps': path()['steps']}]}]
    return records, verdict, gaps, payload


def test_repeated_unknown_witness_deferred_without_deleting_candidate():
    records, _, gaps, payload = fixture()
    filtered, deferred = filter_review_paths(payload, records, EVIDENCE, gaps)
    assert filtered[0]['paths'] == [] and deferred[0]['pruned'] is False
    assert records[0]['status'] == 'unknown' and gaps[records[0]['candidate_id']]['missing_requirements'] == [0]
    assert not gaps[records[0]['candidate_id']]['semantic_truth_certified']


@pytest.mark.parametrize('change', ['quote', 'content', 'source', 'candidate', 'visual'])
def test_new_witness_or_identity_reopens(change):
    records, _, gaps, payload = fixture()
    evidence = deepcopy(EVIDENCE)
    if change == 'quote': payload[0]['paths'][0]['steps'][0]['quote'] = 'Paris'
    elif change == 'content': evidence[0]['text'] += ' Updated information.'
    elif change == 'source': evidence[0]['source'] = 'new document'
    elif change == 'candidate': records[0]['candidate_id'] = 'new identity'
    else: evidence[0]['visual_pages'] = ['new page']
    filtered, deferred = filter_review_paths(payload, records, evidence, gaps)
    assert filtered[0]['paths'] and not deferred


def test_new_alternative_is_reviewed_while_old_unknown_stays_deferred():
    records, _, gaps, payload = fixture()
    payload[0]['paths'].append({'path_index': 1, 'steps': path(quote='Paris')['steps']})
    filtered, deferred = filter_review_paths(payload, records, EVIDENCE, gaps)
    assert [p['path_index'] for p in filtered[0]['paths']] == [1] and len(deferred) == 1


@pytest.mark.parametrize('change', ['duplicate', 'outside', 'contradicted', 'supported', 'missing'])
def test_invalid_or_nonunknown_reviews_never_create_gap(change):
    records, verdict, _, _ = fixture()
    if change == 'duplicate': verdict['paths'] *= 2
    elif change == 'outside': verdict['paths'][0]['supported_requirements'] = [9]
    elif change == 'missing': verdict['paths'] = []
    else: verdict['paths'][0]['verdict'] = change
    assert not record_gaps(records, verdict, EVIDENCE, 1)


def test_length_normalization_only_affects_diagnostic_reason():
    value = review()
    value['paths'][0]['reason'] = 'x' * 1600
    parsed = GapVerdict.model_validate(value)
    assert len(parsed.paths[0].reason) == 280 and parsed.paths[0].verdict == 'supported'
    assert value['paths'][0]['reason'] == 'x' * 1600
    value['paths'][0]['verdict'] = 'supported' + 'x' * 500
    with pytest.raises(ValidationError): GapVerdict.model_validate(value)


def test_workflow_gap_survives_new_irrelevant_evidence_and_retains_unknown():
    records, verdict, gaps, _ = fixture()
    h = Harness(); h.evidence_policy = EvidencePolicy.v5()
    state = {'question': 'Where?', 'requirements': ['birthplace'], 'reviews': 1,
             'candidate_evidence': EVIDENCE + [{'id': 2, 'text': 'Unrelated'}],
             'candidates': records, 'verification_gaps': gaps, 'queries': ['birthplace']}
    state['verification_gap_scope'] = h.gap_scope(state)
    result = h.verify_paths(state)
    assert not h.calls and result['candidates'][0]['status'] == 'unknown'
    assert not result['coverage_complete'] and result['verification_gaps'] == gaps
    # A restarted workflow cannot trust another model/context session's cache.
    restored = Harness(); restored.evidence_policy = EvidencePolicy.v5()
    def fail(*args): raise RuntimeError('real review required')
    restored.call = fail
    with pytest.raises(RuntimeError, match='real review required'): restored.verify_paths(state)


def test_v5_graph_concise_review_does_not_spend_repair_on_long_reason():
    responses = candidate_tape(verdict=review(paths=[{**review()['paths'][0], 'reason': 'x' * 1800}]))
    rt = runtime(responses)
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True, policy_version='v5')
    assert result['answer'] == 'Paris' and rt.model_calls == 6
    assert not any(e['kind'] == 'mainline.rag.schema_repair' for e in rt.events)
    raw = next(e['raw'] for e in rt.events if e['kind'] == 'mainline.rag.response' and e['role'] == 'Verifier')
    assert len(json.loads(raw)['paths'][0]['reason']) == 1800
