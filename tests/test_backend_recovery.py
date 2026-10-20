import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from deepsearch_agent.agents.tool_service import ToolError
from deepsearch_agent.application.bootstrap import create_app
from deepsearch_agent.application.commands import TurnCommand
from deepsearch_agent.application.session_runs import prepare_run
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.application.tool_execution import ToolExecutor
from deepsearch_agent.core.tool_execution import ToolKind, ToolNotExecuted
from deepsearch_agent.workspace.working_memory import WorkingMemoryStore
from test_tool_ledger_integration import setup
from test_team_access import team, login, workspace, WRITE


def test_fresh_memory_handle_replayed_read_authorizes_write_and_replayed_write_revokes(tmp_path):
    store, sid = WorkingMemoryStore(tmp_path / 'memory'), str(uuid4())
    ledger, first = setup(tmp_path, memory=store.bind(sid))
    first.operation_slot = 'read'
    revision = first.invoke('working_memory', {'action': 'read', 'document': 'plan'}).data['revision']
    _, second = setup(tmp_path, memory=store.bind(sid))
    second.operation_slot = 'read'
    assert second.invoke('working_memory', {'action': 'read', 'document': 'plan'}).data['revision'] == revision
    args = {'action': 'replace', 'document': 'plan', 'expected_revision': revision, 'content': 'done'}
    second.operation_slot = 'write'
    second.invoke('working_memory', args)
    _, third = setup(tmp_path, memory=store.bind(sid))
    third.operation_slot = 'read'
    third.invoke('working_memory', {'action': 'read', 'document': 'plan'})
    third.operation_slot = 'write'
    third.invoke('working_memory', args)
    assert third.memory.read_revisions == {}
    third.operation_slot = 'illegal-write'
    with pytest.raises(ToolError, match='memory_read_required'):
        third.invoke('working_memory', args)
    assert [r['status'] for r in ledger.list_run('run')].count('not_executed') == 1
    assert store.bind(sid).read('plan')['content'] == 'done'


def test_stale_replayed_read_cannot_overwrite_and_rejection_is_durable(tmp_path):
    store, sid = WorkingMemoryStore(tmp_path / 'memory'), str(uuid4())
    ledger, first = setup(tmp_path, memory=store.bind(sid))
    first.operation_slot = 'read'
    args = {'action': 'read', 'document': 'plan'}
    revision = first.invoke('working_memory', args).data['revision']
    editor = store.bind(sid)
    editor.replace('plan', 'manual change', editor.read('plan')['revision'])
    for _ in range(2):
        _, resumed = setup(tmp_path, memory=store.bind(sid))
        resumed.operation_slot = 'read'
        resumed.invoke('working_memory', args)
        resumed.operation_slot = 'write'
        with pytest.raises(ToolError, match='memory_conflict'):
            resumed.invoke('working_memory', {'action': 'replace', 'document': 'plan',
                'expected_revision': revision, 'content': 'stale'})
    row = next(r for r in ledger.list_run('run') if r['slot'] == 'write')
    assert row['status'] == 'not_executed' and row['attempt'] == 1
    assert store.bind(sid).read('plan')['content'] == 'manual change'


def test_async_proven_rejection_replays_without_invoking(tmp_path):
    import asyncio
    ledger, session = setup(tmp_path)
    calls = []
    async def reject(_):
        calls.append(1)
        raise ToolNotExecuted('provider_not_contacted')
    async def exercise():
        for _ in range(2):
            with pytest.raises(ToolNotExecuted, match='provider_not_contacted'):
                await session.execution.aexecute('async', 'write', ToolKind.EFFECTFUL, {}, reject)
    asyncio.run(exercise())
    assert calls == [1] and ledger.list_run('run')[0]['status'] == 'not_executed'


def test_cancellation_after_claim_proves_dispatch_did_not_start(tmp_path):
    from deepsearch_agent.core.cancellation import CancellationToken, ResearchCancelled, current_token
    ledger, session = setup(tmp_path)
    token = CancellationToken()
    session.execution.fault = lambda stage, _: token.cancel() if stage == 'after_claim' else None
    reset = current_token.set(token)
    try:
        with pytest.raises(ResearchCancelled):
            session.execution.execute('cancel', 'write', ToolKind.EFFECTFUL, {}, lambda _: pytest.fail('not dispatched'))
    finally:
        current_token.reset(reset)
    row = ledger.list_run('run')[0]
    assert row['status'] == 'not_executed' and row['rejection_code'] == 'cancelled_before_dispatch'


