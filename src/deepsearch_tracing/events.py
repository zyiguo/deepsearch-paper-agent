"""Request-local telemetry: no application or storage dependency."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging

_observer = ContextVar('search_policy_observer', default=None)


def emit(kind, payload):
    observer = _observer.get()
    if observer is not None:
        try:
            observer(kind, payload)
        except Exception:
            # Never print payloads, paths, provider errors or credentials.
            logging.getLogger(__name__).warning('policy_trace_write_failed')


@contextmanager
def bind(observer):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)
