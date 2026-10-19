"""Crash windows measure requests, effects and charges independently."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from deepsearch_agent.adapters.persistence.tool_ledger import SQLiteToolLedger
from deepsearch_agent.application.tool_execution import ToolExecutor
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked, ToolKind


class PowerLoss(BaseException):
    pass


def executor(tmp_path, fault=None, run='run'):
    ledger = SQLiteToolLedger(tmp_path / 'tool-ledger.sqlite3')
    ledger.begin_run(run)
    return ledger, ToolExecutor(ledger, run, fault=fault)


def crash_at(stage):
    def fault(point, operation_id):
        if point == stage:
            raise PowerLoss()
    return fault


@pytest.mark.parametrize('kind', list(ToolKind))
def test_result_commit_before_checkpoint_reuses_without_second_request(tmp_path, kind):
    ledger, worker = executor(tmp_path, crash_at('after_commit'))
    requests = []
    def invoke(ident):
        requests.append(ident)
        return {'value': 12}
    with pytest.raises(PowerLoss):
        worker.execute('round0/step0', 'tool', kind, {'q': 'value'}, invoke)
    ledger = SQLiteToolLedger(ledger.path)
    ledger.recover_interrupted()
    result = ToolExecutor(ledger, 'run').execute('round0/step0', 'tool', kind, {'q': 'value'}, invoke)
    assert result.replayed and result.value == {'value': 12} and len(requests) == 1


@pytest.mark.parametrize('kind', list(ToolKind))
def test_claim_before_dispatch_is_proven_not_sent(tmp_path, kind):
    ledger, worker = executor(tmp_path, crash_at('after_claim'))
    requests = []
    with pytest.raises(PowerLoss):
        worker.execute('step', 'tool', kind, {}, lambda ident: requests.append(ident))
    assert requests == []
    ledger.recover_interrupted('run')
    assert ledger.list_run('run')[0]['status'] == 'pending'
    ToolExecutor(ledger, 'run').execute('step', 'tool', kind, {}, lambda ident: requests.append(ident))
    assert len(requests) == 1


@pytest.mark.parametrize('kind', [ToolKind.PAID, ToolKind.EFFECTFUL])
@pytest.mark.parametrize('window', ['after_dispatch', 'after_effect'])
def test_unconfirmed_nonreadonly_blocks_reexecution(tmp_path, kind, window):
    ledger, worker = executor(tmp_path, crash_at(window))
    requests, effects, charges = [], [], []
    def invoke(ident):
        requests.append(ident)
        (charges if kind == ToolKind.PAID else effects).append(ident)
        return {'ok': True}
    with pytest.raises(PowerLoss):
        worker.execute('step', 'tool', kind, {}, invoke)
    ledger.recover_interrupted('run')
    for _ in range(2):
        with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown'):
            ToolExecutor(ledger, 'run').execute('step', 'tool', kind, {}, invoke)
    assert ledger.list_run('run')[0]['status'] == 'unknown'
    assert len(requests) == len(effects) + len(charges) == (1 if window == 'after_effect' else 0)


def test_readonly_unknown_can_repeat_request_without_business_effect(tmp_path):
    ledger, worker = executor(tmp_path, crash_at('after_effect'))
    requests = []
    def invoke(ident):
        requests.append(ident)
        return ['evidence']
    with pytest.raises(PowerLoss):
        worker.execute('step', 'read', ToolKind.READ_ONLY, {}, invoke)
    ledger.recover_interrupted('run')
    result = ToolExecutor(ledger, 'run').execute('step', 'read', ToolKind.READ_ONLY, {}, invoke)
    assert result.value == ['evidence'] and len(requests) == 2


def test_provider_idempotency_repeats_request_but_not_effect_or_charge(tmp_path):
    ledger, worker = executor(tmp_path, crash_at('after_effect'))
    requests, provider = [], {}
    def invoke(ident):
        requests.append(ident)
        return provider.setdefault(ident, {'receipt': 'single-payment'})
    with pytest.raises(PowerLoss):
        worker.execute('step', 'pay', ToolKind.EFFECTFUL, {}, invoke, idempotency_contract='provider-v1')
    ledger.recover_interrupted('run')
    result = ToolExecutor(ledger, 'run').execute('step', 'pay', ToolKind.EFFECTFUL, {}, invoke,
                                               idempotency_contract='provider-v1')
    assert result.value == {'receipt': 'single-payment'}
    assert len(requests) == 2 and requests[0] == requests[1] and len(provider) == 1


def test_intent_identity_is_step_based_and_rejects_changed_arguments_or_policy(tmp_path):
    ledger, worker = executor(tmp_path)
    calls = []
    invoke = lambda ident: calls.append(ident)
    worker.execute('step1', 'read', ToolKind.READ_ONLY, {'q': 1}, invoke)
    worker.execute('step2', 'read', ToolKind.READ_ONLY, {'q': 1}, invoke)
    assert len(calls) == 2 and calls[0] != calls[1]
    for kind, arguments, capability in [(ToolKind.READ_ONLY, {'q': 2}, ''),
                                         (ToolKind.EFFECTFUL, {'q': 1}, ''),
                                         (ToolKind.READ_ONLY, {'q': 1}, 'new-provider')]:
        with pytest.raises(ToolExecutionBlocked, match='tool_operation_conflict'):
            worker.execute('step1', 'read', kind, arguments, invoke, idempotency_contract=capability)
    assert len(calls) == 2


def test_concurrent_claim_allows_only_one_active_invocation(tmp_path):
    ledger, worker = executor(tmp_path)
    entered, release = Event(), Event()
    calls = []
    def invoke(ident):
        calls.append(ident)
        entered.set()
        assert release.wait(5)
        return {'ok': True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker.execute, 'step', 'write', ToolKind.EFFECTFUL, {}, invoke)
        assert entered.wait(5)
        try:
            with pytest.raises(ToolExecutionBlocked, match='tool_operation_running'):
                ToolExecutor(SQLiteToolLedger(ledger.path), 'run').execute('step', 'write', ToolKind.EFFECTFUL, {}, invoke)
        finally:
            release.set()
        assert first.result().value == {'ok': True}
    assert len(calls) == 1


def test_fencing_rejects_late_old_result_and_unknown_is_not_a_timeout_retry(tmp_path):
    ledger, worker = executor(tmp_path)
    ident = ledger.prepare('run', 'step', 'write', ToolKind.EFFECTFUL, {}, '')
    owner = ledger.claim(ident)
    ledger.dispatch(ident, owner)
    ledger.recover_interrupted('run')
    with pytest.raises(ToolExecutionBlocked, match='tool_execution_fenced'):
        ledger.succeed(ident, owner, {'late': True})
    assert ledger.get(ident)['status'] == 'unknown'


def test_async_unknown_propagates_and_does_not_repeat_effect(tmp_path):
    ledger, worker = executor(tmp_path)
    calls = []
    async def invoke(ident):
        calls.append(ident)
        raise TimeoutError('remote effect may have happened; private detail')
    with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown') as error:
        asyncio.run(worker.aexecute('step', 'external', ToolKind.EFFECTFUL, {}, invoke))
    assert 'private detail' not in str(error.value)
    with pytest.raises(ToolExecutionBlocked, match='tool_outcome_unknown'):
        asyncio.run(worker.aexecute('step', 'external', ToolKind.EFFECTFUL, {}, invoke))
    assert len(calls) == 1


def test_ledger_failure_blocks_before_tool_call(tmp_path, monkeypatch):
    ledger, worker = executor(tmp_path)
    def broken(*args):
        raise OSError('private path')
    monkeypatch.setattr(ledger, 'prepare', broken)
    with pytest.raises(ToolExecutionBlocked, match='tool_ledger_unavailable'):
        worker.execute('step', 'write', ToolKind.EFFECTFUL, {}, lambda ident: pytest.fail('must not call'))


def test_cancellation_keeps_task_cancelled_and_tool_outcome_unknown(tmp_path):
    from deepsearch_agent.core.cancellation import ResearchCancelled
    ledger, worker = executor(tmp_path)
    def invoke(ident):
        raise ResearchCancelled('research_cancelled')
    with pytest.raises(ResearchCancelled):
        worker.execute('step', 'paid', ToolKind.PAID, {}, invoke)
    assert ledger.list_run('run')[0]['status'] == 'unknown'


def test_cancelled_token_prevents_dispatch_even_for_replayed_plans(tmp_path):
    from deepsearch_agent.core.cancellation import CancellationToken, ResearchCancelled, current_token
    ledger, worker = executor(tmp_path)
    token = CancellationToken()
    token.cancel()
    reset = current_token.set(token)
    try:
        with pytest.raises(ResearchCancelled):
            worker.execute('step', 'paid', ToolKind.PAID, {}, lambda ident: pytest.fail('must not call'))
    finally:
        current_token.reset(reset)
    assert ledger.list_run('run') == []
