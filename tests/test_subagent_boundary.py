import json

import pytest
from pydantic import ValidationError

from deepsearch_agent.agents.agent_config import AgentSettings
from deepsearch_agent.application.answer_service import AnswerService
from deepsearch_agent.agents.subagent_contracts import SubagentTask, SubagentResult, compact_results
from deepsearch_agent.core.tokens import estimate_tokens
from deepsearch_agent.agents.tool_service import PaperToolSession
from test_agentic_research import ToolSession, Planner, researcher
from test_agentic_integration import Library, Planner as WorkflowPlanner


def finish(chunk_id):
    return {'tool': '', 'arguments': {}, 'findings': [
        {'text': 'The reported value is 12.', 'evidence_ids': [chunk_id]}], 'unresolved': []}


def test_task_disallows_inherited_messages():
    with pytest.raises(ValidationError):
        SubagentTask(goal='Find value', history=[{'content': 'private'}])


def test_child_returns_only_validated_result_and_each_task_starts_fresh():
    session = ToolSession()
    actions = [{'tool': 'search_evidence', 'arguments': {'query': 'value'}}, finish(session.chunk.chunk_id)]
    planner = Planner(actions * 2)
    worker = researcher(session, planner)
    first = worker.run_task(SubagentTask(goal='Find value'), [])
    second = worker.run_task(SubagentTask(goal='Confirm value'), [session.chunk])
    assert first.status == second.status == 'completed'
    assert first.task_id != second.task_id
    assert first.verification == 'unverified'
    assert first.evidence_ids == second.evidence_ids == [session.chunk.chunk_id]
    assert planner.contexts[0]['observations'] == planner.contexts[2]['observations'] == []
    assert 'messages' not in first.model_dump() and 'observations' not in first.model_dump()
    assert SubagentResult.model_validate_json(first.model_dump_json()) == first


def test_unknown_citation_cannot_become_authoritative_evidence():
    session = ToolSession()
    planner = Planner([{'tool': 'search_evidence', 'arguments': {'query': 'value'}}, finish('chk_' + 'f' * 64)])
    result = researcher(session, planner).run_task(SubagentTask(goal='Value?'), [])
    assert result.status == 'partial' and not result.findings
    assert 'invalid_evidence_reference' in result.unresolved
    assert result.evidence_ids == [session.chunk.chunk_id]


def test_budget_stop_reports_partial_without_invented_summary():
    session = ToolSession(budget=1)
    planner = Planner([{'tool': 'search_evidence', 'arguments': {'query': 'value'}}])
    result = researcher(session, planner).run_task(SubagentTask(goal='Value?'), [])
    assert result.status == 'partial'
    assert result.stop_reason == 'call_budget' and result.tool_calls == 1
    assert not result.findings and result.unresolved


def test_child_context_preserves_request_cancellation_token():
    from deepsearch_agent.core.cancellation import CancellationToken, current_token
    session = ToolSession()
    token = CancellationToken()
    reset = current_token.set(token)
    actions = iter([{'tool': 'search_evidence', 'arguments': {'query': 'value'}},
                    finish(session.chunk.chunk_id)])
    seen = []

    def planner(instruction, schema, data):
        seen.append(current_token.get())
        return schema.model_validate(next(actions))

    try:
        assert researcher(session, planner).run_task(SubagentTask(goal='Value?'), []).status == 'completed'
    finally:
        current_token.reset(reset)
    assert seen == [token, token]


