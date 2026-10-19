"""Claim, dispatch and persist tool outcomes; retries require an explicit safe policy."""
import json
from deepsearch_agent.core.cancellation import ResearchCancelled, current_token

from deepsearch_agent.core.tool_execution import ConfirmedToolResult, ExecutionResult, ToolExecutionBlocked, ToolKind, ToolLedger


class ToolExecutor:
    def __init__(self, ledger: ToolLedger, run_id, *, fault=None):
        self.ledger, self.run_id = ledger, run_id
        self.fault = fault or (lambda stage, operation_id: None)

    def _prepare(self, slot, tool, kind, arguments, idempotency_contract):
        token = current_token.get()
        if token is not None:
            token.check()
        try:
            ident = self.ledger.prepare(self.run_id, slot, tool, kind, arguments, idempotency_contract)
            row = self.ledger.get(ident)
            if row['status'] == 'succeeded':
                return ident, None, ExecutionResult(json.loads(row['result_json']), ident, True)
            if row['status'] == 'unknown' and kind != ToolKind.READ_ONLY and not idempotency_contract:
                raise ToolExecutionBlocked('tool_outcome_unknown', ident)
            owner = self.ledger.claim(ident, allow_unknown=kind == ToolKind.READ_ONLY or bool(idempotency_contract))
            if owner is None:
                # Another execution may have finished after the first read.
                row = self.ledger.get(ident)
                if row['status'] == 'succeeded':
                    return ident, None, ExecutionResult(json.loads(row['result_json']), ident, True)
                raise ToolExecutionBlocked('tool_outcome_unknown' if row['status'] == 'unknown' else 'tool_operation_running', ident)
            return ident, owner, None
        except ToolExecutionBlocked:
            raise
        except Exception:
            raise ToolExecutionBlocked('tool_ledger_unavailable') from None

    def _start(self, ident, owner):
        self.fault('after_claim', ident)
        token = current_token.get()
        if token is not None:
            token.check()
        try:
            self.ledger.dispatch(ident, owner)
        except ToolExecutionBlocked:
            raise
        except Exception:
            raise ToolExecutionBlocked('tool_ledger_unavailable', ident) from None
        self.fault('after_dispatch', ident)

    def _commit(self, ident, owner, value):
        self.fault('after_effect', ident)
        self.ledger.succeed(ident, owner, value)
        self.fault('after_commit', ident)
        return ExecutionResult(value, ident, False)

    def _uncertain(self, ident, owner, kind):
        try:
            self.ledger.uncertain(ident, owner)
        except Exception:
            raise ToolExecutionBlocked('tool_ledger_unavailable', ident) from None

    def execute(self, slot, tool, kind, arguments, invoke, *, idempotency_contract=''):
        """invoke receives the stable operation ID; capability is trusted adapter config."""
        ident, owner, replay = self._prepare(slot, tool, kind, arguments, idempotency_contract)
        if replay is not None:
            return replay
        self._start(ident, owner)
        try:
            return self._commit(ident, owner, invoke(ident))
        except Exception as error:
            self._uncertain(ident, owner, kind)
            if kind != ToolKind.READ_ONLY and not isinstance(error, ResearchCancelled):
                raise ToolExecutionBlocked('tool_outcome_unknown', ident) from None
            raise

    async def aexecute(self, slot, tool, kind, arguments, invoke, *, idempotency_contract=''):
        ident, owner, replay = self._prepare(slot, tool, kind, arguments, idempotency_contract)
        if replay is not None:
            return replay
        self._start(ident, owner)
        try:
            return self._commit(ident, owner, await invoke(ident))
        except Exception as error:
            self._uncertain(ident, owner, kind)
            if kind != ToolKind.READ_ONLY and not isinstance(error, ResearchCancelled):
                raise ToolExecutionBlocked('tool_outcome_unknown', ident) from None
            raise

    def reconcile(self, operation_id, lookup):
        """A missing/timeout response remains unknown; no dispatch occurs here."""
        try:
            row = self.ledger.get(operation_id)
            if row['run_id'] != self.run_id:
                raise ToolExecutionBlocked('tool_operation_not_owned', operation_id)
            if row['status'] == 'succeeded':
                return ExecutionResult(json.loads(row['result_json']), operation_id, True)
            if row['status'] != 'unknown':
                raise ToolExecutionBlocked('tool_reconciliation_conflict', operation_id)
            proof = lookup(operation_id)
            if not isinstance(proof, ConfirmedToolResult):
                raise ToolExecutionBlocked('tool_outcome_unknown', operation_id)
            self.ledger.resolve(operation_id, proof.value)
            return ExecutionResult(proof.value, operation_id, True)
        except ToolExecutionBlocked:
            raise
        except Exception:
            raise ToolExecutionBlocked('tool_reconciliation_unavailable', operation_id) from None
