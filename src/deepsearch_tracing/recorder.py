"""The only RAG-to-storage bridge; no cloud or training lifecycle here."""
from contextlib import contextmanager
import logging

from deepsearch_tracing.events import bind
from deepsearch_tracing.store import TraceStore
from deepsearch_tracing.context import event_context
from deepsearch_tracing.outcomes import classify

logger = logging.getLogger(__name__)


class RAGTraceRecorder:
    def __init__(self, path, *, metadata=None, error_adapter=None):
        self.path = path
        self.error_adapter = error_adapter
        self.metadata = dict(metadata or {})

    @contextmanager
    def capture(self, metadata):
        try:
            store = TraceStore(self.path)
            run_id = store.start({**self.metadata, **metadata, 'communication_trace_version': 1})
        except Exception:
            logger.warning('rag_trace_start_failed')
            # Also hide any outer recorder if this capture could not start.
            with bind(None):
                yield None
            return

        broken = False
        facts = {}

        def observe(kind, payload):
            nonlocal broken
            if broken:
                return
            try:
                store.append(run_id, kind, {**payload, **event_context()})
                if kind == 'rag.outcome':
                    facts['pipeline_outcome'] = payload
                elif kind == 'answer.final':
                    facts['answer_observed'] = True
                elif kind == 'rag.request':
                    facts['last_role'] = payload.get('role')
                elif kind == 'run.error':
                    facts['error_type'] = payload.get('error_type')
                    facts['error_diagnostics'] = payload.get('diagnostics')
            except Exception as error:
                broken = True
                logger.warning('rag_trace_write_failed error_type=%s', type(error).__name__)

        status = 'completed'
        try:
            with bind(observe):
                yield observe
        except BaseException as error:
            status = 'error'
            code = getattr(error, 'status_code', None)
            diagnostics = None
            if self.error_adapter is not None:
                try:
                    diagnostics = self.error_adapter(error)
                except Exception:
                    logger.warning('rag_trace_diagnostics_failed')
            observe('run.error', {'error_type': type(error).__name__,
                                  'status_code': code if type(code) is int else None,
                                  'diagnostics': diagnostics})
            raise
        finally:
            try:
                observe('run.classified', classify('recording_error' if broken else status, **facts))
                store.finish(run_id, 'recording_error' if broken else status)
            except Exception:
                # A failed final commit leaves a running trace, excluded from exports.
                logger.warning('rag_trace_finish_failed')
