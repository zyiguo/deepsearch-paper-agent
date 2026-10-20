"""Tool recovery contracts. A timeout is not evidence that an effect did not occur."""
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ToolKind(StrEnum):
    READ_ONLY = 'read_only'
    PAID = 'paid'
    EFFECTFUL = 'effectful'


class ToolExecutionBlocked(RuntimeError):
    def __init__(self, code, operation_id=''):
        self.code, self.operation_id = code, operation_id
        super().__init__(code)


class ToolNotExecuted(ValueError):
    """Trusted local precondition rejected the call before any external effect."""
    def __init__(self, code, *, replayed=False):
        self.code, self.replayed = code, replayed
        super().__init__(code)


@dataclass(frozen=True)
class ExecutionResult:
    value: object
    operation_id: str
    replayed: bool


@dataclass(frozen=True)
class ConfirmedToolResult:
    """Only a trusted provider status adapter may produce this proof envelope."""
    value: object


class ToolLedger(Protocol):
    def prepare(self, run_id, slot, tool, kind, arguments, capability): ...
    def claim(self, operation_id, *, allow_unknown=False): ...
    def dispatch(self, operation_id, owner): ...
    def succeed(self, operation_id, owner, result): ...
    def uncertain(self, operation_id, owner): ...
    def reject(self, operation_id, owner, code): ...
    def get(self, operation_id): ...
    def resolve(self, operation_id, result): ...
