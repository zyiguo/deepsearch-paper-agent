import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from deepsearch_agent.agents.contracts import Decision, ControlledDecision, CoverageVerdict
from deepsearch_agent.agents.evidence_policy import EvidencePolicy, coverage_result
from deepsearch_agent.agents.runtime import ResearchWorkflow
from deepsearch_experiments.benchmarks.multihop.mainline import run_mainline
from deepsearch_experiments.benchmarks.multihop.answer_projection import validate_projection
from test_multihop_mainline import runtime, happy_tape


def plan(answer_type='entity'):
    return json.dumps({'action': 'research', 'tasks': [], 'instruction': 'Find Alice birthplace',
                       'requirements': ['The birthplace of Alice'], 'answer_type': answer_type})


def verdict(coverage=None):
    return json.dumps({'supported': [0], 'queries': [], 'issues': [], 'missing_answerable': [],
                       'coverage': coverage if coverage is not None else [
                           {'requirement_index': 0, 'status': 'supported', 'citations': [1]}]})


def tape(answer='Paris', answer_type='entity', coverage=None):
    return [plan(answer_type), '{"tool":"search_evidence","arguments":{"query":"Alice","top_k":1}}',
            '{"tool":"","arguments":{}}',
            '{"claims":[{"text":"Alice was born in Paris.","citations":[1]}]}',
            verdict(coverage), answer]


def test_controlled_graph_probes_then_verifies_without_extra_parent_calls():
    rt = runtime(tape())
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True)
    assert result['answer'] == 'Paris'
    assert result['outcome']['status'] == 'verified'
    assert result['coverage']['complete']
    assert rt.model_calls == 6 and rt.search_calls == 1
    inputs = [e for e in rt.events if e['kind'] == 'mainline.communication.model_input']
    assert [e['role'] for e in inputs] == ['Supervisor', 'Researcher', 'Researcher', 'Answerer', 'Verifier']
    assert inputs[-1]['data']['requirements'] == ['The birthplace of Alice']
    assert 'resource_budget' not in inputs[-1]['data']


def test_missing_bridge_coverage_never_certifies_complete():
    gaps, complete = coverage_result(['father identity', 'father birthplace'], [
        {'requirement_index': 1, 'status': 'supported', 'citations': [2]}], {1, 2})
    assert not complete and gaps == ['father identity']


@pytest.mark.parametrize('coverage', [
    [{'requirement_index': 0, 'status': 'supported', 'citations': [999]}],
    [{'requirement_index': 0, 'status': 'supported', 'citations': []}],
    [{'requirement_index': 0, 'status': 'partial', 'citations': [1]}],
    [{'requirement_index': 0, 'status': 'supported', 'citations': [1]}] * 2,
    [{'requirement_index': 7, 'status': 'supported', 'citations': [1]}],
    [],
])
def test_untrusted_coverage_stays_incomplete(coverage):
    assert coverage_result(['required fact'], coverage, {1}) == (['required fact'], False)


def test_coverage_gap_in_real_graph_abstains_without_projection():
    responses = tape(coverage=[])
    responses[-1] = json.dumps({'action': 'stop', 'tasks': [], 'instruction': '',
                               'requirements': ['The birthplace of Alice'], 'answer_type': 'entity'})
    rt = runtime(responses)
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True)
    assert result['answer'] == 'UNKNOWN'
    assert result['outcome']['status'] == 'incomplete'
    assert result['termination_reason'] == 'incomplete_evidence_coverage'
    assert not any(e['kind'] == 'mainline.answer_projection' for e in rt.events)


def test_each_role_has_one_strict_repair_without_cross_role_starvation():
    calls = []
    responses = iter(['invalid', '{"action":"stop","tasks":[],"instruction":""}',
                      'invalid', '{"supported":[],"queries":[],"coverage":[]}', 'invalid'])
    client = SimpleNamespace(complete=lambda prompt: calls.append(prompt) or next(responses))
    workflow = ResearchWorkflow(client, None, evidence_policy=EvidencePolicy())
    workflow.call('Supervisor', '', Decision, {})
    workflow.call('Verifier', '', CoverageVerdict, {})
    assert len(calls) == 4
    assert workflow.role_repairs_remaining['Supervisor'] == workflow.role_repairs_remaining['Verifier'] == 0
    with pytest.raises(ValidationError):
        workflow.call('Supervisor', '', Decision, {})
    assert len(calls) == 5


