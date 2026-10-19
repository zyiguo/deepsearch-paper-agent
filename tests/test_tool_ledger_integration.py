from contextlib import asynccontextmanager
import json
import sqlite3
from uuid import uuid4

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger
from deepsearch_agent.adapters.tool_adapters import langgraph_tools
from deepsearch_agent.agents.agentic_research import AgenticResearcher
from deepsearch_agent.agents.tool_service import PaperToolSession
from deepsearch_agent.application.tool_execution import ToolExecutor
from deepsearch_agent.core.tool_execution import ConfirmedToolResult, ToolExecutionBlocked, ToolKind
from deepsearch_agent.workspace.working_memory import WorkingMemoryStore
from test_agentic_integration import Library
from test_tool_execution_ledger import PowerLoss


def setup(tmp_path, *, library=None, memory=None, fault=None):
    ledger = SQLiteToolLedger(tmp_path / 'ledger.sqlite3')
    ledger.begin_run('run')
    session = PaperToolSession(library or Library(), working_memory=memory)
    session.execution = ToolExecutor(ledger, 'run', fault=fault)
    return ledger, session


def researcher(session, planner, **kwargs):
    return AgenticResearcher(session, planner, tools_factory=langgraph_tools, **kwargs)


def test_child_node_replay_restores_action_and_evidence_without_tool_or_planner_repeat(tmp_path):
    library = Library()
    requests, decisions = [], []
    original = library.search
    def search(*args, **kwargs):
        requests.append('search')
        return original(*args, **kwargs)
    library.search = search
    ledger, session = setup(tmp_path, library=library)
    actions = iter([{'tool': 'search_evidence', 'arguments': {'query': 'fact'}}, {'tool': '', 'arguments': {}}])
    def planner(instruction, schema, data):
        decisions.append(data)
        return schema.model_validate(next(actions))
    def fault(stage, ident):
        if stage == 'after_commit' and ledger.get(ident)['tool'] == 'search_evidence':
            raise PowerLoss()
    session.execution.fault = fault
    with pytest.raises(PowerLoss):
        researcher(session, planner).run('fact?', {'top_k': 1}, [])
    restarted, second = setup(tmp_path, library=library)
    restarted.recover_interrupted('run')
    evidence = researcher(second, planner).run('fact?', {'top_k': 1}, [])
    assert len(requests) == 1 and len(decisions) == 2
    assert evidence == library.chunks and second.evidence == library.chunks
    assert second.calls == 1
    third_ledger, third = setup(tmp_path, library=library)
    assert researcher(third, lambda *a: pytest.fail('completed planner must replay')).run('fact?', {'top_k': 1}, []) == library.chunks
    assert len(requests) == 1 and third.calls == 1


def test_paid_ocr_response_loss_preserves_unknown_and_blocks_child_retry(tmp_path):
    calls = []
    class Analyzer:
        identity = 'synthetic'
        def analyze(self, *args):
            calls.append('charge')
            raise TimeoutError('response was lost')
    ledger, session = setup(tmp_path)
    session.analyzer, session.image_resolver = Analyzer(), lambda *args: 'image'
    actions = iter([{'tool': 'ocr_page', 'arguments': {'source': 'a.pdf', 'page': 1}}])
    planner = lambda instruction, schema, data: schema.model_validate(next(actions))
    with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown'):
        researcher(session, planner).run('fact?', {}, [])
    _, restarted = setup(tmp_path)
    restarted.analyzer, restarted.image_resolver = Analyzer(), lambda *args: 'image'
    with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown'):
        researcher(restarted, lambda *a: pytest.fail('planner must replay')).run('fact?', {}, [])
    assert calls == ['charge']
    assert next(x for x in ledger.list_run('run') if x['tool'] == 'ocr_page')['status'] == 'unknown'


