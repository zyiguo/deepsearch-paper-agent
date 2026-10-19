"""Application error adapter for the standard-library-only trace recorder."""
from deepsearch_tracing.recorder import RAGTraceRecorder as Recorder
from deepsearch_agent.core.models import ModelServiceError


def safe_model_diagnostics(error):
    return error.diagnostics() if isinstance(error, ModelServiceError) else None


class RAGTraceRecorder(Recorder):
    def __init__(self, path, *, metadata=None):
        super().__init__(path, metadata=metadata, error_adapter=safe_model_diagnostics)
