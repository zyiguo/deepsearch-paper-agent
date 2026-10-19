from fastapi.testclient import TestClient

from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.application.bootstrap import create_app
from deepsearch_agent.application.commands import TurnCommand
from deepsearch_agent.application.session_runs import prepare_run
from deepsearch_agent.core.tool_execution import ToolKind
from test_team_access import team, login, workspace, WRITE


def test_workspace_operation_endpoint_requires_membership(team):
    app, client, config = team
    login(client, 'alice')
    wid = workspace(client, 'Alice ledger')
    prefix = f'/w/{wid}/api'
    sid = client.post(prefix + '/sessions', json={'title': 'ledger'}, headers=WRITE).json()['id']
    response = client.post(prefix + f'/sessions/{sid}/turns', json={'question': '模型怎么训练？'}, headers=WRITE)
    assert response.status_code == 201
    run = client.get(prefix + f'/sessions/{sid}/runs').json()[0]['id']
    assert client.get(prefix + f'/sessions/{sid}/runs/{run}/operations').status_code == 200
    login(client, 'bob')
    assert client.get(prefix + f'/sessions/{sid}/runs/{run}/operations').status_code == 404


def test_legacy_checkpoint_without_ledger_is_rejected(tmp_path):
    with TestClient(create_app(tmp_path / 'index.json', llm_settings=LLMSettings())) as client:
        client.post('/api/demo/ingest')
        sid = client.post('/api/sessions', json={'title': 'legacy'}).json()['id']
        state = client.app.state
        run, _ = prepare_run(sid, TurnCommand(question='模型怎么训练？'), state)
        state.research_runs.update(run, 'interrupted', expected={'queued'})
        state.tool_ledger.delete_run(run)
        class ExistingCheckpoint:
            def get_tuple(self, config):
                return object()
        state.research_checkpointer = ExistingCheckpoint()
        response = client.post(f'/api/sessions/{sid}/runs/{run}/resume/stream')
        assert response.status_code == 409 and '工具执行账本' in response.json()['detail']
        assert state.research_runs.get(run, sid)['status'] == 'interrupted'


def test_workspace_ledger_file_is_recovered_at_startup_without_auto_dispatch(tmp_path):
    root = tmp_path / 'index.json'
    with TestClient(create_app(root, llm_settings=LLMSettings())) as first:
        ledger = first.app.state.tool_ledger
        ledger.begin_run('synthetic-run')
        ident = ledger.prepare('synthetic-run', 'step', 'paid', ToolKind.PAID, {}, '')
        owner = ledger.claim(ident)
        ledger.dispatch(ident, owner)
    with TestClient(create_app(root, llm_settings=LLMSettings())) as second:
        row = second.app.state.tool_ledger.get(ident)
        assert row['status'] == 'unknown' and row['attempt'] == 1
