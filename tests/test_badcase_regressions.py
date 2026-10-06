import json
from types import SimpleNamespace

import pytest

from deepsearch_agent.research_graph import Decision, ResearchWorkflow


def test_supervisor_schema_failure_gets_one_strict_repair():
    outputs = iter([
        {'action': 'draft', 'instruction': 'answer', 'status': 'in_progress'},
        {'action': 'draft', 'instruction': 'answer', 'tasks': []},
    ])
    prompts, events = [], []
    def complete(prompt):
        prompts.append(prompt)
        return json.dumps(next(outputs))
    workflow = ResearchWorkflow(SimpleNamespace(complete=complete), None,
                                observer=lambda kind, payload: events.append((kind, payload)))
    result = workflow.call('Supervisor', 'Choose a step.', Decision, {'question': 'question'})
    assert result.action == 'draft'
    assert len(prompts) == 2
    assert 'schema_repair' in prompts[1]
    assert any(kind == 'rag.schema_repair' for kind, _ in events)


def test_schema_repair_budget_is_per_workflow_and_still_fails_closed():
    from pydantic import ValidationError
    calls = []
    def complete(prompt):
        calls.append(prompt)
        return '{"action":"invented"}'
    workflow = ResearchWorkflow(SimpleNamespace(complete=complete), None)
    with pytest.raises(ValidationError):
        workflow.call('Supervisor', 'Choose a step.', Decision, {})
    assert len(calls) == 2
    with pytest.raises(ValidationError):
        workflow.call('Supervisor', 'Choose a step.', Decision, {})
    assert len(calls) == 3


def test_invalid_citation_is_revised_without_retrieval_or_verifier_call():
    from test_research_graph import Client, Library
    from deepsearch_agent.qa import AnswerService
    client = Client([
        {'queries': ['signal']},
        {'claims': [{'text': 'white light', 'citations': [1, 2, 3, 4, 5]}]},
        {'claims': [{'text': 'white light', 'citations': [1]}]},
        {'supported': [0], 'queries': []},
    ])
    result = AnswerService(client, Library(), use_graph=True).answer('signal?')
    assert result.text == 'white light [1]'
    assert len(result.sources) == 1


def test_exhausted_ocr_is_not_advertised_to_next_planner():
    from deepsearch_agent.agent_config import AgentSettings
    from deepsearch_agent.agentic_research import AgenticResearcher
    from deepsearch_agent.tool_service import PaperToolSession
    kb = SimpleNamespace(chunks=[], image_index=None)
    session = PaperToolSession(kb, settings=AgentSettings(max_ocr_calls=1))
    session.ocr_calls = 1
    researcher = AgenticResearcher(session, None)
    data = researcher._planner_data('q', {}, [
        {'name': 'ocr_page', 'arguments': {}}, {'name': 'search_evidence', 'arguments': {}}],
        {'steps': 0, 'observations': []})
    assert [t['name'] for t in data['available_tools']] == ['search_evidence']


def test_benchmark_contract_covers_planner_scope_and_requested_elements_only():
    from deepsearch_agent.benchmarks.ablation import format_instruction
    contract = format_instruction('B')
    assert 'provided page' in contract
    assert 'ONLY' in contract
    assert 'supporting' in contract


def test_ablation_output_budget_is_configurable(monkeypatch):
    from pathlib import Path
    from deepsearch_agent.benchmarks.ablation import vision_settings
    from deepsearch_agent.llm_config import LLMSettings
    from deepsearch_agent.vision_config import VisionSettings
    settings = VisionSettings(llm=LLMSettings(context_window=32768))
    monkeypatch.setattr(LLMSettings, 'load', lambda *args, **kwargs: settings.llm)
    monkeypatch.setattr(VisionSettings, 'load', lambda *args, **kwargs: settings)
    args = SimpleNamespace(vision_env=Path('unused'), llm_env=Path('unused'), max_output_tokens=8192)
    assert vision_settings(args).llm.max_output_tokens == 8192
    del args.max_output_tokens
    assert vision_settings(args).llm.max_output_tokens == 4096
