"""Durable ingestion steps and content-addressed intermediate artifacts.

The journal records intent before a potentially paid stage. An interrupted stage
without a committed artifact is unknown and requires an explicit retry consent.
"""
import json
from pathlib import Path

from deepsearch_agent.adapters.persistence.local_files import atomic_write
from deepsearch_agent.core.identity import fingerprint
from deepsearch_agent.core.tool_execution import ToolNotExecuted


class IngestionBlocked(ValueError):
    pass


class IngestionJournal:
    def __init__(self, root, context, *, confirm_unknown=False, fault=None):
        self.context = context
        self.identity = fingerprint(context)
        self.root = Path(root) / self.identity
        self.confirm_unknown = confirm_unknown
        self.fault = fault or (lambda stage, step: None)

    def _path(self, step, kind):
        return self.root / f'{step}.{kind}.json'

    def read(self, step):
        path = self._path(step, 'state')
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if value['identity'] != self.identity or value['step'] != step:
            raise IngestionBlocked('入库记录身份不一致，请核查后恢复')
        return value

    def _write(self, step, status, *, paid, attempt, **extra):
        prior = self.read(step)
        confirmations = list(prior.get('confirmed_unknown_attempts', [])) if prior else []
        if extra.get('retry_unknown_confirmed') and attempt not in confirmations:
            confirmations.append(attempt)
        value = dict(identity=self.identity, context=self.context, step=step, status=status,
                     paid=paid, attempt=attempt, confirmed_unknown_attempts=confirmations, **extra)
        atomic_write(self._path(step, 'state'), json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def _artifact(self, step):
        path = self._path(step, 'artifact')
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if (value['identity'] != self.identity or value['step'] != step
                or value['digest'] != fingerprint(value['value'])):
            raise IngestionBlocked('入库中间产物校验失败；不会重新产生模型费用')
        return value

    def run(self, step, call, *, paid=False):
        state = self.read(step)
        artifact = self._artifact(step)
        if artifact is not None:
            if state is None or state['status'] != 'completed':
                self._write(step, 'completed', paid=paid, attempt=state['attempt'] if state else 1)
            return artifact['value']
        if state and state['status'] == 'completed':
            raise IngestionBlocked('已完成的入库产物丢失；请恢复备份，不会自动重新计费')
        uncertain = bool(state and state['paid'] and state['status'] in {'running', 'unknown'})
        if uncertain and not self.confirm_unknown:
            raise IngestionBlocked('上次付费步骤结果未知；请核对服务账单后明确确认重试，可能重复计费')
        attempt = (state['attempt'] if state else 0) + 1
        self._write(step, 'running', paid=paid, attempt=attempt, retry_unknown_confirmed=uncertain)
        self.fault('after_dispatch', step)
        try:
            value = call()
            self.fault('after_effect', step)
            envelope = dict(identity=self.identity, step=step, digest=fingerprint(value), value=value)
            atomic_write(self._path(step, 'artifact'), json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode())
            self.fault('after_artifact', step)
            self._write(step, 'completed', paid=paid, attempt=attempt)
            return value
        except ToolNotExecuted:
            self._write(step, 'failed', paid=paid, attempt=attempt)
            raise
        except Exception:
            self._write(step, 'unknown' if paid else 'failed', paid=paid, attempt=attempt)
            raise

    def pending_unknown(self):
        """Read-only inspection; a complete artifact proves a lost state commit."""
        return [p.name.removesuffix('.state.json') for p in self.root.glob('*.state.json')
                if (value := self.read(p.name.removesuffix('.state.json')))['paid']
                and value['status'] in {'running', 'unknown'}
                and self._artifact(value['step']) is None]

    def publication(self, expected, metadata, *, pages_digest):
        self._write('publication', 'prepared', paid=False, attempt=1,
                    expected=expected, metadata=metadata, pages_digest=pages_digest)

    def published(self):
        state = self.read('publication')
        self._write('publication', 'completed', paid=False, attempt=1,
                    expected=state['expected'], metadata=state['metadata'], pages_digest=state['pages_digest'])
