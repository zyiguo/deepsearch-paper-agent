"""The only RAG-to-storage bridge; no cloud or training lifecycle here."""
from contextlib import contextmanager
import logging

from search_policy.events import bind
from search_policy.store import TraceStore

logger = logging.getLogger(__name__)


class RAGTraceRecorder:
    def __init__(self, path, *, metadata=None):
        self.path = path
        self.metadata = dict(metadata or {})

    @contextmanager
    def capture(self, metadata):
        try:
            store = TraceStore(self.path)
            run_id = store.start({**self.metadata, **metadata})
        except Exception:
            logger.warning('rag_trace_start_failed')
            # Also hide any outer recorder if this capture could not start.
            with bind(None):
                yield None
            return

        broken = False

        def observe(kind, payload):
            nonlocal broken
            if broken:
                return
            try:
                store.append(run_id, kind, payload)
            except Exception:
                broken = True
                logger.warning('rag_trace_write_failed')

        status = 'completed'
        try:
            with bind(observe):
                yield observe
        except BaseException as error:
            status = 'error'
            observe('run.error', {'error_type': type(error).__name__})
            raise
        finally:
            try:
                store.finish(run_id, 'recording_error' if broken else status)
            except Exception:
                # A failed final commit leaves a running trace, excluded from exports.
                logger.warning('rag_trace_finish_failed')