def test_working_memory_effect_completed_before_crash_is_not_replaced_again(tmp_path):
    memory = WorkingMemoryStore(tmp_path / 'memory').bind(str(uuid4()))
    revision = memory.read('plan')['revision']
    ledger, session = setup(tmp_path, memory=memory)
    actions = iter([{'tool': 'working_memory', 'arguments': {'action': 'replace', 'document': 'plan',
                    'expected_revision': revision, 'content': 'new plan'}}])
    def fault(stage, ident):
        if stage == 'after_effect' and ledger.get(ident)['tool'] == 'working_memory':
            raise PowerLoss()
    session.execution.fault = fault
    with pytest.raises(PowerLoss):
        researcher(session, lambda i,s,d: s.model_validate(next(actions))).run('fact?', {}, [])
    assert memory.read('plan')['content'] == 'new plan'
    restarted_ledger, restarted = setup(tmp_path, memory=memory)
    restarted_ledger.recover_interrupted('run')
    with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown'):
        researcher(restarted, lambda *a: pytest.fail('do not replan')).run('fact?', {}, [])
    assert memory.read('plan')['content'] == 'new plan'
    assert next(x for x in ledger.list_run('run') if x['tool'] == 'working_memory')['attempt'] == 1


def test_external_tool_unknown_is_not_swallowed_as_planner_feedback(tmp_path):
    class Args(BaseModel):
        pass
    requests = []
    async def write():
        requests.append('effect')
        raise TimeoutError('response lost')
    tool = StructuredTool(name='remote_write', description='External tool', args_schema=Args, coroutine=write)
    @asynccontextmanager
    async def external():
        yield [tool]
    planner = lambda i,s,d: s.model_validate({'tool': 'remote_write', 'arguments': {}})
    for _ in range(2):
        ledger, session = setup(tmp_path)
        with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown'):
            researcher(session, planner, external_tools=external).run('fact?', {}, [])
    assert requests == ['effect']
    assert next(x for x in ledger.list_run('run') if x['tool'] == 'remote_write')['kind'] == 'effectful'


def test_provider_query_confirms_unknown_without_repeating_write(tmp_path):
    ledger, session = setup(tmp_path)
    ident = ledger.prepare('run', 'step', 'write', ToolKind.EFFECTFUL, {}, '')
    owner = ledger.claim(ident)
    ledger.dispatch(ident, owner)
    ledger.uncertain(ident, owner)
    ex = session.execution
    for query in [lambda _: None, lambda _: {'not_found': True}]:
        with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown'):
            ex.reconcile(ident, query)
        assert ledger.get(ident)['status'] == 'unknown'
    result = ex.reconcile(ident, lambda key: ConfirmedToolResult({'receipt': key}))
    assert result.replayed and result.value == {'receipt': ident}
    replay = ex.execute('step', 'write', ToolKind.EFFECTFUL, {}, lambda _: pytest.fail('must not write'))
    assert replay.replayed and replay.value == result.value
    with pytest.raises(ToolExecutionBlocked, match='tool_operation_not_owned'):
        ToolExecutor(ledger, 'other-run').reconcile(ident, lambda _: ConfirmedToolResult(None))


def test_tool_classification_does_not_treat_paid_search_as_readonly(tmp_path):
    _, session = setup(tmp_path)
    assert session.tool_kind('search_evidence', {}) == ToolKind.READ_ONLY
    session.kb.embedding_client = object()
    assert session.tool_kind('search_evidence', {}) == ToolKind.PAID
    assert session.tool_kind('working_memory', {'action': 'read'}) == ToolKind.READ_ONLY
    assert session.tool_kind('working_memory', {'action': 'replace'}) == ToolKind.EFFECTFUL


