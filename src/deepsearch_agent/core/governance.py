"""Trusted billing identity and resource context; standard library only."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

from deepsearch_agent.core.errors import ApplicationError
from deepsearch_agent.core.tool_execution import ToolNotExecuted


class GovernanceDenied(ToolNotExecuted, ApplicationError):
    def __init__(self, code, status_code=429):
        ValueError.__init__(self,code)
        self.code, self.replayed = code, False
        self.status_code, self.detail = status_code, code


@dataclass(frozen=True)
class UsageScope:
    user: str = 'local'
    workspace: str = 'local'
    run_id: str | None = None
    operation_id: str | None = None


current_governance = ContextVar('current_governance', default=None)
current_usage_scope = ContextVar('current_usage_scope', default=UsageScope())
reported_provider_usage = ContextVar('reported_provider_usage', default=None)


@contextmanager
def usage_context(service=None, **changes):
    scope_token = current_usage_scope.set(replace(current_usage_scope.get(), **changes))
    service_token = current_governance.set(service if service is not None else current_governance.get())
    try:
        yield current_usage_scope.get()
    finally:
        current_governance.reset(service_token)
        current_usage_scope.reset(scope_token)


def report_provider_usage(value):
    samples = reported_provider_usage.get()
    if samples is not None:
        total = value.get('total_tokens') if isinstance(value, dict) else None
        samples.append(total if type(total) is int and total >= 0 else None)
