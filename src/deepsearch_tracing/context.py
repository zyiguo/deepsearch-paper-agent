"""Optional application events; observers must never affect answering."""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict

_event_context = ContextVar('rag_event_context', default=None)


def event_context():
    return dict(_event_context.get() or {})


@contextmanager
def event_scope(**fields):
    """Request-local IDs follow async tasks and tool threads, never model context."""
    token = _event_context.set({**event_context(), **fields})
    try:
        yield
    finally:
        _event_context.reset(token)


def notify(observer, kind, payload):
    if observer is not None:
        try:
            observer(kind, {**payload, **event_context()})
        except Exception:
            logging.getLogger(__name__).warning('rag_observer_failed')


def chunk_record(chunk):
    return asdict(chunk)