def test_parent_graph_checkpoint_replays_research_node_without_second_tool_call(tmp_path):
    from deepsearch_agent.agents.runtime import ResearchWorkflow
    from deepsearch_agent.adapters.persistence.checkpoints import checkpoint_saver
    calls = []
    class Model:
        def complete(self, prompt):
            role = prompt.split('。', 1)[0]
            data = json.loads(prompt.split('\ndata: ', 1)[1])
            if role == '你是 Supervisor':
                return json.dumps({'action': 'research' if not data['research_rounds'] else
                    'finish' if data['verified'] else 'draft', 'tasks': [], 'instruction': 'verify'})
            if role == '你是 Researcher':
                calls.append('planner')
                return json.dumps({'tool': '' if data['observations'] else 'search_evidence',
                                   'arguments': {} if data['observations'] else {'query': 'fact'}})
            if role == '你是 Answerer':
                return json.dumps({'claims': [{'text': 'verified local fact', 'citations': [1]}]})
            return json.dumps({'supported': [0], 'queries': []})
    library = Library()
    original = library.search
    def search(*args, **kwargs):
        calls.append('search')
        return original(*args, **kwargs)
    library.search = search
    ledger, session = setup(tmp_path, library=library)
    def fault(stage, ident):
        if stage == 'after_commit' and ledger.get(ident)['tool'] == 'search_evidence':
            raise PowerLoss()
    session.execution.fault = fault
    with sqlite3.connect(tmp_path / 'checkpoints.sqlite3', check_same_thread=False) as connection:
        saver = checkpoint_saver(connection)
        workflow = ResearchWorkflow(Model(), library, tool_session=session, checkpointer=saver, run_id='run')
        with pytest.raises(PowerLoss):
            workflow.answer('fact?', 1, [])
        assert workflow.graph.get_state({'configurable': {'thread_id': 'run'}}).next == ('researcher',)
        restarted_ledger, restarted = setup(tmp_path, library=library)
        restarted_ledger.recover_interrupted('run')
        answer = ResearchWorkflow(Model(), library, tool_session=restarted, checkpointer=saver,
                                  run_id='run').answer('fact?', 1, [], resume=True)
    assert answer.text == 'verified local fact [1]'
    assert calls.count('search') == 1 and calls.count('planner') == 2


def test_operation_api_is_session_scoped_and_omits_private_payloads(tmp_path):
    from fastapi.testclient import TestClient
    from deepsearch_agent.application.bootstrap import create_app
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.application.session_runs import prepare_run
    from deepsearch_agent.application.commands import TurnCommand
    with TestClient(create_app(tmp_path / 'index.json', llm_settings=LLMSettings())) as client:
        client.post('/api/demo/ingest')
        a = client.post('/api/sessions', json={'title': 'A'}).json()['id']
        b = client.post('/api/sessions', json={'title': 'B'}).json()['id']
        state = client.app.state
        run, _ = prepare_run(a, TurnCommand(question='模型怎么训练？'), state)
        execution = ToolExecutor(state.tool_ledger, run)
        execution.execute('step', 'read', ToolKind.READ_ONLY, {'secret': 'private-query'},
                          lambda _: {'secret': 'private-result'})
        response = client.get(f'/api/sessions/{a}/runs/{run}/operations')
        assert response.status_code == 200 and response.json()[0]['status'] == 'succeeded'
        assert 'private-query' not in response.text and 'private-result' not in response.text
        assert client.get(f'/api/sessions/{b}/runs/{run}/operations').status_code == 404
        assert client.delete(f'/api/sessions/{a}').status_code == 409  # queued task still owns its records