def test_checkpoint_snapshot_restores_memory_read_capability(tmp_path):
    store, sid = WorkingMemoryStore(tmp_path / 'memory'), str(uuid4())
    ledger, first = setup(tmp_path, memory=store.bind(sid))
    first.operation_slot = 'read'
    revision = first.invoke('working_memory', {'action': 'read', 'document': 'plan'}).data['revision']
    snapshot = first.runtime_snapshot()
    _, second = setup(tmp_path, memory=store.bind(sid))
    second.restore_runtime(snapshot)
    second.operation_slot = 'write'
    second.invoke('working_memory', {'action': 'replace', 'document': 'plan', 'expected_revision': revision, 'content': 'saved after checkpoint'})
    assert second.memory.read_revisions == {}
    assert second.memory.read('plan')['content'] == 'saved after checkpoint'


def test_memory_clear_revokes_cached_and_checkpoint_reads_even_if_content_is_identical(tmp_path):
    store, sid = WorkingMemoryStore(tmp_path / 'memory'), str(uuid4())
    _, first = setup(tmp_path, memory=store.bind(sid))
    first.operation_slot = 'read'
    args = {'action': 'read', 'document': 'plan'}
    revision = first.invoke('working_memory', args).data['revision']
    snapshot = first.runtime_snapshot()
    store.clear(sid)
    restarted_store = WorkingMemoryStore(tmp_path / 'memory')
    ledger, second = setup(tmp_path, memory=restarted_store.bind(sid))
    second.restore_runtime(snapshot)
    second.operation_slot = 'read'
    assert second.invoke('working_memory', args).data['revision'] == revision
    assert second.memory.read_revisions == {}
    second.operation_slot = 'write'
    with pytest.raises(ToolError, match='memory_read_required'):
        second.invoke('working_memory', {'action': 'replace', 'document': 'plan', 'expected_revision': revision, 'content': 'revoked'})
    assert ledger.list_run('run')[-1]['status'] == 'not_executed'


def test_memory_read_scope_survives_real_store_reconstruction(tmp_path):
    sid = str(uuid4())
    _, first = setup(tmp_path, memory=WorkingMemoryStore(tmp_path / 'memory').bind(sid))
    first.operation_slot = 'read'
    args = {'action': 'read', 'document': 'plan'}
    old = first.invoke('working_memory', args).data
    _, second = setup(tmp_path, memory=WorkingMemoryStore(tmp_path / 'memory').bind(sid))
    second.operation_slot = 'read'
    second.invoke('working_memory', args)
    second.operation_slot = 'write'
    second.invoke('working_memory', {'action': 'replace', 'document': 'plan', 'expected_revision': old['revision'], 'content': 'continued'})
    assert second.memory.read('plan')['content'] == 'continued'


def unknown(state, sid, tool='remote_write', args=None):
    run, _ = prepare_run(sid, TurnCommand(question='模型怎么训练？'), state)
    state.research_runs.update(run, 'interrupted', expected={'queued'})
    ledger = state.tool_ledger
    ident = ledger.prepare(run, 'step', tool, ToolKind.EFFECTFUL, args or {}, '')
    owner = ledger.claim(ident)
    ledger.dispatch(ident, owner)
    ledger.uncertain(ident, owner)
    return run, ident


def test_operator_receipt_resolves_original_slot_and_has_audit(tmp_path):
    with TestClient(create_app(tmp_path / 'index.json', llm_settings=LLMSettings())) as client:
        client.post('/api/demo/ingest')
        sid = client.post('/api/sessions', json={'title': 'reconcile'}).json()['id']
        state = client.app.state
        run, ident = unknown(state, sid)
        url = f'/api/sessions/{sid}/runs/{run}/operations/{ident}'
        body = {'confirmed': True, 'proof_reference': 'provider receipt R-1', 'external_result': 'write complete'}
        assert client.post(url + '/reconcile', json={**body, 'confirmed': False}).status_code == 422
        assert state.tool_ledger.get(ident)['status'] == 'unknown'
        response = client.post(url + '/reconcile', json=body)
        assert response.status_code == 200, response.text
        assert response.json()['ready_to_resume']
        assert client.post(url + '/reconcile', json=body).status_code == 409
        result = ToolExecutor(state.tool_ledger, run).execute('step', 'remote_write', ToolKind.EFFECTFUL, {},
            lambda _: pytest.fail('confirmed effect must replay'))
        assert result.replayed and result.value == 'write complete'
        audit = client.get(url + '/audit').json()
        assert audit['reconciliations'][0]['actor'] == 'local-operator'
        assert audit['reconciliations'][0]['proof_reference'] == body['proof_reference']
        assert 'write complete' not in json.dumps(audit)
        other = client.post('/api/sessions', json={'title': 'other'}).json()['id']
        assert client.get(url.replace(sid, other) + '/audit').status_code == 404