def test_main_receives_findings_child_receives_goal_without_parent_history_or_notes():
    library = Library()
    noise = 'PRIVATE_TOOL_LOG_DO_NOT_FORWARD'

    class NoisySession(PaperToolSession):
        def search_evidence(self, *args, **kwargs):
            result = super().search_evidence(*args, **kwargs)
            result.content = json.dumps({'diagnostic': noise, 'hits': result.data})
            return result

    class Client(WorkflowPlanner):
        def __init__(self):
            super().__init__()
            self.outputs = iter([
                {'tool': 'search_evidence', 'arguments': {'query': 'fact'}},
                {'tool': '', 'arguments': {}, 'findings': [
                    {'text': 'verified local fact', 'evidence_ids': [library.chunks[0].chunk_id]}]},
                {'claims': [{'text': 'verified local fact', 'citations': [1]}]},
                {'supported': [0], 'queries': []},
            ])

        def complete(self, prompt):
            if prompt.startswith('你是 Supervisor。'):
                self.prompts.append(prompt)
                state = json.loads(prompt.split('\ndata: ', 1)[1])
                action = 'research' if not state['research_rounds'] else 'draft' if not state['verified'] else 'finish'
                return json.dumps({'action': action, 'tasks': [], 'instruction': 'Find the local fact',
                    'research_goal': 'Find the fact in the selected local paper',
                    'research_constraints': ['Use the selected paper only.']})
            return super().complete(prompt)

    client = Client()
    events = []
    result = AnswerService(client, library, use_graph=True,
        agent_settings=AgentSettings(enabled=True),
        tool_session_factory=lambda kb, **kwargs: NoisySession(kb, **kwargs))._answer('What about it?', 5,
            history=[{'role': 'user', 'content': 'PRIVATE_PARENT_HISTORY'}],
            memory_context={'language': 'PRIVATE_MEMORY_CONTEXT'},
            observer=lambda kind, value: events.append((kind, value)))
    assert result.text == 'verified local fact [1]'
    children = [p for p in client.prompts if p.startswith('你是 Researcher。')]
    parents = [p for p in client.prompts if p.startswith('你是 Supervisor。')]
    assert any(noise in p for p in children)  # Private tool feedback remains usable by child.
    assert all('PRIVATE_PARENT_HISTORY' not in p and 'PRIVATE_MEMORY_CONTEXT' not in p for p in children)
    assert all(noise not in p for p in parents)
    child_data = json.loads(children[0].split('\ndata: ', 1)[1])
    assert child_data['question'] == 'Find the fact in the selected local paper'
    assert 'history' not in child_data['context'] and 'evidence' not in child_data['context']
    parent_data = json.loads(parents[1].split('\ndata: ', 1)[1])
    assert parent_data['evidence'] == []
    assert parent_data['subtask_results'][0]['findings'][0]['text'] == 'verified local fact'
    assert parent_data['subtask_results'][0]['verification'] == 'unverified'
    assert any(kind == 'subagent.result' for kind, _ in events)
    assert any(p.startswith('你是 Verifier。') for p in client.prompts)


def test_parent_summary_budget_preserves_complete_ids():
    result = SubagentResult(task_id='a' * 32, status='partial', stop_reason='step_budget', tool_calls=8,
        findings=[{'text': 'long finding ' * 70, 'evidence_ids': ['chk_' + str(i) * 64]} for i in range(6)],
        evidence_ids=['chk_' + str(i) * 64 for i in range(6)], unresolved=['unknown ' * 100] * 8).model_dump()
    compact = compact_results([result] * 3, 700)
    assert compact
    assert estimate_tokens(json.dumps(compact, ensure_ascii=False)) <= 700
    assert 'evidence_ids' not in compact[0]  # Full reference list stays outside coordinator context.
    for item in compact:
        for finding in item['findings']:
            assert all(len(ident) == 68 for ident in finding['evidence_ids'])


def test_parent_checkpoint_resumes_with_result_without_child_history(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver
    from deepsearch_agent.agents.runtime import ResearchWorkflow

    library = Library()

    class Interrupted(WorkflowPlanner):
        def complete(self, prompt):
            if prompt.startswith('你是 Supervisor。'):
                state = json.loads(prompt.split('\ndata: ', 1)[1])
                if state['research_rounds']:
                    raise RuntimeError('simulated interruption after child completion')
            return super().complete(prompt)

    with SqliteSaver.from_conn_string(str(tmp_path / 'checkpoints.sqlite3')) as saver:
        first = ResearchWorkflow(Interrupted(), library, tool_session=PaperToolSession(library),
                                 checkpointer=saver, run_id='isolated-run')
        with pytest.raises(RuntimeError, match='simulated interruption'):
            first.answer('fact?', 5, [])
        snapshot = first.graph.get_state({'configurable': {'thread_id': 'isolated-run'}})
        assert snapshot.next == ('supervisor',)
        assert snapshot.values['subtask_results'][0]['verification'] == 'unverified'
        assert 'observations' not in snapshot.values and 'messages' not in snapshot.values
        client = WorkflowPlanner()
        client.outputs = iter([{'claims': [{'text': 'verified local fact', 'citations': [1]}]},
                               {'supported': [0], 'queries': []}])
        resumed = ResearchWorkflow(client, library, tool_session=PaperToolSession(library),
                                   checkpointer=saver, run_id='isolated-run')
        assert resumed.answer('fact?', 5, [], resume=True).text == 'verified local fact [1]'
        assert not any(p.startswith('你是 Researcher。') for p in client.prompts)