def test_resume_after_completed_research_restores_memory_notes_and_budget(tmp_path):
    from test_agentic_integration import Planner
    from deepsearch_agent.agents.runtime import ResearchWorkflow
    from deepsearch_agent.adapters.persistence.checkpoints import checkpoint_saver
    memory = WorkingMemoryStore(tmp_path / 'memory').bind(str(uuid4()))
    revision = memory.read('history')['revision']
    memory.replace('history', 'User explicitly requested concise answers.', revision)
    library = Library()
    ledger, session = setup(tmp_path, memory=memory, library=library)
    first = Planner()
    first.outputs = iter([{'tool': 'working_memory', 'arguments': {'action': 'read', 'document': 'history'}},
                          {'tool': 'search_evidence', 'arguments': {'query': 'fact'}},
                          {'tool': '', 'arguments': {}}])
    complete = first.complete
    def fail_draft(prompt):
        if prompt.startswith('你是 Answerer。'):
            raise RuntimeError('injected interruption after research checkpoint')
        return complete(prompt)
    first.complete = fail_draft
    with sqlite3.connect(tmp_path / 'checkpoint.sqlite3', check_same_thread=False) as connection:
        saver = checkpoint_saver(connection)
        workflow = ResearchWorkflow(first, library, tool_session=session, checkpointer=saver, run_id='run')
        with pytest.raises(RuntimeError, match='injected interruption'):
            workflow.answer('fact?', 1, [])
        restarted_ledger, restarted = setup(tmp_path, memory=memory, library=library)
        restarted_ledger.recover_interrupted('run')
        second = Planner()
        second.outputs = iter([{'claims': [{'text': 'verified local fact', 'citations': [1]}]},
                               {'supported': [0], 'queries': []}])
        answer = ResearchWorkflow(second, library, tool_session=restarted, checkpointer=saver,
                                  run_id='run').answer('fact?', 1, [], resume=True)
    assert answer.text == 'verified local fact [1]'
    assert restarted.calls == 2 and restarted.memory_notes['history'] == 'User explicitly requested concise answers.'
    assert all('concise answers' in p for p in second.prompts if p.startswith('你是 Answerer。'))
    assert len(ledger.list_run('run')) == 5  # three decisions and two tools; no new child calls


def test_unknown_tool_is_visible_in_run_and_sse_and_cannot_resume_without_checkpoint(tmp_path):
    from fastapi.testclient import TestClient
    from deepsearch_agent.application.bootstrap import create_app
    from deepsearch_agent.adapters.llm_config import LLMSettings
    app = create_app(tmp_path / 'index.json', llm_settings=LLMSettings())
    effects = []
    with TestClient(app) as client:
        client.post('/api/demo/ingest')
        sid = client.post('/api/sessions', json={'title': 'ledger'}).json()['id']
        def answer(question, *, run_id, **kwargs):
            def invoke(ident):
                effects.append(ident)
                raise TimeoutError('response lost')
            return ToolExecutor(app.state.tool_ledger, run_id).execute('step', 'paid', ToolKind.PAID, {}, invoke)
        app.state.answer_service.answer = answer
        response = client.post(f'/api/sessions/{sid}/turns/stream', json={'question': '模型怎么训练？'})
        assert 'tool_outcome_unknown' in response.text
        run = client.get(f'/api/sessions/{sid}/runs').json()[0]
        assert run['status'] == 'interrupted' and run['request']['_execution']['error_code'] == 'tool_outcome_unknown'
        operations = client.get(f'/api/sessions/{sid}/runs/{run["id"]}/operations').json()
        assert operations[0]['status'] == 'unknown'
        assert client.post(f'/api/sessions/{sid}/runs/{run["id"]}/resume/stream').status_code == 409
        assert len(effects) == 1
        assert client.delete(f'/api/sessions/{sid}').status_code == 200
        assert app.state.tool_ledger.list_run(run['id']) == []


def test_ledger_files_survive_existing_backup_restore_path(tmp_path):
    from deepsearch_agent.adapters.persistence.recovery import create_bundle, restore_drill
    from deepsearch_agent.adapters.database_config import DatabaseSettings
    root = tmp_path / 'workspace'
    ledger, session = setup(root)
    session.execution.execute('step', 'write', ToolKind.EFFECTFUL, {}, lambda _: {'receipt': 'once'})
    create_bundle(tmp_path / 'backup', {'indexes': root}, DatabaseSettings())
    restore_drill(tmp_path / 'backup', tmp_path / 'restore')
    restored = SQLiteToolLedger(tmp_path / 'restore/files/indexes/ledger.sqlite3')
    result = ToolExecutor(restored, 'run').execute('step', 'write', ToolKind.EFFECTFUL, {},
                                                  lambda _: pytest.fail('restored result must replay'))
    assert result.replayed and result.value == {'receipt': 'once'}