def test_reconciled_operation_continues_original_run_through_resume_api(tmp_path, monkeypatch):
    from deepsearch_agent.core.answers import Answer
    from deepsearch_agent.application import session_runs
    with TestClient(create_app(tmp_path / 'index.json', llm_settings=LLMSettings())) as client:
        client.post('/api/demo/ingest')
        sid = client.post('/api/sessions', json={'title': 'continue original'}).json()['id']
        state = client.app.state
        run, ident = unknown(state, sid)
        prefix = f'/api/sessions/{sid}/runs/{run}'
        result = client.post(prefix + f'/operations/{ident}/reconcile', json={
            'confirmed': True, 'proof_reference': 'provider receipt R-2', 'external_result': 'confirmed'})
        assert result.status_code == 200
        class Checkpoint:
            def get_tuple(self, config):
                assert config['configurable']['thread_id'] == run
                return object()
        state.research_checkpointer = Checkpoint()
        def continue_run(session_id, body, runtime, on_progress=None, *, run_id, resume):
            assert run_id == run and resume
            replay = ToolExecutor(runtime.tool_ledger, run).execute('step', 'remote_write', ToolKind.EFFECTFUL, {},
                lambda _: pytest.fail('resolved operation must not execute again'))
            return runtime.conversations.append_turn(session_id, body.question, Answer(text=replay.value, sources=[]), run_id=run_id)
        monkeypatch.setattr(session_runs, 'generate_turn', continue_run)
        response = client.post(prefix + '/resume/stream')
        assert response.status_code == 200 and 'event: done' in response.text, response.text
        assert state.research_runs.get(run, sid)['status'] == 'completed'
        assert len(state.conversations.get(sid)['turns']) == 1
        assert state.tool_ledger.get(ident)['attempt'] == 1


def test_local_memory_readback_confirms_only_matching_content_and_never_injects_evidence(tmp_path):
    with TestClient(create_app(tmp_path / 'index.json', llm_settings=LLMSettings())) as client:
        client.post('/api/demo/ingest')
        sid = client.post('/api/sessions', json={'title': 'memory'}).json()['id']
        state = client.app.state
        memory = state.working_memory.bind(sid)
        revision = memory.read('plan')['revision']
        args = {'action': 'replace', 'document': 'plan', 'content': 'confirmed plan', 'expected_revision': revision}
        run, ident = unknown(state, sid, 'working_memory', args)
        url = f'/api/sessions/{sid}/runs/{run}/operations/{ident}/reconcile'
        body = {'confirmed': True, 'proof_reference': 'local memory readback'}
        assert client.post(url, json=body).status_code == 409
        memory.replace('plan', args['content'], revision)
        assert client.post(url, json=body).status_code == 200
        result = json.loads(state.tool_ledger.get(ident)['result_json'])
        assert result['data']['updated'] and result['chunks'] == []
        run, ident = unknown(state, sid, 'ocr_page')
        assert client.post(f'/api/sessions/{sid}/runs/{run}/operations/{ident}/reconcile',
            json={**body, 'external_result': 'fabricated evidence'}).status_code == 409


def test_reconciliation_storage_failure_remains_unknown_and_has_safe_diagnostic(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / 'index.json', llm_settings=LLMSettings())) as client:
        client.post('/api/demo/ingest')
        sid = client.post('/api/sessions', json={'title': 'storage failure'}).json()['id']
        state = client.app.state
        run, ident = unknown(state, sid)
        def unavailable(*args, **kwargs):
            raise OSError('private SQL and credentials')
        monkeypatch.setattr(state.tool_ledger, 'resolve', unavailable)
        response = client.post(f'/api/sessions/{sid}/runs/{run}/operations/{ident}/reconcile', json={
            'confirmed': True, 'proof_reference': 'receipt', 'external_result': 'done'})
        assert response.status_code == 503 and 'private SQL' not in response.text
        assert state.tool_ledger.get(ident)['status'] == 'unknown'


def test_reconciliation_writes_require_workspace_editor_and_record_authenticated_actor(team):
    app, client, _ = team
    login(client, 'alice')
    wid = workspace(client, 'Recovery')
    prefix = f'/w/{wid}/api'
    sid = client.post(prefix + '/sessions', json={'title': 'run'}, headers=WRITE).json()['id']
    import asyncio
    child = asyncio.run(app.state.workspace_runtimes.get(wid))
    run, ident = unknown(child.state, sid)
    url = prefix + f'/sessions/{sid}/runs/{run}/operations/{ident}/reconcile'
    body = {'confirmed': True, 'proof_reference': 'receipt', 'external_result': 'done'}
    assert client.put(f'/api/workspaces/{wid}/members', json={'username': 'bob', 'role': 'viewer'}, headers=WRITE).status_code == 200
    login(client, 'bob')
    assert client.post(url, json=body, headers=WRITE).status_code == 403
    login(client, 'alice')
    assert client.post(url, json=body, headers=WRITE).status_code == 200
    audit = client.get(url.removesuffix('reconcile') + 'audit').json()
    assert audit['reconciliations'][0]['actor'] == child.state.tool_ledger.audit(ident)['reconciliations'][0]['actor']
    assert audit['reconciliations'][0]['actor'] != 'local-operator'