def test_extra_state_field_is_not_silently_stripped():
    invalid = '{"action":"stop","tasks":[],"instruction":"","research_rounds":1}'
    outputs = iter([invalid, invalid])
    workflow = ResearchWorkflow(SimpleNamespace(complete=lambda _: next(outputs)), None,
                                evidence_policy=EvidencePolicy())
    with pytest.raises(ValidationError):
        workflow.call('Supervisor', '', Decision, {})


def test_boolean_projection_rejects_country_instead_of_relabeling():
    rt = runtime(tape(answer='Canada', answer_type='boolean'))
    result = run_mainline('Was Alice born in Paris?', rt, evidence_controlled=True)
    assert result['answer'] == 'UNKNOWN' and result['projection_validation']['type_rejected']
    assert rt.model_calls == 6
    assert validate_projection(' YES ', 'boolean') == 'yes'
    assert validate_projection('not sure', 'boolean') == 'UNKNOWN'


def test_original_mainline_tape_and_call_count_unchanged():
    rt = runtime(happy_tape())
    assert run_mainline('Where was Alice born?', rt)['answer'] == 'Paris'
    assert rt.model_calls == 8


def test_single_search_limit_preserves_draft_and_independent_verifier():
    responses = tape()
    responses.pop(2)  # Budget guard ends child, not another planner generation.
    rt = runtime(responses, search_calls=1)
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True)
    assert result['answer'] == 'Paris' and rt.search_calls == 1 and rt.model_calls == 5


def test_two_tool_calls_trigger_coverage_probe_instead_of_third_search():
    responses = tape()
    responses[2] = '{"tool":"search_evidence","arguments":{"query":"Alice Paris","top_k":1}}'
    rt = runtime(responses)
    result = run_mainline('Where was Alice born?', rt, evidence_controlled=True)
    assert result['answer'] == 'Paris' and rt.search_calls == 2
    stops = [e for e in rt.events if e['kind'] == 'mainline.research.tools_done']
    assert stops[0]['stop_reason'] == 'coverage_probe_due'


def test_production_service_opt_in_uses_coverage_and_preserves_child_isolation():
    from deepsearch_agent.application.answer_service import AnswerService
    from deepsearch_agent.agents.agent_config import AgentSettings
    from test_agentic_integration import Library
    prompts = []
    outputs = iter(tape()[:-1])
    client = SimpleNamespace(complete=lambda prompt: prompts.append(prompt) or next(outputs))
    result = AnswerService(client, Library(), context_window=131072,
        agent_settings=AgentSettings(enabled=True, evidence_control_enabled=True))._answer(
            'Where was Alice born?', history=[{'role': 'user', 'content': 'PRIVATE_HISTORY'}])
    assert result.text == 'Alice was born in Paris. [1]'
    verifier = next(p for p in prompts if p.startswith('你是 Verifier。'))
    assert 'requirement_index' in verifier and 'The birthplace of Alice' in verifier
    assert all('PRIVATE_HISTORY' not in p for p in prompts if p.startswith('你是 Researcher。'))


def test_configuration_opt_in_is_validated(tmp_path):
    from deepsearch_agent.agents.agent_config import AgentSettings
    path = tmp_path / 'agent.env'
    assert not AgentSettings.load(path, environ={}).evidence_control_enabled
    assert AgentSettings.load(path, environ={'AGENT_EVIDENCE_CONTROL_ENABLED': 'true'}).evidence_control_enabled
    with pytest.raises(ValueError):
        AgentSettings.load(path, environ={'AGENT_EVIDENCE_CONTROL_ENABLED': 'maybe'})
